"""Resolving numeric card ids to display names via the archived card database.

The collector snapshots Arena's card database (a gzipped SQLite file) whenever
it changes, content-addressed under snapshots/carddb/. Card ids in the log
(cardId, aka grpId) only resolve to names by joining Cards.GrpId ->
Cards.TitleId -> Localizations_enUS.LocId -> Loc.
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import tempfile
from pathlib import Path

from collector.config import Config

_NAME_QUERY = (
    "SELECT c.GrpId, l.Loc FROM Cards c "
    "JOIN Localizations_enUS l ON l.LocId = c.TitleId"
)


class CardDbUnavailable(Exception):
    """No carddb snapshot found in the archive to resolve card names against."""


class CardNames:
    def __init__(self, mapping: dict[int, str]):
        self._mapping = mapping

    def __getitem__(self, card_id: int) -> str:
        return self._mapping.get(card_id, f"Unknown Card {card_id}")


def latest_snapshot(cfg: Config) -> Path | None:
    """The most recently captured carddb snapshot, by capture (mtime) order.

    Multiple snapshots can exist over time (content-addressed by hash); this
    always resolves against the newest one rather than picking, per session,
    the snapshot closest to that session's own timestamp. With only one
    card-era of archived data so far that distinction doesn't matter yet.
    """
    carddb_dir = cfg.snapshots_dir / "carddb"
    if not carddb_dir.exists():
        return None
    candidates = sorted(carddb_dir.glob("Raw_CardDatabase_*.gz"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def load_card_names(cfg: Config) -> CardNames:
    snapshot = latest_snapshot(cfg)
    if snapshot is None:
        raise CardDbUnavailable(f"no carddb snapshot found under {cfg.snapshots_dir / 'carddb'}")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "carddb.sqlite"
        with gzip.open(snapshot, "rb") as src, open(db_path, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(_NAME_QUERY).fetchall()
        finally:
            conn.close()

    return CardNames({grp_id: name for grp_id, name in rows})
