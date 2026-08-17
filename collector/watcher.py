"""Tailing Player.log across Arena restarts without losing bytes.

The two hard parts, and why they are handled the way they are:

*Rotation detection.* Arena renames Player.log to Player-prev.log at launch and
starts a fresh file at the same path. Creation time cannot be trusted to spot
this: NTFS *file system tunneling* re-applies the original creation timestamp
when a file is recreated at the same path within ~15 seconds, which is exactly
the restart pattern. Identity is established instead from the immutable byte
prefix of a growing file, plus the Startup Timestamp in the header.

*Handle lifetime.* The log is opened and closed on every tick rather than held
open. A long-lived handle without FILE_SHARE_DELETE would make Arena's rename
fail and break its logging — the collector must be invisible to the game.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from .archive import PartWriter, archive_raw_file, now_iso
from .config import Config
from .session import FINGERPRINT_SIZE, HEAD_SIZE, parse_header, sha256_file
from .state import Current, State, save as save_state

log = logging.getLogger(__name__)

READ_CHUNK = 1 << 20


def _hash_prefix(path: Path, length: int) -> tuple[str, int]:
    """Hash the first `length` bytes, returning (hash, bytes_actually_read)."""
    with open(path, "rb") as handle:
        data = handle.read(length)
    return hashlib.sha256(data).hexdigest(), len(data)


class Watcher:
    """Owns the capture lifecycle for one Player.log path."""

    def __init__(self, cfg: Config, state: State):
        self.cfg = cfg
        self.state = state
        self.part: PartWriter | None = None
        self._dirty = False

    # ------------------------------------------------------------------ setup

    def startup(self) -> None:
        """Resume, or clean up after, whatever the last run left behind."""
        self.cfg.ensure_dirs()

        # A session that rotated while the collector was down survives only in
        # Player-prev.log. Recover it before touching the orphaned buffer, so
        # the complete copy is on disk first and the partial can be discarded.
        self.recover_prev()

        part_exists = self.cfg.part_path.exists() and self.cfg.part_path.stat().st_size > 0
        current = self.state.current

        if current and part_exists and self._can_resume(current):
            log.info(
                "Resuming session %s at offset %d",
                current.session_id or "(unidentified)",
                current.offset,
            )
            self.part = PartWriter(self.cfg.part_path)
            return

        if part_exists:
            log.info("Finalizing orphaned capture buffer from a previous run")
            self._finalize_part(complete=False, origin="orphan")

        self.state.current = None
        self.cfg.part_path.unlink(missing_ok=True)

    def _can_resume(self, current: Current) -> bool:
        """Is the live log still the same file we were mid-way through?"""
        path = self.cfg.player_log
        if not path.exists():
            return False
        try:
            if path.stat().st_size < current.offset:
                return False
            head_hash, read = _hash_prefix(path, current.head_len)
            return read == current.head_len and head_hash == current.head_hash
        except OSError:
            return False

    # ------------------------------------------------------------------- tick

    def tick(self) -> None:
        """One poll iteration. Never raises for ordinary I/O conditions."""
        path = self.cfg.player_log
        try:
            if not path.exists():
                return
            size = path.stat().st_size
        except OSError as exc:
            log.debug("stat failed for %s: %s", path, exc)
            return

        if self.state.current is None:
            self._begin_session(path, size)
            return

        try:
            rotated = self._detect_rotation(path, size)
        except OSError as exc:
            log.debug("rotation check failed: %s", exc)
            return

        if rotated:
            log.info("Rotation detected on %s", path)
            self._handle_rotation()
            self._begin_session(path, size)
            return

        if size > self.state.current.offset:
            self._ingest(path, size)

    def _detect_rotation(self, path: Path, size: int) -> bool:
        current = self.state.current
        assert current is not None

        # A file that shrank was truncated or replaced.
        if size < current.offset:
            return True

        # A growing file's prefix is immutable, so a changed prefix means a
        # different file.
        head_hash, read = _hash_prefix(path, current.head_len)
        if read < current.head_len:
            return True
        if head_hash != current.head_hash:
            return True

        # Belt and braces: a new Startup Timestamp is a new session even if the
        # prefix comparison was too short to be conclusive.
        if current.head_len < FINGERPRINT_SIZE and current.startup_raw:
            with open(path, "rb") as handle:
                header = parse_header(handle.read(HEAD_SIZE))
            if header.startup_raw and header.startup_raw != current.startup_raw:
                return True

        return False

    # -------------------------------------------------------------- lifecycle

    def _begin_session(self, path: Path, size: int) -> None:
        """Start capturing a session, always from byte zero."""
        try:
            with open(path, "rb") as handle:
                head = handle.read(HEAD_SIZE)
        except OSError as exc:
            log.debug("could not read header of %s: %s", path, exc)
            return

        header = parse_header(head)
        head_len = min(size, FINGERPRINT_SIZE)
        head_hash, read = _hash_prefix(path, head_len)

        self.cfg.part_path.unlink(missing_ok=True)
        self.part = PartWriter(self.cfg.part_path)
        self.state.current = Current(
            source=str(path),
            offset=0,
            head_hash=head_hash,
            head_len=read,
            session_id=header.session_id,
            startup_raw=header.startup_raw,
            detailed_logs=header.detailed_logs,
            started_at=now_iso(),
        )

        if header.detailed_logs is False:
            self._warn(
                "DETAILED LOGS is DISABLED in Arena. Match, deck and event data "
                "are not being written to the log at all. Enable it in Arena: "
                "Settings > Account > Detailed Logs (Plugin Support)."
            )

        log.info("Capturing session %s", header.session_id or "(unidentified)")
        self._ingest(path, size)

    def _ingest(self, path: Path, size: int) -> None:
        """Append everything between the stored offset and EOF."""
        current = self.state.current
        assert current is not None and self.part is not None

        try:
            with open(path, "rb") as handle:
                handle.seek(current.offset)
                written = 0
                while block := handle.read(READ_CHUNK):
                    self.part.append(block)
                    written += len(block)
        except OSError as exc:
            log.warning("read failed on %s: %s", path, exc)
            return

        if not written:
            return

        current.offset += written
        current.last_data_at = now_iso()
        self.part.flush()
        self._dirty = True

        # Once the file is big enough, widen the fingerprint to its full
        # length. The prefix already hashed cannot change, so this is safe.
        if current.head_len < FINGERPRINT_SIZE and current.offset >= FINGERPRINT_SIZE:
            current.head_hash, current.head_len = _hash_prefix(path, FINGERPRINT_SIZE)

        # The header may not have been written yet when the session began.
        if current.session_id is None:
            with open(path, "rb") as handle:
                header = parse_header(handle.read(HEAD_SIZE))
            if header.session_id:
                current.session_id = header.session_id
                current.startup_raw = header.startup_raw
                current.detailed_logs = header.detailed_logs
                log.info("Session identified as %s", header.session_id)

    def _handle_rotation(self) -> None:
        """Close out the session that just ended.

        Player-prev.log now holds that session complete, including the final
        bytes Arena wrote as it exited. It supersedes the live tail, which is
        by construction a prefix of it — so prefer prev and drop the buffer.
        """
        current = self.state.current
        recovered = self.recover_prev()

        superseded = (
            current is not None
            and recovered is not None
            and current.session_id is not None
            and recovered.session_id == current.session_id
            and recovered.bytes >= current.offset
        )

        if superseded:
            log.info(
                "Player-prev.log supersedes the live capture of session %s "
                "(%d bytes vs %d)",
                current.session_id,
                recovered.bytes,
                current.offset,
            )
            self._discard_part()
        else:
            self._finalize_part(complete=True, origin="tail")

        self.state.current = None

    # -------------------------------------------------------------- archiving

    def _finalize_part(self, *, complete: bool, origin: str) -> None:
        """Compress the buffer into the archive, unless it is redundant."""
        if self.part is not None:
            self.part.close()
            self.part = None

        path = self.cfg.part_path
        if not path.exists() or path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            return

        current = self.state.current
        started = current.started_at if current else now_iso()
        source = current.source if current else str(self.cfg.player_log)

        # Skip if a complete capture of this session, at least as large, is
        # already archived — that is the Player-prev.log recovery path winning.
        if current and current.session_id:
            best = self.state.best_capture_for(current.session_id)
            if best and best.complete and best.bytes >= path.stat().st_size:
                log.info(
                    "Discarding redundant buffer for session %s (%s already archived)",
                    current.session_id,
                    best.path,
                )
                self._discard_part()
                return

        record = archive_raw_file(
            self.cfg,
            path,
            source=source,
            complete=complete,
            started_at=started,
            origin=origin,
        )
        if self.state.has_content(record.sha256):
            log.info("Buffer duplicated an existing archive; kept as %s", record.path)
        self.state.sessions[record.sha256] = record
        self._dirty = True
        log.info(
            "Archived session %s -> %s (%.1f MB raw)",
            record.session_id,
            record.path,
            record.bytes / 1e6,
        )
        self._discard_part()

    def _discard_part(self) -> None:
        if self.part is not None:
            self.part.close()
            self.part = None
        self.cfg.part_path.unlink(missing_ok=True)

    def recover_prev(self):
        """Archive Player-prev.log if this collector has never seen it.

        This is the only thing standing between a collector that was down
        during an Arena restart and permanent data loss.
        """
        path = self.cfg.player_prev_log
        try:
            if not path.exists() or path.stat().st_size == 0:
                return None
            size = path.stat().st_size
            with open(path, "rb") as handle:
                header = parse_header(handle.read(HEAD_SIZE))
        except OSError as exc:
            log.debug("could not inspect %s: %s", path, exc)
            return None

        if header.session_id:
            best = self.state.best_capture_for(header.session_id)
            if best and best.bytes >= size:
                return best

        # Hash before compressing: prev is re-examined on every startup and
        # every rotation, and recompressing a 13 MB file to rediscover it is
        # already archived would be pure waste.
        digest = sha256_file(path)
        if self.state.has_content(digest):
            return self.state.sessions[digest]

        try:
            record = archive_raw_file(
                self.cfg,
                path,
                source=str(path),
                complete=True,
                started_at=now_iso(),
                origin="prev",
            )
        except OSError as exc:
            log.warning("failed to archive %s: %s", path, exc)
            return None

        if self.state.has_content(record.sha256):
            # Already archived byte-for-byte under the same name; nothing new.
            return self.state.sessions[record.sha256]

        self.state.sessions[record.sha256] = record
        self._dirty = True
        log.info(
            "Recovered %s -> %s (%.1f MB raw)",
            path.name,
            record.path,
            record.bytes / 1e6,
        )
        return record

    # ------------------------------------------------------------------ misc

    def _warn(self, message: str) -> None:
        log.warning(message)
        if message not in self.state.warnings:
            self.state.warnings.append(message)
            self._dirty = True

    def persist(self, force: bool = False) -> None:
        if self._dirty or force:
            self.state.last_tick = now_iso()
            save_state(self.cfg.state_path, self.state)
            self._dirty = False

    def shutdown(self) -> None:
        """Stop cleanly without ending the Arena session.

        The buffer and offset are left in place so the next run resumes
        appending rather than splitting one Arena session across two archives.
        """
        if self.part is not None:
            self.part.close()
            self.part = None
        self.persist(force=True)
