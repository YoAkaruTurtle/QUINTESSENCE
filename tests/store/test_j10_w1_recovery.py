"""J10 W1: the independent deletion ledger and the restore gate.

Every case builds a real ``ControlStore`` in a temporary directory with a real
ledger directory and a real anchor directory, so a "restore" is an actual file
copy and a reopen, not a mock.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from support import RECORDED_AT, append  # noqa: E402

from quintessence_pcs.foundation.errors import (  # noqa: E402
    AuthorizationRequired,
    InvalidInput,
    Redacted,
)
from quintessence_pcs.store.control import ControlStore  # noqa: E402
from quintessence_pcs.store.deletion import (  # noqa: E402
    DELETION_CLEANED,
    DELETION_PENDING_CLEANUP,
)
from quintessence_pcs.store.deletion_ledger import DeletionLedger  # noqa: E402
from quintessence_pcs.store.recovery import (  # noqa: E402
    REASON_ANCHOR_MISSING,
    REASON_BEFORE_BASELINE,
    REASON_LEDGER_CORRUPT,
    REASON_LEDGER_MISSING,
    REASON_LEDGER_STALE,
    REASON_SAME_POINT,
    RestoreBlocked,
    read_json,
)

NS = "example.test.j10"
NS_B = "example.test.j10.other"
RECORD_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"
KIND = "Observation"
AUTH = "auth:j10-w1"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="j10-w1-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # The instance directory is what a backup would snapshot; the ledger and
        # the anchor deliberately live outside it.
        self.instance_dir = self.root / "instance"
        self.instance_dir.mkdir()
        self.db_path = self.instance_dir / "pcs.sqlite"
        self.ledger_dir = self.root / "ledger"
        self.anchor_dir = self.root / "anchors"
        self.store = self.open_store()

    # ------------------------------------------------------------------ util

    def open_store(self, db_path: Path | None = None) -> ControlStore:
        store = ControlStore.open(
            db_path or self.db_path,
            deletion_ledger_dir=self.ledger_dir,
            deletion_anchor_dir=self.anchor_dir,
        )
        self.addCleanup(lambda: store.close())
        return store

    def record(self, record_id: str = RECORD_ID, namespace: str = NS, body: object = None):
        return append(
            self.store,
            namespace=namespace,
            record_id=record_id,
            version=1,
            body={"text": "secret-body"} if body is None else body,
        )

    def delete(self, record_id: str = RECORD_ID, namespace: str = NS, auth: str = AUTH):
        return self.store.request_deletion(
            target_kind="record",
            namespace=namespace,
            target_id=record_id,
            authorization_ref=auth,
        )

    def snapshot(self, name: str) -> Path:
        target = self.root / name
        destination = sqlite3.connect(str(target))
        try:
            self.store.connection.backup(destination)
        finally:
            destination.close()
        return target

    def restore_db(self, snapshot: Path) -> None:
        self.store.close()
        shutil.copyfile(snapshot, self.db_path)

    def visible(self, store: ControlStore, record_id: str = RECORD_ID, namespace: str = NS) -> bool:
        try:
            store.get_current(namespace, record_id)
        except Redacted:
            return False
        return True

    def ledger(self) -> DeletionLedger:
        return DeletionLedger(self.ledger_dir)

    def epochs(self, store: ControlStore) -> dict[str, int]:
        rows = store.connection.execute(
            "SELECT namespace, privacy_epoch FROM namespace_state"
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}


class LedgerShapeTests(_Base):
    def test_the_ledger_lives_outside_the_database_directory(self) -> None:
        self.record()
        self.delete()

        self.assertTrue(self.ledger_dir.exists())
        self.assertFalse(self.ledger_dir.is_relative_to(self.db_path.parent))

    def test_a_prewrite_carries_identity_authorization_and_a_hash_chain(self) -> None:
        self.record()
        self.delete()

        entries = self.ledger().entries()
        prewrites = [entry for entry in entries if entry.is_prewrite]
        self.assertEqual(1, len(prewrites))
        entry = prewrites[0]
        self.assertEqual(NS, entry.namespace)
        self.assertEqual("record", entry.target_kind)
        self.assertEqual(RECORD_ID, entry.target_id)
        self.assertEqual(AUTH, entry.authorization_ref)
        self.assertTrue(entry.operation_id)
        self.assertEqual(1, entry.privacy_epoch)
        # the confirm line follows the prewrite
        self.assertEqual(2, len(entries))
        self.assertEqual(entry.operation_id, entries[1].operation_id)

    def test_duplicate_delete_writes_no_second_prewrite(self) -> None:
        self.record()
        self.delete()
        self.delete()

        entries = self.ledger().entries()
        self.assertEqual(1, len([e for e in entries if e.is_prewrite]))
        self.assertEqual(1, self.epochs(self.store)[NS])

    def test_authorization_is_checked_before_the_ledger_sees_anything(self) -> None:
        self.record()
        with self.assertRaises(AuthorizationRequired):
            self.delete(auth="   ")

        self.assertTrue(not (self.ledger_dir / "ledger.jsonl").exists()
                        or not self.ledger().entries())
        self.assertEqual(0, self.epochs(self.store).get(NS, 0))

    def test_a_failed_prewrite_refuses_the_deletion(self) -> None:
        # The ledger works at open time and is then made unwritable, so the
        # failure lands on the prewrite instead of on the gate evaluation.
        store = ControlStore.open(
            self.root / "broken.sqlite",
            deletion_ledger_dir=self.ledger_dir,
            deletion_anchor_dir=self.anchor_dir,
        )
        self.addCleanup(store.close)
        append(store, namespace=NS, record_id=OTHER_ID, version=1, body={"text": "x"})
        shutil.rmtree(self.ledger_dir, ignore_errors=True)
        (self.ledger_dir).write_text("not a directory", encoding="utf-8")

        with self.assertRaises(InvalidInput):
            store.request_deletion(
                target_kind="record",
                namespace=NS,
                target_id=OTHER_ID,
                authorization_ref=AUTH,
            )

        rows = store.connection.execute("SELECT COUNT(*) FROM deletion_targets").fetchone()
        self.assertEqual(0, int(rows[0]))

    def test_a_broken_ledger_closes_the_gate_instead_of_deleting(self) -> None:
        # A ledger that cannot even be read at open time must keep the instance
        # closed: an unavailable ledger is not a licence to delete unrecorded.
        broken = self.root / "broken-ledger"
        broken.write_text("not a directory", encoding="utf-8")
        store = ControlStore.open(
            self.root / "broken.sqlite",
            deletion_ledger_dir=broken,
            deletion_anchor_dir=self.anchor_dir,
        )
        self.addCleanup(store.close)

        self.assertFalse(store.restore_decision.is_open)
        self.assertEqual(REASON_LEDGER_MISSING, store.restore_decision.reason)
        with self.assertRaises(RestoreBlocked):
            store.request_deletion(
                target_kind="record",
                namespace=NS,
                target_id=OTHER_ID,
                authorization_ref=AUTH,
            )


class ReplayTests(_Base):
    def test_prewrite_without_a_commit_is_replayed_on_the_next_open(self) -> None:
        self.record()
        epoch = self.epochs(self.store).get(NS, 0)
        self.store.deletion_ledger.prewrite(
            operation_id=uuid.uuid4().hex,
            namespace=NS,
            target_kind="record",
            target_id=RECORD_ID,
            target_version=0,
            authorization_ref=AUTH,
            privacy_epoch=epoch + 1,
        )
        self.assertEqual(epoch, self.epochs(self.store)[NS])

        self.store.close()
        reopened = self.open_store()

        self.assertTrue(reopened.restore_decision.is_open)
        self.assertFalse(self.visible(reopened))
        self.assertEqual(epoch + 1, self.epochs(reopened)[NS])

    def test_an_orphan_prewrite_for_a_missing_target_does_not_block_recovery(self) -> None:
        self.record(record_id=OTHER_ID)
        ledger = self.ledger()
        ledger.ensure_ready(NS, 0)
        ledger.prewrite(
            operation_id=uuid.uuid4().hex,
            namespace=NS,
            target_kind="record",
            target_id="99999999-9999-4999-8999-999999999999",
            target_version=0,
            authorization_ref=AUTH,
            privacy_epoch=1,
        )

        self.store.close()
        reopened = self.open_store()

        self.assertTrue(reopened.restore_decision.is_open)
        self.assertTrue(self.visible(reopened, OTHER_ID))

    def test_pre_deletion_snapshot_does_not_resurrect_the_content(self) -> None:
        self.record()
        backup = self.snapshot("before.sqlite")

        self.delete()
        self.assertFalse(self.visible(self.store))
        self.restore_db(backup)

        restored = self.open_store()
        self.assertTrue(restored.restore_decision.is_open)
        self.assertFalse(self.visible(restored))
        status = restored.connection.execute(
            "SELECT status FROM deletion_targets WHERE target_id = ?", (RECORD_ID,)
        ).fetchone()
        self.assertIsNotNone(status)
        self.assertEqual(DELETION_CLEANED, str(status[0]))

    def test_post_deletion_snapshot_reopens_without_a_replay_error(self) -> None:
        self.record()
        self.delete()
        backup = self.snapshot("after.sqlite")
        self.restore_db(backup)

        restored = self.open_store()

        self.assertTrue(restored.restore_decision.is_open)
        self.assertFalse(self.visible(restored))

    def test_a_legitimate_deletion_does_not_lock_the_gate(self) -> None:
        self.record()
        self.delete()
        self.store.close()

        reopened = self.open_store()
        self.assertTrue(reopened.restore_decision.is_open)

        append(reopened, namespace=NS, record_id=OTHER_ID, version=1, body={"text": "y"})
        reopened.request_deletion(
            target_kind="record", namespace=NS, target_id=OTHER_ID, authorization_ref=AUTH
        )
        reopened.close()
        again = self.open_store()
        self.assertTrue(again.restore_decision.is_open)
        self.assertEqual(2, self.epochs(again)[NS])


class FailClosedTests(_Base):
    def test_a_missing_ledger_closes_the_gate_and_blocks_reads(self) -> None:
        self.record()
        self.delete()
        self.store.close()
        shutil.rmtree(self.ledger_dir)

        reopened = self.open_store()

        self.assertFalse(reopened.restore_decision.is_open)
        self.assertEqual(REASON_LEDGER_MISSING, reopened.restore_decision.reason)
        with self.assertRaises(RestoreBlocked):
            reopened.get_current(NS, RECORD_ID)

    def test_a_truncated_ledger_is_detected(self) -> None:
        self.record()
        self.delete()
        self.store.close()
        ledger_path = self.ledger_dir / "ledger.jsonl"
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
        ledger_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

        reopened = self.open_store()

        self.assertFalse(reopened.restore_decision.is_open)
        self.assertEqual(REASON_LEDGER_CORRUPT, reopened.restore_decision.reason)

    def test_a_ledger_behind_the_database_is_refused(self) -> None:
        self.record()
        empty_ledger = self.root / "empty-ledger"
        empty_ledger.mkdir()
        (empty_ledger / "ledger.jsonl").write_text("", encoding="utf-8")
        (empty_ledger / "ledger.seal").write_text(
            json.dumps({"count": 0, "head": "0" * 64}), encoding="utf-8"
        )
        (empty_ledger / "baseline.json").write_text(
            json.dumps({"version": 1, "namespaces": {NS: 0}}), encoding="utf-8"
        )
        self.delete()
        self.store.close()
        shutil.rmtree(self.ledger_dir)
        shutil.copytree(empty_ledger, self.ledger_dir)

        reopened = self.open_store()

        self.assertFalse(reopened.restore_decision.is_open)
        self.assertIn(
            reopened.restore_decision.reason, (REASON_LEDGER_STALE, REASON_SAME_POINT)
        )

    def test_a_snapshot_before_the_ledger_baseline_is_refused(self) -> None:
        self.record()
        backup = self.snapshot("old.sqlite")
        # A ledger that only starts tracking after this snapshot existed.
        self.delete()
        self.store.close()
        baseline_file = self.ledger_dir / "baseline.json"
        payload = json.loads(baseline_file.read_text(encoding="utf-8"))
        payload["namespaces"][NS] = 5
        baseline_file.write_text(json.dumps(payload), encoding="utf-8")

        self.restore_db(backup)
        reopened = self.open_store()

        self.assertFalse(reopened.restore_decision.is_open)
        self.assertEqual(REASON_BEFORE_BASELINE, reopened.restore_decision.reason)

    def test_a_same_point_rollback_is_refused(self) -> None:
        self.record()
        self.delete()
        db_backup = self.snapshot("point1.sqlite")
        ledger_backup = self.root / "ledger-point1"
        shutil.copytree(self.ledger_dir, ledger_backup)

        append(self.store, namespace=NS, record_id=OTHER_ID, version=1, body={"text": "y"})
        self.store.request_deletion(
            target_kind="record", namespace=NS, target_id=OTHER_ID, authorization_ref=AUTH
        )
        self.store.close()

        # Roll *both* sides back to the earlier point: the anchor still knows 2.
        shutil.copyfile(db_backup, self.db_path)
        shutil.rmtree(self.ledger_dir)
        shutil.copytree(ledger_backup, self.ledger_dir)

        reopened = self.open_store()

        self.assertFalse(reopened.restore_decision.is_open)
        self.assertEqual(REASON_SAME_POINT, reopened.restore_decision.reason)

    def test_a_missing_anchor_with_existing_data_is_refused(self) -> None:
        self.record()
        self.delete()
        self.store.close()
        shutil.rmtree(self.anchor_dir)

        reopened = self.open_store()

        self.assertFalse(reopened.restore_decision.is_open)
        self.assertEqual(REASON_ANCHOR_MISSING, reopened.restore_decision.reason)

    def test_an_open_gate_from_an_older_point_is_treated_as_stale(self) -> None:
        self.record()
        self.delete()
        gate = read_json(self.db_path.parent / "restore-gate.json")
        self.assertEqual("open", gate["status"])
        gate["ledger_head_hash"] = "f" * 64
        gate["ledger_max_epoch"] = {NS: 99}
        (self.db_path.parent / "restore-gate.json").write_text(
            json.dumps(gate), encoding="utf-8"
        )
        self.store.close()

        reopened = self.open_store()

        self.assertFalse(reopened.restore_decision.is_open)


class CrossNamespaceTests(_Base):
    def test_one_namespace_does_not_block_the_other_after_a_restore(self) -> None:
        self.record(record_id=RECORD_ID, namespace=NS)
        self.record(record_id=OTHER_ID, namespace=NS_B)
        backup = self.snapshot("both.sqlite")

        self.delete(RECORD_ID, NS)
        self.delete(OTHER_ID, NS_B)
        self.restore_db(backup)

        restored = self.open_store()

        self.assertTrue(restored.restore_decision.is_open)
        self.assertFalse(self.visible(restored, RECORD_ID, NS))
        self.assertFalse(self.visible(restored, OTHER_ID, NS_B))
        epochs = self.epochs(restored)
        self.assertEqual(1, epochs[NS])
        self.assertEqual(1, epochs[NS_B])


class GateCoverageTests(_Base):
    """AC-12: every guarded entry refuses work while the gate is closed."""

    def setUp(self) -> None:
        super().setUp()
        from quintessence_pcs.store.recovery import RestoreDecision

        self.store.restore_decision = RestoreDecision(
            "closed", "restore_ledger_missing", {}, "0" * 64
        )

    def test_record_reads_are_blocked(self) -> None:
        with self.assertRaises(RestoreBlocked):
            self.store.get_current(NS, RECORD_ID)

    def test_context_build_is_blocked(self) -> None:
        from quintessence_pcs.runtime.context import ContextService

        contexts = ContextService(self.store)
        with self.assertRaises(RestoreBlocked):
            contexts.build_context(namespace=NS, trigger_ref={"kind": "event", "id": "x"})

    def test_maintenance_slice_is_blocked(self) -> None:
        from quintessence_pcs.store.maintenance import MaintenanceStore, run_slice

        maintenance = MaintenanceStore(self.store)
        with self.assertRaises(RestoreBlocked):
            run_slice(
                maintenance,
                namespace=NS,
                task_id="task-1",
                worker=lambda budget: {},
                guard=lambda targets: None,
            )

    def test_memory_delivery_is_blocked(self) -> None:
        from quintessence_pcs.domains.memory.maintenance_delivery import deliver_page

        with self.assertRaises(RestoreBlocked):
            deliver_page(
                self.store,
                namespace=NS,
                event_ids=["e" * 8],
                scope={"scene": NS},
                authorization_ref=AUTH,
            )

    def test_memory_search_is_blocked(self) -> None:
        from quintessence_pcs.domains.memory.scope_recall_search import (
            search_scope_recall,
        )

        with self.assertRaises(RestoreBlocked):
            search_scope_recall(
                object(), query="q", scope_id="scope", guard=self.store.require_ready
            )


class PrewriteOrderingTests(_Base):
    """AC-03: the ledger entry exists before the database barrier does."""

    def test_the_ledger_is_written_before_the_barrier(self) -> None:
        self.record()
        ledger = self.store.deletion_ledger
        observed: list[int] = []

        original = ledger.prewrite

        def spy(**kwargs):
            rows = self.store.connection.execute(
                "SELECT COUNT(*) FROM deletion_targets"
            ).fetchone()
            observed.append(int(rows[0]))
            return original(**kwargs)

        ledger.prewrite = spy  # type: ignore[method-assign]
        try:
            self.delete()
        finally:
            ledger.prewrite = original  # type: ignore[method-assign]

        self.assertEqual([0], observed)

    def test_replay_also_reclears_the_payload(self) -> None:
        self.record()
        backup = self.snapshot("before.sqlite")
        self.delete()
        self.restore_db(backup)

        restored = self.open_store()

        self.assertFalse(self.visible(restored))
        row = restored.connection.execute(
            "SELECT status FROM deletion_targets WHERE target_id = ?", (RECORD_ID,)
        ).fetchone()
        self.assertEqual(DELETION_CLEANED, str(row[0]))
        leftovers = restored.connection.execute(
            "SELECT COUNT(*) FROM private_payloads WHERE body_json LIKE '%secret-body%'"
        ).fetchone()
        self.assertEqual(0, int(leftovers[0]))


class ToolResultInjectionTests(_Base):
    def test_injected_tool_results_cannot_be_assembled_while_closed(self) -> None:
        from quintessence_pcs.runtime.context import ContextService
        from quintessence_pcs.store.recovery import RestoreDecision

        self.store.restore_decision = RestoreDecision(
            "closed", "restore_ledger_missing", {}, "0" * 64
        )
        contexts = ContextService(self.store)
        with self.assertRaises(RestoreBlocked):
            contexts.build_consult_context(
                namespace=NS,
                access=object(),
                input_refs=(),
                tool_results=({"kind": "tool_result", "body": "x"},),
            )


class CleanupVerificationTests(_Base):
    def test_a_replay_that_leaves_the_payload_behind_keeps_the_gate_closed(self) -> None:
        from unittest import mock

        from quintessence_pcs.store import deletion as deletion_module

        self.record()
        backup = self.snapshot("before.sqlite")
        self.delete()
        self.restore_db(backup)

        with mock.patch.object(
            deletion_module.DeletionMixin, "cleanup_deletions", lambda self, **kw: None
        ):
            store = ControlStore.open(
                self.db_path,
                deletion_ledger_dir=self.ledger_dir,
                deletion_anchor_dir=self.anchor_dir,
            )
        self.addCleanup(store.close)

        self.assertFalse(store.restore_decision.is_open)
        self.assertEqual("restore_cleanup_incomplete", store.restore_decision.reason)
