"""Brawl bracket-signal computation and rendering."""

from __future__ import annotations

import gzip
from pathlib import Path

from analysis import bracket_stats
from analysis.bracket_report import render_bracket_stats
from analysis.card_stats import DeckStats, OpponentCommanderTally

from conftest import (
    deck_upsert_line,
    event_set_deck_response_line,
    gre_full_line,
    match_room_state_completed_line,
    match_room_state_playing_line,
    write_carddb_snapshot,
)

OUR_SEAT = 1
OPPONENT_SEAT = 2

OUR_ID = "OURCLIENTID"
OPPONENT_ID = "OPPONENTID"

ISLAND = 75022
C1 = 100001  # our commander, shared by two decks
TERGRID = 200001  # High-tier reference commander
TATYOVA = 200002  # Lower-tier reference commander
KRENKO_MOB_BOSS = 200003  # Lower-tier reference commander
KRENKO_TIN_STREET = 90302  # a DIFFERENT card, used elsewhere in the test suite
GENERIC_OPPONENT = 200004  # not in the reference table


def _write_session(cfg, session_id: str, text: str, *, suffix: str = "aaaaaaaa") -> Path:
    dest = cfg.sessions_dir / f"session-{session_id}-{suffix}.log.gz"
    dest.write_bytes(gzip.compress(text.encode("utf-8")))
    return dest


def _match_text(
    deck_id, *, course_id, match_id, our_commander, opponent_commander, winning_team_id,
    our_seat=OUR_SEAT, opponent_seat=OPPONENT_SEAT,
):
    """A single re-queue + play + reveal + complete cycle for an already
    upserted deck. Reusing (10, 11) as instance ids across matches is safe:
    each match's GRE span is parsed independently."""
    return (
        event_set_deck_response_line(course_id, deck_id, deck_id)
        + match_room_state_playing_line(
            match_id, [(OUR_ID, our_seat, 1), (OPPONENT_ID, opponent_seat, 2)], our_id=OUR_ID
        )
        + gre_full_line(
            our_seat_id=our_seat,
            hand_zone_id=35,
            hand=[(1, ISLAND)],
            command_zone_id=26,
            command_zone_cards=[(10, our_commander, our_seat), (11, opponent_commander, opponent_seat)],
        )
        + match_room_state_completed_line(match_id, winning_team_id=winning_team_id, our_id=OUR_ID)
    )


# --- classify_trend / half_win_rates (pure, no I/O) -------------------------


def test_trend_insufficient_data_below_minimum_games():
    assert bracket_stats.classify_trend([True, False, True, False]) == "Insufficient Data"


def test_trend_rising_when_second_half_wins_more():
    results = [False, False, False, True, True, True]
    assert bracket_stats.classify_trend(results) == "Rising"


def test_trend_falling_when_second_half_wins_less():
    results = [True, True, True, False, False, False]
    assert bracket_stats.classify_trend(results) == "Falling"


def test_trend_plateaued_when_halves_are_similar():
    results = [True, True, False, True, True, False]
    assert bracket_stats.classify_trend(results) == "Plateaued"


def test_trend_boundary_swing_counts_as_rising():
    # first half 2/5 = 40%, second half 3/5 = 60% -- exactly the 20-point cutoff.
    results = [True, True, False, False, False, True, True, True, False, False]
    assert bracket_stats.half_win_rates(results) == (0.4, 0.6)
    assert bracket_stats.classify_trend(results) == "Rising"


# --- estimate_brackets / bracket_index_for (pure) ---------------------------


def _deck(gp: int, gp_wins: int, *, deck_id: str = "d") -> DeckStats:
    return DeckStats(deck_id=deck_id, name=deck_id, format="HistoricBrawl", gp=gp, gp_wins=gp_wins)


def test_estimate_brackets_empty_below_two_qualifying_decks():
    # Only one deck clears BRACKET_MIN_GAMES -- nothing to find a gap against.
    deck_stats = {"a": _deck(gp=20, gp_wins=10), "b": _deck(gp=2, gp_wins=1)}
    assert bracket_stats.estimate_brackets(deck_stats, {}) == []


