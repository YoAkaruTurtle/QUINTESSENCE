"""PCS2-006 B group: the two-phase deletion barrier.

State machine under test::

    ACTIVE --Tx1--> REDACTED_PENDING_CLEANUP --cleanup--> REDACTED_CLEANED

The assertions cover the read/write barrier, shared payloads, authorization,
partial cleanup progress, receipt redaction and the untouched 0001/0002
immutability triggers (plan A, no guard trigger).
"""

from __future__ import annotations

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
    StoreTestCase,
    all_text_cells,
    append,
)

from quintessence_pcs.foundation.errors import (  # noqa: E402
    AuthorizationRequired,
    ContextStale,
    EventNotFound,
    InvalidDeletionTarget,
    RecordNotFound,
    Redacted,
    SourceRedacted,
    StandaloneTransactionRequired,
)
from quintessence_pcs.foundation.migrations import (  # noqa: E402
    default_migrations_dir,
    discover_migrations,
)
from quintessence_pcs.store.control import ControlStore  # noqa: E402
from quintessence_pcs.store.deletion import (  # noqa: E402
    DELETION_CLEANED,
    DELETION_PENDING_CLEANUP,
)
from quintessence_pcs.store.tx_b import OUTCOME_COMMITTED  # noqa: E402

NS = "example.test.deletion"
NS_B = "example.test.deletion.other"
RECORD_ID = "22222222-2222-4222-8222-222222222222"
OTHER_RECORD_ID = "33333333-3333-4333-8333-333333333333"
THIRD_RECORD_ID = "44444444-4444-4444-8444-444444444444"
ABSENT_ID = "12121212-1212-4212-8212-121212121212"
SECRET = "the-private-sentence-that-must-disappear"


