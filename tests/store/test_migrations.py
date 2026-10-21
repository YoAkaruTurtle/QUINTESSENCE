"""Migration-ledger behaviour for the PCS2-004 store.

These tests prove that numbered SQL migrations are applied once, recorded
with a checksum, never rewritten, and applied atomically.  They complement
the record/version scenarios in ``test_record_store.py``.

PCS2-005 added ``0002_events.sql``; the expectations here are derived from the
migration directory instead of hard-coding "there is exactly one migration", so
a new migration does not silently weaken the ledger checks.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from support import StoreTestCase  # noqa: E402

from quintessence_pcs.foundation.errors import MigrationError, MigrationRequired  # noqa: E402
from quintessence_pcs.foundation.migrations import (  # noqa: E402
    default_migrations_dir,
    discover_migrations,
    split_sql_statements,
)
from quintessence_pcs.store.records import RecordStore  # noqa: E402

INIT_SQL = default_migrations_dir() / "0001_init.sql"
MIGRATIONS = discover_migrations(default_migrations_dir())
MIGRATION_VERSIONS = [migration.version for migration in MIGRATIONS]
LATEST_VERSION = MIGRATION_VERSIONS[-1]


class MigrationLedgerTests(StoreTestCase):
    def copy_migrations(self, name: str = "migrations-copy") -> Path:
        target = self.tmp_path / name
        shutil.copytree(default_migrations_dir(), target)
        return target

    def add_migration(self, directory: Path, name: str, text: str) -> Path:
        """Write a synthetic migration after the newest shipped one."""

        path = directory / f"{LATEST_VERSION + 1:04d}_{name}.sql"
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def ledger_rows(self, store: RecordStore | None = None) -> list[sqlite3.Row]:
        active = store if store is not None else self.store
        return active.connection.execute(
            "SELECT migration_id, name, checksum, applied_at "
            "FROM schema_migrations ORDER BY migration_id"
        ).fetchall()

    def table_names(self, store: RecordStore | None = None) -> set[str]:
        active = store if store is not None else self.store
        rows = active.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {row["name"] for row in rows}

    def test_fresh_database_has_the_schema_and_one_ledger_row_per_migration(self) -> None:
        self.assertTrue(
            {
                "schema_migrations",
                "namespace_state",
                "private_payloads",
                "records",
                "record_versions",
            }.issubset(self.table_names())
        )
        rows = self.ledger_rows()
        self.assertEqual(MIGRATION_VERSIONS, [row["migration_id"] for row in rows])
        self.assertEqual(
            [migration.name for migration in MIGRATIONS], [row["name"] for row in rows]
        )
        self.assertEqual(
            [migration.checksum for migration in MIGRATIONS],
            [row["checksum"] for row in rows],
        )
        self.assertEqual(
            hashlib.sha256(INIT_SQL.read_bytes()).hexdigest(), rows[0]["checksum"]
        )
        for row in rows:
            self.assertRegex(row["applied_at"], r"^(?:19|20)[0-9]{2}-.*Z$")
        self.assertEqual(
            1, self.store.connection.execute("PRAGMA foreign_keys").fetchone()[0]
        )

    def test_reopen_does_not_reapply_or_rewrite_the_ledger(self) -> None:
        before = self.ledger_rows()
        self.reopen()
        after = self.ledger_rows()
        self.assertEqual(len(before), len(after))
        self.assertEqual(
            [(row["migration_id"], row["checksum"], row["applied_at"]) for row in before],
            [(row["migration_id"], row["checksum"], row["applied_at"]) for row in after],
        )

        # The same migration files from a copied directory are accepted.
        copied = self.copy_migrations()
        self.reopen(migrations_dir=copied)
        self.assertEqual(len(MIGRATIONS), len(self.ledger_rows()))

    def test_modified_applied_migration_is_rejected(self) -> None:
        copied = self.copy_migrations()
        path = copied / "0001_init.sql"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n-- edited after it was applied\n",
            encoding="utf-8",
            newline="\n",
        )
        self.store.close()
        with self.assertRaises(MigrationError) as caught:
            RecordStore.open(self.db_path, migrations_dir=copied)
        self.assertIn("checksum mismatch", str(caught.exception))

    def test_missing_applied_migration_is_rejected(self) -> None:
        copied = self.copy_migrations()
        second = self.add_migration(
            copied, "marker", "CREATE TABLE migration_marker (id INTEGER PRIMARY KEY);\n"
        )
        self.reopen(migrations_dir=copied)
        second.unlink()

        self.store.close()
        with self.assertRaises(MigrationError) as caught:
            RecordStore.open(self.db_path, migrations_dir=copied)
        self.assertIn("missing", str(caught.exception))

    def test_pending_migration_is_applied_once(self) -> None:
        copied = self.copy_migrations()
        second = self.add_migration(
            copied, "marker", "CREATE TABLE migration_marker (id INTEGER PRIMARY KEY);\n"
        )
        self.reopen(migrations_dir=copied)
        self.assertIn("migration_marker", self.table_names())
        rows = self.ledger_rows()
        self.assertEqual(MIGRATION_VERSIONS + [LATEST_VERSION + 1], [row["migration_id"] for row in rows])
        self.assertEqual(hashlib.sha256(second.read_bytes()).hexdigest(), rows[-1]["checksum"])

        self.reopen(migrations_dir=copied)
        self.assertEqual(len(MIGRATIONS) + 1, len(self.ledger_rows()))

    def test_failed_migration_rolls_back_and_leaves_the_database_usable(self) -> None:
        copied = self.copy_migrations()
        broken = self.add_migration(
            copied, "broken", "CREATE TABLE half_written (id INTEGER PRIMARY KEY);\nCREATE TABLE (;\n"
        )
        other_db = self.tmp_path / "atomic.sqlite"
        with self.assertRaises(MigrationError) as caught:
            RecordStore.open(other_db, migrations_dir=copied)
        self.assertIn(f"{LATEST_VERSION + 1:04d}_broken", str(caught.exception))

        # Retrying with the original migration directory shows that the shipped
        # migrations are committed and the broken one left nothing behind.
        store = RecordStore.open(other_db)
        self.addCleanup(store.close)
        self.assertIn("schema_migrations", self.table_names(store))
        self.assertNotIn("half_written", self.table_names(store))
        rows = self.ledger_rows(store)
        self.assertEqual(MIGRATION_VERSIONS, [row["migration_id"] for row in rows])

    def test_apply_migrations_false_reports_pending_work(self) -> None:
        other_db = self.tmp_path / "deferred.sqlite"
        with self.assertRaises(MigrationRequired) as caught:
            RecordStore.open(other_db, apply_migrations=False)
        self.assertEqual("MIGRATION_REQUIRED", caught.exception.code)

        migrated = RecordStore.open(other_db)
        self.addCleanup(migrated.close)
        inspected = RecordStore.open(other_db, apply_migrations=False)
        self.addCleanup(inspected.close)
        self.assertEqual(len(MIGRATIONS), len(self.ledger_rows(inspected)))

    def test_ledger_is_append_only(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError) as updated:
            self.store.connection.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE migration_id = 1",
                ("0" * 64,),
            )
        self.assertIn("append-only", str(updated.exception))

        with self.assertRaises(sqlite3.IntegrityError) as deleted:
            self.store.connection.execute("DELETE FROM schema_migrations")
        self.assertIn("append-only", str(deleted.exception))

        rows = self.ledger_rows()
        self.assertEqual(MIGRATION_VERSIONS, [row["migration_id"] for row in rows])
        self.assertEqual(
            hashlib.sha256(INIT_SQL.read_bytes()).hexdigest(), rows[0]["checksum"]
        )


class MigrationDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="pcs2-004-migrations-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def test_repository_migrations_are_lf_and_parse_into_statements(self) -> None:
        for migration in MIGRATIONS:
            with self.subTest(migration=migration.version):
                raw = migration.path.read_bytes()
                self.assertNotIn(b"\r", raw)
                self.assertTrue(migration.statements)
        self.assertEqual(
            tuple(range(1, LATEST_VERSION + 1)),
            tuple(migration.version for migration in MIGRATIONS),
        )
        statements = split_sql_statements(INIT_SQL.read_bytes().decode("utf-8"))
        self.assertTrue(any("CREATE TABLE records" in item for item in statements))
        self.assertTrue(any("CREATE TRIGGER record_versions_no_update" in item for item in statements))

    def test_empty_directory_is_rejected(self) -> None:
        with self.assertRaises(MigrationError):
            discover_migrations(self.root)

    def test_versions_must_be_contiguous_from_one(self) -> None:
        self.write("0001_a.sql", "CREATE TABLE a (id INTEGER PRIMARY KEY);\n")
        self.write("0003_c.sql", "CREATE TABLE c (id INTEGER PRIMARY KEY);\n")
        with self.assertRaises(MigrationError) as caught:
            discover_migrations(self.root)
        self.assertIn("contiguous", str(caught.exception))

    def test_duplicate_version_numbers_are_rejected(self) -> None:
        self.write("0001_a.sql", "CREATE TABLE a (id INTEGER PRIMARY KEY);\n")
        self.write("0001_b.sql", "CREATE TABLE b (id INTEGER PRIMARY KEY);\n")
        with self.assertRaises(MigrationError):
            discover_migrations(self.root)

    def test_unexpected_file_name_is_rejected(self) -> None:
        self.write("1_init.sql", "CREATE TABLE a (id INTEGER PRIMARY KEY);\n")
        with self.assertRaises(MigrationError) as caught:
            discover_migrations(self.root)
        self.assertIn("NNNN_name.sql", str(caught.exception))

    def test_non_sql_files_are_ignored(self) -> None:
        self.write("README.md", "notes\n")
        self.write("0001_a.sql", "CREATE TABLE a (id INTEGER PRIMARY KEY);\n")
        migrations = discover_migrations(self.root)
        self.assertEqual((1,), tuple(migration.version for migration in migrations))

    def test_incomplete_trailing_statement_is_rejected(self) -> None:
        self.write("0001_a.sql", "CREATE TABLE a (id INTEGER PRIMARY KEY)\n")
        with self.assertRaises(MigrationError):
            discover_migrations(self.root)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
