"""Parsing DeckUpsertDeckV3/EventSetDeckV3 save events out of a decompressed
session."""

from __future__ import annotations

from analysis.deck_events import CardCount, extract_deck_saves

from conftest import deck_upsert_line, event_set_deck_request_line

DECK_ID = "3474397f-2e4c-49fa-99fb-bb111e960f29"


def test_parses_a_single_deck_save():
    line = deck_upsert_line(
        DECK_ID,
        "Lagaan",
        version="11",
        format="HistoricBrawl",
        last_updated="2026-08-08T02:35:17.4706732-04:00",
        main_deck=[(75022, 5), (75021, 7)],
        command_zone=[(96080, 1)],
    )

    saves = extract_deck_saves(line, session_id="20260817T192832")

    assert len(saves) == 1
    save = saves[0]
    assert save.deck_id == DECK_ID
    assert save.name == "Lagaan"
    assert save.version == "11"
    assert save.format == "HistoricBrawl"
    # Unwrapped: no stray quote characters from Arena's double-encoding.
    assert save.last_updated == "2026-08-08T02:35:17.4706732-04:00"
    assert save.main_deck == (CardCount(75022, 5), CardCount(75021, 7))
    assert save.command_zone[0].card_id == 96080
    assert save.session_id == "20260817T192832"


def test_multiple_saves_in_one_file_all_parse():
    text = (
        deck_upsert_line("deck-a", "Deck A", version="1")
        + "some unrelated log line\r\n"
        + deck_upsert_line("deck-b", "Deck B", version="1")
    )

    saves = extract_deck_saves(text, session_id="s1")

    assert [save.deck_id for save in saves] == ["deck-a", "deck-b"]


def test_no_deck_event_returns_empty_list():
    text = "[UnityCrossThreadLogger]GraphGetGraphState {}\r\nsome other line\r\n"

    assert extract_deck_saves(text, session_id="s1") == []


def test_malformed_payload_is_skipped_not_raised():
    text = (
        "[UnityCrossThreadLogger]==> DeckUpsertDeckV3 {not valid json}\r\n"
        + deck_upsert_line("deck-a", "Deck A", version="1")
    )

    saves = extract_deck_saves(text, session_id="s1")

    assert [save.deck_id for save in saves] == ["deck-a"]


def test_parses_an_event_set_deck_v3_save():
    line = event_set_deck_request_line(
        "c061027c-3033-4ac4-90dc-b28cb399bda5",
        "Draft Deck",
        event_name="QuickDraft_HOB_20260820",
        version="11",
        format="Draft",
        main_deck=[(103443, 1), (79737, 7)],
        sideboard=[(103545, 2)],
    )

    saves = extract_deck_saves(line, session_id="20260825T003744")

    assert len(saves) == 1
    save = saves[0]
    assert save.deck_id == "c061027c-3033-4ac4-90dc-b28cb399bda5"
    assert save.name == "Draft Deck"
    assert save.version == "11"
    assert save.format == "Draft"
    assert save.main_deck == (CardCount(103443, 1), CardCount(79737, 7))
    assert save.sideboard == (CardCount(103545, 2),)


def test_deck_upsert_and_event_set_deck_saves_merge_in_file_order():
    text = (
        deck_upsert_line("deck-a", "Deck A", version="1")
        + event_set_deck_request_line("deck-b", "Deck B", version="1")
        + deck_upsert_line("deck-a", "Deck A", version="2")
    )

    saves = extract_deck_saves(text, session_id="s1")

    assert [(save.deck_id, save.version) for save in saves] == [
        ("deck-a", "1"),
        ("deck-b", "1"),
        ("deck-a", "2"),
    ]


def test_malformed_event_set_deck_payload_is_skipped_not_raised():
    text = (
        "[UnityCrossThreadLogger]==> EventSetDeckV3 {not valid json}\r\n"
        + event_set_deck_request_line("deck-a", "Deck A", version="1")
    )

    saves = extract_deck_saves(text, session_id="s1")

    assert [save.deck_id for save in saves] == ["deck-a"]
