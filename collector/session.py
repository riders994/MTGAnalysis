"""Parsing the small amount of a Player.log we need to identify a session.

This is deliberately the *only* place the collector looks inside the log. We
read the header to answer three questions — when did this session start, is it
the same session we saw last tick, and is Detailed Logs on — and otherwise treat
the file as opaque bytes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Bytes of header we read to identify a session. The Startup Timestamp line
# lands around 1 KB into the file, so this is comfortably enough.
HEAD_SIZE = 16384

# The prefix length used for the rotation fingerprint. A file only ever grows,
# so its first N bytes are immutable and safe to compare across ticks.
FINGERPRINT_SIZE = 8192

_STARTUP_RE = re.compile(rb"Startup Timestamp:\s*([^\r\n]+)")
_DETAILED_RE = re.compile(rb"DETAILED LOGS:\s*(ENABLED|DISABLED)")
_MONO_PATH_RE = re.compile(rb"Mono path\[0\]\s*=\s*'([^']+)'")
_LOG_TIMESTAMP_RE = re.compile(
    rb"\[UnityCrossThreadLogger\](\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2}(?: [AP]M)?)"
)

# Arena writes timestamps in the machine's locale. Cover the common shapes.
_TIMESTAMP_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %H:%M:%S",
    "%d/%m/%Y %I:%M:%S %p",
    "%Y-%m-%d %H:%M:%S",
)


def parse_timestamp(raw: str) -> datetime | None:
    """Parse an Arena log timestamp, tolerating locale variation."""
    text = raw.strip()
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


@dataclass
class Header:
    """What the first few KB of a Player.log tell us about the session."""

    startup_raw: str | None = None
    startup_at: datetime | None = None
    detailed_logs: bool | None = None
    mtga_data_dir: str | None = None

    @property
    def session_id(self) -> str | None:
        """A stable, sortable identifier derived from the startup timestamp."""
        if self.startup_at is None:
            return None
        return self.startup_at.strftime("%Y%m%dT%H%M%S")


def parse_header(head: bytes) -> Header:
    """Extract session identity from the head of a Player.log."""
    header = Header()

    match = _STARTUP_RE.search(head)
    if match:
        header.startup_raw = match.group(1).decode("utf-8", "replace").strip()
        header.startup_at = parse_timestamp(header.startup_raw)

    match = _DETAILED_RE.search(head)
    if match:
        header.detailed_logs = match.group(1) == b"ENABLED"

    match = _MONO_PATH_RE.search(head)
    if match:
        mono_path = match.group(1).decode("utf-8", "replace")
        # '.../MTGA_Data/Managed' -> '.../MTGA_Data'
        marker = "MTGA_Data"
        index = mono_path.find(marker)
        if index != -1:
            header.mtga_data_dir = mono_path[: index + len(marker)]

    return header


def read_head(path: Path, size: int = HEAD_SIZE) -> bytes:
    """Read the leading bytes of a file, tolerating a shorter file."""
    with open(path, "rb") as handle:
        return handle.read(size)


def fingerprint(data: bytes) -> str:
    """Hash a byte prefix for identity comparison."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def log_timestamp_span(data: bytes) -> tuple[str | None, str | None]:
    """First and last wall-clock timestamps appearing in a captured session.

    Recorded in metadata so a session's real time span is known without
    decompressing and parsing the whole capture later.
    """
    matches = _LOG_TIMESTAMP_RE.findall(data)
    if not matches:
        return None, None
    return (
        matches[0].decode("utf-8", "replace"),
        matches[-1].decode("utf-8", "replace"),
    )
