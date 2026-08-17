"""Manual push of the archive to the storage host over SSH.

Archive files are immutable and content-addressed, which makes this idempotent:
re-running after a failed or partial transfer is always safe, and costs only a
repeat of what did not land.

The default transport is the OpenSSH client that Windows 10+ ships, driven by a
manifest diff: list what is already on the remote, send what is missing. rsync
is available behind --rsync but is *not* the default, for two reasons. Windows
rarely has it, and where it does the binary is often WSL's, which resolves
`C:/...` paths against the Linux filesystem and silently sends nothing. More
fundamentally, rsync's advantage is efficiently transferring files that changed
— and these files never change, so a presence check is exactly as efficient with
far less to go wrong.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from .config import Config, PushConfig

log = logging.getLogger(__name__)

# Never shipped: the live buffer, staging files, and machine-local state.
EXCLUDES = (
    "current.part", "*.tmp", "collector.lock", ".staging-*", "state.json", ".tmp",
)


class PushError(Exception):
    pass


def _ssh_options(push: PushConfig) -> list[str]:
    options: list[str] = []
    if push.ssh_port:
        options += ["-p", str(push.ssh_port)]
    if push.identity_file:
        options += ["-i", push.identity_file]
    return options


def _remote_files(push: PushConfig) -> set[str]:
    """List files already on the remote, relative to remote_dir."""
    command = (
        f"mkdir -p {push.remote_dir} && cd {push.remote_dir} && "
        "find . -type f 2>/dev/null | sed 's|^\\./||'"
    )
    result = subprocess.run(
        ["ssh", *_ssh_options(push), push.host, command],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PushError(f"ssh listing failed: {result.stderr.strip()}")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _local_files(cfg: Config) -> list[Path]:
    files: list[Path] = []
    for path in cfg.archive_dir.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(cfg.archive_dir)
        # Skip dot-directories (snapshot staging) anywhere in the path.
        if any(part.startswith(".") for part in relative.parts[:-1]):
            continue
        name = path.name
        if name in EXCLUDES or name.endswith(".tmp") or name.startswith(".staging-"):
            continue
        files.append(path)
    return sorted(files)


def _push_rsync(cfg: Config, push: PushConfig, dry_run: bool) -> str:
    ssh_command = "ssh " + " ".join(_ssh_options(push)) if _ssh_options(push) else "ssh"
    command = ["rsync", "-rtv", "--partial", "-e", ssh_command]
    for pattern in EXCLUDES:
        command += ["--exclude", pattern]
    if dry_run:
        command.append("--dry-run")
    command += [
        cfg.archive_dir.as_posix().rstrip("/") + "/",
        f"{push.host}:{push.remote_dir.rstrip('/')}/",
    ]

    log.info("rsync -> %s:%s", push.host, push.remote_dir)
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise PushError(f"rsync failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def _push_scp(cfg: Config, push: PushConfig, dry_run: bool) -> str:
    remote = _remote_files(push)
    local = _local_files(cfg)
    missing = [
        path for path in local
        if path.relative_to(cfg.archive_dir).as_posix() not in remote
    ]

    if not missing:
        return "Nothing to send; remote is up to date."

    total = sum(path.stat().st_size for path in missing)
    summary = [f"{len(missing)} file(s), {total / 1e6:.1f} MB to send"]
    if dry_run:
        summary += [
            "  " + path.relative_to(cfg.archive_dir).as_posix() for path in missing
        ]
        return "\n".join(summary)

    by_dir: dict[str, list[Path]] = defaultdict(list)
    for path in missing:
        relative = path.relative_to(cfg.archive_dir).as_posix()
        by_dir[str(Path(relative).parent.as_posix())].append(path)

    directories = " ".join(
        f"{push.remote_dir.rstrip('/')}/{d}" if d != "." else push.remote_dir
        for d in by_dir
    )
    make_dirs = subprocess.run(
        ["ssh", *_ssh_options(push), push.host, f"mkdir -p {directories}"],
        capture_output=True,
        text=True,
    )
    if make_dirs.returncode != 0:
        raise PushError(f"remote mkdir failed: {make_dirs.stderr.strip()}")

    for directory, paths in by_dir.items():
        target = push.remote_dir.rstrip("/")
        if directory != ".":
            target = f"{target}/{directory}"
        command = ["scp", *_ssh_options(push), *[str(p) for p in paths],
                   f"{push.host}:{target}/"]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise PushError(f"scp failed for {directory}: {result.stderr.strip()}")
        summary.append(f"  sent {len(paths)} file(s) to {directory}")

    return "\n".join(summary)


def run(cfg: Config, dry_run: bool = False, use_rsync: bool = False) -> str:
    if cfg.push is None:
        raise PushError(
            "No [push] section in the config. Add host and remote_dir, e.g.\n"
            "  [push]\n"
            '  host = "pi@raspberrypi.local"\n'
            '  remote_dir = "/srv/mtga-archive"'
        )
    if not cfg.archive_dir.exists():
        raise PushError(f"Archive directory does not exist: {cfg.archive_dir}")

    if use_rsync:
        if not shutil.which("rsync"):
            raise PushError("--rsync given but rsync is not on PATH.")
        return _push_rsync(cfg, cfg.push, dry_run)

    if not shutil.which("ssh"):
        raise PushError(
            "ssh is not on PATH. On Windows install the OpenSSH client:\n"
            "  Settings > System > Optional features > Add > OpenSSH Client"
        )
    return _push_scp(cfg, cfg.push, dry_run)
