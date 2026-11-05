"""J08 stage 3: slice deadlines and lease ownership on maintenance jobs.

Two properties are load-bearing here:

* a slice that runs past its wall-clock budget stops as ``partial`` with
  ``slice_deadline_exceeded`` -- a deadline is never a success;
* only the attempt that currently owns the lease may write.  A worker whose
  lease expired and was taken over must not close or move the new attempt's
  row, because its late write is indistinguishable from a live worker's.
"""

from __future__ import annotations

import tempfile
import types
import unittest
import uuid
from pathlib import Path

from quintessence_pcs.store.control import ControlStore
from quintessence_pcs.store.maintenance import (
    MaintenanceConflict,
    MaintenanceStore,
    SliceLimits,
    reconcile_operation,
    run_slice,
)

NAMESPACE = "default"
CANDIDATE = "22222222-2222-4222-8222-222222222222"
DECISION = "33333333-3333-4333-8333-333333333333"
NOW = "2026-10-27T00:00:00Z"
LATER = "2026-10-27T01:00:00Z"


def _pass_guard(targets):
    """A guard that always accepts: only for tests of other slice behaviour."""

    return None


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pcs-16-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = ControlStore.open(self.root / "pcs.sqlite")
        self.addCleanup(self.store.close)
        self.jobs = MaintenanceStore(self.store, clock=lambda: NOW)

    def _register(self, **kwargs):
        params = dict(
            namespace=NAMESPACE,
            task_type="memory_organize",
            trigger_key="trigger-1",
            params={"scope": "s1"},
            config_version="j08-v1",
            input_watermark="42",
            target_watermark="50",
        )
        params.update(kwargs)
        return self.jobs.register(**params)

    def _leased(self):
        job = self._register()
        leased, _attempt = self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        return job, leased


class _Clock:
    """A monotonic fake clock that advances on every read."""

    def __init__(self, step: float = 0.0, start: float = 100.0) -> None:
        self._value = start
        self._step = step

    def __call__(self) -> float:
        value = self._value
        self._value += self._step
        return value


class DeadlineTests(_Base):
    def _worker(self, **payload):
        captured: dict = {}

        def worker(budget):
            captured.update(budget)
            return payload

        return worker, captured

    def test_budget_announces_the_deadline(self) -> None:
        job = self._register()
        worker, captured = self._worker(
            scanned=1,
            processed=1,
            complete=True,
            items=[
                {
                    "target_kind": "record",
                    "target_id": "rec-1",
                    "operation_key": "op-1",
                    "outcome": "committed",
                }
            ],
        )
        run_slice(
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            worker=worker,
            limits=SliceLimits(max_seconds=12.0),
            now=NOW,
            clock=_Clock(),
            guard=_pass_guard,
        )
        self.assertEqual(captured["max_seconds"], 12.0)
        self.assertEqual(captured["deadline_seconds"], 12.0)

    def test_slice_over_the_deadline_is_partial_not_success(self) -> None:
        job = self._register()
        # worker itself takes longer than the budget
        worker, _captured = self._worker(scanned=1, processed=1, complete=True)
        report = run_slice(
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            worker=worker,
            limits=SliceLimits(max_seconds=5.0),
            now=NOW,
            clock=_Clock(step=30.0),
            guard=_pass_guard,
        )
        self.assertTrue(report.deadline_exceeded)
        self.assertEqual(report.stop_reason, "slice_deadline_exceeded")
        self.assertEqual(report.state, "paused")
        self.assertNotEqual(report.state, "succeeded")
        # the attempt is closed as partial, never as succeeded
        attempt = self.jobs.attempts(NAMESPACE, job.task_id)[-1]
        self.assertEqual(attempt.outcome, "partial")
        self.assertEqual(attempt.stop_reason, "slice_deadline_exceeded")

    def test_items_after_the_deadline_are_not_recorded(self) -> None:
        job = self._register()
        items = [
            {
                "target_kind": "record",
                "target_id": f"rec-{index}",
                "operation_key": f"op-{index}",
                "outcome": "committed",
            }
            for index in range(4)
        ]
        worker, _captured = self._worker(
            scanned=4, processed=4, items=items, complete=True
        )
        report = run_slice(
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            worker=worker,
            limits=SliceLimits(max_seconds=50.0),
            now=NOW,
            clock=_Clock(step=20.0),
            guard=_pass_guard,
        )
        self.assertTrue(report.deadline_exceeded)
        recorded = self.jobs.items(NAMESPACE, job.task_id)
        self.assertLess(len(recorded), 4)
        self.assertEqual(report.remaining["items_not_recorded"], 4 - len(recorded))
        # the scan position is not advanced when the slice ran out of time
        self.assertIsNone(self.jobs.get(NAMESPACE, job.task_id).scan_position)

    def test_completed_slice_inside_the_budget_still_succeeds(self) -> None:
        job = self._register()
        worker, _captured = self._worker(
            scanned=1,
            processed=1,
            complete=True,
            items=[
                {
                    "target_kind": "record",
                    "target_id": "rec-1",
                    "operation_key": "op-1",
                    "outcome": "committed",
                }
            ],
        )
        report = run_slice(
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            worker=worker,
            limits=SliceLimits(max_seconds=60.0),
            now=NOW,
            clock=_Clock(step=1.0),
            guard=_pass_guard,
        )
        self.assertFalse(report.deadline_exceeded)
        self.assertEqual(report.state, "succeeded")


