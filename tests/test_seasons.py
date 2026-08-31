"""Season boundary loading and lookup for seasonal reports."""

from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path

from analysis.seasons import Season, load_seasons, season_for


def _write_seasons(path: Path, toml_text: str) -> Path:
    path.write_text(textwrap.dedent(toml_text))
    return path


def test_load_seasons_sorted_oldest_first(tmp_path):
    path = _write_seasons(
        tmp_path / "seasons.toml",
        """
        [[season]]
        name = "Second"
        start = 2026-06-01

        [[season]]
        name = "First"
        start = 2026-01-01
        """,
    )

    seasons = load_seasons(path)

    assert [s.name for s in seasons] == ["First", "Second"]


def test_load_seasons_missing_file_returns_empty(tmp_path):
    assert load_seasons(tmp_path / "absent.toml") == []


def test_season_for_before_earliest_is_none():
    seasons = [Season("A", date(2026, 3, 1)), Season("B", date(2026, 6, 1))]
    assert season_for(date(2026, 1, 1), seasons) is None


def test_season_for_on_boundary_date_matches_that_season():
    seasons = [Season("A", date(2026, 3, 1)), Season("B", date(2026, 6, 1))]
    assert season_for(date(2026, 6, 1), seasons).name == "B"


def test_season_for_between_boundaries_matches_the_earlier_season():
    seasons = [Season("A", date(2026, 3, 1)), Season("B", date(2026, 6, 1))]
    assert season_for(date(2026, 4, 15), seasons).name == "A"


def test_season_for_newest_entry_is_open_ended():
    seasons = [Season("A", date(2026, 3, 1)), Season("B", date(2026, 6, 1))]
    assert season_for(date(2030, 1, 1), seasons).name == "B"


def test_shipped_seasons_toml_loads_and_excludes_mbce():
    from analysis.seasons import SEASONS_PATH

    seasons = load_seasons(SEASONS_PATH)

    names = [s.name for s in seasons]
    starts = [s.start for s in seasons]
    assert "The Hobbit" in names
    assert not any("Mystery Booster" in name for name in names)
    assert starts == sorted(starts)
