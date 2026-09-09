"""End-to-end: archived sessions -> per-deck card-performance reports."""

from __future__ import annotations

import gzip
from pathlib import Path

from analysis import card_stats

from conftest import (
    deck_upsert_line,
    event_set_deck_response_line,
    gre_diff_activate_ability_line,
    gre_diff_cast_spell_line,
    gre_diff_draw_line,
    gre_diff_mulligan_line,
    gre_diff_play_land_line,
    gre_diff_turn_line,
    gre_full_line,
    match_room_state_completed_line,
    match_room_state_playing_line,
    write_carddb_snapshot,
)

OUR_SEAT = 1
OPPONENT_SEAT = 2

OUR_ID = "OURCLIENTID"
OPPONENT_ID = "OPPONENTID"


def _write_session(cfg, session_id: str, text: str, *, suffix: str = "aaaaaaaa") -> Path:
    dest = cfg.sessions_dir / f"session-{session_id}-{suffix}.log.gz"
    dest.write_bytes(gzip.compress(text.encode("utf-8")))
    return dest


def _match_session_text(deck_id, name, *, course_id, match_id, winning_team_id, format="HistoricBrawl"):
    return (
        deck_upsert_line(deck_id, name, version="1", format=format, main_deck=[(75022, 1)])
        + event_set_deck_response_line(course_id, deck_id, name)
        + match_room_state_playing_line(
            match_id, [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID
        )
        + match_room_state_completed_line(match_id, winning_team_id=winning_team_id, our_id=OUR_ID)
    )


def test_won_match_reported_with_no_per_card_data_yet(cfg, tmp_path):
    write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})
    text = _match_session_text(
        "deck-abc", "Lagaan", course_id="course-1", match_id="match-1", winning_team_id=1
    )
    _write_session(cfg, "20260817T192832", text)

    stats, summary = card_stats.collect_card_stats(cfg)

    assert summary.sessions_scanned == 1
    assert summary.matches_found == 1
    assert stats["Lagaan"].gp == 1
    assert stats["Lagaan"].gp_wins == 1
    assert stats["Lagaan"].card_tallies == {}

    full_summary = card_stats.run(cfg)
    assert full_summary.decks_written == ["lagaan.md"]
    content = (cfg.archive_dir / "reports" / "card_stats" / "lagaan.md").read_text()
    assert "# Lagaan" in content
    assert "100% (1/1)" in content  # Games Played line
    assert "No per-card data yet." in content


def test_lost_match_win_rate_is_zero(cfg, tmp_path):
    write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})
    text = _match_session_text(
        "deck-abc", "Lagaan", course_id="course-1", match_id="match-1", winning_team_id=2
    )
    _write_session(cfg, "20260817T192832", text)

    stats, _ = card_stats.collect_card_stats(cfg)

    assert stats["Lagaan"].gp == 1
    assert stats["Lagaan"].gp_wins == 0


