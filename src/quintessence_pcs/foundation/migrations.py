"""Numbered SQL migrations with an append-only ledger.

Rules implemented here (PCS2-004):

* migration files are ``NNNN_name.sql`` (four-digit version, lower-case name)
  discovered in one directory;
* versions are contiguous from 1; an unexpected file name, a duplicate or a
  gap is a hard error;
* ``schema_migrations`` records the version, the name and the SHA-256 of the
  exact file bytes of every applied migration;
* an applied migration may not change: a missing file or a different checksum
  stops the store from opening instead of guessing;
* each migration runs in its own transaction together with its ledger row, so
  a failed migration leaves neither half-written tables nor a ledger entry;
* the ledger table itself is created by ``0001_init.sql``; the runner only
  bootstraps *reading* it (an empty ledger without migration 1 is treated as
  corruption, never as "start from scratch").
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from .errors import MigrationError
from .sqlite_db import table_exists
from .timeutil import utc_now_rfc3339

LEDGER_TABLE = "schema_migrations"
_MIGRATION_FILENAME = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z][a-z0-9_]*)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    checksum: str
    statements: tuple[str, ...]


@dataclass(frozen=True)
class MigrationPlan:
    applied: tuple[int, ...]
    pending: tuple[Migration, ...]


def default_migrations_dir() -> Path:
    """Return the repository's ``migrations/`` directory.

    SQL is installed with the package and retains its original ledger bytes.
    Normal wheel installations are unpacked on disk; zipped imports are not
    supported by this Path-returning API. Callers may supply an explicit
    directory instead. No resource is extracted into a temporary directory.
    """

    return Path(__file__).resolve().parents[1] / "resources" / "migrations"


def split_sql_statements(sql: str) -> list[str]:
    """Split a migration file into individually executable statements.

    ``sqlite3.complete_statement`` is used so multi-line ``CREATE TRIGGER
    ... BEGIN ... END;`` blocks stay one statement.  Migration files therefore
    need one semicolon-terminated statement per block; two statements on one
    line would make the later ``execute`` fail loudly rather than split
    silently.
    """

    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        tail = "\n".join(
            line for line in buffer.splitlines() if not line.lstrip().startswith("--")
        )
        if tail.strip():
            raise MigrationError("migration ends with an incomplete SQL statement")
    return statements


def _load_migration(path: Path) -> Migration:
    match = _MIGRATION_FILENAME.fullmatch(path.name)
    if match is None:
        raise MigrationError(
            f"unexpected migration file name {path.name!r}; expected NNNN_name.sql"
        )
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MigrationError(f"migration {path.name} is not UTF-8") from exc
    statements = tuple(split_sql_statements(text))
    if not statements:
        raise MigrationError(f"migration {path.name} contains no SQL statements")
    return Migration(
        version=int(match.group("version")),
        name=match.group("name"),
        path=path,
        checksum=hashlib.sha256(raw).hexdigest(),
        statements=statements,
    )


def discover_migrations(directory: Union[str, Path, None] = None) -> tuple[Migration, ...]:
    """Load and validate every ``*.sql`` migration in *directory*."""

    root = Path(directory) if directory is not None else default_migrations_dir()
    if not root.is_dir():
        raise MigrationError(f"migration directory {root} does not exist")
    migrations: list[Migration] = []
    for path in sorted(root.iterdir()):
        if path.suffix != ".sql":
            continue
        migrations.append(_load_migration(path))
    if not migrations:
        raise MigrationError(f"migration directory {root} contains no *.sql files")
    versions = [migration.version for migration in migrations]
    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        raise MigrationError(
            f"migration versions must be contiguous starting at 1; found {versions}"
        )
    return tuple(migrations)


def _read_ledger(connection: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    rows = connection.execute(
        f"SELECT migration_id, name, checksum FROM {LEDGER_TABLE} ORDER BY migration_id"
    ).fetchall()
    return tuple(
        {
            "migration_id": int(row["migration_id"]),
            "name": str(row["name"]),
            "checksum": str(row["checksum"]),
        }
        for row in rows
    )


def inspect_migrations(
    connection: sqlite3.Connection, directory: Union[str, Path, None] = None
) -> MigrationPlan:
    """Return applied/pending migrations, verifying every applied checksum."""

    migrations = discover_migrations(directory)
    if not table_exists(connection, LEDGER_TABLE):
        return MigrationPlan(applied=(), pending=migrations)

    ledger = _read_ledger(connection)
    if not ledger:
        raise MigrationError(
            f"{LEDGER_TABLE} exists but is empty; refusing to guess the schema state"
        )

    by_version = {migration.version: migration for migration in migrations}
    for entry in ledger:
        version = entry["migration_id"]
        assert isinstance(version, int)
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(
                f"applied migration {version:04d} is missing from the migration directory"
            )
        if entry["checksum"] != migration.checksum:
            raise MigrationError(
                f"applied migration {version:04d}_{migration.name} checksum mismatch: "
                f"ledger={entry['checksum']} file={migration.checksum}"
            )
        if entry["name"] != migration.name:
            raise MigrationError(
                f"applied migration {version:04d} name mismatch: "
                f"ledger={entry['name']!r} file={migration.name!r}"
            )

    applied_versions = tuple(entry["migration_id"] for entry in ledger)
    if applied_versions != tuple(range(1, len(applied_versions) + 1)):
        raise MigrationError(
            f"applied migrations are not contiguous from 1; found {applied_versions}"
        )

    applied_set = set(applied_versions)
    pending = tuple(migration for migration in migrations if migration.version not in applied_set)
    return MigrationPlan(applied=applied_versions, pending=pending)


def _safe_rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _apply_one(connection: sqlite3.Connection, migration: Migration) -> None:
    if connection.in_transaction:
        raise MigrationError("cannot apply a migration inside an open transaction")
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in migration.statements:
            connection.execute(statement)
        connection.execute(
            f"INSERT INTO {LEDGER_TABLE} (migration_id, name, checksum, applied_at) "
            "VALUES (?, ?, ?, ?)",
            (
                migration.version,
                migration.name,
                migration.checksum,
                utc_now_rfc3339(),
            ),
        )
    except BaseException as exc:
        _safe_rollback(connection)
        if isinstance(exc, sqlite3.Error):
            raise MigrationError(
                f"migration {migration.version:04d}_{migration.name} failed: {exc}"
            ) from exc
        raise
    else:
        connection.execute("COMMIT")


def apply_migrations(
    connection: sqlite3.Connection, directory: Union[str, Path, None] = None
) -> tuple[int, ...]:
    """Apply every pending migration; return the versions applied by this call."""

    plan = inspect_migrations(connection, directory)
    applied_now: list[int] = []
    for migration in plan.pending:
        _apply_one(connection, migration)
        applied_now.append(migration.version)
    return tuple(applied_now)


__all__ = [
    "LEDGER_TABLE",
    "Migration",
    "MigrationPlan",
    "apply_migrations",
    "default_migrations_dir",
    "discover_migrations",
    "inspect_migrations",
    "split_sql_statements",
]