def test_estimate_brackets_single_band_when_no_gap_clears_threshold():
    deck_stats = {
        "a": _deck(gp=10, gp_wins=4),  # 40%
        "b": _deck(gp=10, gp_wins=5),  # 50%
        "c": _deck(gp=10, gp_wins=6),  # 60% -- biggest gap here is 10pts, below the 20pt threshold
    }
    bands = bracket_stats.estimate_brackets(deck_stats, {})
    assert len(bands) == 1
    assert set(bands[0].deck_ids) == {"a", "b", "c"}


def test_estimate_brackets_splits_on_a_big_gap():
    deck_stats = {
        "weak": _deck(gp=10, gp_wins=3),  # 30%
        "strong": _deck(gp=10, gp_wins=8),  # 80% -- 50pt gap, well past threshold
    }
    bands = bracket_stats.estimate_brackets(deck_stats, {})
    assert [b.index for b in bands] == [1, 2]
    assert bands[0].deck_ids == ["weak"]
    assert bands[1].deck_ids == ["strong"]


def test_bracket_index_for_places_value_inside_a_band():
    deck_stats = {
        "weak": _deck(gp=10, gp_wins=3),
        "strong": _deck(gp=10, gp_wins=8),
    }
    bands = bracket_stats.estimate_brackets(deck_stats, {})
    assert bracket_stats.bracket_index_for(bands, 0.30) == 1
    assert bracket_stats.bracket_index_for(bands, 0.80) == 2


def test_bracket_index_for_falls_back_to_nearest_edge_in_a_gap():
    deck_stats = {
        "weak": _deck(gp=10, gp_wins=3),  # 30%
        "strong": _deck(gp=10, gp_wins=8),  # 80%
    }
    bands = bracket_stats.estimate_brackets(deck_stats, {})
    assert bracket_stats.bracket_index_for(bands, 0.40) == 1  # closer to 30% than 80%
    assert bracket_stats.bracket_index_for(bands, 0.75) == 2  # closer to 80% than 30%


def test_bracket_index_for_none_without_any_bands():
    assert bracket_stats.bracket_index_for([], 0.5) is None


# --- DeckTrend.since_last_change_results / bracket_signal_for_deck (pure) ---


def _trend(era_start: str, results: list[bool], session_ids: list[str]) -> bracket_stats.DeckTrend:
    return bracket_stats.DeckTrend(
        deck_id="d", match_results=results, match_session_ids=session_ids, era_start_session_id=era_start,
    )


def test_since_last_change_results_is_full_history_without_a_later_era():
    # era_start_session_id == the first match's session -- nothing pre-dates it.
    trend = _trend("20260101T000000", [True, False, True], ["20260101T000000", "20260102T000000", "20260103T000000"])
    assert trend.since_last_change_results == [True, False, True]


def test_since_last_change_results_drops_games_before_the_rebuild():
    trend = _trend(
        "20260103T000000",
        [True, True, False, True],
        ["20260101T000000", "20260102T000000", "20260103T000000", "20260104T000000"],
    )
    assert trend.since_last_change_results == [False, True]


# 20 session ids, "20260101" .. "20260120" -- fixed-width and zero-padded,
# so plain string comparison sorts them chronologically like real ones.
_SESSION_IDS_20 = [f"202601{str(i).zfill(2)}T000000" for i in range(1, 21)]


def test_bracket_signal_for_deck_prefers_since_change_once_it_has_enough_games():
    stats = _deck(gp=20, gp_wins=10)  # 50% lifetime
    # Rebuilt at the 13th session -- the last 8 games (all wins) are "since change".
    trend = _trend(_SESSION_IDS_20[12], [False] * 12 + [True] * 8, _SESSION_IDS_20)
    rate, games = bracket_stats.bracket_signal_for_deck(stats, trend)
    assert (rate, games) == (1.0, 8)


def test_bracket_signal_for_deck_falls_back_to_lifetime_below_the_games_floor():
    stats = _deck(gp=20, gp_wins=10)  # 50% lifetime
    # Rebuilt very recently -- only the last 2 games are "since change".
    trend = _trend(_SESSION_IDS_20[18], [False] * 18 + [True, True], _SESSION_IDS_20)
    rate, games = bracket_stats.bracket_signal_for_deck(stats, trend)
    assert (rate, games) == (0.5, 20)


def test_bracket_signal_for_deck_without_a_trend_uses_lifetime():
    stats = _deck(gp=10, gp_wins=7)
    assert bracket_stats.bracket_signal_for_deck(stats, None) == (0.7, 10)


