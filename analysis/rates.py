"""Shared win-rate formatting, used by every report module."""

from __future__ import annotations


def format_rate(wins: int, n: int) -> str:
    if not n:
        return "—"
    return f"{wins / n:.0%} ({wins}/{n})"


def format_avg(total: int, n: int) -> str:
    if not n:
        return "—"
    return f"{total / n:.2f} ({total}/{n})"
