"""Rendering one deck's personal card-performance stats to Markdown.

Every rate is reported next to its raw game count — small samples are the
explicit point of this report, not something to round away or hide.
"""

from __future__ import annotations

from .carddb import CardNames, LandInfo
from .card_stats import CardTally, DeckStats, OpponentCommanderTally
from .rates import format_avg as _avg
from .rates import format_rate as _rate
from .reports import PeriodStats


def _iih(tally: CardTally) -> str:
    gih_n, gih_wins = tally.oh + tally.gd, tally.oh_wins + tally.gd_wins
    if not gih_n or not tally.gns:
        return "—"
    return f"{(gih_wins / gih_n - tally.gns_wins / tally.gns):+.0%}"


def _grouped_by_name(counts: dict[int, int], names: CardNames) -> dict[str, int]:
    """Sums per-grpId counts into per-name totals. Arena assigns a distinct
    grpId to every art/style variant of a card — most decks only ever use
    one variant per slot, but basic lands routinely mix several, which would
    otherwise split one card into multiple identical-looking report rows.
    Grouping by resolved name merges those variants back together, while
    naturally leaving e.g. Plains and Snow-Covered Plains apart, since those
    are genuinely different names."""
    merged: dict[str, int] = {}
    for card_id, count in counts.items():
        merged[names[card_id]] = merged.get(names[card_id], 0) + count
    return merged


def _card_tallies_by_name(card_tallies: dict[int, CardTally], names: CardNames) -> dict[str, CardTally]:
    """Same grpId-variant merge as _grouped_by_name, for the richer per-card tally."""
    merged: dict[str, CardTally] = {}
    for card_id, tally in card_tallies.items():
        target = merged.setdefault(names[card_id], CardTally())
        target.oh += tally.oh
        target.oh_wins += tally.oh_wins
        target.gd += tally.gd
        target.gd_wins += tally.gd_wins
        target.gns += tally.gns
        target.gns_wins += tally.gns_wins
    return merged


def _card_row(name: str, tally: CardTally) -> tuple[str, str]:
    """Returns (sort_key, rendered_row)."""
    gih_n, gih_wins = tally.oh + tally.gd, tally.oh_wins + tally.gd_wins
    row = (
        f"| {name} | {_rate(tally.oh_wins, tally.oh)} | {_rate(tally.gd_wins, tally.gd)} "
        f"| {_rate(gih_wins, gih_n)} | {_rate(tally.gns_wins, tally.gns)} | {_iih(tally)} |"
    )
    return name, row


def _render_mulliganed_hands(stats: DeckStats | PeriodStats, names: CardNames) -> str:
    heading = (
        "## Mulliganed Hands\n\n"
        "_Cards that showed up in a hand we sent back on a mulligan (not the "
        "hand we kept — that's already covered by OH above). Cards that show "
        "up often here were part of hands we judged unkeepable, which is a "
        "different signal than a bad GIH WR._\n\n"
    )

    if not stats.mulligan_games:
        return heading + "_No mulligans recorded._\n"

    rows = sorted(
        _grouped_by_name(stats.mulliganed_hand_tallies, names).items(),
        key=lambda row: (-row[1], row[0]),
    )
    table_rows = "\n".join(f"| {name} | {count} |" for name, count in rows)
    table = (
        f"{stats.mulligan_games} of {stats.gp} game(s) had at least one mulligan — cards seen "
        "in a sent-back hand, most-frequent first:\n\n"
        "| Card | Times Sent Back |\n|---|---|\n" + table_rows + "\n"
    )
    return heading + table


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
        _grouped_by_name(stats.early_forfeit_hand_tallies, names).items(),
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
    rows = sorted(_card_row(name, tally) for name, tally in _card_tallies_by_name(card_tallies, names).items())
    return (
        "| Card | OH WR | GD WR | GIH WR | GNS WR | IIH |\n"
        "|---|---|---|---|---|---|\n" + "\n".join(row for _, row in rows) + "\n"
    )


def _split_by_basic(tallies: dict[int, int], land_info: LandInfo) -> tuple[dict[int, int], dict[int, int]]:
    """Splits a grpId-keyed tally into (basic-land, non-basic-land) halves —
    must happen before any by-name merge, which loses the grpId this
    classification needs."""
    basics: dict[int, int] = {}
    non_basics: dict[int, int] = {}
    for card_id, value in tallies.items():
        (basics if land_info[card_id].is_basic else non_basics)[card_id] = value
    return basics, non_basics


