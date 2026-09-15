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

One more piece of the user's own domain knowledge, same unverified-from-logs
status as the rank/MMR explanation above: right after a decklist change,
Arena is believed to preferentially match that deck against other
recently-changed decks, to re-rank it quickly rather than testing it against
the general population. bracket_signal_for_deck and
DeckTrend.since_last_change_results build that in — a deck's win rate since
its last real content change (see deck_changelog.current_era_start) is
preferred over its lifetime win rate for bracket placement, once there are
enough games in that window to trust it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from collector.config import Config

from . import card_stats
from .card_stats import DeckStats, OpponentCommanderTally, iter_outcomes
from .carddb import CardDbUnavailable, load_card_names
from .deck_changelog import collect_saves, current_era_start, merge_saves_by_identity

# Need >= 3 games per half to say anything at all about a trend.
TREND_MIN_GAMES = 6
# 20 percentage points, first half vs second half of a deck's match history.
TREND_SWING_THRESHOLD = 0.20

# Below this many games, a deck's win rate can swing by double digits on a
# single result, so it isn't trusted to help locate a bracket boundary at
# all -- same threshold and same reasoning as TREND_MIN_GAMES, just applied
# across decks instead of across time.
BRACKET_MIN_GAMES = 6
# How big a jump between two consecutively-ranked win rates counts as a real
# tier boundary rather than sampling noise, at the sample sizes this account
# actually has -- same magnitude as TREND_SWING_THRESHOLD, for the same
# "don't imply more precision than a few dozen games supports" reason.
BRACKET_GAP_THRESHOLD = 0.20


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
    # Parallel to match_results — which session each match was played in.
    match_session_ids: list[str] = field(default_factory=list)
    # session_id where this deck's CURRENT decklist was first saved (see
    # deck_changelog.current_era_start). "" (sorts before every real
    # session_id) if this deck has no save history at all, making
    # since_last_change_results equivalent to the full match_results.
    era_start_session_id: str = ""

    @property
    def since_last_change_results(self) -> list[bool]:
        """The suffix of match_results played at or after this deck's
        current decklist was saved.

        Rationale (the user's own Arena matchmaking domain knowledge, not
        verified from logs — same status as the Reference Commanders
        appendix): right after a decklist change, Arena is hypothesized to
        preferentially pair a deck against other recently-changed decks
        specifically to re-rank it quickly, rather than against the general
        population. That makes this window a faster, less-diluted read on
        the deck's CURRENT bracket than a lifetime win rate blending in
        whatever the deck used to be before the change.
        """
        return [
            won
            for session_id, won in zip(self.match_session_ids, self.match_results)
            if session_id >= self.era_start_session_id
        ]


@dataclass
class BracketBand:
    """One inferred performance tier: every deck whose win rate landed
    between two big gaps in this segment's sorted deck win rates.

    `index` starts at 1 for the weakest observed band and increases with
    power -- this is NOT a claim about paper Commander's 1-5 bracket scale,
    just a relative ordering specific to this account's own decks."""

    index: int
    low: float
    high: float
    deck_ids: list[str] = field(default_factory=list)


