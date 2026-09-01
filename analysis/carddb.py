"""Resolving numeric card ids to display names via the archived card database.

The collector snapshots Arena's card database (a gzipped SQLite file) whenever
it changes, content-addressed under snapshots/carddb/. Card ids in the log
(cardId, aka grpId) only resolve to names by joining Cards.GrpId ->
Cards.TitleId -> Localizations_enUS.LocId -> Loc.
"""

from __future__ import annotations

import contextlib
import gzip
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from collector.config import Config

_NAME_QUERY = (
    "SELECT c.GrpId, l.Loc FROM Cards c "
    "JOIN Localizations_enUS l ON l.LocId = c.TitleId"
)

# CardType enum value 5 is Land, SuperType enum value 1 is Basic (confirmed
# against a real carddb snapshot's Enums table) — Types/Supertypes are
# comma-separated lists of these small integer codes as text.
_LAND_TYPE = "5"
_BASIC_SUPERTYPE = "1"

# Abilities.Category 1 is "activated ability"; within that, SubCategory 1 is
# specifically a mana ability — confirmed against every mana ability the
# archive's own account has ever activated (all SubCategory 1) and against
# Castle Vantress's real "{2}{U}{U}, T: Scry 2" ability (Category 1,
# SubCategory 15 — activated, not mana).
_ACTIVATED_ABILITY_CATEGORY = 1
_MANA_ABILITY_SUBCATEGORY = 1

_LAND_QUERY = "SELECT GrpId, Types, Supertypes, LinkedFaceGrpIds, AbilityIds FROM Cards"
_ABILITY_QUERY = "SELECT Id, Category, SubCategory FROM Abilities"


class CardDbUnavailable(Exception):
    """No carddb snapshot found in the archive to resolve card names against."""


class CardNames:
    def __init__(self, mapping: dict[int, str]):
        self._mapping = mapping

    def __getitem__(self, card_id: int) -> str:
        return self._mapping.get(card_id, f"Unknown Card {card_id}")


@dataclass(frozen=True)
class LandProfile:
    is_land: bool
    is_basic: bool
    other_face_grp_ids: frozenset[int]
    non_mana_ability_ids: frozenset[int]


_EMPTY_LAND_PROFILE = LandProfile(
    is_land=False, is_basic=False, other_face_grp_ids=frozenset(), non_mana_ability_ids=frozenset()
)


class LandInfo:
    def __init__(self, profiles: dict[int, LandProfile]):
        self._profiles = profiles

    def __getitem__(self, card_id: int) -> LandProfile:
        return self._profiles.get(card_id, _EMPTY_LAND_PROFILE)

    @classmethod
    def empty(cls) -> LandInfo:
        return cls({})


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


@contextlib.contextmanager
def _connect(cfg: Config):
    """Decompress the latest carddb snapshot to a temp file and open it
    read-only, closing/cleaning up on exit. Raises CardDbUnavailable if no
    snapshot has ever been archived."""
    snapshot = latest_snapshot(cfg)
    if snapshot is None:
        raise CardDbUnavailable(f"no carddb snapshot found under {cfg.snapshots_dir / 'carddb'}")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "carddb.sqlite"
        with gzip.open(snapshot, "rb") as src, open(db_path, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            yield conn
        finally:
            conn.close()


def load_card_names(cfg: Config) -> CardNames:
    with _connect(cfg) as conn:
        rows = conn.execute(_NAME_QUERY).fetchall()
    return CardNames({grp_id: name for grp_id, name in rows})


def _parse_ids(field: str | None) -> frozenset[int]:
    if not field:
        return frozenset()
    return frozenset(int(token) for token in field.split(",") if token.strip())


def load_land_info(cfg: Config) -> LandInfo:
    with _connect(cfg) as conn:
        ability_rows = conn.execute(_ABILITY_QUERY).fetchall()
        card_rows = conn.execute(_LAND_QUERY).fetchall()

    non_mana_ability_ids = frozenset(
        ability_id
        for ability_id, category, subcategory in ability_rows
        if category == _ACTIVATED_ABILITY_CATEGORY and subcategory != _MANA_ABILITY_SUBCATEGORY
    )

    profiles: dict[int, LandProfile] = {}
    for grp_id, types, supertypes, linked_face_grp_ids, ability_ids in card_rows:
        if _LAND_TYPE not in (types or "").split(","):
            continue
        is_basic = _BASIC_SUPERTYPE in (supertypes or "").split(",")
        other_face_grp_ids = _parse_ids(linked_face_grp_ids)
        card_ability_ids = frozenset(
            int(pair.split(":", 1)[0]) for pair in (ability_ids or "").split(",") if pair.strip()
        )
        profiles[grp_id] = LandProfile(
            is_land=True,
            is_basic=is_basic,
            other_face_grp_ids=other_face_grp_ids,
            non_mana_ability_ids=card_ability_ids & non_mana_ability_ids,
        )

    return LandInfo(profiles)