def test_estimate_brackets_uses_since_change_signal_to_reclassify_a_rebuilt_deck():
    # Lifetime win rate (50%) would land "rebuilt" in the same tier as
    # "steady" -- but its last 8 games (all wins, a real rebuild) put it in
    # a clearly higher tier, and that's what should win out.
    deck_stats = {
        "steady": _deck(gp=10, gp_wins=5, deck_id="steady"),  # 50%, no trend data at all
        "rebuilt": _deck(gp=20, gp_wins=10, deck_id="rebuilt"),  # 50% lifetime
    }
    deck_trends = {
        "rebuilt": _trend(_SESSION_IDS_20[12], [False] * 12 + [True] * 8, _SESSION_IDS_20),
    }
    bands = bracket_stats.estimate_brackets(deck_stats, deck_trends)
    assert [b.index for b in bands] == [1, 2]
    assert bands[0].deck_ids == ["steady"]
    assert bands[1].deck_ids == ["rebuilt"]


# --- is_ranked_brawl ---------------------------------------------------------


def test_is_ranked_brawl_classification():
    assert bracket_stats.is_ranked_brawl("HistoricBrawlRanked") is True
    assert bracket_stats.is_ranked_brawl("HistoricBrawl") is False
    assert bracket_stats.is_ranked_brawl("Brawl") is False
    assert bracket_stats.is_ranked_brawl(None) is False


# --- aggregate_own_commanders / aggregate_opponent_commanders (pure) --------


def test_aggregate_own_commanders_sums_across_decks_sharing_a_commander():
    deck_stats = {
        "deck-a": DeckStats(
            deck_id="deck-a", name="A", format="HistoricBrawl",
            gp=2, gp_wins=1, own_commander_grp_ids=frozenset({C1}),
        ),
        "deck-b": DeckStats(
            deck_id="deck-b", name="B", format="HistoricBrawl",
            gp=3, gp_wins=2, own_commander_grp_ids=frozenset({C1}),
        ),
    }

    pilots = bracket_stats.aggregate_own_commanders(deck_stats)

    assert pilots[C1].deck_ids == {"deck-a", "deck-b"}
    assert pilots[C1].gp == 5
    assert pilots[C1].gp_wins == 3


def test_aggregate_opponent_commanders_merges_across_decks():
    deck_stats = {
        "deck-a": DeckStats(
            deck_id="deck-a", name="A", format="HistoricBrawl",
            opponent_commander_tallies={TERGRID: OpponentCommanderTally(games=2, wins=1, early_concedes=1)},
        ),
        "deck-b": DeckStats(
            deck_id="deck-b", name="B", format="HistoricBrawl",
            opponent_commander_tallies={TERGRID: OpponentCommanderTally(games=1, wins=0, early_concedes=0)},
        ),
    }

    merged = bracket_stats.aggregate_opponent_commanders(deck_stats)

    assert merged[TERGRID].games == 3
    assert merged[TERGRID].wins == 1
    assert merged[TERGRID].early_concedes == 1


# --- collect_bracket_stats end-to-end ---------------------------------------


