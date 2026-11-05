"""RG04 postprocess host methods, privacy and the Q6 foreground-yield gate."""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests.desktop import support

HERE = Path(__file__).resolve().parent
SIDECAR = HERE.parents[1] / "apps" / "desktop" / "sidecar"
if str(SIDECAR) not in sys.path:
    sys.path.insert(0, str(SIDECAR))

import pcs_bridge  # noqa: E402


class PostprocessBridgeTests(support.DesktopCase):
    def _fake_cli(self) -> Path:
        path = self.tmp_path / "fake_capture.py"
        path.write_text(
            "import json, sys\n"
            "raw = sys.stdin.buffer.read()\n"
            "json.loads(raw)\n"
            "json.dump({'kind':'ok','receipt':{"
            "'disposition':'inserted','durability':'persisted',"
            "'event_refs':[{'ref':'external-rg04','revision':1}]}}, sys.stdout)\n",
            encoding="utf-8",
        )
        return path

    def _unknown_then_ok_cli(self) -> tuple[Path, Path]:
        counter = self.tmp_path / "capture-count.txt"
        path = self.tmp_path / "unknown_then_ok_capture.py"
        path.write_text(
            "import json, sys, pathlib\n"
            f"counter = pathlib.Path({str(counter)!r})\n"
            "raw = sys.stdin.buffer.read()\n"
            "json.loads(raw)\n"
            "n = int(counter.read_text()) if counter.exists() else 0\n"
            "counter.write_text(str(n + 1))\n"
            "if n == 0:\n"
            "    json.dump({'kind': 'unknown', 'error': 'timeout'}, sys.stdout)\n"
            "else:\n"
            "    json.dump({'kind':'ok','receipt':{"
            "'disposition':'inserted','durability':'persisted',"
            "'event_refs':[{'ref':'external-rg05-f01','revision':1}]}}, sys.stdout)\n",
            encoding="utf-8",
        )
        return path, counter

    def _document(
        self,
        *,
        memory: bool,
        capture_entry: Path | None = None,
        retry_backoff_seconds: int = 30,
    ):
        purposes = ["consult"]
        memory_bridge = None
        if memory:
            purposes.append("memory.bridge")
            memory_bridge = {
                "enabled": True,
                "python_executable": sys.executable,
                "entry": str(capture_entry or self._fake_cli()),
                "package_path": str(self.tmp_path),
                "data_directory": "memory-external",
                "agent_id": "rg04-sidecar",
                "installation_id": "rg04-sidecar-install",
                "scope_id": "scope-rg04-sidecar",
                "session_id": "session-rg04-sidecar",
                "retry_backoff_seconds": retry_backoff_seconds,
                "permissions": ["memory_bridge"],
            }
        return support.config_document(
            self.db_path,
            purposes=purposes,
            script=[{"action": "none", "reply": "合成回答：已核对。"}],
            memory_bridge=memory_bridge,
        )

    def _start(
        self,
        *,
        memory: bool = True,
        capture_entry: Path | None = None,
        retry_backoff_seconds: int = 30,
    ):
        document = self._document(
            memory=memory,
            capture_entry=capture_entry,
            retry_backoff_seconds=retry_backoff_seconds,
        )
        process = self.start(document=document)
        info = process.request("app.info")["result"]
        if info["destination"]["confirmation_required"]:
            confirm = process.request(
                "app.confirm_destination",
                {
                    "session_id": info["session_id"],
                    "destination_id": info["destination"]["destination_id"],
                },
            )
            self.assertTrue(confirm["ok"], confirm)
        return process, info

    def _conversation(self, process, info) -> str:
        frame = process.request(
            "conversation.create",
            {"session_id": info["session_id"], "mode": "chat"},
        )
        self.assertTrue(frame["ok"], frame)
        return frame["result"]["conversation"]["conversation_id"]

    def _send(self, process, info, conversation_id: str, text: str, request_id: str):
        return process.request(
            "conversation.send",
            {
                "session_id": info["session_id"],
                "conversation_id": conversation_id,
                "client_request_id": request_id,
                "text": text,
                "confirmed_destination_id": (
                    info["destination"]["destination_id"]
                    if info["destination"]["confirmation_required"]
                    else None
                ),
            },
        )

    def _wait_turn(self, process, info, turn_id: str, *, timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ping = process.request("bridge.ping")
            self.assertTrue(ping["ok"], ping)
            if not ping["result"]["busy"]:
                result = process.request(
                    "turn.result",
                    {"session_id": info["session_id"], "turn_id": turn_id},
                )
                if result["ok"]:
                    return result["result"]
            time.sleep(0.05)
        raise TimeoutError("turn did not settle")

    def _status(self, process, info, conversation_id: str, turn_ids=None):
        params = {
            "session_id": info["session_id"],
            "conversation_id": conversation_id,
        }
        if turn_ids is not None:
            params["turn_ids"] = turn_ids
        frame = process.request("postprocess.status", params)
        self.assertTrue(frame["ok"], frame)
        return frame["result"]

    def _wait_memory_state(
        self,
        process,
        info,
        conversation_id: str,
        turn_id: str,
        state: str,
        *,
        timeout: float = 15.0,
    ):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            status = self._status(process, info, conversation_id, [turn_id])
            last = status["turns"][0]["memory"]
            if last["state"] == state:
                return last
            time.sleep(0.1)
        raise TimeoutError(f"memory state {last} did not become {state}")

    def test_idle_chain_delivers_both_refs_without_retry(self) -> None:
        process, info = self._start()
        conversation_id = self._conversation(process, info)
        sent = self._send(process, info, conversation_id, "第一问", "req-1")
        self.assertTrue(sent["ok"], sent)
        turn_id = sent["result"]["turn_id"]
        self._wait_turn(process, info, turn_id)

        memory = self._wait_memory_state(
            process, info, conversation_id, turn_id, "delivered", timeout=15
        )
        self.assertEqual(2, memory["delivered"])
        self.assertEqual(0, memory["pending"])
        self.assertEqual(
            2, self.scalar("SELECT COUNT(*) FROM experience_source_mapping")
        )
        rows = self.query(
            "SELECT state, attempt FROM memory_delivery ORDER BY delivery_id"
        )
        self.assertEqual(2, len(rows))
        self.assertTrue(all(row["state"] == "applied" for row in rows))
        self.assertTrue(all(row["attempt"] == 1 for row in rows))
        log = "\n".join(process.stderr_lines)
        self.assertIn("postprocess follow-up scheduled", log)
        self.assertGreaterEqual(log.count("postprocess sweep projected="), 2)

    def test_retry_after_idle_delivery_does_not_duplicate(self) -> None:
        process, info = self._start()
        conversation_id = self._conversation(process, info)
        sent = self._send(process, info, conversation_id, "第一问", "req-1")
        self.assertTrue(sent["ok"], sent)
        turn_id = sent["result"]["turn_id"]
        self._wait_turn(process, info, turn_id)
        self._wait_memory_state(
            process, info, conversation_id, turn_id, "delivered", timeout=15
        )
        before = [
            tuple(row)
            for row in self.query(
                "SELECT external_ref, external_revision, attempt "
                "FROM memory_delivery ORDER BY delivery_id"
            )
        ]
        retried = process.request(
            "postprocess.retry",
            {"session_id": info["session_id"], "turn_id": turn_id},
        )
        self.assertTrue(retried["ok"], retried)
        self.assertEqual(0, retried["result"]["delivery"]["claimed"])
        after = [
            tuple(row)
            for row in self.query(
                "SELECT external_ref, external_revision, attempt "
                "FROM memory_delivery ORDER BY delivery_id"
            )
        ]
        self.assertEqual(before, after)
        status = self._status(process, info, conversation_id, [turn_id])
        self.assertEqual("delivered", status["turns"][0]["memory"]["state"])

    def test_restart_completes_pending_turn_without_retry(self) -> None:
        first, info = self._start(memory=False)
        conversation_id = self._conversation(first, info)
        sent = self._send(first, info, conversation_id, "重启前", "req-1")
        self.assertTrue(sent["ok"], sent)
        turn_id = sent["result"]["turn_id"]
        self._wait_turn(first, info, turn_id)
        first.close()

        second, second_info = self._start(memory=True)
        memory = self._wait_memory_state(
            second,
            second_info,
            conversation_id,
            turn_id,
            "delivered",
            timeout=15,
        )
        self.assertEqual(2, memory["delivered"])
        self.assertEqual(0, memory["pending"])
        rows = self.query("SELECT state FROM memory_delivery ORDER BY delivery_id")
        self.assertEqual(2, len(rows))
        self.assertTrue(all(row["state"] == "applied" for row in rows))

    def test_unknown_then_success_backs_off_and_delivers(self) -> None:
        capture, counter = self._unknown_then_ok_cli()
        process, info = self._start(
            capture_entry=capture, retry_backoff_seconds=1
        )
        conversation_id = self._conversation(process, info)
        sent = self._send(process, info, conversation_id, "失败一次", "req-1")
        self.assertTrue(sent["ok"], sent)
        turn_id = sent["result"]["turn_id"]
        self._wait_turn(process, info, turn_id)
        memory = self._wait_memory_state(
            process, info, conversation_id, turn_id, "delivered", timeout=20
        )
        self.assertEqual(2, memory["delivered"])
        self.assertEqual(0, memory["pending"])
        self.assertEqual(3, int(counter.read_text(encoding="utf-8")))
        attempts = [
            row["attempt"]
            for row in self.query(
                "SELECT attempt FROM memory_delivery ORDER BY attempt DESC"
            )
        ]
        self.assertEqual([2, 1], attempts)
        self.assertEqual(
            2,
            self.scalar(
                "SELECT COUNT(*) FROM memory_delivery WHERE state = 'applied'"
            ),
        )

    def test_status_and_retry_after_a_turn(self) -> None:
        process, info = self._start()
        conversation_id = self._conversation(process, info)
        sent = self._send(process, info, conversation_id, "第一问", "req-1")
        self.assertTrue(sent["ok"], sent)
        turn_id = sent["result"]["turn_id"]
        result = self._wait_turn(process, info, turn_id)
        self.assertEqual("completed", result["state"])

        status = self._status(process, info, conversation_id, [turn_id])
        self.assertIn("turns", status)
        turn = status["turns"][0]
        self.assertTrue(turn["transcript_saved"])
        self.assertIn(
            turn["memory"]["state"],
            ("pending", "delivered", "outcome_unknown"),
        )

        retried = process.request(
            "postprocess.retry",
            {"session_id": info["session_id"], "turn_id": turn_id},
        )
        self.assertTrue(retried["ok"], retried)
        self.assertFalse(retried["result"].get("skipped", False))
        self.assertEqual("delivered", retried["result"]["status"]["memory"]["state"])
        final_status = self._status(process, info, conversation_id, [turn_id])
        self.assertIsNotNone(final_status["watermark"])

    def test_retry_without_bridge_is_unavailable(self) -> None:
        process, info = self._start(memory=False)
        conversation_id = self._conversation(process, info)
        sent = self._send(process, info, conversation_id, "无桥", "req-1")
        self.assertTrue(sent["ok"], sent)
        turn_id = sent["result"]["turn_id"]
        self._wait_turn(process, info, turn_id)
        retried = process.request(
            "postprocess.retry",
            {"session_id": info["session_id"], "turn_id": turn_id},
        )
        self.assertFalse(retried["ok"], retried)
        self.assertEqual("SIDECAR_UNAVAILABLE", retried["error"]["code"])
        status = self._status(process, info, conversation_id, [turn_id])
        self.assertEqual("not_configured", status["turns"][0]["memory"]["state"])

    def test_status_rejects_cross_conversation_turn_and_authority(self) -> None:
        process, info = self._start(memory=False)
        first = self._conversation(process, info)
        second = self._conversation(process, info)
        sent = self._send(process, info, first, "第一问", "req-1")
        turn_id = sent["result"]["turn_id"]
        self._wait_turn(process, info, turn_id)
        mismatch = process.request(
            "postprocess.status",
            {
                "session_id": info["session_id"],
                "conversation_id": second,
                "turn_ids": [turn_id],
            },
        )
        self.assertFalse(mismatch["ok"], mismatch)
        poisoned = process.request(
            "postprocess.status",
            {
                "session_id": info["session_id"],
                "conversation_id": second,
                "namespace": "example.other",
            },
        )
        self.assertFalse(poisoned["ok"], poisoned)
        self.assertEqual("AUTHORITY_FIELD_REJECTED", poisoned["error"]["code"])
        listed = process.request("app.info")["result"]
        self.assertIn("postprocess.status", listed["methods"])
        self.assertIn("postprocess.retry", listed["methods"])
        self.assertIn("memory", listed["capabilities"])

    # --------------------------------------------------- Q6 gate (unit)

    def _bare_bridge(
        self,
        *,
        send_pending: int = 0,
        reports: list[dict] | None = None,
        worker=None,
        after_sweep=None,
    ):
        class FakeRuntime:
            def __init__(self) -> None:
                self.calls = 0
                self.reports = list(reports or [])

            def postprocess_sweep(self):
                self.calls += 1
                if after_sweep is not None:
                    after_sweep()
                if self.reports:
                    return self.reports.pop(0)
                return {"projected_refs": 0}

            def memory_bridge_configured(self) -> bool:
                return True

        bridge = pcs_bridge.Bridge.__new__(pcs_bridge.Bridge)
        bridge.runtime = FakeRuntime()
        bridge.worker = worker
        bridge._stopping = threading.Event()
        bridge._model_lock = threading.Lock()
        bridge._send_lock = threading.Lock()
        bridge._send_pending = send_pending
        bridge._sweep_lock = threading.Lock()
        bridge._sweep_timer = None
        bridge._sweep_skips = 0
        bridge._sweep_followups = 0
        return bridge

    def test_sweep_yields_to_a_queued_foreground_send(self) -> None:
        bridge = self._bare_bridge(send_pending=1)
        order: list[str] = []
        # The worker's FIFO order in the dangerous case: sweep first, then send.
        bridge._run_postprocess_sweep_job()
        order.append("send")
        self.assertEqual(["send"], order)
        self.assertEqual(0, bridge.runtime.calls)
        self.assertEqual(1, bridge._sweep_skips)
        self.assertEqual(0, bridge._sweep_followups)
        self.assertIsNone(bridge._sweep_timer)

        bridge._send_pending = 0
        bridge._run_postprocess_sweep_job()
        self.assertEqual(1, bridge.runtime.calls)
        self.assertEqual(1, bridge._sweep_skips)

    def test_sweep_yields_while_a_send_lock_is_held(self) -> None:
        bridge = self._bare_bridge(send_pending=0)
        bridge._model_lock.acquire()
        try:
            bridge._run_postprocess_sweep_job()
        finally:
            bridge._model_lock.release()
        self.assertEqual(0, bridge.runtime.calls)
        self.assertEqual(1, bridge._sweep_skips)

    # ------------------------------------------------- RG05-F01 idle chain

    def test_follow_up_plan_due_and_retry_backoff(self) -> None:
        now = datetime(2026, 10, 29, 3, 4, 54, tzinfo=timezone.utc)
        due = {
            "backlog": {
                "pending": 1,
                "due": 1,
                "next_due_at": None,
                "projection_capped": False,
            }
        }
        plan = pcs_bridge._next_postprocess_follow_up(due, depth=0, now=now)
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(1.0, plan.delay_seconds)
        self.assertEqual(1, plan.depth)

        future = {
            "backlog": {
                "pending": 1,
                "due": 0,
                "next_due_at": "2026-10-29T03:05:24.000000Z",
                "projection_capped": False,
            }
        }
        plan = pcs_bridge._next_postprocess_follow_up(future, depth=1, now=now)
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(30.25, plan.delay_seconds)
        self.assertEqual(2, plan.depth)

        capped = {
            "backlog": {
                "pending": 0,
                "due": 0,
                "next_due_at": None,
                "projection_capped": True,
            }
        }
        plan = pcs_bridge._next_postprocess_follow_up(capped, depth=0, now=now)
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(1.0, plan.delay_seconds)
        self.assertIsNone(
            pcs_bridge._next_postprocess_follow_up(due, depth=3, now=now)
        )
        self.assertIsNone(
            pcs_bridge._next_postprocess_follow_up({}, depth=0, now=now)
        )
        self.assertIsNone(
            pcs_bridge._next_postprocess_follow_up(
                {
                    "backlog": {
                        "pending": 1,
                        "due": 0,
                        "next_due_at": "not-a-time",
                        "projection_capped": False,
                    }
                },
                depth=0,
                now=now,
            )
        )

    def test_follow_up_count_is_bounded_and_external_trigger_resets(self) -> None:
        class FakeWorker:
            def submit(self, job) -> None:
                return None

        bridge = self._bare_bridge(worker=FakeWorker())
        for _ in range(3):
            self.assertTrue(bridge._schedule_postprocess_sweep(0.1, follow_up=True))
            self.assertIsNotNone(bridge._sweep_timer)
            bridge._sweep_timer.cancel()
            bridge._sweep_timer = None
        self.assertEqual(3, bridge._sweep_followups)
        self.assertFalse(bridge._schedule_postprocess_sweep(0.1, follow_up=True))
        self.assertTrue(bridge._schedule_postprocess_sweep(0.1))
        self.assertEqual(0, bridge._sweep_followups)
        bridge._sweep_timer.cancel()
        bridge._sweep_timer = None

    def test_sweep_schedules_one_follow_up_on_progress(self) -> None:
        class FakeWorker:
            def submit(self, job) -> None:
                return None

        report = {
            "projected_refs": 2,
            "delivered": 1,
            "backlog": {
                "pending": 1,
                "due": 1,
                "next_due_at": None,
                "projection_capped": False,
            },
        }
        bridge = self._bare_bridge(worker=FakeWorker(), reports=[report])
        bridge._run_postprocess_sweep_job()
        self.assertEqual(1, bridge.runtime.calls)
        self.assertEqual(1, bridge._sweep_followups)
        self.assertIsNotNone(bridge._sweep_timer)
        bridge._sweep_timer.cancel()
        bridge._sweep_timer = None

    def test_sweep_does_not_follow_up_on_empty_backlog(self) -> None:
        class FakeWorker:
            def submit(self, job) -> None:
                return None

        report = {
            "projected_refs": 0,
            "delivered": 0,
            "backlog": {
                "pending": 0,
                "due": 0,
                "next_due_at": None,
                "projection_capped": False,
            },
        }
        bridge = self._bare_bridge(worker=FakeWorker(), reports=[report])
        bridge._run_postprocess_sweep_job()
        self.assertEqual(1, bridge.runtime.calls)
        self.assertIsNone(bridge._sweep_timer)
        self.assertEqual(0, bridge._sweep_followups)

    def test_follow_up_stops_when_a_send_arrives_during_sweep(self) -> None:
        class FakeWorker:
            def submit(self, job) -> None:
                return None

        bridge = None

        def mark_send() -> None:
            bridge._send_pending = 1

        report = {
            "projected_refs": 2,
            "delivered": 1,
            "backlog": {
                "pending": 1,
                "due": 1,
                "next_due_at": None,
                "projection_capped": False,
            },
        }
        bridge = self._bare_bridge(
            worker=FakeWorker(), reports=[report], after_sweep=mark_send
        )
        bridge._run_postprocess_sweep_job()
        self.assertEqual(1, bridge.runtime.calls)
        self.assertIsNone(bridge._sweep_timer)
        self.assertEqual(0, bridge._sweep_followups)


if __name__ == "__main__":
    unittest.main()
