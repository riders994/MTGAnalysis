"""Diffing two deck states.

previous=None is treated as "every card is an addition", so callers never need
to special-case a deck's first-seen save versus a later one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .deck_events import CardCount, DeckSave

SECTION_ORDER = ("command_zone", "main_deck", "sideboard")
SECTION_LABELS = {
    "command_zone": "Commander",
    "main_deck": "Main Deck",
    "sideboard": "Sideboard",
}


@dataclass(frozen=True)
class ChangeLine:
    section: str
    kind: str  # "add" | "remove" | "change"
    card_id: int
    old_qty: int | None
    new_qty: int | None


def _as_map(cards: tuple[CardCount, ...]) -> dict[int, int]:
    return {card.card_id: card.quantity for card in cards}


def diff_section(
    old: tuple[CardCount, ...], new: tuple[CardCount, ...], section: str
) -> list[ChangeLine]:
    old_map, new_map = _as_map(old), _as_map(new)
    changes = []
    for card_id in sorted(set(old_map) | set(new_map)):
        old_qty, new_qty = old_map.get(card_id), new_map.get(card_id)
        if old_qty == new_qty:
            continue
        if old_qty is None:
            changes.append(ChangeLine(section, "add", card_id, None, new_qty))
        elif new_qty is None:
            changes.append(ChangeLine(section, "remove", card_id, old_qty, None))
        else:
            changes.append(ChangeLine(section, "change", card_id, old_qty, new_qty))
    return changes


def diff_saves(previous: DeckSave | None, current: DeckSave) -> list[ChangeLine]:
    changes = []
    for section in SECTION_ORDER:
        new_cards = getattr(current, section)
        if previous is None:
            changes += [
                ChangeLine(section, "add", card.card_id, None, card.quantity)
                for card in new_cards
            ]
        else:
            old_cards = getattr(previous, section)
            changes += diff_section(old_cards, new_cards, section)
    return changes
