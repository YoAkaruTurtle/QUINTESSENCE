"""RG08 DELIVERY: the maintenance transport stops waiting on cancellation.

The real urllib read cannot be interrupted reliably on this platform (a local
probe showed close/shutdown do not unblock it).  The adapter therefore runs one
bounded daemon helper and lets the worker stop waiting; this test pins the
helper lifecycle without a real provider.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for _path in (REPO_ROOT / "src", REPO_ROOT / "apps" / "desktop" / "sidecar"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.runtime import DirectHttpModel, ModelCancelled  # noqa: E402


def _model() -> DirectHttpModel:
    model = object.__new__(DirectHttpModel)
    model._transport_timeout_cap = None
    model._cancel_event = None
    model._inflight = threading.Event()
    model._inflight_lock = threading.Lock()
    return model


class TransportCancelTests(unittest.TestCase):
    def test_a_blocking_send_is_abandoned_without_waiting_for_the_helper(self) -> None:
        model = _model()
        started = threading.Event()
        release = threading.Event()

        def slow(body_text: str, *, timeout: float, api_key: str) -> str:
            del body_text, timeout, api_key
            started.set()
            release.wait(5.0)
            return "late"

        model._send_blocking = slow  # type: ignore[method-assign]
        event = threading.Event()
        model._cancel_event = event
        result: dict[str, object] = {}

        def run() -> None:
            try:
                result["value"] = model._send("body", timeout=10.0, api_key="k")
            except BaseException as exc:  # noqa: BLE001 - observed by the test
                result["error"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(2.0))
        cancelled_at = time.monotonic()
        event.set()
        thread.join(1.0)
        elapsed = time.monotonic() - cancelled_at
        self.assertFalse(thread.is_alive())
        self.assertLess(elapsed, 0.5)
        self.assertIsInstance(result.get("error"), ModelCancelled)
        self.assertTrue(model.in_flight)
        release.set()
        self._wait_idle(model)
        self.assertFalse(model.in_flight)

    def test_a_second_send_is_refused_while_the_abandoned_helper_drains(self) -> None:
        model = _model()
        started = threading.Event()
        release = threading.Event()
        calls = {"n": 0}

        def slow(body_text: str, *, timeout: float, api_key: str) -> str:
            del body_text, timeout, api_key
            calls["n"] += 1
            started.set()
            release.wait(5.0)
            return "late"

        model._send_blocking = slow  # type: ignore[method-assign]
        event = threading.Event()
        model._cancel_event = event

        def run() -> None:
            try:
                model._send("body", timeout=10.0, api_key="k")
            except BaseException:  # noqa: BLE001 - expected cancellation
                pass

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(2.0))
        event.set()
        thread.join(1.0)
        self.assertTrue(model.in_flight)
        model._cancel_event = threading.Event()
        with self.assertRaises(Exception):
            model._send("body", timeout=10.0, api_key="k")
        self.assertEqual(calls["n"], 1)
        release.set()
        self._wait_idle(model)

    def test_after_draining_the_next_send_works(self) -> None:
        model = _model()
        release = threading.Event()

        def slow(body_text: str, *, timeout: float, api_key: str) -> str:
            del body_text, timeout, api_key
            release.wait(5.0)
            return "first"

        model._send_blocking = slow  # type: ignore[method-assign]
        event = threading.Event()
        model._cancel_event = event
        result: dict[str, object] = {}

        def run() -> None:
            try:
                model._send("body", timeout=10.0, api_key="k")
            except BaseException as exc:  # noqa: BLE001 - expected cancellation
                result["error"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.2)
        event.set()
        thread.join(1.0)
        release.set()
        self._wait_idle(model)
        model._cancel_event = None
        model._send_blocking = (  # type: ignore[method-assign]
            lambda body_text, *, timeout, api_key: "second"
        )
        self.assertEqual(
            model._send("body", timeout=1.0, api_key="k"), "second"
        )

    def test_no_bound_event_keeps_the_plain_blocking_path(self) -> None:
        model = _model()
        model._send_blocking = (  # type: ignore[method-assign]
            lambda body_text, *, timeout, api_key: "plain"
        )
        self.assertEqual(model._send("body", timeout=1.0, api_key="k"), "plain")
        self.assertFalse(model.in_flight)

    @staticmethod
    def _wait_idle(model: DirectHttpModel) -> None:
        deadline = time.monotonic() + 2.0
        while model.in_flight and time.monotonic() < deadline:
            time.sleep(0.01)
        if model.in_flight:
            raise AssertionError("the abandoned helper did not drain")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