def _render_basic_lands(
    land_play_tallies: dict[int, int],
    land_play_tallies_wins: dict[int, int],
    land_info: LandInfo,
    names: CardNames,
    gp: int,
    gp_wins: int,
) -> str:
    basics, _ = _split_by_basic(land_play_tallies, land_info)
    if not basics:
        return "_No basic lands played yet._\n"
    basics_wins = {card_id: v for card_id, v in land_play_tallies_wins.items() if card_id in basics}
    totals = _grouped_by_name(basics, names)
    wins = _grouped_by_name(basics_wins, names)
    rows = sorted((name, total, wins.get(name, 0)) for name, total in totals.items())
    table_rows = "\n".join(
        f"| {name} | {_avg(total, gp)} | {_avg(won, gp_wins)} | {_avg(total - won, gp - gp_wins)} |"
        for name, total, won in rows
    )
    return (
        "| Land | Avg Played (Overall) | Avg Played (Wins) | Avg Played (Losses) |\n"
        "|---|---|---|---|\n" + table_rows + "\n"
    )


def _render_nonbasic_lands(
    land_turn_tallies: dict[int, int],
    land_turn_game_counts: dict[int, int],
    land_info: LandInfo,
    names: CardNames,
) -> str:
    _, nonbasic_turns = _split_by_basic(land_turn_tallies, land_info)
    if not nonbasic_turns:
        return "_No non-basic lands played yet._\n"
    nonbasic_counts = {
        card_id: v for card_id, v in land_turn_game_counts.items() if card_id in nonbasic_turns
    }
    turns_by_name = _grouped_by_name(nonbasic_turns, names)
    counts_by_name = _grouped_by_name(nonbasic_counts, names)
    rows = sorted(
        (name, _avg(turn_sum, counts_by_name[name])) for name, turn_sum in turns_by_name.items()
    )
    table_rows = "\n".join(f"| {name} | {avg_turn} |" for name, avg_turn in rows)
    return "| Land | Avg Turn Played |\n|---|---|\n" + table_rows + "\n"


def _render_dfc_lands(
    land_play_tallies: dict[int, int], other_face_cast_tallies: dict[int, int], names: CardNames
) -> str:
    """Modal-double-faced-card and adventure lands: how often the other
    (non-land) side got cast instead of the land side getting played.
    Omitted entirely for decks with no such lands, same convention as
    Opponent Commanders below."""
    if not other_face_cast_tallies:
        return ""
    played = _grouped_by_name(
        {card_id: v for card_id, v in land_play_tallies.items() if card_id in other_face_cast_tallies},
        names,
    )
    cast = _grouped_by_name(other_face_cast_tallies, names)
    rows = sorted((name, played.get(name, 0), count) for name, count in cast.items())
    table_rows = "\n".join(
        f"| {name} | {land_count} | {cast_count} | {_rate(cast_count, land_count + cast_count)} |"
        for name, land_count, cast_count in rows
    )
    heading = (
        "\n### Modal/Adventure Faces\n\n"
        "_Lands whose other side (an adventure or a modal double-faced "
        "card's spell face) can be cast instead of played as a land — how "
        "often that happened instead._\n\n"
    )
    return (
        heading
        + "| Land | Played as Land | Cast as Other Side | Other-Side Rate |\n|---|---|---|---|\n"
        + table_rows
        + "\n"
    )


def _render_ability_lands(
    land_ability_tallies: dict[int, int],
    land_ability_tallies_wins: dict[int, int],
    names: CardNames,
    gp_wins: int,
    gp_losses: int,
) -> str:
    """Lands with a non-mana activated ability: how often it got used,
    split wins/losses. Omitted entirely for decks with no such lands."""
    if not land_ability_tallies:
        return ""
    totals = _grouped_by_name(land_ability_tallies, names)
    wins = _grouped_by_name(land_ability_tallies_wins, names)
    rows = sorted((name, wins.get(name, 0), total - wins.get(name, 0)) for name, total in totals.items())
    table_rows = "\n".join(
        f"| {name} | {_avg(won, gp_wins)} | {_avg(lost, gp_losses)} |" for name, won, lost in rows
    )
    heading = (
        "\n### Non-Mana Abilities\n\n"
        "_Lands with an activated ability beyond tapping for mana — how "
        "often it got used, split by whether we won or lost that game._\n\n"
    )
    return heading + "| Land | Activated (Wins) | Activated (Losses) |\n|---|---|---|\n" + table_rows + "\n"


