"""Rendering one deck's save history to Markdown."""

from __future__ import annotations

import re

from .carddb import CardNames
from .deck_events import DeckSave
from .diff import SECTION_LABELS, SECTION_ORDER, ChangeLine, diff_saves


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "deck"


def assign_slugs(names_by_deck_id: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Resolve a unique output filename slug per deck.

    Two different decks can slugify to the same name (e.g. both named "Aggro");
    the later one (by iteration order) gets its deck id appended and a warning
    is returned rather than silently overwriting the earlier file.
    """
    slugs: dict[str, str] = {}
    slug_owner: dict[str, str] = {}
    warnings: list[str] = []

    for deck_id, name in names_by_deck_id.items():
        slug = slugify(name)
        if slug_owner.get(slug, deck_id) != deck_id:
            slug = f"{slug}-{deck_id[:8]}"
            warnings.append(f"deck name '{name}' collides with another deck; writing {slug}.md")
        slug_owner[slug] = deck_id
        slugs[deck_id] = slug

    return slugs, warnings


def _line(change: ChangeLine, names: CardNames) -> tuple[str, str]:
    """Returns (sort_key, rendered_line)."""
    name = names[change.card_id]
    if change.kind == "add":
        return name, f"+ {change.new_qty} {name}"
    if change.kind == "remove":
        return name, f"- {change.old_qty} {name}"
    return name, f"~ {name} ({change.old_qty} → {change.new_qty})"


def _render_section(section: str, changes: list[ChangeLine], names: CardNames) -> str:
    lines = sorted(_line(change, names) for change in changes if change.section == section)
    if not lines:
        return ""
    body = "\n".join(line for _, line in lines)
    return f"**{SECTION_LABELS[section]}**\n\n{body}\n"


def _render_entry(save: DeckSave, changes: list[ChangeLine], names: CardNames) -> str:
    date = save.last_updated or save.last_played or "unknown date"
    version = save.version or "?"
    heading = f"### v{version} — {date} _(captured in session {save.session_id})_"
    sections = [_render_section(section, changes, names) for section in SECTION_ORDER]
    body = "\n".join(section for section in sections if section)
    return f"{heading}\n\n{body}".rstrip()


def render_deck_changelog(saves: list[DeckSave], names: CardNames) -> str:
    """`saves`: one deck's history, sorted oldest first."""
    latest = saves[-1]
    header = (
        f"# {latest.name}\n\n"
        f"- **Deck ID:** `{latest.deck_id}`\n"
        f"- **Format:** {latest.format or 'unknown'}\n\n"
        "_Regenerated in full on every run from the archived Player.log "
        "sessions — do not hand-edit._\n\n"
        "## Versions\n\n"
    )

    entries = []
    previous = None
    for save in saves:
        entries.append(_render_entry(save, diff_saves(previous, save), names))
        previous = save
    entries.reverse()  # newest first, standard changelog convention

    return header + "\n\n".join(entries) + "\n"
