"""J08 AC-25: the faults that only show up across a process boundary.

The unit tests above prove the branches; these prove the *recovery contract*
when the process disappears between two durable writes and when an external ack
never arrives.

* A worker killed mid-slice leaves the job leased.  Nothing may be reported
  settled until a fresh lease takes over, and re-recording the same
  ``operation_key`` updates in place rather than duplicating an item.
* A delivery that was applied but never acked must not become ``verified`` by
  being replayed; replay is idempotent and the ack stays missing.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _path in (_ROOT / "src", _ROOT / "tests" / "store"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.store import ControlStore  # noqa: E402
from quintessence_pcs.foundation.errors import InvalidInput  # noqa: E402
from quintessence_pcs.store.maintenance import (  # noqa: E402
    MaintenanceConflict,
    MaintenanceStore,
    run_slice,
)

_MIGRATIONS = _ROOT / "src" / "quintessence_pcs" / "resources" / "migrations"
NAMESPACE = "example.test.j08crash"
NOW = "2026-10-27T00:00:00Z"
LATER = "2026-10-27T00:10:00Z"
MUCH_LATER = "2026-10-27T01:00:00Z"
SOON = "2026-10-27T00:00:30Z"


def _pass_guard(targets):
    """A guard that always accepts: only for tests of other slice behaviour."""

    return None


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="j08-crash-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "pcs.sqlite"
        self.store = ControlStore.open(self.db)
        self.addCleanup(self.store.close)
        self.jobs = MaintenanceStore(self.store, clock=lambda: NOW)

    def job(self, trigger_key="t-1", **kwargs):
        return self.jobs.register(
            namespace=NAMESPACE,
            task_type="memory_delivery",
            trigger_key=trigger_key,
            params={"page": 1},
            config_version="j08.test.v1",
            input_watermark="1",
            target_watermark="5",
            **kwargs,
        )


class CrashMidSliceTests(_Base):
    def test_killed_worker_leaves_the_job_leased_and_a_takeover_can_resume(self):
        job = self.job()
        leased, attempt = self.jobs.lease(NAMESPACE, job.task_id, now=NOW)

        # The worker records one item, then the process dies: no finish_attempt.
        self.jobs.record_item(
            NAMESPACE,
            job.task_id,
            target_kind="event",
            target_id="ev-1",
            target_version=1,
            operation_key="deliver:ev-1",
            outcome="deferred",
            detail="applied_awaiting_external_ack",
            lease_token=leased.lease_token,
            now=NOW,
        )

        # Nobody can steal the lease or close the attempt while it is alive.
        with self.assertRaises(InvalidInput):
            self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        with self.assertRaises(MaintenanceConflict):
            self.jobs.finish_attempt(
                NAMESPACE,
                job.task_id,
                outcome="succeeded",
                next_state="succeeded",
                stop_reason="done",
                lease_token="stale-token",
                now=NOW,
            )

        # After expiry a new owner takes over and re-records the same operation.
        taken, _ = self.jobs.lease(NAMESPACE, job.task_id, now=LATER)
        self.jobs.record_item(
            NAMESPACE,
            job.task_id,
            target_kind="event",
            target_id="ev-1",
            target_version=1,
            operation_key="deliver:ev-1",
            outcome="committed",
            detail="verified",
            lease_token=taken.lease_token,
            now=LATER,
        )
        items = self.jobs.items(NAMESPACE, job.task_id)
        self.assertEqual(len(items), 1, "the same operation key must not duplicate")
        self.assertEqual(items[0].outcome, "committed")

    def test_checkpoint_does_not_advance_past_an_unfinished_item(self):
        job = self.job()
        leased, _ = self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        for key, outcome in (("a", "committed"), ("b", "deferred"), ("c", "committed")):
            self.jobs.record_item(
                NAMESPACE,
                job.task_id,
                target_kind="event",
                target_id=f"ev-{key}",
                target_version=1,
                operation_key=f"deliver:{key}",
                outcome=outcome,
                lease_token=leased.lease_token,
                now=NOW,
            )
        self.jobs.finish_attempt(
            NAMESPACE,
            job.task_id,
            outcome="partial",
            next_state="paused",
            stop_reason="slice_end",
            lease_token=leased.lease_token,
            now=NOW,
        )
        job_after = self.jobs.get(NAMESPACE, job.task_id)
        self.assertEqual(job_after.success_checkpoint, 1)

        # Finishing the gap lets the checkpoint reach the third item.
        self.jobs.resume(NAMESPACE, job.task_id, now=LATER)
        taken, _ = self.jobs.lease(NAMESPACE, job.task_id, now=LATER)
        self.jobs.record_item(
            NAMESPACE,
            job.task_id,
            target_kind="event",
            target_id="ev-b",
            target_version=1,
            operation_key="deliver:b",
            outcome="committed",
            lease_token=taken.lease_token,
            now=LATER,
        )
        self.jobs.finish_attempt(
            NAMESPACE,
            job.task_id,
            outcome="succeeded",
            next_state="succeeded",
            stop_reason="done",
            lease_token=taken.lease_token,
            now=LATER,
        )
        self.assertEqual(self.jobs.get(NAMESPACE, job.task_id).success_checkpoint, 3)

    def test_a_slice_that_dies_mid_worker_is_not_reported_as_success(self):
        job = self.job()

        def worker(_budget):
            raise RuntimeError("killed")

        with self.assertRaises(RuntimeError):
            run_slice(self.jobs, namespace=NAMESPACE, task_id=job.task_id, worker=worker, guard=_pass_guard)
        after = self.jobs.get(NAMESPACE, job.task_id)
        self.assertNotEqual(after.state, "succeeded")
        self.assertEqual(after.state, "failed")


class RestartAfterCheckpointTests(_Base):
    def test_checkpoint_survives_a_restart_and_the_next_slice_continues(self):
        """AC-25: closing the process must not lose confirmed progress.

        A new store must see the same checkpoint and item set, and the next
        slice must resume *after* it instead of replaying the settled work.
        """

        job = self.job()

        def first(_budget):
            return {
                "scanned": 1,
                "processed": 1,
                "units": 1,
                "complete": False,
                "stop_reason": "page_limit",
                "items": [
                    {
                        "target_kind": "memory_delivery",
                        "target_id": "del-1",
                        "operation_key": "deliver:del-1",
                        "outcome": "no_change",
                    }
                ],
            }

        run_slice(self.jobs, namespace=NAMESPACE, task_id=job.task_id, worker=first, guard=_pass_guard)
        before = self.jobs.get(NAMESPACE, job.task_id)
        self.assertEqual(before.success_checkpoint, 1)
        self.store.close()

        # A brand-new connection to the same file: nothing may be re-derived
        # from memory of the previous process.
        reopened = ControlStore.open(self.db)
        self.addCleanup(reopened.close)
        jobs = MaintenanceStore(reopened, clock=lambda: LATER)
        after_restart = jobs.get(NAMESPACE, job.task_id)
        self.assertEqual(after_restart.success_checkpoint, 1)
        self.assertEqual(after_restart.state, before.state)
        self.assertEqual(len(jobs.items(NAMESPACE, job.task_id)), 1)

        seen: list[str] = []

        def second(_budget):
            seen.extend(
                item.operation_key for item in jobs.items(NAMESPACE, job.task_id)
            )
            return {
                "scanned": 1,
                "processed": 1,
                "units": 1,
                "complete": True,
                "items": [
                    {
                        "target_kind": "memory_delivery",
                        "target_id": "del-2",
                        "operation_key": "deliver:del-2",
                        "outcome": "no_change",
                    }
                ],
            }

        resumed_job = jobs.resume(NAMESPACE, job.task_id, now=LATER)
        self.assertEqual(resumed_job.state, "pending")
        resumed = run_slice(
            jobs, namespace=NAMESPACE, task_id=job.task_id, worker=second, now=LATER, guard=_pass_guard)
        self.assertEqual(resumed.state, "succeeded")
        self.assertEqual(jobs.get(NAMESPACE, job.task_id).success_checkpoint, 2)
        # The settled item was not recorded twice.
        self.assertEqual(seen, ["deliver:del-1"])
        self.assertEqual(len(jobs.items(NAMESPACE, job.task_id)), 2)


class LostAckTests(_Base):
    def test_applied_without_ack_cannot_be_promoted_by_replay(self):
        """A replayed delivery settles the same operation, not a second one."""

        job = self.job()
        leased, _ = self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        self.jobs.record_item(
            NAMESPACE,
            job.task_id,
            target_kind="memory_delivery",
            target_id="del-1",
            target_version=1,
            operation_key="delivery:del-1",
            outcome="deferred",
            detail="applied_awaiting_external_ack",
            lease_token=leased.lease_token,
            now=NOW,
        )
        self.jobs.finish_attempt(
            NAMESPACE,
            job.task_id,
            outcome="partial",
            next_state="paused",
            stop_reason="slice_end",
            lease_token=leased.lease_token,
            now=NOW,
        )

        self.jobs.resume(NAMESPACE, job.task_id, now=LATER)
        taken, _ = self.jobs.lease(NAMESPACE, job.task_id, now=LATER)
        self.jobs.record_item(
            NAMESPACE,
            job.task_id,
            target_kind="memory_delivery",
            target_id="del-1",
            target_version=1,
            operation_key="delivery:del-1",
            outcome="deferred",
            detail="applied_awaiting_external_ack",
            lease_token=taken.lease_token,
            now=LATER,
        )
        items = self.jobs.items(NAMESPACE, job.task_id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].outcome, "deferred")
        self.assertEqual(self.jobs.get(NAMESPACE, job.task_id).success_checkpoint, 0)


class RealProcessKillTests(_Base):
    """A2C: the worker process is really killed, not merely "abandoned".

    The earlier cases release the lease in-process, which proves the state
    machine but not that a dying process leaves recoverable evidence.  Here a
    child process leases the job, records one item and is then terminated while
    it still holds the lease -- exactly the shape of a power cut or a taskkill.
    """

    def _child_script(self) -> Path:
        script = self.dir / "killed_worker.py"
        script.write_text(
            "\n".join(
                [
                    "import sys, time, os",
                    f"sys.path.insert(0, r'{_ROOT / 'src'}')",
                    "from quintessence_pcs.store import ControlStore",
                    "from quintessence_pcs.store.maintenance import MaintenanceStore",
                    "path, ns, now = sys.argv[1], sys.argv[2], sys.argv[3]",
                    "store = ControlStore.open(path)",
                    "jobs = MaintenanceStore(store, clock=lambda: now)",
                    "job = jobs.register(",
                    "    namespace=ns, task_type='memory_delivery',",
                    "    trigger_key='killed', params={'page': 1},",
                    "    config_version='j08.test.v1',",
                    ")",
                    "leased, _ = jobs.lease(ns, job.task_id, now=now)",
                    "jobs.record_item(",
                    "    ns, job.task_id, target_kind='memory_delivery',",
                    "    target_id='del-1', target_version=1,",
                    "    operation_key='delivery:del-1', outcome='deferred',",
                    "    detail='applied_awaiting_external_ack',",
                    "    lease_token=leased.lease_token, now=now,",
                    ")",
                    "print('READY', flush=True)",
                    "time.sleep(120)",
                ]
            ),
            encoding="utf-8",
        )
        return script

    def test_a_terminated_process_leaves_a_recoverable_lease(self):
        child_db = self.dir / "killed.sqlite"
        script = self._child_script()
        process = subprocess.Popen(
            [sys.executable, str(script), str(child_db), NAMESPACE, NOW],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "READY")
            process.kill()
            returncode = process.wait(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
            process.stdout.close()
            process.stderr.close()

        self.assertNotEqual(returncode, 0)

        store = ControlStore.open(child_db)
        self.addCleanup(store.close)
        jobs = MaintenanceStore(store, clock=lambda: NOW)
        job = jobs.register(
            namespace=NAMESPACE,
            task_type="memory_delivery",
            trigger_key="killed",
            params={"page": 1},
            config_version="j08.test.v1",
        )
        # the lease is still held by the dead process
        self.assertEqual(job.state, "leased")
        self.assertEqual(job.attempt, 1)
        items = jobs.items(NAMESPACE, job.task_id)
        self.assertEqual(len(items), 1)
        self.assertEqual(job.success_checkpoint, 0)

        # a live lease cannot be stolen ...
        with self.assertRaises(InvalidInput):
            jobs.lease(NAMESPACE, job.task_id, now=SOON)
        # ... but an expired one is recovered, and the item is not duplicated
        taken, _ = jobs.lease(NAMESPACE, job.task_id, now=MUCH_LATER)
        self.assertEqual(taken.attempt, 2)
        jobs.record_item(
            NAMESPACE,
            job.task_id,
            target_kind="memory_delivery",
            target_id="del-1",
            target_version=1,
            operation_key="delivery:del-1",
            outcome="deferred",
            detail="applied_awaiting_external_ack",
            lease_token=taken.lease_token,
            now=MUCH_LATER,
        )
        self.assertEqual(len(jobs.items(NAMESPACE, job.task_id)), 1)
        self.assertEqual(jobs.get(NAMESPACE, job.task_id).success_checkpoint, 0)


if __name__ == "__main__":
    unittest.main()