def test_collect_bracket_stats_segments_ranked_casual_and_excludes_non_brawl(cfg, tmp_path):
    write_carddb_snapshot(cfg, tmp_path, {ISLAND: "Island", C1: "Our Commander", TERGRID: "Tergrid, God of Fright"})

    text = (
        deck_upsert_line(
            "deck-a", "Deck A", format="HistoricBrawl", main_deck=[(ISLAND, 1)], command_zone=[(C1, 1)],
        )
        + _match_text("deck-a", course_id="c1", match_id="m1", our_commander=C1, opponent_commander=TERGRID, winning_team_id=1)
        + deck_upsert_line(
            "deck-b", "Deck B", format="HistoricBrawlRanked", main_deck=[(ISLAND, 1)], command_zone=[(C1, 1)],
        )
        + _match_text("deck-b", course_id="c2", match_id="m2", our_commander=C1, opponent_commander=TERGRID, winning_team_id=1)
        + deck_upsert_line("deck-c", "Deck C", format="Standard", main_deck=[(ISLAND, 1)])
        + event_set_deck_response_line("c3", "deck-c", "deck-c")
        + match_room_state_playing_line("m3", [(OUR_ID, OUR_SEAT, 1), (OPPONENT_ID, OPPONENT_SEAT, 2)], our_id=OUR_ID)
        + match_room_state_completed_line("m3", winning_team_id=1, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T192832", text)

    data, summary = bracket_stats.collect_bracket_stats(cfg)

    assert "Deck A" in data.casual.deck_stats
    assert "Deck B" in data.ranked.deck_stats
    assert "Deck C" not in data.casual.deck_stats and "Deck C" not in data.ranked.deck_stats
    assert data.casual.commander_pilots[C1].gp == 1
    assert data.ranked.commander_pilots[C1].gp == 1
    assert summary.matches_found == 3


# --- rendered report ---------------------------------------------------------


def test_run_writes_overview_with_limitations_and_tables(cfg, tmp_path):
    write_carddb_snapshot(
        cfg, tmp_path,
        {ISLAND: "Island", C1: "Our Commander", TERGRID: "Tergrid, God of Fright"},
    )
    text = (
        deck_upsert_line(
            "deck-a", "Deck A", format="HistoricBrawl", main_deck=[(ISLAND, 1)], command_zone=[(C1, 1)],
        )
        + _match_text("deck-a", course_id="c1", match_id="m1", our_commander=C1, opponent_commander=TERGRID, winning_team_id=1)
    )
    _write_session(cfg, "20260817T192832", text)

    summary = bracket_stats.run(cfg)

    assert summary.files_written == ["overview.md"]
    content = (cfg.archive_dir / "reports" / "bracket_stats" / "overview.md").read_text()
    assert "## Limitations" in content
    assert "pure MMR" in content
    assert "Tergrid, God of Fright" in content
    assert "Our Commander" in content


def test_reference_tier_annotation_and_krenko_naming_collision(cfg, tmp_path):
    write_carddb_snapshot(
        cfg, tmp_path,
        {
            ISLAND: "Island", C1: "Our Commander",
            TERGRID: "Tergrid, God of Fright",
            KRENKO_TIN_STREET: "Krenko, Tin Street Kingpin",
            KRENKO_MOB_BOSS: "Krenko, Mob Boss",
        },
    )
    text = (
        deck_upsert_line(
            "deck-a", "Deck A", format="HistoricBrawl", main_deck=[(ISLAND, 1)], command_zone=[(C1, 1)],
        )
        + _match_text("deck-a", course_id="c1", match_id="m1", our_commander=C1, opponent_commander=TERGRID, winning_team_id=2)
        + _match_text("deck-a", course_id="c2", match_id="m2", our_commander=C1, opponent_commander=KRENKO_TIN_STREET, winning_team_id=1)
        + _match_text("deck-a", course_id="c3", match_id="m3", our_commander=C1, opponent_commander=KRENKO_MOB_BOSS, winning_team_id=1)
    )
    _write_session(cfg, "20260817T192832", text)

    data, _ = bracket_stats.collect_bracket_stats(cfg)
    from analysis.carddb import load_card_names
    names = load_card_names(cfg)
    content = render_bracket_stats(data, names)

    # Tergrid (High) and Krenko Mob Boss (Lower) get annotated...
    assert "| Tergrid, God of Fright | 1 | 0% (0/1) | 0% (0/1) | High |" in content
    assert "| Krenko, Mob Boss | 1 | 100% (1/1) | 0% (0/1) | Lower |" in content
    # ...but the differently-named Kingpin card does not.
    assert "| Krenko, Tin Street Kingpin | 1 | 100% (1/1) | 0% (0/1) | — |" in content


def test_reference_check_matches_expectation(cfg, tmp_path):
    write_carddb_snapshot(
        cfg, tmp_path,
        {
            ISLAND: "Island", C1: "Our Commander",
            TERGRID: "Tergrid, God of Fright",  # High tier
            TATYOVA: "Tatyova, Benthic Druid",  # Lower tier
        },
    )
    text = (
        deck_upsert_line(
            "deck-a", "Deck A", format="HistoricBrawl", main_deck=[(ISLAND, 1)], command_zone=[(C1, 1)],
        )
        # We lose to the High-tier commander...
        + _match_text("deck-a", course_id="c1", match_id="m1", our_commander=C1, opponent_commander=TERGRID, winning_team_id=2)
        # ...and beat the Lower-tier one.
        + _match_text("deck-a", course_id="c2", match_id="m2", our_commander=C1, opponent_commander=TATYOVA, winning_team_id=1)
    )
    _write_session(cfg, "20260817T192832", text)

    summary = bracket_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "bracket_stats" / "overview.md").read_text()

    assert summary.files_written == ["overview.md"]
    assert "matches expectation" in content


def test_reference_check_reports_insufficient_data_with_one_tier_only(cfg, tmp_path):
    write_carddb_snapshot(
        cfg, tmp_path,
        {ISLAND: "Island", C1: "Our Commander", GENERIC_OPPONENT: "Some Rando Commander"},
    )
    text = (
        deck_upsert_line(
            "deck-a", "Deck A", format="HistoricBrawl", main_deck=[(ISLAND, 1)], command_zone=[(C1, 1)],
        )
        + _match_text("deck-a", course_id="c1", match_id="m1", our_commander=C1, opponent_commander=GENERIC_OPPONENT, winning_team_id=1)
    )
    _write_session(cfg, "20260817T192832", text)

    bracket_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "bracket_stats" / "overview.md").read_text()

    assert "not enough reference-commander encounters yet" in content


