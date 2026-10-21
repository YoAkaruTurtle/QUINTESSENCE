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

import sqlite3
from pathlib import Path
from typing import Union

from .errors import CorruptStore


def connect(path: Union[str, Path]) -> sqlite3.Connection:
    """Open *path* with row access by name and verified foreign keys."""

    connection = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
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
