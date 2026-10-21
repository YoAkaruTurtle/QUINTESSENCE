"""PCS2-008 C4/C5: the bounded step, atomic completion and crash windows.

The critical property is that ``belief append`` and ``work_progress`` share one
Tx B transaction.  The tests therefore include an explicit "progress row already
exists" case (the belief append must roll back), an in-process two-context race
on the primary key, and real subprocess kills after and before the commit.
Nothing here uses a Goal or a scheduler: one call is one step.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains.cognition.beliefs import BeliefService  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    ContextStale,
    EventNotFound,
    ScopeViolation,
)
from quintessence_pcs.runtime import CognitionRuntime  # noqa: E402
from quintessence_pcs.runtime.step import MODEL_FAILURE_CODE  # noqa: E402
from quintessence_pcs.store.errors import (  # noqa: E402
    InvalidWorkCompletion,
    WorkAlreadyCompleted,
)

SRC_DIR = HERE.parents[1] / "src"


class ScriptedModel:
    """Test double implementing the same interface as ``FakeModel``."""

    adapter_kind = "fake-model"
    policy_version = "pcs2-009.test-scripted.v1"
    model_ref = "test:scripted"

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0
        self.feedback: list[str | None] = []

    def propose(self, *, rendered_input, references, feedback=None):
        self.calls += 1
        self.feedback.append(feedback)
        value = self._outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class BoundedStepTests(CognitionTestCase):
    # ------------------------------------------------------------- happy path

    def test_run_step_commits_belief_and_work_in_one_step(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        before = self.revision()
        result = self.run_step(event.event_id)

        self.assertEqual("committed", result.outcome)
        self.assertTrue(result.ok)
        self.assertEqual(1, result.model_calls)
        self.assertEqual(0, result.repairs)
        self.assertEqual(before + 1, result.state_revision)
        self.assertIsNotNone(result.belief_ref)
        self.assertEqual("supported", result.belief_status)
        self.assertIsNotNone(result.receipt)
        self.assertEqual("committed", result.receipt.outcome)

        progress = self.store.work_progress(result.work_id)
        self.assertIsNotNone(progress)
        self.assertEqual("committed", progress.outcome)
        self.assertEqual(result.state_revision, progress.completed_state_revision)

        # pending_work is never rewritten (0002 CHECK); completion is its own row.
        row = self.store.connection.execute(
            "SELECT status FROM pending_work WHERE work_id = ?", (result.work_id,)
        ).fetchone()
        self.assertEqual("pending", row["status"])

        belief = self.store.get_current(NS, result.belief_ref.record_id)
        self.assertEqual("supported", belief.status)
        self.assertEqual("capability:file-read", belief.body["claim"])
        self.assertEqual(
            {"scene": "pcs2-008.synthetic.file-read"}, belief.scope
        )

        again = self.run_step(event.event_id)
        self.assertEqual("no_pending_work", again.outcome)
        self.assertEqual(result.state_revision, again.state_revision)
        self.assertEqual(1, len(self.store.work_progress_rows(namespace=NS)))

    def test_second_step_revises_the_same_belief(self) -> None:
        first = self.ingest(
            source_instance="step-one", event_type="CapabilityObserved:file-read"
        )
        one = self.run_step(first.event_id)
        self.assertEqual("committed", one.outcome)

        second = self.ingest(
            source_instance="step-two", event_type="CapabilityObserved:file-read"
        )
        two = self.run_step(second.event_id)
        self.assertEqual("committed", two.outcome)
        self.assertEqual(one.belief_ref.record_id, two.belief_ref.record_id)
        belief = self.store.get_current(NS, one.belief_ref.record_id)
        self.assertEqual(2, belief.record_version)
        self.assertEqual("supported", belief.status)
        self.assertEqual("moderate", belief.body["strength"])
        self.assertEqual(2, len(belief.body["evidence_links"]))

    def test_wait_completes_work_without_a_belief_or_revision(self) -> None:
        event = self.ingest(event_type="NotACapability")
        before = self.revision()
        result = self.run_step(event.event_id)
        self.assertEqual("checked_no_belief", result.outcome)
        self.assertTrue(result.ok)
        self.assertIsNone(result.belief_ref)
        self.assertIsNotNone(result.receipt)
        self.assertEqual("noop", result.receipt.outcome)
        self.assertEqual(before, result.state_revision)
        self.assertEqual(before, self.revision())
        progress = self.store.work_progress(result.work_id)
        self.assertEqual("checked_no_belief", progress.outcome)
        self.assertEqual(
            0, len(self.store.current_versions_of_kind(NS, "belief"))
        )

    # ---------------------------------------------------------------- repair

    def test_one_format_repair_then_commit(self) -> None:
        model = ScriptedModel(
            ["not json at all", '{"operation":"wait","reason":"repaired"}']
        )
        event = self.ingest(event_type="NotACapability")
        runtime = CognitionRuntime(self.store, model=model, clock=self.clock)
        result = runtime.run_step(namespace=NS, trigger_event_id=event.event_id)
        self.assertEqual("checked_no_belief", result.outcome)
        self.assertEqual(2, result.model_calls)
        self.assertEqual(1, result.repairs)
        self.assertIsNotNone(model.feedback[1])
        self.assertIsNotNone(self.store.work_progress(result.work_id))

    def test_repair_exhausted_leaves_the_work_pending(self) -> None:
        model = ScriptedModel(["still not json", "also not json"])
        event = self.ingest(event_type="NotACapability")
        before = self.revision()
        runtime = CognitionRuntime(self.store, model=model, clock=self.clock)
        result = runtime.run_step(namespace=NS, trigger_event_id=event.event_id)
        self.assertEqual("failed", result.outcome)
        self.assertEqual("INVALID_SCHEMA", result.error_code)
        self.assertEqual(2, result.model_calls)
        self.assertEqual(1, result.repairs)
        self.assertIsNone(self.store.work_progress(result.work_id))
        self.assertIsNotNone(self.work_for(event.event_id))
        self.assertEqual(before, self.revision())

    def test_unsupported_operation_is_repaired_once_then_fails(self) -> None:
        model = ScriptedModel(
            [
                '{"operation":"open_question"}',
                '{"operation":"open_question"}',
            ]
        )
        event = self.ingest(event_type="NotACapability")
        runtime = CognitionRuntime(self.store, model=model, clock=self.clock)
        result = runtime.run_step(namespace=NS, trigger_event_id=event.event_id)
        self.assertEqual("failed", result.outcome)
        self.assertEqual("UNSUPPORTED_OPERATION", result.error_code)
        self.assertIsNone(self.store.work_progress(result.work_id))

    def test_model_supplied_state_revision_is_rejected_not_used(self) -> None:
        model = ScriptedModel(
            [
                '{"operation":"wait","reason":"x","state_revision":999}',
                '{"operation":"wait","reason":"repaired"}',
            ]
        )
        event = self.ingest(event_type="NotACapability")
        before = self.revision()
        runtime = CognitionRuntime(self.store, model=model, clock=self.clock)
        result = runtime.run_step(namespace=NS, trigger_event_id=event.event_id)
        self.assertEqual("checked_no_belief", result.outcome)
        self.assertEqual(2, result.model_calls)
        self.assertEqual(1, result.repairs)
        self.assertEqual(before, result.state_revision)
        self.assertNotEqual(999, result.state_revision)

    def test_model_exception_is_a_failed_step_without_progress(self) -> None:
        model = ScriptedModel([RuntimeError("boom")])
        event = self.ingest(event_type="NotACapability")
        runtime = CognitionRuntime(self.store, model=model, clock=self.clock)
        result = runtime.run_step(namespace=NS, trigger_event_id=event.event_id)
        self.assertEqual("failed", result.outcome)
        self.assertEqual(MODEL_FAILURE_CODE, result.error_code)
        self.assertIsNone(self.store.work_progress(result.work_id))
        self.assertIsNotNone(self.work_for(event.event_id))

    def test_noop_revision_completes_work_without_a_new_version(self) -> None:
        first = self.ingest(
            source_instance="noop-one", event_type="CapabilityObserved:file-read"
        )
        step = self.run_step(first.event_id)
        belief = self.store.get_current(NS, step.belief_ref.record_id)
        second = self.ingest(
            source_instance="noop-two", event_type="CapabilityObserved:file-read"
        )
        proposal = {
            "operation": "revise_belief",
            "target": self.record_ref(belief),
            "body": dict(belief.body),
            "scope": dict(belief.scope or {}),
        }
        model = ScriptedModel([json.dumps(proposal)])
        runtime = CognitionRuntime(self.store, model=model, clock=self.clock)
        result = runtime.run_step(namespace=NS, trigger_event_id=second.event_id)
        self.assertEqual("checked_no_belief", result.outcome)
        self.assertEqual("noop", result.receipt.outcome)
        if result.work_id is None:
            self.fail("the no-op step did not complete a work item")
        progress = self.store.work_progress(result.work_id)
        self.assertEqual("checked_no_belief", progress.outcome)
        current = self.store.get_current(NS, belief.record_id)
        self.assertEqual(1, current.record_version)
        versions = self.store.connection.execute(
            "SELECT COUNT(*) FROM record_versions "
            "WHERE namespace = ? AND record_id = ?",
            (NS, belief.record_id),
        ).fetchone()[0]
        self.assertEqual(1, versions)

    # ------------------------------------------------------------- atomicity

    def test_work_progress_primary_key_decides_the_race(self) -> None:
        event = self.ingest(event_type="NotACapability")
        first_context = self.build_context(event.event_id)
        second_context = self.build_context(event.event_id)
        self.assertEqual(
            first_context.state_revision, second_context.state_revision
        )
        self.assertEqual(
            first_context.context_event_seq, second_context.context_event_seq
        )
        work = self.work_for(event.event_id)
        before = self.revision()

        self.commit_wait(first_context, work)
        with self.assertRaises(WorkAlreadyCompleted):
            self.commit_wait(second_context, work)

        self.assertEqual(before, self.revision())
        self.assertEqual(1, len(self.store.work_progress_rows(namespace=NS)))
        wait_rows = self.store.connection.execute(
            "SELECT COUNT(*) FROM processed_commands "
            "WHERE namespace = ? AND operation = 'cognition.wait'",
            (NS,),
        ).fetchone()[0]
        self.assertEqual(1, wait_rows)
        self.assertEqual("no_pending_work", self.run_step(event.event_id).outcome)

    def test_belief_append_rolls_back_when_progress_already_exists(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        wait_context = self.build_context(event.event_id)
        work = self.work_for(event.event_id)
        self.commit_wait(wait_context, work)

        belief_context = self.build_context(event.event_id)
        body = self.belief_body(
            evidence_links=[self.evidence_link(self.event_ref(event.event_id))]
        )
        proposal = {
            "operation": "propose_belief",
            "body": body,
            "scope": self.default_scope("capability"),
        }
        beliefs_before = self.count_rows("record_versions")
        commands_before = self.count_rows("processed_commands")
        with self.assertRaises(WorkAlreadyCompleted):
            self.beliefs.submit_proposal(
                namespace=NS,
                context_id=belief_context.context_id,
                request_id=str(uuid.uuid4()),
                proposal=proposal,
                work_id=work.work_id,
            )
        self.assertEqual(beliefs_before, self.count_rows("record_versions"))
        self.assertEqual(commands_before, self.count_rows("processed_commands"))
        self.assertEqual(1, len(self.store.work_progress_rows(namespace=NS)))

    def test_work_progress_rows_are_immutable(self) -> None:
        event = self.ingest(event_type="NotACapability")
        context = self.build_context(event.event_id)
        work = self.work_for(event.event_id)
        self.commit_wait(context, work)
        with self.assertRaises(sqlite3.IntegrityError) as updated:
            self.store.connection.execute(
                "UPDATE work_progress SET outcome = 'committed' WHERE work_id = ?",
                (work.work_id,),
            )
        self.assertIn("immutable", str(updated.exception))
        with self.assertRaises(sqlite3.IntegrityError) as deleted:
            self.store.connection.execute(
                "DELETE FROM work_progress WHERE work_id = ?", (work.work_id,)
            )
        self.assertIn("immutable", str(deleted.exception))

    # --------------------------------------------------------------- restart

    def test_a_persisted_context_can_be_used_from_another_connection(self) -> None:
        trigger = self.ingest(event_type="CapabilityObserved:file-read")
        context = self.build_context(trigger.event_id)
        work = self.work_for(trigger.event_id)
        proposal = {
            "operation": "propose_belief",
            "body": self.belief_body(
                evidence_links=[
                    self.evidence_link(self.event_ref(trigger.event_id))
                ]
            ),
            "scope": self.default_scope("capability"),
        }
        second = self.open_second_store()
        submission = BeliefService(second, clock=self.clock).submit_proposal(
            namespace=NS,
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            proposal=proposal,
            work_id=work.work_id,
        )
        self.assertTrue(submission.appended)
        progress = second.work_progress(work.work_id)
        self.assertEqual("committed", progress.outcome)
        self.assertEqual(1, len(second.work_progress_rows(namespace=NS)))

    def test_deletion_after_capture_rejects_the_commit(self) -> None:
        trigger = self.ingest(event_type="CapabilityObserved:file-read")
        work = self.work_for(trigger.event_id)
        context = self.build_context(trigger.event_id)
        body = self.belief_body(
            claim="capability:deleted-evidence",
            evidence_links=[self.evidence_link(self.event_ref(trigger.event_id))],
        )
        self.delete_event(trigger.event_id)
        before_versions = self.count_rows("record_versions")
        with self.assertRaises(ContextStale) as caught:
            self.beliefs.propose_belief(
                namespace=NS,
                context_id=context.context_id,
                request_id=str(uuid.uuid4()),
                body=body,
                scope=self.default_scope("capability"),
            )
        self.assertEqual("CONTEXT_STALE", caught.exception.code)
        self.assertNotIn("deleted-evidence", str(caught.exception))
        self.assertEqual(before_versions, self.count_rows("record_versions"))
        # The deleted trigger's work is now terminal, not an unfinished row.
        self.assertIsNone(self.work_for(trigger.event_id))
        progress = self.store.work_progress(work.work_id)
        self.assertIsNotNone(progress)
        self.assertEqual("skipped", progress.outcome)
        self.assertEqual("source_redacted", progress.reason)
        self.assertIsNone(progress.context_id)

    def test_unknown_trigger_raises_not_found(self) -> None:
        with self.assertRaises(EventNotFound):
            self.run_step(str(uuid.uuid4()))

    # ------------------------------------------------------------ subprocess

    def _spawn(self, mode: str, event_id: str, tag: str):
        marker = self.tmp_path / f"marker-{tag}-{uuid.uuid4().hex}.json"
        process = subprocess.Popen(
            [
                sys.executable,
                str(HERE / "cognition_child.py"),
                str(SRC_DIR),
                mode,
                str(self.db_path),
                NS,
                event_id,
                str(marker),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        def cleanup() -> None:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process.stderr is not None:
                process.stderr.close()

        self.addCleanup(cleanup)
        return process, marker

    def _wait_marker(self, process, marker: Path) -> dict:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if marker.exists():
                return json.loads(marker.read_text(encoding="utf-8"))
            if process.poll() is not None:
                break
            time.sleep(0.05)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        detail = ""
        if process.stderr is not None:
            detail = process.stderr.read().decode("utf-8", "replace")
        self.fail(
            f"child produced no marker (exit={process.poll()}): {detail}"
        )

    @staticmethod
    def _kill(process) -> None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    def test_killed_after_commit_is_not_reprocessed(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        process, marker = self._spawn("run", event.event_id, "after")
        payload = self._wait_marker(process, marker)
        self._kill(process)
        self.assertEqual("committed", payload["outcome"])

        second = self.open_second_store()
        runtime = CognitionRuntime(second, clock=self.clock)
        progress = second.work_progress_rows(namespace=NS)
        self.assertEqual(1, len(progress))
        self.assertEqual("committed", progress[0].outcome)
        self.assertEqual(payload["context_id"], progress[0].context_id)
        self.assertEqual(payload["state_revision"], progress[0].completed_state_revision)
        again = runtime.run_step(namespace=NS, trigger_event_id=event.event_id)
        self.assertEqual("no_pending_work", again.outcome)
        self.assertEqual(1, len(second.work_progress_rows(namespace=NS)))
        beliefs = second.current_versions_of_kind(NS, "belief")
        self.assertEqual(1, len(beliefs))

    def test_killed_before_commit_leaves_work_pending_and_retry_is_safe(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        process, marker = self._spawn("build", event.event_id, "before")
        payload = self._wait_marker(process, marker)
        self._kill(process)
        self.assertEqual("built", payload["outcome"])

        second = self.open_second_store()
        context = second.load_context_snapshot(payload["context_id"])
        self.assertTrue(context.rendered_input)
        self.assertIsNotNone(second.unfinished_work_for_event(NS, event.event_id))
        self.assertEqual((), second.work_progress_rows(namespace=NS))

        # Process B continues with exactly the Context process A captured.
        work = second.unfinished_work_for_event(NS, event.event_id)[0]
        proposal = {
            "operation": "propose_belief",
            "body": self.belief_body(
                evidence_links=[
                    self.evidence_link(self.event_ref(event.event_id))
                ]
            ),
            "scope": self.default_scope("capability"),
        }
        submission = BeliefService(second, clock=self.clock).submit_proposal(
            namespace=NS,
            context_id=payload["context_id"],
            request_id=str(uuid.uuid4()),
            proposal=proposal,
            work_id=work.work_id,
        )
        self.assertTrue(submission.appended)
        self.assertEqual(1, len(second.work_progress_rows(namespace=NS)))
        self.assertEqual(1, len(second.current_versions_of_kind(NS, "belief")))
        runtime = CognitionRuntime(second, clock=self.clock)
        again = runtime.run_step(namespace=NS, trigger_event_id=event.event_id)
        self.assertEqual("no_pending_work", again.outcome)

    def test_two_processes_only_one_commits(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        first_process, first_marker = self._spawn("run", event.event_id, "race-a")
        second_process, second_marker = self._spawn("run", event.event_id, "race-b")
        first = self._wait_marker(first_process, first_marker)
        second = self._wait_marker(second_process, second_marker)
        self._kill(first_process)
        self._kill(second_process)

        outcomes = {first["outcome"], second["outcome"]}
        self.assertIn("committed", outcomes)
        self.assertEqual(
            1,
            sum(1 for outcome in outcomes if outcome == "committed"),
            outcomes,
        )
        store = self.open_second_store()
        progress = store.work_progress_rows(namespace=NS)
        self.assertEqual(1, len(progress))
        self.assertEqual("committed", progress[0].outcome)
        self.assertEqual(1, len(store.current_versions_of_kind(NS, "belief")))


class TxScopeCompletionTests(CognitionTestCase):
    """The restricted ``complete_work`` method cannot be used to fake progress."""

    def _wait_context(self):
        event = self.ingest(event_type="NotACapability")
        context = self.build_context(event.event_id)
        work = self.work_for(event.event_id)
        return context, work

    def _command(self, context, work, apply):
        return self.store.commit_command(
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            operation="cognition.wait",
            request_payload={"work_id": work.work_id},
            apply=apply,
            namespace=NS,
        )

    def test_complete_work_requires_the_scope_context(self) -> None:
        context, work = self._wait_context()

        def apply(scope):
            scope.complete_work(
                work_id=work.work_id,
                context_id="a-different-context",
                outcome="checked_no_belief",
            )

        with self.assertRaises(ScopeViolation):
            self._command(context, work, apply)
        self.assertIsNone(self.store.work_progress(work.work_id))

    def test_complete_work_cannot_run_twice(self) -> None:
        context, work = self._wait_context()

        def apply(scope):
            scope.complete_work(
                work_id=work.work_id,
                context_id=context.context_id,
                outcome="checked_no_belief",
            )
            scope.complete_work(
                work_id=work.work_id,
                context_id=context.context_id,
                outcome="checked_no_belief",
            )

        with self.assertRaises(InvalidWorkCompletion):
            self._command(context, work, apply)
        self.assertIsNone(self.store.work_progress(work.work_id))

    def test_append_after_complete_work_is_rejected(self) -> None:
        context, work = self._wait_context()

        def apply(scope):
            scope.complete_work(
                work_id=work.work_id,
                context_id=context.context_id,
                outcome="checked_no_belief",
            )
            scope.append_record_version(
                record_id=str(uuid.uuid4()),
                record_version=1,
                kind="belief",
                body=self.belief_body(),
                status="proposed",
                schema_version="1",
            )

        with self.assertRaises(InvalidWorkCompletion):
            self._command(context, work, apply)
        self.assertIsNone(self.store.work_progress(work.work_id))

    def test_committed_requires_an_appended_version(self) -> None:
        context, work = self._wait_context()

        def apply(scope):
            scope.complete_work(
                work_id=work.work_id,
                context_id=context.context_id,
                outcome="committed",
            )

        with self.assertRaises(InvalidWorkCompletion):
            self._command(context, work, apply)
        self.assertIsNone(self.store.work_progress(work.work_id))

    def test_checked_no_belief_rejects_an_appended_version(self) -> None:
        context, work = self._wait_context()

        def apply(scope):
            scope.append_record_version(
                record_id=str(uuid.uuid4()),
                record_version=1,
                kind="belief",
                body=self.belief_body(),
                status="proposed",
                schema_version="1",
            )
            scope.complete_work(
                work_id=work.work_id,
                context_id=context.context_id,
                outcome="checked_no_belief",
            )

        with self.assertRaises(InvalidWorkCompletion):
            self._command(context, work, apply)
        self.assertIsNone(self.store.work_progress(work.work_id))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
