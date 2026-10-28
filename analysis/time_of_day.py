"""Win rate by time of day, per format group.

The question this answers: does the opponent pool shift enough across the day
to move our win rate? Each match is bucketed into one of eight 3-hour blocks
by the wall-clock time Arena stamped on its Playing room-state line — a real
per-match time, unlike reports.py's session-start bucketing, which would lump
an evening's worth of matches into whatever hour Arena was launched.

Arena's header timestamps carry no timezone; they are the Windows machine's
local clock, which is exactly what "time of day" should mean here. A match
whose header can't be parsed falls back to its session's start time and is
counted in Summary.fallback_times.

Brawl is split into ranked and casual, the same way bracket_stats.py does,
because the two queues are matchmade from different populations — and the
opponent pool is the whole point of this report.

Per format, a chi-square test of independence (block x win/loss) says
whether the spread across blocks is more than noise. With few games per block
its expected counts drop below 5 and the test gets unreliable — the report
says so rather than hiding the p-value.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime

from collector.config import Config

from .bracket_stats import is_ranked_brawl
from .card_stats import iter_outcomes
from .deck_changelog import collect_saves, merge_saves_by_identity
from .format_groups import classify_format
from .rates import format_rate

BLOCK_HOURS = 3
BLOCKS = tuple(range(0, 24, BLOCK_HOURS))

_HEADER_RE = re.compile(r"\[UnityCrossThreadLogger\]([^\r\n\[]+?): ")
# Arena writes the timestamp in the Windows locale's format. The US form is
# the one confirmed in the archive; the others are the common alternatives.
_HEADER_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",
    "%d/%m/%Y %H:%M:%S",
    "%d.%m.%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)


@dataclass
class BlockStats:
    gp: int = 0
    gp_wins: int = 0


@dataclass
class FormatTimeStats:
    format_group: str
    blocks: dict[int, BlockStats] = field(default_factory=lambda: {b: BlockStats() for b in BLOCKS})

    @property
    def gp(self) -> int:
        return sum(b.gp for b in self.blocks.values())

    @property
    def gp_wins(self) -> int:
        return sum(b.gp_wins for b in self.blocks.values())


@dataclass
class Summary:
    sessions_scanned: int = 0
    matches_found: int = 0
    fallback_times: int = 0
    files_written: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def report_group(format_: str | None) -> str:
    """classify_format's group, with Brawl split into ranked and casual."""
    group = classify_format(format_)
    if group == "Brawl":
        return "Brawl (ranked)" if is_ranked_brawl(format_) else "Brawl (casual)"
    return group


def match_start_time(text: str, span_start: int) -> datetime | None:
    """The wall-clock time on the log header line containing span_start (the
    match's Playing room-state event), or None if it has none we can parse."""
    line_start = text.rfind("\n", 0, span_start) + 1
    header = _HEADER_RE.match(text, line_start, span_start + 1)
    if header is None:
        return None
    raw = header.group(1).strip()
    for fmt in _HEADER_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def block_for(dt: datetime) -> int:
    return dt.hour - dt.hour % BLOCK_HOURS


