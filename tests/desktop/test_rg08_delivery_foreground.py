"""RG08 DELIVERY: foreground starts its model attempt while maintenance drains.

This is an in-process desktop bridge (the real ``_RuntimeWorker`` queue, the real
runtime and the real scripted chat model), not a second scheduler.  The
maintenance transport is made slow by wrapping only its ``_send_blocking``;
the cancellation still travels through the reader-thread Event.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for _path in (REPO_ROOT / "src", REPO_ROOT / "apps" / "desktop" / "sidecar"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tests.desktop import support  # noqa: E402

from host_config import load_config  # noqa: E402
from pcs_bridge import Bridge  # noqa: E402
from quintessence_pcs.domains.memory.experience_bridge import (  # noqa: E402
    BridgeConfig,
    ExperienceBridge,
)
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

NS = support.NAMESPACE
HOST = support.HOST_INSTANCE
ACCESS_REF = support.ACCESS_REF


class _StubAdapter:
    def capture(self, event, *, actor_origin, timeout_seconds):
        del actor_origin, timeout_seconds
        return {
            "kind": "ok",
            "receipt": {
                "disposition": "inserted",
                "durability": "persisted",
                "event_refs": [
                    {
                        "ref": f"event:rg08-delivery:{event['source_event_key'][:40]}",
                        "revision": 1,
                    }
                ],
            },
        }


def _experience_access() -> ExperienceAccess:
    return ExperienceAccess(
        namespace=NS,
        host_instance=HOST,
        access_ref=ACCESS_REF,
        privacy_classes=frozenset({"private"}),
        purposes=frozenset({"consult", "memory.bridge", "review"}),
    )


class ForegroundLatencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rg08-delivery-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.db_path = root / "host.sqlite"
        document = support.config_document(
            self.db_path,
            purposes=["consult", "reasoning", "review", "memory.bridge"],
            script=[
                {"action": "none", "reply": "first synthetic answer"},
                {"action": "none", "reply": "second synthetic answer"},
                {"action": "none", "reply": "third synthetic answer"},
            ],
            maintenance={"enabled": True, "permissions": ["maintenance"]},
            review_script=[{"operation": "wait", "reason": "synthetic"}],
            query_plan={
                "kind": "belief_current",
                "claim_types": ["capability", "general_hypothesis"],
                "scope": {"scene": "lab.synthetic"},
                "limit": 5,
            },
        )
        document["cognition"] = {"enabled": True, "propose": True}
        document["budget"]["pcs_context_max_bytes"] = 65536
        config_path = support.write_config(root, document)
        self.bridge = Bridge(str(config_path), None, None)
        self.addCleanup(self._close)
        self.assertEqual(self.bridge.runtime is not None, True)
        self.maintenance_started = threading.Event()
        self.maintenance_release = threading.Event()
        self.foreground_started = threading.Event()
        self.foreground_done = threading.Event()
        self.turn_id = None
        self.attempt_before = 0

    def _close(self) -> None:
        self.maintenance_release.set()
        self.foreground_done.wait(3.0)
        try:
            self.bridge.worker.stop()
        finally:
            self.bridge._release_lock()

    def _count_attempts(self) -> int:
        connection = sqlite3.connect(str(self.db_path))
        try:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM model_attempts"
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def _run_on_worker(self, fn, timeout: float = 10.0):
        box: dict[str, object] = {}
        done = threading.Event()

        def job() -> None:
            try:
                box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                box["error"] = exc
            finally:
                done.set()

        self.bridge.worker.submit(job)
        self.assertTrue(done.wait(timeout), "worker job did not finish")
        if "error" in box:
            raise box["error"]  # type: ignore[misc]
        return box.get("value")

    def _setup_turn(self) -> None:
        runtime = self.bridge.runtime
        assert runtime is not None
        runtime.experience_bridge = ExperienceBridge(
            runtime.store,
            _StubAdapter(),
            BridgeConfig(scope_id="scope-rg08-delivery", max_batch=8),
        )
        conversation = runtime.conversation_create()
        conversation, created = runtime.conversation_send(
            conversation_id=conversation.conversation_id,
            client_request_id="rg08-delivery-turn-1",
            text="the synthetic shelf holds one blue box",
        )
        execution = runtime.conversation_execute(
            conversation=conversation, creation=created
        )
        self.assertEqual(execution.turn.state, "completed")
        runtime.experience_bridge.sync(
            _experience_access(), namespace=NS, limit=10
        )
        delivery = runtime.experience_bridge.deliver(
            _experience_access(),
            namespace=NS,
            limit=10,
            capture_timeout_seconds=2.0,
        )
        self.assertGreaterEqual(delivery.applied, 1, delivery)
        self.turn_id = created.turn.turn_id

    def _install_slow_maintenance(self) -> None:
        runtime = self.bridge.runtime
        assert runtime is not None
        original = runtime.maintenance_model._send_blocking

        def slow(body_text: str, *, timeout: float, api_key: str) -> str:
            self.maintenance_started.set()
            self.maintenance_release.wait(30.0)
            return original(body_text, timeout=timeout, api_key=api_key)

        runtime.maintenance_model._send_blocking = slow  # type: ignore[method-assign]
        chat_original = runtime.model._send_blocking

        def chat_probe(body_text: str, *, timeout: float, api_key: str) -> str:
            self.foreground_started.set()
            return chat_original(body_text, timeout=timeout, api_key=api_key)

        runtime.model._send_blocking = chat_probe  # type: ignore[method-assign]

    def test_foreground_model_starts_while_cancelled_transport_drains(self) -> None:
        self._run_on_worker(self._setup_turn)
        self.attempt_before = self._count_attempts()
        self._run_on_worker(self._install_slow_maintenance)
        with self.bridge._maintenance_lock:
            self.bridge._maintenance_turns = [self.turn_id]
        self.bridge.worker.submit(self.bridge._run_maintenance_review_job)
        self.assertTrue(self.maintenance_started.wait(5.0))
        cancelled_at = time.monotonic()
        self.assertTrue(
            self.bridge._request_maintenance_yield()
        )

        def foreground() -> object:
            try:
                runtime = self.bridge.runtime
                assert runtime is not None
                conversation = runtime.conversation_create()
                conversation, created = runtime.conversation_send(
                    conversation_id=conversation.conversation_id,
                    client_request_id="rg08-delivery-turn-2",
                    text="a second synthetic turn",
                )
                execution = runtime.conversation_execute(
                    conversation=conversation, creation=created
                )
                return execution.turn.state
            finally:
                self.foreground_done.set()

        self.bridge.worker.submit(lambda: foreground())
        self.assertTrue(
            self.foreground_started.wait(3.0),
            "foreground never reached its model call",
        )
        latency = time.monotonic() - cancelled_at
        print(f"foreground-to-model latency: {latency:.3f}s", file=sys.stderr)
        self.assertLess(latency, 1.0, latency)
        self.assertTrue(self.bridge.runtime.maintenance_model.in_flight)
        # The foreground attempt is a real ledger row, not only a queued job.
        attempts = self._count_attempts()
        self.assertGreater(attempts, self.attempt_before)
        self.maintenance_release.set()
        deadline = time.monotonic() + 3.0
        while (
            self.bridge.runtime.maintenance_model.in_flight
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self.assertFalse(self.bridge.runtime.maintenance_model.in_flight)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
