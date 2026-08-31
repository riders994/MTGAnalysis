"""Brawl bracket-signal computation and rendering."""

from __future__ import annotations

import gzip
import sqlite3
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


def _write_carddb_snapshot(cfg, tmp_path: Path, cards: dict[int, str]) -> None:
    carddb_dir = cfg.snapshots_dir / "carddb"
    carddb_dir.mkdir(parents=True, exist_ok=True)

    db_path = tmp_path / "carddb.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE Cards (GrpId INTEGER PRIMARY KEY, TitleId INTEGER)")
        conn.execute("CREATE TABLE Localizations_enUS (LocId INTEGER PRIMARY KEY, Loc TEXT)")
        for grp_id, name in cards.items():
            title_id = grp_id + 1000
            conn.execute("INSERT INTO Cards (GrpId, TitleId) VALUES (?, ?)", (grp_id, title_id))
            conn.execute(
                "INSERT INTO Localizations_enUS (LocId, Loc) VALUES (?, ?)", (title_id, name)
            )
        conn.commit()
    finally:
        conn.close()

    dest = carddb_dir / "Raw_CardDatabase_test.gz"
    with open(db_path, "rb") as src, gzip.open(dest, "wb") as out:
        out.write(src.read())


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
    _write_carddb_snapshot(cfg, tmp_path, {ISLAND: "Island", C1: "Our Commander", TERGRID: "Tergrid, God of Fright"})

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
    _write_carddb_snapshot(
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
    _write_carddb_snapshot(
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
    _write_carddb_snapshot(
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
    _write_carddb_snapshot(
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
    _write_carddb_snapshot(
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


def test_deck_trend_rising_and_insufficient_data_render(cfg, tmp_path):
    _write_carddb_snapshot(
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

    assert "| Deck A | 6 | 50% (3/6) | Rising | 0% → 100% |" in content
    assert "| Deck B | 1 | 100% (1/1) | Insufficient Data | — |" in content


def test_run_produces_nothing_for_archive_with_no_brawl_data(cfg, tmp_path):
    _write_carddb_snapshot(cfg, tmp_path, {ISLAND: "Island"})
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
    _write_carddb_snapshot(cfg, tmp_path, {ISLAND: "Island", C1: "Our Commander", TERGRID: "Tergrid, God of Fright"})
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