class DeletionTestCase(StoreTestCase):
    store_class = ControlStore

    def record(
        self,
        *,
        record_id: str = RECORD_ID,
        body: object = None,
        version: int = 1,
        namespace: str = NS,
    ):
        body = {"text": "body"} if body is None else body
        return append(
            self.store,
            namespace=namespace,
            record_id=record_id,
            version=version,
            body=body,
        )

    def ingest(self, *, namespace: str = NS, payload: object = None, source_event_id: str | None = None):
        return self.store.ingest_event(
            namespace=namespace,
            source_kind="synthetic_fixture",
            source_instance="deletion-instance",
            source_event_id=source_event_id or f"source-{uuid.uuid4()}",
            event_type="ObservationRecorded",
            observed_at=None,
            payload={"text": "event"} if payload is None else payload,
            privacy_class="private",
            causation_refs=(),
            source_position=None,
            recorded_at=RECORDED_AT,
        )

    def watermarks(self, namespace: str = NS) -> tuple[int, int, int]:
        row = self.store.connection.execute(
            "SELECT state_revision, last_event_seq, privacy_epoch "
            "FROM namespace_state WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        if row is None:
            return (0, 0, 0)
        return (int(row["state_revision"]), int(row["last_event_seq"]), int(row["privacy_epoch"]))

    def delete_record(
        self, record_id: str = RECORD_ID, *, auth: str = "auth:test-1", namespace: str = NS
    ):
        return self.store.request_deletion(
            target_kind="record",
            namespace=namespace,
            target_id=record_id,
            authorization_ref=auth,
        )

    def delete_event(
        self, event_id: str, *, auth: str = "auth:test-1", namespace: str = NS
    ):
        return self.store.request_deletion(
            target_kind="event",
            namespace=namespace,
            target_id=event_id,
            authorization_ref=auth,
        )

    def payload_row(self, payload_ref: str) -> sqlite3.Row:
        row = self.store.connection.execute(
            "SELECT payload_ref, sha256, body_json FROM private_payloads WHERE payload_ref = ?",
            (payload_ref,),
        ).fetchone()
        self.assertIsNotNone(row, f"missing payload {payload_ref}")
        return row

    def target_rows(self) -> int:
        return int(
            self.store.connection.execute("SELECT COUNT(*) FROM deletion_targets").fetchone()[0]
        )


class BarrierTests(DeletionTestCase):
    def test_active_to_redacted_pending_cleanup_on_tx1(self) -> None:
        stored = self.record(body={"text": "still-on-disk"})
        before = self.watermarks()

        target = self.delete_record()

        after = self.watermarks()
        self.assertEqual(DELETION_PENDING_CLEANUP, target.status)
        self.assertEqual("auth:test-1", target.authorization_ref)
        self.assertEqual(before[0] + 1, after[0])  # state_revision +1
        self.assertEqual(before[1], after[1])  # events untouched
        self.assertEqual(before[2] + 1, after[2])  # privacy_epoch +1
        self.assertEqual(1, self.target_rows())

        # The barrier is readable state before any cleanup has run...
        pending = self.store.deletion_targets(status=DELETION_PENDING_CLEANUP)
        self.assertEqual(1, len(pending))
        self.assertEqual(DELETION_PENDING_CLEANUP, pending[0].status)
        self.assertEqual("auth:test-1", pending[0].authorization_ref)
        # ...and the body is still there, so REDACTED is not caused by cleanup.
        raw = self.payload_row(stored.body_payload_ref)
        self.assertEqual('{"text":"still-on-disk"}', raw["body_json"])

        with self.assertRaises(Redacted) as caught:
            self.store.get_version(NS, RECORD_ID, 1)
        self.assertEqual("REDACTED", caught.exception.code)
        with self.assertRaises(Redacted):
            self.store.get_current(NS, RECORD_ID)

    def test_redacted_pending_cleanup_to_redacted_cleaned_on_cleanup(self) -> None:
        stored = self.record(body={"text": SECRET})
        self.delete_record()
        before = self.watermarks()

        report = self.store.cleanup_deletions()

        self.assertEqual(1, len(report.results))
        self.assertEqual(1, report.cleaned_count)
        self.assertEqual(0, report.failed_count)
        self.assertEqual(1, report.cleared_payloads)
        result = report.results[0]
        self.assertIsNone(result.error)
        self.assertEqual(DELETION_CLEANED, result.target.status)
        self.assertIsNone(result.target.last_error)
        self.assertEqual(before, self.watermarks())

        raw = self.payload_row(stored.body_payload_ref)
        self.assertEqual("", raw["body_json"])
        self.assertNotEqual(stored.body_sha256, raw["sha256"])
        with self.assertRaises(Redacted):
            self.store.get_version(NS, RECORD_ID, 1)

        # The transition is durable, and a second cleanup pass is a no-op.
        state = self.store.deletion_targets()
        self.assertEqual([DELETION_CLEANED], [target.status for target in state])
        second = self.store.cleanup_deletions()
        self.assertEqual((), second.results)
        self.assertEqual(before, self.watermarks())
        self.assertEqual(1, self.target_rows())

    def test_repeat_delete_is_idempotent(self) -> None:
        self.record()
        first = self.delete_record()
        after_first = self.watermarks()

        second = self.delete_record(auth="auth:different")
        self.assertEqual(first, second)
        self.assertEqual(after_first, self.watermarks())
        self.assertEqual(1, self.target_rows())
        # The original authorization is kept, not overwritten.
        self.assertEqual("auth:test-1", second.authorization_ref)

    def test_delete_unknown_target_is_not_found_without_side_effects(self) -> None:
        with self.assertRaises(RecordNotFound):
            self.delete_record(ABSENT_ID)
        with self.assertRaises(EventNotFound):
            self.delete_event(ABSENT_ID)
        self.assertEqual(0, self.target_rows())
        self.assertEqual((0, 0, 0), self.watermarks())

    def test_deletion_requires_an_explicit_authorization(self) -> None:
        self.record()
        before = self.watermarks()
        for auth in (None, "", "   ", 7, "x" * 513):
            with self.subTest(auth=auth):
                with self.assertRaises(AuthorizationRequired) as caught:
                    self.store.request_deletion(
                        target_kind="record",
                        namespace=NS,
                        target_id=RECORD_ID,
                        authorization_ref=auth,  # type: ignore[arg-type]
                    )
                self.assertEqual("SCOPE_VIOLATION", caught.exception.code)
        self.assertEqual(0, self.target_rows())
        self.assertEqual(before, self.watermarks())
        # The ordinary read path is unaffected by the rejected requests.
        self.assertEqual(RECORD_ID, self.store.get_version(NS, RECORD_ID, 1).record_id)

    def test_invalid_target_kind_is_rejected(self) -> None:
        self.record()
        with self.assertRaises(InvalidDeletionTarget) as caught:
            self.store.request_deletion(
                target_kind="namespace",
                namespace=NS,
                target_id=RECORD_ID,
                authorization_ref="auth:test-1",
            )
        self.assertEqual("INVALID_SCHEMA", caught.exception.code)
        self.assertEqual(0, self.target_rows())

    def test_request_deletion_and_cleanup_require_their_own_transaction(self) -> None:
        self.record()
        before = self.watermarks()
        with self.store.transaction():
            with self.assertRaises(StandaloneTransactionRequired) as caught:
                self.delete_record()
            self.assertEqual("INVALID_TRANSITION", caught.exception.code)
            with self.assertRaises(StandaloneTransactionRequired):
                self.store.cleanup_deletions()
        self.assertEqual(0, self.target_rows())
        self.assertEqual(before, self.watermarks())


class ReadBarrierTests(DeletionTestCase):
    def test_every_version_of_a_deleted_record_is_redacted(self) -> None:
        self.record(version=1, body={"text": "v1"})
        self.record(version=2, body={"text": "v2"})
        self.record(version=3, body={"text": "v3"})
        self.delete_record()
        for version in (1, 2, 3):
            with self.subTest(version=version):
                with self.assertRaises(Redacted):
                    self.store.get_version(NS, RECORD_ID, version)
        with self.assertRaises(Redacted):
            self.store.get_current(NS, RECORD_ID)
        # The barrier does not remove the immutable rows (plan A).
        self.assertEqual(
            3,
            int(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM record_versions WHERE record_id = ?", (RECORD_ID,)
                ).fetchone()[0]
            ),
        )

    def test_cross_namespace_reads_stay_not_found(self) -> None:
        self.record()
        self.delete_record()
        with self.assertRaises(RecordNotFound) as caught:
            self.store.get_version(NS_B, RECORD_ID, 1)
        self.assertEqual("NOT_FOUND", caught.exception.code)
        with self.assertRaises(RecordNotFound):
            self.store.get_current(NS_B, RECORD_ID)
        with self.assertRaises(Redacted):
            self.store.get_version(NS, RECORD_ID, 1)

    def test_deleted_event_reads_are_redacted(self) -> None:
        receipt = self.ingest()
        self.delete_event(receipt.event_id)
        for read in (
            lambda: self.store.get_event(NS, receipt.event_id),
            lambda: self.store.causes_of(NS, receipt.event_id),
            lambda: self.store.pending_work_for(NS, receipt.event_id),
        ):
            with self.assertRaises(Redacted):
                read()

    def test_deleted_record_cannot_receive_a_new_version(self) -> None:
        self.record(version=1)
        self.delete_record()
        with self.assertRaises(Redacted):
            self.record(version=2, body={"text": "revived"})

        # Also through the ordinary command path: a fresh snapshot passes the
        # watermark check, but the write barrier still refuses the append.
        snapshot = self.store.capture_snapshot(NS)
        revision_before = self.store.current_state_revision(NS)
        with self.assertRaises(Redacted):
            self.store.commit_command(
                context_id=snapshot.context_id,
                request_id=str(uuid.uuid4()),
                operation="proposal.submit",
                request_payload={"kind": "Observation"},
                apply=lambda scope: scope.append_record_version(
                    record_id=RECORD_ID,
                    record_version=2,
                    kind="Observation",
                    body={"text": "revived"},
                ),
            )
        self.assertEqual(revision_before, self.store.current_state_revision(NS))
        self.assertEqual(
            1,
            int(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM record_versions WHERE record_id = ?", (RECORD_ID,)
                ).fetchone()[0]
            ),
        )


