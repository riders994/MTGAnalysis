"""CLI wiring for `python -m analysis`."""

from __future__ import annotations

import gzip
import sqlite3
import textwrap
from pathlib import Path

import pytest

from analysis.__main__ import main

from conftest import (
    deck_upsert_line,
    event_set_deck_response_line,
    match_room_state_completed_line,
    match_room_state_playing_line,
)


@pytest.fixture
def populated(tmp_path: Path) -> Path:
    """A config plus an archive holding one deck save + won match, and a carddb snapshot."""
    archive = tmp_path / "archive"
    config = tmp_path / "config.toml"
    config.write_text(
        textwrap.dedent(
            f"""
            [paths]
            archive_dir = "{archive.as_posix()}"
            """
        )
    )

    sessions_dir = archive / "sessions"
    sessions_dir.mkdir(parents=True)
    session_text = (
        deck_upsert_line("deck-abc", "Lagaan", version="1", main_deck=[(75022, 1)])
        + event_set_deck_response_line("course-1", "deck-abc", "Lagaan")
        + match_room_state_playing_line(
            "match-1", [("OURCLIENTID", 1, 1), ("OPPONENTID", 2, 2)], our_id="OURCLIENTID"
        )
        + match_room_state_completed_line("match-1", winning_team_id=1, our_id="OURCLIENTID")
    )
    (sessions_dir / "session-20260817T100000-aaaaaaaa.log.gz").write_bytes(
        gzip.compress(session_text.encode("utf-8"))
    )

    carddb_dir = archive / "snapshots" / "carddb"
    carddb_dir.mkdir(parents=True)
    db_path = tmp_path / "carddb.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE Cards (GrpId INTEGER PRIMARY KEY, TitleId INTEGER, "
            "Types TEXT, Supertypes TEXT, LinkedFaceGrpIds TEXT, AbilityIds TEXT)"
        )
        conn.execute("CREATE TABLE Localizations_enUS (LocId INTEGER PRIMARY KEY, Loc TEXT)")
        conn.execute(
            "CREATE TABLE Abilities (Id INTEGER PRIMARY KEY, Category INTEGER, SubCategory INTEGER)"
        )
        conn.execute(
            "INSERT INTO Cards (GrpId, TitleId, Types, Supertypes) VALUES (75022, 1075022, '5', '1')"
        )
        conn.execute(
            "INSERT INTO Localizations_enUS (LocId, Loc) VALUES (1075022, 'Island')"
        )
        conn.commit()
    finally:
        conn.close()
    with open(db_path, "rb") as src, gzip.open(carddb_dir / "Raw_CardDatabase_test.gz", "wb") as out:
        out.write(src.read())

    return config


@pytest.mark.parametrize(
    "argv",
    [
        ["--config", "{config}", "deck-changelog"],
        ["deck-changelog", "--config", "{config}"],
    ],
    ids=["config-before-subcommand", "config-after-subcommand"],
)
def test_config_accepted_on_either_side_of_the_subcommand(populated, argv, capsys):
    resolved = [a.format(config=populated) for a in argv]
    assert main(resolved) == 0
    assert "wrote 1 changelog(s)" in capsys.readouterr().out


def test_deck_changelog_reports_the_written_file(populated, capsys):
    assert main(["--config", str(populated), "deck-changelog"]) == 0
    out = capsys.readouterr().out
    assert "found 1 deck save(s)" in out
    assert "lagaan.md" in out


def test_missing_config_points_at_discover(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "absent.toml"), "deck-changelog"]) == 2
    assert "discover" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--config", "{config}", "card-stats"],
        ["card-stats", "--config", "{config}"],
    ],
    ids=["config-before-subcommand", "config-after-subcommand"],
)
def test_card_stats_config_accepted_on_either_side_of_the_subcommand(populated, argv, capsys):
    resolved = [a.format(config=populated) for a in argv]
    assert main(resolved) == 0
    assert "wrote 1 report(s)" in capsys.readouterr().out


def test_card_stats_reports_the_written_file(populated, capsys):
    assert main(["--config", str(populated), "card-stats"]) == 0
    out = capsys.readouterr().out
    assert "found 1 match(es)" in out
    assert "lagaan.md" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["--config", "{config}", "reports"],
        ["reports", "--config", "{config}"],
    ],
    ids=["config-before-subcommand", "config-after-subcommand"],
)
def test_reports_config_accepted_on_either_side_of_the_subcommand(populated, argv, capsys):
    resolved = [a.format(config=populated) for a in argv]
    assert main(resolved) == 0
    out = capsys.readouterr().out
    assert "format summary report(s)" in out
    assert "per-deck period report(s)" in out


def test_reports_writes_annual_monthly_and_seasonal_files_for_the_populated_standard_deck(populated, capsys):
    # `populated`'s deck_upsert_line defaults to format="Standard" and its
    # session is dated 2026-08-17 — Standard counts toward all three
    # cadences here, since the shipped seasons.toml places that date in
    # "The Hobbit" season (started 2026-08-14).
    assert main(["--config", str(populated), "reports"]) == 0
    out = capsys.readouterr().out
    assert "annual/summary/2026-standard.md" in out
    assert "annual/decks/2026-lagaan.md" in out
    assert "monthly/summary/2026-08-standard.md" in out
    assert "monthly/decks/2026-08-lagaan.md" in out
    assert "seasonal/summary/the-hobbit-standard.md" in out
    assert "seasonal/decks/the-hobbit-lagaan.md" in out
