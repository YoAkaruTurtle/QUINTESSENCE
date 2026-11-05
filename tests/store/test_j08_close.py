"""J08 close-out: a slice must have a guard, and a rate limit must not fail.

Two ways this feature could lie, and what is pinned here:

* a maintenance slice that cannot re-check its targets must not run at all.
  Forgetting the guard is not a licence to fall back to the unchecked
  behaviour: the job pauses with a readable reason and nothing else moves.
* an external rate limit is a *retryable wait*, not a failure.  The attempt is
  recorded as ``waiting_dependency`` with a growing backoff stamp, so a retry
  loop cannot spin and the work is not retired.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _path in (_ROOT / "src", _ROOT / "tests" / "store"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.foundation.errors import InvalidInput  # noqa: E402
from quintessence_pcs.store import ControlStore  # noqa: E402
from quintessence_pcs.store.maintenance import (  # noqa: E402
    MaintenanceStore,
    RateLimited,
    SliceLimits,
    run_slice,
)

NAMESPACE = "example.test.j08close"
NOW = "2026-10-27T00:00:00Z"
LATER = "2026-10-27T00:05:00Z"
MUCH_LATER = "2026-10-27T01:00:00Z"


def _pass_guard(targets):
    return None


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pcs-j08close-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = ControlStore.open(self.root / "pcs.sqlite")
        self.addCleanup(self.store.close)
        self.jobs = MaintenanceStore(
            self.store, clock=lambda: NOW, backoff_seconds=(30, 60, 120, 300)
        )

    def register(self):
        return self.jobs.register(
            namespace=NAMESPACE,
            task_type="memory_organize",
            trigger_key="trigger-close",
            params={"scope": "s1"},
            config_version="j08-close-v1",
            input_watermark="42",
            target_watermark="50",
        )

    def worker(self, *, items=None, complete=True):
        def run(budget):
            return {
                "scanned": 1,
                "processed": len(items or ()),
                "units": 10,
                "complete": complete,
                "items": list(items or ()),
                "next_scan_position": {"cursor": 1},
            }

        return run


class GuardEnforcementTests(_Base):
    """AC-21: no guard, no slice."""

    def test_missing_guard_refuses_before_any_side_effect(self) -> None:
        job = self.register()

        with self.assertRaises(InvalidInput) as caught:
            run_slice(
                self.jobs,
                namespace=NAMESPACE,
                task_id=job.task_id,
                worker=self.worker(items=[]),
                now=NOW,
            )

        self.assertIn("guard_missing", str(caught.exception))
        after = self.jobs.get(NAMESPACE, job.task_id)
        self.assertEqual(after.state, "paused")
        self.assertEqual(after.last_error, "guard_missing")
        # Nothing ran: no attempt, no item, no checkpoint.
        self.assertEqual(after.attempt, 0)
        self.assertEqual(self.jobs.attempts(NAMESPACE, job.task_id), ())
        self.assertEqual(self.jobs.items(NAMESPACE, job.task_id), ())
        self.assertEqual(after.success_checkpoint, 0)

    def test_non_callable_guard_is_refused(self) -> None:
        job = self.register()

        with self.assertRaises(InvalidInput) as caught:
            run_slice(
                self.jobs,
                namespace=NAMESPACE,
                task_id=job.task_id,
                worker=self.worker(items=[]),
                now=NOW,
                guard="not-a-callable",
            )

        self.assertIn("guard_unavailable", str(caught.exception))
        after = self.jobs.get(NAMESPACE, job.task_id)
        self.assertEqual(after.state, "paused")
        self.assertEqual(after.attempt, 0)

    def test_a_guard_that_blows_up_stops_the_slice(self) -> None:
        job = self.register()

        def broken(targets):
            raise RuntimeError("guard backend down")

        report = run_slice(
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            worker=self.worker(items=[{"target_kind": "record", "target_id": "rec-1"}]),
            now=NOW,
            guard=broken,
        )

        self.assertEqual(report.stop_reason, "guard_unavailable")
        self.assertEqual(report.state, "paused")
        self.assertNotEqual(report.state, "succeeded")
        # A guard fault is not evidence about the target, so nothing is
        # recorded as settled and the checkpoint does not move.
        self.assertEqual(self.jobs.items(NAMESPACE, job.task_id), ())
        self.assertEqual(
            self.jobs.get(NAMESPACE, job.task_id).success_checkpoint, 0
        )

    def test_a_passing_guard_lets_the_slice_progress(self) -> None:
        job = self.register()
        report = run_slice(
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            worker=self.worker(
                items=[
                    {
                        "target_kind": "record",
                        "target_id": "rec-1",
                        "operation_key": "op-1",
                        "outcome": "committed",
                        "receipt_ref": "receipt-1",
                    }
                ]
            ),
            now=NOW,
            guard=_pass_guard,
        )

        self.assertEqual(report.state, "succeeded")
        self.assertEqual(self.jobs.get(NAMESPACE, job.task_id).success_checkpoint, 1)

    def test_a_committed_item_survives_the_target_going_stale(self) -> None:
        job = self.register()

        def stale(targets):
            raise InvalidInput("authorization_revoked")

        report = run_slice(
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            worker=self.worker(
                items=[
                    {
                        "target_kind": "record",
                        "target_id": "rec-1",
                        "operation_key": "op-1",
                        "outcome": "committed",
                        "receipt_ref": "receipt-1",
                    }
                ]
            ),
            now=NOW,
            guard=stale,
        )

        self.assertEqual(report.stop_reason, "stale_target")
        self.assertEqual(report.state, "paused")
        items = self.jobs.items(NAMESPACE, job.task_id)
        self.assertEqual(len(items), 1)
        # The commit happened; a later revocation cannot rewrite that fact.
        self.assertEqual(items[0].outcome, "committed")
        self.assertTrue((items[0].detail or "").startswith("stale_after_commit:"))
        # ...but it does not make the job look finished.
        self.assertNotEqual(report.state, "succeeded")


class RateLimitTests(_Base):
    """AC-25 (rate limit): a wait, with backoff, not a failure."""

    def test_rate_limit_waits_instead_of_failing(self) -> None:
        job = self.register()

        def limited(budget):
            raise RateLimited("upstream 429", retry_after=30)

        report = run_slice(
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            worker=limited,
            now=NOW,
            guard=_pass_guard,
        )

        self.assertEqual(report.stop_reason, "rate_limited")
        self.assertEqual(report.state, "waiting_dependency")
        after = self.jobs.get(NAMESPACE, job.task_id)
        self.assertEqual(after.state, "waiting_dependency")
        self.assertIsNotNone(after.next_retry_at)
        # Not a failure: the job is still leasable and still has its work.
        self.assertNotEqual(after.state, "failed")
        self.assertEqual(self.jobs.items(NAMESPACE, job.task_id), ())
        self.assertEqual(after.success_checkpoint, 0)

    def test_the_backoff_stamp_grows_between_attempts(self) -> None:
        job = self.register()

        def limited(budget):
            raise RateLimited("upstream 429")

        first = run_slice(
            self.jobs, namespace=NAMESPACE, task_id=job.task_id,
            worker=limited, now=NOW, guard=_pass_guard,
        )
        second = run_slice(
            self.jobs, namespace=NAMESPACE, task_id=job.task_id,
            worker=limited, now=NOW, guard=_pass_guard,
        )

        self.assertEqual(first.stop_reason, "rate_limited")
        self.assertEqual(second.stop_reason, "rate_limited")
        self.assertGreater(
            second.job.next_retry_at,
            first.job.next_retry_at,
            "a retry loop must not spin: the second wait is further out",
        )
        self.assertEqual(second.job.attempt, 2)

    def test_recovery_after_a_rate_limit_processes_the_work(self) -> None:
        job = self.register()

        def limited(budget):
            raise RateLimited("upstream 429")

        run_slice(
            self.jobs, namespace=NAMESPACE, task_id=job.task_id,
            worker=limited, now=NOW, guard=_pass_guard,
        )
        recovered = run_slice(
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            worker=self.worker(
                items=[
                    {
                        "target_kind": "record",
                        "target_id": "rec-1",
                        "operation_key": "op-1",
                        "outcome": "committed",
                        "receipt_ref": "receipt-1",
                    }
                ]
            ),
            now=LATER,
            guard=_pass_guard,
        )

        self.assertEqual(recovered.state, "succeeded")
        self.assertEqual(
            self.jobs.get(NAMESPACE, job.task_id).success_checkpoint, 1
        )
        self.assertEqual(len(self.jobs.items(NAMESPACE, job.task_id)), 1)

    def test_a_rate_limited_slice_does_not_block_the_task_budget(self) -> None:
        job = self.register()

        def limited(budget):
            raise RateLimited("upstream 429")

        for _ in range(2):
            run_slice(
                self.jobs, namespace=NAMESPACE, task_id=job.task_id,
                worker=limited, now=NOW, guard=_pass_guard,
            )

        after = self.jobs.get(NAMESPACE, job.task_id)
        self.assertEqual(after.units_spent, 0)
        # The slice cap still applies: a rate limit is not a way to run forever.
        with self.assertRaises(InvalidInput):
            for _ in range(12):
                run_slice(
                    self.jobs, namespace=NAMESPACE, task_id=job.task_id,
                    worker=limited, now=MUCH_LATER,
                    guard=_pass_guard, limits=SliceLimits(max_slices=3),
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
