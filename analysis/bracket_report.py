"""Rendering the Brawl bracket-signal report to Markdown.

Every section here answers, as best the data allows, one of the three
questions this report exists for: how many brackets effectively exist, each
commander's starting bracket, and each deck's current bracket. None of them
can be answered with certainty — see the Limitations section, which is
rendered first and is not decorative.
"""

from __future__ import annotations

from dataclasses import dataclass

from .bracket_reference import REFERENCE_COMMANDERS, ReferenceEntry
from .bracket_stats import (
    BracketData,
    BracketSegment,
    CommanderPilotStats,
    classify_trend,
    half_win_rates,
)
from .card_stats import DeckStats, OpponentCommanderTally
from .carddb import CardNames
from .rates import format_rate as _rate


def _render_limitations() -> str:
    return (
        "## Limitations\n\n"
        "_Arena exposes no ground-truth bracket for Brawl. Competitive/"
        "ranked Brawl (HistoricBrawlRanked) does show a rank in the client, "
        "but that rank is the same constructedRankInfo ladder already "
        "shared by Standard and Alchemy — one unified rank, not a "
        "Brawl-specific one. A rank change after a ranked-Brawl match could "
        "equally have come from an unrelated Standard or Alchemy match "
        "played in between, so it can't be attributed to Brawl without also "
        "matching each rank snapshot to the queue/match that produced it — "
        "not attempted in this report. Independent of that, this rank is "
        "**pure MMR** — a win/loss-driven matchmaking rating, not an "
        "independent measure of deck power — and it is **demoted every "
        "month**, so a rising or falling value would say as much about the "
        "calendar as about the deck.\n\n"
        "Everything below is therefore a best-effort **relative ranking** "
        "built from this account's own win/loss history, cross-checked "
        "against a small hardcoded list of commanders the user already has "
        "strong priors about (see the Reference Commanders appendix). It is "
        "not a bracket oracle, and per-deck sample sizes are small — read "
        "every rate next to its raw game count._\n\n"
    )


def _render_how_many_brackets() -> str:
    return (
        "## How Many Brackets Exist?\n\n"
        "_Arena gives no bracket count to report — there is no equivalent "
        "of paper Commander's 1-5 scale exposed anywhere in the client or "
        "logs for Brawl. The best this report can do is a relative "
        "ordering of commanders and decks by win rate, below._\n\n"
    )


def _reference_entry_for(name: str) -> ReferenceEntry | None:
    return REFERENCE_COMMANDERS.get(name)


@dataclass
class ReferenceValidation:
    high_tier_games: int
    high_tier_wins: int
    lower_tier_games: int
    lower_tier_wins: int

    @property
    def high_tier_win_rate(self) -> float:
        return self.high_tier_wins / self.high_tier_games if self.high_tier_games else 0.0

    @property
    def lower_tier_win_rate(self) -> float:
        return self.lower_tier_wins / self.lower_tier_games if self.lower_tier_games else 0.0

    @property
    def matches_expectation(self) -> bool:
        return self.high_tier_win_rate < self.lower_tier_win_rate


def _reference_validation(
    tallies: dict[int, OpponentCommanderTally], names: CardNames
) -> ReferenceValidation | None:
    """None if we haven't faced at least one reference commander from EACH
    tier (can't compare what we don't have)."""
    high_games = high_wins = lower_games = lower_wins = 0
    for grp_id, tally in tallies.items():
        entry = _reference_entry_for(names[grp_id])
        if entry is None:
            continue
        if entry.tier == "High":
            high_games += tally.games
            high_wins += tally.wins
        else:
            lower_games += tally.games
            lower_wins += tally.wins

    if not high_games or not lower_games:
        return None
    return ReferenceValidation(high_games, high_wins, lower_games, lower_wins)


def _render_reference_check(validation: ReferenceValidation | None) -> str:
    if validation is None:
        return (
            "_Reference check: not enough reference-commander encounters yet "
            "to check (need at least one game against a commander from each "
            "tier)._\n\n"
        )
    verdict = "**matches expectation**" if validation.matches_expectation else "**does not match expectation**"
    return (
        f"_Reference check: faced {validation.high_tier_games} game(s) against "
        f"High-tier reference commanders ({_rate(validation.high_tier_wins, validation.high_tier_games)}) "
        f"vs. {validation.lower_tier_games} game(s) against Lower-tier "
        f"({_rate(validation.lower_tier_wins, validation.lower_tier_games)}) — "
        f"{verdict} (harder against the commanders flagged as stronger)._\n\n"
    )


def _render_opponent_commander_table(
    tallies: dict[int, OpponentCommanderTally], names: CardNames
) -> str:
    if not tallies:
        return "_No opponent commanders faced yet in this segment._\n\n"
    rows = sorted(
        ((names[grp_id], tally, _reference_entry_for(names[grp_id])) for grp_id, tally in tallies.items()),
        key=lambda row: (-row[1].games, row[0]),
    )
    table_rows = "\n".join(
        f"| {name} | {tally.games} | {_rate(tally.wins, tally.games)} "
        f"| {_rate(tally.early_concedes, tally.games)} | {entry.tier if entry else '—'} |"
        for name, tally, entry in rows
    )
    return (
        "| Commander | Faced | Our Win Rate | Early Concedes | Reference Tier |\n"
        "|---|---|---|---|---|\n" + table_rows + "\n\n"
    )


