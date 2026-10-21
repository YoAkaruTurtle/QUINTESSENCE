"""PCS2-006 A group: Tx B, snapshots, watermarks and idempotent commands.

Every test builds its own temporary database and uses ``ControlStore``, the
PCS2-006 composition of the record, event and control layers.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from support import (  # noqa: E402
    RECORDED_AT,
    RECORD_ID,
    REPO_ROOT,
    StoreTestCase,
    append,
)

from quintessence_pcs.foundation import identifiers  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    CommandConflict,
    ContextStale,
    CorruptStore,
    DbBusy,
    InvalidInput,
    ScopeViolation,
    SnapshotNotFound,
    StandaloneTransactionRequired,
    StateRevisionConflict,
)
from quintessence_pcs.foundation.jsonutil import canonical_json_text, sha256_hex  # noqa: E402
from quintessence_pcs.store.control import ControlStore  # noqa: E402
from quintessence_pcs.store.tx_b import (  # noqa: E402
    OUTCOME_COMMITTED,
    OUTCOME_NOOP,
    CommitReceipt,
)

NS = "example.test.txb"
NS_B = "example.test.txb.other"
OPERATION = "proposal.submit"
PAYLOAD = {"kind": "Observation", "body": {"text": "first"}}
OTHER_RECORD_ID = "55555555-5555-4555-8555-555555555555"


def new_request_id() -> str:
    return str(uuid.uuid4())


class TxBTestCase(StoreTestCase):
    store_class = ControlStore

    def ingest(self, **overrides: object) -> object:
        parameters = {
            "namespace": NS,
            "source_kind": "synthetic_fixture",
            "source_instance": "txb-instance",
            "source_event_id": f"source-{uuid.uuid4()}",
            "event_type": "ObservationRecorded",
            "observed_at": None,
            "payload": {"text": "event"},
            "privacy_class": "private",
            "causation_refs": (),
            "source_position": None,
            "recorded_at": RECORDED_AT,
        }
        parameters.update(overrides)
        return self.store.ingest_event(**parameters)  # type: ignore[arg-type]

    def watermarks(self, namespace: str = NS) -> tuple[int, int, int]:
        row = self.store.connection.execute(
            "SELECT state_revision, last_event_seq, privacy_epoch "
            "FROM namespace_state WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        if row is None:
            return (0, 0, 0)
        return (int(row["state_revision"]), int(row["last_event_seq"]), int(row["privacy_epoch"]))

    def command_rows(self) -> int:
        return int(
            self.store.connection.execute("SELECT COUNT(*) FROM processed_commands").fetchone()[0]
        )

    def receipt_row(self, request_id: str, namespace: str = NS) -> sqlite3.Row:
        return self.store.connection.execute(
            "SELECT request_digest, operation, receipt_json, committed_at "
            "FROM processed_commands WHERE namespace = ? AND request_id = ?",
            (namespace, request_id),
        ).fetchone()

    def commit(
        self,
        *,
        snapshot: object,
        request_id: str,
        apply: object,
        operation: str = OPERATION,
        payload: object = PAYLOAD,
        namespace: str | None = None,
    ) -> CommitReceipt:
        return self.store.commit_command(
            context_id=snapshot.context_id,  # type: ignore[attr-defined]
            request_id=request_id,
            operation=operation,
            request_payload=payload,  # type: ignore[arg-type]
            apply=apply,  # type: ignore[arg-type]
            namespace=namespace,
        )


def append_apply(*, record_id: str = RECORD_ID, body: object = None, version: int = 1):
    body = {"text": "written"} if body is None else body

    def apply(scope: object) -> None:
        scope.append_record_version(  # type: ignore[attr-defined]
            record_id=record_id,
            record_version=version,
            kind="Observation",
            body=body,
        )

    return apply


class SnapshotTests(TxBTestCase):
    def test_snapshot_is_persisted_with_a_server_generated_id(self) -> None:
        self.ingest()
        append(self.store, namespace=NS, record_id=RECORD_ID, version=1, body={"text": "one"})
        before = self.watermarks()

        snapshot = self.store.capture_snapshot(NS)

        self.assertEqual(36, len(snapshot.context_id))
        self.assertRegex(
            snapshot.context_id,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        )
        self.assertEqual(NS, snapshot.namespace)
        self.assertEqual(before, self.watermarks())
        self.assertEqual(before[0], snapshot.state_revision)
        self.assertEqual(before[1], snapshot.context_event_seq)
        self.assertEqual(before[2], snapshot.privacy_epoch)

        row = self.store.connection.execute(
            "SELECT context_id, namespace, state_revision, context_event_seq, "
            "privacy_epoch, created_at FROM context_snapshots WHERE context_id = ?",
            (snapshot.context_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(
            (
                snapshot.context_id,
                snapshot.namespace,
                snapshot.state_revision,
                snapshot.context_event_seq,
                snapshot.privacy_epoch,
                snapshot.created_at,
            ),
            tuple(row),
        )

        # Capturing writes only the snapshot row: no domain revision is taken.
        self.assertEqual(1, self.store.current_state_revision(NS))

    def test_snapshot_survives_close_and_reopen(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        self.reopen()
        request_id = new_request_id()
        receipt = self.commit(snapshot=snapshot, request_id=request_id, apply=append_apply())
        self.assertEqual(OUTCOME_COMMITTED, receipt.outcome)
        self.assertEqual(1, receipt.state_revision)
        self.assertEqual(1, self.store.current_state_revision(NS))


class CommitTests(TxBTestCase):
    def test_commit_appends_versions_and_advances_revision_exactly_once(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        request_id = new_request_id()

        def apply(scope: object) -> None:
            scope.append_record_version(  # type: ignore[attr-defined]
                record_id=RECORD_ID,
                record_version=1,
                kind="Observation",
                body={"text": "first"},
            )
            scope.append_record_version(  # type: ignore[attr-defined]
                record_id=OTHER_RECORD_ID,
                record_version=1,
                kind="Observation",
                body={"text": "second"},
            )

        receipt = self.commit(snapshot=snapshot, request_id=request_id, apply=apply)

        self.assertEqual(OUTCOME_COMMITTED, receipt.outcome)
        self.assertEqual(1, receipt.state_revision)
        self.assertEqual(1, self.store.current_state_revision(NS))
        self.assertEqual(
            (
                {"record_id": RECORD_ID, "record_version": 1},
                {"record_id": OTHER_RECORD_ID, "record_version": 1},
            ),
            tuple(
                {"record_id": ref.record_id, "record_version": ref.record_version}
                for ref in receipt.record_refs
            ),
        )
        first = self.store.get_version(NS, RECORD_ID, 1)
        second = self.store.get_version(NS, OTHER_RECORD_ID, 1)
        self.assertEqual(1, first.committed_state_revision)
        self.assertEqual(1, second.committed_state_revision)
        self.assertEqual(receipt.state_revision, first.committed_state_revision)

        row = self.receipt_row(request_id)
        self.assertEqual(OPERATION, row["operation"])
        self.assertEqual(receipt.committed_at, row["committed_at"])
        stored = json.loads(row["receipt_json"])
        self.assertEqual("committed", stored["outcome"])
        self.assertEqual(1, stored["state_revision"])

    def test_receipt_contains_no_private_body(self) -> None:
        secret = "private-body-must-not-appear-in-a-receipt"
        snapshot = self.store.capture_snapshot(NS)
        receipt = self.commit(
            snapshot=snapshot,
            request_id=new_request_id(),
            apply=append_apply(body={"text": secret}),
        )

        stored_text = self.receipt_row(receipt.request_id)["receipt_json"]
        self.assertNotIn(secret, stored_text)
        self.assertNotIn(secret, repr(receipt))
        self.assertEqual(("record_id", "record_version"), tuple(sorted(vars(receipt.record_refs[0]))))

    def test_retry_with_the_same_digest_returns_the_original_receipt(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        request_id = new_request_id()
        first = self.commit(snapshot=snapshot, request_id=request_id, apply=append_apply())
        versions_before = int(
            self.store.connection.execute("SELECT COUNT(*) FROM record_versions").fetchone()[0]
        )
        receipts_before = self.command_rows()
        revision_before = self.store.current_state_revision(NS)

        def must_not_run(scope: object) -> None:  # pragma: no cover - must not run
            raise AssertionError("a retry must not run apply")

        second = self.commit(snapshot=snapshot, request_id=request_id, apply=must_not_run)

        self.assertEqual(first, second)
        self.assertEqual(revision_before, self.store.current_state_revision(NS))
        self.assertEqual(versions_before, int(
            self.store.connection.execute("SELECT COUNT(*) FROM record_versions").fetchone()[0]
        ))
        self.assertEqual(receipts_before, self.command_rows())

    def test_retry_is_still_idempotent_after_a_watermark_moved(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        request_id = new_request_id()
        first = self.commit(snapshot=snapshot, request_id=request_id, apply=append_apply())
        append(
            self.store,
            namespace=NS,
            record_id=OTHER_RECORD_ID,
            version=1,
            body={"text": "unrelated"},
        )
        self.assertEqual(2, self.store.current_state_revision(NS))

        # J.6 step 1 precedes the watermark check: a retry returns the stored
        # receipt instead of CONTEXT_STALE.
        second = self.commit(snapshot=snapshot, request_id=request_id, apply=append_apply())
        self.assertEqual(first, second)
        self.assertEqual(2, self.store.current_state_revision(NS))

    def test_same_request_id_with_different_content_is_a_conflict(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        request_id = new_request_id()
        self.commit(snapshot=snapshot, request_id=request_id, apply=append_apply())
        revision_before = self.store.current_state_revision(NS)
        receipts_before = self.command_rows()

        with self.assertRaises(CommandConflict) as caught:
            self.commit(
                snapshot=snapshot,
                request_id=request_id,
                payload={"kind": "Observation", "body": {"text": "different"}},
                apply=append_apply(body={"text": "different"}),
            )
        self.assertEqual("ID_CONFLICT", caught.exception.code)
        self.assertEqual(revision_before, self.store.current_state_revision(NS))
        self.assertEqual(receipts_before, self.command_rows())
        self.assertEqual({"text": "written"}, self.store.get_version(NS, RECORD_ID, 1).body)

    def test_digest_is_computed_from_namespace_operation_context_and_payload(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        request_id = new_request_id()
        self.commit(snapshot=snapshot, request_id=request_id, apply=append_apply())

        expected = sha256_hex(
            canonical_json_text(
                {
                    "context_id": snapshot.context_id,
                    "namespace": NS,
                    "operation": OPERATION,
                    "request_payload": PAYLOAD,
                }
            )
        )
        self.assertEqual(expected, self.receipt_row(request_id)["request_digest"])

        # Same request_id and payload, different operation -> different digest.
        with self.assertRaises(CommandConflict):
            self.commit(
                snapshot=snapshot,
                request_id=request_id,
                operation="proposal.other",
                apply=append_apply(),
            )

        # Same request_id, payload and operation, different context_id -> a new
        # snapshot is a different commit basis, so it is not a retry.
        second_snapshot = self.store.capture_snapshot(NS)
        with self.assertRaises(CommandConflict):
            self.commit(
                snapshot=second_snapshot,
                request_id=request_id,
                apply=append_apply(),
            )

    def test_same_request_id_in_two_namespaces_commits_twice(self) -> None:
        request_id = new_request_id()
        first = self.commit(
            snapshot=self.store.capture_snapshot(NS),
            request_id=request_id,
            apply=append_apply(record_id=RECORD_ID),
        )
        second = self.commit(
            snapshot=self.store.capture_snapshot(NS_B),
            request_id=request_id,
            apply=append_apply(record_id=RECORD_ID),
        )
        self.assertEqual(NS, first.namespace)
        self.assertEqual(NS_B, second.namespace)
        self.assertEqual(1, self.store.current_state_revision(NS))
        self.assertEqual(1, self.store.current_state_revision(NS_B))
        self.assertEqual(2, self.command_rows())


class StaleBasisTests(TxBTestCase):
    def test_unknown_context_id_is_not_found(self) -> None:
        with self.assertRaises(SnapshotNotFound) as caught:
            self.store.commit_command(
                context_id=str(uuid.uuid4()),
                request_id=new_request_id(),
                operation=OPERATION,
                request_payload=PAYLOAD,
                apply=append_apply(),
            )
        self.assertEqual("NOT_FOUND", caught.exception.code)
        self.assertEqual(0, self.command_rows())

    def test_snapshot_namespace_mismatch_is_a_scope_violation(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        with self.assertRaises(ScopeViolation) as caught:
            self.commit(
                snapshot=snapshot,
                request_id=new_request_id(),
                apply=append_apply(),
                namespace=NS_B,
            )
        self.assertEqual("SCOPE_VIOLATION", caught.exception.code)
        self.assertEqual(0, self.command_rows())

    def test_privacy_epoch_is_checked_first_and_reports_context_stale(self) -> None:
        self.ingest()
        snapshot = self.store.capture_snapshot(NS)
        # Move all three watermarks: privacy must win.
        self.store.connection.execute(
            "UPDATE namespace_state SET privacy_epoch = privacy_epoch + 1, "
            "state_revision = state_revision + 1 WHERE namespace = ?",
            (NS,),
        )
        self.ingest()

        with self.assertRaises(ContextStale) as caught:
            self.commit(snapshot=snapshot, request_id=new_request_id(), apply=append_apply())
        self.assertEqual("CONTEXT_STALE", caught.exception.code)
        self.assertEqual(0, self.command_rows())

    def test_event_sequence_is_checked_before_state_revision(self) -> None:
        self.ingest()
        snapshot = self.store.capture_snapshot(NS)
        append(self.store, namespace=NS, record_id=RECORD_ID, version=1, body={"text": "domain"})
        self.ingest()

        with self.assertRaises(ContextStale) as caught:
            self.commit(snapshot=snapshot, request_id=new_request_id(), apply=append_apply())
        self.assertEqual("CONTEXT_STALE", caught.exception.code)
        self.assertEqual(0, self.command_rows())

    def test_state_revision_mismatch_is_a_revision_conflict(self) -> None:
        append(self.store, namespace=NS, record_id=RECORD_ID, version=1, body={"text": "one"})
        snapshot = self.store.capture_snapshot(NS)
        append(
            self.store,
            namespace=NS,
            record_id=OTHER_RECORD_ID,
            version=1,
            body={"text": "two"},
        )

        with self.assertRaises(StateRevisionConflict) as caught:
            self.commit(snapshot=snapshot, request_id=new_request_id(), apply=append_apply())
        self.assertEqual("REVISION_CONFLICT", caught.exception.code)
        self.assertEqual(0, self.command_rows())

    def test_a_stale_command_writes_nothing_and_occupies_no_request_id(self) -> None:
        append(self.store, namespace=NS, record_id=RECORD_ID, version=1, body={"text": "one"})
        stale_snapshot = self.store.capture_snapshot(NS)
        append(
            self.store,
            namespace=NS,
            record_id=OTHER_RECORD_ID,
            version=1,
            body={"text": "two"},
        )
        request_id = new_request_id()

        with self.assertRaises(StateRevisionConflict):
            self.commit(
                snapshot=stale_snapshot,
                request_id=request_id,
                apply=append_apply(record_id="66666666-6666-4666-8666-666666666666"),
            )
        self.assertEqual(0, self.command_rows())
        self.assertEqual(2, self.store.current_state_revision(NS))

        fresh = self.store.capture_snapshot(NS)
        receipt = self.commit(
            snapshot=fresh,
            request_id=request_id,
            apply=append_apply(record_id="66666666-6666-4666-8666-666666666666"),
        )
        self.assertEqual(OUTCOME_COMMITTED, receipt.outcome)
        self.assertEqual(3, self.store.current_state_revision(NS))


class CommandTransactionTests(TxBTestCase):
    def test_apply_failure_rolls_back_everything_and_the_retry_succeeds(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        request_id = new_request_id()

        def failing(scope: object) -> None:
            scope.append_record_version(  # type: ignore[attr-defined]
                record_id=RECORD_ID,
                record_version=1,
                kind="Observation",
                body={"text": "half"},
            )
            raise RuntimeError("injected apply failure")

        with self.assertRaises(RuntimeError):
            self.commit(snapshot=snapshot, request_id=request_id, apply=failing)

        self.assertEqual(0, self.store.current_state_revision(NS))
        self.assertEqual(0, self.command_rows())
        self.assertEqual(
            0,
            int(self.store.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]),
        )
        self.assertEqual(
            0,
            int(self.store.connection.execute("SELECT COUNT(*) FROM private_payloads").fetchone()[0]),
        )

        # The same snapshot and request_id are still usable: nothing was
        # committed and no request id was occupied.
        receipt = self.commit(snapshot=snapshot, request_id=request_id, apply=append_apply())
        self.assertEqual(OUTCOME_COMMITTED, receipt.outcome)
        self.assertEqual(1, receipt.state_revision)

    def test_a_swallowed_apply_failure_aborts_the_command(self) -> None:
        snapshot = self.store.capture_snapshot(NS)

        def swallow(scope: object) -> None:
            with self.assertRaises(InvalidInput):
                scope.append_record_version(  # type: ignore[attr-defined]
                    record_id="not-a-uuid",
                    record_version=1,
                    kind="Observation",
                    body={"text": "x"},
                )

        with self.assertRaises(CorruptStore) as caught:
            self.commit(snapshot=snapshot, request_id=new_request_id(), apply=swallow)
        self.assertEqual("CORRUPT_STORE", caught.exception.code)
        self.assertEqual(0, self.store.current_state_revision(NS))
        self.assertEqual(0, self.command_rows())

    def test_legal_noop_stores_a_receipt_without_advancing_revision(self) -> None:
        append(self.store, namespace=NS, record_id=RECORD_ID, version=1, body={"text": "one"})
        snapshot = self.store.capture_snapshot(NS)
        request_id = new_request_id()
        receipt = self.commit(snapshot=snapshot, request_id=request_id, apply=lambda scope: None)

        self.assertEqual(OUTCOME_NOOP, receipt.outcome)
        self.assertEqual(1, receipt.state_revision)
        self.assertEqual((), receipt.record_refs)
        self.assertEqual(1, self.store.current_state_revision(NS))
        self.assertEqual(1, self.command_rows())

        # A retry returns the stored no-op receipt and runs no apply.
        def must_not_run(scope: object) -> None:  # pragma: no cover - must not run
            raise AssertionError("a retry must not run apply")

        self.assertEqual(receipt, self.commit(snapshot=snapshot, request_id=request_id, apply=must_not_run))

    def test_tx_scope_has_no_connection_or_deletion_surface(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        forbidden = (
            "connection",
            "execute",
            "executescript",
            "cursor",
            "commit",
            "rollback",
            "delete_record",
            "delete_event",
            "delete_target",
            "request_deletion",
            "cleanup_deletions",
        )

        def inspect(scope: object) -> None:
            self.assertEqual(NS, scope.namespace)  # type: ignore[attr-defined]
            self.assertTrue(callable(scope.append_record_version))  # type: ignore[attr-defined]
            for name in forbidden:
                with self.subTest(member=name):
                    self.assertFalse(hasattr(scope, name))
                    with self.assertRaises(AttributeError):
                        getattr(scope, name)

        self.commit(snapshot=snapshot, request_id=new_request_id(), apply=inspect)
        self.assertEqual(1, self.command_rows())
        self.assertEqual(0, self.store.current_state_revision(NS))

    def test_commit_command_requires_its_own_transaction(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        with self.store.transaction():
            with self.assertRaises(StandaloneTransactionRequired) as caught:
                self.commit(snapshot=snapshot, request_id=new_request_id(), apply=append_apply())
        self.assertEqual("INVALID_TRANSITION", caught.exception.code)
        self.assertEqual(0, self.command_rows())

    def test_lock_contention_maps_to_db_busy(self) -> None:
        self.store.connection.execute("PRAGMA busy_timeout = 50")
        locker = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=5.0)
        self.addCleanup(locker.close)
        locker.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(DbBusy) as caught:
                self.store.capture_snapshot(NS)
            self.assertEqual("DB_BUSY", caught.exception.code)
        finally:
            locker.execute("ROLLBACK")

        # The lock is gone and the store is still usable.
        self.assertEqual(0, self.store.capture_snapshot(NS).state_revision)

    def test_a_commit_that_cannot_take_the_write_lock_is_db_busy(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        self.store.connection.execute("PRAGMA busy_timeout = 50")
        locker = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=5.0)
        self.addCleanup(locker.close)
        locker.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(DbBusy) as caught:
                self.commit(
                    snapshot=snapshot,
                    request_id=new_request_id(),
                    apply=append_apply(),
                )
            self.assertEqual("DB_BUSY", caught.exception.code)
        finally:
            locker.execute("ROLLBACK")

        self.assertEqual(0, self.command_rows())
        receipt = self.commit(
            snapshot=snapshot, request_id=new_request_id(), apply=append_apply()
        )
        self.assertEqual(OUTCOME_COMMITTED, receipt.outcome)


class InputValidationTests(TxBTestCase):
    def test_request_fields_are_validated(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        bad_requests = ("", "not-a-uuid", "22222222-2222-4222-8222-22222222222A", 7)
        for request_id in bad_requests:
            with self.subTest(request_id=request_id):
                with self.assertRaises(InvalidInput):
                    self.commit(
                        snapshot=snapshot,
                        request_id=request_id,  # type: ignore[arg-type]
                        apply=append_apply(),
                    )
        for operation in ("", "Proposal", "1proposal", "a..b", "a b", "x" * 129):
            with self.subTest(operation=operation):
                with self.assertRaises(InvalidInput):
                    self.commit(
                        snapshot=snapshot,
                        request_id=new_request_id(),
                        operation=operation,
                        apply=append_apply(),
                    )
        for context_id in ("", "x" * 129, 7):
            with self.subTest(context_id=context_id):
                with self.assertRaises(InvalidInput):
                    self.store.commit_command(
                        context_id=context_id,  # type: ignore[arg-type]
                        request_id=new_request_id(),
                        operation=OPERATION,
                        request_payload=PAYLOAD,
                        apply=append_apply(),
                    )
        for payload in ([], "text", 7, None):
            with self.subTest(payload=payload):
                with self.assertRaises(InvalidInput):
                    self.store.commit_command(
                        context_id=snapshot.context_id,
                        request_id=new_request_id(),
                        operation=OPERATION,
                        request_payload=payload,  # type: ignore[arg-type]
                        apply=append_apply(),
                    )

    def test_request_fields_match_the_frozen_contract(self) -> None:
        request = json.loads(
            (REPO_ROOT / "contracts" / "v2" / "request.json").read_text(encoding="utf-8")
        )
        common = json.loads(
            (REPO_ROOT / "contracts" / "v2" / "_common.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            request["properties"]["operation"]["pattern"],
            identifiers.OPERATION_PATTERN.pattern,
        )
        self.assertEqual(
            common["$defs"]["uuid"]["pattern"], identifiers.UUID_PATTERN.pattern
        )
        self.assertEqual(
            common["$defs"]["namespace"]["pattern"], identifiers.NAMESPACE_PATTERN.pattern
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
