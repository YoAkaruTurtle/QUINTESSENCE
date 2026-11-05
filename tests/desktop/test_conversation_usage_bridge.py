"""UI-F01: the read-only usage and turn-trace bridge methods.

Real sidecar, real store and the real scripted transport (no network, no real
model).  The script makes the first content generation malformed so the repair
path actually runs and writes its persisted marker; the tests then prove the
numbers, the repair proof, the BUSY rule and that a restart reads the same data.
"""

from __future__ import annotations

import time
import unittest

from tests.desktop.support import DesktopCase, config_document


def reply(text: str) -> dict:
    return {"action": "none", "reply": text}


class ConversationUsageBridgeTests(DesktopCase):
    def _start(self, *, script, delay_ms: int = 0):
        document = config_document(self.db_path, script=script, delay_ms=delay_ms)
        process = self.start(document=document)
        info = process.request("app.info")["result"]
        self.assertTrue(info["host"]["ready"], info)
        self.assertIsNotNone(info["destination"], info)
        if info["destination"]["confirmation_required"]:
            confirm = process.request(
                "app.confirm_destination",
                {
                    "session_id": info["session_id"],
                    "destination_id": info["destination"]["destination_id"],
                },
            )
            self.assertTrue(confirm["ok"], confirm)
        return process, info, document

    def _conversation(self, process, info) -> str:
        frame = process.request(
            "conversation.create",
            {"session_id": info["session_id"], "mode": "chat"},
        )
        self.assertTrue(frame["ok"], frame)
        return frame["result"]["conversation"]["conversation_id"]

    def _send(self, process, info, conversation_id, text, request_id):
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

    def _wait_turn(self, process, info, turn_id, *, timeout: float = 30.0):
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

    def _usage(self, process, info, conversation_id, turn_id=None):
        return process.request(
            "conversation.usage",
            {
                "session_id": info["session_id"],
                "conversation_id": conversation_id,
                "turn_id": turn_id,
            },
        )

    def _trace(self, process, info, turn_id):
        return process.request(
            "turn.trace", {"session_id": info["session_id"], "turn_id": turn_id}
        )

    # ---------------------------------------------------------------- tests

    def test_usage_and_trace_report_real_requests_and_the_repair_marker(self) -> None:
        script = [{"action": "none", "reply": ""}, reply("修复后的回答")]
        process, info, _document = self._start(script=script)
        self.assertIn("conversation.usage", info["methods"])
        self.assertIn("turn.trace", info["methods"])
        conversation = self._conversation(process, info)
        sent = self._send(process, info, conversation, "格式修复", "f01-repair")
        self.assertTrue(sent["ok"], sent)
        turn_id = sent["result"]["turn_id"]
        result = self._wait_turn(process, info, turn_id)
        self.assertEqual("completed", result["state"])

        usage = self._usage(process, info, conversation, turn_id)
        self.assertTrue(usage["ok"], usage)
        payload = usage["result"]
        self.assertEqual(2, payload["attempts_total"])
        self.assertEqual(2, payload["attempts_known"])
        self.assertTrue(payload["tokens"]["complete"])
        self.assertEqual(320, payload["tokens"]["total"])
        self.assertEqual(1, payload["counts"]["repairs"])
        self.assertEqual(0, payload["counts"]["repairs_unbound"])
        self.assertEqual(turn_id, payload["turn"]["turn_id"])
        self.assertEqual(320, payload["turn"]["tokens"]["total"])
        self.assertEqual(1, payload["turn"]["counts"]["repairs"])
        latest = payload["latest_request"]
        self.assertEqual("repair", latest["kind"])
        self.assertTrue(latest["repair"])
        self.assertEqual("format", latest["repair_reason"])
        # No authority material, request hash or body may leave the host.
        rendered = str(payload)
        self.assertNotIn("authorization_ref", rendered)
        self.assertNotIn("request_hash", rendered)
        self.assertNotIn("rendered_input", rendered)

        trace = self._trace(process, info, turn_id)
        self.assertTrue(trace["ok"], trace)
        view = trace["result"]
        self.assertEqual("content", view["final"]["source"])
        # A collab.v1 turn has one content stage and never invents an
        # expression stage row; the fallback flag therefore stays false.
        self.assertEqual(["content"], [stage["kind"] for stage in view["stages"]])
        self.assertEqual("succeeded", view["stages"][0]["status"])
        self.assertFalse(view["final"]["fallback"])
        attempts = [
            attempt
            for stage in view["stages"]
            for context in stage["contexts"]
            for attempt in context["attempts"]
        ]
        self.assertEqual(["primary", "repair"], [item["kind"] for item in attempts])
        self.assertEqual(1, view["counts"]["repairs"])
        self.assertEqual(0, view["counts"]["repairs_unbound"])
        self.assertEqual(0, view["counts"]["repair_unknown"])
        self.assertFalse(view["truncated"])
        self.assertGreaterEqual(view["attempts_total"], 2)

        events = process.request(
            "turn.events",
            {"session_id": info["session_id"], "turn_id": turn_id},
        )
        self.assertTrue(events["ok"], events)
        markers = [
            event for event in events["result"]["events"]
            if event["type"] == "repair_attempt"
        ]
        self.assertEqual(1, len(markers))
        marker = markers[0]
        self.assertEqual({"stage", "reason"}, {key for key in marker if key in ("stage", "reason")})
        rendered_event = str(marker)
        self.assertNotIn("authorization_id", rendered_event)
        self.assertNotIn("request_hash", rendered_event)
        self.assertNotIn("sha256:", rendered_event)
        self.assertEqual("format", marker["reason"])

        again = self._usage(process, info, conversation)
        self.assertTrue(again["ok"], again)
        self.assertEqual(payload["tokens"], again["result"]["tokens"])

    def test_usage_is_busy_while_a_turn_runs(self) -> None:
        process, info, _document = self._start(
            script=[reply("慢一点的回答")], delay_ms=900
        )
        conversation = self._conversation(process, info)
        sent = self._send(process, info, conversation, "慢问题", "f01-busy")
        self.assertTrue(sent["ok"], sent)
        turn_id = sent["result"]["turn_id"]
        busy = self._usage(process, info, conversation, turn_id)
        self.assertFalse(busy["ok"], busy)
        self.assertEqual("BUSY", busy["error"]["code"])
        result = self._wait_turn(process, info, turn_id)
        self.assertEqual("completed", result["state"])
        settled = self._usage(process, info, conversation, turn_id)
        self.assertTrue(settled["ok"], settled)
        self.assertEqual(1, settled["result"]["attempts_total"])

    def test_a_restart_reads_the_same_persisted_numbers(self) -> None:
        script = [{"action": "none", "reply": ""}, reply("重启后的回答")]
        process, info, document = self._start(script=script)
        conversation = self._conversation(process, info)
        sent = self._send(process, info, conversation, "重启验证", "f01-restart")
        turn_id = sent["result"]["turn_id"]
        self._wait_turn(process, info, turn_id)
        before = self._usage(process, info, conversation, turn_id)["result"]
        before_trace = self._trace(process, info, turn_id)["result"]

        process.close()
        restarted = self.start(document=document)
        new_info = restarted.request("app.info")["result"]
        after = self._usage(restarted, new_info, conversation, turn_id)["result"]
        after_trace = self._trace(restarted, new_info, turn_id)["result"]
        self.assertEqual(before["tokens"], after["tokens"])
        self.assertEqual(before["counts"], after["counts"])
        self.assertEqual(before_trace["counts"], after_trace["counts"])
        self.assertEqual(before_trace["final"], after_trace["final"])
        self.assertEqual(
            [item["kind"] for item in before_trace["stages"]],
            [item["kind"] for item in after_trace["stages"]],
        )

    def test_unknown_turn_and_foreign_conversation_are_refused(self) -> None:
        process, info, _document = self._start(script=[reply("回答")])
        conversation = self._conversation(process, info)
        missing = self._usage(
            process, info, "00000000-0000-4000-8000-000000000000"
        )
        self.assertFalse(missing["ok"], missing)
        self.assertEqual("NOT_FOUND", missing["error"]["code"])
        bad_trace = self._trace(
            process, info, "00000000-0000-4000-8000-000000000000"
        )
        self.assertFalse(bad_trace["ok"], bad_trace)
        self.assertEqual("NOT_FOUND", bad_trace["error"]["code"])
        sent = self._send(process, info, conversation, "问题", "f01-notfound")
        turn_id = sent["result"]["turn_id"]
        self._wait_turn(process, info, turn_id)
        other = self._conversation(process, info)
        wrong = self._usage(process, info, other, turn_id)
        self.assertFalse(wrong["ok"], wrong)
        self.assertEqual("NOT_FOUND", wrong["error"]["code"])


if __name__ == "__main__":
    unittest.main()
