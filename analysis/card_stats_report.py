"""Rendering one deck's personal card-performance stats to Markdown.

Every rate is reported next to its raw game count — small samples are the
explicit point of this report, not something to round away or hide.
"""

from __future__ import annotations

from .carddb import CardNames
from .card_stats import CardTally, DeckStats
from .reports import PeriodStats


def _rate(wins: int, n: int) -> str:
    if not n:
        return "—"
    return f"{wins / n:.0%} ({wins}/{n})"


def _iih(tally: CardTally) -> str:
    gih_n, gih_wins = tally.oh + tally.gd, tally.oh_wins + tally.gd_wins
    if not gih_n or not tally.gns:
        return "—"
    return f"{(gih_wins / gih_n - tally.gns_wins / tally.gns):+.0%}"


def _card_row(card_id: int, tally: CardTally, names: CardNames) -> tuple[str, str]:
    """Returns (sort_key, rendered_row)."""
    name = names[card_id]
    gih_n, gih_wins = tally.oh + tally.gd, tally.oh_wins + tally.gd_wins
    row = (
        f"| {name} | {_rate(tally.oh_wins, tally.oh)} | {_rate(tally.gd_wins, tally.gd)} "
        f"| {_rate(gih_wins, gih_n)} | {_rate(tally.gns_wins, tally.gns)} | {_iih(tally)} |"
    )
    return name, row


def _render_early_forfeits(stats: DeckStats | PeriodStats, names: CardNames) -> str:
    heading = (
        "## Early Forfeits\n\n"
        "_Games we conceded by turn 4 (or before turn 1 ever started — "
        "\"turn 0\", conceded during the mulligan/opening-hand review). Cards "
        "that show up often here were sitting dead in hand when we gave up — "
        "worth reconsidering, not just cards with a bad GIH WR._\n\n"
    )

    if not stats.early_forfeit_games:
        return heading + "_No early forfeits recorded._\n"

    rows = sorted(
        ((names[card_id], count) for card_id, count in stats.early_forfeit_hand_tallies.items()),
        key=lambda row: (-row[1], row[0]),
    )
    table_rows = "\n".join(f"| {name} | {count} |" for name, count in rows)
    table = (
        f"{stats.early_forfeit_games} of {stats.gp} game(s) — cards still unplayed in hand "
        "when we conceded, most-frequent first:\n\n"
        "| Card | Times Stuck |\n|---|---|\n" + table_rows + "\n"
    )
    return heading + table


def _render_card_table(card_tallies: dict[int, CardTally], names: CardNames) -> str:
    if not card_tallies:
        return "_No per-card data yet._\n"
    rows = sorted(_card_row(card_id, tally, names) for card_id, tally in card_tallies.items())
    return (
        "| Card | OH WR | GD WR | GIH WR | GNS WR | IIH |\n"
        "|---|---|---|---|---|---|\n" + "\n".join(row for _, row in rows) + "\n"
    )


def render_card_stats(stats: DeckStats, names: CardNames) -> str:
    header = (
        f"# {stats.name}\n\n"
        f"- **Deck ID:** `{stats.deck_id}`\n"
        f"- **Format:** {stats.format or 'unknown'}\n"
        f"- **Games Played:** {_rate(stats.gp_wins, stats.gp)}\n"
        f"- **Mulligan Rate:** {_rate(stats.mulligan_games, stats.gp)}\n\n"
        "_Personal stats from this account's own games only — samples are "
        "small by nature, read the raw N alongside every rate. Brawl "
        "commanders and unused sideboard cards will trivially show 0% seen, "
        "since they aren't tracked through the hand zone the same way._\n\n"
    )
    body = _render_card_table(stats.card_tallies, names)
    return header + body + "\n" + _render_early_forfeits(stats, names)


def render_period_stats(stats: PeriodStats, names: CardNames) -> str:
    header = (
        f"# {stats.cadence.title()} — {stats.period_key} — {stats.format_group}\n\n"
        f"- **Games Played:** {_rate(stats.gp_wins, stats.gp)}\n"
        f"- **Mulligan Rate:** {_rate(stats.mulligan_games, stats.gp)}\n\n"
        "_Rolled up across every deck of this format group played this "
        "period — a card's numbers sum across every deck that included it, "
        "as if the account played one continuous decklist all period. "
        "Samples are small by nature, read the raw N alongside every "
        "rate._\n\n"
    )
    body = _render_card_table(stats.card_tallies, names)
    return header + body + "\n" + _render_early_forfeits(stats, names)
