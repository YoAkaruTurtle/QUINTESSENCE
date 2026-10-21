"""Shared fixtures for the PCS2-004 storage tests.

Every test gets a fresh database in a temporary directory.  No test writes to
private instance directories and no test depends on another test's database.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from quintessence_pcs.store.records import RecordStore, RecordVersion  # noqa: E402

NAMESPACE = "example.test"
NAMESPACE_B = "example.test.other"
RECORD_ID = "22222222-2222-4222-8222-222222222222"
RECORD_ID_B = "33333333-3333-4333-8333-333333333333"
OTHER_RECORD_ID = "44444444-4444-4444-8444-444444444444"
RECORDED_AT = "2026-10-17T00:00:00Z"

BODIES: dict[int, dict[str, Any]] = {
    1: {"text": "first"},
    2: {"text": "middle"},
    3: {"text": "latest"},
}


def append(
    store: RecordStore,
    *,
    version: int,
    body: Mapping[str, Any],
    namespace: str = NAMESPACE,
    record_id: str = RECORD_ID,
    kind: str = "Observation",
    status: str = "active",
    schema_version: str = "1",
    privacy_class: str = "private",
    recorded_at: str = RECORDED_AT,
) -> RecordVersion:
    return store.append_record_version(
        namespace=namespace,
        record_id=record_id,
        record_version=version,
        kind=kind,
        body=body,
        status=status,
        schema_version=schema_version,
        privacy_class=privacy_class,
        recorded_at=recorded_at,
    )


class StoreTestCase(unittest.TestCase):
    """Fresh migrated database per test, cleaned up afterwards."""

    #: PCS2-005 event tests reuse this harness with ``EventStore``.
    store_class = RecordStore

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pcs2-004-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.db_path = self.tmp_path / "pcs.sqlite"
        self.store = self.store_class.open(self.db_path)
        self.addCleanup(lambda: self.store.close())

    def reopen(self, **kwargs: Any) -> RecordStore:
        self.store.close()
        self.store = self.store_class.open(self.db_path, **kwargs)
        return self.store

    def seed_three_versions(self) -> tuple[str, str]:
        with self.store.transaction():
            for version in (1, 2, 3):
                append(self.store, version=version, body=BODIES[version])
        return NAMESPACE, RECORD_ID

    def count_rows(self, table: str) -> int:
        return int(
            self.store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )

    def current_pointer(self, namespace: str = NAMESPACE, record_id: str = RECORD_ID) -> int | None:
        row = self.store.connection.execute(
            "SELECT current_version FROM records WHERE namespace = ? AND record_id = ?",
            (namespace, record_id),
        ).fetchone()
        return None if row is None else int(row[0])


def all_text_cells(connection: Any) -> list[tuple[str, str]]:
    """Every non-null TEXT cell in every user table, as ``(location, value)``.

    PCS2-006 uses this to search the *whole database* for a deleted body's
    digest instead of only checking the one payload row it knows about.
    """

    tables = [
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    cells: list[tuple[str, str]] = []
    for table in tables:
        columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        text_columns = [
            row["name"] for row in columns if str(row["type"]).upper().startswith("TEXT")
        ]
        for column in text_columns:
            rows = connection.execute(
                f'SELECT rowid AS rid, "{column}" AS value FROM "{table}"'
            ).fetchall()
            for row in rows:
                if row["value"] is not None:
                    cells.append((f"{table}.{column}[rowid={row['rid']}]", str(row["value"])))
    return cells


__all__ = [
    "BODIES",
    "NAMESPACE",
    "NAMESPACE_B",
    "OTHER_RECORD_ID",
    "RECORDED_AT",
    "RECORD_ID",
    "RECORD_ID_B",
    "REPO_ROOT",
    "SRC_DIR",
    "StoreTestCase",
    "all_text_cells",
    "append",
]
