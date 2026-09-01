"""Reconstructing per-game hand contents from GRE's stateful Full/Diff protocol.

gameStateMessage.type is GameStateType_Full exactly once per game (a complete
snapshot) then GameStateType_Diff for every subsequent message (only changed
zones/gameObjects, plus diffDeletedInstanceIds for removed objects). This is
materially more complex than any other parser in this package: it must carry
running state (a zoneId -> zone map, an instanceId -> object map) across an
entire game rather than parsing one event in isolation.

Only our own objects (ownerSeatId == our seat) ever reveal a grpId in this
data — opponent's hidden-zone cards stay as bare instance ids, which is
exactly what's needed since we only score our own deck's cards. The one
confirmed exception is ZoneType_Command: Brawl commanders are public
knowledge for both players from the start of the game, so an opponent's
commander does reveal its grpId (visibility Visibility_Public) even though
every other opponent object stays hidden — confirmed directly against real
archived Brawl matches, where the Command Zone is a single shared zone (no
zone-level ownerSeatId) holding both players' commanders, distinguished by
each object's own ownerSeatId.

That same zone isn't exclusively commanders, though: non-Card objects
(confirmed real: a Boon from a curse-like effect, an Emblem from a
planeswalker ultimate) also transiently pass through it, owned by whichever
player they affect, and reveal a grpId the same way. Their grpId doesn't
resolve to a card name — carddb has no entry for it — so
_update_opponent_commanders filters to GameObjectType_Card to keep them out
of opponent_commander_grp_ids.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_INBOUND_GRE_RE = re.compile(r"Match to \w+: GreToClientEvent\r?\n(\{[^\r\n]+\})")


@dataclass(frozen=True)
class GameRecord:
    match_id: str
    game_number: int
    opening_hand: frozenset[int]  # grpIds
    drawn: frozenset[int]  # grpIds drawn after the opening hand
    final_hand: frozenset[int]  # grpIds still in hand at the last known state
    mulligan_count: int
    last_turn: int | None  # None means the game ended before turnInfo ever appeared
    opponent_commander_grp_ids: frozenset[int]  # empty outside Brawl (no Command Zone)
    # Union of grpIds across every hand we sent back on a mulligan this game
    # (not the hand we kept — that's opening_hand). Empty if mulligan_count
    # is 0.
    mulliganed_hand_grp_ids: frozenset[int]
    # Land-use tracking, all our-own-seat only (same visibility filtering as
    # opening_hand/drawn above). Kept generic here — cross-referencing against
    # a specific decklist's basics/DFCs/abilities is card_stats.py's job.
    land_play_counts: dict[int, int]  # grpId -> times we played it as a land
    land_first_play_turn: dict[int, int]  # grpId -> turn of its first play this game
    cast_grp_ids: dict[int, int]  # grpId -> times we cast it as a spell
    ability_activation_counts: dict[tuple[int, int], int]  # (source grpId, ability id) -> times activated


@dataclass
class _Zone:
    zone_type: str | None
    owner_seat_id: int | None
    object_instance_ids: set[int] = field(default_factory=set)


@dataclass
class _Object:
    grp_id: int
    owner_seat_id: int | None
    zone_id: int | None
    type: str | None


def _iter_game_state_messages(span_text: str):
    """Every GameStateMessage in a GRE traffic span, in file order.

    A single GreToClientEvent blob can bundle several GRE messages together
    (e.g. a die roll alongside a game-state update); only the game-state ones
    are yielded.
    """
    for match in _INBOUND_GRE_RE.finditer(span_text):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            log.debug("unparsable GreToClientEvent, skipping")
            continue
        messages = (payload.get("greToClientEvent") or {}).get("greToClientMessages") or []
        for message in messages:
            if message.get("type") == "GREMessageType_GameStateMessage":
                state = message.get("gameStateMessage")
                if state:
                    yield state


class _GameTracker:
    """Mutable running state for one game, built incrementally from Full+Diff messages."""

    def __init__(self, our_seat_id: int):
        self.our_seat_id = our_seat_id
        self.game_number = 1
        self.zones: dict[int, _Zone] = {}
        self.objects: dict[int, _Object] = {}
        self.hand_zone_id: int | None = None
        self.opening_hand: set[int] | None = None
        self.drawn: set[int] = set()
        self.mulligan_count = 0
        # Union of grpIds across every hand sent back on a mulligan — each
        # captured from _last_hand_snapshot at the moment mulligan_count is
        # seen to increase, i.e. before that same diff's own zone/object
        # changes replace it with the fresh hand.
        self.mulliganed_hand_grp_ids: set[int] = set()
        # Land-use tracking — see GameRecord's matching fields for what each
        # one means; reset per-game in apply_full, preserved across a
        # mid-game reseed the same way drawn/mulliganed_hand_grp_ids are.
        self.land_play_counts: dict[int, int] = {}
        self.land_first_play_turn: dict[int, int] = {}
        self.cast_grp_ids: dict[int, int] = {}
        self.ability_activation_counts: dict[tuple[int, int], int] = {}
        # None until the first turnInfo message arrives — a game that ends
        # (e.g. an early concession) before this ever appears is turn 0: not
        # even a single turn was completed.
        self.last_turn: int | None = None
        # Continuously updated hand contents, used both to snapshot the
        # opening hand right before the first draw, and as the final-hand /
        # no-draw-fallback value once the game ends.
        self._last_hand_snapshot: set[int] = set()
        # Every opponent commander grpId seen in a Command Zone this game —
        # accumulated rather than snapshotted, since a commander that's cast
        # to the battlefield (and later dies back to the Command Zone) would
        # otherwise drop out of a point-in-time read.
        self.opponent_commander_grp_ids: set[int] = set()

    def _seed_zones_and_objects(self, message: dict) -> None:
        self.zones = {}
        self.objects = {}
        self._merge_zones(message.get("zones"))
        self._merge_objects(message.get("gameObjects"))
        self.hand_zone_id = next(
            (
                zone_id
                for zone_id, zone in self.zones.items()
                if zone.zone_type == "ZoneType_Hand" and zone.owner_seat_id == self.our_seat_id
            ),
            None,
        )
        self._update_opponent_commanders()

    def _update_opponent_commanders(self) -> None:
        for zone in self.zones.values():
            if zone.zone_type != "ZoneType_Command":
                continue
            for instance_id in zone.object_instance_ids:
                obj = self.objects.get(instance_id)
                if (
                    obj is not None
                    and obj.type == "GameObjectType_Card"
                    and obj.owner_seat_id not in (None, self.our_seat_id)
                ):
                    self.opponent_commander_grp_ids.add(obj.grp_id)

    def apply_full(self, message: dict) -> None:
        game_info = message.get("gameInfo") or {}
        self.game_number = game_info.get("gameNumber", 1)
        self.opening_hand = None
        self.drawn = set()
        self.mulligan_count = 0
        self.mulliganed_hand_grp_ids = set()
        self.last_turn = None
        self.opponent_commander_grp_ids = set()
        self.land_play_counts = {}
        self.land_first_play_turn = {}
        self.cast_grp_ids = {}
        self.ability_activation_counts = {}

        self._seed_zones_and_objects(message)
        self._last_hand_snapshot = self._current_hand_grp_ids()
        self._merge_players(message.get("players"))

    def reseed(self, message: dict) -> None:
        """A mid-game reconnect resync: another Full snapshot for the SAME
        gameNumber (confirmed against real archived data — stage is
        GameStage_Play, not GameStage_Start). Rebuild zone/object state from
        it, but keep opening_hand/drawn/mulligan_count/last_turn accumulated
        so far — this isn't a new game, so that history must survive.

        Any card visible in this snapshot's hand that isn't already known as
        the opening hand or an earlier draw must have been drawn during the
        resync gap (no draw annotation was seen for it, since resync
        snapshots carry no annotations) — counted as drawn rather than lost.
        """
        self._seed_zones_and_objects(message)
        fresh_hand = self._current_hand_grp_ids()
        already_known = (self.opening_hand or set()) | self.drawn
        self.drawn |= fresh_hand - already_known
        self._last_hand_snapshot = fresh_hand
        self._merge_players(message.get("players"))

    def apply_diff(self, message: dict) -> None:
        drawn_instance_ids = self._draw_instance_ids(message.get("annotations"))
        played_land_instance_ids = self._hand_exit_instance_ids(message.get("annotations"), "PlayLand")
        cast_spell_instance_ids = self._hand_exit_instance_ids(message.get("annotations"), "CastSpell")
        ability_activations = self._ability_activations(message.get("annotations"))

        # A mulligan's players[].mulliganCount bump and its hand-replacement
        # (diffDeletedInstanceIds/zones/gameObjects) land in the SAME diff —
        # confirmed against a real archived mulligan. So the hand sent back
        # is still sitting in _last_hand_snapshot right up until this same
        # message's own zone/object merge below overwrites it with the fresh
        # hand; must be captured before that happens.
        previous_mulligan_count = self.mulligan_count
        self._merge_players(message.get("players"))
        if self.mulligan_count > previous_mulligan_count:
            self.mulliganed_hand_grp_ids |= self._last_hand_snapshot

        turn_info = message.get("turnInfo") or {}
        if "turnNumber" in turn_info:
            self.last_turn = turn_info["turnNumber"]

        # Opening hand is whatever was in hand right before the FIRST real
        # draw — this naturally excludes any mulliganed-away hand, since a
        # mulligan's replacement happens via diffDeletedInstanceIds/zones/
        # gameObjects before this point. Confirmed against a real archived
        # mulligan (same match used to confirm the comment above).
        if drawn_instance_ids and self.opening_hand is None:
            self.opening_hand = set(self._last_hand_snapshot)

        for instance_id in message.get("diffDeletedInstanceIds") or []:
            self._remove_object(instance_id)

        self._merge_zones(message.get("zones"))
        self._merge_objects(message.get("gameObjects"))
        self._update_opponent_commanders()

        for instance_id in drawn_instance_ids:
            obj = self.objects.get(instance_id)
            if obj is not None and obj.owner_seat_id == self.our_seat_id:
                self.drawn.add(obj.grp_id)

        for instance_id in played_land_instance_ids:
            obj = self.objects.get(instance_id)
            if obj is not None and obj.owner_seat_id == self.our_seat_id:
                self.land_play_counts[obj.grp_id] = self.land_play_counts.get(obj.grp_id, 0) + 1
                if self.last_turn is not None:
                    self.land_first_play_turn.setdefault(obj.grp_id, self.last_turn)

        for instance_id in cast_spell_instance_ids:
            obj = self.objects.get(instance_id)
            if obj is not None and obj.owner_seat_id == self.our_seat_id:
                self.cast_grp_ids[obj.grp_id] = self.cast_grp_ids.get(obj.grp_id, 0) + 1

        for source_instance_id, ability_id in ability_activations:
            obj = self.objects.get(source_instance_id)
            if obj is not None:
                key = (obj.grp_id, ability_id)
                self.ability_activation_counts[key] = self.ability_activation_counts.get(key, 0) + 1

        self._last_hand_snapshot = self._current_hand_grp_ids()

    def _draw_instance_ids(self, annotations: list[dict] | None) -> list[int]:
        """Instance ids drawn into our hand by this message's annotations."""
        drawn_ids = []
        for annotation in annotations or []:
            if "AnnotationType_ZoneTransfer" not in (annotation.get("type") or []):
                continue
            details = {d.get("key"): d for d in annotation.get("details") or []}
            category = (details.get("category") or {}).get("valueString") or []
            if "Draw" not in category:
                continue
            zone_dest = (details.get("zone_dest") or {}).get("valueInt32") or []
            if self.hand_zone_id not in zone_dest:
                continue
            drawn_ids.extend(annotation.get("affectedIds") or [])
        return drawn_ids

    def _hand_exit_instance_ids(self, annotations: list[dict] | None, category_name: str) -> list[int]:
        """Instance ids that left our hand this message via a ZoneTransfer of
        the given category — "PlayLand" (to the battlefield) or "CastSpell"
        (to the stack). Mirrors _draw_instance_ids but filters zone_src
        (leaving our hand) rather than zone_dest (entering it)."""
        ids = []
        for annotation in annotations or []:
            if "AnnotationType_ZoneTransfer" not in (annotation.get("type") or []):
                continue
            details = {d.get("key"): d for d in annotation.get("details") or []}
            category = (details.get("category") or {}).get("valueString") or []
            if category_name not in category:
                continue
            zone_src = (details.get("zone_src") or {}).get("valueInt32") or []
            if self.hand_zone_id not in zone_src:
                continue
            ids.extend(annotation.get("affectedIds") or [])
        return ids

    def _ability_activations(self, annotations: list[dict] | None) -> list[tuple[int, int]]:
        """(source instance id, ability id) for every ability WE activated
        this message. AnnotationType_UserActionTaken records the action
        (affectorId is the acting player's seat; details.actionType 4 is
        "activate an ability", with details.abilityGrpId — despite the name,
        an Abilities.Id, not a Cards.GrpId — set to the ability activated).
        Its affectedIds[0] is an ability-instance id, not a permanent, so the
        source permanent is found via a same-diff
        AnnotationType_AbilityInstanceCreated annotation sharing that same
        id in its own affectedIds[0], whose affectorId is the source
        permanent's instance id. Confirmed against real archived data."""
        ability_sources: dict[int, int] = {}
        for annotation in annotations or []:
            if "AnnotationType_AbilityInstanceCreated" not in (annotation.get("type") or []):
                continue
            affected = annotation.get("affectedIds") or []
            source = annotation.get("affectorId")
            if affected and source is not None:
                ability_sources[affected[0]] = source

        activations = []
        for annotation in annotations or []:
            if "AnnotationType_UserActionTaken" not in (annotation.get("type") or []):
                continue
            if annotation.get("affectorId") != self.our_seat_id:
                continue
            details = {d.get("key"): d for d in annotation.get("details") or []}
            action_type = (details.get("actionType") or {}).get("valueInt32") or []
            ability_id = (details.get("abilityGrpId") or {}).get("valueInt32") or []
            if action_type != [4] or not ability_id or ability_id[0] == 0:
                continue
            affected = annotation.get("affectedIds") or []
            if not affected:
                continue
            source_instance_id = ability_sources.get(affected[0])
            if source_instance_id is not None:
                activations.append((source_instance_id, ability_id[0]))
        return activations

    def _merge_players(self, players: list[dict] | None) -> None:
        """A players[] update can carry just one player (e.g. their own
        mulligan decision), not always both — only ours is of interest."""
        for entry in players or []:
            if entry.get("systemSeatNumber") == self.our_seat_id and "mulliganCount" in entry:
                self.mulligan_count = entry["mulliganCount"]

    def _current_hand_grp_ids(self) -> set[int]:
        if self.hand_zone_id is None:
            return set()
        zone = self.zones.get(self.hand_zone_id)
        if zone is None:
            return set()
        return {
            self.objects[instance_id].grp_id
            for instance_id in zone.object_instance_ids
            if instance_id in self.objects
        }

    def _merge_zones(self, zones: list[dict] | None) -> None:
        for entry in zones or []:
            zone_id = entry.get("zoneId")
            if zone_id is None:
                continue
            zone = self.zones.get(zone_id)
            if zone is None:
                zone = _Zone(zone_type=entry.get("type"), owner_seat_id=entry.get("ownerSeatId"))
                self.zones[zone_id] = zone
            else:
                zone.zone_type = entry.get("type", zone.zone_type)
                zone.owner_seat_id = entry.get("ownerSeatId", zone.owner_seat_id)
            if "objectInstanceIds" in entry:
                zone.object_instance_ids = set(entry["objectInstanceIds"])

    def _merge_objects(self, game_objects: list[dict] | None) -> None:
        for entry in game_objects or []:
            instance_id = entry.get("instanceId")
            grp_id = entry.get("grpId")
            if instance_id is None or grp_id is None:
                continue
            zone_id = entry.get("zoneId")
            owner_seat_id = entry.get("ownerSeatId")

            # A diff can carry an object that already exists but has moved
            # zones (e.g. cast from hand) — drop its stale membership first,
            # since the zones array isn't guaranteed to be resent for every
            # zone a move touches.
            previous = self.objects.get(instance_id)
            if previous is not None and previous.zone_id not in (None, zone_id):
                old_zone = self.zones.get(previous.zone_id)
                if old_zone is not None:
                    old_zone.object_instance_ids.discard(instance_id)

            obj_type = entry.get("type", previous.type if previous is not None else None)
            self.objects[instance_id] = _Object(grp_id, owner_seat_id, zone_id, obj_type)
            if zone_id is not None:
                zone = self.zones.setdefault(
                    zone_id, _Zone(zone_type=None, owner_seat_id=owner_seat_id)
                )
                zone.object_instance_ids.add(instance_id)

    def _remove_object(self, instance_id: int) -> None:
        obj = self.objects.pop(instance_id, None)
        if obj is None:
            return
        for zone in self.zones.values():
            zone.object_instance_ids.discard(instance_id)

    def finalize(self, match_id: str) -> GameRecord:
        opening_hand = self.opening_hand if self.opening_hand is not None else self._last_hand_snapshot
        return GameRecord(
            match_id=match_id,
            game_number=self.game_number,
            opening_hand=frozenset(opening_hand),
            drawn=frozenset(self.drawn),
            final_hand=frozenset(self._last_hand_snapshot),
            mulligan_count=self.mulligan_count,
            last_turn=self.last_turn,
            opponent_commander_grp_ids=frozenset(self.opponent_commander_grp_ids),
            mulliganed_hand_grp_ids=frozenset(self.mulliganed_hand_grp_ids),
            land_play_counts=dict(self.land_play_counts),
            land_first_play_turn=dict(self.land_first_play_turn),
            cast_grp_ids=dict(self.cast_grp_ids),
            ability_activation_counts=dict(self.ability_activation_counts),
        )


