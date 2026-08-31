"""Classifying Arena's many Format attribute strings into the four broad
groups the user actually reports on.

Arena has several string variants per real format family — confirmed
directly against the archive and the client's own format enum:
Standard/TraditionalStandard/ArtisanStandard/100CardStandard;
Brawl/HistoricBrawl/HistoricBrawlRanked; Alchemy/DirectGameAlchemy;
Limited-shaped queues show up as DirectGameLimited/Sealed/Draft rather than
any single "Limited" string. A keyword match handles all of these without
needing to enumerate every exact spelling Arena might use.
"""

from __future__ import annotations

FORMAT_GROUPS: dict[str, tuple[str, ...]] = {
    "Standard": ("Standard",),
    "Limited": ("Limited", "Draft", "Sealed"),
    "Brawl": ("Brawl",),
    "Alchemy": ("Alchemy",),
}


def classify_format(format_: str | None) -> str:
    """The canonical group a deck's Format attribute belongs to.

    Falls back to the raw format string itself when no keyword matches, so a
    format we haven't seen yet is never silently dropped from the annual
    "everything" report — it just gets its own group named after itself.
    """
    if format_ is None:
        return "Unknown"
    for group, keywords in FORMAT_GROUPS.items():
        if any(keyword in format_ for keyword in keywords):
            return group
    return format_
