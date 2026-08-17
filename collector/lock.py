"""Single-instance guard.

Task Scheduler is configured with IgnoreNew, but the task can also be started
by hand, and two collectors tailing the same log would duplicate captures and
race on the shared `.part` buffer.
"""

from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunning(Exception):
    pass


class SingleInstance:
    """Advisory exclusive lock on a file, released on exit or process death."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def __enter__(self) -> "SingleInstance":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        # "r+" rather than "a+": on Windows an append-mode handle ignores seek,
        # which would break both the byte-range lock and the pid write.
        self._handle = open(self.path, "r+")
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            raise AlreadyRunning(
                f"Another collector already holds {self.path}. "
                "Only one instance may tail the log at a time."
            ) from exc

        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(str(os.getpid()))
        self._handle.flush()
        return self

    def __exit__(self, *exc_info) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None
