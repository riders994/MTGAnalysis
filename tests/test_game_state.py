"""Reconstructing per-game opening hand and draws from GRE Full/Diff traffic."""

from __future__ import annotations

from analysis.game_state import parse_games

from conftest import gre_diff_draw_line, gre_diff_mulligan_line, gre_diff_move_line, gre_full_line

OUR_SEAT = 2
HAND_ZONE = 35


def test_opening_hand_and_one_draw_no_mulligan():
    text = gre_full_line(
        our_seat_id=OUR_SEAT,
        hand_zone_id=HAND_ZONE,
        hand=[(1, 100), (2, 101), (3, 102)],
    ) + gre_diff_draw_line(
        drawn_instance_id=4, drawn_grp_id=103, hand_zone_id=HAND_ZONE, our_seat_id=OUR_SEAT
    )

    records = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)

    assert len(records) == 1
    record = records[0]
    assert record.opening_hand == frozenset({100, 101, 102})
    assert record.drawn == frozenset({103})


def test_mulligan_before_first_draw_is_excluded_from_opening_hand():
    # players[].mulliganCount would be the fallback cross-check for this
    # heuristic if it turns out not to hold up against real archived data.
    text = (
        gre_full_line(
            our_seat_id=OUR_SEAT,
            hand_zone_id=HAND_ZONE,
            hand=[(1, 100), (2, 101), (3, 102)],
        )
        + gre_diff_mulligan_line(
            old_instance_ids=[1, 2, 3],
            new_hand=[(11, 200), (12, 201)],
            hand_zone_id=HAND_ZONE,
            our_seat_id=OUR_SEAT,
        )
        + gre_diff_draw_line(
            drawn_instance_id=13, drawn_grp_id=202, hand_zone_id=HAND_ZONE, our_seat_id=OUR_SEAT
        )
    )

    records = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)

    assert records[0].opening_hand == frozenset({200, 201})
    assert 100 not in records[0].opening_hand
    assert records[0].drawn == frozenset({202})


def test_no_draw_before_game_ends_falls_back_to_last_hand_snapshot():
    # An early concession before any draw happened — opening_hand must still
    # be populated from the Full message rather than lost.
    text = gre_full_line(
        our_seat_id=OUR_SEAT,
        hand_zone_id=HAND_ZONE,
        hand=[(1, 100), (2, 101)],
    )

    records = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)

    assert records[0].opening_hand == frozenset({100, 101})
    assert records[0].drawn == frozenset()


def test_object_moving_zones_is_removed_from_its_old_zone():
    text = (
        gre_full_line(
            our_seat_id=OUR_SEAT,
            hand_zone_id=HAND_ZONE,
            hand=[(1, 100), (2, 101)],
        )
        + gre_diff_draw_line(
            drawn_instance_id=3, drawn_grp_id=102, hand_zone_id=HAND_ZONE, our_seat_id=OUR_SEAT
        )
        # Card 1 is cast from hand to the battlefield (zone 90) before a
        # second draw — its identity should not linger in the hand zone.
        + gre_diff_move_line(instance_id=1, grp_id=100, new_zone_id=90, owner_seat_id=OUR_SEAT)
        + gre_diff_draw_line(
            drawn_instance_id=4, drawn_grp_id=103, hand_zone_id=HAND_ZONE, our_seat_id=OUR_SEAT
        )
    )

    records = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)

    # Opening hand was snapshotted before any of this, so it's unaffected...
    assert records[0].opening_hand == frozenset({100, 101})
    # ...but both real draws should still be captured correctly.
    assert records[0].drawn == frozenset({102, 103})


def test_two_full_messages_produce_two_game_records():
    # Simulates a hypothetical Bo3 match — validates parser mechanics only,
    # not real Bo3 data (none has been archived yet).
    text = (
        gre_full_line(
            our_seat_id=OUR_SEAT, hand_zone_id=HAND_ZONE, hand=[(1, 100)], game_number=1
        )
        + gre_diff_draw_line(
            drawn_instance_id=2, drawn_grp_id=101, hand_zone_id=HAND_ZONE, our_seat_id=OUR_SEAT
        )
        + gre_full_line(
            our_seat_id=OUR_SEAT, hand_zone_id=HAND_ZONE, hand=[(10, 200)], game_number=2
        )
    )

    records = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)

    assert len(records) == 2
    assert records[0].game_number == 1
    assert records[0].opening_hand == frozenset({100})
    assert records[1].game_number == 2
    assert records[1].opening_hand == frozenset({200})


def test_opponent_draw_into_their_own_hand_zone_is_ignored():
    # Opponent's draw targets their own hand zone (31), not ours (35) — the
    # zone_dest filter alone should exclude it from our drawn set.
    text = gre_full_line(
        our_seat_id=OUR_SEAT,
        hand_zone_id=HAND_ZONE,
        hand=[(1, 100)],
        opponent_hand_zone_id=31,
        opponent_hand_instance_ids=[50, 51],
    ) + gre_diff_draw_line(
        drawn_instance_id=52, drawn_grp_id=999, hand_zone_id=31, our_seat_id=1
    )

    records = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)

    assert records[0].drawn == frozenset()
