"""J09 WP1: the bounded maintenance entry really is reachable from the host.

J08 left one hole: ``run_slice`` had no production caller.  These tests drive the
real desktop runtime (and, once, the real sidecar process) instead of calling
``run_slice`` directly, so "the entry exists" means "the host can ask for a
slice", not "a function is importable".

Nothing here mocks the store, the lease, the guard or the delivery worker: the
only fake is the bridge adapter, which never runs because ``sync`` does not
deliver.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for _path in (REPO_ROOT / "src", REPO_ROOT / "apps" / "desktop" / "sidecar"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tests.desktop import support  # noqa: E402

from host_config import load_config  # noqa: E402
from quintessence_pcs.domains.memory.experience_bridge import (  # noqa: E402
    BridgeConfig,
    ExperienceBridge,
)
from quintessence_pcs.domains.cognition.maintenance_guard import (  # noqa: E402
    MaintenanceStale,
)
from quintessence_pcs.store import CognitionStore  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402
from quintessence_pcs.store.maintenance import MaintenanceStore  # noqa: E402

NS = support.NAMESPACE


class _StubAdapter:
    """The bridge only needs an adapter for ``deliver``; ``sync`` never calls it."""

    def capture(self, event, *, actor_origin, timeout_seconds):  # pragma: no cover
        raise AssertionError("sync must not reach the delivery adapter")


def _experience_access() -> ExperienceAccess:
    return ExperienceAccess(
        namespace=NS,
        host_instance=support.HOST_INSTANCE,
        access_ref=support.ACCESS_REF,
        privacy_classes=frozenset({"private"}),
        purposes=frozenset({"memory.bridge"}),
    )


class HostEntryTests(unittest.TestCase):
    """In-process runtime tests: fast, and the guard can be made to fail."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="j09-host-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.db_path = self.tmp_path / "host.sqlite"
        self.config_path = support.write_config(
            self.tmp_path,
            support.config_document(
                self.db_path,
                purposes=["consult", "reasoning", "memory.bridge"],
                maintenance={"enabled": True, "permissions": ["maintenance"]},
            ),
        )
        self.config = load_config(self.config_path)
        import runtime as runtime_module

        self.runtime_module = runtime_module
        self.runtime = runtime_module.DesktopRuntime(self.config)
        self.addCleanup(self.runtime.close)
        self.event_id = self._seed_projection()

    def _seed_projection(self) -> str:
        row = self.runtime.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="the synthetic shelf holds one blue box",
            privacy_class="private",
            retention="standard",
        )
        self.runtime.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=support.HOST_INSTANCE,
            access_ref=support.ACCESS_REF,
            policy_ref="policy:j04",
            input_refs=[
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": row.input_id,
                    "content_version": row.content_version,
                }
            ],
            result="answered",
        )
        bridge = ExperienceBridge(
            self.runtime.store,
            _StubAdapter(),
            BridgeConfig(scope_id="scope-j09", max_batch=8),
        )
        bridge.sync(_experience_access(), namespace=NS, limit=10)
        event_row = self.runtime.store.connection.execute(
            "SELECT event_id FROM experience_source_mapping WHERE namespace = ?",
            (NS,),
        ).fetchone()
        return str(event_row["event_id"])

    # ------------------------------------------------------------- permission

    def test_status_requires_the_maintenance_permission(self) -> None:
        path = support.write_config(
            self.tmp_path,
            support.config_document(self.db_path),
            name="no-maintenance.json",
        )
        config = load_config(path)
        runtime = self.runtime_module.DesktopRuntime(config)
        self.addCleanup(runtime.close)
        with self.assertRaises(Exception) as caught:
            runtime.maintenance_status()
        self.assertEqual(getattr(caught.exception, "code", None), "MAINTENANCE_FORBIDDEN")

    def test_run_requires_the_maintenance_permission(self) -> None:
        path = support.write_config(
            self.tmp_path,
            support.config_document(self.db_path),
            name="no-maintenance-run.json",
        )
        config = load_config(path)
        runtime = self.runtime_module.DesktopRuntime(config)
        self.addCleanup(runtime.close)
        with self.assertRaises(Exception) as caught:
            runtime.maintenance_run(event_ids=[self.event_id])
        self.assertEqual(getattr(caught.exception, "code", None), "MAINTENANCE_FORBIDDEN")

    # ------------------------------------------------------------------ slice

    def test_run_delivers_one_bounded_page_and_stops_at_applied(self) -> None:
        result = self.runtime.maintenance_run(event_ids=[self.event_id])

        self.assertEqual(result["processed"], 1)
        # No external ACK source exists, so the slice must stop and wait rather
        # than claim success.
        self.assertEqual(result["state"], "paused")
        self.assertEqual(result["stop_reason"], "awaits_confirmation")
        self.assertEqual(result["job"]["success_checkpoint"], 0)

        state = self.runtime.store.connection.execute(
            "SELECT state FROM memory_delivery WHERE namespace = ?", (NS,)
        ).fetchone()
        self.assertEqual(state["state"], "applied")

    def test_a_missing_guard_never_runs_and_records_no_item(self) -> None:
        """The entry always passes the real guard; when it fails, nothing lands."""

        def _refuse(store, *, namespace, targets):
            raise MaintenanceStale("source_deleted")

        original = self.runtime_module.require_live_targets
        self.runtime_module.require_live_targets = _refuse
        self.addCleanup(
            lambda: setattr(self.runtime_module, "require_live_targets", original)
        )

        result = self.runtime.maintenance_run(event_ids=[self.event_id])

        self.assertEqual(result["stop_reason"], "stale_target")
        self.assertEqual(result["state"], "paused")
        self.assertEqual(result["job"]["success_checkpoint"], 0)
        self.assertEqual(result["processed"], 0)
        # Nothing was delivered and no item exists: the boundary check ran
        # before the worker, so a stale page cannot leave a side effect behind.
        maintenance = self.runtime._maintenance_store()
        self.assertEqual(maintenance.items(NS, result["task_id"]), ())
        self.assertTrue(
            str(result["job"]["last_error"]).startswith("stale_target:")
        )
        applied = self.runtime.store.connection.execute(
            "SELECT COUNT(*) AS n FROM memory_delivery WHERE namespace = ? AND state = 'applied'",
            (NS,),
        ).fetchone()
        self.assertEqual(applied["n"], 0, "a refused slice must not deliver")

    # -------------------------------------------------------------- lifecycle

    def test_status_pause_resume_cancel_are_observable(self) -> None:
        result = self.runtime.maintenance_run(event_ids=[self.event_id])
        task_id = result["task_id"]

        status = self.runtime.maintenance_status(task_id=task_id)
        self.assertEqual(status["jobs"][0]["task_id"], task_id)
        self.assertEqual(status["jobs"][0]["state"], "paused")
        self.assertEqual(status["jobs"][0]["last_stop_reason"], "awaits_confirmation")
        self.assertEqual(status["health"]["by_state"], {"paused": 1})
        self.assertIn("oldest_created_at", status["health"])
        self.assertIn("pending", status["health"])

        resumed = self.runtime.maintenance_control(task_id=task_id, action="resume")
        self.assertEqual(resumed["job"]["state"], "pending")

        paused = self.runtime.maintenance_control(task_id=task_id, action="pause")
        self.assertEqual(paused["job"]["state"], "paused")

        cancelled = self.runtime.maintenance_control(task_id=task_id, action="cancel")
        self.assertEqual(cancelled["job"]["state"], "cancelled")

        status = self.runtime.maintenance_status(task_id=task_id)
        self.assertEqual(status["jobs"][0]["state"], "cancelled")

    def test_the_same_trigger_is_not_registered_twice(self) -> None:
        first = self.runtime.maintenance_run(event_ids=[])
        # The second call must find the *same* task rather than a new one; it
        # then refuses to lease a paused job, which is the honest outcome.
        with self.assertRaises(Exception) as caught:
            self.runtime.maintenance_run(event_ids=[])
        self.assertIn("cannot lease a job in state paused", str(caught.exception))
        jobs = self.runtime.maintenance_status()["health"]
        self.assertEqual(jobs["by_state"], {"paused": 1})

    def test_a_cancelled_job_cannot_be_run(self) -> None:
        first = self.runtime.maintenance_run(event_ids=[])
        self.runtime.maintenance_control(task_id=first["task_id"], action="cancel")
        with self.assertRaises(Exception) as caught:
            self.runtime.maintenance_run(
                event_ids=[], task_id=first["task_id"]
            )
        self.assertIn("cancelled", str(caught.exception))


