"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from collector.config import ConfigError, load

from . import __version__, bracket_stats, card_stats, deck_changelog, reports


def cmd_deck_changelog(cfg, args: argparse.Namespace) -> int:
    summary = deck_changelog.run(cfg)
    print(f"scanned {summary.sessions_scanned} session(s), "
          f"found {summary.saves_found} deck save(s)")
    print(f"wrote {len(summary.decks_written)} changelog(s) to "
          f"{cfg.archive_dir / 'reports' / 'changelogs'}")
    for name in sorted(summary.decks_written):
        print(f"  {name}")
    for warning in summary.warnings:
        print(f"  ! {warning}")
    return 1 if summary.warnings and not summary.decks_written else 0


def cmd_card_stats(cfg, args: argparse.Namespace) -> int:
    summary = card_stats.run(cfg)
    print(f"scanned {summary.sessions_scanned} session(s), "
          f"found {summary.matches_found} match(es)")
    print(f"wrote {len(summary.decks_written)} report(s) to "
          f"{cfg.archive_dir / 'reports' / 'card_stats'}")
    for name in sorted(summary.decks_written):
        print(f"  {name}")
    for warning in summary.warnings:
        print(f"  ! {warning}")
    return 1 if summary.warnings and not summary.decks_written else 0


def cmd_bracket_stats(cfg, args: argparse.Namespace) -> int:
    summary = bracket_stats.run(cfg)
    print(f"scanned {summary.sessions_scanned} session(s), "
          f"found {summary.matches_found} match(es)")
    print(f"wrote {len(summary.files_written)} report(s) to "
          f"{cfg.archive_dir / 'reports' / 'bracket_stats'}")
    for name in sorted(summary.files_written):
        print(f"  {name}")
    for warning in summary.warnings:
        print(f"  ! {warning}")
    return 1 if summary.warnings and not summary.files_written else 0


def cmd_reports(cfg, args: argparse.Namespace) -> int:
    summary = reports.run(cfg)
    print(f"scanned {summary.sessions_scanned} session(s), "
          f"found {summary.matches_found} match(es)")
    print(f"wrote {len(summary.summaries_written)} format summary report(s) and "
          f"{len(summary.decks_written)} per-deck period report(s) to "
          f"{cfg.archive_dir / 'reports'}")
    for name in sorted(summary.summaries_written) + sorted(summary.decks_written):
        print(f"  {name}")
    for warning in summary.warnings:
        print(f"  ! {warning}")
    return 1 if summary.warnings and not summary.summaries_written and not summary.decks_written else 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", type=Path, default=argparse.SUPPRESS, help="path to config.toml"
    )
    common.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="debug logging",
    )

    parser = argparse.ArgumentParser(
        prog="analysis",
        description="Reports over the collector's archive.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    changelog_parser = sub.add_parser(
        "deck-changelog",
        help="regenerate per-deck Markdown changelogs",
        parents=[common],
    )
    changelog_parser.set_defaults(func=cmd_deck_changelog)

    card_stats_parser = sub.add_parser(
        "card-stats",
        help="regenerate per-deck personal card-performance reports",
        parents=[common],
    )
    card_stats_parser.set_defaults(func=cmd_card_stats)

    bracket_stats_parser = sub.add_parser(
        "bracket-stats",
        help="regenerate the Brawl bracket-signal report (best-effort, no ground-truth data)",
        parents=[common],
    )
    bracket_stats_parser.set_defaults(func=cmd_bracket_stats)

    reports_parser = sub.add_parser(
        "reports",
        help="regenerate monthly/seasonal/annual format & per-deck rollups",
        parents=[common],
    )
    reports_parser.set_defaults(func=cmd_reports)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.config = getattr(args, "config", None)
    args.verbose = getattr(args, "verbose", False)

    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    cfg.ensure_dirs()

    return args.func(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
