"""Orchestration: scan the archive, group deck saves by deck, write changelogs.

Regenerated from scratch on every run rather than tracked incrementally —
sessions are already immutable and deduplicated in the archive, and the
corpus is small enough that re-deriving everything each time costs nothing
but avoids a second state file that could drift from the archive it comes
from. This mirrors collector.verify's own "just re-check everything" approach.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from collector.config import Config

from .carddb import CardDbUnavailable, load_card_names
from .changelog import assign_slugs, render_deck_changelog
from .deck_events import DeckSave, extract_deck_saves
from .sessions import iter_sessions, read_text


@dataclass
class Summary:
    sessions_scanned: int = 0
    saves_found: int = 0
    decks_written: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _version_key(save: DeckSave, order: int) -> tuple[int, int]:
    try:
        return (int(save.version), order)
    except (TypeError, ValueError):
        return (-1, order)


def collect_saves(cfg: Config) -> tuple[dict[str, list[DeckSave]], Summary]:
    summary = Summary()
    by_deck: dict[str, list[DeckSave]] = defaultdict(list)

    for order, session in enumerate(iter_sessions(cfg)):
        summary.sessions_scanned += 1
        saves = extract_deck_saves(read_text(session), session_id=session.session_id)
        for save in saves:
            by_deck[save.deck_id].append(save)
            summary.saves_found += 1

    for saves in by_deck.values():
        saves.sort(key=lambda save: _version_key(save, 0))

    return by_deck, summary


def run(cfg: Config) -> Summary:
    by_deck, summary = collect_saves(cfg)
    if not by_deck:
        return summary

    try:
        names = load_card_names(cfg)
    except CardDbUnavailable as exc:
        summary.warnings.append(str(exc))
        return summary

    changelogs_dir = cfg.archive_dir / "changelogs"
    changelogs_dir.mkdir(parents=True, exist_ok=True)

    deck_names = {deck_id: saves[-1].name for deck_id, saves in by_deck.items()}
    slugs, collision_warnings = assign_slugs(deck_names)
    summary.warnings.extend(collision_warnings)

    for deck_id, saves in by_deck.items():
        dest = changelogs_dir / f"{slugs[deck_id]}.md"
        dest.write_text(render_deck_changelog(saves, names), encoding="utf-8")
        summary.decks_written.append(dest.name)

    return summary
