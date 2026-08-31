"""Chronological iteration over archived sessions.

Sessions are the only thing this package reads from the archive itself; card
database lookups live in carddb.py.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from collector.config import Config


@dataclass(frozen=True)
class SessionFile:
    path: Path
    session_id: str


def session_datetime(session: SessionFile) -> datetime:
    """When this session started, parsed from its session_id (YYYYMMDDTHHMMSS)."""
    return datetime.strptime(session.session_id, "%Y%m%dT%H%M%S")


def iter_sessions(cfg: Config) -> list[SessionFile]:
    """Every archived session, oldest first.

    Filenames sort chronologically (session-YYYYMMDDTHHMMSS-<hash>.log.gz),
    the same ordering collector.verify already relies on.
    """
    if not cfg.sessions_dir.exists():
        return []
    sessions = []
    for path in sorted(cfg.sessions_dir.rglob("*.log.gz")):
        session_id = path.name.split("-")[1]
        sessions.append(SessionFile(path=path, session_id=session_id))
    return sessions


def read_text(session: SessionFile) -> str:
    """Decompress one session to text, tolerating undecodable bytes."""
    with gzip.open(session.path, "rb") as handle:
        return handle.read().decode("utf-8", "replace")
