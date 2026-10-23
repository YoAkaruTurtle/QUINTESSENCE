"""Cross-process data-instance lock for the desktop Consult sidecar.

One desktop application instance may run at most one sidecar that writes one
PCS data instance.  This is not a permanent PCS-wide exclusion: the lock is
bound to one database path, held by an OS file handle for the lifetime of the
sidecar process and released by the operating system if that process exits
abnormally.  The lock file itself is never deleted, so a stale file cannot
block a later start.

The lock is taken before any SQLite connection is opened.  A second sidecar
therefore never creates a write connection to a database that is already in
use; it reports ``DATA_LOCKED`` instead.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_LOCK_SUFFIX = ".lock"
_LOCK_BYTE = 1


class DataLocked(Exception):
    """Another process already owns this data instance."""


class DataInstanceLock:
    """An OS-backed exclusive lock on one database instance path."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).resolve()
        self.lock_path = self.database_path.with_name(
            self.database_path.name + _LOCK_SUFFIX
        )
        self._fd: int | None = None
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self, *, owner: str) -> None:
        """Try to take the lock without blocking; raise :class:`DataLocked`."""

        if self._held:
            raise RuntimeError("the data lock is already held by this object")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise DataLocked("the data instance lock file is not writable") from exc
        try:
            if os.fstat(fd).st_size < _LOCK_BYTE:
                os.write(fd, b"\n")
            _lock(fd)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            raise DataLocked("another process owns this data instance") from exc
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        self._fd = fd
        self._held = True
        record = {
            "schema": "desktop.data-lock.v1",
            "pid": os.getpid(),
            "owner": owner,
            "database": str(self.database_path),
        }
        try:
            payload = json.dumps(record, ensure_ascii=False).encode("utf-8")
            os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
            os.ftruncate(fd, _LOCK_BYTE)
            os.write(fd, payload)
            os.fsync(fd)
        except OSError:
            # Diagnostics are best effort; the lock itself is already held.
            pass

    def release(self) -> None:
        if not self._held:
            return
        fd = self._fd
        self._fd = None
        self._held = False
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> "DataInstanceLock":
        self.acquire(owner="sidecar")
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()


def _lock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_BYTE)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_BYTE)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


__all__ = ["DataInstanceLock", "DataLocked"]
