"""Rendering the Brawl bracket-signal report to Markdown.

Every section here answers, as best the data allows, one of the three
questions this report exists for: how many brackets effectively exist, each
commander's starting bracket, and each deck's current bracket. None of them
can be answered with certainty — see the Limitations section, which is
rendered first and is not decorative. The bracket count itself is now a real
(if coarse) estimate — see bracket_stats.estimate_brackets — rather than a
flat "can't be done"; every "Est. Bracket" column below places that entity
against those same account-specific bands.
"""

from __future__ import annotations

from dataclasses import dataclass

from .bracket_reference import REFERENCE_COMMANDERS, ReferenceEntry
from .bracket_stats import (
    BRACKET_GAP_THRESHOLD,
    BRACKET_MIN_GAMES,
    BracketBand,
    BracketData,
    BracketSegment,
    CommanderPilotStats,
    DeckTrend,
    bracket_index_for,
    bracket_signal_for_deck,
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


def _render_bracket_band_summary(title: str, bands: list[BracketBand]) -> str:
    if not bands:
        return (
            f"_{title}: fewer than 2 decks have {BRACKET_MIN_GAMES}+ games yet — "
            "not enough to compare, let alone find a gap between them._\n\n"
        )
    if len(bands) == 1:
        band = bands[0]
        return (
            f"_{title}: {len(band.deck_ids)} deck(s) qualify "
            f"({band.low:.0%}–{band.high:.0%} win rate) and no gap of "
            f"{BRACKET_GAP_THRESHOLD:.0%} or more separates any of them — this account's "
            "data doesn't yet support more than one distinct tier._\n\n"
        )
    rows = "\n".join(
        f"- Bracket {band.index}: {band.low:.0%}–{band.high:.0%} win rate ({len(band.deck_ids)} deck(s))"
        for band in bands
    )
    return f"_{title}: {len(bands)} distinct tiers found in this account's own decks:_\n\n{rows}\n\n"


def _render_how_many_brackets(data: BracketData) -> str:
    return (
        "## How Many Brackets Exist?\n\n"
        "_Arena gives no bracket count to report — there is no equivalent "
        "of paper Commander's 1-5 scale exposed anywhere in the client or "
        "logs for Brawl. The best available substitute: look for real gaps "
        "in this account's own deck win rates. Two decks with "
        f"{BRACKET_MIN_GAMES}+ games each, separated by a "
        f"{BRACKET_GAP_THRESHOLD:.0%}-or-more jump in win rate, are treated as sitting in "
        "different tiers; decks below that game count aren't used to find the "
        "boundaries. Only decks qualify for this — commanders faced or "
        "piloted are not pooled in here, because that would also be mixing "
        "in other players' piloting skill (see the overlap note further "
        "down). Every boundary below is specific to this account's own "
        "matches and can shift as more games accumulate — it says nothing "
        "about the wider Brawl population.\n\n"
        "One refinement: a deck's win rate since its last decklist change is "
        "used in place of its lifetime win rate here, once that window "
        f"itself has {BRACKET_MIN_GAMES}+ games. Per the user's own Arena "
        "experience — unverified from logs, same status as the Reference "
        "Commanders appendix — Arena is believed to preferentially match a "
        "freshly-changed deck against other freshly-changed decks, to "
        "re-rank it quickly rather than testing it against the general "
        "population; that makes the since-change window converge on a "
        "deck's *current* tier faster than blending in whatever the deck "
        "used to be. See the Deck Trends table's \"Since Last Change\" "
        "column for the raw numbers behind this. It isn't applied to "
        "opponent commanders below — their per-commander samples are "
        "already too thin to slice any further._\n\n"
        + _render_bracket_band_summary("Ranked Brawl", data.ranked.bracket_bands)
        + _render_bracket_band_summary("Casual Brawl", data.casual.bracket_bands)
    )


def _reference_entry_for(name: str) -> ReferenceEntry | None:
    return REFERENCE_COMMANDERS.get(name)


def _merge_opponent_tallies_by_name(
    tallies: dict[int, OpponentCommanderTally], names: CardNames
) -> dict[str, OpponentCommanderTally]:
    """Arena assigns a new grp_id to every reprint, so the same commander
    faced in two different printings would otherwise show up as two
    half-sized rows. Fold by display name instead, same rationale as
    deck_changelog's merge-by-identity."""
    merged: dict[str, OpponentCommanderTally] = {}
    for grp_id, tally in tallies.items():
        target = merged.setdefault(names[grp_id], OpponentCommanderTally())
        target.games += tally.games
        target.wins += tally.wins
        target.early_concedes += tally.early_concedes
    return merged


def _merge_pilots_by_name(
    pilots: dict[int, CommanderPilotStats], names: CardNames
) -> dict[str, CommanderPilotStats]:
    """Same reprint-collapsing as _merge_opponent_tallies_by_name, for
    commanders we've piloted ourselves. The merged entry's grp_id is just
    whichever printing was folded in first — nothing downstream reads it."""
    merged: dict[str, CommanderPilotStats] = {}
    for grp_id, pilot in pilots.items():
        target = merged.setdefault(names[grp_id], CommanderPilotStats(grp_id=grp_id))
        target.deck_ids |= pilot.deck_ids
        target.gp += pilot.gp
        target.gp_wins += pilot.gp_wins
        target.early_forfeit_games += pilot.early_forfeit_games
    return merged


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
    tallies: dict[str, OpponentCommanderTally],
) -> ReferenceValidation | None:
    """None if we haven't faced at least one reference commander from EACH
    tier (can't compare what we don't have)."""
    high_games = high_wins = lower_games = lower_wins = 0
    for name, tally in tallies.items():
        entry = _reference_entry_for(name)
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


def _bracket_label(bands: list[BracketBand], games: int, win_rate: float) -> str:
    """"—" below BRACKET_MIN_GAMES (a single data point can't be placed with
    any confidence) or if this segment has no bands to place it in yet."""
    if games < BRACKET_MIN_GAMES:
        return "—"
    index = bracket_index_for(bands, win_rate)
    return str(index) if index is not None else "—"


def _render_opponent_commander_table(
    tallies: dict[str, OpponentCommanderTally], bands: list[BracketBand]
) -> str:
    if not tallies:
        return "_No opponent commanders faced yet in this segment._\n\n"
    rows = sorted(
        ((name, tally, _reference_entry_for(name)) for name, tally in tallies.items()),
        key=lambda row: (-row[1].games, row[0]),
    )
    table_rows = "\n".join(
        f"| {name} | {tally.games} | {_rate(tally.wins, tally.games)} "
        f"| {_rate(tally.early_concedes, tally.games)} | {entry.tier if entry else '—'} "
        # Inverted: the opponent's power runs opposite our win rate against
        # them (we winning a lot means they're weak), placed on the same
        # scale estimate_brackets built from our own decks' win rates.
        f"| {_bracket_label(bands, tally.games, 1 - tally.wins / tally.games)} |"
        for name, tally, entry in rows
    )
    return (
        "| Commander | Faced | Our Win Rate | Early Concedes | Reference Tier | Est. Bracket |\n"
        "|---|---|---|---|---|---|\n" + table_rows + "\n\n"
    )


def _overlap_names(
    pilots: dict[str, CommanderPilotStats], opponent_tallies: dict[str, OpponentCommanderTally]
) -> set[str]:
    return set(pilots) & set(opponent_tallies)