@dataclass
class BracketSegment:
    """One matchmaking population: ranked Brawl, or casual Brawl."""

    deck_stats: dict[str, DeckStats] = field(default_factory=dict)
    commander_pilots: dict[int, CommanderPilotStats] = field(default_factory=dict)
    opponent_commander_tallies: dict[int, OpponentCommanderTally] = field(default_factory=dict)
    deck_trends: dict[str, DeckTrend] = field(default_factory=dict)
    # Derived from deck_stats alone (see estimate_brackets) -- the one part
    # of this segment where the same person piloted every entity, so a
    # relative win-rate comparison isn't also comparing different pilots'
    # skill.
    bracket_bands: list[BracketBand] = field(default_factory=list)


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
    filenames; join_deck_to_matches emits matches in file-offset order).

    `deck_ids` here are canonical deck identities (see
    deck_changelog.merge_saves_by_identity), same as DeckStats' keys — but
    iter_outcomes' GameOutcome.deck_id is always a raw Arena deck_id, so
    every outcome is resolved to its canonical identity before folding in,
    re-deriving the mapping independently rather than threading it through
    from card_stats.collect_card_stats — consistent with this codebase's
    existing "regenerate from scratch" style (collect_saves is already
    called independently by card_stats.py, reports.py, deck_changelog.py)."""
    raw_by_deck, _ = collect_saves(cfg)
    by_deck, deck_id_to_canonical = merge_saves_by_identity(raw_by_deck)

    trends = {deck_id: DeckTrend(deck_id) for deck_id in deck_ids}
    for deck_id, trend in trends.items():
        saves = by_deck.get(deck_id)
        # "" if there's no save history at all -- sorts before every real
        # session_id, so since_last_change_results falls back to the whole
        # match history rather than silently dropping it.
        trend.era_start_session_id = current_era_start(saves) if saves else ""

    throwaway_summary = card_stats.Summary()  # counts already reported from collect_card_stats
    for outcome, _text, _dt in iter_outcomes(cfg, throwaway_summary):
        canonical = deck_id_to_canonical.get(outcome.deck_id)
        trend = trends.get(canonical) if canonical is not None else None
        if trend is not None:
            trend.match_results.append(outcome.won)
            trend.match_session_ids.append(outcome.session_id)
    return trends


def bracket_signal_for_deck(stats: DeckStats, trend: DeckTrend | None) -> tuple[float, int]:
    """The (win_rate, games) pair used to place one deck for bracket
    purposes: its win rate since the last decklist change, once that's
    reached BRACKET_MIN_GAMES, else its all-time record.

    See DeckTrend.since_last_change_results for why the post-change window
    is preferred when it's big enough to trust — this is just the
    threshold-gated choice between that and the lifetime fallback, reused
    by both estimate_brackets (to find the boundaries) and the Deck Trends
    render (to place each deck against them), so the two always agree."""
    since_change = trend.since_last_change_results if trend else []
    if len(since_change) >= BRACKET_MIN_GAMES:
        return sum(since_change) / len(since_change), len(since_change)
    return (stats.gp_wins / stats.gp if stats.gp else 0.0), stats.gp


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


def estimate_brackets(
    deck_stats: dict[str, DeckStats], deck_trends: dict[str, DeckTrend]
) -> list[BracketBand]:
    """How many performance tiers this segment's own decks actually
    separate into: sort decks meeting BRACKET_MIN_GAMES (via
    bracket_signal_for_deck — since-last-change win rate where there's
    enough of it, else lifetime) by that win rate, and cut a new band open
    wherever a gap of >= BRACKET_GAP_THRESHOLD opens between two
    consecutive decks. Decks are the only entities in a BracketSegment
    piloted exclusively by this account, so this is the one win-rate
    comparison here that isn't also mixing in different players' skill --
    see the overlap note in bracket_report.py for why opponent/own-commander
    win rates aren't pooled in with this.

    Returns [] if fewer than 2 decks qualify -- can't find a gap with 0 or 1
    points to compare."""
    qualifying = []
    for deck_id, stats in deck_stats.items():
        rate, games = bracket_signal_for_deck(stats, deck_trends.get(deck_id))
        if games >= BRACKET_MIN_GAMES:
            qualifying.append((deck_id, rate))
    qualifying.sort(key=lambda pair: pair[1])
    if len(qualifying) < 2:
        return []

    bands: list[list[tuple[str, float]]] = [[qualifying[0]]]
    for prev, cur in zip(qualifying, qualifying[1:]):
        if cur[1] - prev[1] >= BRACKET_GAP_THRESHOLD:
            bands.append([])
        bands[-1].append(cur)

    return [
        BracketBand(
            index=i + 1,
            low=band[0][1],
            high=band[-1][1],
            deck_ids=[deck_id for deck_id, _ in band],
        )
        for i, band in enumerate(bands)
    ]


def bracket_index_for(bands: list[BracketBand], win_rate: float) -> int | None:
    """Which band a win rate falls into, for placing an entity that wasn't
    necessarily one of the decks estimate_brackets was built from (an
    opponent commander's inverted win rate, say). A value inside an existing
    band's [low, high] lands there; a value in a gap between bands, or
    outside every band's range, is assigned to whichever band's edge it's
    numerically closest to -- still the best available guess, just less
    confident than a value that landed inside an observed band.

    None if there are no bands to place it in at all.
    """
    if not bands:
        return None
    for band in bands:
        if band.low <= win_rate <= band.high:
            return band.index
    closest = min(bands, key=lambda b: min(abs(win_rate - b.low), abs(win_rate - b.high)))
    return closest.index


def _build_segment(cfg: Config, deck_stats: dict[str, DeckStats]) -> BracketSegment:
    deck_trends = collect_deck_trends(cfg, set(deck_stats))
    return BracketSegment(
        deck_stats=deck_stats,
        commander_pilots=aggregate_own_commanders(deck_stats),
        opponent_commander_tallies=aggregate_opponent_commanders(deck_stats),
        deck_trends=deck_trends,
        bracket_bands=estimate_brackets(deck_stats, deck_trends),
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
    for canonical, stats in all_deck_stats.items():
        if not stats.own_commander_grp_ids:
            continue  # not a Brawl deck
        if is_ranked_brawl(stats.format):
            ranked_decks[canonical] = stats
        else:
            casual_decks[canonical] = stats

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

    bracket_dir = cfg.archive_dir / "reports" / "bracket_stats"
    bracket_dir.mkdir(parents=True, exist_ok=True)
    dest = bracket_dir / "overview.md"
    dest.write_text(render_bracket_stats(data, names), encoding="utf-8")
    summary.files_written.append(dest.name)

    return summary