class SharedPayloadTests(DeletionTestCase):
    def test_shared_record_payload_survives_until_the_last_reference_is_deleted(self) -> None:
        first = self.record(record_id=RECORD_ID, body={"text": "shared"})
        second = self.record(record_id=OTHER_RECORD_ID, body={"text": "shared"})
        self.assertEqual(1, int(
            self.store.connection.execute("SELECT COUNT(*) FROM private_payloads").fetchone()[0]
        ))
        self.assertEqual(first.body_payload_ref, second.body_payload_ref)

        self.delete_record(RECORD_ID)
        self.store.cleanup_deletions()
        # A is gone, B is untouched and the shared body is still readable.
        with self.assertRaises(Redacted):
            self.store.get_version(NS, RECORD_ID, 1)
        self.assertEqual({"text": "shared"}, self.store.get_version(NS, OTHER_RECORD_ID, 1).body)
        self.assertEqual('{"text":"shared"}', self.payload_row(second.body_payload_ref)["body_json"])

        self.delete_record(OTHER_RECORD_ID)
        report = self.store.cleanup_deletions()
        self.assertEqual(1, report.cleared_payloads)
        self.assertEqual("", self.payload_row(second.body_payload_ref)["body_json"])
        with self.assertRaises(Redacted):
            self.store.get_version(NS, OTHER_RECORD_ID, 1)

    def test_shared_record_and_event_payload_is_cleared_only_after_both_are_deleted(self) -> None:
        receipt = self.ingest(payload={"text": "shared-body"})
        stored = self.record(body={"text": "shared-body"})
        self.assertEqual(1, int(
            self.store.connection.execute("SELECT COUNT(*) FROM private_payloads").fetchone()[0]
        ))

        self.delete_record()
        self.store.cleanup_deletions()
        self.assertEqual({"text": "shared-body"}, self.store.get_event(NS, receipt.event_id).payload)
        self.assertEqual('{"text":"shared-body"}', self.payload_row(stored.body_payload_ref)["body_json"])

        self.delete_event(receipt.event_id)
        report = self.store.cleanup_deletions()
        self.assertEqual(1, report.cleared_payloads)
        self.assertEqual("", self.payload_row(stored.body_payload_ref)["body_json"])
        with self.assertRaises(Redacted):
            self.store.get_event(NS, receipt.event_id)


