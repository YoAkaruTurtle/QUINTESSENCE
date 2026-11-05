"""0016 + WP1: bounded maintenance jobs, their checkpoint and recovery.

Three claims are pinned here, because each of them is a way the feature could
quietly lie:

* the checkpoint is derived from confirmed items, so it can never leap over an
  unfinished one (scan position is a different column and proves nothing);
* a job cannot become ``succeeded`` while any item is outstanding, so producing
  a candidate or a review decision cannot be reported as a finished task;
* recovery reads the authoritative receipt first and never re-proposes under a
  new request id.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _path in (_ROOT / "src", _ROOT / "tests" / "store"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.store import ControlStore  # noqa: E402
from quintessence_pcs.store.maintenance import (  # noqa: E402
    MaintenanceConflict,
    MaintenanceStore,
    reconcile_operation,
)

_MIGRATIONS = _ROOT / "src" / "quintessence_pcs" / "resources" / "migrations"
NAMESPACE = "example.test.maintenance"
NOW = "2026-10-27T00:00:00Z"
LATER = "2026-10-27T00:10:00Z"
MUCH_LATER = "2026-10-27T01:00:00Z"


def _copy_upto(limit: int, target: Path) -> Path:
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        if int(path.name[:4]) <= limit:
            shutil.copy2(path, target / path.name)
    return target


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

    def token(self, job):
        """The live lease token: writes from a stale attempt are refused."""
        return self.jobs.get(NAMESPACE, job.task_id).lease_token


class MigrationTests(_Base):
    def test_m16_01_empty_database_reaches_0016(self) -> None:
        rows = self.store.connection.execute(
            "SELECT migration_id, name FROM schema_migrations ORDER BY migration_id"
        ).fetchall()
        self.assertEqual(
            [(row[0], row[1]) for row in rows[-1:]], [(17, "conversation_turns")]
        )
        tables = {
            row[0]
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(
            {"maintenance_jobs", "maintenance_items", "maintenance_attempts"} <= tables
        )

    def test_m16_02_upgrade_from_0015_keeps_rows_and_passes_checks(self) -> None:
        old_dir = self.root / "m15"
        old_dir.mkdir()
        _copy_upto(15, old_dir)
        path = self.root / "upgrade.sqlite"
        old = ControlStore.open(path, migrations_dir=old_dir)
        old.connection.execute(
            "INSERT INTO namespace_state (namespace) VALUES (?)", (NAMESPACE,)
        )
        old.connection.commit()
        before = old.connection.execute(
            "SELECT COUNT(*) FROM namespace_state"
        ).fetchone()[0]
        old.close()

        upgraded = ControlStore.open(path)
        self.addCleanup(upgraded.close)
        self.assertEqual(
            upgraded.connection.execute(
                "SELECT MAX(migration_id) FROM schema_migrations"
            ).fetchone()[0],
            17,
        )
        self.assertEqual(
            upgraded.connection.execute(
                "SELECT COUNT(*) FROM namespace_state"
            ).fetchone()[0],
            before,
        )
        self.assertEqual(
            upgraded.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )
        self.assertEqual(
            upgraded.connection.execute("PRAGMA foreign_key_check").fetchall(), []
        )

    def test_m16_03_failing_0016_rolls_back_and_keeps_ledger_at_0015(self) -> None:
        old_dir = self.root / "m15b"
        old_dir.mkdir()
        _copy_upto(15, old_dir)
        path = self.root / "broken.sqlite"
        store = ControlStore.open(path, migrations_dir=old_dir)
        store.close()

        broken = self.root / "m16broken"
        shutil.copytree(old_dir, broken)
        (broken / "0016_maintenance_jobs.sql").write_text(
            "CREATE TABLE maintenance_jobs (bad;", encoding="utf-8"
        )
        with self.assertRaises(Exception):
            ControlStore.open(path, migrations_dir=broken)
        reopened = ControlStore.open(path, migrations_dir=old_dir)
        self.addCleanup(reopened.close)
        self.assertEqual(
            reopened.connection.execute(
                "SELECT MAX(migration_id) FROM schema_migrations"
            ).fetchone()[0],
            15,
        )
        self.assertEqual(
            reopened.connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'maintenance_jobs'"
            ).fetchone()[0],
            0,
        )


class RegistrationTests(_Base):
    def test_m16_10_same_trigger_and_params_is_idempotent(self) -> None:
        first = self._register()
        second = self._register()
        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM maintenance_jobs"
            ).fetchone()[0],
            1,
        )

    def test_m16_11_same_trigger_different_params_is_a_conflict(self) -> None:
        self._register()
        with self.assertRaises(MaintenanceConflict):
            self._register(params={"scope": "s2"})

    def test_m16_12_input_watermark_is_fixed_at_registration(self) -> None:
        job = self._register()
        self.assertEqual(job.input_watermark, "42")
        self.assertEqual(job.target_watermark, "50")
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.success_checkpoint, 0)


class LeaseTests(_Base):
    def test_m16_20_lease_opens_one_attempt_and_sets_token(self) -> None:
        job = self._register()
        leased, attempt = self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        self.assertEqual(leased.state, "leased")
        self.assertEqual(leased.attempt, 1)
        self.assertIsNotNone(leased.lease_token)
        self.assertEqual(attempt.attempt_no, 1)
        self.assertIsNone(attempt.finished_at)

    def test_m16_21_live_lease_cannot_be_taken_twice(self) -> None:
        job = self._register()
        self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        with self.assertRaises(Exception):
            self.jobs.lease(NAMESPACE, job.task_id, now=NOW)

    def test_m16_22_expired_lease_is_recoverable(self) -> None:
        job = self._register()
        self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        # The first worker crashed; its lease is long expired by MUCH_LATER.
        leased, attempt = self.jobs.lease(NAMESPACE, job.task_id, now=MUCH_LATER)
        self.assertEqual(leased.attempt, 2)
        self.assertEqual(attempt.attempt_no, 2)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM maintenance_attempts"
            ).fetchone()[0],
            2,
        )

    def test_m16_23_finished_job_cannot_be_leased(self) -> None:
        job = self._register()
        self.jobs.set_state(NAMESPACE, job.task_id, "cancelled", reason="host")
        with self.assertRaises(Exception):
            self.jobs.lease(NAMESPACE, job.task_id, now=NOW)


class CheckpointTests(_Base):
    def _job(self):
        job = self._register()
        self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        return job

    def token(self, job):
        """The live lease token: writes from a stale attempt are refused."""
        return self.jobs.get(NAMESPACE, job.task_id).lease_token

    def _item(self, job, no_key, outcome):
        return self.jobs.record_item(
            NAMESPACE,
            job.task_id,
            lease_token=self.token(job),
            target_kind="record",
            target_id=f"rec-{no_key}",
            operation_key=no_key,
            outcome=outcome,
        )

    def test_m16_30_checkpoint_stops_at_first_unconfirmed_item(self) -> None:
        job = self._job()
        self._item(job, "op-1", "committed")
        self._item(job, "op-2", "committed")
        self._item(job, "op-3", "awaiting_human")
        self._item(job, "op-4", "committed")
        updated = self.jobs.confirm_progress(NAMESPACE, job.task_id, lease_token=self.token(job), now=NOW)
        self.assertEqual(updated.success_checkpoint, 2)

    def test_m16_31_scan_position_never_moves_the_checkpoint(self) -> None:
        job = self._job()
        self._item(job, "op-1", "committed")
        moved = self.jobs.set_scan_position(
            NAMESPACE, job.task_id, {"after": "rec-9"},
            lease_token=self.token(job), now=NOW
        )
        self.assertEqual(moved.success_checkpoint, 0)
        self.assertIn("rec-9", moved.scan_position or "")

    def test_m16_32_checkpoint_catches_up_after_the_item_settles(self) -> None:
        job = self._job()
        self._item(job, "op-1", "committed")
        self._item(job, "op-2", "awaiting_human")
        self.assertEqual(
            self.jobs.confirm_progress(NAMESPACE, job.task_id, lease_token=self.token(job), now=NOW).success_checkpoint,
            1,
        )
        self._item(job, "op-2", "committed")
        self.assertEqual(
            self.jobs.confirm_progress(NAMESPACE, job.task_id, lease_token=self.token(job), now=NOW).success_checkpoint,
            2,
        )

    def test_m16_33_repeated_operation_key_updates_in_place(self) -> None:
        job = self._job()
        first = self._item(job, "op-1", "pending")
        second = self._item(job, "op-1", "committed")
        self.assertEqual(first.item_no, second.item_no)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM maintenance_items"
            ).fetchone()[0],
            1,
        )


class SuccessGuardTests(_Base):
    def test_m16_40_job_cannot_succeed_with_outstanding_items(self) -> None:
        job = self._register()
        self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        self.jobs.record_item(
            NAMESPACE,
            job.task_id,
            lease_token=self.token(job),
            target_kind="record",
            target_id="rec-1",
            operation_key="op-1",
            outcome="awaiting_human",
        )
        with self.assertRaises(Exception):
            self.jobs.finish_attempt(
                NAMESPACE, job.task_id, lease_token=self.token(job), outcome="succeeded", next_state="succeeded", now=NOW
            )

    def test_m16_41_job_cannot_succeed_before_recording_any_item(self) -> None:
        job = self._register()
        self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        with self.assertRaises(Exception):
            self.jobs.finish_attempt(
                NAMESPACE, job.task_id, lease_token=self.token(job), outcome="succeeded", next_state="succeeded", now=NOW
            )

    def test_m16_42_job_succeeds_when_every_item_is_confirmed(self) -> None:
        job = self._register()
        self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        for key in ("op-1", "op-2"):
            self.jobs.record_item(
                NAMESPACE,
                job.task_id,
                lease_token=self.token(job),
                target_kind="record",
                target_id=f"rec-{key}",
                operation_key=key,
                outcome="committed",
            )
        done = self.jobs.finish_attempt(
            NAMESPACE, job.task_id, lease_token=self.token(job), outcome="succeeded", next_state="succeeded", now=LATER
        )
        self.assertEqual(done.state, "succeeded")
        self.assertEqual(done.success_checkpoint, 2)
        self.assertIsNone(done.lease_token)

    def test_m16_43_pause_and_cancel_never_look_like_success(self) -> None:
        job = self._register()
        self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        paused = self.jobs.finish_attempt(
            NAMESPACE, job.task_id, lease_token=self.token(job), outcome="paused", next_state="paused", now=NOW
        )
        self.assertEqual(paused.state, "paused")
        self.jobs.resume(NAMESPACE, job.task_id, now=LATER)
        resumed, _attempt = self.jobs.lease(NAMESPACE, job.task_id, now=LATER)
        self.assertEqual(resumed.state, "leased")
        cancelled = self.jobs.finish_attempt(
            NAMESPACE, job.task_id, lease_token=self.token(job), outcome="cancelled", next_state="cancelled", now=LATER
        )
        self.assertEqual(cancelled.state, "cancelled")
        self.assertNotEqual(cancelled.state, "succeeded")


class AuditTests(_Base):
    def test_m16_50_attempts_are_append_only(self) -> None:
        job = self._register()
        self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        self.jobs.finish_attempt(
            NAMESPACE, job.task_id, lease_token=self.token(job), outcome="partial", next_state="paused", now=LATER
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("DELETE FROM maintenance_attempts")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE maintenance_attempts SET phase = 'rewritten'"
            )

    def test_m16_51_item_identity_is_immutable(self) -> None:
        job = self._register()
        self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        self.jobs.record_item(
            NAMESPACE,
            job.task_id,
            lease_token=self.token(job),
            target_kind="record",
            target_id="rec-1",
            operation_key="op-1",
            outcome="pending",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE maintenance_items SET target_id = 'rec-2'"
            )

    def test_m16_52_health_reports_bounded_state_counts(self) -> None:
        job = self._register()
        self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        health = self.jobs.health(NAMESPACE, now=NOW)
        self.assertEqual(health["by_state"], {"leased": 1})
        self.assertEqual(health["leased"], 1)
        self.assertIn("oldest_created_at", health)


class RecoveryTests(_Base):
    """WP4: recover the authoritative result, never re-propose blindly."""

    def _job(self):
        job = self._register()
        self.jobs.lease(NAMESPACE, job.task_id, now=NOW)
        return job

    def test_m16_60_absent_operation_is_reported_as_retryable(self) -> None:
        job = self._job()
        self.store.lookup_command_receipt = lambda **kwargs: None  # type: ignore[assignment]
        self.store.candidate_by_request = lambda **kwargs: None  # type: ignore[assignment]
        outcome = reconcile_operation(
            self.store,
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            operation_key="op-1",
            request_id="11111111-1111-4111-8111-111111111111",
            target_kind="record",
            target_id="rec-1",
            lease_token=self.token(job),
        )
        self.assertEqual(outcome.state, "absent")
        item = self.jobs.items(NAMESPACE, job.task_id)[0]
        self.assertEqual(item.outcome, "pending")

    def test_m16_61_registered_candidate_is_awaiting_not_committed(self) -> None:
        job = self._job()
        self.store.lookup_command_receipt = lambda **kwargs: None  # type: ignore[assignment]
        self.store.candidate_by_request = lambda **kwargs: {  # type: ignore[assignment]
            "artifact_id": "22222222-2222-4222-8222-222222222222",
            "artifact_version": 1,
        }
        outcome = reconcile_operation(
            self.store,
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            operation_key="op-1",
            request_id="11111111-1111-4111-8111-111111111111",
            target_kind="record",
            target_id="rec-1",
            lease_token=self.token(job),
        )
        self.assertEqual(outcome.state, "candidate_pending")
        item = self.jobs.items(NAMESPACE, job.task_id)[0]
        self.assertEqual(item.outcome, "awaiting_human")
        self.assertEqual(
            item.candidate_id, "22222222-2222-4222-8222-222222222222"
        )
        self.assertEqual(
            self.jobs.confirm_progress(NAMESPACE, job.task_id, lease_token=self.token(job), now=NOW).success_checkpoint,
            0,
        )

    def test_m16_62_committed_receipt_wins_over_candidate(self) -> None:
        job = self._job()

        class _Receipt:
            state_revision = 7

        self.store.lookup_command_receipt = lambda **kwargs: _Receipt()  # type: ignore[assignment]
        self.store.candidate_by_request = lambda **kwargs: {  # type: ignore[assignment]
            "artifact_id": "22222222-2222-4222-8222-222222222222",
            "artifact_version": 1,
        }
        outcome = reconcile_operation(
            self.store,
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            operation_key="op-1",
            request_id="11111111-1111-4111-8111-111111111111",
            target_kind="record",
            target_id="rec-1",
            lease_token=self.token(job),
        )
        self.assertEqual(outcome.state, "committed")
        item = self.jobs.items(NAMESPACE, job.task_id)[0]
        self.assertEqual(item.outcome, "committed")
        self.assertEqual(
            item.receipt_ref, "receipt:11111111-1111-4111-8111-111111111111"
        )
        self.assertEqual(
            self.jobs.confirm_progress(NAMESPACE, job.task_id, lease_token=self.token(job), now=NOW).success_checkpoint,
            1,
        )

    def test_m16_63_recovery_is_idempotent(self) -> None:
        job = self._job()

        class _Receipt:
            state_revision = 3

        self.store.lookup_command_receipt = lambda **kwargs: _Receipt()  # type: ignore[assignment]
        self.store.candidate_by_request = lambda **kwargs: None  # type: ignore[assignment]
        for _ in range(2):
            reconcile_operation(
                self.store,
                self.jobs,
                namespace=NAMESPACE,
                task_id=job.task_id,
                operation_key="op-1",
                request_id="11111111-1111-4111-8111-111111111111",
                target_kind="record",
                target_id="rec-1",
                lease_token=self.token(job),
            )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM maintenance_items"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class SliceTests(_Base):
    """The supervisor enforces the caps; the worker only does bounded work."""

    def _job(self):
        return self._register()

    def test_m16_70_slice_records_items_and_recomputes_checkpoint(self) -> None:
        from quintessence_pcs.store.maintenance import run_slice

        job = self._job()

        def worker(budget):
            self.assertEqual(budget["max_scan"], 200)
            return {
                "scanned": 3,
                "processed": 2,
                "units": 40,
                "items": [
                    {"target_kind": "record", "target_id": "rec-1", "outcome": "committed"},
                    {"target_kind": "record", "target_id": "rec-2", "outcome": "no_change"},
                ],
                "next_scan_position": {"after": "rec-2"},
                "complete": False,
                "stop_reason": "slice_end",
            }

        report = run_slice(
            self.jobs, namespace=NAMESPACE, task_id=job.task_id, worker=worker, now=NOW, guard=_pass_guard)
        self.assertEqual(report.job.success_checkpoint, 2)
        self.assertEqual(report.job.state, "paused")
        self.assertEqual(report.job.units_spent, 40)
        self.assertIn("rec-2", report.job.scan_position)

    def test_m16_70b_cancelled_slice_is_paused_without_items_or_checkpoint(self) -> None:
        from quintessence_pcs.store.maintenance import TaskCancelled, run_slice

        job = self._job()
        before = self.jobs.get(NAMESPACE, job.task_id)

        def worker(budget):
            del budget
            raise TaskCancelled("foreground_cancel")

        report = run_slice(
            self.jobs,
            namespace=NAMESPACE,
            task_id=job.task_id,
            worker=worker,
            now=NOW,
            guard=_pass_guard,
        )
        after = self.jobs.get(NAMESPACE, job.task_id)
        self.assertEqual(after.state, "paused")
        self.assertEqual(report.stop_reason, "foreground_cancel")
        self.assertEqual(report.processed, 0)
        self.assertEqual(after.success_checkpoint, before.success_checkpoint)
        self.assertEqual(after.scan_position, before.scan_position)
        attempts = self.jobs.attempts(NAMESPACE, job.task_id)
        self.assertEqual(attempts[-1].outcome, "cancelled")
        self.assertEqual(attempts[-1].stop_reason, "foreground_cancel")
        self.assertEqual(self.jobs.items(NAMESPACE, job.task_id), ())

    def test_m16_71_slice_caps_are_enforced_against_the_worker(self) -> None:
        from quintessence_pcs.store.maintenance import run_slice

        job = self._job()

        def worker(budget):
            return {"scanned": 10_000, "processed": 1, "units": 1, "items": []}

        with self.assertRaises(Exception):
            run_slice(
                self.jobs, namespace=NAMESPACE, task_id=job.task_id, worker=worker, now=NOW, guard=_pass_guard)
        failed = self.jobs.get(NAMESPACE, job.task_id)
        self.assertEqual(failed.state, "failed")

    def test_m16_72_units_accumulate_across_resumes(self) -> None:
        from quintessence_pcs.store.maintenance import SliceLimits, run_slice

        limits = SliceLimits(max_total_units=100)
        job = self._job()

        def worker(budget):
            self.assertLessEqual(budget["remaining_units"], 100)
            return {
                "scanned": 1,
                "processed": 1,
                "units": 60,
                "items": [
                    {"target_kind": "record", "target_id": "rec-1", "outcome": "committed"}
                ],
                "complete": False,
            }

        run_slice(
            self.jobs, namespace=NAMESPACE, task_id=job.task_id, worker=worker,
            limits=limits, now=NOW,
            guard=_pass_guard,
        )
        after_first = self.jobs.get(NAMESPACE, job.task_id)
        self.assertEqual(after_first.units_spent, 60)
        self.jobs.resume(NAMESPACE, job.task_id, now=LATER)
        second = run_slice(
            self.jobs, namespace=NAMESPACE, task_id=job.task_id, worker=worker,
            limits=limits, now=LATER,
            guard=_pass_guard,
        )
        self.assertEqual(second.job.units_spent, 120)
        self.jobs.resume(NAMESPACE, job.task_id, now=MUCH_LATER)
        with self.assertRaises(Exception):
            run_slice(
                self.jobs, namespace=NAMESPACE, task_id=job.task_id, worker=worker,
                limits=limits, now=MUCH_LATER,
            guard=_pass_guard,
        )
