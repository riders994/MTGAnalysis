"""End-to-end: archived sessions -> monthly/seasonal/annual rollup reports."""

from __future__ import annotations

import gzip
import sqlite3
import textwrap
from datetime import date
from pathlib import Path

from analysis import reports
from analysis.seasons import Season

from conftest import (
    deck_upsert_line,
    event_set_deck_response_line,
    gre_full_line,
    match_room_state_completed_line,
    match_room_state_playing_line,
)

OUR_ID = "OURCLIENTID"
OPPONENT_ID = "OPPONENTID"
OUR_SEAT = 1
OPPONENT_SEAT = 2


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


def _match_session_text(deck_id, name, *, course_id, match_id, winning_team_id, format):
    return (
        deck_upsert_line(deck_id, name, version="1", format=format, main_deck=[(75022, 1)])
        + event_set_deck_response_line(course_id, deck_id, name)
        + match_room_state_playing_line(
            match_id, [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID
        )
        + match_room_state_completed_line(match_id, winning_team_id=winning_team_id, our_id=OUR_ID)
    )


def _write_seasons_toml(tmp_path: Path, toml_text: str) -> Path:
    path = tmp_path / "seasons.toml"
    path.write_text(textwrap.dedent(toml_text))
    return path


def test_annual_splits_by_format_group_monthly_excludes_limited(cfg, tmp_path, monkeypatch):
    """A Standard match and a HistoricBrawl match in the same month: annual
    produces two format summaries; monthly (Brawl/Standard/Alchemy only)
    also splits them into two summaries here since both groups qualify."""
    _write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})
    monkeypatch.setattr(reports, "load_seasons", lambda: [])

    text = _match_session_text(
        "deck-std", "Riddles", course_id="c1", match_id="m1", winning_team_id=1, format="Standard"
    ) + _match_session_text(
        "deck-brawl", "Welshie", course_id="c2", match_id="m2", winning_team_id=2, format="HistoricBrawl"
    )
    _write_session(cfg, "20260817T100000", text)

    summaries, deck_reports, summary = reports.collect_period_stats(cfg)

    assert summary.matches_found == 2
    assert ("annual", "2026", "Standard") in summaries
    assert ("annual", "2026", "Brawl") in summaries
    assert ("monthly", "2026-08", "Standard") in summaries
    assert ("monthly", "2026-08", "Brawl") in summaries
    assert summaries[("annual", "2026", "Standard")].gp == 1
    assert summaries[("annual", "2026", "Standard")].gp_wins == 1
    assert summaries[("annual", "2026", "Brawl")].gp_wins == 0

    assert ("annual", "2026", "Riddles") in deck_reports
    assert ("annual", "2026", "Welshie") in deck_reports


def test_limited_format_only_counts_toward_annual_and_seasonal_not_monthly(cfg, tmp_path, monkeypatch):
    _write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})
    monkeypatch.setattr(reports, "load_seasons", lambda: [Season("Test Season", date(2026, 1, 1))])

    text = _match_session_text(
        "deck-lim", "Draft Deck", course_id="c1", match_id="m1", winning_team_id=1,
        format="DirectGameLimited",
    )
    _write_session(cfg, "20260817T100000", text)

    summaries, _, summary = reports.collect_period_stats(cfg)

    assert ("annual", "2026", "Limited") in summaries
    assert ("seasonal", "Test Season", "Limited") in summaries
    assert not any(cadence == "monthly" for cadence, _, _ in summaries)


def test_match_before_earliest_season_excluded_from_seasonal_only(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "load_seasons", lambda: [Season("Later Season", date(2027, 1, 1))])
    _write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})

    text = _match_session_text(
        "deck-std", "Riddles", course_id="c1", match_id="m1", winning_team_id=1, format="Standard"
    )
    _write_session(cfg, "20260817T100000", text)

    summaries, _, _ = reports.collect_period_stats(cfg)

    assert ("annual", "2026", "Standard") in summaries
    assert ("monthly", "2026-08", "Standard") in summaries
    assert not any(cadence == "seasonal" for cadence, _, _ in summaries)


def test_per_deck_period_gp_matches_summary_gp_when_one_deck_per_group(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "load_seasons", lambda: [])
    _write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})

    text = _match_session_text(
        "deck-brawl", "Welshie", course_id="c1", match_id="m1", winning_team_id=1, format="HistoricBrawl"
    )
    _write_session(cfg, "20260817T100000", text)

    summaries, deck_reports, _ = reports.collect_period_stats(cfg)

    summary_stats = summaries[("annual", "2026", "Brawl")]
    deck_stats = deck_reports[("annual", "2026", "Welshie")]
    assert summary_stats.gp == deck_stats.gp == 1
    assert deck_stats.name == "Welshie"


