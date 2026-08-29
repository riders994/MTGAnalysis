import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector.config import Config  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

HEADER_TEMPLATE = """Mono path[0] = 'C:/Program Files/Wizards of the Coast/MTGA/MTGA_Data/Managed'
Mono config path = 'C:/Program Files/Wizards of the Coast/MTGA/MonoBleedingEdge/etc'
Initialize engine version: 2022.3.62f2 (7670c08855a9)
DETAILED LOGS: {detailed}
Startup Timestamp: {startup}
Loaded embedded metadata
Version: 2026.62.0.13653 / 2026.62.0.13653.1304277 /
"""


def make_log(startup: str, *, detailed: bool = True, padding: int = 12000,
             marker: str = "A") -> bytes:
    """A synthetic Player.log header, padded past the fingerprint window."""
    header = HEADER_TEMPLATE.format(
        detailed="ENABLED" if detailed else "DISABLED", startup=startup
    )
    filler = "".join(
        f"[UnityCrossThreadLogger]{startup}: filler line {marker}{i}\n"
        for i in range(padding // 60 + 1)
    )
    return (header + filler).encode("utf-8")


def body(startup: str, count: int, marker: str = "B") -> bytes:
    return "".join(
        f"[UnityCrossThreadLogger]{startup}: event {marker}{i}\n" for i in range(count)
    ).encode("utf-8")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config = Config(
        archive_dir=tmp_path / "archive",
        player_log=tmp_path / "mtga" / "Player.log",
        player_prev_log=tmp_path / "mtga" / "Player-prev.log",
        poll_interval=0.0,
        snapshot_interval=0.0,
        source_path=tmp_path / "config.toml",
    )
    config.player_log.parent.mkdir(parents=True, exist_ok=True)
    config.ensure_dirs()
    return config


def deck_upsert_line(
    deck_id: str,
    name: str,
    *,
    version: str = "1",
    format: str = "Standard",
    last_updated: str = "2026-08-08T02:35:17.4706732-04:00",
    last_played: str | None = None,
    main_deck=(),
    sideboard=(),
    command_zone=(),
    request_id: str = "11111111-1111-1111-1111-111111111111",
) -> str:
    """A synthetic DeckUpsertDeckV3 log line, with Arena's double JSON-encoding.

    Attribute values that are dates come back from Arena as a JSON string
    containing an already-JSON-encoded string (embedded quotes and all), so
    this wraps last_updated/last_played with an extra json.dumps to match.
    """
    attributes = [
        {"name": "Version", "value": version},
        {"name": "LastUpdated", "value": json.dumps(last_updated)},
        {"name": "Format", "value": format},
    ]
    if last_played is not None:
        attributes.append({"name": "LastPlayed", "value": json.dumps(last_played)})

    def cards(pairs):
        return [{"cardId": card_id, "quantity": quantity} for card_id, quantity in pairs]

    request = {
        "Summary": {"DeckId": deck_id, "Name": name, "Attributes": attributes},
        "Deck": {
            "MainDeck": cards(main_deck),
            "Sideboard": cards(sideboard),
            "CommandZone": cards(command_zone),
        },
        "ActionType": "Updated",
    }
    outer = {"id": request_id, "request": json.dumps(request)}
    return f"[UnityCrossThreadLogger]==> DeckUpsertDeckV3 {json.dumps(outer)}\r\n"


def event_set_deck_response_line(
    course_id: str,
    deck_id: str,
    name: str = "Deck",
    *,
    request_id: str = "22222222-2222-2222-2222-222222222222",
) -> str:
    """A synthetic EventSetDeckV3 response: '<== EventSetDeckV3(<id>)' header,
    then the JSON payload as the next physical line — confirmed real shape."""
    payload = {
        "CourseId": course_id,
        "InternalEventName": "Play_Brawl_Historic",
        "CourseDeckSummary": {"DeckId": deck_id, "Name": name},
    }
    return f"<== EventSetDeckV3({request_id})\r\n{json.dumps(payload)}\r\n"


def match_room_state_playing_line(
    match_id: str,
    reserved_players,  # list of (user_id, system_seat_id, team_id)
    *,
    our_id: str = "OURCLIENTID",
    timestamp: str = "8/17/2026 7:29:34 PM",
) -> str:
    """A synthetic MatchGameRoomStateChangedEvent(Playing): header line ending
    in the event name, JSON payload as the next physical line."""
    payload = {
        "matchGameRoomStateChangedEvent": {
            "gameRoomInfo": {
                "gameRoomConfig": {
                    "matchId": match_id,
                    "reservedPlayers": [
                        {"userId": user_id, "systemSeatId": seat, "teamId": team}
                        for user_id, seat, team in reserved_players
                    ],
                },
                "stateType": "MatchGameRoomStateType_Playing",
            }
        }
    }
    header = f"[UnityCrossThreadLogger]{timestamp}: Match to {our_id}: MatchGameRoomStateChangedEvent"
    return f"{header}\r\n{json.dumps(payload)}\r\n"


def match_room_state_completed_line(
    match_id: str,
    winning_team_id: int,
    *,
    reason: str = "ResultReason_Normal",
    include_game_scope: bool = True,
    our_id: str = "OURCLIENTID",
    timestamp: str = "8/17/2026 7:41:11 PM",
) -> str:
    """A synthetic MatchGameRoomStateChangedEvent(MatchCompleted). Real payloads
    carry both a MatchScope_Game and a MatchScope_Match result entry."""
    result_list = []
    if include_game_scope:
        result_list.append(
            {"scope": "MatchScope_Game", "result": "ResultType_WinLoss",
             "winningTeamId": winning_team_id, "reason": reason}
        )
    result_list.append(
        {"scope": "MatchScope_Match", "result": "ResultType_WinLoss",
         "winningTeamId": winning_team_id, "reason": reason}
    )
    payload = {
        "matchGameRoomStateChangedEvent": {
            "gameRoomInfo": {
                "gameRoomConfig": {"matchId": match_id, "reservedPlayers": []},
                "stateType": "MatchGameRoomStateType_MatchCompleted",
                "finalMatchResult": {"matchId": match_id, "resultList": result_list},
            }
        }
    }
    header = f"[UnityCrossThreadLogger]{timestamp}: Match to {our_id}: MatchGameRoomStateChangedEvent"
    return f"{header}\r\n{json.dumps(payload)}\r\n"


def _gre_line(gre_messages, *, our_id: str = "OURCLIENTID", timestamp: str = "t") -> str:
    payload = {"greToClientEvent": {"greToClientMessages": gre_messages}}
    return f"[UnityCrossThreadLogger]{timestamp}: Match to {our_id}: GreToClientEvent\r\n{json.dumps(payload)}\r\n"


def gre_full_line(
    *,
    our_seat_id: int,
    hand_zone_id: int,
    hand,  # list of (instance_id, grp_id)
    opponent_hand_zone_id: int | None = None,
    opponent_hand_instance_ids=(),
    game_number: int = 1,
    **kwargs,
) -> str:
    """A synthetic GameStateType_Full message: our own hand fully revealed
    (grpId present), an opponent's hand present only as bare instance ids
    (hidden information) — matching Arena's confirmed visibility rules."""
    zones = [
        {
            "zoneId": hand_zone_id,
            "type": "ZoneType_Hand",
            "ownerSeatId": our_seat_id,
            "objectInstanceIds": [instance_id for instance_id, _ in hand],
        }
    ]
    if opponent_hand_zone_id is not None:
        zones.append(
            {
                "zoneId": opponent_hand_zone_id,
                "type": "ZoneType_Hand",
                "ownerSeatId": 3 - our_seat_id,
                "objectInstanceIds": list(opponent_hand_instance_ids),
            }
        )

    game_objects = [
        {"instanceId": instance_id, "grpId": grp_id, "type": "GameObjectType_Card",
         "zoneId": hand_zone_id, "ownerSeatId": our_seat_id}
        for instance_id, grp_id in hand
    ]

    message = {
        "type": "GREMessageType_GameStateMessage",
        "gameStateMessage": {
            "type": "GameStateType_Full",
            "gameInfo": {"gameNumber": game_number},
            "zones": zones,
            "gameObjects": game_objects,
        },
    }
    return _gre_line([message], **kwargs)


def gre_diff_mulligan_line(
    *, old_instance_ids, new_hand, hand_zone_id: int, our_seat_id: int, **kwargs
) -> str:
    """A synthetic mulligan diff: old hand deleted, fresh hand dealt — no
    Draw annotation, matching the real London-mulligan replacement shape."""
    message = {
        "type": "GREMessageType_GameStateMessage",
        "gameStateMessage": {
            "type": "GameStateType_Diff",
            "diffDeletedInstanceIds": list(old_instance_ids),
            "zones": [
                {
                    "zoneId": hand_zone_id,
                    "objectInstanceIds": [instance_id for instance_id, _ in new_hand],
                }
            ],
            "gameObjects": [
                {"instanceId": instance_id, "grpId": grp_id, "type": "GameObjectType_Card",
                 "zoneId": hand_zone_id, "ownerSeatId": our_seat_id}
                for instance_id, grp_id in new_hand
            ],
        },
    }
    return _gre_line([message], **kwargs)


def gre_diff_draw_line(
    *, drawn_instance_id: int, drawn_grp_id: int, hand_zone_id: int, our_seat_id: int, **kwargs
) -> str:
    """A synthetic draw diff: a ZoneTransfer annotation (category Draw) into
    our hand zone, plus the revealing gameObjects entry for the drawn card."""
    message = {
        "type": "GREMessageType_GameStateMessage",
        "gameStateMessage": {
            "type": "GameStateType_Diff",
            "annotations": [
                {
                    "affectedIds": [drawn_instance_id],
                    "type": ["AnnotationType_ZoneTransfer"],
                    "details": [
                        {"key": "zone_dest", "valueInt32": [hand_zone_id]},
                        {"key": "category", "valueString": ["Draw"]},
                    ],
                }
            ],
            "gameObjects": [
                {"instanceId": drawn_instance_id, "grpId": drawn_grp_id,
                 "type": "GameObjectType_Card", "zoneId": hand_zone_id,
                 "ownerSeatId": our_seat_id}
            ],
        },
    }
    return _gre_line([message], **kwargs)


def gre_diff_move_line(*, instance_id: int, grp_id: int, new_zone_id: int, owner_seat_id: int, **kwargs) -> str:
    """A synthetic diff moving an existing object to a new zone (e.g. cast
    from hand), with no accompanying zones-array update for the old zone —
    matching the real "only gameObjects carries the new zoneId" shape."""
    message = {
        "type": "GREMessageType_GameStateMessage",
        "gameStateMessage": {
            "type": "GameStateType_Diff",
            "gameObjects": [
                {"instanceId": instance_id, "grpId": grp_id, "type": "GameObjectType_Card",
                 "zoneId": new_zone_id, "ownerSeatId": owner_seat_id}
            ],
        },
    }
    return _gre_line([message], **kwargs)


@pytest.fixture
def real_logs():
    """The captured sample logs, when present (they are gitignored)."""
    current = REPO_ROOT / "Player.log"
    previous = REPO_ROOT / "Player-prev.log"
    if not current.exists() or not previous.exists():
        pytest.skip("real Player.log fixtures not present")
    return previous, current
