"""PCS2-008-R1: a deleted pending source must not block the cognition loop.

The regression this file locks down: Tx A created ``pending_work`` for an event,
then the event was deleted before derivation.  Before R1 every later
``build_context`` raised ``NEEDS_CONTEXT`` forever because the deleted source
was still in the unfinished queue and could not be completed or cancelled.

R1 gives that work a durable terminal state:

* Tx1 writes ``work_progress(outcome='skipped', reason='source_redacted',
  context_id=NULL)`` in the same transaction as the deletion barrier;
* ``CognitionStore.open`` performs one bounded, idempotent reconciliation for
  barriers that predate R1;
* ``unfinished_work`` defensively excludes deleted sources even before the
  skip row exists;
* a Context lists the deleted source only as a body-free
  ``{ref, reason:"source_redacted"}`` omission, never as a pending source;
* a deleted trigger returns ``no_pending_work`` with an explicit note instead
  of fabricating a Context/WAIT.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
SRC_DIR = HERE.parents[1] / "src"

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.foundation.errors import (  # noqa: E402
    ContextStale,
    Redacted,
    SourceRedacted,
)


class RedactedWorkRecoveryTests(CognitionTestCase):
    # ------------------------------------------------------------- helpers

    def _historic_barrier(self, event_id: str) -> None:
        """Commit a deletion barrier without the R1 Tx1 hook.

        This mimics a database written before R1 (or a writer that died before
        the hook existed): the event is under the barrier, its work has no
        ``work_progress`` row, and the body has not been cleaned yet.
        """

        now = "2026-10-17T00:00:00Z"
        with self.store.transaction():
            self.store.connection.execute(
                "INSERT INTO deletion_targets ("
                "target_kind, namespace, target_id, status, authorization_ref, "
                "created_at, updated_at"
                ") VALUES ('event', ?, ?, 'pending_cleanup', "
                "'auth:historic-barrier', ?, ?)",
                (NS, event_id, now, now),
            )
            self.store.connection.execute(
                "UPDATE namespace_state "
                "SET state_revision = state_revision + 1, "
                "    privacy_epoch = privacy_epoch + 1 "
                "WHERE namespace = ?",
                (NS,),
            )

    def _run_reconcile_child(self, event_id: str) -> dict:
        marker = self.tmp_path / f"reconcile-{uuid.uuid4().hex}.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(HERE / "cognition_child.py"),
                str(SRC_DIR),
                "reconcile",
                str(self.db_path),
                NS,
                event_id,
                str(marker),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(marker.read_text(encoding="utf-8"))

    def _skipped(self, work_id: str):
        progress = self.store.work_progress(work_id)
        self.assertIsNotNone(progress)
        self.assertEqual("skipped", progress.outcome)
        self.assertEqual("source_redacted", progress.reason)
        self.assertIsNone(progress.context_id)
        return progress

    def _event_payload_body(self, event_id: str) -> tuple[str, str]:
        row = self.store.connection.execute(
            "SELECT ej.payload_ref AS payload_ref, pp.body_json AS body_json "
            "FROM event_journal ej "
            "JOIN private_payloads pp ON pp.payload_ref = ej.payload_ref "
            "WHERE ej.namespace = ? AND ej.event_id = ?",
            (NS, event_id),
        ).fetchone()
        return row["payload_ref"], row["body_json"]

    # --------------------------------------------------------------- cases

    def test_two_pending_sources_delete_one_still_processes_the_other(self) -> None:
        first = self.ingest(
            source_instance="first",
            event_type="CapabilityObserved:file-read",
            payload={"secret": "UNIQUE-DELETED-SOURCE-MARKER"},
        )
        second = self.ingest(
            source_instance="second",
            event_type="CapabilityObserved:file-read",
            causation_refs=(self.event_ref(first.event_id),),
        )
        first_work = self.work_for(first.event_id)
        self.assertIsNotNone(first_work)
        before_delete = self.watermarks()

        self.delete_event(first.event_id)
        after_delete = self.watermarks()
        self.assertEqual(before_delete[0] + 1, after_delete[0])
        self.assertEqual(before_delete[1], after_delete[1])
        self.assertEqual(before_delete[2] + 1, after_delete[2])
        skipped = self._skipped(first_work.work_id)
        self.assertEqual(after_delete[0], skipped.completed_state_revision)
        # The skip itself is watermark-neutral.
        self.assertEqual(after_delete, self.watermarks())

        result = self.run_step(second.event_id)
        self.assertEqual("committed", result.outcome)
        context = self.contexts.load_context(result.context_id)
        self.assertNotIn(
            first.event_id,
            {ref.event_id for ref in context.pending_source_event_refs},
        )
        self.assertTrue(
            any(
                omission.get("reason") == "source_redacted"
                and omission.get("ref", {}).get("event_id") == first.event_id
                for omission in context.omissions
            )
        )
        self.assertNotIn("UNIQUE-DELETED-SOURCE-MARKER", context.rendered_input)

        terminal = self.run_step(first.event_id)
        self.assertEqual("no_pending_work", terminal.outcome)
        self.assertIsNone(terminal.context_id)
        self.assertIn("no processable work", terminal.note or "")

        rows = self.store.work_progress_rows(namespace=NS)
        by_work = {row.work_id: row for row in rows}
        self.assertEqual("skipped", by_work[first_work.work_id].outcome)
        self.assertEqual("committed", by_work[result.work_id].outcome)
        self.assertEqual(2, len(rows))

    def test_delete_before_cleanup_does_not_block(self) -> None:
        first = self.ingest(
            source_instance="before-cleanup",
            event_type="CapabilityObserved:file-read",
        )
        second = self.ingest(
            source_instance="after-cleanup",
            event_type="CapabilityObserved:file-read",
            causation_refs=(self.event_ref(first.event_id),),
        )
        first_work = self.work_for(first.event_id)
        payload_ref, payload_body = self._event_payload_body(first.event_id)
        self.assertTrue(payload_body)

        self.delete_event(first.event_id)  # barrier committed, cleanup NOT run
        # The body still physically exists; the barrier is what protects it.
        still_present = self.store.connection.execute(
            "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
            (payload_ref,),
        ).fetchone()
        self.assertTrue(still_present["body_json"])
        with self.assertRaises(Redacted):
            self.store.get_event(NS, first.event_id)
        self._skipped(first_work.work_id)

        result = self.run_step(second.event_id)
        self.assertEqual("committed", result.outcome)
        context = self.contexts.load_context(result.context_id)
        self.assertNotIn(
            first.event_id,
            {ref.event_id for ref in context.pending_source_event_refs},
        )
        self.assertTrue(
            any(
                omission.get("reason") == "source_redacted"
                and omission.get("ref", {}).get("event_id") == first.event_id
                for omission in context.omissions
            )
        )

    def test_historic_barrier_is_recovered_by_a_new_process(self) -> None:
        event = self.ingest(
            source_instance="historic",
            event_type="CapabilityObserved:file-read",
        )
        work = self.work_for(event.event_id)
        self._historic_barrier(event.event_id)
        self.assertIsNone(self.store.work_progress(work.work_id))
        before = self.watermarks()

        marker = self._run_reconcile_child(event.event_id)
        self.assertEqual("reconciled", marker["outcome"])
        self.assertGreaterEqual(marker["skipped"], 1)

        skipped = self._skipped(work.work_id)
        self.assertEqual(before[0], skipped.completed_state_revision)
        self.assertEqual(before, self.watermarks())

        terminal = self.run_step(event.event_id)
        self.assertEqual("no_pending_work", terminal.outcome)
        self.assertIn("no processable work", terminal.note or "")

        # A second coordination call is a pure no-op.
        report = self.store.reconcile_redacted_work(namespace=NS)
        self.assertEqual(0, report.skipped_count)
        self.assertEqual(before, self.watermarks())
        rows = [
            row
            for row in self.store.work_progress_rows(namespace=NS)
            if row.work_id == work.work_id
        ]
        self.assertEqual(1, len(rows))

    def test_reconcile_is_idempotent_and_watermark_neutral(self) -> None:
        event = self.ingest(
            source_instance="idempotent",
            event_type="CapabilityObserved:file-read",
        )
        work = self.work_for(event.event_id)
        self._historic_barrier(event.event_id)
        before = self.watermarks()

        first = self.store.reconcile_redacted_work(namespace=NS)
        self.assertEqual(1, first.skipped_count)
        second = self.store.reconcile_redacted_work(namespace=NS)
        self.assertEqual(0, second.skipped_count)
        self.assertEqual(before, self.watermarks())

        skipped = self._skipped(work.work_id)
        self.assertEqual(before[0], skipped.completed_state_revision)
        self.assertEqual(
            1,
            len(
                [
                    row
                    for row in self.store.work_progress_rows(namespace=NS)
                    if row.work_id == work.work_id
                ]
            ),
        )
        with self.assertRaises(sqlite3.IntegrityError) as updated:
            self.store.connection.execute(
                "UPDATE work_progress SET outcome = 'committed' WHERE work_id = ?",
                (work.work_id,),
            )
        self.assertIn("immutable", str(updated.exception))

    def test_all_deleted_pending_sources_return_no_work(self) -> None:
        first = self.ingest(source_instance="all-first")
        second = self.ingest(source_instance="all-second")
        first_work = self.work_for(first.event_id)
        second_work = self.work_for(second.event_id)
        self.delete_event(first.event_id)
        self.delete_event(second.event_id)
        before = self.watermarks()

        first_result = self.run_step(first.event_id)
        second_result = self.run_step(second.event_id)
        for result in (first_result, second_result):
            self.assertEqual("no_pending_work", result.outcome)
            self.assertTrue(result.ok)
            self.assertIsNone(result.context_id)
            self.assertIn("no processable work", result.note or "")
        self.assertEqual(before, self.watermarks())
        self.assertEqual(0, self.count_rows("context_snapshots"))
        self.assertEqual(
            0, len(self.store.current_versions_of_kind(NS, "belief"))
        )
        self.assertEqual("skipped", self.store.work_progress(first_work.work_id).outcome)
        self.assertEqual("skipped", self.store.work_progress(second_work.work_id).outcome)

    def test_old_context_after_a_pending_source_deletion_is_context_stale(self) -> None:
        first = self.ingest(source_instance="old-context-first")
        second = self.ingest(
            source_instance="old-context-second",
            event_type="CapabilityObserved:file-read",
        )
        context = self.build_context(second.event_id)
        self.assertIn(
            first.event_id,
            {ref.event_id for ref in context.pending_source_event_refs},
        )
        self.delete_event(first.event_id)
        before_versions = self.count_rows("record_versions")

        with self.assertRaises(ContextStale) as caught:
            self.beliefs.propose_belief(
                namespace=NS,
                context_id=context.context_id,
                request_id=str(uuid.uuid4()),
                body=self.belief_body(evidence_links=[]),
                scope={},
            )
        self.assertEqual("CONTEXT_STALE", caught.exception.code)
        self.assertEqual(before_versions, self.count_rows("record_versions"))
        # The untouched second source is still processable; the deleted first
        # source is terminal.
        self.assertIsNotNone(self.work_for(second.event_id))
        self.assertIsNone(self.work_for(first.event_id))

        result = self.run_step(second.event_id)
        self.assertEqual("committed", result.outcome)

    def test_skip_preserves_the_source_tombstone(self) -> None:
        event = self.ingest(
            source_instance="tombstone-instance",
            source_event_id="tombstone-key",
            event_type="CapabilityObserved:file-read",
        )
        work = self.work_for(event.event_id)
        self.delete_event(event.event_id)
        self._skipped(work.work_id)

        with self.assertRaises(Redacted):
            self.store.get_event(NS, event.event_id)
        with self.assertRaises(SourceRedacted):
            self.ingest(
                source_instance="tombstone-instance",
                source_event_id="tombstone-key",
                event_type="CapabilityObserved:file-read",
            )
        # The terminal skip row did not create a Context or revive the source.
        self.assertEqual(0, self.count_rows("context_snapshots"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
