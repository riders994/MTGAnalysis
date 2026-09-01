"""Reconstructing per-game opening hand and draws from GRE Full/Diff traffic."""

from __future__ import annotations

from analysis.game_state import parse_games

from conftest import (
    gre_diff_activate_ability_line,
    gre_diff_cast_spell_line,
    gre_diff_draw_line,
    gre_diff_mulligan_count_line,
    gre_diff_mulligan_line,
    gre_diff_move_line,
    gre_diff_play_land_line,
    gre_diff_turn_line,
    gre_full_line,
)

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


def test_mulliganed_hand_is_captured_from_the_sent_back_hand():
    # Confirmed real shape (session 20260817T194331): the mulliganCount bump
    # and the hand swap land in the SAME diff. The sent-back hand (100, 101,
    # 102) must be recorded, and must NOT include the kept hand (200, 201).
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
            mulligan_count=1,
        )
        + gre_diff_draw_line(
            drawn_instance_id=13, drawn_grp_id=202, hand_zone_id=HAND_ZONE, our_seat_id=OUR_SEAT
        )
    )

    record = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)[0]

    assert record.mulligan_count == 1
    assert record.mulliganed_hand_grp_ids == frozenset({100, 101, 102})
    assert record.opening_hand == frozenset({200, 201})


def test_no_mulligan_means_no_mulliganed_hand():
    text = gre_full_line(our_seat_id=OUR_SEAT, hand_zone_id=HAND_ZONE, hand=[(1, 100)])

    record = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)[0]

    assert record.mulliganed_hand_grp_ids == frozenset()


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


def test_mulligan_count_and_last_turn_and_final_hand_are_tracked():
    text = (
        gre_full_line(our_seat_id=OUR_SEAT, hand_zone_id=HAND_ZONE, hand=[(1, 100)])
        + gre_diff_mulligan_count_line(our_seat_id=OUR_SEAT, mulligan_count=1)
        + gre_diff_draw_line(
            drawn_instance_id=2, drawn_grp_id=101, hand_zone_id=HAND_ZONE, our_seat_id=OUR_SEAT
        )
        + gre_diff_turn_line(turn_number=3)
    )

    record = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)[0]

    assert record.mulligan_count == 1
    assert record.last_turn == 3
    assert record.final_hand == frozenset({100, 101})


def test_last_turn_is_none_when_no_turn_info_ever_arrives():
    text = gre_full_line(our_seat_id=OUR_SEAT, hand_zone_id=HAND_ZONE, hand=[(1, 100)])

    record = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)[0]

    assert record.last_turn is None


def test_midgame_resync_with_same_game_number_is_not_a_new_game():
    # A real reconnect resync: a second Full message with the SAME
    # gameNumber, well after the game has started (stage=Play, not Start in
    # the real data) — confirmed against a real archived match. Accumulated
    # opening_hand/drawn/mulligan/turn state must survive, and this must
    # produce exactly one GameRecord, not two.
    text = (
        gre_full_line(
            our_seat_id=OUR_SEAT, hand_zone_id=HAND_ZONE, hand=[(1, 100)], game_number=1
        )
        + gre_diff_mulligan_count_line(our_seat_id=OUR_SEAT, mulligan_count=1)
        + gre_diff_draw_line(
            drawn_instance_id=2, drawn_grp_id=101, hand_zone_id=HAND_ZONE, our_seat_id=OUR_SEAT
        )
        + gre_diff_turn_line(turn_number=5)
        # Resync: same game_number=1. Hand now also contains a new card (3,
        # grpId 102) that arrived during the reconnect gap with no draw
        # annotation — it should be inferred as drawn, not lost.
        + gre_full_line(
            our_seat_id=OUR_SEAT,
            hand_zone_id=HAND_ZONE,
            hand=[(1, 100), (2, 101), (3, 102)],
            game_number=1,
        )
    )

    records = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)

    assert len(records) == 1
    record = records[0]
    assert record.opening_hand == frozenset({100})
    assert record.drawn == frozenset({101, 102})
    assert record.mulligan_count == 1
    assert record.last_turn == 5
    assert record.final_hand == frozenset({100, 101, 102})


def test_opponent_commander_grp_id_is_revealed_and_ours_is_excluded():
    # Command Zone is the one confirmed exception to "opponent objects stay
    # hidden" — both commanders reveal grpId, distinguished by ownerSeatId.
    text = gre_full_line(
        our_seat_id=OUR_SEAT,
        hand_zone_id=HAND_ZONE,
        hand=[(1, 100)],
        command_zone_id=26,
        command_zone_cards=[
            (10, 90302, OUR_SEAT),  # our own commander
            (11, 96080, 3 - OUR_SEAT),  # opponent's commander
        ],
    )

    record = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)[0]

    assert record.opponent_commander_grp_ids == frozenset({96080})


def test_non_card_command_zone_object_is_not_an_opponent_commander():
    # Confirmed real shape: a Boon (e.g. Loch Larent's curse) or Emblem
    # (e.g. "The Ring tempts you") transiently sits in the same Command Zone
    # as the real commander, owned by the opposing seat, revealing a grpId
    # that isn't a card — must not be picked up as a second "commander".
    text = gre_full_line(
        our_seat_id=OUR_SEAT,
        hand_zone_id=HAND_ZONE,
        hand=[(1, 100)],
        command_zone_id=26,
        command_zone_cards=[(11, 96080, 3 - OUR_SEAT)],
        command_zone_non_cards=[(12, 6, 3 - OUR_SEAT, "GameObjectType_Boon")],
    )

    record = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)[0]

    assert record.opponent_commander_grp_ids == frozenset({96080})


