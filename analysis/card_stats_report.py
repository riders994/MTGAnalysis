"""Rendering one deck's personal card-performance stats to Markdown.

Every rate is reported next to its raw game count — small samples are the
explicit point of this report, not something to round away or hide.
"""

from __future__ import annotations

from .carddb import CardNames
from .card_stats import CardTally, DeckStats


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


def render_card_stats(stats: DeckStats, names: CardNames) -> str:
    header = (
        f"# {stats.name}\n\n"
        f"- **Deck ID:** `{stats.deck_id}`\n"
        f"- **Format:** {stats.format or 'unknown'}\n"
        f"- **Games Played:** {_rate(stats.gp_wins, stats.gp)}\n\n"
        "_Personal stats from this account's own games only — samples are "
        "small by nature, read the raw N alongside every rate. Brawl "
        "commanders and unused sideboard cards will trivially show 0% seen, "
        "since they aren't tracked through the hand zone the same way._\n\n"
    )

    if not stats.card_tallies:
        return header + "_No per-card data yet._\n"

    rows = sorted(
        _card_row(card_id, tally, names) for card_id, tally in stats.card_tallies.items()
    )
    table = (
        "| Card | OH WR | GD WR | GIH WR | GNS WR | IIH |\n"
        "|---|---|---|---|---|---|\n" + "\n".join(row for _, row in rows) + "\n"
    )
    return header + table
