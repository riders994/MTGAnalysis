"""End-to-end: archived sessions -> per-deck card-performance reports."""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

from analysis import card_stats

from conftest import (
    deck_upsert_line,
    event_set_deck_response_line,
    gre_diff_draw_line,
    gre_diff_turn_line,
    gre_full_line,
    match_room_state_completed_line,
    match_room_state_playing_line,
)

OUR_SEAT = 1
OPPONENT_SEAT = 2

OUR_ID = "OURCLIENTID"
OPPONENT_ID = "OPPONENTID"


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
    _write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})
    text = _match_session_text(
        "deck-abc", "Lagaan", course_id="course-1", match_id="match-1", winning_team_id=1
    )
    _write_session(cfg, "20260817T192832", text)

    stats, summary = card_stats.collect_card_stats(cfg)

    assert summary.sessions_scanned == 1
    assert summary.matches_found == 1
    assert stats["deck-abc"].gp == 1
    assert stats["deck-abc"].gp_wins == 1
    assert stats["deck-abc"].card_tallies == {}

    full_summary = card_stats.run(cfg)
    assert full_summary.decks_written == ["lagaan.md"]
    content = (cfg.archive_dir / "card_stats" / "lagaan.md").read_text()
    assert "# Lagaan" in content
    assert "100% (1/1)" in content  # Games Played line
    assert "No per-card data yet." in content


def test_lost_match_win_rate_is_zero(cfg, tmp_path):
    _write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})
    text = _match_session_text(
        "deck-abc", "Lagaan", course_id="course-1", match_id="match-1", winning_team_id=2
    )
    _write_session(cfg, "20260817T192832", text)

    stats, _ = card_stats.collect_card_stats(cfg)

    assert stats["deck-abc"].gp == 1
    assert stats["deck-abc"].gp_wins == 0


def test_match_bound_to_unknown_deck_warns_and_is_skipped(cfg, tmp_path):
    _write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})
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
    _write_carddb_snapshot(cfg, tmp_path, {75022: "Island", 75021: "Plains", 96080: "Sol Ring"})

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
    tallies = stats["deck-abc"].card_tallies
    assert tallies[75022].oh == 1 and tallies[75022].oh_wins == 1
    assert tallies[75021].gd == 1 and tallies[75021].gd_wins == 1
    assert tallies[96080].gns == 1 and tallies[96080].gns_wins == 1

    full_summary = card_stats.run(cfg)
    content = (cfg.archive_dir / "card_stats" / "lagaan.md").read_text()
    assert full_summary.decks_written == ["lagaan.md"]
    assert "Island" in content and "100% (1/1)" in content
    assert "No per-card data yet." not in content


def test_opponent_commander_tallied_and_rendered_for_brawl_deck(cfg, tmp_path):
    _write_carddb_snapshot(
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

    tallies = stats["deck-abc"].opponent_commander_tallies
    assert tallies[96080].games == 1
    assert tallies[96080].wins == 1
    assert 90302 not in tallies  # our own commander is never tallied as an opponent

    full_summary = card_stats.run(cfg)
    content = (cfg.archive_dir / "card_stats" / "lagaan.md").read_text()
    assert full_summary.decks_written == ["lagaan.md"]
    assert "## Opponent Commanders" in content
    assert "Sol Ring" in content
    assert "100% (1/1)" in content


def test_opponent_commander_early_concede_uses_turn_6_not_turn_4_cutoff(cfg, tmp_path):
    _write_carddb_snapshot(
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
    deck_stats = stats["deck-abc"]

    # Turn 6 is past EARLY_FORFEIT_MAX_TURN (4) — the card-in-hand tracker
    # doesn't count it...
    assert deck_stats.early_forfeit_games == 0
    # ...but the opponent-commander tracker uses a looser cutoff (turn 6).
    tally = deck_stats.opponent_commander_tallies[96080]
    assert tally.games == 1 and tally.wins == 0
    assert tally.early_concedes == 1

    card_stats.run(cfg)
    content = (cfg.archive_dir / "card_stats" / "lagaan.md").read_text()
    assert "**Commander with most early concedes:** Sol Ring (1 of 1 game(s))" in content


def test_non_brawl_deck_report_omits_opponent_commanders_section(cfg, tmp_path):
    _write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})
    text = _match_session_text(
        "deck-abc", "Lagaan", course_id="course-1", match_id="match-1", winning_team_id=1,
        format="Standard",
    )
    _write_session(cfg, "20260817T192832", text)

    card_stats.run(cfg)
    content = (cfg.archive_dir / "card_stats" / "lagaan.md").read_text()

    assert "Opponent Commanders" not in content


def test_own_commander_grp_ids_populated_from_command_zone(cfg, tmp_path):
    _write_carddb_snapshot(
        cfg, tmp_path, {75022: "Island", 90302: "Krenko, Tin Street Kingpin"}
    )
    text = deck_upsert_line(
        "deck-abc", "Lagaan", version="1", format="HistoricBrawl",
        main_deck=[(75022, 1)], command_zone=[(90302, 1)],
    )
    _write_session(cfg, "20260817T192832", text)

    stats, _ = card_stats.collect_card_stats(cfg)

    assert stats["deck-abc"].own_commander_grp_ids == frozenset({90302})


def test_own_commander_grp_ids_empty_for_non_brawl_deck(cfg, tmp_path):
    _write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})
    text = deck_upsert_line(
        "deck-abc", "Lagaan", version="1", format="Standard", main_deck=[(75022, 1)],
    )
    _write_session(cfg, "20260817T192832", text)

    stats, _ = card_stats.collect_card_stats(cfg)

    assert stats["deck-abc"].own_commander_grp_ids == frozenset()


def test_own_commander_grp_ids_includes_both_partner_commanders(cfg, tmp_path):
    _write_carddb_snapshot(
        cfg, tmp_path, {75022: "Island", 90302: "Krenko, Tin Street Kingpin", 96080: "Sol Ring"}
    )
    text = deck_upsert_line(
        "deck-abc", "Lagaan", version="1", format="HistoricBrawl",
        main_deck=[(75022, 1)], command_zone=[(90302, 1), (96080, 1)],
    )
    _write_session(cfg, "20260817T192832", text)

    stats, _ = card_stats.collect_card_stats(cfg)

    assert stats["deck-abc"].own_commander_grp_ids == frozenset({90302, 96080})


def test_missing_carddb_surfaces_as_warning_not_exception(cfg):
    text = _match_session_text(
        "deck-abc", "Lagaan", course_id="course-1", match_id="match-1", winning_team_id=1
    )
    _write_session(cfg, "20260817T192832", text)

    summary = card_stats.run(cfg)

    assert summary.decks_written == []
    assert summary.warnings
    assert "carddb" in summary.warnings[0]
