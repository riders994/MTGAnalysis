"""Binding matches to the decks that were queued for them.

There is no reliable shared id between EventSetDeckV3 (which produces a fresh
CourseId per queue attempt) and MatchGameRoomStateChangedEvent (whose own
reservedPlayers[].courseId field is, confirmed by direct inspection, an
unrelated avatar cosmetic id — a naming collision in Arena's own schema, not
a join key: it matches that player's PreferredCosmetics.Avatar, not any
CourseId). Instead this walks one session in file order as a small state
machine: remember the most recently queued deck, and whenever a match's
Playing state appears, bind ITS matchId to that running deck state.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

_OUR_ID_INBOUND_RE = re.compile(r"Match to (\w+):")
_OUR_ID_OUTBOUND_RE = re.compile(r"(\w+) to Match:")
_EVENT_SET_DECK_RESPONSE_RE = re.compile(r"<== EventSetDeckV3\([^)]*\)\r?\n(\{[^\r\n]+\})")
_ROOM_STATE_RE = re.compile(r"MatchGameRoomStateChangedEvent[^\r\n]*\r?\n(\{[^\r\n]+\})")


@dataclass(frozen=True)
class ReservedPlayer:
    user_id: str
    system_seat_id: int
    team_id: int


@dataclass(frozen=True)
class GameOutcome:
    match_id: str
    deck_id: str
    won: bool
    session_id: str
    our_seat_id: int
    reason: str | None  # e.g. "ResultReason_Concede" — combined with won, tells
    # you whether WE conceded (reason is Concede and won is False) or the
    # opponent did (reason is Concede and won is True); a concession always
    # defeats the conceder.
    span: tuple[int, int]  # bounds this match's GRE traffic within the same log_text


@dataclass
class _PendingMatch:
    match_id: str
    deck_id: str
    our_team_id: int
    our_seat_id: int
    span_start: int


def extract_our_user_id(log_text: str) -> str | None:
    """The client id this session's traffic is addressed to/from — confirmed
    identical to our own userId in MatchGameRoomStateChangedEvent's
    reservedPlayers[]."""
    match = _OUR_ID_INBOUND_RE.search(log_text) or _OUR_ID_OUTBOUND_RE.search(log_text)
    return match.group(1) if match else None


def _parse_queued_deck(payload_text: str) -> str | None:
    """The DeckId bound to an EventSetDeckV3 response, or None if unparsable."""
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        log.debug("unparsable EventSetDeckV3 response, skipping")
        return None
    return (payload.get("CourseDeckSummary") or {}).get("DeckId")


def _reserved_players(raw: list[dict] | None) -> list[ReservedPlayer]:
    players = []
    for entry in raw or []:
        user_id, seat, team = entry.get("userId"), entry.get("systemSeatId"), entry.get("teamId")
        if user_id is None or seat is None or team is None:
            continue
        players.append(ReservedPlayer(user_id, seat, team))
    return players


def _parse_room_state(payload_text: str) -> dict | None:
    """The bits of one MatchGameRoomStateChangedEvent this package cares about."""
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        log.debug("unparsable MatchGameRoomStateChangedEvent, skipping")
        return None

    info = (payload.get("matchGameRoomStateChangedEvent") or {}).get("gameRoomInfo") or {}
    config = info.get("gameRoomConfig") or {}
    state_type, match_id = info.get("stateType"), config.get("matchId")
    if not state_type or not match_id:
        return None

    return {
        "state_type": state_type,
        "match_id": match_id,
        "reserved_players": _reserved_players(config.get("reservedPlayers")),
        "final_result": info.get("finalMatchResult") or {},
    }


def join_deck_to_matches(log_text: str, *, session_id: str) -> list[GameOutcome]:
    """Bind each completed match in a session to the deck queued for it.

    Handles zero, one, or several queue-then-match cycles in one session, an
    abandoned queue (deck queued, no match followed), and a match that
    completes with no Playing state seen (started outside this session's
    visible window) — all by simply producing no GameOutcome for them.
    """
    our_user_id = extract_our_user_id(log_text)
    if our_user_id is None:
        return []

    events: list[tuple[int, str, object]] = []
    for match in _EVENT_SET_DECK_RESPONSE_RE.finditer(log_text):
        deck_id = _parse_queued_deck(match.group(1))
        if deck_id is not None:
            events.append((match.start(), "deck", deck_id))
    for match in _ROOM_STATE_RE.finditer(log_text):
        room_state = _parse_room_state(match.group(1))
        if room_state is not None:
            events.append((match.start(), "room", room_state))
    events.sort(key=lambda item: item[0])

    outcomes: list[GameOutcome] = []
    current_deck_id: str | None = None
    pending: dict[str, _PendingMatch] = {}

    for offset, kind, value in events:
        if kind == "deck":
            current_deck_id = value
            continue

        room_state = value
        if room_state["state_type"] == "MatchGameRoomStateType_Playing":
            if current_deck_id is None:
                continue
            our_player = next(
                (p for p in room_state["reserved_players"] if p.user_id == our_user_id),
                None,
            )
            if our_player is None:
                continue
            pending[room_state["match_id"]] = _PendingMatch(
                match_id=room_state["match_id"],
                deck_id=current_deck_id,
                our_team_id=our_player.team_id,
                our_seat_id=our_player.system_seat_id,
                span_start=offset,
            )

        elif room_state["state_type"] == "MatchGameRoomStateType_MatchCompleted":
            match_pending = pending.pop(room_state["match_id"], None)
            if match_pending is None:
                continue
            result = next(
                (
                    r
                    for r in room_state["final_result"].get("resultList") or []
                    if r.get("scope") == "MatchScope_Match"
                ),
                None,
            )
            if result is None:
                continue
            outcomes.append(
                GameOutcome(
                    match_id=match_pending.match_id,
                    deck_id=match_pending.deck_id,
                    won=result.get("winningTeamId") == match_pending.our_team_id,
                    session_id=session_id,
                    our_seat_id=match_pending.our_seat_id,
                    reason=result.get("reason"),
                    span=(match_pending.span_start, offset),
                )
            )

    return outcomes
