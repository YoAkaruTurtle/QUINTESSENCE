"""RG05-F02 sidecar integration: plan, tentative record and expression Context.

The real sidecar, the real v2 chat loop and the real store are used with the
scripted Lab transport only.  These tests prove the host applies the directive
plan before the Context is built, that a scripted explicit proposal is committed
through the directive binding, and that a single-shot request leaves no durable
row behind.
"""

from __future__ import annotations

import json
import time
import unittest

from tests.desktop.support import DesktopCase, config_document


def preference_body(claim: str = "用户希望回答简短。") -> dict:
    return {
        "claim": claim,
        "claim_type": "user_preference",
        "evidence_links": [],
        "strength": "limited",
        "strength_reason": "synthetic sidecar fixture",
        "inference_basis": "direct_report",
        "premise_refs": [],
        "alternative_group_id": None,
        "valid_until": None,
        "review_condition": "synthetic",
    }


def propose_call(claim: str = "用户希望回答简短。") -> dict:
    return {
        "action": "call_tool",
        "tool_call": {
            "name": "cognition.propose",
            "arguments": {
                "changes": [
                    {
                        "op": "propose_belief",
                        "body": preference_body(claim),
                    }
                ]
            },
        },
    }


def final(text: str = "好的。") -> dict:
    return {"action": "final", "reply": text}