def _render_own_commander_table(
    pilots: dict[str, CommanderPilotStats],
    opponent_tallies: dict[str, OpponentCommanderTally],
    bands: list[BracketBand],
) -> str:
    if not pilots:
        return "_No commanders piloted yet in this segment._\n\n"
    rows = sorted(
        ((name, pilot, _reference_entry_for(name)) for name, pilot in pilots.items()),
        key=lambda row: (-row[1].gp, row[0]),
    )
    table_rows = "\n".join(
        f"| {name} | {len(pilot.deck_ids)} | {pilot.gp} | {_rate(pilot.gp_wins, pilot.gp)} "
        f"| {entry.tier if entry else '—'} | {_bracket_label(bands, pilot.gp, pilot.gp_wins / pilot.gp)} |"
        for name, pilot, entry in rows
    )
    table = (
        "| Commander | Decks | Games | Our Win Rate | Reference Tier | Est. Bracket |\n"
        "|---|---|---|---|---|---|\n" + table_rows + "\n\n"
    )

    overlap = _overlap_names(pilots, opponent_tallies)
    if not overlap:
        return table
    overlap_notes = "\n".join(
        f"- You've also piloted **{name}** yourself "
        f"({pilots[name].gp} games, {_rate(pilots[name].gp_wins, pilots[name].gp)}) — "
        "not directly comparable to the win rate *against* it above; one "
        "measures your skill with the deck, the other measures your success "
        "facing someone else's build of it."
        for name in sorted(overlap)
    )
    return table + overlap_notes + "\n\n"


def _render_deck_trends(
    deck_stats: dict[str, DeckStats],
    deck_trends: dict[str, DeckTrend],
    names: CardNames,
    bands: list[BracketBand],
    *,
    is_ranked: bool,
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

        # Only shown once a real rebuild has actually happened -- if the
        # since-change window covers the whole recorded history (no rebuild
        # yet), there's nothing to distinguish from Overall Win Rate.
        since_change = trend.since_last_change_results if trend else []
        since_display = (
            _rate(sum(since_change), len(since_change))
            if trend and len(since_change) < len(match_results)
            else "—"
        )

        signal_rate, signal_games = bracket_signal_for_deck(stats, trend)
        bracket = _bracket_label(bands, signal_games, signal_rate)
        rows.append(
            (stats.name, stats.gp, _rate(stats.gp_wins, stats.gp), since_display, bracket, label, detail)
        )

    rows.sort(key=lambda row: (-row[1], row[0]))
    table_rows = "\n".join(
        f"| {name} | {gp} | {overall} | {since_change} | {bracket} | {label} | {detail} |"
        for name, gp, overall, since_change, bracket, label, detail in rows
    )
    table = (
        "| Deck | Games | Overall Win Rate | Since Last Change | Est. Bracket | Trend | First Half → Second Half |\n"
        "|---|---|---|---|---|---|---|\n" + table_rows + "\n\n"
    )
    return caveat + table


def _render_segment(title: str, segment: BracketSegment, names: CardNames, *, is_ranked: bool) -> str:
    if not segment.deck_stats and not segment.opponent_commander_tallies:
        return f"### {title}\n\n_No {title} data yet._\n\n"

    opponent_by_name = _merge_opponent_tallies_by_name(segment.opponent_commander_tallies, names)
    pilots_by_name = _merge_pilots_by_name(segment.commander_pilots, names)
    validation = _reference_validation(opponent_by_name)
    return (
        f"### {title}\n\n"
        "#### Opponent Commanders (their starting bracket, as we've experienced it)\n\n"
        + _render_opponent_commander_table(opponent_by_name, segment.bracket_bands)
        + _render_reference_check(validation)
        + "#### Our Commanders (their starting bracket, as we've piloted them)\n\n"
        + _render_own_commander_table(pilots_by_name, opponent_by_name, segment.bracket_bands)
        + "#### Deck Trends (each deck's current bracket)\n\n"
        + _render_deck_trends(
            segment.deck_stats, segment.deck_trends, names, segment.bracket_bands, is_ranked=is_ranked
        )
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
        + _render_how_many_brackets(data)
        + "## Commander & Deck Brackets\n\n"
        + _render_segment("Ranked Brawl", data.ranked, names, is_ranked=True)
        + _render_segment("Casual Brawl", data.casual, names, is_ranked=False)
        + _render_reference_appendix()
    )