def test_overlap_between_piloted_and_faced_commander_is_noted(cfg, tmp_path):
    write_carddb_snapshot(
        cfg, tmp_path,
        {ISLAND: "Island", C1: "Our Commander", TERGRID: "Tergrid, God of Fright"},
    )
    text = (
        # We pilot C1 ourselves in deck-a...
        deck_upsert_line(
            "deck-a", "Deck A", format="HistoricBrawl", main_deck=[(ISLAND, 1)], command_zone=[(C1, 1)],
        )
        + _match_text("deck-a", course_id="c1", match_id="m1", our_commander=C1, opponent_commander=TERGRID, winning_team_id=1)
        # ...and, in deck-b, face an opponent piloting C1 too.
        + deck_upsert_line(
            "deck-b", "Deck B", format="HistoricBrawl", main_deck=[(ISLAND, 1)], command_zone=[(TERGRID, 1)],
        )
        + _match_text("deck-b", course_id="c2", match_id="m2", our_commander=TERGRID, opponent_commander=C1, winning_team_id=1)
    )
    _write_session(cfg, "20260817T192832", text)

    bracket_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "bracket_stats" / "overview.md").read_text()

    assert "You've also piloted **Our Commander** yourself" in content


def test_reprinted_commander_is_merged_by_name_not_split_by_grp_id(cfg, tmp_path):
    # Two different grp_ids (reprints) sharing one display name — Arena
    # assigns a fresh grp_id per printing, so this must be folded into one
    # row rather than showing up as two half-sized ones.
    TERGRID_REPRINT = 200005
    write_carddb_snapshot(
        cfg, tmp_path,
        {
            ISLAND: "Island", C1: "Our Commander",
            TERGRID: "Tergrid, God of Fright",
            TERGRID_REPRINT: "Tergrid, God of Fright",
        },
    )
    text = (
        deck_upsert_line(
            "deck-a", "Deck A", format="HistoricBrawl", main_deck=[(ISLAND, 1)], command_zone=[(C1, 1)],
        )
        + _match_text("deck-a", course_id="c1", match_id="m1", our_commander=C1, opponent_commander=TERGRID, winning_team_id=1)
        + _match_text("deck-a", course_id="c2", match_id="m2", our_commander=C1, opponent_commander=TERGRID_REPRINT, winning_team_id=2)
    )
    _write_session(cfg, "20260817T192832", text)

    bracket_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "bracket_stats" / "overview.md").read_text()

    assert "| Tergrid, God of Fright | 2 | 50% (1/2) | 0% (0/2) | High |" in content
    # The opponent table has exactly one Tergrid row (the merged one above);
    # any split-by-grp_id regression would add a second "| Tergrid... | 1 |" row.
    assert content.count("| Tergrid, God of Fright | 1 |") == 0


