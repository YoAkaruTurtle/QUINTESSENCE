"""RG08 R-E: the bridge's maintenance job obeys the Q6 foreground gate."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for _path in (REPO_ROOT / "src", REPO_ROOT / "apps" / "desktop" / "sidecar"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from pcs_bridge import Bridge  # noqa: E402


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.resume_tasks: list[str | None] = []

    def maintenance_after_turn(
        self,
        turn_id,
        *,
        should_yield=None,
        resume_task_id=None,
        allow_repair=False,
    ):
        del should_yield, allow_repair
        self.calls.append(turn_id)
        self.resume_tasks.append(resume_task_id)
        return {"status": "ran", "state": "succeeded"}


class Q6GateTests(unittest.TestCase):
    def _bridge(self) -> tuple[Bridge, _Runtime]:
        bridge = object.__new__(Bridge)
        bridge._stopping = threading.Event()
        bridge._maintenance_lock = threading.Lock()
        bridge._maintenance_turns = ["turn-1"]
        bridge._maintenance_resume = None
        bridge._maintenance_resume_timer = None
        bridge.runtime = _Runtime()
        bridge.worker = None
        return bridge, bridge.runtime

    def test_a_pending_foreground_send_defers_without_calling_the_runtime(self) -> None:
        bridge, runtime = self._bridge()
        bridge._foreground_send_pending = lambda: True
        bridge._run_maintenance_review_job()
        self.assertEqual(runtime.calls, [])
        self.assertEqual(bridge._maintenance_turns, ["turn-1"])

    def test_idle_foreground_runs_one_turn_and_consumes_it(self) -> None:
        bridge, runtime = self._bridge()
        bridge._foreground_send_pending = lambda: False
        bridge._run_maintenance_review_job()
        self.assertEqual(runtime.calls, ["turn-1"])
        self.assertEqual(bridge._maintenance_turns, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