class ContentAddressingTests(DeletionTestCase):
    def test_no_text_cell_in_the_database_retains_the_deleted_digest(self) -> None:
        stored = self.record(body={"text": SECRET})
        digest = stored.body_sha256
        self.delete_record()
        self.store.cleanup_deletions()

        raw = self.payload_row(stored.body_payload_ref)
        self.assertEqual("", raw["body_json"])
        self.assertNotEqual(digest, raw["sha256"])

        hits = [
            (location, value)
            for location, value in all_text_cells(self.store.connection)
            if SECRET in value or digest in value
        ]
        self.assertEqual([], hits)

    def test_rewriting_the_same_content_creates_a_new_payload_not_a_revival(self) -> None:
        original = self.record(record_id=RECORD_ID, body={"text": SECRET})
        self.delete_record(RECORD_ID)
        self.store.cleanup_deletions()
        tombstone_ref = original.body_payload_ref

        fresh = self.record(record_id=OTHER_RECORD_ID, body={"text": SECRET})
        self.assertNotEqual(tombstone_ref, fresh.body_payload_ref)
        self.assertEqual(2, int(
            self.store.connection.execute("SELECT COUNT(*) FROM private_payloads").fetchone()[0]
        ))
        self.assertEqual("", self.payload_row(tombstone_ref)["body_json"])
        self.assertEqual({"text": SECRET}, self.store.get_version(NS, OTHER_RECORD_ID, 1).body)

        # De-duplication still works among live rows: the third write reuses
        # the second payload row and does not resurrect the tombstone.
        third = self.record(record_id=THIRD_RECORD_ID, body={"text": SECRET})
        self.assertEqual(fresh.body_payload_ref, third.body_payload_ref)
        self.assertEqual(2, int(
            self.store.connection.execute("SELECT COUNT(*) FROM private_payloads").fetchone()[0]
        ))
        with self.assertRaises(Redacted):
            self.store.get_version(NS, RECORD_ID, 1)


