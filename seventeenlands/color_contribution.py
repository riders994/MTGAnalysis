"""Decomposes 17lands' per-color-combination win rates into two effects:
how much each individual color contributes to win rate at a given deck
color-count, and how much the color-count itself (2-color vs. 3-color,
etc.) contributes to win rate.

Pulls from the same API that backs https://www.17lands.com/deck_color_data.

Usage:
    python -m seventeenlands.color_contribution --expansion HOB
    python -m seventeenlands.color_contribution --expansion HOB --format TradDraft
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

API_URL = "https://www.17lands.com/color_ratings/data"
COLORS = "WUBRG"

# Below 4 colors, decomposition is over-determined (more combos than the 5
# unknowns), so the least-squares fit averages noise out. At 4 colors it's
# exactly 5 equations for 5 unknowns with no slack at all — one thin combo
# (seen for real: 3 games at HOB) can send every color's estimate to
# implausible extremes. Requiring a minimum sample per combo keeps that from
# passing itself off as a real effect.
MIN_GAMES_PER_COMBO = 20


@dataclass
class ComboRow:
    colors: str  # e.g. "WU", always ordered WUBRG
    count: int
    wins: int
    games: int

    @property
    def win_rate(self) -> float:
        return self.wins / self.games if self.games else 0.0


@dataclass
class CountSummary:
    count: int
    wins: int
    games: int

    @property
    def win_rate(self) -> float:
        return self.wins / self.games if self.games else 0.0


def fetch_color_ratings(
    expansion: str,
    event_type: str = "PremierDraft",
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """Raw rows from 17lands' color_ratings API (no start/end date = all
    time). Always requests combine_splash=true: it folds a splash color
    into the deck's true color count instead of 17lands' default of
    tracking "+Splash" as its own bucket under the lower count — the
    combined buckets are what make comparing across color counts below
    meaningful."""
    params = {"expansion": expansion, "event_type": event_type, "combine_splash": "true"}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "MTGAnalysis/color_contribution"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if isinstance(payload, dict) and "errors" in payload:
        raise ValueError(f"17lands rejected the request: {payload['errors']}")
    return payload


def parse_rows(raw_rows: list[dict]) -> tuple[dict[int, CountSummary], dict[int, list[ComboRow]]]:
    """Splits the API's flat row list into per-count summaries (17lands'
    own "Two-color" etc. totals) and the individual combo rows under each.
    Skips the "All Decks" grand total and any colorless/unparsed rows."""
    summaries: dict[int, CountSummary] = {}
    combos: dict[int, list[ComboRow]] = {}
    for row in raw_rows:
        short_name = row["short_name"]
        if row["is_summary"]:
            if isinstance(short_name, int):
                summaries[short_name] = CountSummary(short_name, row["wins"], row["games"])
            continue
        if not isinstance(short_name, str) or not short_name:
            continue
        count = len(short_name)
        combos.setdefault(count, []).append(ComboRow(short_name, count, row["wins"], row["games"]))
    return summaries, combos


def _weighted_least_squares(rows: list[tuple[list[float], float, float]]) -> list[float]:
    """Solves for b minimizing sum(w * (y - x.b)**2) via the normal
    equations (X^T W X) b = X^T W y, using Gaussian elimination with
    partial pivoting. rows are (x_vector, y, weight) triples."""
    n = len(rows[0][0])
    ata = [[0.0] * n for _ in range(n)]
    aty = [0.0] * n
    for x, y, w in rows:
        for i in range(n):
            aty[i] += w * x[i] * y
            for j in range(n):
                ata[i][j] += w * x[i] * x[j]
    return _gaussian_solve(ata, aty)


def _gaussian_solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    augmented = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(augmented[r][col]))
        if abs(augmented[pivot_row][col]) < 1e-9:
            raise ValueError("singular system")
        augmented[col], augmented[pivot_row] = augmented[pivot_row], augmented[col]
        pivot = augmented[col][col]
        for r in range(col + 1, n):
            factor = augmented[r][col] / pivot
            for c in range(col, n + 1):
                augmented[r][c] -= factor * augmented[col][c]
    x = [0.0] * n
    for row in range(n - 1, -1, -1):
        total = augmented[row][n] - sum(augmented[row][c] * x[c] for c in range(row + 1, n))
        x[row] = total / augmented[row][row]
    return x


def color_effects_by_count(combos: dict[int, list[ComboRow]]) -> dict[int, dict[str, float]]:
    """For each color count with enough distinct combinations to solve for
    5 unknowns, fits b_c such that count * win_rate(combo) ~ sum(b_c for c
    in combo), weighted by games. That's equivalent to modeling each
    combo's win rate as the average of its colors' individual per-slot
    contributions — solvable here because within a fixed count, every
    color combination 17lands reports (all C(5,count) of them) appears, so
    the design is fully determined. b_c is re-centered on the count's own
    mean so results read as "+/- N points vs. an average color at this
    count," not an absolute rate."""
    effects: dict[int, dict[str, float]] = {}
    for count, rows in combos.items():
        if len(rows) < len(COLORS) or any(row.games < MIN_GAMES_PER_COMBO for row in rows):
            continue  # not enough distinct combos, or too thin a sample, to isolate 5 color effects
        wls_rows = [
            ([1.0 if c in combo.colors else 0.0 for c in COLORS], count * combo.win_rate, float(combo.games))
            for combo in rows
        ]
        try:
            b = _weighted_least_squares(wls_rows)
        except ValueError:
            continue
        mean_b = sum(b) / len(b)
        effects[count] = {c: b[i] - mean_b for i, c in enumerate(COLORS)}
    return effects


