"""Binding archived matches to the deck queued for them."""

from __future__ import annotations

from analysis.match_events import extract_our_user_id, join_deck_to_matches

from conftest import (
    event_set_deck_response_line,
    match_room_state_completed_line,
    match_room_state_playing_line,
)

OUR_ID = "OURCLIENTID"
OPPONENT_ID = "OPPONENTID"


def test_win_is_recorded():
    text = (
        event_set_deck_response_line("course-1", "deck-abc", "Lagaan")
        + match_room_state_playing_line(
            "match-1", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID
        )
        + match_room_state_completed_line("match-1", winning_team_id=1, our_id=OUR_ID)
    )

    outcomes = join_deck_to_matches(text, session_id="s1")

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.match_id == "match-1"
    assert outcome.deck_id == "deck-abc"
    assert outcome.won is True
    assert outcome.session_id == "s1"


def test_loss_is_recorded():
    text = (
        event_set_deck_response_line("course-1", "deck-abc", "Lagaan")
        + match_room_state_playing_line(
            "match-1", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID
        )
        + match_room_state_completed_line("match-1", winning_team_id=2, our_id=OUR_ID)
    )

    outcomes = join_deck_to_matches(text, session_id="s1")

    assert outcomes[0].won is False


def test_abandoned_queue_produces_no_outcome():
    text = event_set_deck_response_line("course-1", "deck-abc", "Lagaan")

    assert join_deck_to_matches(text, session_id="s1") == []


def test_stray_match_with_no_queued_deck_produces_no_outcome():
    text = (
        match_room_state_playing_line(
            "match-1", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID
        )
        + match_room_state_completed_line("match-1", winning_team_id=1, our_id=OUR_ID)
    )

    assert join_deck_to_matches(text, session_id="s1") == []


def test_extract_our_user_id_inbound_header():
    text = match_room_state_playing_line("match-1", [(OUR_ID, 1, 1)], our_id=OUR_ID)
    assert extract_our_user_id(text) == OUR_ID


def test_extract_our_user_id_outbound_header():
    text = f"{OUR_ID} to Match: ClientToGremessage\r\n{{}}\r\n"
    assert extract_our_user_id(text) == OUR_ID


def test_malformed_deck_response_is_skipped_not_raised():
    text = (
        "<== EventSetDeckV3(bad-id)\r\n{not valid json}\r\n"
        + match_room_state_playing_line(
            "match-1", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID
        )
        + match_room_state_completed_line("match-1", winning_team_id=1, our_id=OUR_ID)
    )

    # No deck was successfully queued, so the match is simply unbound.
    assert join_deck_to_matches(text, session_id="s1") == []


def test_malformed_room_state_is_skipped_not_raised():
    text = (
        event_set_deck_response_line("course-1", "deck-abc", "Lagaan")
        + "[UnityCrossThreadLogger]t: Match to X: MatchGameRoomStateChangedEvent\r\n{bad json}\r\n"
    )

    assert join_deck_to_matches(text, session_id="s1") == []
