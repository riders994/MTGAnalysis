"""Configuration loading and path resolution."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_SNAPSHOT_INTERVAL = 300.0

# Unity writes its player log to LocalLow/<Company>/<Product>/Player.log.
WINDOWS_LOG_DIR = r"%USERPROFILE%\AppData\LocalLow\Wizards Of The Coast\MTGA"


class ConfigError(Exception):
    """Raised when a config file is missing, malformed, or incomplete."""


def expand(raw: str) -> Path:
    """Expand %VARS%, $VARS and ~ in a configured path."""
    return Path(os.path.expanduser(os.path.expandvars(str(raw))))


@dataclass
class SnapshotSource:
    """A named set of globs to snapshot whenever their contents change."""

    name: str
    globs: list[str]

    def resolve(self) -> list[Path]:
        """Expand the globs to existing files, deduplicated and sorted."""
        found: set[Path] = set()
        for pattern in self.globs:
            expanded = os.path.expanduser(os.path.expandvars(pattern))
            # Split the pattern into a fixed root and a glob tail so that
            # absolute Windows paths (with a drive letter) glob correctly.
            path = Path(expanded)
            anchor = Path(path.anchor) if path.anchor else Path(".")
            relative = path.relative_to(anchor) if path.anchor else path
            try:
                for match in anchor.glob(str(relative)):
                    if match.is_file():
                        found.add(match)
            except (OSError, ValueError, NotImplementedError):
                continue
        return sorted(found)


@dataclass
class PushConfig:
    """Where `push` sends the archive."""

    host: str
    remote_dir: str
    ssh_port: int | None = None
    identity_file: str | None = None


@dataclass
class Config:
    archive_dir: Path
    # Absent on a storage/reporting host, where there is no Arena to watch.
    player_log: Path | None
    player_prev_log: Path | None
    poll_interval: float = DEFAULT_POLL_INTERVAL
    snapshot_interval: float = DEFAULT_SNAPSHOT_INTERVAL
    snapshots: list[SnapshotSource] = field(default_factory=list)
    push: PushConfig | None = None
    source_path: Path | None = None

    # Derived archive locations.
    @property
    def sessions_dir(self) -> Path:
        return self.archive_dir / "sessions"

    @property
    def snapshots_dir(self) -> Path:
        return self.archive_dir / "snapshots"

    @property
    def state_path(self) -> Path:
        return self.archive_dir / "state.json"

    @property
    def log_path(self) -> Path:
        return self.archive_dir / "collector.log"

    @property
    def lock_path(self) -> Path:
        return self.archive_dir / "collector.lock"

    @property
    def part_path(self) -> Path:
        """The single in-progress capture buffer."""
        return self.sessions_dir / "current.part"

    def ensure_dirs(self) -> None:
        for directory in (self.archive_dir, self.sessions_dir, self.snapshots_dir):
            directory.mkdir(parents=True, exist_ok=True)


def default_config_path() -> Path:
    """Config lives next to the package's repo root unless overridden."""
    return Path(__file__).resolve().parent.parent / "config.toml"


def load(path: Path | None = None) -> Config:
    """Load and validate a TOML config file."""
    path = Path(path) if path else default_config_path()
    if not path.exists():
        raise ConfigError(
            f"No config at {path}.\n"
            "Run `python -m collector discover` to generate one, then copy the "
            "printed block into config.toml (see config.example.toml)."
        )

    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    paths = data.get("paths", {})
    if "archive_dir" not in paths:
        raise ConfigError(f"{path}: [paths] archive_dir is required")

    # player_log is only needed to capture. On the storage host the archive is
    # read, verified and reported on without Arena being anywhere nearby, so a
    # config with just archive_dir is valid there.
    player_log = expand(paths["player_log"]) if paths.get("player_log") else None
    prev_raw = paths.get("player_prev_log")
    player_prev = (
        expand(prev_raw)
        if prev_raw
        else (player_log.with_name("Player-prev.log") if player_log else None)
    )

    capture = data.get("capture", {})
    snapshots = [
        SnapshotSource(name=entry["name"], globs=list(entry.get("globs", [])))
        for entry in data.get("snapshot", [])
        if entry.get("name")
    ]

    push_data = data.get("push")
    push = None
    if push_data and push_data.get("host") and push_data.get("remote_dir"):
        push = PushConfig(
            host=push_data["host"],
            remote_dir=push_data["remote_dir"],
            ssh_port=push_data.get("ssh_port"),
            identity_file=push_data.get("identity_file"),
        )

    return Config(
        archive_dir=expand(paths["archive_dir"]),
        player_log=player_log,
        player_prev_log=player_prev,
        poll_interval=float(
            capture.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL)
        ),
        snapshot_interval=float(
            capture.get("snapshot_interval_seconds", DEFAULT_SNAPSHOT_INTERVAL)
        ),
        snapshots=snapshots,
        push=push,
        source_path=path,
    )
