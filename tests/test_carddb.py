"""Resolving card ids via a synthetic carddb snapshot."""

from __future__ import annotations

import gzip
import os
import sqlite3
from pathlib import Path

from analysis.carddb import latest_snapshot, load_card_names


def _build_carddb(db_path: Path, cards: dict[int, str]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE Cards (GrpId INTEGER PRIMARY KEY, TitleId INTEGER)")
        conn.execute("CREATE TABLE Localizations_enUS (LocId INTEGER PRIMARY KEY, Loc TEXT)")
        for grp_id, name in cards.items():
            title_id = grp_id + 1000  # distinct from GrpId, mirrors the real indirection
            conn.execute("INSERT INTO Cards (GrpId, TitleId) VALUES (?, ?)", (grp_id, title_id))
            conn.execute(
                "INSERT INTO Localizations_enUS (LocId, Loc) VALUES (?, ?)", (title_id, name)
            )
        conn.commit()
    finally:
        conn.close()


def _write_snapshot(cfg, tmp_path: Path, name: str, cards: dict[int, str], *, mtime: float) -> Path:
    carddb_dir = cfg.snapshots_dir / "carddb"
    carddb_dir.mkdir(parents=True, exist_ok=True)

    db_path = tmp_path / f"{name}.sqlite"
    _build_carddb(db_path, cards)

    dest = carddb_dir / f"Raw_CardDatabase_{name}.gz"
    with open(db_path, "rb") as src, gzip.open(dest, "wb") as out:
        out.write(src.read())
    os.utime(dest, (mtime, mtime))
    return dest


def test_load_card_names_resolves_known_and_unknown_ids(cfg, tmp_path):
    _write_snapshot(cfg, tmp_path, "a", {96080: "Absolute Virtue", 75022: "Island"}, mtime=1000)

    names = load_card_names(cfg)

    assert names[96080] == "Absolute Virtue"
    assert names[75022] == "Island"
    assert names[999999] == "Unknown Card 999999"


def test_latest_snapshot_picks_the_newest_by_mtime(cfg, tmp_path):
    older = _write_snapshot(cfg, tmp_path, "older", {1: "Old Card"}, mtime=1000)
    newer = _write_snapshot(cfg, tmp_path, "newer", {1: "New Card"}, mtime=2000)

    assert latest_snapshot(cfg) == newer
    assert latest_snapshot(cfg) != older

    names = load_card_names(cfg)
    assert names[1] == "New Card"