def test_no_command_zone_means_no_opponent_commanders():
    text = gre_full_line(our_seat_id=OUR_SEAT, hand_zone_id=HAND_ZONE, hand=[(1, 100)])

    record = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)[0]

    assert record.opponent_commander_grp_ids == frozenset()


def test_opponent_commander_survives_midgame_resync():
    text = (
        gre_full_line(
            our_seat_id=OUR_SEAT,
            hand_zone_id=HAND_ZONE,
            hand=[(1, 100)],
            command_zone_id=26,
            command_zone_cards=[(11, 96080, 3 - OUR_SEAT)],
            game_number=1,
        )
        + gre_diff_turn_line(turn_number=3)
        + gre_full_line(
            our_seat_id=OUR_SEAT,
            hand_zone_id=HAND_ZONE,
            hand=[(1, 100)],
            command_zone_id=26,
            command_zone_cards=[(11, 96080, 3 - OUR_SEAT)],
            game_number=1,
        )
    )

    records = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)

    assert len(records) == 1
    assert records[0].opponent_commander_grp_ids == frozenset({96080})


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


BATTLEFIELD_ZONE = 28
STACK_ZONE = 27


def test_land_play_tracked_with_turn_of_first_play():
    text = (
        gre_full_line(our_seat_id=OUR_SEAT, hand_zone_id=HAND_ZONE, hand=[(1, 100)])
        + gre_diff_turn_line(turn_number=3)
        + gre_diff_play_land_line(
            instance_id=1, grp_id=100, hand_zone_id=HAND_ZONE,
            battlefield_zone_id=BATTLEFIELD_ZONE, our_seat_id=OUR_SEAT,
        )
    )

    record = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)[0]

    assert record.land_play_counts == {100: 1}
    assert record.land_first_play_turn == {100: 3}


def test_second_play_of_same_land_increments_count_but_keeps_first_turn():
    # A bounce land or similar returning the same land to hand and back —
    # the count should grow but the recorded first-play turn should not move.
    text = (
        gre_full_line(our_seat_id=OUR_SEAT, hand_zone_id=HAND_ZONE, hand=[(1, 100)])
        + gre_diff_turn_line(turn_number=2)
        + gre_diff_play_land_line(
            instance_id=1, grp_id=100, hand_zone_id=HAND_ZONE,
            battlefield_zone_id=BATTLEFIELD_ZONE, our_seat_id=OUR_SEAT,
        )
        + gre_diff_turn_line(turn_number=6)
        + gre_diff_play_land_line(
            instance_id=2, grp_id=100, hand_zone_id=HAND_ZONE,
            battlefield_zone_id=BATTLEFIELD_ZONE, our_seat_id=OUR_SEAT,
        )
    )

    record = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)[0]

    assert record.land_play_counts == {100: 2}
    assert record.land_first_play_turn == {100: 2}


def test_opponent_land_play_is_not_counted():
    # Symmetric with test_opponent_draw_into_their_own_hand_zone_is_ignored:
    # the opponent's own land drop leaves THEIR hand zone, not ours.
    text = gre_full_line(
        our_seat_id=OUR_SEAT,
        hand_zone_id=HAND_ZONE,
        hand=[(1, 100)],
        opponent_hand_zone_id=31,
        opponent_hand_instance_ids=[50],
    ) + gre_diff_play_land_line(
        instance_id=50, grp_id=999, hand_zone_id=31,
        battlefield_zone_id=BATTLEFIELD_ZONE, our_seat_id=1,
    )

    record = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)[0]

    assert record.land_play_counts == {}


def test_cast_spell_of_other_face_tracked_separately_from_land_play():
    # A modal-double-faced/adventure land's other face is a distinct grpId,
    # cast rather than played — must not be folded into land_play_counts.
    text = (
        gre_full_line(our_seat_id=OUR_SEAT, hand_zone_id=HAND_ZONE, hand=[(1, 200)])
        + gre_diff_cast_spell_line(
            instance_id=1, grp_id=200, hand_zone_id=HAND_ZONE,
            stack_zone_id=STACK_ZONE, our_seat_id=OUR_SEAT,
        )
    )

    record = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)[0]

    assert record.cast_grp_ids == {200: 1}
    assert record.land_play_counts == {}


def test_ability_activation_attributed_to_its_source_land():
    text = (
        gre_full_line(
            our_seat_id=OUR_SEAT, hand_zone_id=HAND_ZONE, hand=[(1, 100)],
        )
        + gre_diff_play_land_line(
            instance_id=1, grp_id=100, hand_zone_id=HAND_ZONE,
            battlefield_zone_id=BATTLEFIELD_ZONE, our_seat_id=OUR_SEAT,
        )
        + gre_diff_activate_ability_line(
            source_instance_id=1, ability_instance_id=900, ability_id=50001,
            acting_seat_id=OUR_SEAT,
        )
    )

    record = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)[0]

    assert record.ability_activation_counts == {(100, 50001): 1}


def test_opponent_ability_activation_is_not_counted():
    text = (
        gre_full_line(our_seat_id=OUR_SEAT, hand_zone_id=HAND_ZONE, hand=[(1, 100)])
        + gre_diff_activate_ability_line(
            source_instance_id=77, ability_instance_id=900, ability_id=50001,
            acting_seat_id=3 - OUR_SEAT,
        )
    )

    record = parse_games(text, match_id="match-1", our_seat_id=OUR_SEAT)[0]

    assert record.ability_activation_counts == {}
