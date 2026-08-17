"""End-to-end replay of the real captured logs.

The strongest test available: feed the actual Player-prev.log and Player.log
through the collector the way Arena would write them, and require the archive
to come back byte-identical.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import random

from collector import state as state_mod
from collector.watcher import Watcher

from conftest import make_log

SESSION_ONE = "20260816T111009"
SESSION_TWO = "20260816T113745"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stream_into(path, data: bytes, watcher: Watcher, rng: random.Random) -> None:
    """Append data in irregular chunks, ticking between writes."""
    offset = 0
    path.write_bytes(b"")
    while offset < len(data):
        chunk = rng.randint(1, 400_000)
        with open(path, "ab") as handle:
            handle.write(data[offset : offset + chunk])
        offset += chunk
        watcher.tick()


def test_replay_real_logs_round_trips_byte_identical(cfg, real_logs):
    prev_path, current_path = real_logs
    prev_bytes = prev_path.read_bytes()
    current_bytes = current_path.read_bytes()
    rng = random.Random(1337)

    watcher = Watcher(cfg, state_mod.load(cfg.state_path))
    watcher.startup()

    # Session one, streamed in as Arena would write it.
    stream_into(cfg.player_log, prev_bytes, watcher, rng)
    assert watcher.state.current.session_id == SESSION_ONE
    assert watcher.state.current.offset == len(prev_bytes)

    # Arena restarts: session one rotates to prev, session two begins.
    cfg.player_prev_log.write_bytes(prev_bytes)
    stream_into(cfg.player_log, current_bytes, watcher, rng)
    assert watcher.state.current.session_id == SESSION_TWO

    # Arena restarts again, closing out session two.
    cfg.player_prev_log.write_bytes(current_bytes)
    cfg.player_log.write_bytes(make_log("8/17/2026 9:00:00 AM"))
    watcher.tick()

    archived = {}
    for path in cfg.sessions_dir.rglob("*.log.gz"):
        archived[path.name.split("-")[1]] = gzip.decompress(path.read_bytes())

    assert archived[SESSION_ONE] == prev_bytes
    assert archived[SESSION_TWO] == current_bytes

    # Each real session stored exactly once, despite being seen as a live
    # tail and again as Player-prev.log.
    names = [p.name for p in cfg.sessions_dir.rglob("*.log.gz")]
    assert sum(SESSION_ONE in n for n in names) == 1
    assert sum(SESSION_TWO in n for n in names) == 1


def test_replay_metadata_matches_source(cfg, real_logs):
    prev_path, _ = real_logs
    prev_bytes = prev_path.read_bytes()

    cfg.player_prev_log.write_bytes(prev_bytes)
    watcher = Watcher(cfg, state_mod.load(cfg.state_path))
    watcher.startup()

    meta = json.loads(next(cfg.sessions_dir.rglob("*.meta.json")).read_text())

    assert meta["session_id"] == SESSION_ONE
    assert meta["sha256"] == sha(prev_bytes)
    assert meta["bytes"] == len(prev_bytes)
    assert meta["startup_timestamp"] == "8/16/2026 11:10:09 AM"
    assert meta["detailed_logs"] is True
    assert meta["first_log_timestamp"] == "8/16/2026 11:10:13 AM"
    assert meta["last_log_timestamp"] == "8/16/2026 11:37:38 AM"
    assert meta["mtga_data_dir"].endswith("MTGA_Data")
    # Real logs compress hard; confirm the archive is actually smaller.
    assert meta["compressed_bytes"] < meta["bytes"] / 5