def block_label(block: int) -> str:
    return f"{block:02d}:00–{block + BLOCK_HOURS:02d}:00"


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval — behaves sensibly at small n and at 0%/100%,
    where the normal approximation collapses to a zero-width interval."""
    if not n:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def chi_square_sf(x: float, df: int) -> float:
    """P(X >= x) for a chi-square distribution with integer df, in closed
    form (no scipy): a finite Poisson-style series, plus an erfc term for
    odd df."""
    if x <= 0:
        return 1.0
    half = x / 2
    if df % 2 == 0:
        term, total = 1.0, 1.0
        for i in range(1, df // 2):
            term *= half / i
            total += term
        return math.exp(-half) * total
    total = math.erfc(math.sqrt(half))
    term = math.sqrt(half) * 2 / math.sqrt(math.pi)
    for k in range(1, (df + 1) // 2):
        if k > 1:
            term *= half / (k - 0.5)
        total += math.exp(-half) * term
    return total


@dataclass(frozen=True)
class ChiSquareResult:
    statistic: float
    df: int
    p_value: float
    min_expected: float


def chi_square_independence(stats: FormatTimeStats) -> ChiSquareResult | None:
    """Block x win/loss independence test over the blocks that have games.
    None when there is nothing to compare (fewer than two populated blocks,
    or every game a win or every game a loss)."""
    populated = [b for b in stats.blocks.values() if b.gp]
    n, wins = stats.gp, stats.gp_wins
    if len(populated) < 2 or wins in (0, n):
        return None
    win_share = wins / n
    statistic = 0.0
    min_expected = math.inf
    for block in populated:
        for observed, share in ((block.gp_wins, win_share), (block.gp - block.gp_wins, 1 - win_share)):
            expected = block.gp * share
            min_expected = min(min_expected, expected)
            statistic += (observed - expected) ** 2 / expected
    df = len(populated) - 1
    return ChiSquareResult(statistic, df, chi_square_sf(statistic, df), min_expected)


def collect_time_of_day_stats(cfg: Config) -> tuple[dict[str, FormatTimeStats], Summary]:
    raw_by_deck_saves, _ = collect_saves(cfg)
    by_deck_saves, deck_id_to_canonical = merge_saves_by_identity(raw_by_deck_saves)
    deck_format = {canonical: saves[-1].format for canonical, saves in by_deck_saves.items()}

    summary = Summary()
    stats: dict[str, FormatTimeStats] = {}
    for outcome, text, session_dt in iter_outcomes(cfg, summary):
        canonical = deck_id_to_canonical.get(outcome.deck_id)
        if canonical is None or canonical not in deck_format:
            summary.warnings.append(
                f"match in session {outcome.session_id} bound to deck "
                f"{outcome.deck_id}, which has no known decklist; skipping"
            )
            continue

        dt = match_start_time(text, outcome.span[0])
        if dt is None:
            dt = session_dt
            summary.fallback_times += 1

        group = report_group(deck_format[canonical])
        block = stats.setdefault(group, FormatTimeStats(group)).blocks[block_for(dt)]
        block.gp += 1
        if outcome.won:
            block.gp_wins += 1

    return stats, summary


def render_time_of_day(stats: dict[str, FormatTimeStats], summary: Summary) -> str:
    lines = [
        "# Win Rate by Time of Day",
        "",
        "Matches bucketed by the local wall-clock time each match started, in "
        f"{BLOCK_HOURS}-hour blocks. CI is a 95% Wilson interval; Δ is the block's "
        "win rate minus the format's overall rate.",
    ]
    if summary.fallback_times:
        lines += [
            "",
            f"{summary.fallback_times} match(es) had no parseable timestamp and were "
            "bucketed by session start instead.",
        ]

    for group in sorted(stats, key=lambda g: (-stats[g].gp, g)):
        fmt = stats[group]
        overall = fmt.gp_wins / fmt.gp
        lines += ["", f"## {group}", "", f"Overall: {format_rate(fmt.gp_wins, fmt.gp)}", ""]
        lines += ["| Block | Win rate | 95% CI | Δ vs overall |", "|---|---|---|---|"]
        for block_start, block in fmt.blocks.items():
            if not block.gp:
                lines.append(f"| {block_label(block_start)} | — | | |")
                continue
            low, high = wilson_interval(block.gp_wins, block.gp)
            delta = block.gp_wins / block.gp - overall
            lines.append(
                f"| {block_label(block_start)} | {format_rate(block.gp_wins, block.gp)} "
                f"| {low:.0%}–{high:.0%} | {delta * 100:+.0f} pts |"
            )

        test = chi_square_independence(fmt)
        lines.append("")
        if test is None:
            lines.append("Chi-square: not enough variation to test.")
            continue
        verdict = "likely real" if test.p_value < 0.05 else "consistent with noise"
        lines.append(
            f"Chi-square: χ²={test.statistic:.2f}, df={test.df}, p={test.p_value:.3f} "
            f"— {verdict}."
        )
        if test.min_expected < 5:
            lines.append(
                f"Smallest expected cell count is {test.min_expected:.1f} (< 5), so treat "
                "the p-value as rough."
            )

    return "\n".join(lines) + "\n"


def run(cfg: Config) -> Summary:
    stats, summary = collect_time_of_day_stats(cfg)
    if not stats:
        return summary

    out_dir = cfg.archive_dir / "reports" / "time_of_day"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "overview.md"
    dest.write_text(render_time_of_day(stats, summary), encoding="utf-8")
    summary.files_written.append(dest.name)
    return summary
