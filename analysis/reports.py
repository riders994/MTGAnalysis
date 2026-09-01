"""Orchestration: periodic (monthly/seasonal/annual) rollups of the same
personal card-performance metrics card_stats.py computes all-time.

Two report kinds per (cadence, period_key): a format-level summary rolling
up every deck of that format group played that period, and a per-deck
report scoped to just that period's games (distinct from card_stats.py's
all-time per-deck reports, which never reset). Regenerated from scratch on
every run, same rationale as deck_changelog.py and card_stats.py.

Known simplifications:
- A game's period/season comes from its session's start time, not a
  per-match timestamp (none exists in the archive) — a session straddling a
  month/season boundary buckets every match in it by the session's start.
- Rolling several different decks of one format group into one period
  bucket means a card's OH/GD/GNS tally sums across every deck that
  included it that period, as if the account played one continuous
  decklist of that format all period.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from collector.config import Config

from .carddb import CardDbUnavailable, LandInfo, load_card_names, load_land_info
from .card_stats import (
    CardTally,
    DeckStats,
    OpponentCommanderTally,
    decklist_card_ids,
    fold_game,
    iter_outcomes,
)
from .changelog import assign_slugs, slugify
from .deck_changelog import collect_saves, merge_saves_by_identity
from .format_groups import classify_format
from .game_state import parse_games
from .seasons import Season, load_seasons, season_for

MONTHLY_GROUPS = frozenset({"Brawl", "Standard", "Alchemy"})
SEASONAL_GROUPS = frozenset({"Standard", "Limited"})


@dataclass
class PeriodStats:
    cadence: str  # "monthly" | "seasonal" | "annual"
    period_key: str  # e.g. "2026-08", "The Hobbit", "2026"
    format_group: str
    gp: int = 0
    gp_wins: int = 0
    mulligan_games: int = 0
    mulliganed_hand_tallies: dict[int, int] = field(default_factory=dict)
    mulliganed_hand_count: int = 0
    mulliganed_hand_land_count: int = 0
    kept_hand_land_count: int = 0
    kept_hand_land_count_wins: int = 0
    early_forfeit_games: int = 0
    early_forfeit_hand_tallies: dict[int, int] = field(default_factory=dict)
    card_tallies: dict[int, CardTally] = field(default_factory=dict)
    opponent_commander_tallies: dict[int, OpponentCommanderTally] = field(default_factory=dict)
    land_play_tallies: dict[int, int] = field(default_factory=dict)
    land_play_tallies_wins: dict[int, int] = field(default_factory=dict)
    land_turn_tallies: dict[int, int] = field(default_factory=dict)
    land_turn_game_counts: dict[int, int] = field(default_factory=dict)
    other_face_cast_tallies: dict[int, int] = field(default_factory=dict)
    land_ability_tallies: dict[int, int] = field(default_factory=dict)
    land_ability_tallies_wins: dict[int, int] = field(default_factory=dict)


@dataclass
class Summary:
    sessions_scanned: int = 0
    matches_found: int = 0
    games_parsed: int = 0
    summaries_written: list[str] = field(default_factory=list)
    decks_written: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _periods_for(dt, group: str, seasons: list[Season]) -> list[tuple[str, str]]:
    """Every (cadence, period_key) this game's date/format-group counts
    toward. Annual has no format filter — it's the "everything" cadence."""
    periods = [("annual", str(dt.year))]
    if group in MONTHLY_GROUPS:
        periods.append(("monthly", f"{dt.year:04d}-{dt.month:02d}"))
    if group in SEASONAL_GROUPS:
        season = season_for(dt.date(), seasons)
        if season is not None:
            periods.append(("seasonal", season.name))
    return periods


