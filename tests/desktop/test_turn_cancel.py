"""RG03 desktop bridge: cooperative cancel and completion races.

Every test uses the real sidecar over the framing pipe with the scripted v2
transport.  No GUI is involved.
"""

from __future__ import annotations

import time
import unittest

from tests.desktop.test_turn_events import (
    TurnBridgeCase,
    v2_call,
    v2_final,
)


class TurnCancelDesktopTests(TurnBridgeCase):
    def test_cancel_wins_and_keeps_published_commentary(self) -> None:
        script = [
            v2_call(
                "cognition.partition",
                {"limit": 5},
                commentary="我翻一下。",
            ),
            v2_final("不应该出现的回答。"),
        ]
        process, info = self._start(script=script, delay_ms=900)
        conversation = self._conversation(process, info)
        turn_id = self._send(process, info, conversation, "查一下。", "c1-req")
        self._wait_events(
            process,
            info,
            turn_id,
            lambda page: any(
                event["type"] == "message_published"
                and event.get("kind") == "commentary"
                for event in page["events"]
            ),
        )
        cancel = self._cancel(process, info, turn_id)
        self.assertTrue(cancel["ok"], cancel)
        self.assertTrue(cancel["result"]["cancel_requested"])
        self.assertFalse(cancel["result"]["terminal"])
        result = self._wait_result(process, info, turn_id)
        self.assertEqual("cancelled", result["state"])
        self.assertIsNone(result["final"])
        page = process.request(
            "conversation.get",
            {"session_id": info["session_id"], "conversation_id": conversation},
        )["result"]
        self.assertEqual(
            ["user", "commentary"], [item["kind"] for item in page["messages"]]
        )
        events = self._events(process, info, turn_id)["result"]
        kinds = [event["type"] for event in events["events"]]
        self.assertIn("turn_cancelled", kinds)
        self.assertNotIn("turn_completed", kinds)
        # A second cancel after the terminal state is honest, not a guess.
        again = self._cancel(process, info, turn_id)
        self.assertTrue(again["ok"], again)
        self.assertFalse(again["result"]["cancel_requested"])
        self.assertIsNone(again["result"]["terminal"])
        self.assertEqual("NOT_RUNNING", again["result"]["reason"])
        # The late model result is audited, never published.
        attempts = int(self.scalar("SELECT COUNT(*) FROM model_attempts"))
        # Depending on where the boundary check lands, the in-flight response
        # either never happened (1 attempt) or was discarded (2).  Every
        # completed-but-unused attempt must be audited exactly once.
        self.assertIn(attempts, (1, 2))
        late = [
            event
            for event in events["events"]
            if event["type"] == "late_result_discarded"
        ]
        self.assertEqual(attempts - 1, len(late))
        self.assertEqual(
            0, int(self.scalar("SELECT COUNT(*) FROM collab_turn_messages WHERE kind = 'final'"))
        )
        interaction = self.scalar(
            "SELECT result FROM collab_interactions LIMIT 1"
        )
        self.assertEqual("incomplete", interaction)

    def test_completion_wins_and_cancel_returns_not_running(self) -> None:
        process, info = self._start(
            script=[v2_final("正常完成的回答。")]
        )
        conversation = self._conversation(process, info)
        turn_id = self._send(process, info, conversation, "答一下。", "c2-req")
        result = self._wait_result(process, info, turn_id)
        self.assertEqual("completed", result["state"])
        self.assertEqual("正常完成的回答。", result["final"]["text"])
        cancel = self._cancel(process, info, turn_id)
        self.assertTrue(cancel["ok"], cancel)
        self.assertFalse(cancel["result"]["cancel_requested"])
        self.assertIsNone(cancel["result"]["terminal"])
        self.assertEqual("NOT_RUNNING", cancel["result"]["reason"])
        after = self._wait_result(process, info, turn_id)
        self.assertEqual("completed", after["state"])
        self.assertEqual("正常完成的回答。", after["final"]["text"])

    def test_busy_reads_and_cross_session_send_are_immediate(self) -> None:
        script = [
            v2_call("cognition.partition", {"limit": 5}),
            v2_final("完成。"),
        ]
        process, info = self._start(script=script, delay_ms=900)
        conversation = self._conversation(process, info)
        other = self._conversation(process, info)
        turn_id = self._send(process, info, conversation, "查一下。", "c3-req")
        # Cross-session send: BUSY, not queued.
        busy = process.request(
            "conversation.send",
            {
                "session_id": info["session_id"],
                "conversation_id": other,
                "client_request_id": "c3-other",
                "text": "另一个会话。",
                "confirmed_destination_id": (
                    info["destination"]["destination_id"]
                    if info["destination"]["confirmation_required"]
                    else None
                ),
            },
        )
        self.assertFalse(busy["ok"], busy)
        self.assertEqual("BUSY", busy["error"]["code"])
        # Reads that need the DB refuse immediately; the ring and cancel do not.
        for method, params in (
            ("turn.result", {"session_id": info["session_id"], "turn_id": turn_id}),
            (
                "conversation.get",
                {"session_id": info["session_id"], "conversation_id": conversation},
            ),
            ("turn.steps", {"session_id": info["session_id"], "turn_id": turn_id}),
        ):
            reply = process.request(method, params)
            self.assertFalse(reply["ok"], (method, reply))
            self.assertEqual("BUSY", reply["error"]["code"], method)
        live = self._events(process, info, turn_id)
        self.assertTrue(live["ok"], live)
        cancel = self._cancel(process, info, turn_id)
        self.assertTrue(cancel["ok"], cancel)
        result = self._wait_result(process, info, turn_id)
        self.assertEqual("cancelled", result["state"])


if __name__ == "__main__":
    unittest.main()
