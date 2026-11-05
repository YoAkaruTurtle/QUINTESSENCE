"""0015: an 'ok' model attempt may be a tool round, not only a final answer.

The J07 agent loop calls the model once per tool round.  Those calls succeed
with ``finish_reason='tool_calls'``, which the 0007 ledger refused to record as
``ok`` -- every real agent round was unsettleable and left its reservation
counted.  These tests pin the narrowed relaxation: a tool round is allowed,
while an ``ok`` row still has to carry the parsed response and no error.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _path in (_ROOT / "src", _ROOT / "tests" / "store"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.store import ControlStore  # noqa: E402

MIGRATIONS_DIR = _ROOT / "src" / "quintessence_pcs" / "resources" / "migrations"


def copy_migrations_upto(limit: int, target: Path) -> Path:
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name[:4]) <= limit:
            shutil.copy2(path, target / path.name)
    return target


AUTH_ID = "44444444-4444-4444-8444-444444444444"

_ROW = {
    "run_id": "11111111-1111-4111-8111-111111111111",
    "attempt_no": 1,
    "attempt_id": "22222222-2222-4222-8222-222222222222",
    "namespace": "example.test.model",
    "context_id": "33333333-3333-4333-8333-333333333333",
    "authorization_id": AUTH_ID,
    "authorization_ref": "synthetic",
    "manifest_hash": "sha256:" + "0" * 64,
    "provider": "deepseek",
    "model_requested": "deepseek-flash",
    "adapter_kind": "direct-http",
    "policy_version": "pcs2-009",
    "request_payload_ref": "req-1",
    "request_hash": "sha256:" + "1" * 64,
    "started_at": "2026-10-27T00:00:00Z",
}


def _seed_parents(connection: sqlite3.Connection) -> None:
    """Insert the two rows ``model_attempts`` points at."""

    connection.execute(
        "INSERT INTO model_authorizations (authorization_id, namespace,"
        " authorization_ref, provider, model, manifest_hash, max_calls,"
        " max_output_tokens_per_call, max_wall_seconds, granted_at, status,"
        " updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            AUTH_ID, "example.test.model", "synthetic", "deepseek",
            "deepseek-flash", "sha256:" + "0" * 64, 8, 512, 60,
            "2026-10-27T00:00:00Z", "active", "2026-10-27T00:00:00Z",
        ),
    )
    connection.execute(
        "INSERT INTO context_snapshots (context_id, namespace, state_revision,"
        " context_event_seq, privacy_epoch, created_at) VALUES (?,?,?,?,?,?)",
        (
            "33333333-3333-4333-8333-333333333333", "example.test.model",
            0, 0, 0, "2026-10-27T00:00:00Z",
        ),
    )


def _insert_finished(
    connection: sqlite3.Connection,
    *,
    finish_reason: str,
    outcome: str = "ok",
    response_ref: str | None = "resp-1",
) -> None:
    response_hash = "sha256:" + "2" * 64 if response_ref else None
    connection.execute(
        "INSERT INTO model_attempts ("
        "run_id, attempt_no, attempt_id, namespace, context_id, authorization_id,"
        " authorization_ref, manifest_hash, provider, model_requested,"
        " adapter_kind, policy_version, request_payload_ref, request_hash,"
        " response_payload_ref, response_hash, finish_reason, status, outcome,"
        " started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _ROW["run_id"], _ROW["attempt_no"], _ROW["attempt_id"],
            _ROW["namespace"], _ROW["context_id"], _ROW["authorization_id"],
            _ROW["authorization_ref"], _ROW["manifest_hash"], _ROW["provider"],
            _ROW["model_requested"], _ROW["adapter_kind"], _ROW["policy_version"],
            _ROW["request_payload_ref"], _ROW["request_hash"],
            response_ref, response_hash, finish_reason, "finished", outcome,
            _ROW["started_at"], "2026-10-27T00:00:05Z",
        ),
    )


class ToolRoundLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pcs-15-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _fresh(self) -> ControlStore:
        store = ControlStore.open(self.root / "fresh.sqlite")
        self.addCleanup(store.close)
        return store

    def _old(self, limit: int) -> Path:
        directory = self.root / f"m{limit}"
        directory.mkdir()
        copy_migrations_upto(limit, directory)
        return directory

    # ------------------------------------------------------------------ M15-01
    def test_m15_01_empty_database_reaches_0016(self) -> None:
        store = self._fresh()
        ledger = dict(
            store.connection.execute(
                "SELECT migration_id, name FROM schema_migrations"
            ).fetchall()
        )
        self.assertEqual(17, max(ledger))
        self.assertEqual("model_attempt_tool_rounds", ledger[15])
        self.assertEqual("maintenance_jobs", ledger[16])
        self.assertEqual("conversation_turns", ledger[17])
        indexes = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        for name in (
            "model_attempts_authorization_idx",
            "model_attempts_context_idx",
            "model_attempts_namespace_started_idx",
        ):
            self.assertIn(name, indexes)
        triggers = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        self.assertIn("model_attempts_no_delete", triggers)
        self.assertIn("model_attempts_finish_only", triggers)

    # ------------------------------------------------------------------ M15-02
    def test_m15_02_tool_round_is_a_legal_ok_row(self) -> None:
        store = self._fresh()
        store.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            _seed_parents(store.connection)
            _insert_finished(store.connection, finish_reason="tool_calls")
        finally:
            store.connection.execute("PRAGMA foreign_keys = ON")
        row = store.connection.execute(
            "SELECT outcome, finish_reason FROM model_attempts"
        ).fetchone()
        self.assertEqual(("ok", "tool_calls"), tuple(row))

    # ------------------------------------------------------------------ M15-03
    def test_m15_03_stop_is_still_legal(self) -> None:
        store = self._fresh()
        store.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            _seed_parents(store.connection)
            _insert_finished(store.connection, finish_reason="stop")
        finally:
            store.connection.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(
            ("ok", "stop"),
            tuple(store.connection.execute(
                "SELECT outcome, finish_reason FROM model_attempts").fetchone()),
        )

    # ------------------------------------------------------------------ M15-04
    def test_m15_04_missing_response_payload_cannot_be_ok(self) -> None:
        store = self._fresh()
        store.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            _seed_parents(store.connection)
            with self.assertRaises(sqlite3.IntegrityError):
                _insert_finished(
                    store.connection, finish_reason="stop", response_ref=None
                )
        finally:
            store.connection.execute("PRAGMA foreign_keys = ON")

    # ------------------------------------------------------------------ M15-05
    def test_m15_05_unknown_finish_reason_cannot_be_ok(self) -> None:
        store = self._fresh()
        store.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            _seed_parents(store.connection)
            with self.assertRaises(sqlite3.IntegrityError):
                _insert_finished(store.connection, finish_reason="length")
        finally:
            store.connection.execute("PRAGMA foreign_keys = ON")

    # ------------------------------------------------------------------ M15-06
    def test_m15_06_upgrade_preserves_existing_rows(self) -> None:
        old_dir = self._old(14)
        old_path = self.root / "old.sqlite"
        old = ControlStore.open(old_path, migrations_dir=old_dir)
        old.connection.execute("PRAGMA foreign_keys = OFF")
        _seed_parents(old.connection)
        _insert_finished(old.connection, finish_reason="stop")
        before = old.connection.execute(
            "SELECT run_id, attempt_id, outcome, finish_reason, started_at "
            "FROM model_attempts"
        ).fetchall()
        old.close()

        upgraded = ControlStore.open(old_path)
        self.addCleanup(upgraded.close)
        after = upgraded.connection.execute(
            "SELECT run_id, attempt_id, outcome, finish_reason, started_at "
            "FROM model_attempts"
        ).fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])
        self.assertEqual(
            17,
            upgraded.connection.execute(
                "SELECT MAX(migration_id) FROM schema_migrations"
            ).fetchone()[0],
        )

    # ------------------------------------------------------------------ M15-07
    def test_m15_07_foreign_keys_and_integrity_hold(self) -> None:
        store = self._fresh()
        self.assertEqual([], store.connection.execute("PRAGMA foreign_key_check").fetchall())
        self.assertEqual(
            "ok",
            store.connection.execute("PRAGMA integrity_check").fetchone()[0],
        )

    # ------------------------------------------------------------------ M15-08
    def test_m15_08_audit_triggers_survive_the_rebuild(self) -> None:
        store = self._fresh()
        store.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            _seed_parents(store.connection)
            _insert_finished(store.connection, finish_reason="tool_calls")
        finally:
            store.connection.execute("PRAGMA foreign_keys = ON")
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("DELETE FROM model_attempts")
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute(
                "UPDATE model_attempts SET finish_reason = 'stop'"
            )
