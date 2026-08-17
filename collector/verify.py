"""Integrity checking of the archive.

Weeks of unattended capture are only worth as much as their verifiability, so
every archived session can be re-read and re-hashed against the metadata
written when it was captured.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

CHUNK = 1 << 20

# A damaged archive must be *reported*, not raise. zlib.error is not an
# OSError, so it has to be named explicitly.
CORRUPTION = (OSError, EOFError, gzip.BadGzipFile, zlib.error)


@dataclass
class Result:
    checked: int = 0
    ok: int = 0
    problems: list[str] = field(default_factory=list)
    raw_bytes: int = 0
    compressed_bytes: int = 0


def _check_session(path: Path) -> tuple[bool, str | None, int, int]:
    """Decompress and re-hash one session, comparing against its sidecar."""
    meta_path = path.with_name(path.name[: -len(".log.gz")] + ".meta.json")
    if not meta_path.exists():
        return False, f"{path.name}: missing metadata sidecar", 0, 0

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return False, f"{path.name}: unreadable metadata ({exc})", 0, 0

    digest = hashlib.sha256()
    total = 0
    try:
        with gzip.open(path, "rb") as handle:
            while block := handle.read(CHUNK):
                digest.update(block)
                total += len(block)
    except CORRUPTION as exc:
        return False, f"{path.name}: corrupt gzip ({exc})", 0, 0

    compressed = path.stat().st_size
    if meta.get("sha256") != digest.hexdigest():
        return False, f"{path.name}: sha256 mismatch against metadata", total, compressed
    if meta.get("bytes") != total:
        return (
            False,
            f"{path.name}: byte count mismatch "
            f"(metadata {meta.get('bytes')}, actual {total})",
            total,
            compressed,
        )
    return True, None, total, compressed


def run(cfg: Config) -> Result:
    result = Result()
    if not cfg.sessions_dir.exists():
        return result

    for path in sorted(cfg.sessions_dir.rglob("*.log.gz")):
        result.checked += 1
        ok, problem, raw, compressed = _check_session(path)
        if ok:
            result.ok += 1
            result.raw_bytes += raw
            result.compressed_bytes += compressed
        else:
            result.problems.append(problem)

    for path in sorted(cfg.snapshots_dir.rglob("*.gz")):
        result.checked += 1
        try:
            with gzip.open(path, "rb") as handle:
                while handle.read(CHUNK):
                    pass
            result.ok += 1
            result.compressed_bytes += path.stat().st_size
        except CORRUPTION as exc:
            result.problems.append(f"{path.name}: corrupt gzip ({exc})")

    return result