def test_how_many_brackets_reports_two_tiers_with_a_real_gap(cfg, tmp_path):
    write_carddb_snapshot(
        cfg, tmp_path,
        {ISLAND: "Island", C1: "Our Commander", TERGRID: "Tergrid, God of Fright", GENERIC_OPPONENT: "Some Rando Commander"},
    )
    # 20% vs 90% win rate, 10 games each -- a 70pt gap, well past BRACKET_GAP_THRESHOLD.
    weak_matches = "".join(
        _match_text(
            "deck-a", course_id=f"wc{i}", match_id=f"wm{i}",
            our_commander=C1, opponent_commander=GENERIC_OPPONENT,
            winning_team_id=2 if i < 8 else 1,
        )
        for i in range(10)
    )
    strong_matches = "".join(
        _match_text(
            "deck-b", course_id=f"sc{i}", match_id=f"sm{i}",
            our_commander=TERGRID, opponent_commander=GENERIC_OPPONENT,
            winning_team_id=1 if i < 9 else 2,
        )
        for i in range(10)
    )
    text = (
        deck_upsert_line(
            "deck-a", "Weak Deck", format="HistoricBrawl", main_deck=[(ISLAND, 1)], command_zone=[(C1, 1)],
        )
        + weak_matches
        + deck_upsert_line(
            "deck-b", "Strong Deck", format="HistoricBrawl", main_deck=[(ISLAND, 1)], command_zone=[(TERGRID, 1)],
        )
        + strong_matches
    )
    _write_session(cfg, "20260817T192832", text)

    bracket_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "bracket_stats" / "overview.md").read_text()

    assert "Casual Brawl: 2 distinct tiers found in this account's own decks:" in content
    assert "Bracket 1: 20%–20% win rate (1 deck(s))" in content
    assert "Bracket 2: 90%–90% win rate (1 deck(s))" in content
    # The same bands place each deck (and, on our own commander table, the
    # commander piloting it) in its bracket.
    assert "| Weak Deck | 10 | 20% (2/10) | — | 1 |" in content
    assert "| Strong Deck | 10 | 90% (9/10) | — | 2 |" in content
    assert "| Our Commander | 1 | 10 | 20% (2/10) | — | 1 |" in content
    assert "| Tergrid, God of Fright | 1 | 10 | 90% (9/10) | High | 2 |" in content


def test_rebuild_changes_bracket_placement_and_shows_since_last_change(cfg, tmp_path):
    write_carddb_snapshot(
        cfg, tmp_path,
        {ISLAND: "Island", C1: "Our Commander", TERGRID: "Tergrid, God of Fright", GENERIC_OPPONENT: "Some Rando Commander"},
    )

    # Old build: 4 losses.
    pre_change_losses = "".join(
        _match_text(
            "deck-a", course_id=f"pc{i}", match_id=f"pm{i}",
            our_commander=C1, opponent_commander=GENERIC_OPPONENT, winning_team_id=2,
        )
        for i in range(4)
    )
    session1 = (
        deck_upsert_line(
            "deck-a", "Rebuilt Deck", version="1", format="HistoricBrawl",
            main_deck=[(ISLAND, 1)], command_zone=[(C1, 1)],
        )
        + pre_change_losses
    )
    _write_session(cfg, "20260801T000000", session1)

    # A real decklist change -- Island count bumped 1 -> 2.
    session2 = deck_upsert_line(
        "deck-a", "Rebuilt Deck", version="2", format="HistoricBrawl",
        main_deck=[(ISLAND, 2)], command_zone=[(C1, 1)],
    )
    _write_session(cfg, "20260802T000000", session2)

    # New build: 8 wins.
    post_change_wins = "".join(
        _match_text(
            "deck-a", course_id=f"nc{i}", match_id=f"nm{i}",
            our_commander=C1, opponent_commander=GENERIC_OPPONENT, winning_team_id=1,
        )
        for i in range(8)
    )
    _write_session(cfg, "20260803T000000", post_change_wins)

    # A second, never-rebuilt deck to actually find a gap against.
    deck_b_matches = "".join(
        _match_text(
            "deck-b", course_id=f"b{i}", match_id=f"bm{i}",
            our_commander=TERGRID, opponent_commander=GENERIC_OPPONENT,
            winning_team_id=2 if i < 8 else 1,
        )
        for i in range(10)
    )
    session4 = (
        deck_upsert_line(
            "deck-b", "Steady Deck", format="HistoricBrawl", main_deck=[(ISLAND, 1)], command_zone=[(TERGRID, 1)],
        )
        + deck_b_matches
    )
    _write_session(cfg, "20260804T000000", session4)

    bracket_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "bracket_stats" / "overview.md").read_text()

    # Lifetime blends the old 0/4 and new 8/8 into 67% -- but the
    # Since Last Change column isolates the post-rebuild record...
    assert "| Rebuilt Deck | 12 | 67% (8/12) | 100% (8/8) |" in content
    # ...and that 100% is what wins for bracket placement (Bracket 2, well
    # above Steady Deck's steady 20%), not the blended 67% alone.
    assert "| Rebuilt Deck | 12 | 67% (8/12) | 100% (8/8) | 2 |" in content
    assert "| Steady Deck | 10 | 20% (2/10) | — | 1 |" in content


