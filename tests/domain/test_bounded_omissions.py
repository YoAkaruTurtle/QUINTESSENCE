"""PCS2-008-R1.1: bound Context `source_redacted` omissions to this round.

R1 made a deleted pending source terminal, but it also enumerated **every**
historical skipped source in every later Context.  A long-lived namespace could
therefore exceed the 24 KiB brief with unrelated history and block a new event.

R1.1 keeps the durable audit (`work_progress(outcome='skipped')`) and changes
only the Context omission scope:

* candidates are this round's trusted trigger, the current pending batch, and
  their server-validated causation refs;
* a deleted source in that candidate range is reported as a minimal
  `{ref, reason:"source_redacted"}` omission and is not listed as a pending
  source or rendered as body;
* historical skipped work outside that range is not enumerated, not rendered,
  and does not consume the budget;
* if the real current-round omission set does not fit, the existing explicit
  `CONTEXT_TOO_LARGE` failure remains; nothing is silently truncated.

T1–T6 below are the acceptance cases required by the R1.1 card.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
SRC_DIR = HERE.parents[1] / "src"

from cognition_support import NS, NS_B, CognitionTestCase  # noqa: E402

from quintessence_pcs.foundation.errors import (  # noqa: E402
    ContextStale,
    Redacted,
    SourceRedacted,
)
from quintessence_pcs.foundation.jsonutil import canonical_json_text  # noqa: E402
from quintessence_pcs.store.cognition import MAX_CONTEXT_BYTES  # noqa: E402


class BoundedOmissionTests(CognitionTestCase):
    # ------------------------------------------------------------- helpers

    def _make_deleted_events(self, count: int, prefix: str) -> list[str]:
        event_ids: list[str] = []
        for index in range(count):
            event = self.ingest(
                source_instance=f"{prefix}-{index}",
                event_type="TestTrigger",
            )
            event_ids.append(event.event_id)
            self.delete_event(event.event_id)
        return event_ids

    def _skipped_event_ids(self) -> set[str]:
        rows = self.store.connection.execute(
            "SELECT p.event_id AS event_id "
            "FROM pending_work p "
            "JOIN work_progress w ON w.work_id = p.work_id "
            "WHERE p.namespace = ? AND w.outcome = 'skipped' "
            "ORDER BY p.created_at, p.work_id",
            (NS,),
        ).fetchall()
        return {str(row["event_id"]) for row in rows}

    def _historical_omission_bytes(self) -> tuple[int, set[str]]:
        """What the old R1 algorithm would serialize for this namespace."""

        event_ids = self._skipped_event_ids()
        old_omissions = [
            {
                "ref": self.event_ref(event_id),
                "reason": "source_redacted",
            }
            for event_id in sorted(event_ids)
        ]
        return (
            len(canonical_json_text(old_omissions).encode("utf-8")),
            event_ids,
        )

    @staticmethod
    def _omitted_event_ids(context) -> set[str]:
        ids: set[str] = set()
        for omission in context.omissions:
            if omission.get("reason") != "source_redacted":
                continue
            ref = omission.get("ref")
            if (
                isinstance(ref, dict)
                and ref.get("ref_type") == "event"
                and isinstance(ref.get("event_id"), str)
            ):
                ids.add(ref["event_id"])
        return ids

    @staticmethod
    def _pending_event_ids(context) -> set[str]:
        return {ref.event_id for ref in context.pending_source_event_refs}

    def _new_live_event(self, *, instance: str = "unrelated-new"):
        return self.ingest(
            source_instance=instance,
            event_type="CapabilityObserved:file-read",
        )

    def _open_store_in_new_process(self, event_id: str) -> dict:
        """A real second process opening/migrating/reconciling the same DB."""

        marker = self.tmp_path / f"t4-open-{uuid.uuid4().hex}.json"
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

    # ------------------------------------------------------------------ T1

    def test_t1_historical_skipped_do_not_consume_an_unrelated_context(self) -> None:
        historical = self._make_deleted_events(230, "t1-history")
        old_bytes, old_ids = self._historical_omission_bytes()
        self.assertEqual(set(historical), old_ids)
        # The old R1 algorithm would have put all of these into the Context.
        self.assertGreater(old_bytes, MAX_CONTEXT_BYTES)

        # The terminal state is already durable via Tx1; reconciliation is a
        # no-op and must not move watermarks.
        before = self.watermarks()
        report = self.store.reconcile_redacted_work(namespace=NS)
        self.assertEqual(0, report.skipped_count)
        self.assertEqual(before, self.watermarks())

        new = self._new_live_event()
        result = self.run_step(new.event_id)
        self.assertEqual("committed", result.outcome)
        context = self.contexts.load_context(result.context_id)
        self.assertEqual(set(), self._omitted_event_ids(context))
        self.assertFalse(old_ids & self._omitted_event_ids(context))
        self.assertLessEqual(
            len(context.rendered_input.encode("utf-8")), MAX_CONTEXT_BYTES
        )
        self.assertIn(new.event_id, self._pending_event_ids(context))
        # The audit is still there; only the unrelated Context stopped copying it.
        self.assertEqual(230, len(self._skipped_event_ids()))

    # ------------------------------------------------------------------ T2

    def test_t2_deleted_candidate_in_the_current_batch_is_omitted(self) -> None:
        deleted = self.ingest(
            source_instance="t2-deleted",
            event_type="CapabilityObserved:file-read",
            payload={"secret": "T2-DELETED-BODY-SECRET"},
        )
        live = self.ingest(
            source_instance="t2-live",
            event_type="CapabilityObserved:file-read",
            causation_refs=(self.event_ref(deleted.event_id),),
        )
        deleted_work = self.work_for(deleted.event_id)
        self.delete_event(deleted.event_id)

        context = self.build_context(live.event_id)
        omitted = self._omitted_event_ids(context)
        self.assertEqual({deleted.event_id}, omitted)
        self.assertNotIn(deleted.event_id, self._pending_event_ids(context))
        self.assertIn(live.event_id, self._pending_event_ids(context))
        self.assertNotIn("T2-DELETED-BODY-SECRET", context.rendered_input)
        progress = self.store.work_progress(deleted_work.work_id)
        self.assertEqual("skipped", progress.outcome)
        self.assertIsNone(progress.context_id)

        result = self.run_step(live.event_id)
        self.assertEqual("committed", result.outcome)

    # ------------------------------------------------------------------ T3

    def test_t3_omission_does_not_leak_into_the_next_round(self) -> None:
        deleted = self.ingest(
            source_instance="t3-deleted",
            event_type="TestTrigger",
        )
        first_trigger = self.ingest(
            source_instance="t3-first",
            event_type="TestTrigger",
            causation_refs=(self.event_ref(deleted.event_id),),
        )
        self.delete_event(deleted.event_id)
        first_result = self.run_step(first_trigger.event_id)
        self.assertEqual("checked_no_belief", first_result.outcome)
        first = self.contexts.load_context(first_result.context_id)
        self.assertIn(deleted.event_id, self._omitted_event_ids(first))

        second_trigger = self.ingest(
            source_instance="t3-second",
            event_type="CapabilityObserved:file-read",
        )
        second = self.build_context(second_trigger.event_id)
        self.assertNotIn(deleted.event_id, self._omitted_event_ids(second))
        self.assertEqual(set(), self._omitted_event_ids(second))
        result = self.run_step(second_trigger.event_id)
        self.assertEqual("committed", result.outcome)

    # ------------------------------------------------------------------ T4

    def test_t4_restart_and_reconcile_do_not_reenumerate_history(self) -> None:
        historical = self._make_deleted_events(5, "t4-history")
        before = self.watermarks()

        # A real second process opens/migrates/reconciles the same DB.  The
        # historical skipped rows are already terminal, so this is a no-op; it
        # must not move watermarks or re-enumerate them later.
        marker = self._open_store_in_new_process(historical[0])
        self.assertEqual("reconciled", marker["outcome"])
        self.assertGreaterEqual(marker["skipped"], 5)
        self.assertEqual(before, self.watermarks())

        report = self.store.reconcile_redacted_work(namespace=NS)
        self.assertEqual(0, report.skipped_count)
        self.assertEqual(before, self.watermarks())
        self.assertEqual(set(historical), self._skipped_event_ids())

        new = self._new_live_event(instance="t4-new")
        result = self.run_step(new.event_id)
        self.assertEqual("committed", result.outcome)
        context = self.contexts.load_context(result.context_id)
        self.assertFalse(
            self._omitted_event_ids(context) & set(historical)
        )
        self.assertEqual(set(), self._omitted_event_ids(context))
        self.assertEqual(set(historical), self._skipped_event_ids())

    # ------------------------------------------------------------------ T5

    def test_t5_old_context_stays_stale_and_source_stays_redacted(self) -> None:
        deleted = self.ingest(
            source_instance="t5-deleted",
            source_event_id="t5-key",
            event_type="CapabilityObserved:file-read",
            payload={"secret": "T5-SECRET"},
        )
        live = self.ingest(
            source_instance="t5-live",
            event_type="CapabilityObserved:file-read",
            causation_refs=(self.event_ref(deleted.event_id),),
        )
        old_context = self.build_context(live.event_id)
        self.assertEqual(set(), self._omitted_event_ids(old_context))
        # PCS2-010-P0: a live pending source body is projected as data while
        # it is live (the pre-card invariant was "never copy any payload").
        self.assertIn("T5-SECRET", old_context.rendered_input)

        self.delete_event(deleted.event_id)
        before_versions = self.count_rows("record_versions")
        with self.assertRaises(ContextStale) as caught:
            self.beliefs.propose_belief(
                namespace=NS,
                context_id=old_context.context_id,
                request_id=str(uuid.uuid4()),
                body=self.belief_body(evidence_links=[]),
                scope={},
            )
        self.assertEqual("CONTEXT_STALE", caught.exception.code)
        self.assertEqual(before_versions, self.count_rows("record_versions"))

        with self.assertRaises(Redacted):
            self.store.get_event(NS, deleted.event_id)
        with self.assertRaises(SourceRedacted):
            self.ingest(
                source_instance="t5-deleted",
                source_event_id="t5-key",
                event_type="CapabilityObserved:file-read",
            )
        # The deletion invalidates the stored projection, and a Context built
        # after it never contains the deleted body.
        self.store.cleanup_deletions()
        with self.assertRaises(Redacted):
            self.store.load_context_snapshot(old_context.context_id)
        fresh = self.build_context(
            self.ingest(
                source_instance="t5-fresh",
                event_type="CapabilityObserved:file-read",
            ).event_id
        )
        self.assertNotIn("T5-SECRET", fresh.rendered_input)

    # ------------------------------------------------------------------ T6

    def test_t6_cross_namespace_deleted_source_does_not_leak(self) -> None:
        foreign = self.ingest(
            namespace=NS_B,
            source_instance="t6-foreign",
            event_type="TestTrigger",
            payload={"secret": "FOREIGN-SECRET"},
        )
        self.delete_event(foreign.event_id, namespace=NS_B)
        trigger = self._new_live_event(instance="t6-main")
        context = self.build_context(trigger.event_id)
        self.assertEqual(set(), self._omitted_event_ids(context))
        self.assertNotIn(foreign.event_id, self._omitted_event_ids(context))
        self.assertNotIn("FOREIGN-SECRET", context.rendered_input)

    def test_t6_many_current_round_candidates_are_all_reported(self) -> None:
        related = self._make_deleted_events(40, "t6-related")
        trigger = self.ingest(
            source_instance="t6-related-trigger",
            event_type="CapabilityObserved:file-read",
            causation_refs=tuple(self.event_ref(event_id) for event_id in related),
        )
        context = self.build_context(trigger.event_id)
        self.assertEqual(set(related), self._omitted_event_ids(context))
        self.assertLessEqual(
            len(context.rendered_input.encode("utf-8")), MAX_CONTEXT_BYTES
        )
        result = self.run_step(trigger.event_id)
        self.assertEqual("committed", result.outcome)

    def test_t6_overflowing_current_candidates_fail_explicitly(self) -> None:
        from quintessence_pcs.store.errors import ContextTooLarge

        related = self._make_deleted_events(180, "t6-overflow")
        trigger = self.ingest(
            source_instance="t6-overflow-trigger",
            event_type="CapabilityObserved:file-read",
            causation_refs=tuple(self.event_ref(event_id) for event_id in related),
        )
        before_contexts = self.count_rows("context_snapshots")
        with self.assertRaises(ContextTooLarge):
            self.build_context(trigger.event_id)
        # Explicit failure, not a silently truncated omission list: no Context
        # row was written and the trigger work is still pending.
        self.assertEqual(before_contexts, self.count_rows("context_snapshots"))
        self.assertIsNotNone(self.work_for(trigger.event_id))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