class HostEntrySidecarTests(support.DesktopCase):
    """One end-to-end check over the real sidecar protocol."""

    def test_the_bridge_exposes_the_maintenance_entry(self) -> None:
        document = support.config_document(
            self.db_path,
            maintenance={"enabled": True, "permissions": ["maintenance"]},
        )
        process = self.start(document=document)
        process.wait_ready()
        reply = process.request("maintenance.status")
        self.assertNotIn("error", reply, reply)
        self.assertIn("health", reply["result"])
        self.assertEqual(reply["result"]["jobs"], [])

    def test_without_the_permission_the_bridge_refuses(self) -> None:
        process = self.start()
        process.wait_ready()
        reply = process.request("maintenance.status")
        self.assertIn("error", reply, reply)
        self.assertEqual(reply["error"]["code"], "MAINTENANCE_FORBIDDEN")

    def test_unknown_parameters_are_rejected(self) -> None:
        document = support.config_document(
            self.db_path,
            maintenance={"enabled": True, "permissions": ["maintenance"]},
        )
        process = self.start(document=document)
        process.wait_ready()
        reply = process.request("maintenance.run", {"scope": "nope"})
        self.assertIn("error", reply, reply)
        self.assertEqual(reply["error"]["code"], "INVALID_PARAMS")

    # ------------------------------------------------- paused / leased / terminal

    def _maintenance_document(self) -> dict:
        return support.config_document(
            self.db_path,
            maintenance={"enabled": True, "permissions": ["maintenance"]},
        )

    def _seed_leased_job(self) -> str:
        """Create the live-lease state the bridge cannot reach in one call."""

        store = CognitionStore.open(self.db_path)
        try:
            maintenance = MaintenanceStore(store)
            job = maintenance.register(
                namespace=support.NAMESPACE,
                task_type="memory_delivery",
                trigger_key="rg09:host-entry:leased",
                params={"event_ids": []},
                config_version="rg09.host-entry.v1",
            )
            maintenance.lease(support.NAMESPACE, job.task_id)
            return job.task_id
        finally:
            store.close()

    def test_a_paused_job_is_a_state_error_not_an_unavailable_sidecar(self) -> None:
        process = self.start(document=self._maintenance_document())
        first = process.request("maintenance.run", {})
        self.assertIn("result", first, first)
        self.assertEqual(first["result"]["state"], "paused")
        task_id = first["result"]["task_id"]

        second = process.request("maintenance.run", {})
        self.assertIn("error", second, second)
        self.assertEqual(second["error"]["code"], "MAINTENANCE_STATE_INVALID")
        self.assertNotEqual(second["error"]["code"], "SIDECAR_UNAVAILABLE")
        self.assertIn("paused", second["error"]["message"])

        status = process.request("maintenance.status", {"task_id": task_id})
        self.assertEqual(status["result"]["jobs"][0]["state"], "paused")

    def test_a_cancelled_job_is_a_state_error_not_an_unavailable_sidecar(self) -> None:
        process = self.start(document=self._maintenance_document())
        first = process.request("maintenance.run", {})
        task_id = first["result"]["task_id"]
        cancelled = process.request(
            "maintenance.control", {"task_id": task_id, "action": "cancel"}
        )
        self.assertEqual(cancelled["result"]["job"]["state"], "cancelled")

        reply = process.request("maintenance.run", {"task_id": task_id})
        self.assertIn("error", reply, reply)
        self.assertEqual(reply["error"]["code"], "MAINTENANCE_STATE_INVALID")
        self.assertNotEqual(reply["error"]["code"], "SIDECAR_UNAVAILABLE")
        self.assertIn("cancelled", reply["error"]["message"])

    def test_a_live_leased_job_is_a_state_error_not_an_unavailable_sidecar(self) -> None:
        task_id = self._seed_leased_job()
        process = self.start(document=self._maintenance_document())

        reply = process.request("maintenance.run", {"task_id": task_id})
        self.assertIn("error", reply, reply)
        self.assertEqual(reply["error"]["code"], "MAINTENANCE_STATE_INVALID")
        self.assertNotEqual(reply["error"]["code"], "SIDECAR_UNAVAILABLE")
        self.assertIn("leased", reply["error"]["message"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