def test_match_bound_to_unknown_deck_warns_and_is_skipped(cfg, tmp_path):
    write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})
    # A match/queue sequence with no corresponding DeckUpsertDeckV3 save.
    text = (
        event_set_deck_response_line("course-1", "deck-unknown", "Mystery Deck")
        + match_room_state_playing_line(
            "match-1", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID
        )
        + match_room_state_completed_line("match-1", winning_team_id=1, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T192832", text)

    stats, summary = card_stats.collect_card_stats(cfg)

    assert stats == {}
    assert summary.matches_found == 1
    assert summary.warnings  # unknown-deck warning surfaced

    full_summary = card_stats.run(cfg)
    assert full_summary.decks_written == []


def test_per_card_stats_from_gre_traffic(cfg, tmp_path):
    """Checkpoint B: GRE traffic between Playing and MatchCompleted feeds
    real OH/GD/GNS bucketing, not just GP/GP WR."""
    write_carddb_snapshot(cfg, tmp_path, {75022: "Island", 75021: "Plains", 96080: "Sol Ring"})

    text = (
        deck_upsert_line(
            "deck-abc", "Lagaan", version="1",
            main_deck=[(75022, 1), (75021, 1), (96080, 1)],
        )
        + event_set_deck_response_line("course-1", "deck-abc", "Lagaan")
        + match_room_state_playing_line(
            "match-1", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID
        )
        + gre_full_line(our_seat_id=1, hand_zone_id=35, hand=[(1, 75022)])  # Island in opening hand
        + gre_diff_draw_line(
            drawn_instance_id=2, drawn_grp_id=75021, hand_zone_id=35, our_seat_id=1
        )  # Plains drawn later; Sol Ring (96080) never seen
        + match_room_state_completed_line("match-1", winning_team_id=1, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T192832", text)

    stats, summary = card_stats.collect_card_stats(cfg)

    assert summary.games_parsed == 1
    tallies = stats["Lagaan"].card_tallies
    assert tallies[75022].oh == 1 and tallies[75022].oh_wins == 1
    assert tallies[75021].gd == 1 and tallies[75021].gd_wins == 1
    assert tallies[96080].gns == 1 and tallies[96080].gns_wins == 1

    full_summary = card_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "card_stats" / "lagaan.md").read_text()
    assert full_summary.decks_written == ["lagaan.md"]
    assert "Island" in content and "100% (1/1)" in content
    assert "No per-card data yet." not in content


def test_basic_land_art_variants_merge_by_name_snow_stays_separate(cfg, tmp_path):
    """Arena assigns a distinct grpId to each art style of a basic land, so
    a deck can carry several grpIds that all say "Plains". Collection stays
    keyed by the raw grpId, but the rendered report must merge those variants
    into one row — while Snow-Covered Plains, a genuinely different card,
    stays on its own row."""
    write_carddb_snapshot(
        cfg, tmp_path,
        {75021: "Plains", 75023: "Plains", 75024: "Snow-Covered Plains", 96080: "Sol Ring"},
    )

    text = (
        deck_upsert_line(
            "deck-abc", "Lagaan", version="1",
            main_deck=[(75021, 1), (75023, 1), (75024, 1), (96080, 1)],
        )
        + event_set_deck_response_line("course-1", "deck-abc", "Lagaan")
        + match_room_state_playing_line(
            "match-1", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID
        )
        + gre_full_line(our_seat_id=1, hand_zone_id=35, hand=[(1, 75021)])  # Plains style A in OH
        + gre_diff_draw_line(
            drawn_instance_id=2, drawn_grp_id=75023, hand_zone_id=35, our_seat_id=1
        )  # Plains style B drawn later; Snow-Covered Plains and Sol Ring never seen
        + match_room_state_completed_line("match-1", winning_team_id=1, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T192832", text)

    stats, _ = card_stats.collect_card_stats(cfg)

    # Collection itself stays keyed by raw grpId — the two Plains variants
    # are still tracked separately here, unmerged.
    tallies = stats["Lagaan"].card_tallies
    assert tallies[75021].oh == 1 and tallies[75021].oh_wins == 1
    assert tallies[75023].gd == 1 and tallies[75023].gd_wins == 1
    assert tallies[75024].gns == 1

    full_summary = card_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "card_stats" / "lagaan.md").read_text()
    assert full_summary.decks_written == ["lagaan.md"]

    assert content.count("| Plains |") == 1
    assert "| Plains | 100% (1/1) | 100% (1/1) | 100% (2/2) | — | — |" in content
    assert "| Snow-Covered Plains | — | — | — | 100% (1/1) | — |" in content


def test_mulliganed_hand_tallied_and_rendered(cfg, tmp_path):
    """Cards sent back on a mulligan are tallied per-card, distinct from
    the deck-level Mulligan Rate — and from OH, which only covers the hand
    we kept."""
    write_carddb_snapshot(cfg, tmp_path, {75022: "Island", 75021: "Plains", 96080: "Sol Ring"})

    text = (
        deck_upsert_line(
            "deck-abc", "Lagaan", version="1",
            main_deck=[(75022, 1), (75021, 1), (96080, 1)],
        )
        + event_set_deck_response_line("course-1", "deck-abc", "Lagaan")
        + match_room_state_playing_line(
            "match-1", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID
        )
        + gre_full_line(
            our_seat_id=1, hand_zone_id=35,
            hand=[(1, 75022), (2, 75021), (3, 96080)],  # all three, sent back
        )
        + gre_diff_mulligan_line(
            old_instance_ids=[1, 2, 3],
            new_hand=[(11, 75021)],  # kept hand: Plains only
            hand_zone_id=35,
            our_seat_id=1,
            mulligan_count=1,
        )
        + gre_diff_draw_line(
            drawn_instance_id=12, drawn_grp_id=75022, hand_zone_id=35, our_seat_id=1
        )  # Island drawn later
        + match_room_state_completed_line("match-1", winning_team_id=1, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T192832", text)

    stats, _ = card_stats.collect_card_stats(cfg)

    deck_stats = stats["Lagaan"]
    assert deck_stats.mulligan_games == 1
    assert deck_stats.mulliganed_hand_tallies == {75022: 1, 75021: 1, 96080: 1}
    # The kept hand (Plains) is scored as OH, not folded into the mulligan tally.
    assert deck_stats.card_tallies[75021].oh == 1

    full_summary = card_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "card_stats" / "lagaan.md").read_text()
    assert full_summary.decks_written == ["lagaan.md"]
    assert "## Mulliganed Hands" in content
    assert "| Sol Ring | 1 |" in content
    assert "1 of 1 game(s) had at least one mulligan" in content


def test_opponent_commander_tallied_and_rendered_for_brawl_deck(cfg, tmp_path):
    write_carddb_snapshot(
        cfg, tmp_path, {75022: "Island", 96080: "Sol Ring", 90302: "Krenko, Tin Street Kingpin"}
    )

    text = (
        deck_upsert_line(
            "deck-abc", "Lagaan", version="1", format="HistoricBrawl",
            main_deck=[(75022, 1)], command_zone=[(90302, 1)],
        )
        + event_set_deck_response_line("course-1", "deck-abc", "Lagaan")
        + match_room_state_playing_line(
            "match-1", [(OUR_ID, OUR_SEAT, 1), (OPPONENT_ID, OPPONENT_SEAT, 2)], our_id=OUR_ID
        )
        + gre_full_line(
            our_seat_id=OUR_SEAT,
            hand_zone_id=35,
            hand=[(1, 75022)],
            command_zone_id=26,
            command_zone_cards=[(10, 90302, OUR_SEAT), (11, 96080, OPPONENT_SEAT)],
        )
        + match_room_state_completed_line("match-1", winning_team_id=1, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T192832", text)

    stats, _ = card_stats.collect_card_stats(cfg)

    tallies = stats["Lagaan"].opponent_commander_tallies
    assert tallies[96080].games == 1
    assert tallies[96080].wins == 1
    assert 90302 not in tallies  # our own commander is never tallied as an opponent

    full_summary = card_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "card_stats" / "lagaan.md").read_text()
    assert full_summary.decks_written == ["lagaan.md"]
    assert "## Opponent Commanders" in content
    assert "Sol Ring" in content
    assert "100% (1/1)" in content


def test_opponent_commander_early_concede_uses_turn_6_not_turn_4_cutoff(cfg, tmp_path):
    write_carddb_snapshot(
        cfg, tmp_path, {75022: "Island", 96080: "Sol Ring", 90302: "Krenko, Tin Street Kingpin"}
    )

    text = (
        deck_upsert_line(
            "deck-abc", "Lagaan", version="1", format="HistoricBrawl",
            main_deck=[(75022, 1)], command_zone=[(90302, 1)],
        )
        + event_set_deck_response_line("course-1", "deck-abc", "Lagaan")
        + match_room_state_playing_line(
            "match-1", [(OUR_ID, OUR_SEAT, 1), (OPPONENT_ID, OPPONENT_SEAT, 2)], our_id=OUR_ID
        )
        + gre_full_line(
            our_seat_id=OUR_SEAT,
            hand_zone_id=35,
            hand=[(1, 75022)],
            command_zone_id=26,
            command_zone_cards=[(10, 90302, OUR_SEAT), (11, 96080, OPPONENT_SEAT)],
        )
        + gre_diff_turn_line(turn_number=6)
        + match_room_state_completed_line(
            "match-1", winning_team_id=2, reason="ResultReason_Concede", our_id=OUR_ID
        )
    )
    _write_session(cfg, "20260817T192832", text)

    stats, _ = card_stats.collect_card_stats(cfg)
    deck_stats = stats["Lagaan"]

    # Turn 6 is past EARLY_FORFEIT_MAX_TURN (4) — the card-in-hand tracker
    # doesn't count it...
    assert deck_stats.early_forfeit_games == 0
    # ...but the opponent-commander tracker uses a looser cutoff (turn 6).
    tally = deck_stats.opponent_commander_tallies[96080]
    assert tally.games == 1 and tally.wins == 0
    assert tally.early_concedes == 1

    card_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "card_stats" / "lagaan.md").read_text()
    assert "**Commander with most early concedes:** Sol Ring (1 of 1 game(s))" in content


def test_non_brawl_deck_report_omits_opponent_commanders_section(cfg, tmp_path):
    write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})
    text = _match_session_text(
        "deck-abc", "Lagaan", course_id="course-1", match_id="match-1", winning_team_id=1,
        format="Standard",
    )
    _write_session(cfg, "20260817T192832", text)

    card_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "card_stats" / "lagaan.md").read_text()

    assert "Opponent Commanders" not in content


def test_own_commander_grp_ids_populated_from_command_zone(cfg, tmp_path):
    write_carddb_snapshot(
        cfg, tmp_path, {75022: "Island", 90302: "Krenko, Tin Street Kingpin"}
    )
    text = deck_upsert_line(
        "deck-abc", "Lagaan", version="1", format="HistoricBrawl",
        main_deck=[(75022, 1)], command_zone=[(90302, 1)],
    )
    _write_session(cfg, "20260817T192832", text)

    stats, _ = card_stats.collect_card_stats(cfg)

    assert stats["Lagaan"].own_commander_grp_ids == frozenset({90302})


def test_own_commander_grp_ids_empty_for_non_brawl_deck(cfg, tmp_path):
    write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})
    text = deck_upsert_line(
        "deck-abc", "Lagaan", version="1", format="Standard", main_deck=[(75022, 1)],
    )
    _write_session(cfg, "20260817T192832", text)

    stats, _ = card_stats.collect_card_stats(cfg)

    assert stats["Lagaan"].own_commander_grp_ids == frozenset()


