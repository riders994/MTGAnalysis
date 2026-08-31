"""Curated reference commanders for cross-checking the internal bracket-proxy
win-rate signal (bracket_stats.py). NOT fetched from any external source (no
EDHREC, no web calls) — this is the user's own MTG domain judgment, hardcoded
as a small table. Only two tiers, matching what the user actually supplied:
"High" and "Lower" (relative to each other) — this is NOT a stand-in for
paper Commander's 1-5 bracket scale, and no attempt is made to map onto it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReferenceEntry:
    tier: str  # "High" | "Lower"
    note: str | None = None


# Keyed by the card's exact Arena-localized display name, as resolved via
# carddb.CardNames — must match verbatim (including punctuation) or the
# lookup silently misses; a miss is never an error, just "not a reference
# commander," surfaced in the report as an empty Reference Tier cell.
REFERENCE_COMMANDERS: dict[str, ReferenceEntry] = {
    "Esika, God of the Tree": ReferenceEntry(
        tier="High",
        note=(
            "Powerful build-around, but supports some genuinely weak decks — "
            "the card's ceiling isn't every pilot's floor."
        ),
    ),
    "Tergrid, God of Fright": ReferenceEntry(tier="High"),
    "Niv-Mizzet, Parun": ReferenceEntry(tier="High"),
    "Niv-Mizzet, Visionary": ReferenceEntry(tier="High"),
    "Tatyova, Benthic Druid": ReferenceEntry(tier="Lower"),
    "Krenko, Mob Boss": ReferenceEntry(tier="Lower"),
}
