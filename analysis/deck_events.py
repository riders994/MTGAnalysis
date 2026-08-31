"""Extracting deck-save events from one session's decompressed text.

A deck save is logged as either a DeckUpsertDeckV3 or an EventSetDeckV3 RPC
*request* (the deck-builder save and the in-event/draft deck-submit paths,
respectively — not to be confused with EventSetDeckV3's own *response*, a
differently-shaped `<== EventSetDeckV3(id)` line that match_events.py reads
for a different purpose, binding decks to matches). Both request shapes are
an outer JSON object whose "request" field is itself a JSON-encoded string
(double-encoded), which in turn holds the deck's Summary (attributes like
Version, Format, dates) and its MainDeck/Sideboard/CommandZone card lists;
EventSetDeckV3's inner object additionally carries an EventName field, which
is ignored here. Not every session contains one of these — most are plain
menu browsing — so absence is the common case, not an error.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Arena writes \r\n; the payload never itself contains a literal newline, so
# stopping at the first \r or \n safely bounds one line's JSON.
_DECK_UPSERT_RE = re.compile(r"\[UnityCrossThreadLogger\]==> DeckUpsertDeckV3 (\{[^\r\n]+\})")
_EVENT_SET_DECK_REQUEST_RE = re.compile(r"\[UnityCrossThreadLogger\]==> EventSetDeckV3 (\{[^\r\n]+\})")


@dataclass(frozen=True)
class CardCount:
    card_id: int
    quantity: int


@dataclass(frozen=True)
class DeckSave:
    deck_id: str
    name: str
    format: str | None
    version: str | None
    last_updated: str | None
    last_played: str | None
    main_deck: tuple[CardCount, ...] = field(default_factory=tuple)
    sideboard: tuple[CardCount, ...] = field(default_factory=tuple)
    command_zone: tuple[CardCount, ...] = field(default_factory=tuple)
    session_id: str = ""


def _unwrap(value: str | None) -> str | None:
    """Undo Arena's double JSON-encoding of date-valued attributes.

    LastUpdated/LastPlayed come back as the literal string '"2026-08-08T..."'
    (embedded quotes and all) rather than plain text — a second json.loads
    strips them. Left as-is if it doesn't look wrapped.
    """
    if value and value[:1] == '"' and value[-1:] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _attr(attributes: list[dict], name: str) -> str | None:
    for entry in attributes:
        if entry.get("name") == name:
            return _unwrap(entry.get("value"))
    return None


def _cards(entries: list[dict] | None) -> tuple[CardCount, ...]:
    return tuple(
        CardCount(entry["cardId"], entry["quantity"])
        for entry in entries or []
        if "cardId" in entry and "quantity" in entry
    )


def parse_deck_upsert(request_json: str) -> DeckSave | None:
    """Parse the inner (escaped) JSON string of one DeckUpsertDeckV3 request."""
    try:
        inner = json.loads(request_json)
    except json.JSONDecodeError:
        return None

    summary = inner.get("Summary") or {}
    deck = inner.get("Deck") or {}
    deck_id = summary.get("DeckId")
    if not deck_id:
        return None

    attributes = summary.get("Attributes") or []
    return DeckSave(
        deck_id=deck_id,
        name=summary.get("Name") or deck_id,
        format=_attr(attributes, "Format"),
        version=_attr(attributes, "Version"),
        last_updated=_attr(attributes, "LastUpdated"),
        last_played=_attr(attributes, "LastPlayed"),
        main_deck=_cards(deck.get("MainDeck")),
        sideboard=_cards(deck.get("Sideboard")),
        command_zone=_cards(deck.get("CommandZone")),
    )


def extract_deck_saves(log_text: str, *, session_id: str) -> list[DeckSave]:
    """Every DeckUpsertDeckV3/EventSetDeckV3 save found in one decompressed
    session, in file order (the two share a request shape, so are merged by
    match position rather than handled as two separate passes)."""
    saves = []
    matches = sorted(
        (*_DECK_UPSERT_RE.finditer(log_text), *_EVENT_SET_DECK_REQUEST_RE.finditer(log_text)),
        key=lambda match: match.start(),
    )
    for match in matches:
        try:
            outer = json.loads(match.group(1))
        except json.JSONDecodeError:
            log.debug("session %s: unparsable deck-save line, skipping", session_id)
            continue

        request_raw = outer.get("request")
        if not request_raw:
            continue

        save = parse_deck_upsert(request_raw)
        if save is not None:
            saves.append(dataclasses.replace(save, session_id=session_id))
    return saves
