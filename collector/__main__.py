"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import __version__, discover, push as push_mod, snapshot, state as state_mod, verify
from .config import Config, ConfigError, default_config_path, load
from .lock import AlreadyRunning, SingleInstance
from .watcher import Watcher

# How often to write state even when nothing changed, so `status` can tell a
# live collector from a dead one.
HEARTBEAT_SECONDS = 60.0


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    cfg.ensure_dirs()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        cfg.log_path, maxBytes=5 << 20, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Under pythonw.exe there is no console and sys.stderr is None.
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    if cfg.player_log is None:
        print(
            f"{cfg.source_path}: [paths] player_log is required to capture.\n"
            "Run `python -m collector discover` to find it. (A config without "
            "it is fine on the storage host, where only verify and reporting "
            "run.)",
            file=sys.stderr,
        )
        return 2

    try:
        lock = SingleInstance(cfg.lock_path)
        lock.__enter__()
    except AlreadyRunning as exc:
        print(exc, file=sys.stderr)
        return 1

    setup_logging(cfg, args.verbose)
    log = logging.getLogger("collector")
    log.info("Collector %s starting; archive at %s", __version__, cfg.archive_dir)
    log.info("Watching %s", cfg.player_log)

    state = state_mod.load(cfg.state_path)
    watcher = Watcher(cfg, state)
    stop = threading.Event()

    def handle_signal(signum, _frame):
        log.info("Received signal %s; shutting down", signum)
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), handle_signal)
            except (ValueError, OSError):
                pass

    try:
        watcher.startup()
        snapshot.run(cfg, state)
        watcher.persist(force=True)

        next_snapshot = time.monotonic() + cfg.snapshot_interval
        next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS

        while not stop.is_set():
            try:
                watcher.tick()
            except Exception:  # never let one bad tick kill the collector
                log.exception("Unhandled error during tick")

            now = time.monotonic()
            if now >= next_snapshot:
                try:
                    snapshot.run(cfg, state)
                except Exception:
                    log.exception("Unhandled error during snapshot")
                next_snapshot = now + cfg.snapshot_interval

            if now >= next_heartbeat:
                watcher.persist(force=True)
                next_heartbeat = now + HEARTBEAT_SECONDS
            else:
                watcher.persist()

            stop.wait(cfg.poll_interval)
    finally:
        watcher.shutdown()
        log.info("Collector stopped")
        lock.__exit__(None, None, None)

    return 0


def _age(iso: str) -> str:
    if not iso:
        return "never"
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    delta = datetime.now().astimezone() - when
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    state = state_mod.load(cfg.state_path)

    running = False
    try:
        with SingleInstance(cfg.lock_path):
            running = False
    except AlreadyRunning:
        running = True

    print(f"config      {cfg.source_path}")
    print(f"archive     {cfg.archive_dir}")
    if cfg.player_log is None:
        print("watching    nothing (storage host: no player_log configured)")
    else:
        print(f"watching    {cfg.player_log}"
              + ("" if cfg.player_log.exists() else "   (not present)"))
    print(f"collector   {'RUNNING' if running else 'not running'}"
          f"   last tick {_age(state.last_tick)}")
    print()

    if state.current:
        current = state.current
        part = cfg.part_path
        size = part.stat().st_size if part.exists() else 0
        print("in progress")
        print(f"  session   {current.session_id or '(unidentified)'}"
              f"   started {current.startup_raw or '?'}")
        print(f"  buffered  {size / 1e6:.2f} MB   last data {_age(current.last_data_at)}")
        detailed = {True: "enabled", False: "DISABLED", None: "unknown"}[
            current.detailed_logs
        ]
        print(f"  detailed  {detailed}")
    else:
        print("in progress   none (Arena not running, or no data yet)")
    print()

    sessions = sorted(state.sessions.values(), key=lambda s: s.session_id)
    raw = sum(s.bytes for s in sessions)
    compressed = 0
    for session in sessions:
        path = cfg.archive_dir / session.path
        if path.exists():
            compressed += path.stat().st_size

    print(f"archived    {len(sessions)} session(s)")
    if sessions:
        ratio = f"{raw / compressed:.1f}x" if compressed else "n/a"
        print(f"  raw       {raw / 1e6:.1f} MB   compressed {compressed / 1e6:.1f} MB  ({ratio})")
        print(f"  span      {sessions[0].session_id}  ->  {sessions[-1].session_id}")
        incomplete = [s for s in sessions if not s.complete]
        if incomplete:
            print(f"  partial   {len(incomplete)} session(s) captured mid-stream")
    print(f"snapshots   {len(state.snapshots)} unique file version(s)")

    if args.sessions and sessions:
        print()
        print("  session          bytes      complete  path")
        for session in sessions:
            print(f"  {session.session_id}  {session.bytes:>10,}  "
                  f"{'yes' if session.complete else 'no ':>8}  {session.path}")

    if state.warnings:
        print()
        print("warnings")
        for warning in state.warnings:
            print(f"  ! {warning}")

    return 0


