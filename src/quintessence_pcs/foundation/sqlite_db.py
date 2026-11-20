"""SQLite connection setup shared by every store path.

Two deliberate choices:

* ``isolation_level=None`` puts the connection in manual-commit mode, so a
  transaction is exactly what the caller writes (``BEGIN`` / ``COMMIT`` /
  ``ROLLBACK``) and ``executescript`` cannot silently commit a half-applied
  migration.
* ``PRAGMA foreign_keys = ON`` is set on every connection and then read back.
  SQLite enables foreign keys per connection and off by default; a connection
  without it would accept orphan version rows, so a non-``1`` setting is
  treated as a hard failure rather than a warning.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

from .errors import CorruptStore


@contextmanager
def hold_existing_lock(path: Path) -> Iterator[None]:
    """Pin an existing SQLite writer lock without changing its bytes."""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise OSError("the existing writer lock must be a regular file")
    fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def connect(
    path: Union[str, Path], *, read_only: bool = False
) -> sqlite3.Connection:
    """Open *path* with row access by name and verified foreign keys."""

    target = str(path)
    if read_only:
        # The caller holds the normal writer lock for this connection's life.
        # SQLite mode=ro alone can create WAL and SHM sidecars. An immutable
        # open avoids those writes, but is only valid without pending journals.
        database = Path(path).absolute()
        for suffix in ("-wal", "-journal"):
            companion = database.with_name(database.name + suffix)
            try:
                metadata = companion.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size:
                raise sqlite3.OperationalError(
                    "pending database journal requires foreground recovery"
                )
        target = f"{database.as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(
        target, uri=read_only, isolation_level=None, timeout=5.0
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        if read_only:
            connection.execute("PRAGMA query_only = ON")
    except BaseException:
        connection.close()
        raise
    if enabled != 1:
        connection.close()
        raise CorruptStore(
            f"connection reports foreign_keys={enabled!r}; refusing to use it without FK enforcement"
        )
    return connection


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


__all__ = ["connect", "table_exists"]
