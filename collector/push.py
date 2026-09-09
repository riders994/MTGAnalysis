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

Every transfer in a push runs over a single authentication. Files go up in one
tar stream rather than one scp per directory, and where the ssh client supports
connection multiplexing the remaining listing call shares that connection — so a
passphrase-protected key is unlocked once per push, not once per file.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from . import state as state_mod
from .archive import now_iso
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


@contextmanager
def _shared_connection(push: PushConfig):
    """Yield ssh options that make every command in a push reuse one connection.

    With `ControlMaster=auto` the first command opens the connection and the
    rest ride along on it, so a key passphrase is entered once. Win32-OpenSSH
    has no multiplexing, so there we yield nothing and rely on the transfer
    itself being a single command.
    """
    if os.name == "nt":
        yield []
        return

    with tempfile.TemporaryDirectory(prefix="mtga-push-") as directory:
        options = [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={Path(directory) / 'cm'}",
            "-o", "ControlPersist=60",
        ]
        try:
            yield options
        finally:
            # Close the master here; ControlPersist would otherwise outlive the
            # socket directory and leave ssh complaining about a gone path.
            subprocess.run(
                ["ssh", *_ssh_options(push), *options, "-O", "exit", push.host],
                capture_output=True,
                text=True,
            )


def _remote_files(push: PushConfig, shared: list[str]) -> set[str]:
    """List files already on the remote, relative to remote_dir."""
    command = (
        f"mkdir -p {push.remote_dir} && cd {push.remote_dir} && "
        "find . -type f 2>/dev/null | sed 's|^\\./||'"
    )
    result = subprocess.run(
        ["ssh", *_ssh_options(push), *shared, push.host, command],
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
    # rsync opens one connection of its own, so it needs no sharing options.
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


def _send_tar(cfg: Config, push: PushConfig, missing: list[Path],
              shared: list[str]) -> None:
    """Send every missing file in one tar stream over one ssh command.

    tar carries the directory structure with it, so this replaces both the
    remote mkdir and the per-directory scp calls — one connection, and with it
    one passphrase prompt, for the whole transfer.
    """
    relatives = [path.relative_to(cfg.archive_dir).as_posix() for path in missing]
    remote_dir = push.remote_dir.rstrip("/")
    remote_command = f"mkdir -p {remote_dir} && tar -x -f - -C {remote_dir}"

    with tempfile.TemporaryDirectory(prefix="mtga-push-") as directory:
        # -T keeps the file list off the command line, which Windows caps at
        # ~32k characters — a long archive would otherwise blow past it.
        list_file = Path(directory) / "files"
        list_file.write_text("\n".join(relatives) + "\n", encoding="utf-8")

        # tar's stderr goes to a file rather than a pipe: we block on ssh until
        # the stream ends, so nothing would be draining a pipe tar could fill.
        tar_errors = Path(directory) / "tar-stderr"
        with open(tar_errors, "wb") as tar_stderr:
            sender = subprocess.Popen(
                ["tar", "-c", "-f", "-", "-C", str(cfg.archive_dir),
                 "-T", str(list_file)],
                stdout=subprocess.PIPE,
                stderr=tar_stderr,
            )
            stream = sender.stdout
            try:
                receiver = subprocess.Popen(
                    ["ssh", *_ssh_options(push), *shared, push.host, remote_command],
                    stdin=stream,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            finally:
                # Only the receiver should hold the read end, or tar never
                # sees EOF and the push hangs.
                if stream is not None:
                    stream.close()
            _, receiver_errors = receiver.communicate()
            sender.wait()

        sender_problem = tar_errors.read_text(errors="replace").strip()

    # ssh first: if it died mid-stream, tar only failed because its pipe closed,
    # and ssh is the one holding the reason.
    if receiver.returncode != 0:
        raise PushError(
            f"transfer failed: {receiver_errors.decode(errors='replace').strip()}"
        )
    if sender.returncode != 0:
        raise PushError(f"tar failed: {sender_problem}")


def _send_scp(cfg: Config, push: PushConfig, missing: list[Path],
              shared: list[str]) -> None:
    """Fallback for hosts without tar: one scp per directory.

    Without connection sharing this authenticates once per directory, which is
    why it is the fallback and not the default.
    """
    by_dir: dict[str, list[Path]] = defaultdict(list)
    for path in missing:
        relative = path.relative_to(cfg.archive_dir).as_posix()
        by_dir[str(Path(relative).parent.as_posix())].append(path)

    directories = " ".join(
        f"{push.remote_dir.rstrip('/')}/{d}" if d != "." else push.remote_dir
        for d in by_dir
    )
    make_dirs = subprocess.run(
        ["ssh", *_ssh_options(push), *shared, push.host, f"mkdir -p {directories}"],
        capture_output=True,
        text=True,
    )
    if make_dirs.returncode != 0:
        raise PushError(f"remote mkdir failed: {make_dirs.stderr.strip()}")

    for directory, paths in by_dir.items():
        target = push.remote_dir.rstrip("/")
        if directory != ".":
            target = f"{target}/{directory}"
        command = ["scp", *_ssh_options(push), *shared, *[str(p) for p in paths],
                   f"{push.host}:{target}/"]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise PushError(f"scp failed for {directory}: {result.stderr.strip()}")


def _push_ssh(cfg: Config, push: PushConfig, dry_run: bool) -> str:
    with _shared_connection(push) as shared:
        remote = _remote_files(push, shared)
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

        log.info("sending %d file(s) -> %s:%s", len(missing), push.host,
                 push.remote_dir)
        if shutil.which("tar"):
            _send_tar(cfg, push, missing, shared)
        else:
            _send_scp(cfg, push, missing, shared)

    summary.append(f"  sent to {push.host}:{push.remote_dir.rstrip('/')}/")
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
        result = _push_rsync(cfg, cfg.push, dry_run)
    else:
        if not shutil.which("ssh"):
            raise PushError(
                "ssh is not on PATH. On Windows install the OpenSSH client:\n"
                "  Settings > System > Optional features > Add > OpenSSH Client"
            )
        result = _push_ssh(cfg, cfg.push, dry_run)

    if not dry_run:
        state = state_mod.load(cfg.state_path)
        state.last_push = now_iso()
        state_mod.save(cfg.state_path, state)

    return result
