"""seventeenlands.color_contribution: parsing 17lands' color_ratings API
shape and the weighted-least-squares color/count decompositions built on
top of it."""

from __future__ import annotations

import itertools

import pytest

from seventeenlands.color_contribution import (
    COLORS,
    MIN_GAMES_PER_COMBO,
    CountSummary,
    color_effects_by_count,
    count_trend,
    parse_rows,
)

# A trimmed real response shape (is_summary rows plus the combos under
# them), matching what /color_ratings/data?combine_splash=true returns.
SAMPLE_RAW_ROWS = [
    {"is_summary": True, "color_name": "Mono-color", "short_name": 1, "wins": 6769, "games": 11905},
    {"is_summary": False, "color_name": "Mono-White", "short_name": "W", "wins": 241, "games": 435},
    {"is_summary": False, "color_name": "Mono-Blue", "short_name": "U", "wins": 2352, "games": 4306},
    {"is_summary": True, "color_name": "Two-color", "short_name": 2, "wins": 222585, "games": 399533},
    {"is_summary": False, "color_name": "Azorius (WU)", "short_name": "WU", "wins": 41360, "games": 74188},
    {"is_summary": False, "color_name": "Dimir (UB)", "short_name": "UB", "wins": 8445, "games": 15932},
    {"is_summary": True, "color_name": "Five-color", "short_name": 5, "wins": 0, "games": 1},
    {"is_summary": False, "color_name": "All Colors (WUBRG)", "short_name": "WUBRG", "wins": 0, "games": 1},
    {"is_summary": True, "color_name": "All Decks", "short_name": "All", "wins": 236139, "games": 425256},
]


def test_parse_rows_splits_summaries_from_combos_and_skips_grand_total():
    summaries, combos = parse_rows(SAMPLE_RAW_ROWS)

    assert set(summaries) == {1, 2, 5}
    assert summaries[2].wins == 222585
    assert summaries[2].games == 399533
    assert summaries[2].win_rate == 222585 / 399533

    assert {row.colors for row in combos[1]} == {"W", "U"}
    assert {row.colors for row in combos[2]} == {"WU", "UB"}
    assert combos[5][0].colors == "WUBRG"


def test_parse_rows_ignores_the_all_decks_total():
    summaries, _ = parse_rows(SAMPLE_RAW_ROWS)
    assert "All" not in summaries
    assert all(isinstance(count, int) for count in summaries)


def _synthetic_combo_rows(true_power: dict[str, float], baseline: float, count: int, games: int):
    """Every count-color combination of COLORS, with win_rate built exactly
    from an additive per-color model — used to check the WLS decomposition
    recovers a known-true set of color effects."""
    rows = []
    for combo in itertools.combinations(COLORS, count):
        colors = "".join(c for c in COLORS if c in combo)
        win_rate = baseline + sum(true_power[c] for c in combo) / count
        rows.append(
            {
                "is_summary": False,
                "color_name": colors,
                "short_name": colors,
                "wins": round(win_rate * games),
                "games": games,
            }
        )
    return rows


def test_color_effects_recovers_known_additive_color_powers():
    true_power = {"W": 0.02, "U": -0.01, "B": 0.05, "R": -0.03, "G": -0.03}
    raw_rows = _synthetic_combo_rows(true_power, baseline=0.5, count=2, games=100_000)
    _, combos = parse_rows(raw_rows)

    effects = color_effects_by_count(combos)

    mean_power = sum(true_power.values()) / len(true_power)
    for color, power in true_power.items():
        assert effects[2][color] == pytest.approx(power - mean_power, abs=1e-3)


def test_color_effects_skips_counts_below_the_minimum_sample_per_combo():
    true_power = {"W": 0.02, "U": -0.01, "B": 0.05, "R": -0.03, "G": -0.03}
    raw_rows = _synthetic_combo_rows(true_power, baseline=0.5, count=4, games=MIN_GAMES_PER_COMBO - 1)
    _, combos = parse_rows(raw_rows)

    assert color_effects_by_count(combos) == {}


def test_color_effects_skips_counts_with_too_few_distinct_combos():
    # count=5 (WUBRG) only ever has one possible combination, never five.
    raw_rows = _synthetic_combo_rows({"W": 0, "U": 0, "B": 0, "R": 0, "G": 0}, 0.5, count=5, games=10_000)
    _, combos = parse_rows(raw_rows)

    assert color_effects_by_count(combos) == {}


def test_count_trend_recovers_a_known_linear_slope():
    summaries = {
        1: CountSummary(1, wins=6000, games=10_000),  # 60%
        2: CountSummary(2, wins=5500, games=10_000),  # 55%
        3: CountSummary(3, wins=5000, games=10_000),  # 50%
    }

    intercept, slope = count_trend(summaries)

    assert slope == pytest.approx(-0.05, abs=1e-6)
    assert intercept == pytest.approx(0.65, abs=1e-6)


def test_count_trend_needs_at_least_two_distinct_counts():
    assert count_trend({1: CountSummary(1, wins=1, games=2)}) is None