def collect_period_stats(
    cfg: Config,
) -> tuple[dict[tuple[str, str, str], PeriodStats], dict[tuple[str, str, str], DeckStats], Summary]:
    """Returns (format summaries keyed by (cadence, period_key, format_group),
    per-deck period reports keyed by (cadence, period_key, canonical deck
    identity — see deck_changelog.merge_saves_by_identity), Summary)."""
    raw_by_deck_saves, _ = collect_saves(cfg)
    by_deck_saves, deck_id_to_canonical = merge_saves_by_identity(raw_by_deck_saves)
    seasons = load_seasons()
    try:
        land_info = load_land_info(cfg)
    except CardDbUnavailable:
        land_info = LandInfo.empty()
    summary = Summary()

    deck_format: dict[str, str | None] = {}
    decklist_ids: dict[str, set[int]] = {}
    deck_names: dict[str, str] = {}
    for canonical, saves in by_deck_saves.items():
        latest = saves[-1]
        deck_format[canonical] = latest.format
        decklist_ids[canonical] = decklist_card_ids(latest)
        deck_names[canonical] = latest.name

    summaries: dict[tuple[str, str, str], PeriodStats] = {}
    deck_reports: dict[tuple[str, str, str], DeckStats] = {}

    for outcome, text, dt in iter_outcomes(cfg, summary):
        canonical = deck_id_to_canonical.get(outcome.deck_id)
        if canonical is None or canonical not in deck_format:
            summary.warnings.append(
                f"match in session {outcome.session_id} bound to deck "
                f"{outcome.deck_id}, which has no known decklist; skipping"
            )
            continue

        group = classify_format(deck_format[canonical])
        periods = _periods_for(dt, group, seasons)

        span_text = text[outcome.span[0] : outcome.span[1]]
        games = parse_games(span_text, match_id=outcome.match_id, our_seat_id=outcome.our_seat_id)

        for cadence, period_key in periods:
            summary_stats = summaries.setdefault(
                (cadence, period_key, group), PeriodStats(cadence, period_key, group)
            )
            summary_stats.gp += 1
            if outcome.won:
                summary_stats.gp_wins += 1

            deck_key = (cadence, period_key, canonical)
            deck_stats = deck_reports.get(deck_key)
            if deck_stats is None:
                deck_stats = DeckStats(
                    deck_id=by_deck_saves[canonical][-1].deck_id,
                    name=deck_names[canonical],
                    format=deck_format[canonical],
                )
                deck_reports[deck_key] = deck_stats
            deck_stats.gp += 1
            if outcome.won:
                deck_stats.gp_wins += 1

            for game in games:
                fold_game(summary_stats, game, outcome, decklist_ids[canonical], land_info)
                fold_game(deck_stats, game, outcome, decklist_ids[canonical], land_info)

        summary.games_parsed += len(games)

    return summaries, deck_reports, summary


def run(cfg: Config) -> Summary:
    from .card_stats_report import render_card_stats, render_period_stats  # avoids a module cycle

    summaries, deck_reports, summary = collect_period_stats(cfg)
    if not summaries and not deck_reports:
        return summary

    try:
        names = load_card_names(cfg)
        land_info = load_land_info(cfg)
    except CardDbUnavailable as exc:
        summary.warnings.append(str(exc))
        return summary

    reports_dir = cfg.archive_dir / "reports"

    for (cadence, period_key, group), stats in summaries.items():
        dest_dir = reports_dir / cadence / "summary"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{slugify(period_key)}-{slugify(group)}.md"
        dest.write_text(render_period_stats(stats, names, land_info), encoding="utf-8")
        summary.summaries_written.append(f"{cadence}/summary/{dest.name}")

    # Slug collisions between two decks sharing a period are resolved per
    # (cadence, period_key), same as card_stats.py resolves them per archive.
    by_period: dict[tuple[str, str], dict[str, str]] = {}
    for cadence, period_key, canonical in deck_reports:
        by_period.setdefault((cadence, period_key), {})[canonical] = deck_reports[
            (cadence, period_key, canonical)
        ].name

    for (cadence, period_key), names_by_deck in by_period.items():
        slugs, collision_warnings = assign_slugs(names_by_deck)
        summary.warnings.extend(collision_warnings)
        dest_dir = reports_dir / cadence / "decks"
        dest_dir.mkdir(parents=True, exist_ok=True)
        for canonical, slug in slugs.items():
            stats = deck_reports[(cadence, period_key, canonical)]
            dest = dest_dir / f"{slugify(period_key)}-{slug}.md"
            dest.write_text(render_card_stats(stats, names, land_info), encoding="utf-8")
            summary.decks_written.append(f"{cadence}/decks/{dest.name}")

    return summary