class OwnershipTests(_Base):
    def test_second_lease_while_live_is_refused(self) -> None:
        job, leased = self._leased()
        with self.assertRaises(Exception):
            self.jobs.lease(NAMESPACE, job.task_id, now=NOW)

    def test_expired_lease_can_be_taken_over(self) -> None:
        job, leased = self._leased()
        stale_token = leased.lease_token
        taken, _attempt = self.jobs.lease(NAMESPACE, job.task_id, now=LATER)
        self.assertNotEqual(taken.lease_token, stale_token)
        self.assertEqual(taken.attempt, 2)

    def test_stale_worker_cannot_close_the_new_attempt(self) -> None:
        job, leased = self._leased()
        stale_token = leased.lease_token
        taken, _attempt = self.jobs.lease(NAMESPACE, job.task_id, now=LATER)
        with self.assertRaises(MaintenanceConflict):
            self.jobs.finish_attempt(
                NAMESPACE,
                job.task_id,
                outcome="succeeded",
                next_state="succeeded",
                lease_token=stale_token,
                now=LATER,
            )
        # the live attempt is untouched and can still close normally
        live = self.jobs.get(NAMESPACE, job.task_id)
        self.assertEqual(live.state, "leased")
        self.assertEqual(live.lease_token, taken.lease_token)
        closed = self.jobs.finish_attempt(
            NAMESPACE,
            job.task_id,
            outcome="paused",
            next_state="paused",
            lease_token=taken.lease_token,
            now=LATER,
        )
        self.assertEqual(closed.state, "paused")

    def test_stale_worker_cannot_record_items(self) -> None:
        job, leased = self._leased()
        stale_token = leased.lease_token
        self.jobs.lease(NAMESPACE, job.task_id, now=LATER)
        with self.assertRaises(MaintenanceConflict):
            self.jobs.record_item(
                NAMESPACE,
                job.task_id,
                target_kind="record",
                target_id="rec-1",
                operation_key="op-1",
                outcome="committed",
                lease_token=stale_token,
            )
        with self.assertRaises(MaintenanceConflict):
            self.jobs.record_item(
                NAMESPACE,
                job.task_id,
                target_kind="record",
                target_id="rec-1",
                operation_key="op-1",
                outcome="committed",
                lease_token=None,
            )

    def test_stale_worker_cannot_move_the_scan_position(self) -> None:
        job, leased = self._leased()
        stale_token = leased.lease_token
        self.jobs.lease(NAMESPACE, job.task_id, now=LATER)
        with self.assertRaises(MaintenanceConflict):
            self.jobs.set_scan_position(
                NAMESPACE,
                job.task_id,
                {"after": "rec-9"},
                lease_token=stale_token,
                now=LATER,
            )


class DecisionRecoveryTests(_Base):
    """A review answered the round; the caller only lost the reply."""

    REQUEST = "11111111-1111-4111-8111-111111111111"

    def _stub_candidate(self) -> None:
        self.store.candidate_by_request = lambda **kwargs: {  # type: ignore[assignment]
            "artifact_id": CANDIDATE,
            "artifact_version": 1,
        }
        self.store.lookup_command_receipt = lambda **kwargs: None  # type: ignore[assignment]

    def test_recorded_decision_is_recovered_instead_of_reproposing(self) -> None:
        job, leased = self._leased()
        self._stub_candidate()
        decision = types.SimpleNamespace(
            decision="deferred", decision_id=DECISION, impact="E1"
        )
        source = types.SimpleNamespace(
            latest_decision=lambda ns, cid, ver: decision
        )
        recovery = reconcile_operation(
            self.store,
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            operation_key="op-1",
            request_id=self.REQUEST,
            target_kind="record",
            target_id="rec-1",
            lease_token=self.jobs.get(NAMESPACE, job.task_id).lease_token,
            decision_source=source,
        )
        self.assertEqual(recovery.state, "decision_recorded")
        self.assertIs(recovery.decision, decision)
        item = self.jobs.items(NAMESPACE, job.task_id)[0]
        self.assertEqual(item.outcome, "deferred")
        self.assertEqual(item.decision_id, DECISION)
        self.assertEqual(item.candidate_id, CANDIDATE)

    def test_committed_receipt_wins_over_a_decision(self) -> None:
        job, leased = self._leased()
        self._stub_candidate()
        self.store.lookup_command_receipt = lambda **kwargs: (  # type: ignore[assignment]
            types.SimpleNamespace(state_revision=7)
        )
        source = types.SimpleNamespace(
            latest_decision=lambda ns, cid, ver: types.SimpleNamespace(
                decision="deferred", decision_id=DECISION, impact="E1"
            )
        )
        recovery = reconcile_operation(
            self.store,
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            operation_key="op-1",
            request_id=self.REQUEST,
            target_kind="record",
            target_id="rec-1",
            lease_token=self.jobs.get(NAMESPACE, job.task_id).lease_token,
            decision_source=source,
        )
        self.assertEqual(recovery.state, "committed")
        self.assertEqual(
            self.jobs.items(NAMESPACE, job.task_id)[0].outcome, "committed"
        )

    def test_missing_decision_source_falls_back_to_candidate(self) -> None:
        job, leased = self._leased()
        self._stub_candidate()
        recovery = reconcile_operation(
            self.store,
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            operation_key="op-1",
            request_id=self.REQUEST,
            target_kind="record",
            target_id="rec-1",
            lease_token=self.jobs.get(NAMESPACE, job.task_id).lease_token,
        )
        self.assertEqual(recovery.state, "candidate_pending")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
