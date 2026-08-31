"""Classifying Arena's many Format attribute strings into report groups."""

from __future__ import annotations

import pytest

from analysis.format_groups import classify_format

REAL_FORMAT_STRINGS = [
    ("Standard", "Standard"),
    ("TraditionalStandard", "Standard"),
    ("ArtisanStandard", "Standard"),
    ("100CardStandard", "Standard"),
    ("Brawl", "Brawl"),
    ("HistoricBrawl", "Brawl"),
    ("HistoricBrawlRanked", "Brawl"),
    ("Alchemy", "Alchemy"),
    ("DirectGameAlchemy", "Alchemy"),
    ("DirectGameLimited", "Limited"),
    ("Sealed", "Limited"),
    ("Draft", "Limited"),
]


@pytest.mark.parametrize("format_, expected_group", REAL_FORMAT_STRINGS)
def test_classify_format_against_real_archive_strings(format_, expected_group):
    assert classify_format(format_) == expected_group


def test_unrecognized_format_falls_back_to_itself():
    assert classify_format("Explorer") == "Explorer"


def test_none_format_is_unknown():
    assert classify_format(None) == "Unknown"
