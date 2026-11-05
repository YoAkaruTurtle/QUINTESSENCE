"""J10 regression: a Lab seed must not be born corrupt.

The W2 unblock traced ``turn:host_failure`` to ``_seed_lab`` writing a
``general_hypothesis`` belief with ``inference_basis="observation"``.  The seed
body is read back through ``parse_belief_body`` during Context assembly, so the
mismatch raised ``CorruptStore`` on every later turn in that namespace -- after
the seed itself had already been committed.  The basis is now derived from the
claim type, and this test pins the general_hypothesis seed end to end.
"""

from __future__ import annotations

import time

from tests.desktop.support import DesktopCase, config_document


class LabSeedBeliefBasisTests(DesktopCase):
    def _document(self) -> dict:
        document = config_document(self.db_path)
        document["lab"]["belief"]["claim_type"] = "general_hypothesis"
        document["lab"]["belief"]["strength"] = "limited"
        document["conversation"] = {
            "history_max_turns": 8,
            "history_max_bytes": 4096,
            "tools_enabled": False,
            "sdrpe": {"mode": "off"},
        }
        document["destination"]["model_protocol"] = "collab.v2"
        document["destination"]["transport"]["script"] = [
            {"action": "final", "reply": "收到。"},
            {"action": "final", "reply": "收到。"},
        ]
        return document

    def test_a_general_hypothesis_seed_still_completes_a_turn(self) -> None:
        process = self.start(document=self._document())
        info = process.request("app.info")["result"]
        self.assertTrue(info["host"]["ready"], info["host"])
        self.assertIsNone(info["host"]["reason_code"], info["host"])
        session_id = info["session_id"]
        destination_id = info["destination"]["destination_id"]
        process.request(
            "app.confirm_destination",
            {"session_id": session_id, "destination_id": destination_id},
        )
        created = process.request(
            "conversation.create", {"session_id": session_id, "mode": "chat"}
        )
        self.assertTrue(created["ok"], created)
        conversation_id = created["result"]["conversation"]["conversation_id"]
        sent = process.request(
            "conversation.send",
            {
                "session_id": session_id,
                "conversation_id": conversation_id,
                "client_request_id": f"j10-seed-{time.time_ns()}",
                "text": "你好",
                "confirmed_destination_id": destination_id,
            },
        )
        self.assertTrue(sent["ok"], sent)
        turn_id = sent["result"]["turn_id"]
        deadline = time.monotonic() + 60
        result = None
        while time.monotonic() < deadline:
            reply = process.request(
                "turn.result", {"session_id": session_id, "turn_id": turn_id}
            )
            if reply.get("ok") and reply["result"].get("terminal"):
                result = reply["result"]
                break
            time.sleep(0.05)
        self.assertIsNotNone(result, "turn did not finish")
        self.assertEqual("completed", result["state"], result)
        self.assertIsNone(result.get("failure_reason"), result)