class CleanupProgressTests(DeletionTestCase):
    def test_failed_cleanup_keeps_the_barrier_and_resumes_from_progress(self) -> None:
        first = self.record(version=1, body={"text": "version-one"})
        second = self.record(version=2, body={"text": "version-two"})
        self.assertNotEqual(first.body_payload_ref, second.body_payload_ref)
        self.delete_record()
        before = self.watermarks()

        # Block only the second payload update; the first must still be cleared
        # in its own transaction, so progress survives the failure.
        self.store.connection.execute(
            "CREATE TRIGGER test_block_payload_update BEFORE UPDATE ON private_payloads "
            f"WHEN OLD.payload_ref = '{second.body_payload_ref}' "
            "BEGIN SELECT RAISE(ABORT, 'blocked for the progress test'); END"
        )
        try:
            report = self.store.cleanup_deletions()
        finally:
            self.store.connection.execute("DROP TRIGGER test_block_payload_update")

        self.assertEqual(1, report.failed_count)
        self.assertIsNotNone(report.results[0].error)
        target = self.store.deletion_targets()[0]
        self.assertEqual(DELETION_PENDING_CLEANUP, target.status)
        self.assertIsNotNone(target.last_error)
        self.assertEqual(before, self.watermarks())
        # Partial progress: version 1 was cleared, version 2 was not.
        self.assertEqual("", self.payload_row(first.body_payload_ref)["body_json"])
        self.assertEqual(
            '{"text":"version-two"}', self.payload_row(second.body_payload_ref)["body_json"]
        )
        with self.assertRaises(Redacted):
            self.store.get_version(NS, RECORD_ID, 2)

        resumed = self.store.cleanup_deletions()
        self.assertEqual(1, resumed.cleaned_count)
        self.assertEqual(1, resumed.cleared_payloads)
        target = self.store.deletion_targets()[0]
        self.assertEqual(DELETION_CLEANED, target.status)
        self.assertIsNone(target.last_error)
        self.assertEqual("", self.payload_row(second.body_payload_ref)["body_json"])
        self.assertEqual(before, self.watermarks())

    def test_cleanup_failure_message_never_contains_the_body(self) -> None:
        stored = self.record(body={"text": SECRET})
        self.delete_record()
        self.store.connection.execute(
            "CREATE TRIGGER test_block_payload_update BEFORE UPDATE ON private_payloads "
            "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
        )
        try:
            report = self.store.cleanup_deletions()
        finally:
            self.store.connection.execute("DROP TRIGGER test_block_payload_update")
        self.assertIsNotNone(report.results[0].error)
        self.assertNotIn(SECRET, report.results[0].error or "")
        self.assertNotIn(SECRET, str(self.store.deletion_targets()[0]))

        # The un-cleared payload still holds the body; a resumed pass clears it.
        self.assertIn(SECRET, self.payload_row(stored.body_payload_ref)["body_json"])


class EventTombstoneTests(DeletionTestCase):
    def test_reingesting_a_deleted_source_key_is_source_redacted(self) -> None:
        receipt = self.ingest(payload={"text": SECRET}, source_event_id="source-fixed-1")
        self.delete_event(receipt.event_id)

        # Before cleanup: tombstone, not duplicate.
        with self.assertRaises(SourceRedacted) as caught:
            self.ingest(payload={"text": SECRET}, source_event_id="source-fixed-1")
        self.assertEqual("SOURCE_REDACTED", caught.exception.code)
        # ...and a different payload is still SOURCE_REDACTED, not ID_CONFLICT.
        with self.assertRaises(SourceRedacted):
            self.ingest(payload={"text": "other"}, source_event_id="source-fixed-1")

        payload_ref = self.store.connection.execute(
            "SELECT payload_ref FROM event_journal WHERE event_id = ?", (receipt.event_id,)
        ).fetchone()["payload_ref"]
        original = self.payload_row(payload_ref)["body_json"]
        self.store.cleanup_deletions()
        with self.assertRaises(SourceRedacted):
            self.ingest(payload={"text": SECRET}, source_event_id="source-fixed-1")

        self.assertEqual(
            1,
            int(self.store.connection.execute("SELECT COUNT(*) FROM event_journal").fetchone()[0]),
        )
        self.assertEqual("", self.payload_row(payload_ref)["body_json"])
        self.assertNotEqual(original, "")

    def test_deleted_source_key_wins_over_payload_comparison(self) -> None:
        receipt = self.ingest(payload={"text": "original"}, source_event_id="source-fixed-2")
        self.delete_event(receipt.event_id)
        self.store.cleanup_deletions()
        # Even an identical payload is a tombstone, never "duplicate".
        with self.assertRaises(SourceRedacted):
            self.ingest(payload={"text": "original"}, source_event_id="source-fixed-2")


