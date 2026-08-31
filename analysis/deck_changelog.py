"""Orchestration: scan the archive, group deck saves by deck identity, write
changelogs.

Regenerated from scratch on every run rather than tracked incrementally —
sessions are already immutable and deduplicated in the archive, and the
corpus is small enough that re-deriving everything each time costs nothing
but avoids a second state file that could drift from the archive it comes
from. This mirrors collector.verify's own "just re-check everything" approach.

Deck identity is name-based, not raw-deck_id-based: Arena assigns a new
DeckId when a deck is deleted and recreated, which this project's owner uses
as a deliberate "refresh" workflow — merge_saves_by_identity folds those
reissued deck_ids back into one continuous history by exact name match.

Exception: GENERIC_DECK_NAMES. Arena applies these as the permanent,
never-auto-changed name for any deck the player hasn't renamed — every
unrenamed draft, sealed pool, or web import keeps one of these forever. So
unlike a real rename, many genuinely unrelated decks can share one of these
names *at once*, not just sequentially, and merging them by name would
splice unrelated decks' histories together. Each raw deck_id under one of
these names keeps its own separate identity instead.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from collector.config import Config

from .carddb import CardDbUnavailable, load_card_names
from .changelog import assign_slugs, render_deck_changelog
from .deck_events import DeckSave, extract_deck_saves
from .sessions import iter_sessions, read_text

GENERIC_DECK_NAMES = frozenset({"Draft Deck", "Sealed Deck", "Imported Deck"})


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


def merge_saves_by_identity(
    by_deck: dict[str, list[DeckSave]],
) -> tuple[dict[str, list[DeckSave]], dict[str, str]]:
    """Merge raw Arena deck_ids into one continuous identity by exact name
    match: Arena assigns a brand-new deck_id when a deck is deleted and
    recreated under the same name, and no delete event exists in the logs
    to key off instead. Assumes sequential delete-then-recreate, not two
    genuinely simultaneous same-named decks — except for GENERIC_DECK_NAMES,
    where that assumption doesn't hold (see module docstring), so each
    deck_id there keeps its own identity (canonical = the deck_id itself)
    instead of being merged.

    `by_deck` (collect_saves' output) must already have each deck_id's own
    saves sorted chronologically and be visited in chronological
    first-appearance order (both already true of collect_saves' output).
    Raw deck_id groups sharing a name are concatenated in that same order
    and never re-sorted by Version, since Version numbering restarts at
    each new deck_id and would interleave two decks' versions if resorted.

    Returns (saves keyed by canonical identity instead of raw deck_id,
    raw deck_id -> canonical identity map, for resolving GameOutcome.deck_id
    — always a raw id — downstream).
    """
    merged: dict[str, list[DeckSave]] = {}
    deck_id_to_canonical: dict[str, str] = {}
    for deck_id, saves in by_deck.items():
        name = saves[-1].name
        canonical = deck_id if name in GENERIC_DECK_NAMES else name
        deck_id_to_canonical[deck_id] = canonical
        merged.setdefault(canonical, []).extend(saves)
    return merged, deck_id_to_canonical


def run(cfg: Config) -> Summary:
    raw_by_deck, summary = collect_saves(cfg)
    by_deck, _ = merge_saves_by_identity(raw_by_deck)
    if not by_deck:
        return summary

    try:
        names = load_card_names(cfg)
    except CardDbUnavailable as exc:
        summary.warnings.append(str(exc))
        return summary

    changelogs_dir = cfg.archive_dir / "reports" / "changelogs"
    changelogs_dir.mkdir(parents=True, exist_ok=True)

    deck_names = {canonical: saves[-1].name for canonical, saves in by_deck.items()}
    slugs, collision_warnings = assign_slugs(deck_names)
    summary.warnings.extend(collision_warnings)

    for canonical, saves in by_deck.items():
        dest = changelogs_dir / f"{slugs[canonical]}.md"
        dest.write_text(render_deck_changelog(saves, names), encoding="utf-8")
        summary.decks_written.append(dest.name)

    return summary
