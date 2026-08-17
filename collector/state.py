"""Durable collector state, written atomically.

State survives restarts and is what lets the collector resume a session it was
in the middle of, recognise a session it has already archived, and avoid
re-storing an unchanged card database.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

STATE_VERSION = 1


@dataclass
class Current:
    """The session currently being captured into the .part buffer."""

    source: str
    offset: int = 0
    head_hash: str = ""
    head_len: int = 0
    session_id: str | None = None
    startup_raw: str | None = None
    detailed_logs: bool | None = None
    started_at: str = ""
    last_data_at: str = ""


@dataclass
class Archived:
    """One archived session capture."""

    session_id: str
    sha256: str
    bytes: int
    path: str
    complete: bool
    archived_at: str


@dataclass
class State:
    version: int = STATE_VERSION
    current: Current | None = None
    # content sha256 -> archived session (exact-duplicate guard)
    sessions: dict[str, Archived] = field(default_factory=dict)
    # content sha256 -> snapshot record
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    # snapshot source path -> (size, mtime) seen last, to skip cheap re-hashing
    snapshot_stats: dict[str, list[float]] = field(default_factory=dict)
    last_tick: str = ""
    warnings: list[str] = field(default_factory=list)

    def best_capture_for(self, session_id: str) -> Archived | None:
        """The largest capture already archived for a given session."""
        candidates = [s for s in self.sessions.values() if s.session_id == session_id]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.bytes)

    def has_content(self, sha256: str) -> bool:
        return sha256 in self.sessions


def load(path: Path) -> State:
    if not path.exists():
        return State()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)

        if data.get("version") != STATE_VERSION:
            return State()

        current = Current(**data["current"]) if data.get("current") else None
        sessions = {
            key: Archived(**value) for key, value in data.get("sessions", {}).items()
        }
        return State(
            version=STATE_VERSION,
            current=current,
            sessions=sessions,
            snapshots=data.get("snapshots", {}),
            snapshot_stats=data.get("snapshot_stats", {}),
            last_tick=data.get("last_tick", ""),
            warnings=data.get("warnings", []),
        )
    except (json.JSONDecodeError, OSError, TypeError, AttributeError):
        # A corrupt or drifted state file must never block capture. Starting
        # fresh costs at most a re-archived session, which content hashing and
        # the content-addressed filenames then dedupe.
        return State()


def save(path: Path, state: State) -> None:
    """Write state atomically so a crash mid-write cannot corrupt it."""
    payload = {
        "version": STATE_VERSION,
        "current": asdict(state.current) if state.current else None,
        "sessions": {key: asdict(value) for key, value in state.sessions.items()},
        "snapshots": state.snapshots,
        "snapshot_stats": state.snapshot_stats,
        "last_tick": state.last_tick,
        "warnings": state.warnings,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=".state-", suffix=".tmp",
        delete=False,
    )
    try:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, path)
    except BaseException:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise
