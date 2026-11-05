"""Bind a memory data directory to the installation that wrote it (J11).

The upstream memory store records, inside ``memory.sqlite3``, the absolute
directory it was created with (``instance_meta.data_directory``).  Copying or
moving an instance therefore leaves a store that names the old location, and
every recall fails with ``IDENTITY_UNBOUND`` — the failure happens far from the
cause, with no hint about what to repair.

This module makes the mismatch visible at startup and offers exactly one
controlled repair:

* :func:`ensure_binding` compares the recorded directory with the configured
  one.  Equal (after normalisation) means nothing happens.
* A mismatch raises :class:`InstanceBindingMismatch` unless the host explicitly
  declared an adoption (``by`` + ``reason``).  Nothing is repaired silently, so
  a copied instance can never inherit another installation's identity by
  accident.
* :func:`adopt` performs the repair once, records the old/new values with who
  and why, and is refused when the store already points at the right directory.

This module never creates a memory store and never deletes one: it only reads
or updates the binding row of an existing ``memory.sqlite3``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping

#: Raised code when a memory store belongs to a different directory.
IDENTITY_UNBOUND = "IDENTITY_UNBOUND"

#: Name of the upstream database inside ``memory_bridge.data_directory``.
MEMORY_DB_NAME = "memory.sqlite3"

#: Audit log written next to the database when an adoption happens.
ADOPTION_LOG_NAME = "binding-adoption.jsonl"

_META_TABLE = "instance_meta"


class InstanceBindingMismatch(Exception):
    """The memory store names a different directory than the host configured."""

    code = IDENTITY_UNBOUND

    def __init__(self, stored: str, expected: str, *, data_directory: str):
        self.stored = stored
        self.expected = expected
        self.data_directory = data_directory
        super().__init__(
            f"{IDENTITY_UNBOUND}: the memory store records "
            f"{stored!r} but the host points at {expected!r}. "
            "This is what copying an instance without its binding does. "
            "Re-run with memory_bridge.adopt_data_directory = "
            '{"by": "...", "reason": "..."} to adopt the new directory once, '
            "or point memory_bridge.data_directory back at the recorded one."
        )


class InstanceBindingRefused(Exception):
    """An adoption was asked for but cannot be performed safely."""

    code = "IDENTITY_ADOPT_REFUSED"


def _db_path(data_directory: str | Path) -> Path:
    return Path(data_directory) / MEMORY_DB_NAME


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    # A plain path plus ``query_only`` rather than a ``file:...?mode=ro`` URI:
    # the test guard inspects the first argument of every ``sqlite3.connect``
    # as a filesystem path, and a URI string cannot be resolved to one.
    connection = sqlite3.connect(str(path))
    if read_only:
        connection.execute("PRAGMA query_only = 1")
    return connection


def normalize(directory: str | Path) -> str:
    """Return the comparison form of a directory (case-insensitive on Windows)."""

    text = os.path.expanduser(str(directory))
    absolute = os.path.abspath(text)
    return os.path.normcase(absolute)


def _directory(directory: str | Path) -> str:
    """The exact string the upstream store compares against ``instance_meta``.

    ``scope_recall`` compares the stored text with
    ``os.path.normcase(os.path.abspath(...))`` of the configured binding using
    SQL equality, which is case sensitive even on Windows.  Writing the raw
    absolute path (``<drive>:/...`` with an upper-case drive) while the upstream compares the normcased form
    (``<drive>:/...`` lower-cased) leaves a store that starts fine and then refuses every recall
    with a bare ``IDENTITY_UNBOUND``.  Keep the two spellings identical.
    """

    return os.path.normcase(os.path.abspath(os.fspath(directory)))


def read_recorded_directory(data_directory: str | Path) -> str | None:
    """Return the directory recorded in ``instance_meta``, or ``None``.

    ``None`` means "no binding to compare" — either the database does not exist
    yet (a fresh instance) or it has no meta row (a store that upstream has not
    initialised).  Callers must treat that as "nothing to verify", never as
    "matches".
    """

    path = _db_path(data_directory)
    if not path.is_file():
        return None
    try:
        connection = _connect(path, read_only=True)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            f"SELECT data_directory FROM {_META_TABLE} WHERE singleton = 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if row is None or not isinstance(row[0], str):
        return None
    return row[0]


def _append_audit(directory: Path, record: Mapping[str, Any]) -> None:
    path = directory / ADOPTION_LOG_NAME
    line = json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def adopt(
    data_directory: str | Path,
    *,
    by: str,
    reason: str,
    now: str | None = None,
) -> dict[str, Any]:
    """Point an existing memory store at ``data_directory``, once.

    Refused when the store already matches (nothing to adopt) or when the
    adoption would be unattributable (empty ``by``/``reason``).
    """

    if not by.strip():
        raise InstanceBindingRefused("an adoption must name who performed it")
    if not reason.strip():
        raise InstanceBindingRefused("an adoption must record why it was needed")

    directory = Path(data_directory)
    path = _db_path(directory)
    if not path.is_file():
        raise InstanceBindingRefused(
            f"there is no {MEMORY_DB_NAME} under {str(directory)!r} to adopt"
        )

    expected = _directory(directory)
    recorded = read_recorded_directory(directory)
    if recorded is None:
        raise InstanceBindingRefused(
            "the memory store has no instance_meta row; nothing to adopt"
        )
    if normalize(recorded) == normalize(expected):
        raise InstanceBindingRefused(
            "the memory store already points at this directory; adoption is not needed"
        )

    connection = _connect(path, read_only=False)
    try:
        with connection:
            connection.execute(
                f"UPDATE {_META_TABLE} SET data_directory = ? WHERE singleton = 1",
                (expected,),
            )
        row = connection.execute(
            f"SELECT data_directory FROM {_META_TABLE} WHERE singleton = 1"
        ).fetchone()
    finally:
        connection.close()

    record = {
        "adopted_at": now or _utc_now(),
        "adopted_by": by,
        "adopt_reason": reason,
        "memory_db": str(path),
        "old_data_directory": recorded,
        "new_data_directory": expected,
        "kind": "memory_instance_binding_adoption",
    }
    _append_audit(directory, record)
    record["recorded_after"] = None if row is None else row[0]
    return record


def ensure_binding(
    data_directory: str | Path,
    *,
    adopt_by: str | None = None,
    adopt_reason: str | None = None,
    now: str | None = None,
) -> dict[str, Any] | None:
    """Verify the store belongs here; adopt only when explicitly declared.

    Returns the adoption record when one happened, otherwise ``None``.  Raises
    :class:`InstanceBindingMismatch` when the store belongs elsewhere and no
    adoption was declared, so a copy fails at startup instead of at the first
    recall.
    """

    recorded = read_recorded_directory(data_directory)
    if recorded is None:
        return None
    expected = _directory(data_directory)
    if recorded == expected:
        return None
    if normalize(recorded) == normalize(expected):
        # Same directory, different spelling (the drive letter's case differs
        # on Windows).  The identity did not change, so no adoption is needed,
        # but the raw string still has to match what the upstream compares or
        # every recall fails with a bare ``IDENTITY_UNBOUND``.
        return _canonicalise(data_directory, recorded, expected, now=now)
    if adopt_by is None or adopt_reason is None:
        raise InstanceBindingMismatch(
            recorded, expected, data_directory=str(data_directory)
        )
    return adopt(data_directory, by=adopt_by, reason=adopt_reason, now=now)


def _canonicalise(
    data_directory: str | Path,
    recorded: str,
    expected: str,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Rewrite a same-directory spelling difference; never changes identity."""

    directory = Path(data_directory)
    path = _db_path(directory)
    connection = _connect(path, read_only=False)
    try:
        with connection:
            connection.execute(
                f"UPDATE {_META_TABLE} SET data_directory = ? WHERE singleton = 1",
                (expected,),
            )
    finally:
        connection.close()

    record = {
        "adopted_at": now or _utc_now(),
        "adopted_by": "host",
        "adopt_reason": "the recorded directory differed only in spelling",
        "memory_db": str(path),
        "old_data_directory": recorded,
        "new_data_directory": expected,
        "kind": "memory_instance_binding_canonicalisation",
    }
    _append_audit(directory, record)
    return record


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "ADOPTION_LOG_NAME",
    "IDENTITY_UNBOUND",
    "InstanceBindingMismatch",
    "InstanceBindingRefused",
    "MEMORY_DB_NAME",
    "adopt",
    "ensure_binding",
    "normalize",
    "read_recorded_directory",
]