def count_trend(summaries: dict[int, CountSummary]) -> tuple[float, float] | None:
    """Weighted linear regression of win rate on color count (weighted by
    games): returns (intercept, slope), or None with fewer than 2 distinct
    counts to fit against. Slope is win-rate points per additional color,
    averaged across the whole range — the per-count table is what actually
    shows the shape, which in practice is not linear (2-color decks
    typically peak above both mono-color and heavier splashes)."""
    if len(summaries) < 2:
        return None
    rows = [([1.0, float(count)], s.win_rate, float(s.games)) for count, s in summaries.items()]
    intercept, slope = _weighted_least_squares(rows)
    return intercept, slope


def render_report(
    expansion: str,
    event_type: str,
    summaries: dict[int, CountSummary],
    combos: dict[int, list[ComboRow]],
    effects: dict[int, dict[str, float]],
    trend: tuple[float, float] | None,
) -> str:
    lines = [f"# {expansion} {event_type} — color contribution to win rate", ""]

    lines.append("## Color count vs. win rate")
    lines.append("")
    lines.append("| Colors | Games | Win rate |")
    lines.append("|---|---|---|")
    for count in sorted(summaries):
        s = summaries[count]
        lines.append(f"| {count} | {s.games:,} | {s.win_rate:.1%} |")
    lines.append("")
    if trend is not None:
        intercept, slope = trend
        lines.append(
            f"Weighted trend: {slope * 100:+.2f} win-rate points per additional color "
            f"(intercept {intercept:.1%}) — see the table above for the actual, non-linear shape."
        )
        lines.append("")

    lines.append("## Per-color contribution at each color count")
    lines.append("")
    for count in sorted(effects):
        lines.append(f"### {count}-color decks")
        lines.append("")
        lines.append("| Color | Contribution (pts vs. an average color at this count) |")
        lines.append("|---|---|")
        for color, value in sorted(effects[count].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {color} | {value * 100:+.2f} |")
        lines.append("")

    skipped = sorted(set(combos) - set(effects))
    if skipped:
        lines.append(
            f"(Skipped color-effect decomposition for count(s) {', '.join(map(str, skipped))} "
            "— too few distinct color combinations, or too small a sample on at least one of "
            "them, to solve for 5 independent color effects.)"
        )
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--expansion", required=True, help="17lands set code, e.g. HOB")
    parser.add_argument(
        "--format", dest="event_type", default="PremierDraft",
        help="17lands event type: PremierDraft, TradDraft, QuickDraft, ... (default: PremierDraft)",
    )
    parser.add_argument("--start-date", help="YYYY-MM-DD, omit for all time")
    parser.add_argument("--end-date", help="YYYY-MM-DD, omit for all time")
    parser.add_argument("--out", type=argparse.FileType("w"), default=sys.stdout)
    args = parser.parse_args(argv)

    try:
        raw_rows = fetch_color_ratings(args.expansion, args.event_type, args.start_date, args.end_date)
    except (urllib.error.URLError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    summaries, combos = parse_rows(raw_rows)
    if not summaries:
        print("error: no data returned — check the expansion code and format", file=sys.stderr)
        return 1

    effects = color_effects_by_count(combos)
    trend = count_trend(summaries)
    print(render_report(args.expansion, args.event_type, summaries, combos, effects, trend), file=args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
