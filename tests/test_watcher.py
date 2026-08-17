"""Rotation and resume behaviour — the cases that silently lose data."""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

from collector import state as state_mod
from collector.watcher import Watcher

from conftest import body, make_log

S1 = "8/16/2026 11:10:09 AM"
S2 = "8/16/2026 11:37:45 AM"


def new_watcher(cfg) -> Watcher:
    watcher = Watcher(cfg, state_mod.load(cfg.state_path))
    watcher.startup()
    return watcher


def archived_bytes(cfg) -> dict[str, bytes]:
    """session_id -> decompressed capture, for every archived session."""
    out = {}
    for path in sorted(cfg.sessions_dir.rglob("*.log.gz")):
        session_id = path.name.split("-")[1]
        out[session_id] = gzip.decompress(path.read_bytes())
    return out


def test_plain_append_captures_every_byte(cfg):
    data = make_log(S1)
    cfg.player_log.write_bytes(data)

    watcher = new_watcher(cfg)
    watcher.tick()
    assert watcher.state.current is not None
    assert watcher.state.current.offset == len(data)

    extra = body(S1, 50)
    with open(cfg.player_log, "ab") as handle:
        handle.write(extra)
    watcher.tick()

    assert watcher.state.current.offset == len(data) + len(extra)
    assert cfg.part_path.read_bytes() == data + extra


def test_session_identity_parsed_from_header(cfg):
    cfg.player_log.write_bytes(make_log(S1))
    watcher = new_watcher(cfg)
    watcher.tick()

    assert watcher.state.current.session_id == "20260816T111009"
    assert watcher.state.current.detailed_logs is True


def test_truncation_is_detected(cfg):
    cfg.player_log.write_bytes(make_log(S1) + body(S1, 200))
    watcher = new_watcher(cfg)
    watcher.tick()
    first_offset = watcher.state.current.offset

    # Arena restarts: a brand new, much shorter file at the same path.
    cfg.player_log.write_bytes(make_log(S2))
    watcher.tick()

    assert watcher.state.current.session_id == "20260816T113745"
    assert watcher.state.current.offset < first_offset
    assert "20260816T111009" in archived_bytes(cfg)


def test_same_size_replacement_is_detected(cfg):
    """The NTFS file-system-tunneling case.

    A file deleted and recreated at the same path within ~15s keeps its
    original creation timestamp, so creation time proves nothing. Here the
    replacement is also byte-for-byte the same *length*, defeating a size
    check too. Only the content fingerprint catches it.
    """
    first = make_log(S1, marker="A")
    cfg.player_log.write_bytes(first)
    watcher = new_watcher(cfg)
    watcher.tick()

    second = make_log(S2, marker="A")
    # Pad to exactly the same length as the first session.
    if len(second) < len(first):
        second += b"#" * (len(first) - len(second))
    else:
        second = second[: len(first)]
    assert len(second) == len(first)

    stat_before = cfg.player_log.stat()
    cfg.player_log.write_bytes(second)
    import os

    os.utime(cfg.player_log, (stat_before.st_atime, stat_before.st_mtime))

    watcher.tick()

    assert watcher.state.current.session_id == "20260816T113745"
    assert "20260816T111009" in archived_bytes(cfg)


def test_restart_mid_session_resumes_without_gap_or_duplicate(cfg):
    head = make_log(S1)
    cfg.player_log.write_bytes(head)

    watcher = new_watcher(cfg)
    watcher.tick()
    watcher.shutdown()

    # Arena keeps writing while the collector is down.
    more = body(S1, 100)
    with open(cfg.player_log, "ab") as handle:
        handle.write(more)

    resumed = new_watcher(cfg)
    resumed.tick()

    assert resumed.state.current.session_id == "20260816T111009"
    assert cfg.part_path.read_bytes() == head + more
    # Nothing archived yet: the session is still in progress.
    assert archived_bytes(cfg) == {}


def test_rotation_while_collector_down_recovers_from_prev(cfg):
    """The data-loss case that matters most."""
    session_one = make_log(S1) + body(S1, 300)
    cfg.player_log.write_bytes(session_one)

    watcher = new_watcher(cfg)
    watcher.tick()
    watcher.shutdown()

    # Collector is down. Arena exits and restarts: session one becomes prev,
    # a new session begins, and the collector never saw the transition.
    cfg.player_prev_log.write_bytes(session_one)
    cfg.player_log.write_bytes(make_log(S2))

    recovered = new_watcher(cfg)
    recovered.tick()

    captured = archived_bytes(cfg)
    assert captured["20260816T111009"] == session_one
    assert recovered.state.current.session_id == "20260816T113745"


def test_prev_supersedes_live_tail_without_duplicating(cfg):
    """Arena writes final bytes at exit that the tail never saw.

    Player-prev.log holds the complete session, and the live buffer is a
    strict prefix of it, so only prev should be archived.
    """
    partial = make_log(S1) + body(S1, 100)
    cfg.player_log.write_bytes(partial)

    watcher = new_watcher(cfg)
    watcher.tick()

    complete = partial + body(S1, 20, marker="TAIL")
    cfg.player_prev_log.write_bytes(complete)
    cfg.player_log.write_bytes(make_log(S2))
    watcher.tick()

    captured = archived_bytes(cfg)
    assert list(captured) == ["20260816T111009"]
    assert captured["20260816T111009"] == complete

    sessions = list(watcher.state.sessions.values())
    assert len(sessions) == 1
    assert sessions[0].complete


def test_prev_recovered_only_once_across_restarts(cfg):
    session_one = make_log(S1) + body(S1, 50)
    cfg.player_prev_log.write_bytes(session_one)
    cfg.player_log.write_bytes(make_log(S2))

    for _ in range(3):
        watcher = new_watcher(cfg)
        watcher.tick()
        watcher.shutdown()

    assert len(list(cfg.sessions_dir.rglob("*.log.gz"))) == 1


def test_disabled_detailed_logs_raises_a_warning(cfg):
    cfg.player_log.write_bytes(make_log(S1, detailed=False))
    watcher = new_watcher(cfg)
    watcher.tick()

    assert watcher.state.warnings
    assert "DETAILED LOGS" in watcher.state.warnings[0]


def test_metadata_records_hash_and_span(cfg):
    import json

    session_one = make_log(S1) + body(S1, 40)
    cfg.player_prev_log.write_bytes(session_one)
    watcher = new_watcher(cfg)

    meta_path = next(cfg.sessions_dir.rglob("*.meta.json"))
    meta = json.loads(meta_path.read_text())

    assert meta["sha256"] == hashlib.sha256(session_one).hexdigest()
    assert meta["bytes"] == len(session_one)
    assert meta["startup_timestamp"] == S1
    assert meta["detailed_logs"] is True
    assert meta["first_log_timestamp"] == S1
    assert meta["collector_utc_offset"]
    assert meta["origin"] == "prev"
