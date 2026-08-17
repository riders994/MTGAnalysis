import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector.config import Config  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

HEADER_TEMPLATE = """Mono path[0] = 'C:/Program Files/Wizards of the Coast/MTGA/MTGA_Data/Managed'
Mono config path = 'C:/Program Files/Wizards of the Coast/MTGA/MonoBleedingEdge/etc'
Initialize engine version: 2022.3.62f2 (7670c08855a9)
DETAILED LOGS: {detailed}
Startup Timestamp: {startup}
Loaded embedded metadata
Version: 2026.62.0.13653 / 2026.62.0.13653.1304277 /
"""


def make_log(startup: str, *, detailed: bool = True, padding: int = 12000,
             marker: str = "A") -> bytes:
    """A synthetic Player.log header, padded past the fingerprint window."""
    header = HEADER_TEMPLATE.format(
        detailed="ENABLED" if detailed else "DISABLED", startup=startup
    )
    filler = "".join(
        f"[UnityCrossThreadLogger]{startup}: filler line {marker}{i}\n"
        for i in range(padding // 60 + 1)
    )
    return (header + filler).encode("utf-8")


def body(startup: str, count: int, marker: str = "B") -> bytes:
    return "".join(
        f"[UnityCrossThreadLogger]{startup}: event {marker}{i}\n" for i in range(count)
    ).encode("utf-8")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config = Config(
        archive_dir=tmp_path / "archive",
        player_log=tmp_path / "mtga" / "Player.log",
        player_prev_log=tmp_path / "mtga" / "Player-prev.log",
        poll_interval=0.0,
        snapshot_interval=0.0,
        source_path=tmp_path / "config.toml",
    )
    config.player_log.parent.mkdir(parents=True, exist_ok=True)
    config.ensure_dirs()
    return config


@pytest.fixture
def real_logs():
    """The captured sample logs, when present (they are gitignored)."""
    current = REPO_ROOT / "Player.log"
    previous = REPO_ROOT / "Player-prev.log"
    if not current.exists() or not previous.exists():
        pytest.skip("real Player.log fixtures not present")
    return previous, current
