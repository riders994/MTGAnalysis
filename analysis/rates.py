"""Shared win-rate formatting, used by every report module."""

from __future__ import annotations


def format_rate(wins: int, n: int) -> str:
    if not n:
        return "—"
    return f"{wins / n:.0%} ({wins}/{n})"