def test_own_commander_grp_ids_includes_both_partner_commanders(cfg, tmp_path):
    write_carddb_snapshot(
        cfg, tmp_path, {75022: "Island", 90302: "Krenko, Tin Street Kingpin", 96080: "Sol Ring"}
    )
    text = deck_upsert_line(
        "deck-abc", "Lagaan", version="1", format="HistoricBrawl",
        main_deck=[(75022, 1)], command_zone=[(90302, 1), (96080, 1)],
    )
    _write_session(cfg, "20260817T192832", text)

    stats, _ = card_stats.collect_card_stats(cfg)

    assert stats["Lagaan"].own_commander_grp_ids == frozenset({90302, 96080})


def test_deck_reissued_under_same_name_keeps_all_time_stats_continuous(cfg, tmp_path):
    """Delete+recreate under the same name gets a new deck_id from Arena —
    the all-time report must keep counting across that boundary, not reset."""
    write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})
    first_match = _match_session_text(
        "deck-one", "Foo", course_id="course-1", match_id="match-1", winning_team_id=1
    )
    _write_session(cfg, "20260817T100000", first_match, suffix="aaaaaaaa")

    second_match = _match_session_text(
        "deck-two", "Foo", course_id="course-2", match_id="match-2", winning_team_id=2
    )
    _write_session(cfg, "20260817T110000", second_match, suffix="bbbbbbbb")

    stats, summary = card_stats.collect_card_stats(cfg)

    assert summary.matches_found == 2
    assert stats["Foo"].gp == 2
    assert stats["Foo"].gp_wins == 1

    full_summary = card_stats.run(cfg)
    assert full_summary.decks_written == ["foo.md"]
    content = (cfg.archive_dir / "reports" / "card_stats" / "foo.md").read_text()
    assert "50% (1/2)" in content  # Games Played line


