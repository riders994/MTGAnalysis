"""Win rate by time of day: per-match timestamps, blocking, and the stats."""

from __future__ import annotations

import gzip
from datetime import datetime

import pytest

from analysis import time_of_day
from analysis.time_of_day import (
    BlockStats,
    FormatTimeStats,
    block_for,
    chi_square_independence,
    chi_square_sf,
    match_start_time,
    wilson_interval,
)

from conftest import (
    deck_upsert_line,
    event_set_deck_response_line,
    match_room_state_completed_line,
    match_room_state_playing_line,
)

PLAYERS = [("OURCLIENTID", 1, 1), ("OPPONENTID", 2, 2)]


def _match(match_id: str, deck_id: str, *, won: bool, started: str) -> str:
    return (
        event_set_deck_response_line(f"course-{match_id}", deck_id)
        + match_room_state_playing_line(match_id, PLAYERS, timestamp=started)
        + match_room_state_completed_line(match_id, winning_team_id=1 if won else 2)
    )


def _write_session(cfg, session_id: str, text: str) -> None:
    dest = cfg.sessions_dir / f"session-{session_id}-aaaaaaaa.log.gz"
    dest.write_bytes(gzip.compress(text.encode("utf-8")))


def test_match_start_time_reads_the_playing_lines_own_header():
    text = match_room_state_playing_line("m", PLAYERS, timestamp="8/17/2026 11:05:00 PM")
    offset = text.index("MatchGameRoomStateChangedEvent")
    assert match_start_time(text, offset) == datetime(2026, 8, 17, 23, 5, 0)


def test_match_start_time_is_none_without_a_parseable_header():
    text = match_room_state_playing_line("m", PLAYERS, timestamp="t")
    assert match_start_time(text, text.index("MatchGameRoomStateChangedEvent")) is None


@pytest.mark.parametrize(
    ("hour", "block"), [(0, 0), (2, 0), (3, 3), (11, 9), (12, 12), (23, 21)]
)
def test_block_for(hour, block):
    assert block_for(datetime(2026, 8, 17, hour, 30)) == block


def test_wilson_interval_stays_wide_at_extremes():
    low, high = wilson_interval(3, 3)
    assert high == 1.0 and 0.4 < low < 0.5


@pytest.mark.parametrize(
    ("x", "df", "expected"),
    [(3.841, 1, 0.05), (5.991, 2, 0.05), (7.815, 3, 0.05), (14.067, 7, 0.05), (2.0, 5, 0.8491)],
)
def test_chi_square_sf_matches_reference_values(x, df, expected):
    assert chi_square_sf(x, df) == pytest.approx(expected, abs=5e-4)


def test_chi_square_independence_needs_variation():
    stats = FormatTimeStats("Standard")
    stats.blocks[0] = BlockStats(gp=4, gp_wins=4)
    stats.blocks[12] = BlockStats(gp=4, gp_wins=4)
    assert chi_square_independence(stats) is None


def test_chi_square_independence_flags_a_lopsided_split():
    stats = FormatTimeStats("Standard")
    stats.blocks[9] = BlockStats(gp=40, gp_wins=32)
    stats.blocks[21] = BlockStats(gp=40, gp_wins=12)
    result = chi_square_independence(stats)
    assert result.df == 1
    assert result.p_value < 0.001


def test_run_buckets_matches_by_their_own_time_not_the_session_start(cfg):
    # One session started in the morning, but the second match is late at
    # night — it must land in the 21:00 block, not the session's 09:00 one.
    text = (
        deck_upsert_line("deck-std", "Mono Red", format="Standard")
        + deck_upsert_line("deck-brawl", "Brawler", format="HistoricBrawl")
        + deck_upsert_line("deck-ranked", "Ladder", format="HistoricBrawlRanked")
        + _match("m1", "deck-std", won=True, started="8/17/2026 9:15:00 AM")
        + _match("m2", "deck-std", won=False, started="8/17/2026 10:40:00 PM")
        + _match("m3", "deck-brawl", won=True, started="8/17/2026 1:00:00 PM")
        + _match("m4", "deck-ranked", won=False, started="8/17/2026 1:30:00 PM")
    )
    _write_session(cfg, "20260817T090000", text)

    stats, summary = time_of_day.collect_time_of_day_stats(cfg)
    assert summary.fallback_times == 0
    assert stats["Standard"].blocks[9] == BlockStats(gp=1, gp_wins=1)
    assert stats["Standard"].blocks[21] == BlockStats(gp=1, gp_wins=0)
    assert stats["Brawl (casual)"].blocks[12] == BlockStats(gp=1, gp_wins=1)
    assert stats["Brawl (ranked)"].blocks[12] == BlockStats(gp=1, gp_wins=0)

    summary = time_of_day.run(cfg)
    report = (cfg.archive_dir / "reports" / "time_of_day" / "overview.md").read_text()
    assert summary.files_written == ["overview.md"]
    assert "## Standard" in report
    assert "## Brawl (casual)" in report and "## Brawl (ranked)" in report
    assert "| 21:00–24:00 | 0% (0/1) |" in report


def test_run_falls_back_to_session_start_when_the_header_is_unparseable(cfg):
    text = deck_upsert_line("deck-std", "Mono Red") + _match("m1", "deck-std", won=True, started="t")
    _write_session(cfg, "20260817T150000", text)

    stats, summary = time_of_day.collect_time_of_day_stats(cfg)
    assert summary.fallback_times == 1
    assert stats["Standard"].blocks[15].gp == 1
