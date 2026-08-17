"""Locating Arena's log, its card database, and candidate tracker data.

Nothing here is hardcoded into the collector's runtime behaviour — discovery
prints a config block for you to review and paste. Install paths vary (Steam
vs standalone), and we do not yet know what your tracker stores or where.
"""

from __future__ import annotations

import os
from pathlib import Path

from .session import HEAD_SIZE, parse_header

TRACKER_HINT = ("draft", "mtga", "arena", "untapped", "mtg")
TRACKER_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db3", ".json")
# Directories that are large, uninteresting, and slow to walk.
SKIP_DIRS = {"cache", "caches", "gpucache", "code cache", "logs", "crashpad",
             "node_modules", "temp", "tmp", "blob_storage", "service worker"}
MAX_WALK_DEPTH = 5


def default_log_path() -> Path:
    """Unity writes to LocalLow/<Company>/<Product>/Player.log."""
    base = os.environ.get("USERPROFILE")
    if base:
        return (
            Path(base)
            / "AppData"
            / "LocalLow"
            / "Wizards Of The Coast"
            / "MTGA"
            / "Player.log"
        )
    return Path.home() / "AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log"


def find_player_log(explicit: Path | None = None) -> Path | None:
    candidates = [explicit] if explicit else []
    candidates.append(default_log_path())
    candidates.append(Path.cwd() / "Player.log")
    for candidate in candidates:
        if candidate and candidate.exists():
            # Absolute, so the emitted config works from any working directory
            # (the Scheduled Task's is not the one you ran discover from).
            return candidate.resolve()
    return None


def mtga_data_dir(log_path: Path) -> Path | None:
    """Read the install location out of the log's own first line."""
    try:
        with open(log_path, "rb") as handle:
            header = parse_header(handle.read(HEAD_SIZE))
    except OSError:
        return None
    if not header.mtga_data_dir:
        return None
    return Path(header.mtga_data_dir.replace("\\", "/"))


def card_database_globs(data_dir: Path | None) -> list[str]:
    if data_dir is None:
        return []
    raw = data_dir / "Downloads" / "Raw"
    return [
        str(raw / "Raw_CardDatabase_*.mtga"),
        str(raw / "Raw_CardDatabase_*.db"),
    ]


def _walk(root: Path, depth: int = 0):
    if depth > MAX_WALK_DEPTH:
        return
    try:
        entries = list(root.iterdir())
    except (OSError, PermissionError):
        return
    for entry in entries:
        try:
            if entry.is_dir():
                if entry.name.lower() in SKIP_DIRS:
                    continue
                yield from _walk(entry, depth + 1)
            elif entry.is_file():
                yield entry
        except (OSError, PermissionError):
            continue


def tracker_candidates(limit: int = 25) -> list[Path]:
    """Files under AppData that plausibly hold a tracker's match history."""
    roots = [
        Path(p)
        for p in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA"))
        if p
    ]
    if not roots:
        return []

    found: list[tuple[int, Path]] = []
    for root in roots:
        for path in _walk(root):
            if path.suffix.lower() not in TRACKER_SUFFIXES:
                continue
            haystack = str(path).lower()
            if not any(hint in haystack for hint in TRACKER_HINT):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < 1024:
                continue
            found.append((size, path))

    found.sort(reverse=True)
    return [path for _, path in found[:limit]]


def _toml_list(values: list[str]) -> str:
    if not values:
        return "[]"
    body = ",\n".join(f'  "{v}"' for v in (str(x).replace("\\", "/") for x in values))
    return "[\n" + body + ",\n]"


def report(explicit_log: Path | None = None) -> str:
    """Human-readable findings plus a ready-to-paste config block."""
    lines: list[str] = []
    log_path = find_player_log(explicit_log)

    lines.append("== Arena log ==")
    if log_path:
        size = log_path.stat().st_size
        with open(log_path, "rb") as handle:
            header = parse_header(handle.read(HEAD_SIZE))
        lines.append(f"  found:            {log_path}  ({size / 1e6:.2f} MB)")
        lines.append(f"  startup:          {header.startup_raw or 'unknown'}")
        detailed = {True: "ENABLED", False: "DISABLED", None: "unknown"}[
            header.detailed_logs
        ]
        lines.append(f"  detailed logs:    {detailed}")
        if header.detailed_logs is False:
            lines.append(
                "  !! Detailed Logs is OFF. Match, deck and event data are not "
                "written at all.\n"
                "     Enable it in Arena: Settings > Account > Detailed Logs, "
                "then restart Arena."
            )
        prev = log_path.with_name("Player-prev.log")
        lines.append(
            f"  previous session: {prev}"
            + ("" if prev.exists() else "  (not present yet)")
        )
    else:
        lines.append(f"  NOT FOUND. Expected at {default_log_path()}")

    data_dir = mtga_data_dir(log_path) if log_path else None
    lines.append("")
    lines.append("== Card database ==")
    globs = card_database_globs(data_dir)
    if data_dir:
        lines.append(f"  MTGA_Data:        {data_dir}")
        matched = [p for g in globs for p in Path(g).parent.glob(Path(g).name)]
        if matched:
            for path in matched:
                lines.append(f"  found:            {path.name} ({path.stat().st_size / 1e6:.1f} MB)")
        else:
            lines.append("  no Raw_CardDatabase_* matched yet (globs still configured)")
    else:
        lines.append("  could not derive MTGA_Data from the log header")

    lines.append("")
    lines.append("== Tracker candidates ==")
    trackers = tracker_candidates()
    if trackers:
        for path in trackers:
            lines.append(f"  {path.stat().st_size / 1e6:8.2f} MB  {path}")
        lines.append("")
        lines.append("  Review these and keep only the ones that are really yours.")
    else:
        lines.append("  none found under %APPDATA% / %LOCALAPPDATA%")

    lines.append("")
    lines.append("=" * 68)
    lines.append("Paste into config.toml (edit paths as needed):")
    lines.append("=" * 68)
    lines.append("")
    lines.append("[paths]")
    lines.append(f'archive_dir = "{Path.cwd().as_posix()}/archive"')
    lines.append(
        f'player_log = "{log_path.as_posix() if log_path else default_log_path().as_posix()}"'
    )
    lines.append("")
    lines.append("[capture]")
    lines.append("poll_interval_seconds = 2.0")
    lines.append("snapshot_interval_seconds = 300")
    if globs:
        lines.append("")
        lines.append("[[snapshot]]")
        lines.append('name = "carddb"')
        lines.append(f"globs = {_toml_list(globs)}")
    if trackers:
        lines.append("")
        lines.append("[[snapshot]]")
        lines.append('name = "tracker"')
        lines.append(f"globs = {_toml_list([str(p) for p in trackers[:10]])}")
    lines.append("")
    lines.append("# [push]")
    lines.append('# host = "weezy@linuxbox"')
    lines.append('# remote_dir = "/home/weezy/mtga-archive"')

    return "\n".join(lines)
