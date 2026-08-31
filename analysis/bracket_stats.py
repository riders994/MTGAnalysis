"""Best-effort Brawl bracket-signal computation.

Arena exposes no ground-truth bracket for Brawl. Ranked Brawl (Format
"HistoricBrawlRanked") does show the player a rank in the client, but the
user confirmed it's the same constructedRankInfo ladder already shared by
Standard and Alchemy (RankGetCombinedRankInfo's only per-account rank field,
confirmed by direct inspection of archived Player.log traffic) — one unified
rank, not a Brawl-specific one. That makes it unusable as a Brawl-specific
signal without also matching each rank snapshot to the queue/match that
produced it, which this pass doesn't attempt. Independent of that, the user
also clarified this rank is pure MMR — a win/loss-driven matchmaking rating,
not an independent measure of deck power — and is demoted every month, so
even a queue-attributed value would not be a bracket signal on its own.

Everything here is therefore an internal, win-rate-based proxy built from our
own account's games, reusing card_stats.py's per-deck aggregation rather than
re-scanning the archive. Ranked and casual Brawl are kept as separate segments
throughout, since they're matchmade from different populations.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from collector.config import Config

from . import card_stats
from .card_stats import DeckStats, OpponentCommanderTally, iter_outcomes
from .carddb import CardDbUnavailable, load_card_names

# Need >= 3 games per half to say anything at all about a trend.
TREND_MIN_GAMES = 6
# 20 percentage points, first half vs second half of a deck's match history.
TREND_SWING_THRESHOLD = 0.20


@dataclass
class CommanderPilotStats:
    """A commander we've personally piloted, folded across every deck_id that
    used it (e.g. the same commander rebuilt into a second decklist)."""

    grp_id: int
    deck_ids: set[str] = field(default_factory=set)
    gp: int = 0
    gp_wins: int = 0
    early_forfeit_games: int = 0


@dataclass
class DeckTrend:
    deck_id: str
    # Chronological match outcomes (oldest first), one bool per MATCH (not
    # per game — a Bo3 match's games all share the same outcome.won, so
    # tracking at match granularity avoids inflating the trend with
    # duplicated results within one match).
    match_results: list[bool] = field(default_factory=list)


@dataclass
class BracketSegment:
    """One matchmaking population: ranked Brawl, or casual Brawl."""

    deck_stats: dict[str, DeckStats] = field(default_factory=dict)
    commander_pilots: dict[int, CommanderPilotStats] = field(default_factory=dict)
    opponent_commander_tallies: dict[int, OpponentCommanderTally] = field(default_factory=dict)
    deck_trends: dict[str, DeckTrend] = field(default_factory=dict)


@dataclass
class BracketData:
    ranked: BracketSegment
    casual: BracketSegment


@dataclass
class Summary:
    sessions_scanned: int = 0
    matches_found: int = 0
    games_parsed: int = 0
    files_written: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def is_ranked_brawl(format_: str | None) -> bool:
    """HistoricBrawlRanked is the only known ranked-Brawl format string so
    far; a plain "Ranked" substring check keeps this working if Arena adds
    other ranked-Brawl variants later, the same way format_groups.py matches
    format families by keyword."""
    return bool(format_) and "Ranked" in format_


def aggregate_own_commanders(deck_stats: dict[str, DeckStats]) -> dict[int, CommanderPilotStats]:
    """Every commander we've personally piloted, folded across every deck_id
    that used it. Pure groupby over already-computed DeckStats — no new
    session scan required."""
    pilots: dict[int, CommanderPilotStats] = {}
    for deck_id, stats in deck_stats.items():
        for grp_id in stats.own_commander_grp_ids:
            pilot = pilots.setdefault(grp_id, CommanderPilotStats(grp_id=grp_id))
            pilot.deck_ids.add(deck_id)
            pilot.gp += stats.gp
            pilot.gp_wins += stats.gp_wins
            pilot.early_forfeit_games += stats.early_forfeit_games
    return pilots


def aggregate_opponent_commanders(
    deck_stats: dict[str, DeckStats],
) -> dict[int, OpponentCommanderTally]:
    """Opponent-commander tallies are per-deck in DeckStats; this merges them
    across every deck in the given set (a given opponent commander might be
    faced across several of our own decks)."""
    merged: dict[int, OpponentCommanderTally] = {}
    for stats in deck_stats.values():
        for grp_id, tally in stats.opponent_commander_tallies.items():
            target = merged.setdefault(grp_id, OpponentCommanderTally())
            target.games += tally.games
            target.wins += tally.wins
            target.early_concedes += tally.early_concedes
    return merged


def collect_deck_trends(cfg: Config, deck_ids: set[str]) -> dict[str, DeckTrend]:
    """Chronological match-level win/loss per deck, oldest first — relies on
    iter_outcomes' guaranteed oldest-first ordering (iter_sessions sorts
    filenames; join_deck_to_matches emits matches in file-offset order)."""
    trends = {deck_id: DeckTrend(deck_id) for deck_id in deck_ids}
    throwaway_summary = card_stats.Summary()  # counts already reported from collect_card_stats
    for outcome, _text, _dt in iter_outcomes(cfg, throwaway_summary):
        trend = trends.get(outcome.deck_id)
        if trend is not None:
            trend.match_results.append(outcome.won)
    return trends


def half_win_rates(match_results: list[bool]) -> tuple[float, float]:
    """First-half/second-half win rates, recomputed at render time rather
    than stored — same "nothing can drift" rationale as CardTally's GIH."""
    mid = len(match_results) // 2
    first, second = match_results[:mid], match_results[mid:]
    return sum(first) / len(first), sum(second) / len(second)


