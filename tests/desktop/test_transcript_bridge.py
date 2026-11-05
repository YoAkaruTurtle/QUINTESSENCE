"""RG04 04.4: the ``conversation.search`` host method and its boundaries."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

from tests.desktop import support

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quintessence_pcs.store import CognitionStore  # noqa: E402


class TranscriptBridgeTests(support.DesktopCase):
    def _start(self, *, script, delay_ms: int = 0):
        document = support.config_document(
            self.db_path,
            script=script,
            delay_ms=delay_ms,
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
            if ping["ok"] and not ping["result"]["busy"]:
                result = process.request(
                    "turn.result",
                    {"session_id": info["session_id"], "turn_id": turn_id},
                )
                if result["ok"]:
                    return result["result"]
            time.sleep(0.05)
        raise TimeoutError("turn did not settle")

    def _search(self, process, info, **params):
        frame = process.request(
            "conversation.search",
            {"session_id": info["session_id"], **params},
        )
        return frame

    def test_search_finds_current_turn_and_rejects_bad_parameters(self) -> None:
        process, info = self._start(
            script=[{"action": "none", "reply": "读书会定在周五。"}]
        )
        conversation_id = self._conversation(process, info)
        sent = self._send(process, info, conversation_id, "读书会怎么安排？", "req-1")
        self.assertTrue(sent["ok"], sent)
        self._wait_turn(process, info, sent["result"]["turn_id"])

        frame = self._search(process, info, query="读书会")
        self.assertTrue(frame["ok"], frame)
        items = frame["result"]["items"]
        self.assertEqual(2, len(items))
        self.assertEqual({"user", "assistant"}, {item["role"] for item in items})
        self.assertEqual(
            {conversation_id}, {item["conversation_id"] for item in items}
        )
        self.assertIn("methods", process.request("app.info")["result"])
        self.assertIn(
            "conversation.search",
            process.request("app.info")["result"]["methods"],
        )

        too_many = self._search(process, info, query="读书会", limit=11)
        self.assertFalse(too_many["ok"])
        unknown = self._search(process, info, query="读书会", namespace="other")
        self.assertFalse(unknown["ok"])
        self.assertEqual("AUTHORITY_FIELD_REJECTED", unknown["error"]["code"])
        empty = self._search(process, info, query="  ")
        self.assertFalse(empty["ok"])

    def test_search_returns_legacy_records_without_a_conversation(self) -> None:
        store = CognitionStore.open(self.db_path)
        try:
            input_row = store.register_input(
                namespace=support.NAMESPACE,
                origin_kind="user_statement",
                body="旧记录：读书会",
                privacy_class="private",
                retention="standard",
            )
            interaction = store.register_interaction(
                namespace=support.NAMESPACE,
                mode="consult",
                activation_kind="user_input",
                host_instance=support.HOST_INSTANCE,
                access_ref=support.ACCESS_REF,
                policy_ref=support.POLICY_REF,
                input_refs=[
                    {
                        "ref_type": "collab_input",
                        "namespace": support.NAMESPACE,
                        "input_id": input_row.input_id,
                        "content_version": input_row.content_version,
                    }
                ],
                result="answered",
            )
            context_id = store.capture_snapshot(support.NAMESPACE).context_id
            store.register_artifact(
                namespace=support.NAMESPACE,
                kind="reply",
                interaction_id=interaction,
                context_id=context_id,
                origin_kind="model_output",
                body="旧记录回答：读书会",
                retention="standard",
            )
        finally:
            store.close()
        process, info = self._start(
            script=[{"action": "none", "reply": "不应使用的脚本。"}]
        )
        frame = self._search(process, info, query="旧记录")
        self.assertTrue(frame["ok"], frame)
        items = frame["result"]["items"]
        self.assertEqual(2, len(items))
        for item in items:
            self.assertEqual("legacy", item["source"])
            self.assertIsNone(item["conversation_id"])
            self.assertIsNone(item["turn_no"])
            self.assertIsNotNone(item["legacy_interaction_id"])

    def test_search_cursor_pages_without_duplicates(self) -> None:
        process, info = self._start(
            script=[
                {"action": "none", "reply": "读书会第一版。"},
                {"action": "none", "reply": "读书会第二版。"},
            ]
        )
        conversation_id = self._conversation(process, info)
        for index in (1, 2):
            sent = self._send(
                process,
                info,
                conversation_id,
                f"读书会问题 {index}",
                f"req-{index}",
            )
            self.assertTrue(sent["ok"], sent)
            self._wait_turn(process, info, sent["result"]["turn_id"])
        first = self._search(process, info, query="读书会", limit=1)
        self.assertTrue(first["ok"], first)
        self.assertEqual(1, len(first["result"]["items"]))
        cursor = first["result"]["next_cursor"]
        self.assertIsNotNone(cursor)
        second = self._search(process, info, query="读书会", limit=1, before=cursor)
        self.assertTrue(second["ok"], second)
        self.assertEqual(1, len(second["result"]["items"]))
        self.assertNotEqual(
            first["result"]["items"][0]["message_id"],
            second["result"]["items"][0]["message_id"],
        )

    def test_search_is_busy_while_a_turn_is_running(self) -> None:
        process, info = self._start(
            script=[{"action": "none", "reply": "慢回答。"}], delay_ms=1500
        )
        conversation_id = self._conversation(process, info)
        sent = self._send(process, info, conversation_id, "慢问题", "req-1")
        self.assertTrue(sent["ok"], sent)
        try:
            frame = self._search(process, info, query="慢问题")
            self.assertFalse(frame["ok"], frame)
            self.assertEqual("BUSY", frame["error"]["code"])
        finally:
            self._wait_turn(process, info, sent["result"]["turn_id"])


if __name__ == "__main__":
    unittest.main()