def parse_games(span_text: str, *, match_id: str, our_seat_id: int) -> list[GameRecord]:
    """Reconstruct each game's opening hand and later draws for our own seat.

    A GameStateType_Full message with a NEW gameNumber starts a new game
    (finalizing whichever game was previously in progress first) — this keys
    state by game_number for a genuine Bo3 match. But a Full message can also
    arrive mid-game after a reconnect, carrying the SAME gameNumber as the
    game already in progress (confirmed against real archived data: stage is
    GameStage_Play, not GameStage_Start) — that's a resync, not a new game,
    and must not reset accumulated opening_hand/drawn/mulligan/turn state.
    """
    records: list[GameRecord] = []
    tracker: _GameTracker | None = None

    for message in _iter_game_state_messages(span_text):
        state_type = message.get("type")
        if state_type == "GameStateType_Full":
            game_number = (message.get("gameInfo") or {}).get("gameNumber", 1)
            if tracker is not None and tracker.game_number == game_number:
                tracker.reseed(message)
            else:
                if tracker is not None:
                    records.append(tracker.finalize(match_id))
                tracker = _GameTracker(our_seat_id)
                tracker.apply_full(message)
        elif state_type == "GameStateType_Diff":
            if tracker is None:
                log.debug("Diff message before any Full message, skipping")
                continue
            tracker.apply_diff(message)

    if tracker is not None:
        records.append(tracker.finalize(match_id))

    return records
