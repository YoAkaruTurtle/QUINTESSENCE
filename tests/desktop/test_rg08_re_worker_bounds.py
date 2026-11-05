"""RG08 R-E: the worker yields at item boundaries and never overruns its page."""

from __future__ import annotations

import sys
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for _path in (REPO_ROOT / "src", REPO_ROOT / "apps" / "desktop" / "sidecar"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.runtime.maintenance_cognition import (  # noqa: E402
    MaintenanceCognitionWorker,
    _SliceDeadline,
)


def _item(index: int) -> dict:
    return {
        "target_kind": "event",
        "target_id": f"event-{index}",
        "operation_key": f"cognition:event-{index}",
        "outcome": "no_change",
        "detail": "synthetic",
    }


class WorkerBoundTests(unittest.TestCase):
    def _worker(self, *, yield_after: int) -> MaintenanceCognitionWorker:
        worker = object.__new__(MaintenanceCognitionWorker)
        worker._material = SimpleNamespace(primary_usable_sources=(object(), object()))
        worker._max_events = 2
        worker._clock = time.monotonic
        worker._lease_token = None
        seen = {"n": 0}

        def process(index: int, *, deadline: float | None = None):
            del deadline
            seen["n"] += 1
            return _item(index), 0

        def should_yield() -> bool:
            return seen["n"] >= yield_after

        worker._process = process
        worker._should_yield = should_yield
        return worker

    def test_one_foreground_yield_after_the_first_item_keeps_the_rest(self) -> None:
        worker = self._worker(yield_after=1)
        result = worker({"max_process": 2, "deadline_seconds": 10.0})
        self.assertEqual(result["processed"], 1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["stop_reason"], "foreground_yield")
        self.assertEqual(result["remaining"]["available"], 2)

    def test_no_yield_processes_the_whole_page(self) -> None:
        worker = self._worker(yield_after=99)
        result = worker({"max_process": 2, "deadline_seconds": 10.0})
        self.assertEqual(result["processed"], 2)
        self.assertTrue(result["complete"])

    def test_deadline_stops_before_the_second_item(self) -> None:
        now = {"t": 0.0}
        worker = self._worker(yield_after=99)
        worker._clock = lambda: now["t"]

        def slow_item(index: int, *, deadline: float | None = None):
            del deadline
            now["t"] += 20.0
            return _item(index), 1

        worker._process = slow_item
        result = worker({"max_process": 2, "deadline_seconds": 10.0})
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["stop_reason"], "slice_deadline_exceeded")
        self.assertFalse(result["complete"])

    def test_deadline_stops_before_a_format_repair(self) -> None:
        now = {"t": 0.0}

        class SlowModel:
            adapter_kind = "direct-http"

            def bind_run(self, *, namespace: str, context: Any):
                del namespace, context

                @contextmanager
                def scope():
                    yield

                return scope()

            def propose(self, *, rendered_input: str, references: Any, feedback: Any):
                del rendered_input, references, feedback
                now["t"] += 8.0
                return "not strict json"

        worker = object.__new__(MaintenanceCognitionWorker)
        worker._model = SlowModel()
        worker._namespace = "example.test.rg08"
        worker._model_call_cap = 7.0
        worker._model_call_reserve = 1.0
        worker._clock = lambda: now["t"]
        with self.assertRaises(_SliceDeadline) as raised:
            worker._generate(
                SimpleNamespace(rendered_input="synthetic"),
                deadline=10.0,
            )
        self.assertEqual(raised.exception.calls, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
