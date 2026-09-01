"""Resolving card ids via a synthetic carddb snapshot."""

from __future__ import annotations

import gzip
import os
import sqlite3
from pathlib import Path

from analysis.carddb import latest_snapshot, load_card_names, load_land_info

from conftest import write_carddb_snapshot


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


PLAINS = 91301  # basic land
CASTLE_VANTRESS = 70389  # non-basic land, one mana ability + one non-mana ability
JIDOOR = 96164  # MDFC land face
OVERTURE = 96165  # JIDOOR's linked (non-land) spell face
SOL_RING = 96080  # not a land at all

MANA_ABILITY = 1002  # "{T}: Add {U}." — Category 1, SubCategory 1
SCRY_ABILITY = 50001  # "{2}{U}{U}, T: Scry 2." — Category 1, SubCategory 15
ETB_TRIGGER = 50002  # not an activated ability at all — Category 3


def test_load_land_info_classifies_basics_dfcs_and_non_mana_abilities(cfg, tmp_path):
    write_carddb_snapshot(
        cfg,
        tmp_path,
        {
            PLAINS: "Plains",
            CASTLE_VANTRESS: "Castle Vantress",
            JIDOOR: "Jidoor, Aristocratic Capital",
            OVERTURE: "Overture",
            SOL_RING: "Sol Ring",
        },
        lands={
            PLAINS: {"types": "5", "supertypes": "1"},
            CASTLE_VANTRESS: {
                "types": "5",
                "ability_ids": f"{MANA_ABILITY}:0,{SCRY_ABILITY}:0,{ETB_TRIGGER}:0",
            },
            JIDOOR: {"types": "5", "linked_face_grp_ids": str(OVERTURE)},
        },
        abilities={
            MANA_ABILITY: (1, 1),
            SCRY_ABILITY: (1, 15),
            ETB_TRIGGER: (3, 0),
        },
    )

    land_info = load_land_info(cfg)

    plains = land_info[PLAINS]
    assert plains.is_land is True
    assert plains.is_basic is True
    assert plains.other_face_grp_ids == frozenset()
    assert plains.non_mana_ability_ids == frozenset()

    castle = land_info[CASTLE_VANTRESS]
    assert castle.is_land is True
    assert castle.is_basic is False
    assert castle.non_mana_ability_ids == frozenset({SCRY_ABILITY})

    jidoor = land_info[JIDOOR]
    assert jidoor.is_land is True
    assert jidoor.other_face_grp_ids == frozenset({OVERTURE})

    # Not a land at all — every field falls back to the shared empty profile.
    sol_ring = land_info[SOL_RING]
    assert sol_ring.is_land is False
    assert sol_ring.is_basic is False

    # Never seen in this snapshot — same empty-profile fallback, no KeyError.
    unknown = land_info[999999]
    assert unknown.is_land is False
