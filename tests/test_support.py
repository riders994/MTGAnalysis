"""Config loading, snapshots, push selection, verification, and discovery."""

from __future__ import annotations

import gzip
import os
import sqlite3
import textwrap
from pathlib import Path

import pytest

from collector import discover, push as push_mod, snapshot, state as state_mod, verify
from collector.config import ConfigError, SnapshotSource, load
from collector.session import parse_header
from collector.watcher import Watcher

from conftest import body, make_log

S1 = "8/16/2026 11:10:09 AM"


# ------------------------------------------------------------------- config


def write_config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        textwrap.dedent(
            f"""
            [paths]
            archive_dir = "{(tmp_path / 'archive').as_posix()}"
            player_log = "{(tmp_path / 'mtga' / 'Player.log').as_posix()}"

            [capture]
            poll_interval_seconds = 1.5
            """
        )
        + extra
    )
    return path


def test_config_defaults_prev_log_beside_player_log(tmp_path):
    cfg = load(write_config(tmp_path))
    assert cfg.player_prev_log.name == "Player-prev.log"
    assert cfg.player_prev_log.parent == cfg.player_log.parent
    assert cfg.poll_interval == 1.5


def test_config_missing_file_explains_how_to_make_one(tmp_path):
    with pytest.raises(ConfigError, match="discover"):
        load(tmp_path / "nope.toml")


def test_config_without_player_log_is_valid_for_a_storage_host(tmp_path):
    """The Pi holds and reports on the archive; there is no Arena to watch."""
    path = tmp_path / "config.toml"
    path.write_text(f'[paths]\narchive_dir = "{(tmp_path / "arch").as_posix()}"\n')

    cfg = load(path)
    assert cfg.player_log is None
    assert cfg.player_prev_log is None
    assert cfg.archive_dir.name == "arch"


def test_config_requires_archive_dir(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[paths]\nplayer_log = "/tmp/Player.log"\n')
    with pytest.raises(ConfigError, match="archive_dir"):
        load(path)


def test_push_config_parsed(tmp_path):
    cfg = load(
        write_config(
            tmp_path,
            '\n[push]\nhost = "me@box"\nremote_dir = "/srv/arch"\nssh_port = 2222\n',
        )
    )
    assert cfg.push.host == "me@box"
    assert cfg.push.ssh_port == 2222


# ----------------------------------------------------------------- snapshots


def test_snapshot_stores_once_and_again_only_on_change(cfg, tmp_path):
    target = tmp_path / "carddb" / "Raw_CardDatabase_abc.mtga"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"version one" * 100)
    cfg.snapshots = [
        SnapshotSource(name="carddb", globs=[str(target.parent / "Raw_*.mtga")])
    ]
    state = state_mod.State()

    assert snapshot.run(cfg, state) == 1
    assert snapshot.run(cfg, state) == 0  # unchanged, nothing new stored

    target.write_bytes(b"version two" * 100)
    assert snapshot.run(cfg, state) == 1
    assert len(list(cfg.snapshots_dir.rglob("*.gz"))) == 2


def test_snapshot_of_live_sqlite_is_a_valid_database(cfg, tmp_path):
    db_path = tmp_path / "tracker" / "matches.db"
    db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(db_path)
    connection.execute("create table matches(id integer, deck text)")
    connection.executemany(
        "insert into matches values(?,?)", [(i, f"deck {i}") for i in range(200)]
    )
    connection.commit()

    # Deliberately left open and unfinalized, as a running tracker would.
    cfg.snapshots = [SnapshotSource(name="tracker", globs=[str(db_path)])]
    state = state_mod.State()
    assert snapshot.run(cfg, state) == 1
    connection.close()

    stored = next(cfg.snapshots_dir.rglob("*.gz"))
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(stored.read_bytes()))

    check = sqlite3.connect(restored)
    assert check.execute("select count(*) from matches").fetchone()[0] == 200
    check.close()


# -------------------------------------------------------------------- verify


