"""Content-addressed snapshots of the card database and tracker data.

Everything here is stored by content hash, so a file that never changes is
stored exactly once no matter how long the collector runs. The card database
changes only on set updates; a tracker database changes constantly but is small.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from .archive import gzip_file_to, now_iso
from .config import Config, SnapshotSource
from .session import sha256_file
from .state import State

log = logging.getLogger(__name__)

SQLITE_MAGIC = b"SQLite format 3\x00"


def is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def _consistent_copy(src: Path, dest: Path) -> None:
    """Copy a possibly-live file to dest, consistently if it is SQLite.

    Reading a database that is being written byte-by-byte yields a torn copy
    that may not even open. The backup API walks the file under a read lock
    and produces a valid database instead.
    """
    if not is_sqlite(src):
        dest.write_bytes(src.read_bytes())
        return

    source_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5.0)
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()


def _snapshot_one(cfg: Config, state: State, source: SnapshotSource, path: Path) -> bool:
    """Snapshot a single file if its content is new. Returns True if stored."""
    key = str(path)
    try:
        stat = path.stat()
    except OSError:
        return False

    # Cheap gate: skip hashing entirely when size and mtime are unchanged.
    seen = state.snapshot_stats.get(key)
    if seen and seen[0] == stat.st_size and seen[1] == stat.st_mtime:
        return False

    # Staged under a dot-directory so a push running concurrently skips it.
    staging_root = cfg.archive_dir / ".tmp"
    staging_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=staging_root) as tmpdir:
        staged = Path(tmpdir) / path.name
        try:
            _consistent_copy(path, staged)
        except (OSError, sqlite3.Error) as exc:
            log.warning("could not snapshot %s: %s", path, exc)
            return False

        digest = sha256_file(staged)
        state.snapshot_stats[key] = [stat.st_size, stat.st_mtime]

        if digest in state.snapshots:
            return False

        dest = cfg.snapshots_dir / source.name / f"{path.name}-{digest[:12]}.gz"
        if not dest.exists():
            gzip_file_to(staged, dest)

    state.snapshots[digest] = {
        "source": source.name,
        "origin_path": key,
        "name": path.name,
        "bytes": stat.st_size,
        "path": dest.relative_to(cfg.archive_dir).as_posix(),
        "captured_at": now_iso(),
        "source_mtime": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(
            timespec="seconds"
        ),
    }
    log.info("Snapshot %s/%s (%.1f MB)", source.name, path.name, stat.st_size / 1e6)
    return True


def run(cfg: Config, state: State) -> int:
    """Snapshot every configured source. Returns the number stored."""
    stored = 0
    for source in cfg.snapshots:
        for path in source.resolve():
            try:
                if _snapshot_one(cfg, state, source, path):
                    stored += 1
            except OSError as exc:
                log.warning("snapshot failed for %s: %s", path, exc)
    return stored