def test_land_use_section_reports_basics_split_wins_and_losses(cfg, tmp_path):
    PLAINS = 91301
    write_carddb_snapshot(
        cfg, tmp_path, {PLAINS: "Plains"}, lands={PLAINS: {"types": "5", "supertypes": "1"}}
    )
    deck_save = deck_upsert_line(
        "deck-abc", "Lagaan", version="1", main_deck=[(PLAINS, 3)]
    ) + event_set_deck_response_line("course-1", "deck-abc", "Lagaan")

    win_game = (
        deck_save
        + match_room_state_playing_line("match-1", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID)
        + gre_full_line(our_seat_id=1, hand_zone_id=35, hand=[(1, PLAINS)])
        + gre_diff_play_land_line(
            instance_id=1, grp_id=PLAINS, hand_zone_id=35, battlefield_zone_id=28, our_seat_id=1
        )
        + gre_diff_draw_line(drawn_instance_id=2, drawn_grp_id=PLAINS, hand_zone_id=35, our_seat_id=1)
        + gre_diff_play_land_line(
            instance_id=2, grp_id=PLAINS, hand_zone_id=35, battlefield_zone_id=28, our_seat_id=1
        )
        + match_room_state_completed_line("match-1", winning_team_id=1, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T100000", win_game, suffix="aaaaaaaa")

    loss_game = (
        deck_save
        + match_room_state_playing_line("match-2", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID)
        + gre_full_line(our_seat_id=1, hand_zone_id=35, hand=[(3, PLAINS)])
        + gre_diff_play_land_line(
            instance_id=3, grp_id=PLAINS, hand_zone_id=35, battlefield_zone_id=28, our_seat_id=1
        )
        + match_room_state_completed_line("match-2", winning_team_id=2, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T110000", loss_game, suffix="bbbbbbbb")

    full_summary = card_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "card_stats" / "lagaan.md").read_text()

    assert full_summary.decks_written == ["lagaan.md"]
    assert "## Land Use" in content
    assert (
        "**Lands played per game:** 1.50 (3/2) overall — 2.00 (2/1) in wins — 1.00 (1/1) in losses"
        in content
    )
    assert "### Basic Lands" in content
    assert "| Plains | 1.50 (3/2) | 2.00 (2/1) | 1.00 (1/1) |" in content


def test_land_use_section_reports_kept_vs_mulliganed_hand_land_counts(cfg, tmp_path):
    PLAINS, ISLAND, FOREST, SOL_RING = 91301, 75022, 91300, 96080
    write_carddb_snapshot(
        cfg,
        tmp_path,
        {PLAINS: "Plains", ISLAND: "Island", FOREST: "Forest", SOL_RING: "Sol Ring"},
        lands={
            PLAINS: {"types": "5", "supertypes": "1"},
            ISLAND: {"types": "5", "supertypes": "1"},
            FOREST: {"types": "5", "supertypes": "1"},
        },
    )
    deck_save = deck_upsert_line(
        "deck-abc", "Lagaan", version="1",
        main_deck=[(PLAINS, 4), (ISLAND, 4), (FOREST, 4), (SOL_RING, 1)],
    ) + event_set_deck_response_line("course-1", "deck-abc", "Lagaan")

    # Win: kept hand (no mulligan) has 2 lands (Plains, Island).
    win_game = (
        deck_save
        + match_room_state_playing_line("match-1", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID)
        + gre_full_line(our_seat_id=1, hand_zone_id=35, hand=[(1, PLAINS), (2, ISLAND)])
        + gre_diff_draw_line(drawn_instance_id=3, drawn_grp_id=SOL_RING, hand_zone_id=35, our_seat_id=1)
        + match_room_state_completed_line("match-1", winning_team_id=1, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T100000", win_game, suffix="aaaaaaaa")

    # Loss: kept hand (no mulligan) has 1 land (Plains).
    loss_game = (
        deck_save
        + match_room_state_playing_line("match-2", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID)
        + gre_full_line(our_seat_id=1, hand_zone_id=35, hand=[(4, PLAINS)])
        + gre_diff_draw_line(drawn_instance_id=5, drawn_grp_id=SOL_RING, hand_zone_id=35, our_seat_id=1)
        + match_room_state_completed_line("match-2", winning_team_id=2, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T110000", loss_game, suffix="bbbbbbbb")

    # Loss, with a mulligan: sent-back hand has 3 lands (Plains, Island,
    # Forest); kept hand (after the mulligan) has 1 land (Plains).
    mulligan_game = (
        deck_save
        + match_room_state_playing_line("match-3", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID)
        + gre_full_line(our_seat_id=1, hand_zone_id=35, hand=[(6, PLAINS), (7, ISLAND), (8, FOREST)])
        + gre_diff_mulligan_line(
            old_instance_ids=[6, 7, 8],
            new_hand=[(16, PLAINS)],
            hand_zone_id=35,
            our_seat_id=1,
            mulligan_count=1,
        )
        + gre_diff_draw_line(drawn_instance_id=17, drawn_grp_id=SOL_RING, hand_zone_id=35, our_seat_id=1)
        + match_room_state_completed_line("match-3", winning_team_id=2, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T120000", mulligan_game, suffix="cccccccc")

    card_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "card_stats" / "lagaan.md").read_text()

    assert (
        "**Lands in hand:** kept hand 1.33 (4/3) overall — 2.00 (2/1) in wins — "
        "1.00 (2/2) in losses — vs. 3.00 (3/1) in a hand sent back on a mulligan"
        in content
    )
    assert (
        "**Lands in hand:** sent back 3.00 (3/1) — vs. kept 1.33 (4/3)" in content
    )


def test_land_use_section_reports_nonbasic_avg_turn_played(cfg, tmp_path):
    OVERGROWN_TOMB = 68734
    write_carddb_snapshot(
        cfg, tmp_path, {OVERGROWN_TOMB: "Overgrown Tomb"}, lands={OVERGROWN_TOMB: {"types": "5"}}
    )
    deck_save = deck_upsert_line(
        "deck-abc", "Lagaan", version="1", main_deck=[(OVERGROWN_TOMB, 1)]
    ) + event_set_deck_response_line("course-1", "deck-abc", "Lagaan")

    game1 = (
        deck_save
        + match_room_state_playing_line("match-1", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID)
        + gre_full_line(our_seat_id=1, hand_zone_id=35, hand=[(1, OVERGROWN_TOMB)])
        + gre_diff_turn_line(turn_number=2)
        + gre_diff_play_land_line(
            instance_id=1, grp_id=OVERGROWN_TOMB, hand_zone_id=35, battlefield_zone_id=28, our_seat_id=1
        )
        + match_room_state_completed_line("match-1", winning_team_id=1, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T100000", game1, suffix="aaaaaaaa")

    game2 = (
        deck_save
        + match_room_state_playing_line("match-2", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID)
        + gre_full_line(our_seat_id=1, hand_zone_id=35, hand=[(1, OVERGROWN_TOMB)])
        + gre_diff_turn_line(turn_number=4)
        + gre_diff_play_land_line(
            instance_id=1, grp_id=OVERGROWN_TOMB, hand_zone_id=35, battlefield_zone_id=28, our_seat_id=1
        )
        + match_room_state_completed_line("match-2", winning_team_id=2, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T110000", game2, suffix="bbbbbbbb")

    card_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "card_stats" / "lagaan.md").read_text()

    assert "### Non-Basic Lands" in content
    assert "| Overgrown Tomb | 3.00 (6/2) |" in content


def test_land_use_section_reports_dfc_other_side_cast_rate(cfg, tmp_path):
    JIDOOR, OVERTURE = 96164, 96165
    write_carddb_snapshot(
        cfg,
        tmp_path,
        {JIDOOR: "Jidoor, Aristocratic Capital", OVERTURE: "Overture"},
        lands={JIDOOR: {"types": "5", "linked_face_grp_ids": str(OVERTURE)}},
    )
    deck_save = deck_upsert_line(
        "deck-abc", "Lagaan", version="1", main_deck=[(JIDOOR, 1)]
    ) + event_set_deck_response_line("course-1", "deck-abc", "Lagaan")

    played_as_land = (
        deck_save
        + match_room_state_playing_line("match-1", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID)
        + gre_full_line(our_seat_id=1, hand_zone_id=35, hand=[(1, JIDOOR)])
        + gre_diff_play_land_line(
            instance_id=1, grp_id=JIDOOR, hand_zone_id=35, battlefield_zone_id=28, our_seat_id=1
        )
        + match_room_state_completed_line("match-1", winning_team_id=1, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T100000", played_as_land, suffix="aaaaaaaa")

    cast_as_spell = (
        deck_save
        + match_room_state_playing_line("match-2", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID)
        + gre_full_line(our_seat_id=1, hand_zone_id=35, hand=[(2, OVERTURE)])
        + gre_diff_cast_spell_line(
            instance_id=2, grp_id=OVERTURE, hand_zone_id=35, stack_zone_id=27, our_seat_id=1
        )
        + match_room_state_completed_line("match-2", winning_team_id=2, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T110000", cast_as_spell, suffix="bbbbbbbb")

    card_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "card_stats" / "lagaan.md").read_text()

    assert "### Modal/Adventure Faces" in content
    assert "| Jidoor, Aristocratic Capital | 1 | 1 | 50% (1/2) |" in content


def test_land_use_section_reports_non_mana_ability_activation_split_wins_and_losses(cfg, tmp_path):
    CASTLE_VANTRESS = 70389
    MANA_ABILITY, SCRY_ABILITY = 1002, 50001
    write_carddb_snapshot(
        cfg,
        tmp_path,
        {CASTLE_VANTRESS: "Castle Vantress"},
        lands={CASTLE_VANTRESS: {"types": "5", "ability_ids": f"{MANA_ABILITY}:0,{SCRY_ABILITY}:0"}},
        abilities={MANA_ABILITY: (1, 1), SCRY_ABILITY: (1, 15)},
    )
    deck_save = deck_upsert_line(
        "deck-abc", "Lagaan", version="1", main_deck=[(CASTLE_VANTRESS, 1)]
    ) + event_set_deck_response_line("course-1", "deck-abc", "Lagaan")

    win_game = (
        deck_save
        + match_room_state_playing_line("match-1", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID)
        + gre_full_line(our_seat_id=1, hand_zone_id=35, hand=[(1, CASTLE_VANTRESS)])
        + gre_diff_play_land_line(
            instance_id=1, grp_id=CASTLE_VANTRESS, hand_zone_id=35, battlefield_zone_id=28, our_seat_id=1
        )
        # A non-mana ability activation (tracked) and a mana ability
        # activation (excluded) in the same game.
        + gre_diff_activate_ability_line(
            source_instance_id=1, ability_instance_id=900, ability_id=SCRY_ABILITY, acting_seat_id=1
        )
        + gre_diff_activate_ability_line(
            source_instance_id=1, ability_instance_id=901, ability_id=MANA_ABILITY, acting_seat_id=1
        )
        + match_room_state_completed_line("match-1", winning_team_id=1, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T100000", win_game, suffix="aaaaaaaa")

    loss_game = (
        deck_save
        + match_room_state_playing_line("match-2", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID)
        + gre_full_line(our_seat_id=1, hand_zone_id=35, hand=[(1, CASTLE_VANTRESS)])
        + gre_diff_play_land_line(
            instance_id=1, grp_id=CASTLE_VANTRESS, hand_zone_id=35, battlefield_zone_id=28, our_seat_id=1
        )
        + gre_diff_activate_ability_line(
            source_instance_id=1, ability_instance_id=902, ability_id=SCRY_ABILITY, acting_seat_id=1
        )
        + match_room_state_completed_line("match-2", winning_team_id=2, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T110000", loss_game, suffix="bbbbbbbb")

    card_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "card_stats" / "lagaan.md").read_text()

    assert "### Non-Mana Abilities" in content
    assert "| Castle Vantress | 1.00 (1/1) | 1.00 (1/1) |" in content


def test_missing_carddb_surfaces_as_warning_not_exception(cfg):
    text = _match_session_text(
        "deck-abc", "Lagaan", course_id="course-1", match_id="match-1", winning_team_id=1
    )
    _write_session(cfg, "20260817T192832", text)

    summary = card_stats.run(cfg)

    assert summary.decks_written == []
    assert summary.warnings
    assert "carddb" in summary.warnings[0]
