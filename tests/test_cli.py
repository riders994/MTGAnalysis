"""CLI wiring: argument placement, and commands that need no Arena present."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from collector.__main__ import main

from conftest import body, make_log

S1 = "8/16/2026 11:10:09 AM"


@pytest.fixture
def populated(tmp_path: Path) -> Path:
    """A config plus an archive holding one captured session."""
    archive = tmp_path / "archive"
    config = tmp_path / "config.toml"
    config.write_text(
        textwrap.dedent(
            f"""
            [paths]
            archive_dir = "{archive.as_posix()}"
            player_log = "{(tmp_path / 'mtga' / 'Player.log').as_posix()}"
            """
        )
    )
    prev = tmp_path / "mtga" / "Player-prev.log"
    prev.parent.mkdir(parents=True, exist_ok=True)
    prev.write_bytes(make_log(S1) + body(S1, 30))

    from collector import state as state_mod
    from collector.config import load
    from collector.watcher import Watcher

    watcher = Watcher(load(config), state_mod.State())
    watcher.startup()
    watcher.persist(force=True)  # as `run` does, so `status` can see it
    return config


@pytest.mark.parametrize(
    "argv",
    [
        ["--config", "{config}", "verify"],
        ["verify", "--config", "{config}"],
    ],
    ids=["config-before-subcommand", "config-after-subcommand"],
)
def test_config_accepted_on_either_side_of_the_subcommand(populated, argv, capsys):
    resolved = [a.format(config=populated) for a in argv]
    assert main(resolved) == 0
    assert "1 ok" in capsys.readouterr().out


def test_status_reports_the_archived_session(populated, capsys):
    assert main(["--config", str(populated), "status", "--sessions"]) == 0
    out = capsys.readouterr().out
    assert "archived    1 session(s)" in out
    assert "20260816T111009" in out


def test_status_reports_never_pushed_until_a_push_succeeds(populated, capsys):
    from collector import state as state_mod
    from collector.archive import now_iso
    from collector.config import load

    assert main(["--config", str(populated), "status"]) == 0
    assert "last push   never" in capsys.readouterr().out

    cfg = load(populated)
    state = state_mod.load(cfg.state_path)
    state.last_push = now_iso()
    state_mod.save(cfg.state_path, state)

    assert main(["--config", str(populated), "status"]) == 0
    assert "last push   0s ago" in capsys.readouterr().out


def test_run_refuses_a_storage_host_config(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text(f'[paths]\narchive_dir = "{(tmp_path / "a").as_posix()}"\n')

    assert main(["--config", str(config), "run"]) == 2
    assert "player_log is required" in capsys.readouterr().err


def test_missing_config_points_at_discover(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "absent.toml"), "verify"]) == 2
    assert "discover" in capsys.readouterr().err


def test_verify_exits_nonzero_when_the_archive_is_damaged(populated, capsys):
    from collector.config import load

    cfg = load(populated)
    archive_file = next(cfg.sessions_dir.rglob("*.log.gz"))
    data = bytearray(archive_file.read_bytes())
    data[len(data) // 2] ^= 0xFF
    archive_file.write_bytes(bytes(data))

    assert main(["--config", str(populated), "verify"]) == 1
    assert "problem" in capsys.readouterr().out
