"""Writing captures into the immutable archive.

The live session is buffered to an uncompressed `.part` file and only
compressed on finalize. Appending to a single gzip stream would mean a crash
mid-write corrupts the tail — precisely the data we most want to keep — so the
raw buffer is the source of truth until a session is known to be over.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .config import Config
from .session import HEAD_SIZE, Header, parse_header, _LOG_TIMESTAMP_RE
from .state import Archived

CHUNK = 1 << 20
# Overlap carried between chunks so a timestamp straddling a read boundary is
# still matched.
_OVERLAP = 64


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def utc_offset() -> str:
    offset = datetime.now().astimezone().strftime("%z")
    return f"{offset[:3]}:{offset[3:]}" if offset else ""


class PartWriter:
    """Append-only buffer for the session currently being captured."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "ab")

    @property
    def size(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def append(self, data: bytes) -> None:
        self._handle.write(data)

    def flush(self) -> None:
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._handle.close()


def _write_gz_atomic(src: Path, dest: Path) -> tuple[str, int, str | None, str | None]:
    """Compress src to dest in one pass, collecting hash and timestamp span.

    Returns (sha256, raw_byte_count, first_timestamp, last_timestamp).
    """
    digest = hashlib.sha256()
    total = 0
    first: str | None = None
    last: str | None = None
    carry = b""

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with open(src, "rb") as reader, open(tmp, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as out:
            while block := reader.read(CHUNK):
                digest.update(block)
                total += len(block)
                out.write(block)

                window = carry + block
                matches = _LOG_TIMESTAMP_RE.findall(window)
                if matches:
                    if first is None:
                        first = matches[0].decode("utf-8", "replace")
                    last = matches[-1].decode("utf-8", "replace")
                carry = window[-_OVERLAP:]
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(tmp, dest)
    return digest.hexdigest(), total, first, last


def _destination(cfg: Config, header: Header, raw_path: Path, sha256: str) -> Path:
    """Date-partitioned, content-addressed archive path."""
    when = header.startup_at or datetime.fromtimestamp(raw_path.stat().st_mtime)
    session_id = header.session_id or when.strftime("%Y%m%dT%H%M%S")
    directory = cfg.sessions_dir / f"{when:%Y}" / f"{when:%m}" / f"{when:%d}"
    return directory / f"session-{session_id}-{sha256[:8]}.log.gz"


def archive_raw_file(
    cfg: Config,
    raw_path: Path,
    *,
    source: str,
    complete: bool,
    started_at: str,
    origin: str,
) -> Archived:
    """Compress a raw capture into the archive and write its metadata sidecar.

    `origin` distinguishes a live tail from a Player-prev.log recovery.
    `complete` records whether the session was captured from its very first
    byte through to a clean rotation.
    """
    with open(raw_path, "rb") as handle:
        header = parse_header(handle.read(HEAD_SIZE))

    # The final name is content-addressed, but the hash is only known once the
    # file has been read. Compress to a staging path, then rename into place.
    staging = cfg.sessions_dir / f".staging-{os.getpid()}.log.gz"
    sha256, total, first_ts, last_ts = _write_gz_atomic(raw_path, staging)

    dest = _destination(cfg, header, raw_path, sha256)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, dest)

    session_id = header.session_id or dest.name.split("-")[1]
    record = Archived(
        session_id=session_id,
        sha256=sha256,
        bytes=total,
        path=dest.relative_to(cfg.archive_dir).as_posix(),
        complete=complete,
        archived_at=now_iso(),
    )

    meta = {
        **asdict(record),
        "startup_timestamp": header.startup_raw,
        "detailed_logs": header.detailed_logs,
        "mtga_data_dir": header.mtga_data_dir,
        "source_path": source,
        "origin": origin,
        "capture_started_at": started_at,
        "capture_finished_at": record.archived_at,
        # The log's wall-clock timestamps carry no timezone. Record the
        # collector's offset now or lose the ability to normalize later.
        "collector_utc_offset": utc_offset(),
        "first_log_timestamp": first_ts,
        "last_log_timestamp": last_ts,
        "compressed_bytes": dest.stat().st_size,
    }
    meta_path = dest.with_name(dest.name[: -len(".log.gz")] + ".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return record


def gzip_file_to(src: Path, dest: Path) -> None:
    """Compress a file into the archive atomically."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with open(src, "rb") as handle, open(tmp, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as out:
            shutil.copyfileobj(handle, out, CHUNK)
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(tmp, dest)