def test_deck_trend_rising_and_insufficient_data_render(cfg, tmp_path):
    write_carddb_snapshot(
        cfg, tmp_path, {ISLAND: "Island", C1: "Our Commander", GENERIC_OPPONENT: "Some Rando Commander"},
    )
    matches = "".join(
        _match_text(
            "deck-a", course_id=f"c{i}", match_id=f"m{i}",
            our_commander=C1, opponent_commander=GENERIC_OPPONENT,
            winning_team_id=2 if i < 3 else 1,  # first 3 losses, last 3 wins
        )
        for i in range(6)
    )
    text = (
        deck_upsert_line(
            "deck-a", "Deck A", format="HistoricBrawl", main_deck=[(ISLAND, 1)], command_zone=[(C1, 1)],
        )
        + matches
        + deck_upsert_line(
            "deck-b", "Deck B", format="HistoricBrawl", main_deck=[(ISLAND, 1)], command_zone=[(TERGRID, 1)],
        )
        + _match_text("deck-b", course_id="c-b", match_id="m-b", our_commander=TERGRID, opponent_commander=GENERIC_OPPONENT, winning_team_id=1)
    )
    _write_session(cfg, "20260817T192832", text)

    bracket_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "bracket_stats" / "overview.md").read_text()

    # Deck A is the only deck with >= BRACKET_MIN_GAMES games here, so there's
    # no second point to find a gap against -- Est. Bracket stays "—" even
    # though Deck A itself clears the games threshold.
    assert "| Deck A | 6 | 50% (3/6) | — | — | Rising | 0% → 100% |" in content
    assert "| Deck B | 1 | 100% (1/1) | — | — | Insufficient Data | — |" in content


def test_run_produces_nothing_for_archive_with_no_brawl_data(cfg, tmp_path):
    write_carddb_snapshot(cfg, tmp_path, {ISLAND: "Island"})
    text = (
        deck_upsert_line("deck-c", "Deck C", format="Standard", main_deck=[(ISLAND, 1)])
        + event_set_deck_response_line("c1", "deck-c", "deck-c")
        + match_room_state_playing_line("m1", [(OUR_ID, OUR_SEAT, 1), (OPPONENT_ID, OPPONENT_SEAT, 2)], our_id=OUR_ID)
        + match_room_state_completed_line("m1", winning_team_id=1, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T192832", text)

    summary = bracket_stats.run(cfg)

    assert summary.files_written == []
    assert summary.warnings == []


def test_run_surfaces_missing_carddb_as_warning(cfg):
    text = (
        deck_upsert_line(
            "deck-a", "Deck A", format="HistoricBrawl", main_deck=[(ISLAND, 1)], command_zone=[(C1, 1)],
        )
        + _match_text("deck-a", course_id="c1", match_id="m1", our_commander=C1, opponent_commander=TERGRID, winning_team_id=1)
    )
    _write_session(cfg, "20260817T192832", text)

    summary = bracket_stats.run(cfg)

    assert summary.files_written == []
    assert any("carddb" in warning for warning in summary.warnings)


def test_ranked_segment_reports_no_data_yet_when_only_casual_played(cfg, tmp_path):
    write_carddb_snapshot(cfg, tmp_path, {ISLAND: "Island", C1: "Our Commander", TERGRID: "Tergrid, God of Fright"})
    text = (
        deck_upsert_line(
            "deck-a", "Deck A", format="HistoricBrawl", main_deck=[(ISLAND, 1)], command_zone=[(C1, 1)],
        )
        + _match_text("deck-a", course_id="c1", match_id="m1", our_commander=C1, opponent_commander=TERGRID, winning_team_id=1)
    )
    _write_session(cfg, "20260817T192832", text)

    bracket_stats.run(cfg)
    content = (cfg.archive_dir / "reports" / "bracket_stats" / "overview.md").read_text()

    assert "### Ranked Brawl" in content
    assert "_No Ranked Brawl data yet._" in content
    assert "### Casual Brawl" in content
