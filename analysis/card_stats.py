"""Orchestration: bind archived matches to decks, tally per-card in-hand
performance, render one Markdown report per deck.

Regenerated from scratch on every run, same rationale as deck_changelog.py.
Landed in two checkpoints: collect_card_stats' deck-level GP/GP-WR numbers
come from match_events.py alone (no GRE parsing); the per-card OH/GD/GIH/
GNS/IIH tallies are a strictly additive second pass built on game_state.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from collector.config import Config

from .carddb import CardDbUnavailable, load_card_names
from .changelog import assign_slugs
from .deck_changelog import collect_saves
from .game_state import parse_games
from .match_events import join_deck_to_matches
from .sessions import iter_sessions, read_text


@dataclass
class CardTally:
    oh: int = 0
    oh_wins: int = 0
    gd: int = 0
    gd_wins: int = 0
    gns: int = 0
    gns_wins: int = 0
    # gih / gih_wins are oh+gd / oh_wins+gd_wins, computed at render time —
    # not stored, so there's exactly one place counts could drift out of sync.


@dataclass
class DeckStats:
    deck_id: str
    name: str
    format: str | None
    gp: int = 0
    gp_wins: int = 0
    card_tallies: dict[int, CardTally] = field(default_factory=dict)


@dataclass
class Summary:
    sessions_scanned: int = 0
    matches_found: int = 0
    games_parsed: int = 0
    decks_written: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _decklist_card_ids(save) -> set[int]:
    return {
        card.card_id
        for cards in (save.main_deck, save.sideboard, save.command_zone)
        for card in cards
    }


def collect_card_stats(cfg: Config) -> tuple[dict[str, DeckStats], Summary]:
    by_deck_saves, _ = collect_saves(cfg)
    summary = Summary()

    stats: dict[str, DeckStats] = {}
    decklist_ids: dict[str, set[int]] = {}
    for deck_id, saves in by_deck_saves.items():
        latest = saves[-1]
        stats[deck_id] = DeckStats(deck_id=deck_id, name=latest.name, format=latest.format)
        decklist_ids[deck_id] = _decklist_card_ids(latest)

    for session in iter_sessions(cfg):
        summary.sessions_scanned += 1
        text = read_text(session)

        for outcome in join_deck_to_matches(text, session_id=session.session_id):
            summary.matches_found += 1
            deck_stats = stats.get(outcome.deck_id)
            if deck_stats is None:
                summary.warnings.append(
                    f"match in session {session.session_id} bound to deck "
                    f"{outcome.deck_id}, which has no known decklist; skipping"
                )
                continue

            deck_stats.gp += 1
            if outcome.won:
                deck_stats.gp_wins += 1

            span_text = text[outcome.span[0] : outcome.span[1]]
            games = parse_games(span_text, match_id=outcome.match_id, our_seat_id=outcome.our_seat_id)
            for game in games:
                summary.games_parsed += 1
                for card_id in decklist_ids[outcome.deck_id]:
                    tally = deck_stats.card_tallies.setdefault(card_id, CardTally())
                    if card_id in game.opening_hand:
                        tally.oh += 1
                        if outcome.won:
                            tally.oh_wins += 1
                    elif card_id in game.drawn:
                        tally.gd += 1
                        if outcome.won:
                            tally.gd_wins += 1
                    else:
                        tally.gns += 1
                        if outcome.won:
                            tally.gns_wins += 1

    return stats, summary


def run(cfg: Config) -> Summary:
    from .card_stats_report import render_card_stats  # deferred: avoids a module cycle

    stats, summary = collect_card_stats(cfg)
    if not stats:
        return summary

    try:
        names = load_card_names(cfg)
    except CardDbUnavailable as exc:
        summary.warnings.append(str(exc))
        return summary

    card_stats_dir = cfg.archive_dir / "card_stats"
    card_stats_dir.mkdir(parents=True, exist_ok=True)

    deck_names = {deck_id: deck_stats.name for deck_id, deck_stats in stats.items()}
    slugs, collision_warnings = assign_slugs(deck_names)
    summary.warnings.extend(collision_warnings)

    for deck_id, deck_stats in stats.items():
        dest = card_stats_dir / f"{slugs[deck_id]}.md"
        dest.write_text(render_card_stats(deck_stats, names), encoding="utf-8")
        summary.decks_written.append(dest.name)

    return summary