def _overlap_grp_ids(
    pilots: dict[int, CommanderPilotStats], opponent_tallies: dict[int, OpponentCommanderTally]
) -> set[int]:
    return set(pilots) & set(opponent_tallies)


def _render_own_commander_table(
    pilots: dict[int, CommanderPilotStats],
    opponent_tallies: dict[int, OpponentCommanderTally],
    names: CardNames,
) -> str:
    if not pilots:
        return "_No commanders piloted yet in this segment._\n\n"
    rows = sorted(
        ((names[grp_id], pilot, _reference_entry_for(names[grp_id])) for grp_id, pilot in pilots.items()),
        key=lambda row: (-row[1].gp, row[0]),
    )
    table_rows = "\n".join(
        f"| {name} | {len(pilot.deck_ids)} | {pilot.gp} | {_rate(pilot.gp_wins, pilot.gp)} "
        f"| {entry.tier if entry else '—'} |"
        for name, pilot, entry in rows
    )
    table = (
        "| Commander | Decks | Games | Our Win Rate | Reference Tier |\n"
        "|---|---|---|---|---|\n" + table_rows + "\n\n"
    )

    overlap = _overlap_grp_ids(pilots, opponent_tallies)
    if not overlap:
        return table
    overlap_notes = "\n".join(
        f"- You've also piloted **{names[grp_id]}** yourself "
        f"({pilots[grp_id].gp} games, {_rate(pilots[grp_id].gp_wins, pilots[grp_id].gp)}) — "
        "not directly comparable to the win rate *against* it above; one "
        "measures your skill with the deck, the other measures your success "
        "facing someone else's build of it."
        for grp_id in sorted(overlap, key=lambda g: names[g])
    )
    return table + overlap_notes + "\n\n"


def _render_deck_trends(
    deck_stats: dict[str, DeckStats], deck_trends: dict, names: CardNames, *, is_ranked: bool
) -> str:
    if not deck_stats:
        return "_No decks in this segment yet._\n\n"

    caveat = (
        "_Most decks have far fewer games than the minimum needed to say "
        "anything about a trend — read the Games column before trusting the "
        "label._"
    )
    if is_ranked:
        caveat += (
            " _Ranked Brawl is demoted every month; a \"Falling\" read right "
            "after a season boundary can reflect the reset, not the deck "
            "getting weaker — match dates aren't cross-referenced against "
            "season boundaries in this report._"
        )
    caveat += "\n\n"

    rows = []
    for deck_id, stats in deck_stats.items():
        trend = deck_trends.get(deck_id)
        match_results = trend.match_results if trend else []
        label = classify_trend(match_results)
        detail = "—"
        if label != "Insufficient Data":
            first_wr, second_wr = half_win_rates(match_results)
            detail = f"{first_wr:.0%} → {second_wr:.0%}"
        rows.append((stats.name, stats.gp, _rate(stats.gp_wins, stats.gp), label, detail))

    rows.sort(key=lambda row: (-row[1], row[0]))
    table_rows = "\n".join(
        f"| {name} | {gp} | {overall} | {label} | {detail} |"
        for name, gp, overall, label, detail in rows
    )
    table = (
        "| Deck | Games | Overall Win Rate | Trend | First Half → Second Half |\n"
        "|---|---|---|---|---|\n" + table_rows + "\n\n"
    )
    return caveat + table


def _render_segment(title: str, segment: BracketSegment, names: CardNames, *, is_ranked: bool) -> str:
    if not segment.deck_stats and not segment.opponent_commander_tallies:
        return f"### {title}\n\n_No {title} data yet._\n\n"

    validation = _reference_validation(segment.opponent_commander_tallies, names)
    return (
        f"### {title}\n\n"
        "#### Opponent Commanders (their starting bracket, as we've experienced it)\n\n"
        + _render_opponent_commander_table(segment.opponent_commander_tallies, names)
        + _render_reference_check(validation)
        + "#### Our Commanders (their starting bracket, as we've piloted them)\n\n"
        + _render_own_commander_table(segment.commander_pilots, segment.opponent_commander_tallies, names)
        + "#### Deck Trends (each deck's current bracket)\n\n"
        + _render_deck_trends(segment.deck_stats, segment.deck_trends, names, is_ranked=is_ranked)
    )


def _render_reference_appendix() -> str:
    rows = "\n".join(
        f"| {name} | {entry.tier} | {entry.note or '—'} |"
        for name, entry in sorted(REFERENCE_COMMANDERS.items())
    )
    return (
        "## Reference Commanders\n\n"
        "_Hardcoded from the user's own MTG domain knowledge — not fetched "
        "from any external source. Used only to sanity-check the win-rate "
        "signal above, not as a definitive bracket scale._\n\n"
        "| Commander | Tier | Note |\n|---|---|---|\n" + rows + "\n"
    )


def render_bracket_stats(data: BracketData, names: CardNames) -> str:
    return (
        "# Brawl Bracket Signal\n\n"
        + _render_limitations()
        + _render_how_many_brackets()
        + "## Commander & Deck Brackets\n\n"
        + _render_segment("Ranked Brawl", data.ranked, names, is_ranked=True)
        + _render_segment("Casual Brawl", data.casual, names, is_ranked=False)
        + _render_reference_appendix()
    )
