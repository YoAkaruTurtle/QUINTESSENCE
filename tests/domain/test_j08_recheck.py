"""J08 定向复查：AC-21 守卫接线、触发消费者、崩溃后恢复。

这组测试补齐三处"实现了但没有调用方"：

* 监督器在**记录每一项之前**重查目标（`run_slice(guard=...)`），撤销或删除
  之后不能再把该项写成成功；
* 触发判定有**真实消费者**（`register_trigger_tasks`），并且对同一页重复评估
  只登记一个任务；
* 已提交的回执不会被"目标后来失效"改写。

这些用真实 `MaintenanceStore` 与真实 SQLite，不起桩。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _path in (_ROOT / "src",):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.domains.cognition.maintenance_guard import (  # noqa: E402
    MaintenanceStale,
    require_live_targets,
)
from quintessence_pcs.domains.cognition.maintenance_triggers import (  # noqa: E402
    evaluate_triggers,
    register_trigger_tasks,
)
from quintessence_pcs.domains.memory.maintenance_delivery import (  # noqa: E402
    deliver_page,
)
from quintessence_pcs.store import CognitionStore  # noqa: E402
from quintessence_pcs.store.maintenance import (  # noqa: E402
    MaintenanceStore,
    SliceLimits,
    run_slice,
)

_MIGRATIONS = _ROOT / "src" / "quintessence_pcs" / "resources" / "migrations"
NS = "example.test.j08recheck"
NOW = "2026-10-27T12:00:00Z"

_MISSING = "00000000-0000-4000-8000-000000000001"


def _pass_guard(targets):
    """A guard that always accepts: only for tests of other slice behaviour."""

    return None


class _View:
    def __init__(self, record_id, *, version=1, review_after=None, paused=False):
        self.ref = {"record_id": record_id, "record_version": version}
        self.review_after = review_after
        self.last_evaluated_at = None
        self.paused = paused
        self.review_condition = None
        self.source_revision = None


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pcs-recheck-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.store = CognitionStore.open(root / "pcs.sqlite")
        self.addCleanup(self.store.close)
        self.jobs = MaintenanceStore(self.store, clock=lambda: NOW)

    def register(self, trigger_key="t-1", **kwargs):
        params = dict(
            namespace=NS,
            task_type="cognition.review",
            trigger_key=trigger_key,
            params={"seed": trigger_key},
            config_version="j08.v1",
        )
        params.update(kwargs)
        return self.jobs.register(**params)


class GuardWiringTests(_Base):
    """AC-21: the supervisor re-checks targets before recording them."""

    def test_revoked_authorization_is_recorded_as_stale_not_success(self):
        job = self.register()

        def worker(budget):
            return {
                "scanned": 1,
                "processed": 1,
                "units": 1,
                "complete": True,
                "items": [
                    {
                        "target_kind": "record",
                        "target_id": "rec-1",
                        "target_version": 1,
                        "operation_key": "op-1",
                        "outcome": "committed",
                    }
                ],
            }

        def guard(targets):
            raise MaintenanceStale("authorization_revoked")

        report = run_slice(
            self.jobs, namespace=NS, task_id=job.task_id, worker=worker,
            limits=SliceLimits(max_seconds=60.0), now=NOW, guard=guard,
        )

        self.assertEqual(report.stop_reason, "stale_target")
        self.assertEqual(report.state, "paused")
        self.assertNotEqual(report.state, "succeeded")
        items = self.jobs.items(NS, job.task_id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].outcome, "failed")
        self.assertIn("authorization_revoked", items[0].detail or "")
        # the checkpoint never advanced past the refused item
        self.assertEqual(self.jobs.get(NS, job.task_id).success_checkpoint, 0)

    def test_receipt_keeps_authoritative_outcome_after_target_went_stale(self):
        job = self.register(trigger_key="t-2")

        def worker(budget):
            return {
                "scanned": 1,
                "processed": 1,
                "units": 1,
                "complete": True,
                "items": [
                    {
                        "target_kind": "record",
                        "target_id": "rec-2",
                        "target_version": 1,
                        "operation_key": "op-2",
                        "outcome": "committed",
                        "receipt_ref": "receipt:abc",
                    }
                ],
            }

        def guard(targets):
            raise MaintenanceStale("target_gone")

        report = run_slice(
            self.jobs, namespace=NS, task_id=job.task_id, worker=worker,
            limits=SliceLimits(max_seconds=60.0), now=NOW, guard=guard,
        )

        # the commit really happened; a later stale target must not rewrite it
        items = self.jobs.items(NS, job.task_id)
        self.assertEqual(items[0].outcome, "committed")
        self.assertIn("stale_after_commit", items[0].detail or "")
        self.assertEqual(report.stop_reason, "stale_target")
        self.assertEqual(report.state, "paused")

    def test_live_targets_let_a_normal_slice_succeed(self):
        job = self.register(trigger_key="t-3")

        def worker(budget):
            return {
                "scanned": 1, "processed": 1, "units": 1, "complete": True,
                "items": [
                    {
                        "target_kind": "record", "target_id": "rec-3",
                        "target_version": 1, "operation_key": "op-3",
                        "outcome": "no_change",
                    }
                ],
            }

        report = run_slice(
            self.jobs, namespace=NS, task_id=job.task_id, worker=worker,
            limits=SliceLimits(max_seconds=60.0), now=NOW,
            guard=lambda targets: None,
        )
        self.assertEqual(report.state, "succeeded")
        self.assertEqual(report.stop_reason, "complete")

    def test_missing_record_is_stale_for_the_guard(self):
        with self.assertRaises(MaintenanceStale) as caught:
            require_live_targets(
                self.store, namespace=NS,
                targets=[{"kind": "record", "id": _MISSING, "version": 1}],
            )
        self.assertEqual(caught.exception.reason, "target_gone")


class TriggerConsumerTests(_Base):
    """WP3: a judged trigger becomes a registered task, exactly once."""

    def test_due_review_registers_a_task_and_the_slice_runs(self):
        view = _View("q-1", review_after="2026-10-01T00:00:00Z")
        candidates = evaluate_triggers(views=[view], now=NOW)
        self.assertEqual(len(candidates), 1)

        jobs = register_trigger_tasks(
            self.jobs, namespace=NS, candidates=candidates,
            config_version="j08.v1", input_watermark=NOW,
        )
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.trigger_key, "trigger:q-1:review_due")

        report = run_slice(
            self.jobs, namespace=NS, task_id=job.task_id,
            worker=lambda budget: {
                "scanned": 1, "processed": 1, "units": 1, "complete": True,
                "items": [
                    {
                        "target_kind": "record", "target_id": "q-1",
                        "target_version": 1, "operation_key": "op-q1",
                        "outcome": "no_change",
                    }
                ],
            },
            limits=SliceLimits(max_seconds=60.0), now=NOW,
            guard=_pass_guard,
        )
        self.assertEqual(report.state, "succeeded")

    def test_re_evaluating_the_same_page_does_not_open_a_second_task(self):
        view = _View("q-2", review_after="2026-10-01T00:00:00Z")
        first = register_trigger_tasks(
            self.jobs, namespace=NS,
            candidates=evaluate_triggers(views=[view], now=NOW),
            config_version="j08.v1",
        )
        second = register_trigger_tasks(
            self.jobs, namespace=NS,
            candidates=evaluate_triggers(views=[view], now=NOW),
            config_version="j08.v1",
        )
        self.assertEqual(first[0].task_id, second[0].task_id)

    def test_unjudgeable_prose_never_registers_a_task(self):
        view = _View("q-3")
        view.review_condition = "when it seems relevant"
        candidates = evaluate_triggers(views=[view], now=NOW, include_unjudgeable=True)
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0].judgeable)
        self.assertEqual(
            register_trigger_tasks(
                self.jobs, namespace=NS, candidates=candidates,
                config_version="j08.v1",
            ),
            (),
        )

    def test_paused_subject_is_still_never_woken(self):
        view = _View("q-4", review_after="2026-10-01T00:00:00Z", paused=True)
        self.assertEqual(evaluate_triggers(views=[view], now=NOW), ())


class DeliveryWiringTests(_Base):
    """WP2: the delivery module against the real store.

    Without an adapter projection there is nothing to deliver, and the module
    says so instead of pretending: the outcome is ``waiting_dependency`` and
    nothing is marked applied.  This is the production ceiling today.
    """

    def _ingest(self, source_event_id: str = "ev-recheck-1"):
        return self.store.ingest_event(
            namespace=NS,
            source_kind="host_observation",
            source_instance="recheck-host",
            source_event_id=source_event_id,
            event_type="RecheckObserved",
            payload={"text": "synthetic observation"},
            observed_at=NOW,
            recorded_at=NOW,
        )

    def test_without_a_projection_delivery_waits_instead_of_inventing_one(self):
        receipt = self._ingest()
        outcomes = deliver_page(
            self.store,
            namespace=NS,
            event_ids=[receipt.event_id],
            scope={},
            authorization_ref="auth:recheck",
        )
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].outcome, "waiting_dependency")
        self.assertEqual(outcomes[0].detail, "no_adapter")

    def test_a_deleted_source_is_refused_at_the_delivery_boundary(self):
        receipt = self._ingest("ev-recheck-2")
        self.store.request_deletion(
            target_kind="event",
            namespace=NS,
            target_id=receipt.event_id,
            authorization_ref="auth:user-deleted",
        )
        outcomes = deliver_page(
            self.store,
            namespace=NS,
            event_ids=[receipt.event_id],
            scope={},
            authorization_ref="auth:recheck",
        )
        self.assertEqual(outcomes[0].outcome, "failed")
        self.assertEqual(outcomes[0].detail, "source_gone")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