class ExpressionSidecarTests(DesktopCase):
    def document(self, script, *, sdrpe_mode="off"):
        document = config_document(self.db_path, script=script)
        document["budget"]["pcs_context_max_bytes"] = 65536
        document["cognition"] = {"enabled": True, "propose": True}
        document["conversation"] = {
            "history_max_turns": 8,
            "history_max_bytes": 4096,
            "tools_enabled": True,
            "sdrpe": {"mode": sdrpe_mode},
        }
        document["destination"]["model_protocol"] = "collab.v2"
        return document

    def start_chat(self, script, *, sdrpe_mode="off"):
        process = self.start(document=self.document(script, sdrpe_mode=sdrpe_mode))
        info = process.request("app.info")["result"]
        process.request(
            "app.confirm_destination",
            {
                "session_id": info["session_id"],
                "destination_id": info["destination"]["destination_id"],
            },
        )
        created = process.request(
            "conversation.create",
            {"session_id": info["session_id"], "mode": "chat"},
        )
        self.assertTrue(created["ok"], created)
        conversation_id = created["result"]["conversation"]["conversation_id"]
        return process, info, conversation_id

    def send(self, process, info, conversation_id, text):
        sent = process.request(
            "conversation.send",
            {
                "session_id": info["session_id"],
                "conversation_id": conversation_id,
                "client_request_id": f"rg05-f02-{time.time_ns()}",
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

    def test_explicit_preference_is_committed_without_a_review_item(self) -> None:
        process, info, conversation_id = self.start_chat(
            [propose_call(), final("记住了。")]
        )
        result = self.send(process, info, conversation_id, "记住：以后回答尽量简短。")
        self.assertEqual("completed", result["state"], result)
        rows = self.query(
            "SELECT decision, authority_kind, impact FROM cognition_review_decisions "
            "ORDER BY decision_seq"
        )
        self.assertEqual(
            [("committed", "user_directive", "E1")],
            [tuple(row) for row in rows],
        )
        self.assertEqual(
            0,
            self.scalar(
                "SELECT COUNT(*) FROM collab_artifacts "
                "WHERE kind = 'candidate' AND status = 'pending'"
            ),
        )
        self.assertGreaterEqual(
            self.scalar("SELECT COUNT(*) FROM records WHERE kind = 'belief'"), 1
        )

    def test_single_shot_leaves_no_durable_preference(self) -> None:
        process, info, conversation_id = self.start_chat([final("好，这次简短。")])
        result = self.send(process, info, conversation_id, "这次回答短一点。")
        self.assertEqual("completed", result["state"], result)
        self.assertEqual(
            0,
            self.scalar(
                "SELECT COUNT(*) FROM record_versions "
                "WHERE kind = 'expression_preference'"
            ),
        )
        self.assertEqual(
            0,
            self.scalar(
                "SELECT COUNT(*) FROM cognition_review_decisions"
            ),
        )
        self.assertEqual(0, self.scalar("SELECT COUNT(*) FROM collab_artifacts WHERE kind='candidate'"))

    def test_ordinary_expression_creates_no_preference_record(self) -> None:
        """J11: a passing remark must not become a stored expression rule."""

        process, info, conversation_id = self.start_chat(
            [final("嗯。"), final("好。")]
        )
        first = self.send(process, info, conversation_id, "我其实更喜欢简短一点的回答。")
        self.assertEqual("completed", first["state"], first)
        self.assertEqual(
            0,
            self.scalar(
                "SELECT COUNT(*) FROM record_versions "
                "WHERE kind = 'expression_preference'"
            ),
        )
        second = self.send(process, info, conversation_id, "那你说说看。")
        self.assertEqual("completed", second["state"], second)
        document = self.latest_document()
        self.assertNotIn("expression", document)

    # ------------------------------------------------------------- RG07

    def attempts(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM model_attempts"))

    def stages(self, turn_id: str):
        return self.query(
            "SELECT kind, status, skip_reason FROM collab_turn_stages "
            "WHERE turn_id = ? ORDER BY CASE kind WHEN 'content' THEN 0 ELSE 1 END",
            (turn_id,),
        )

    def test_on_mode_runs_two_stages_and_hands_the_commentary_down(self) -> None:
        script = [
            {
                "action": "final",
                "commentary": "我翻一下。",
                "reply": "记录里没有还挂着的待办。",
            },
            final("记录里没有还挂着的问题了。"),
        ]
        process, info, conversation_id = self.start_chat(script, sdrpe_mode="on")
        started = self.attempts()
        result = self.send(process, info, conversation_id, "记录里还有什么没解决的？")
        self.assertEqual("completed", result["state"], result)
        self.assertEqual(2, self.attempts() - started)
        self.assertEqual("记录里没有还挂着的问题了。", result["final"]["text"])
        rows = self.query(
            "SELECT stage_id, kind, status FROM collab_turn_stages "
            "ORDER BY CASE kind WHEN 'content' THEN 0 ELSE 1 END"
        )
        self.assertEqual(
            [("content", "succeeded"), ("expression", "succeeded")],
            [(row["kind"], row["status"]) for row in rows],
        )
        contexts = self.query(
            "SELECT sc.context_id, sc.purpose FROM collab_stage_contexts sc "
            "ORDER BY sc.ordinal"
        )
        self.assertEqual(
            ["content_initial", "expression"], [row["purpose"] for row in contexts]
        )
        ref = self.scalar(
            "SELECT rendered_input_payload_ref FROM context_snapshots "
            "WHERE context_id = ?",
            (contexts[-1]["context_id"],),
        )
        document = json.loads(
            json.loads(
                self.scalar(
                    "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
                    (ref,),
                )
            )["rendered_input"]
        )
        self.assertEqual(
            "记录里没有还挂着的待办。",
            document["content_handoff"]["public_draft"],
        )
        self.assertEqual(1, len(document["published_messages"]))
        self.assertEqual("我翻一下。", document["published_messages"][0]["text"])
        self.assertEqual(
            [document["published_messages"][0]["ref"]["artifact_id"]],
            document["content_handoff"]["already_public_message_refs"],
        )
        self.assertNotIn("tools", document)
        self.assertNotIn("cognition", document)
        self.assertLessEqual(
            len(json.dumps(document, ensure_ascii=False).encode("utf-8")), 65536
        )
        self.assertEqual(
            "on", info["capabilities"]["expression"]["effective_mode"]
        )

    def test_off_mode_stays_single_stage_and_reports_the_mode(self) -> None:
        process, info, conversation_id = self.start_chat(
            [final("单阶段回答。")], sdrpe_mode="off"
        )
        started = self.attempts()
        result = self.send(process, info, conversation_id, "普通问题。")
        self.assertEqual("completed", result["state"], result)
        self.assertEqual(1, self.attempts() - started)
        self.assertEqual(
            [("content", "succeeded")],
            [
                (row["kind"], row["status"])
                for row in self.query(
                    "SELECT kind, status FROM collab_turn_stages"
                )
            ],
        )
        self.assertEqual("off", info["capabilities"]["expression"]["mode"])
        self.assertEqual("off", info["capabilities"]["expression"]["effective_mode"])
        self.assertFalse(info["capabilities"]["expression"]["auto_available"])

    def test_auto_mode_reports_reserved_and_never_runs(self) -> None:
        process, info, conversation_id = self.start_chat(
            [final("自动模式不跑。")], sdrpe_mode="auto"
        )
        started = self.attempts()
        result = self.send(process, info, conversation_id, "你好。")
        self.assertEqual("completed", result["state"], result)
        self.assertEqual(1, self.attempts() - started)
        rows = self.stages(result["turn_id"])
        self.assertEqual(
            [("content", "succeeded"), ("expression", "skipped")],
            [(row["kind"], row["status"]) for row in rows],
        )
        self.assertEqual("auto_not_implemented", rows[1]["skip_reason"])
        caps = info["capabilities"]["expression"]
        self.assertEqual("auto", caps["mode"])
        self.assertEqual("off", caps["effective_mode"])
        self.assertFalse(caps["auto_available"])


    # ------------------------------------------- F02-RESTART-01 (desktop)

    def latest_document(self) -> dict:
        row = self.query(
            "SELECT rendered_input_payload_ref FROM context_snapshots "
            "ORDER BY created_at DESC LIMIT 1"
        )
        self.assertTrue(row)
        body = self.scalar(
            "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
            (row[0]["rendered_input_payload_ref"],),
        )
        return json.loads(json.loads(body)["rendered_input"])

    def new_conversation(self, process, info) -> str:
        created = process.request(
            "conversation.create",
            {"session_id": info["session_id"], "mode": "chat"},
        )
        self.assertTrue(created["ok"], created)
        return created["result"]["conversation"]["conversation_id"]

    def start_existing(self, script):
        process = self.start(document=self.document(script))
        info = process.request("app.info")["result"]
        process.request(
            "app.confirm_destination",
            {
                "session_id": info["session_id"],
                "destination_id": info["destination"]["destination_id"],
            },
        )
        return process, info

    def test_an_ordinary_remark_stays_out_of_the_record_store_after_a_restart(self) -> None:
        process, info, conversation_id = self.start_chat([final("嗯。")])
        first = self.send(process, info, conversation_id, "我其实更喜欢简短一点的回答。")
        self.assertEqual("completed", first["state"], first)
        self.assertEqual(
            0,
            self.scalar(
                "SELECT COUNT(*) FROM record_versions "
                "WHERE kind = 'expression_preference'"
            ),
        )
        process.close()
        process, info = self.start_existing([final("好。"), final("在。")])
        again = self.send(process, info, conversation_id, "那你说说看。")
        self.assertEqual("completed", again["state"], again)
        self.assertNotIn("expression", self.latest_document())
        other = self.new_conversation(process, info)
        elsewhere = self.send(process, info, other, "你好。")
        self.assertEqual("completed", elsewhere["state"], elsewhere)
        self.assertNotIn("expression", self.latest_document())

    def test_confirmed_and_revoked_read_back_after_a_restart(self) -> None:
        process, info, conversation_id = self.start_chat(
            [
                propose_call(),
                final("记住了。"),
                final("嗯。"),
                final("好。"),
            ]
        )
        explicit = self.send(process, info, conversation_id, "记住：以后回答尽量简短。")
        self.assertEqual("completed", explicit["state"], explicit)
        self.assertEqual(
            1,
            self.scalar(
                "SELECT COUNT(*) FROM cognition_review_decisions "
                "WHERE authority_kind = 'user_directive' AND decision = 'committed'"
            ),
        )
        self.assertEqual(
            0,
            self.scalar(
                "SELECT COUNT(*) FROM record_versions "
                "WHERE kind = 'expression_preference'"
            ),
        )
        process.close()

        process, info = self.start_existing([final("嗯。")])
        other = self.new_conversation(process, info)
        committed = self.send(process, info, other, "你记得我的偏好吗？")
        self.assertEqual("completed", committed["state"], committed)
        document = self.latest_document()
        confirmed = document["expression"]["confirmed"]
        self.assertEqual("concise", confirmed[0]["value"])
        self.assertNotIn("tentative", document["expression"])


if __name__ == "__main__":
    unittest.main()
