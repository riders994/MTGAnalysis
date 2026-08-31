"""Season boundaries for seasonal reports, loaded from the repo-tracked
seasons.toml (real-world MTG release data, not a machine-specific path —
unlike collector's gitignored config.toml)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SEASONS_PATH = Path(__file__).parent / "seasons.toml"


@dataclass(frozen=True)
class Season:
    name: str
    start: date


def load_seasons(path: Path = SEASONS_PATH) -> list[Season]:
    """Every configured season, sorted oldest first."""
    if not path.exists():
        return []
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    seasons = [Season(name=entry["name"], start=entry["start"]) for entry in data.get("season", [])]
    seasons.sort(key=lambda season: season.start)
    return seasons


def season_for(d: date, seasons: list[Season]) -> Season | None:
    """The season whose start is the latest one at or before `d` — open-ended
    until the next entry's start. None if `d` predates every configured
    season (or none are configured at all)."""
    current: Season | None = None
    for season in seasons:
        if season.start > d:
            break
        current = season
    return current
