"""UI-F02: the preference view/revoke bridge (confirmed, tentative, inactive).

The real sidecar and store are used.  Tentative values are created by a real
turn (the host's deterministic directive plan), never by the settings API; the
confirmed fixture is one supported ``user_preference`` record written through
the public store surface before the sidecar opens the instance.
"""

from __future__ import annotations

import json
import time
import unittest
import uuid

from tests.desktop.support import NAMESPACE, DesktopCase, config_document

CONFIRMED_CLAIM = "用户在聊天中希望回答保持简短。"
SESSION_TEXT = "我其实更喜欢简短一点的回答。"


class PreferenceBridgeTests(DesktopCase):
    def document(
        self,
        *,
        delay_ms: int = 0,
        propose: bool = False,
        cognition_enabled: bool = True,
        script: list[dict] | None = None,
    ):
        document = config_document(
            self.db_path,
            delay_ms=delay_ms,
            script=script or [{"action": "final", "reply": "嗯。"}],
        )
        document["budget"]["pcs_context_max_bytes"] = 65536
        document["cognition"] = {"enabled": cognition_enabled, "propose": propose}
        document["conversation"] = {
            "history_max_turns": 8,
            "history_max_bytes": 4096,
            "tools_enabled": True,
            "sdrpe": {"mode": "off"},
        }
        document["destination"]["model_protocol"] = "collab.v2"
        return document

    def start_chat(self, *, delay_ms: int = 0, propose: bool = False):
        process = self.start(document=self.document(delay_ms=delay_ms, propose=propose))
        info = process.request("app.info")["result"]
        process.request(
            "app.confirm_destination",
            {
                "session_id": info["session_id"],
                "destination_id": info["destination"]["destination_id"],
            },
        )
        created = process.request(
            "conversation.create", {"session_id": info["session_id"], "mode": "chat"}
        )
        self.assertTrue(created["ok"], created)
        conversation_id = created["result"]["conversation"]["conversation_id"]
        return process, info, conversation_id

    def send(self, process, info, conversation_id, text=SESSION_TEXT):
        sent = process.request(
            "conversation.send",
            {
                "session_id": info["session_id"],
                "conversation_id": conversation_id,
                "client_request_id": f"ui-f02-pref-{time.time_ns()}",
                "text": text,
                "confirmed_destination_id": info["destination"]["destination_id"],
            },
        )
        self.assertTrue(sent["ok"], sent)
        turn_id = sent["result"]["turn_id"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            reply = process.request(
                "turn.result",
                {"session_id": info["session_id"], "turn_id": turn_id},
            )
            if reply.get("ok") and reply["result"].get("terminal"):
                return reply["result"]
            time.sleep(0.05)
        raise TimeoutError(f"turn {turn_id} did not finish")

    def preferences(self, process, info, conversation_id=None):
        params = {"session_id": info["session_id"]}
        if conversation_id is not None:
            params["conversation_id"] = conversation_id
        reply = process.request("preference.list", params)
        self.assertTrue(reply["ok"], reply)
        return reply["result"]

    def revoke(self, process, info, conversation_id, dimension="length", reason=None):
        params = {
            "session_id": info["session_id"],
            "conversation_id": conversation_id,
            "dimension": dimension,
        }
        if reason is not None:
            params["reason"] = reason
        return process.request("preference.revoke", params)

    def seed_confirmed(self) -> None:
        from quintessence_pcs.store import CognitionStore

        store = CognitionStore.open(self.db_path)
        try:
            row = store.register_input(
                namespace=NAMESPACE,
                origin_kind="user_statement",
                body="以后回答简短一点。",
                privacy_class="private",
                retention="standard",
                content_version=1,
            )
            ref = {
                "ref_type": "collab_input",
                "namespace": NAMESPACE,
                "input_id": row.input_id,
                "content_version": 1,
            }
            store.append_record_version(
                namespace=NAMESPACE,
                record_id=str(uuid.uuid4()),
                record_version=1,
                kind="belief",
                body={
                    "claim": CONFIRMED_CLAIM,
                    "claim_type": "user_preference",
                    "evidence_links": [
                        {
                            "ref": ref,
                            "relation": "supports",
                            "origin_group_id": f"collab_input:{row.input_id}",
                            "applicability_reason": "synthetic bridge fixture",
                        }
                    ],
                    "strength": "limited",
                    "strength_reason": "synthetic bridge fixture",
                    "inference_basis": "direct_report",
                    "premise_refs": [],
                    "alternative_group_id": None,
                    "valid_until": None,
                    "review_condition": "expression_preference:length=concise",
                },
                status="supported",
                schema_version="1",
                privacy_class="private",
                scope={"scene": "lab.synthetic"},
            )
        finally:
            store.close()

    def boot_with_confirmed(self):
        """Create the database, seed one confirmed preference, then start."""

        process, info, _conversation = self.start_chat()
        process.close()
        self.seed_confirmed()
        process, info, conversation_id = self.start_chat()
        return process, info, conversation_id

    # ------------------------------------------------------------------ read

    def test_confirmed_reaches_expression_when_cognition_is_disabled(self) -> None:
        """Reading an existing preference is not a cognition update."""

        process, info, _conversation = self.start_chat()
        process.close()
        self.seed_confirmed()
        document = self.document(
            cognition_enabled=False,
            script=[
                {"action": "final", "reply": "简短草稿。"},
                {"action": "final", "reply": "简短表达。"},
            ],
        )
        document["conversation"]["sdrpe"] = {"mode": "on"}
        process = self.start(document=document)
        info = process.request("app.info")["result"]
        process.request(
            "app.confirm_destination",
            {
                "session_id": info["session_id"],
                "destination_id": info["destination"]["destination_id"],
            },
        )
        created = process.request(
            "conversation.create", {"session_id": info["session_id"], "mode": "chat"}
        )
        conversation_id = created["result"]["conversation"]["conversation_id"]
        result = self.send(process, info, conversation_id)
        self.assertEqual("completed", result["state"], result)
        rows = self.query(
            "SELECT rendered_input_payload_ref FROM context_snapshots "
            "ORDER BY created_at DESC, context_id DESC"
        )
        document = None
        for row in rows:
            body = self.query(
                "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
                (row["rendered_input_payload_ref"],),
            )
            if not body:
                continue
            candidate = json.loads(json.loads(body[0]["body_json"])["rendered_input"])
            if "expression" in candidate:
                document = candidate
                break
        self.assertIsNotNone(document, "no expression context was persisted")
        confirmed = document["expression"]["confirmed"]
        self.assertEqual(("length", "concise"), (confirmed[0]["dimension"], confirmed[0]["value"]))
        self.assertNotIn("cognition", document)
        self.assertNotIn("tools", document)

    def test_info_discloses_the_preference_capabilities(self) -> None:
        process, info, _conversation = self.start_chat()
        self.assertEqual(
            {"read": True, "revoke": True}, info["capabilities"]["preferences"]
        )
        self.assertIn("preference.list", info["methods"])
        self.assertIn("preference.revoke", info["methods"])

    def test_confirmed_is_listed_even_without_a_conversation(self) -> None:
        process, info, _conversation = self.boot_with_confirmed()
        result = self.preferences(process, info)
        self.assertEqual(1, len(result["confirmed"]))
        item = result["confirmed"][0]
        self.assertEqual(("length", "concise"), (item["dimension"], item["value"]))
        self.assertEqual("简短", item["label"])
        self.assertEqual({"scene": "lab.synthetic"}, item["scope"])
        self.assertNotIn("quote", item)
        self.assertNotIn(CONFIRMED_CLAIM, json.dumps(result))
        self.assertEqual([], result["tentative"])
        self.assertIsNone(result["conversation"])
        self.assertEqual(
            {"enabled": False, "reason": "EXPRESSION_RECORDING_RETIRED"},
            result["expression_recording"],
        )
        self.assertEqual(
            {"enabled": False, "reason": "COGNITION_PROPOSE_DISABLED"},
            result["confirmed_recording"],
        )

    def test_an_ordinary_turn_lists_no_tentative_preference(self) -> None:
        # J11: the host no longer records a tentative expression preference from
        # an ordinary remark, so the settings view stays empty.
        process, info, conversation_id = self.start_chat()
        result = self.send(process, info, conversation_id)
        self.assertEqual("completed", result["state"], result)
        listed = self.preferences(process, info, conversation_id)
        self.assertEqual([], listed["tentative"])
        self.assertEqual([], listed["inactive"])
        self.assertEqual(
            0,
            self.scalar(
                "SELECT COUNT(*) FROM record_versions "
                "WHERE kind = 'expression_preference'"
            ),
        )
        self.assertNotIn(SESSION_TEXT, json.dumps(listed))

    # ---------------------------------------------------------------- revoke

    def test_revoke_without_an_active_preference_is_not_active(self) -> None:
        process, info, conversation_id = self.start_chat()
        self.send(process, info, conversation_id)
        reply = self.revoke(process, info, conversation_id, reason="设置里撤销")
        self.assertFalse(reply["ok"])
        self.assertEqual("NOT_FOUND", reply["error"]["code"])
        self.assertEqual(
            0,
            self.scalar(
                "SELECT COUNT(*) FROM record_versions "
                "WHERE kind = 'expression_preference'"
            ),
        )

    def test_second_revoke_is_refused_as_not_active(self) -> None:
        process, info, conversation_id = self.start_chat()
        self.send(process, info, conversation_id)
        first = self.revoke(process, info, conversation_id)
        self.assertEqual("NOT_FOUND", first["error"]["code"])
        again = self.revoke(process, info, conversation_id)
        self.assertFalse(again["ok"])
        self.assertEqual("NOT_FOUND", again["error"]["code"])

    def test_revoke_of_an_unknown_dimension_is_not_found(self) -> None:
        process, info, conversation_id = self.start_chat()
        reply = self.revoke(process, info, conversation_id, dimension="tone")
        self.assertFalse(reply["ok"])
        self.assertEqual("NOT_FOUND", reply["error"]["code"])

    def test_revoke_rejects_an_unknown_conversation(self) -> None:
        process, info, _conversation = self.start_chat()
        reply = self.revoke(
            process, info, "55555555-5555-4555-8555-555555555555"
        )
        self.assertFalse(reply["ok"])
        self.assertEqual("NOT_FOUND", reply["error"]["code"])

    def test_revoke_rejects_authority_fields(self) -> None:
        process, info, conversation_id = self.start_chat()
        reply = process.request(
            "preference.revoke",
            {
                "session_id": info["session_id"],
                "conversation_id": conversation_id,
                "dimension": "length",
                "authorization_ref": "authorization:forged",
            },
        )
        self.assertFalse(reply["ok"])
        self.assertEqual("AUTHORITY_FIELD_REJECTED", reply["error"]["code"])

    def test_bad_dimension_and_reason_are_invalid_params(self) -> None:
        process, info, conversation_id = self.start_chat()
        bad_dimension = self.revoke(process, info, conversation_id, dimension="tools")
        self.assertFalse(bad_dimension["ok"])
        self.assertEqual("INVALID_PARAMS", bad_dimension["error"]["code"])
        bad_reason = self.revoke(
            process, info, conversation_id, reason="x" * 513
        )
        self.assertFalse(bad_reason["ok"])
        self.assertEqual("INVALID_PARAMS", bad_reason["error"]["code"])


if __name__ == "__main__":
    unittest.main()