def _render_land_use(stats: DeckStats | PeriodStats, names: CardNames, land_info: LandInfo) -> str:
    gp_losses = stats.gp - stats.gp_wins
    total_played = sum(stats.land_play_tallies.values())
    total_played_wins = sum(stats.land_play_tallies_wins.values())
    kept_hand_land_count_losses = stats.kept_hand_land_count - stats.kept_hand_land_count_wins

    heading = (
        "## Land Use\n\n"
        f"**Lands played per game:** {_avg(total_played, stats.gp)} overall — "
        f"{_avg(total_played_wins, stats.gp_wins)} in wins — "
        f"{_avg(total_played - total_played_wins, gp_losses)} in losses\n\n"
        f"**Lands in hand:** kept hand {_avg(stats.kept_hand_land_count, stats.gp)} overall — "
        f"{_avg(stats.kept_hand_land_count_wins, stats.gp_wins)} in wins — "
        f"{_avg(kept_hand_land_count_losses, gp_losses)} in losses — vs. "
        f"{_avg(stats.mulliganed_hand_land_count, stats.mulliganed_hand_count)} in a hand sent back "
        "on a mulligan\n\n"
    )
    basics = (
        "### Basic Lands\n\n"
        + _render_basic_lands(
            stats.land_play_tallies, stats.land_play_tallies_wins, land_info, names, stats.gp, stats.gp_wins
        )
        + "\n"
    )
    nonbasics = (
        "### Non-Basic Lands\n\n"
        + _render_nonbasic_lands(stats.land_turn_tallies, stats.land_turn_game_counts, land_info, names)
    )
    dfc = _render_dfc_lands(stats.land_play_tallies, stats.other_face_cast_tallies, names)
    abilities = _render_ability_lands(
        stats.land_ability_tallies, stats.land_ability_tallies_wins, names, stats.gp_wins, gp_losses
    )
    return heading + basics + nonbasics + dfc + abilities


def _render_opponent_commanders(
    tallies: dict[int, OpponentCommanderTally], names: CardNames
) -> str:
    """Empty outside Brawl (no Command Zone), so the whole section is
    omitted there rather than shown with a placeholder — unlike Early
    Forfeits, this genuinely doesn't apply to other formats."""
    if not tallies:
        return ""
    heading = (
        "## Opponent Commanders\n\n"
        "_Every Brawl commander we've faced, most-played-against first — "
        "the win rate is ours, against decks led by that commander, not a "
        "measure of the commander's overall power. Early Concedes counts "
        "games we gave up in by turn 6 (roughly our own 3rd turn) or before "
        "turn 1 ever started — a commander that shows up here often is one "
        "we're folding against fast, which is as telling as the win rate._\n\n"
    )

    ranked_by_early_concedes = sorted(
        ((names[commander_id], tally) for commander_id, tally in tallies.items()),
        key=lambda row: (-row[1].early_concedes, row[0]),
    )
    top_name, top_tally = ranked_by_early_concedes[0]
    callout = ""
    if top_tally.early_concedes > 0:
        callout = (
            f"**Commander with most early concedes:** {top_name} "
            f"({top_tally.early_concedes} of {top_tally.games} game(s))\n\n"
        )

    rows = sorted(
        ((names[commander_id], tally) for commander_id, tally in tallies.items()),
        key=lambda row: (-row[1].games, row[0]),
    )
    table_rows = "\n".join(
        f"| {name} | {tally.games} | {_rate(tally.wins, tally.games)} "
        f"| {_rate(tally.early_concedes, tally.games)} |"
        for name, tally in rows
    )
    table = (
        "| Commander | Faced | Our Win Rate | Early Concedes |\n|---|---|---|---|\n"
        + table_rows
        + "\n"
    )
    return "\n" + heading + callout + table


def render_card_stats(stats: DeckStats, names: CardNames, land_info: LandInfo) -> str:
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
    return (
        header
        + body
        + "\n"
        + _render_land_use(stats, names, land_info)
        + "\n"
        + _render_mulliganed_hands(stats, names)
        + "\n"
        + _render_early_forfeits(stats, names)
        + _render_opponent_commanders(stats.opponent_commander_tallies, names)
    )


def render_period_stats(stats: PeriodStats, names: CardNames, land_info: LandInfo) -> str:
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
    return (
        header
        + body
        + "\n"
        + _render_land_use(stats, names, land_info)
        + "\n"
        + _render_mulliganed_hands(stats, names)
        + "\n"
        + _render_early_forfeits(stats, names)
        + _render_opponent_commanders(stats.opponent_commander_tallies, names)
    )