def classify_trend(match_results: list[bool]) -> str:
    """One of "Rising", "Falling", "Plateaued", "Insufficient Data".

    Deliberately coarse given how few games most decks have: splits the
    chronological match list into a first and second half and compares win
    rates, rather than any smoothed rolling average that would imply more
    precision than the sample supports.
    """
    if len(match_results) < TREND_MIN_GAMES:
        return "Insufficient Data"
    first_wr, second_wr = half_win_rates(match_results)
    # Rounded to avoid float noise (e.g. 0.6 - 0.4 == 0.19999999999999998)
    # landing exactly on TREND_SWING_THRESHOLD's boundary on the wrong side.
    delta = round(second_wr - first_wr, 9)
    if delta >= TREND_SWING_THRESHOLD:
        return "Rising"
    if delta <= -TREND_SWING_THRESHOLD:
        return "Falling"
    return "Plateaued"


def _build_segment(cfg: Config, deck_stats: dict[str, DeckStats]) -> BracketSegment:
    return BracketSegment(
        deck_stats=deck_stats,
        commander_pilots=aggregate_own_commanders(deck_stats),
        opponent_commander_tallies=aggregate_opponent_commanders(deck_stats),
        deck_trends=collect_deck_trends(cfg, set(deck_stats)),
    )


def collect_bracket_stats(cfg: Config) -> tuple[BracketData, Summary]:
    all_deck_stats, card_summary = card_stats.collect_card_stats(cfg)
    summary = Summary(
        sessions_scanned=card_summary.sessions_scanned,
        matches_found=card_summary.matches_found,
        games_parsed=card_summary.games_parsed,
        warnings=list(card_summary.warnings),
    )

    ranked_decks: dict[str, DeckStats] = {}
    casual_decks: dict[str, DeckStats] = {}
    for deck_id, stats in all_deck_stats.items():
        if not stats.own_commander_grp_ids:
            continue  # not a Brawl deck
        if is_ranked_brawl(stats.format):
            ranked_decks[deck_id] = stats
        else:
            casual_decks[deck_id] = stats

    data = BracketData(
        ranked=_build_segment(cfg, ranked_decks),
        casual=_build_segment(cfg, casual_decks),
    )
    return data, summary


def run(cfg: Config) -> Summary:
    from .bracket_report import render_bracket_stats  # deferred: avoids a module cycle

    data, summary = collect_bracket_stats(cfg)
    if not data.ranked.deck_stats and not data.casual.deck_stats:
        return summary  # nothing Brawl-shaped in the archive; not a warning, just nothing to report

    try:
        names = load_card_names(cfg)
    except CardDbUnavailable as exc:
        summary.warnings.append(str(exc))
        return summary

    bracket_dir = cfg.archive_dir / "bracket_stats"
    bracket_dir.mkdir(parents=True, exist_ok=True)
    dest = bracket_dir / "overview.md"
    dest.write_text(render_bracket_stats(data, names), encoding="utf-8")
    summary.files_written.append(dest.name)

    return summary
