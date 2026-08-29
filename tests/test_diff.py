"""Diffing two deck states."""

from __future__ import annotations

from analysis.deck_events import CardCount, DeckSave
from analysis.diff import ChangeLine, diff_saves


def make_save(main_deck=(), **kwargs) -> DeckSave:
    defaults = dict(
        deck_id="deck-1",
        name="Test Deck",
        format="Standard",
        version="1",
        last_updated=None,
        last_played=None,
    )
    defaults.update(kwargs)
    return DeckSave(main_deck=tuple(main_deck), **defaults)


def test_first_save_is_all_additions():
    save = make_save(main_deck=[CardCount(1, 4)], command_zone=[CardCount(99, 1)])

    changes = diff_saves(None, save)

    assert set(changes) == {
        ChangeLine("main_deck", "add", 1, None, 4),
        ChangeLine("command_zone", "add", 99, None, 1),
    }


def test_add_remove_and_quantity_change():
    old = make_save(main_deck=[CardCount(1, 2), CardCount(2, 1)])
    new = make_save(main_deck=[CardCount(1, 4), CardCount(3, 1)])

    changes = diff_saves(old, new)

    assert set(changes) == {
        ChangeLine("main_deck", "change", 1, 2, 4),
        ChangeLine("main_deck", "remove", 2, 1, None),
        ChangeLine("main_deck", "add", 3, None, 1),
    }


def test_unaffected_cards_produce_no_change_line():
    old = make_save(main_deck=[CardCount(1, 2)])
    new = make_save(main_deck=[CardCount(1, 2)])

    assert diff_saves(old, new) == []