class ReceiptRedactionTests(DeletionTestCase):
    def test_replaying_a_committed_command_for_a_deleted_target_is_desensitized(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        request_id = str(uuid.uuid4())
        payload = {"kind": "Observation", "body": {"text": SECRET}}
        first = self.store.commit_command(
            context_id=snapshot.context_id,
            request_id=request_id,
            operation="proposal.submit",
            request_payload=payload,
            apply=lambda scope: scope.append_record_version(
                record_id=RECORD_ID,
                record_version=1,
                kind="Observation",
                body={"text": SECRET},
            ),
        )
        self.assertEqual(OUTCOME_COMMITTED, first.outcome)
        stored_receipt = self.store.connection.execute(
            "SELECT receipt_json FROM processed_commands WHERE request_id = ?", (request_id,)
        ).fetchone()["receipt_json"]
        self.assertNotIn(SECRET, stored_receipt)

        self.delete_record()
        self.store.cleanup_deletions()
        revision_after_delete = self.store.current_state_revision(NS)

        # Replaying the committed request returns the stored (body-free)
        # receipt even though the snapshot basis is stale; idempotency is
        # checked first (J.6 step 1).
        def must_not_run(scope: object) -> None:  # pragma: no cover - must not run
            raise AssertionError("a retry must not run apply")

        replay = self.store.commit_command(
            context_id=snapshot.context_id,
            request_id=request_id,
            operation="proposal.submit",
            request_payload=payload,
            apply=must_not_run,
        )
        self.assertEqual(first, replay)
        self.assertNotIn(SECRET, repr(replay))
        self.assertEqual(revision_after_delete, self.store.current_state_revision(NS))

        # A new request on the same old snapshot is stale and returns nothing.
        with self.assertRaises(ContextStale) as caught:
            self.store.commit_command(
                context_id=snapshot.context_id,
                request_id=str(uuid.uuid4()),
                operation="proposal.submit",
                request_payload=payload,
                apply=lambda scope: scope.append_record_version(
                    record_id=THIRD_RECORD_ID,
                    record_version=1,
                    kind="Observation",
                    body={"text": SECRET},
                ),
            )
        self.assertEqual("CONTEXT_STALE", caught.exception.code)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(revision_after_delete, self.store.current_state_revision(NS))
        self.assertEqual(
            0,
            int(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM record_versions WHERE record_id = ?", (THIRD_RECORD_ID,)
                ).fetchone()[0]
            ),
        )