def cmd_discover(cfg: Config | None, args: argparse.Namespace) -> int:
    explicit = Path(args.log) if args.log else None
    print(discover.report(explicit))
    return 0


def cmd_verify(cfg: Config, args: argparse.Namespace) -> int:
    result = verify.run(cfg)
    print(f"checked {result.checked} file(s), {result.ok} ok, "
          f"{len(result.problems)} problem(s)")
    if result.raw_bytes:
        ratio = (
            f"{result.raw_bytes / result.compressed_bytes:.1f}x"
            if result.compressed_bytes
            else "n/a"
        )
        print(f"  {result.raw_bytes / 1e6:.1f} MB raw -> "
              f"{result.compressed_bytes / 1e6:.1f} MB stored ({ratio})")
    for problem in result.problems:
        print(f"  ! {problem}")
    return 1 if result.problems else 0


def cmd_push(cfg: Config, args: argparse.Namespace) -> int:
    try:
        print(push_mod.run(cfg, dry_run=args.dry_run, use_rsync=args.rsync))
    except push_mod.PushError as exc:
        print(f"push failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_install(cfg: Config, args: argparse.Namespace) -> int:
    from . import install as install_mod

    try:
        print(install_mod.install(cfg.source_path or default_config_path()))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def cmd_uninstall(cfg: Config | None, args: argparse.Namespace) -> int:
    from . import install as install_mod

    try:
        print(install_mod.uninstall())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    # Shared options, attached to both the top level and every subcommand, so
    # `collector --config X push` and `collector push --config X` both work.
    # SUPPRESS keeps a subcommand's unset default from clobbering a value
    # already given at the top level.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", type=Path, default=argparse.SUPPRESS, help="path to config.toml"
    )
    common.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="debug logging",
    )

    parser = argparse.ArgumentParser(
        prog="collector",
        description="Capture MTG Arena's Player.log so sessions survive restarts.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="run the capture loop", parents=[common])
    run_parser.set_defaults(func=cmd_run, needs_config=True)

    status_parser = sub.add_parser(
        "status", help="show what has been captured", parents=[common]
    )
    status_parser.add_argument(
        "--sessions", action="store_true", help="list every archived session"
    )
    status_parser.set_defaults(func=cmd_status, needs_config=True)

    discover_parser = sub.add_parser(
        "discover",
        help="locate Arena paths and print a config block",
        parents=[common],
    )
    discover_parser.add_argument("--log", help="explicit Player.log path to inspect")
    discover_parser.set_defaults(func=cmd_discover, needs_config=False)

    verify_parser = sub.add_parser(
        "verify", help="check archive integrity", parents=[common]
    )
    verify_parser.set_defaults(func=cmd_verify, needs_config=True)

    push_parser = sub.add_parser(
        "push", help="send the archive to the storage host", parents=[common]
    )
    push_parser.add_argument("--dry-run", action="store_true", help="show what would send")
    push_parser.add_argument(
        "--rsync", action="store_true", help="use rsync instead of ssh+scp"
    )
    push_parser.set_defaults(func=cmd_push, needs_config=True)

    install_parser = sub.add_parser(
        "install", help="register the Scheduled Task", parents=[common]
    )
    install_parser.set_defaults(func=cmd_install, needs_config=True)

    uninstall_parser = sub.add_parser(
        "uninstall", help="remove the Scheduled Task", parents=[common]
    )
    uninstall_parser.set_defaults(func=cmd_uninstall, needs_config=False)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.config = getattr(args, "config", None)
    args.verbose = getattr(args, "verbose", False)

    cfg = None
    if args.needs_config:
        try:
            cfg = load(args.config)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        cfg.ensure_dirs()

    return args.func(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