def test_run_writes_summary_and_deck_files_under_reports_dir(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "load_seasons", lambda: [])
    _write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})

    text = _match_session_text(
        "deck-brawl", "Welshie", course_id="c1", match_id="m1", winning_team_id=1, format="HistoricBrawl"
    )
    _write_session(cfg, "20260817T100000", text)

    summary = reports.run(cfg)

    assert "annual/summary/2026-brawl.md" in summary.summaries_written
    assert "annual/decks/2026-welshie.md" in summary.decks_written
    assert "monthly/summary/2026-08-brawl.md" in summary.summaries_written
    assert "monthly/decks/2026-08-welshie.md" in summary.decks_written

    summary_content = (cfg.archive_dir / "reports" / "annual" / "summary" / "2026-brawl.md").read_text()
    assert "Annual — 2026 — Brawl" in summary_content
    assert "100% (1/1)" in summary_content

    deck_content = (cfg.archive_dir / "reports" / "annual" / "decks" / "2026-welshie.md").read_text()
    assert "# Welshie" in deck_content


def test_deck_never_seen_via_deck_upsert_warns_and_is_skipped(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "load_seasons", lambda: [])
    _write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})

    text = (
        event_set_deck_response_line("c1", "deck-unknown", "Mystery Deck")
        + match_room_state_playing_line("m1", [(OUR_ID, 1, 1), (OPPONENT_ID, 2, 2)], our_id=OUR_ID)
        + match_room_state_completed_line("m1", winning_team_id=1, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T100000", text)

    summaries, deck_reports, summary = reports.collect_period_stats(cfg)

    assert summaries == {}
    assert deck_reports == {}
    assert summary.matches_found == 1
    assert summary.warnings


def test_opponent_commanders_rolled_up_into_brawl_summary_and_deck_reports(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "load_seasons", lambda: [])
    _write_carddb_snapshot(
        cfg, tmp_path, {75022: "Island", 96080: "Sol Ring", 90302: "Krenko, Tin Street Kingpin"}
    )

    text = (
        deck_upsert_line(
            "deck-brawl", "Welshie", version="1", format="HistoricBrawl",
            main_deck=[(75022, 1)], command_zone=[(90302, 1)],
        )
        + event_set_deck_response_line("c1", "deck-brawl", "Welshie")
        + match_room_state_playing_line(
            "m1", [(OUR_ID, OUR_SEAT, 1), (OPPONENT_ID, OPPONENT_SEAT, 2)], our_id=OUR_ID
        )
        + gre_full_line(
            our_seat_id=OUR_SEAT,
            hand_zone_id=35,
            hand=[(1, 75022)],
            command_zone_id=26,
            command_zone_cards=[(10, 90302, OUR_SEAT), (11, 96080, OPPONENT_SEAT)],
        )
        + match_room_state_completed_line("m1", winning_team_id=1, our_id=OUR_ID)
    )
    _write_session(cfg, "20260817T100000", text)

    summaries, deck_reports, _ = reports.collect_period_stats(cfg)

    summary_tallies = summaries[("annual", "2026", "Brawl")].opponent_commander_tallies
    deck_tallies = deck_reports[("annual", "2026", "Welshie")].opponent_commander_tallies
    assert summary_tallies[96080].games == 1 and summary_tallies[96080].wins == 1
    assert deck_tallies[96080].games == 1 and deck_tallies[96080].wins == 1

    reports.run(cfg)
    summary_content = (cfg.archive_dir / "reports" / "annual" / "summary" / "2026-brawl.md").read_text()
    deck_content = (cfg.archive_dir / "reports" / "annual" / "decks" / "2026-welshie.md").read_text()
    assert "Sol Ring" in summary_content and "## Opponent Commanders" in summary_content
    assert "Sol Ring" in deck_content and "## Opponent Commanders" in deck_content


def test_deck_reissued_under_same_name_merges_into_one_period_report(cfg, tmp_path, monkeypatch):
    """Delete+recreate under the same name gets a new deck_id from Arena —
    the periodic per-deck report must fold both matches into one deck, not
    write two separate reports for the same period."""
    monkeypatch.setattr(reports, "load_seasons", lambda: [])
    _write_carddb_snapshot(cfg, tmp_path, {75022: "Island"})

    first_match = _match_session_text(
        "deck-one", "Welshie", course_id="c1", match_id="m1", winning_team_id=1, format="HistoricBrawl"
    )
    _write_session(cfg, "20260817T100000", first_match, suffix="aaaaaaaa")

    second_match = _match_session_text(
        "deck-two", "Welshie", course_id="c2", match_id="m2", winning_team_id=2, format="HistoricBrawl"
    )
    _write_session(cfg, "20260817T110000", second_match, suffix="bbbbbbbb")

    summaries, deck_reports, summary = reports.collect_period_stats(cfg)

    assert summary.matches_found == 2
    deck_keys = [key for key in deck_reports if key[:2] == ("annual", "2026")]
    assert deck_keys == [("annual", "2026", "Welshie")]
    assert deck_reports[("annual", "2026", "Welshie")].gp == 2


def test_missing_carddb_surfaces_as_warning_not_exception(cfg):
    text = _match_session_text(
        "deck-brawl", "Welshie", course_id="c1", match_id="m1", winning_team_id=1, format="HistoricBrawl"
    )
    _write_session(cfg, "20260817T100000", text)

    summary = reports.run(cfg)

    assert summary.summaries_written == []
    assert summary.decks_written == []
    assert summary.warnings
    assert "carddb" in summary.warnings[0]
