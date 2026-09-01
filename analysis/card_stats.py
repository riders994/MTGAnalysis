"""Orchestration: bind archived matches to decks, tally per-card in-hand
performance, render one Markdown report per deck.

Regenerated from scratch on every run, same rationale as deck_changelog.py.
Landed in two checkpoints: collect_card_stats' deck-level GP/GP-WR numbers
come from match_events.py alone (no GRE parsing); the per-card OH/GD/GIH/
GNS/IIH tallies are a strictly additive second pass built on game_state.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime

from collector.config import Config

from .carddb import CardDbUnavailable, LandInfo, load_card_names, load_land_info
from .changelog import assign_slugs
from .deck_changelog import collect_saves, merge_saves_by_identity
from .game_state import GameRecord, parse_games
from .match_events import GameOutcome, join_deck_to_matches
from .sessions import iter_sessions, read_text, session_datetime

# Real archived concedes cluster at turn 0 (conceded before turnInfo ever
# appeared — during the mulligan/opening-hand review), 2, and 4, then jump to
# 6+. This cutoff is a judgment call, not a rule from Arena or 17lands; tune
# it if a larger sample doesn't bear the pattern out.
EARLY_FORFEIT_MAX_TURN = 4

# Turn numbers are a GLOBAL/absolute counter across both players (confirmed
# against real archived data), not per-player — turn 6 is roughly our 3rd
# turn. Used only for the opponent-commander early-concede tally below,
# which the user asked to mark "early" at a looser cutoff than
# EARLY_FORFEIT_MAX_TURN's card-in-hand tracker.
COMMANDER_EARLY_CONCEDE_MAX_TURN = 6


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
class OpponentCommanderTally:
    games: int = 0  # times we've faced a deck led by this commander
    wins: int = 0  # times we won that matchup
    early_concedes: int = 0  # times WE conceded by COMMANDER_EARLY_CONCEDE_MAX_TURN


@dataclass
class DeckStats:
    deck_id: str  # latest raw Arena DeckId seen for this identity, display only — grouping uses name identity
    name: str
    format: str | None
    gp: int = 0
    gp_wins: int = 0
    mulligan_games: int = 0  # games where we took at least one mulligan
    mulliganed_hand_tallies: dict[int, int] = field(default_factory=dict)  # card_id -> times sent back
    early_forfeit_games: int = 0  # games WE conceded by EARLY_FORFEIT_MAX_TURN (or turn 0)
    early_forfeit_hand_tallies: dict[int, int] = field(default_factory=dict)  # card_id -> times stuck
    card_tallies: dict[int, CardTally] = field(default_factory=dict)
    # Always empty outside Brawl — no Command Zone means no opponent
    # commander is ever seen. card_id here means the commander's grpId.
    opponent_commander_tallies: dict[int, OpponentCommanderTally] = field(default_factory=dict)
    # Our own commander(s) — empty outside Brawl. 2 entries for a
    # Partner/Background pairing.
    own_commander_grp_ids: frozenset[int] = field(default_factory=frozenset)
    # Land-use tracking — all keyed by grpId (land identity), all restricted
    # to this deck's own decklist. "_wins" variants are the subset of the
    # matching total tallied only from games we won (losses = total - wins).
    land_play_tallies: dict[int, int] = field(default_factory=dict)  # times played as a land
    land_play_tallies_wins: dict[int, int] = field(default_factory=dict)
    land_turn_tallies: dict[int, int] = field(default_factory=dict)  # summed first-play turn
    land_turn_game_counts: dict[int, int] = field(default_factory=dict)  # games it was played in
    other_face_cast_tallies: dict[int, int] = field(default_factory=dict)  # times its other face was cast
    land_ability_tallies: dict[int, int] = field(default_factory=dict)  # non-mana ability activations
    land_ability_tallies_wins: dict[int, int] = field(default_factory=dict)


@dataclass
class Summary:
    sessions_scanned: int = 0
    matches_found: int = 0
    games_parsed: int = 0
    decks_written: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def decklist_card_ids(save) -> set[int]:
    return {
        card.card_id
        for cards in (save.main_deck, save.sideboard, save.command_zone)
        for card in cards
    }


def own_commander_ids(save) -> frozenset[int]:
    """Every commander grpId in this deck's Command Zone (2 for a Partner/
    Background pairing). Empty outside Brawl — no Command Zone means no
    commander was ever saved."""
    return frozenset(card.card_id for card in save.command_zone)


def iter_outcomes(cfg: Config, summary: Summary) -> Iterator[tuple[GameOutcome, str, datetime]]:
    """Every bound match across every archived session, oldest first.

    Yields (outcome, that session's full log text, the session's start
    datetime) so callers can slice GRE spans and bucket by real-world date
    without re-walking the archive themselves. Updates summary.
    sessions_scanned/matches_found as it goes — shared by both card_stats.py
    (all-time per-deck reports) and reports.py (period-scoped reports).
    """
    for session in iter_sessions(cfg):
        summary.sessions_scanned += 1
        text = read_text(session)
        dt = session_datetime(session)

        for outcome in join_deck_to_matches(text, session_id=session.session_id):
            summary.matches_found += 1
            yield outcome, text, dt


def fold_game(
    target, game: GameRecord, outcome: GameOutcome, decklist_ids: set[int], land_info: LandInfo
) -> None:
    """Fold one parsed game into any stats object exposing mulligan_games/
    mulliganed_hand_tallies/card_tallies/early_forfeit_games/
    early_forfeit_hand_tallies/opponent_commander_tallies/the land_* tallies
    — shared by DeckStats (all-time, one deck) and reports.PeriodStats (one
    period, possibly several decks of the same format)."""
    if game.mulligan_count > 0:
        target.mulligan_games += 1
        for card_id in game.mulliganed_hand_grp_ids:
            if card_id in decklist_ids:
                target.mulliganed_hand_tallies[card_id] = (
                    target.mulliganed_hand_tallies.get(card_id, 0) + 1
                )

    commander_early_concede = _is_early_self_forfeit(outcome, game, COMMANDER_EARLY_CONCEDE_MAX_TURN)
    for commander_id in game.opponent_commander_grp_ids:
        commander_tally = target.opponent_commander_tallies.setdefault(
            commander_id, OpponentCommanderTally()
        )
        commander_tally.games += 1
        if outcome.won:
            commander_tally.wins += 1
        if commander_early_concede:
            commander_tally.early_concedes += 1

    for card_id in decklist_ids:
        tally = target.card_tallies.setdefault(card_id, CardTally())
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

    if _is_early_self_forfeit(outcome, game, EARLY_FORFEIT_MAX_TURN):
        target.early_forfeit_games += 1
        for card_id in game.final_hand:
            if card_id in decklist_ids:
                target.early_forfeit_hand_tallies[card_id] = (
                    target.early_forfeit_hand_tallies.get(card_id, 0) + 1
                )

    for card_id, count in game.land_play_counts.items():
        if card_id not in decklist_ids:
            continue
        target.land_play_tallies[card_id] = target.land_play_tallies.get(card_id, 0) + count
        if outcome.won:
            target.land_play_tallies_wins[card_id] = (
                target.land_play_tallies_wins.get(card_id, 0) + count
            )

    for card_id, turn in game.land_first_play_turn.items():
        if card_id not in decklist_ids:
            continue
        target.land_turn_tallies[card_id] = target.land_turn_tallies.get(card_id, 0) + turn
        target.land_turn_game_counts[card_id] = target.land_turn_game_counts.get(card_id, 0) + 1

    for card_id in decklist_ids:
        other_face_grp_ids = land_info[card_id].other_face_grp_ids
        if not other_face_grp_ids:
            continue
        cast_count = sum(game.cast_grp_ids.get(other_id, 0) for other_id in other_face_grp_ids)
        if cast_count:
            target.other_face_cast_tallies[card_id] = (
                target.other_face_cast_tallies.get(card_id, 0) + cast_count
            )

    for card_id in decklist_ids:
        non_mana_ability_ids = land_info[card_id].non_mana_ability_ids
        if not non_mana_ability_ids:
            continue
        activation_count = sum(
            game.ability_activation_counts.get((card_id, ability_id), 0)
            for ability_id in non_mana_ability_ids
        )
        if activation_count:
            target.land_ability_tallies[card_id] = (
                target.land_ability_tallies.get(card_id, 0) + activation_count
            )
            if outcome.won:
                target.land_ability_tallies_wins[card_id] = (
                    target.land_ability_tallies_wins.get(card_id, 0) + activation_count
                )


def collect_card_stats(cfg: Config) -> tuple[dict[str, DeckStats], Summary]:
    raw_by_deck_saves, _ = collect_saves(cfg)
    by_deck_saves, deck_id_to_canonical = merge_saves_by_identity(raw_by_deck_saves)
    try:
        land_info = load_land_info(cfg)
    except CardDbUnavailable:
        land_info = LandInfo.empty()
    summary = Summary()

    stats: dict[str, DeckStats] = {}
    decklist_ids: dict[str, set[int]] = {}
    for canonical, saves in by_deck_saves.items():
        latest = saves[-1]
        stats[canonical] = DeckStats(
            deck_id=latest.deck_id,
            name=latest.name,
            format=latest.format,
            own_commander_grp_ids=own_commander_ids(latest),
        )
        decklist_ids[canonical] = decklist_card_ids(latest)

    for outcome, text, _dt in iter_outcomes(cfg, summary):
        canonical = deck_id_to_canonical.get(outcome.deck_id)
        deck_stats = stats.get(canonical) if canonical is not None else None
        if deck_stats is None:
            summary.warnings.append(
                f"match in session {outcome.session_id} bound to deck "
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
            fold_game(deck_stats, game, outcome, decklist_ids[canonical], land_info)

    return stats, summary


def _is_early_self_forfeit(outcome: GameOutcome, game: GameRecord, max_turn: int) -> bool:
    """True if WE gave up on this game by max_turn (or before turn 1 ever
    started). A concession always defeats the conceder, so reason == Concede
    and we lost means it was ours, not the opponent's."""
    we_conceded = outcome.reason == "ResultReason_Concede" and not outcome.won
    return we_conceded and (game.last_turn is None or game.last_turn <= max_turn)


def run(cfg: Config) -> Summary:
    from .card_stats_report import render_card_stats  # deferred: avoids a module cycle

    stats, summary = collect_card_stats(cfg)
    if not stats:
        return summary

    try:
        names = load_card_names(cfg)
        land_info = load_land_info(cfg)
    except CardDbUnavailable as exc:
        summary.warnings.append(str(exc))
        return summary

    card_stats_dir = cfg.archive_dir / "reports" / "card_stats"
    card_stats_dir.mkdir(parents=True, exist_ok=True)

    deck_names = {canonical: deck_stats.name for canonical, deck_stats in stats.items()}
    slugs, collision_warnings = assign_slugs(deck_names)
    summary.warnings.extend(collision_warnings)

    for canonical, deck_stats in stats.items():
        dest = card_stats_dir / f"{slugs[canonical]}.md"
        dest.write_text(render_card_stats(deck_stats, names, land_info), encoding="utf-8")
        summary.decks_written.append(dest.name)

    return summary