def test_verify_flags_a_corrupted_archive(cfg):
    cfg.player_prev_log.write_bytes(make_log(S1) + body(S1, 40))
    watcher = Watcher(cfg, state_mod.State())
    watcher.startup()

    assert verify.run(cfg).problems == []

    archive_file = next(cfg.sessions_dir.rglob("*.log.gz"))
    data = bytearray(archive_file.read_bytes())
    data[len(data) // 2] ^= 0xFF
    archive_file.write_bytes(bytes(data))

    result = verify.run(cfg)
    assert len(result.problems) == 1
    assert "corrupt gzip" in result.problems[0] or "mismatch" in result.problems[0]


def test_verify_flags_a_missing_sidecar(cfg):
    cfg.player_prev_log.write_bytes(make_log(S1))
    Watcher(cfg, state_mod.State()).startup()

    next(cfg.sessions_dir.rglob("*.meta.json")).unlink()
    assert "missing metadata sidecar" in verify.run(cfg).problems[0]


# ---------------------------------------------------------------------- push


def test_push_never_ships_local_only_files(cfg):
    cfg.ensure_dirs()
    (cfg.sessions_dir / "current.part").write_bytes(b"in progress")
    (cfg.archive_dir / "state.json").write_bytes(b"{}")
    (cfg.archive_dir / "collector.lock").write_bytes(b"123")
    (cfg.sessions_dir / ".staging-99.log.gz").write_bytes(b"x")
    (cfg.sessions_dir / "session-20260816T111009-abcd1234.log.gz").write_bytes(b"real")

    shipped = {p.name for p in push_mod._local_files(cfg)}
    assert shipped == {"session-20260816T111009-abcd1234.log.gz"}


def test_push_requires_configuration(cfg):
    with pytest.raises(push_mod.PushError, match="No \\[push\\] section"):
        push_mod.run(cfg)


def test_push_defaults_to_ssh_even_when_rsync_is_available(cfg, monkeypatch):
    """rsync on PATH must not hijack the transport.

    On Windows an rsync on PATH is usually WSL's, which resolves C:/... against
    the Linux filesystem and silently sends nothing.
    """
    from collector.config import PushConfig

    cfg.push = PushConfig(host="pi@pi.local", remote_dir="/srv/arch")
    monkeypatch.setattr(push_mod.shutil, "which", lambda name: f"/usr/bin/{name}")

    called = []
    monkeypatch.setattr(push_mod, "_push_ssh", lambda *a, **k: called.append("ssh"))
    monkeypatch.setattr(push_mod, "_push_rsync", lambda *a, **k: called.append("rsync"))

    push_mod.run(cfg)
    assert called == ["ssh"]

    push_mod.run(cfg, use_rsync=True)
    assert called == ["ssh", "rsync"]


def test_push_sends_every_file_over_one_connection(cfg, monkeypatch):
    """One authentication per push, not one per file.

    A passphrase-protected key is unlocked once per ssh connection, so the
    transfer must be a single command however many files and directories it
    covers — and the listing call must share that same connection.
    """
    from collector.config import PushConfig

    cfg.ensure_dirs()
    for day in ("20260816", "20260817", "20260818"):
        directory = cfg.sessions_dir / day
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"session-{day}T111009-abcd1234.log.gz").write_bytes(b"data")

    cfg.push = PushConfig(host="pi@pi.local", remote_dir="/srv/arch")
    monkeypatch.setattr(push_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(push_mod, "_remote_files", lambda push, shared: set())

    connections = []

    class FakeProcess:
        returncode = 0
        stdout = None

        def __init__(self, command):
            self.args = command

        def communicate(self, input=None, timeout=None):
            return b"", b""

        def wait(self):
            return 0

        def poll(self):
            return 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_popen(command, **kwargs):
        # `-O exit` closes the shared master; it authenticates nothing.
        if command[0] == "ssh" and "-O" not in command:
            connections.append(command)
        return FakeProcess(command)

    monkeypatch.setattr(push_mod.subprocess, "Popen", fake_popen)

    out = push_mod.run(cfg)

    assert len(connections) == 1, f"expected one ssh command, got {connections}"
    assert "3 file(s)" in out


def test_push_shares_one_ssh_connection_across_commands(cfg):
    """The listing and the transfer must reuse a single authenticated session."""
    from collector.config import PushConfig

    push = PushConfig(host="pi@pi.local", remote_dir="/srv/arch")
    with push_mod._shared_connection(push) as shared:
        if os.name == "nt":  # Win32-OpenSSH cannot multiplex; nothing to share.
            assert shared == []
        else:
            assert "ControlMaster=auto" in shared
            assert any(option.startswith("ControlPath=") for option in shared)


def test_push_explains_a_missing_ssh_client(cfg, monkeypatch):
    from collector.config import PushConfig

    cfg.push = PushConfig(host="pi@pi.local", remote_dir="/srv/arch")
    monkeypatch.setattr(push_mod.shutil, "which", lambda name: None)

    with pytest.raises(push_mod.PushError, match="OpenSSH Client"):
        push_mod.run(cfg)


# ------------------------------------------------------------------ discover


def test_discover_derives_install_dir_from_the_log_header(tmp_path):
    log_path = tmp_path / "Player.log"
    log_path.write_bytes(make_log(S1))

    assert discover.mtga_data_dir(log_path) == Path(
        "C:/Program Files/Wizards of the Coast/MTGA/MTGA_Data"
    )
    globs = discover.card_database_globs(discover.mtga_data_dir(log_path))
    assert any(g.endswith("Raw_CardDatabase_*.mtga") for g in globs)


def test_discover_report_emits_an_absolute_log_path(tmp_path, monkeypatch):
    log_path = tmp_path / "Player.log"
    log_path.write_bytes(make_log(S1))
    monkeypatch.chdir(tmp_path)

    report = discover.report(Path("Player.log"))
    assert f'player_log = "{log_path.as_posix()}"' in report
    assert "detailed logs:    ENABLED" in report


def test_discover_report_warns_when_detailed_logs_are_off(tmp_path):
    log_path = tmp_path / "Player.log"
    log_path.write_bytes(make_log(S1, detailed=False))

    report = discover.report(log_path)
    assert "Detailed Logs is OFF" in report


# ------------------------------------------------------------------- parsing


def test_header_parsing_tolerates_a_24_hour_locale():
    header = make_log(S1).replace(
        b"Startup Timestamp: 8/16/2026 11:10:09 AM",
        b"Startup Timestamp: 8/16/2026 23:10:09",
    )
    assert parse_header(header).session_id == "20260816T231009"


def test_header_parsing_survives_an_unknown_timestamp_format():
    header = make_log(S1).replace(
        b"Startup Timestamp: 8/16/2026 11:10:09 AM",
        b"Startup Timestamp: sometime on Tuesday",
    )
    parsed = parse_header(header)
    assert parsed.session_id is None
    assert parsed.startup_raw == "sometime on Tuesday"
