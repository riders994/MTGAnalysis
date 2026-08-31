"""End-to-end: archived sessions -> per-deck Markdown changelogs."""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

from analysis import deck_changelog

from conftest import deck_upsert_line

CARDS = {
    75022: "Island",
    75021: "Plains",
    96080: "Absolute Virtue",
    1: "Mox Opal",
}


def _write_session(cfg, session_id: str, text: str, *, suffix: str = "aaaaaaaa") -> Path:
    dest = cfg.sessions_dir / f"session-{session_id}-{suffix}.log.gz"
    dest.write_bytes(gzip.compress(text.encode("utf-8")))
    return dest


def _write_carddb_snapshot(cfg, tmp_path: Path) -> None:
    carddb_dir = cfg.snapshots_dir / "carddb"
    carddb_dir.mkdir(parents=True, exist_ok=True)

    db_path = tmp_path / "carddb.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE Cards (GrpId INTEGER PRIMARY KEY, TitleId INTEGER)")
        conn.execute("CREATE TABLE Localizations_enUS (LocId INTEGER PRIMARY KEY, Loc TEXT)")
        for grp_id, name in CARDS.items():
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


def test_initial_save_and_later_diff(cfg, tmp_path):
    _write_carddb_snapshot(cfg, tmp_path)

    initial = deck_upsert_line(
        "deck-abc",
        "Lagaan",
        version="11",
        format="HistoricBrawl",
        last_updated="2026-08-08T02:35:17.4706732-04:00",
        main_deck=[(75022, 5), (75021, 7)],
        command_zone=[(96080, 1)],
    )
    _write_session(cfg, "20260817T192832", initial)

    later = deck_upsert_line(
        "deck-abc",
        "Lagaan",
        version="12",
        format="HistoricBrawl",
        last_updated="2026-08-18T10:00:00-04:00",
        main_deck=[(75022, 6), (1, 1)],  # Island bumped 5->6, Plains removed, Mox Opal added
        command_zone=[(96080, 1)],
    )
    _write_session(cfg, "20260818T100000", later)

    summary = deck_changelog.run(cfg)

    assert summary.sessions_scanned == 2
    assert summary.saves_found == 2
    assert summary.decks_written == ["lagaan.md"]
    assert summary.warnings == []

    content = (cfg.archive_dir / "reports" / "changelogs" / "lagaan.md").read_text()

    assert "# Lagaan" in content
    assert "`deck-abc`" in content
    assert "HistoricBrawl" in content

    # Newest entry first, with only the actual diff.
    v12_index = content.index("### v12")
    v11_index = content.index("### v11")
    assert v12_index < v11_index
    assert "+ 1 Mox Opal" in content
    assert "- 7 Plains" in content
    assert "~ Island (5 → 6)" in content

    # First-seen entry lists the full initial decklist as additions.
    assert "+ 1 Absolute Virtue" in content
    assert "+ 5 Island" in content
    assert "+ 7 Plains" in content


def test_deck_reissued_under_same_name_merges_into_one_history(cfg, tmp_path):
    """A deck deleted in Arena and recreated under the same name gets a new
    DeckId — there's no delete event to key off, so same exact name is
    treated as the same deck, reissued, not a genuine collision."""
    _write_carddb_snapshot(cfg, tmp_path)

    first = deck_upsert_line(
        "deck-one", "Same Name", version="1", main_deck=[(75022, 1), (75021, 1)]
    )
    second = deck_upsert_line(
        "deck-two", "Same Name", version="1", main_deck=[(75021, 1), (1, 1)]
    )
    _write_session(cfg, "20260817T100000", first, suffix="aaaaaaaa")
    _write_session(cfg, "20260817T110000", second, suffix="bbbbbbbb")

    summary = deck_changelog.run(cfg)

    assert summary.decks_written == ["same-name.md"]
    assert summary.warnings == []  # no collision — this is the same deck, reissued

    content = (cfg.archive_dir / "reports" / "changelogs" / "same-name.md").read_text()

    # Both deck_ids' saves land in one continuous, chronologically ordered
    # history: two "v1" entries (one per deck_id), newest first.
    assert content.count("### v1") == 2
    first_v1 = content.index("### v1")
    second_v1 = content.index("### v1", first_v1 + 1)
    assert "session 20260817T110000" in content[first_v1:second_v1]
    assert "session 20260817T100000" in content[second_v1:]

    # The reissued deck's entry diffs against the deleted deck's last state,
    # not a reset to "everything added": Plains is unchanged across the
    # boundary (no +/- line for it), Island is dropped, Mox Opal is added.
    boundary_entry = content[first_v1:second_v1]
    assert "Plains" not in boundary_entry
    assert "- 1 Island" in boundary_entry
    assert "+ 1 Mox Opal" in boundary_entry


def test_generic_deck_names_never_merge_across_deck_ids(cfg, tmp_path):
    """Unlike a genuine rename, "Draft Deck" is Arena's permanent default
    name for any unrenamed draft — two different drafts left unrenamed are
    two unrelated decks that happen to share that name, not one deck
    reissued, so each deck_id must get its own changelog file."""
    _write_carddb_snapshot(cfg, tmp_path)

    first = deck_upsert_line(
        "deck-one", "Draft Deck", version="1", main_deck=[(75022, 1), (75021, 1)]
    )
    second = deck_upsert_line(
        "deck-two", "Draft Deck", version="1", main_deck=[(1, 1), (96080, 1)]
    )
    _write_session(cfg, "20260817T100000", first, suffix="aaaaaaaa")
    _write_session(cfg, "20260817T110000", second, suffix="bbbbbbbb")

    summary = deck_changelog.run(cfg)

    # First-encountered deck_id keeps the plain slug; the later one sharing
    # the name collides and gets its deck_id appended (assign_slugs' existing
    # collision handling — flagged with a warning, not silently merged).
    assert sorted(summary.decks_written) == ["draft-deck-deck-two.md", "draft-deck.md"]
    assert summary.warnings

    first_content = (cfg.archive_dir / "reports" / "changelogs" / "draft-deck.md").read_text()
    second_content = (cfg.archive_dir / "reports" / "changelogs" / "draft-deck-deck-two.md").read_text()

    # Each file has its own full initial decklist — no cross-deck diffing.
    assert "+ 1 Island" in first_content and "+ 1 Plains" in first_content
    assert "Mox Opal" not in first_content
    assert "+ 1 Mox Opal" in second_content and "+ 1 Absolute Virtue" in second_content
    assert "Island" not in second_content


def test_no_deck_events_writes_nothing(cfg, tmp_path):
    _write_carddb_snapshot(cfg, tmp_path)
    _write_session(cfg, "20260817T100000", "just a menu session, no deck saves\r\n")

    summary = deck_changelog.run(cfg)

    assert summary.sessions_scanned == 1
    assert summary.saves_found == 0
    assert summary.decks_written == []
    assert summary.warnings == []


def test_missing_carddb_surfaces_as_warning_not_exception(cfg):
    save = deck_upsert_line("deck-abc", "Lagaan", version="1", main_deck=[(75022, 1)])
    _write_session(cfg, "20260817T100000", save)

    summary = deck_changelog.run(cfg)

    assert summary.decks_written == []
    assert summary.warnings
    assert "carddb" in summary.warnings[0]