class AuthorizationIsolationTests(DeletionTestCase):
    def test_ordinary_commit_apply_cannot_reach_the_deletion_api(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        attempts: list[str] = []

        def try_to_delete(scope: object) -> None:
            for member in ("delete_target", "delete_record", "delete_event",
                           "request_deletion", "cleanup_deletions", "connection", "execute"):
                with self.assertRaises(AttributeError):
                    getattr(scope, member)
                attempts.append(member)

        receipt = self.store.commit_command(
            context_id=snapshot.context_id,
            request_id=str(uuid.uuid4()),
            operation="proposal.submit",
            request_payload={"kind": "Observation"},
            apply=try_to_delete,
        )
        self.assertEqual(("delete_target", "delete_record", "delete_event",
                          "request_deletion", "cleanup_deletions", "connection", "execute"),
                         tuple(attempts))
        self.assertEqual(0, self.target_rows())
        self.assertEqual("noop", receipt.outcome)

    def test_an_apply_that_calls_delete_target_fails_and_writes_nothing(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        request_id = str(uuid.uuid4())
        with self.assertRaises(AttributeError):
            self.store.commit_command(
                context_id=snapshot.context_id,
                request_id=request_id,
                operation="proposal.submit",
                request_payload={"kind": "Observation"},
                apply=lambda scope: scope.delete_target(
                    target_kind="record",
                    namespace=NS,
                    target_id=RECORD_ID,
                    authorization_ref="auth:stolen",
                ),
            )
        self.assertEqual(0, self.target_rows())
        self.assertEqual(0, self.store.current_state_revision(NS))
        self.assertEqual(
            0,
            int(self.store.connection.execute(
                "SELECT COUNT(*) FROM processed_commands WHERE request_id = ?", (request_id,)
            ).fetchone()[0]),
        )

    def test_even_the_private_store_handle_cannot_delete_inside_apply(self) -> None:
        """The barrier owns its transaction; a nested request is refused."""

        snapshot = self.store.capture_snapshot(NS)

        def reach_through_the_private_handle(scope: object) -> None:
            with self.assertRaises(StandaloneTransactionRequired) as caught:
                scope._store.request_deletion(  # type: ignore[attr-defined]
                    target_kind="record",
                    namespace=NS,
                    target_id=RECORD_ID,
                    authorization_ref="auth:bypass",
                )
            self.assertEqual("INVALID_TRANSITION", caught.exception.code)

        receipt = self.store.commit_command(
            context_id=snapshot.context_id,
            request_id=str(uuid.uuid4()),
            operation="proposal.submit",
            request_payload={"kind": "Observation"},
            apply=reach_through_the_private_handle,
        )
        self.assertEqual("noop", receipt.outcome)
        self.assertEqual(0, self.target_rows())
        self.assertEqual(0, self.store.current_state_revision(NS))

    def test_immutable_triggers_are_untouched_and_still_enforced(self) -> None:
        self.record()
        receipt = self.ingest()
        self.delete_record()
        self.delete_event(receipt.event_id)
        self.store.cleanup_deletions()

        versions = [row["migration_id"] for row in self.store.connection.execute(
            "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
        ).fetchall()]
        # PCS2-007 adds 0004; the expectation is derived from the shipped
        # migration directory instead of pinning the 006-era number.
        self.assertEqual(
            [migration.version for migration in discover_migrations(default_migrations_dir())],
            versions,
        )
        triggers = {
            row["name"]
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        self.assertTrue(
            {
                "record_versions_no_update",
                "record_versions_no_delete",
                "event_journal_no_update",
                "event_journal_no_delete",
            }.issubset(triggers)
        )
        self.assertEqual(
            0,
            int(self.store.connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'private_payloads'"
            ).fetchone()[0]),
        )
        self.assertEqual(
            0,
            int(self.store.connection.execute(
                "SELECT COUNT(*) FROM sqlite_temp_master WHERE type = 'trigger'"
            ).fetchone()[0]),
        )

        with self.assertRaises(sqlite3.IntegrityError) as updated:
            self.store.connection.execute(
                "UPDATE record_versions SET status = 'retracted' "
                "WHERE namespace = ? AND record_id = ? AND record_version = 1",
                (NS, RECORD_ID),
            )
        self.assertIn("immutable", str(updated.exception))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM record_versions WHERE namespace = ? AND record_id = ?",
                (NS, RECORD_ID),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE event_journal SET event_type = 'Changed' WHERE event_id = ?",
                (receipt.event_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM event_journal WHERE event_id = ?", (receipt.event_id,)
            )

        # The rows are still there; only the read barrier hides them.
        self.assertEqual(
            1,
            int(self.store.connection.execute(
                "SELECT COUNT(*) FROM record_versions WHERE record_id = ?", (RECORD_ID,)
            ).fetchone()[0]),
        )
        self.assertEqual(
            1,
            int(self.store.connection.execute(
                "SELECT COUNT(*) FROM event_journal WHERE event_id = ?", (receipt.event_id,)
            ).fetchone()[0]),
        )


class DeletionSchemaTests(DeletionTestCase):
    def test_status_values_are_closed_at_the_schema_level(self) -> None:
        self.record()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO deletion_targets ("
                "target_kind, namespace, target_id, status, authorization_ref, "
                "created_at, updated_at) VALUES ('record', ?, ?, 'deleted', 'auth:x', ?, ?)",
                (NS, RECORD_ID, RECORDED_AT, RECORDED_AT),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO deletion_targets ("
                "target_kind, namespace, target_id, status, authorization_ref, "
                "created_at, updated_at) VALUES ('namespace', ?, ?, 'pending_cleanup', 'auth:x', ?, ?)",
                (NS, RECORD_ID, RECORDED_AT, RECORDED_AT),
            )
        self.assertEqual(0, self.target_rows())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
