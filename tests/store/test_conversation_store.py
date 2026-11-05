"""RG02: conversation/turn storage, idempotency, transcript and history reads.

Every test uses an in-process temporary database with the real store and the
real Context service.  No model, network or private content is involved.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOMAIN = HERE.parent / "domain"
for _path in (HERE, DOMAIN):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.collaboration.consult import (  # noqa: E402
    _artifact_sources,
    _artifact_ref,
)
from quintessence_pcs.conversation.dto import turn_result_dict  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    ContextStale,
    InvalidInput,
    RecordNotFound,
)
from quintessence_pcs.store.conversation import (  # noqa: E402
    HistoryMessage,
    TurnStateConflict,
)
from quintessence_pcs.store.errors import ClientRequestConflict  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:rg02-store"
ACCESS_REF = "authorization:rg02-store"
POLICY_REF = "policy:collab.v1"
PRIVACY = "private"


class ConversationStoreTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.access = ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({PRIVACY}),
            purposes=frozenset({"consult"}),
        )
        self.profile_ref = self.profiles.get_active_profile(NS).ref.to_dict()

    # ------------------------------------------------------------- helpers

    def create(self, *, mode: str = "chat", host: str = HOST):
        return self.store.create_conversation(
            namespace=NS, host_instance=host, mode=mode
        )

    def input_ref(self, body: str) -> dict:
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body=body,
            privacy_class=PRIVACY,
            retention="standard",
        )
        return {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": row.content_version,
        }

    def register_turn(self, conversation, text, request_id=None):
        if request_id is None:
            request_id = f"req-{self.store.connection.execute('SELECT lower(hex(randomblob(8)))').fetchone()[0]}"
        return self.store.register_turn(
            namespace=NS,
            host_instance=conversation.host_instance,
            conversation_id=conversation.conversation_id,
            client_request_id=request_id,
            text=text,
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": conversation.mode, "renderer_version": "test"},
        )

    def build_context(self, conversation, text):
        """Create the input, turn, Context and interaction for one turn."""

        creation = self.register_turn(conversation, text)
        turn = creation.turn
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.access,
            input_refs=[creation.input_ref],
            adapter_kind="direct-http",
        )
        interaction_id = self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref=POLICY_REF,
            input_refs=[creation.input_ref],
            context_id=context.context_id,
        )
        stage = self.store.begin_turn(namespace=NS, turn_id=turn.turn_id)
        self.store.bind_stage_context(
            namespace=NS, stage_id=stage.stage_id,
            context_id=context.context_id,
        )
        return creation, context, interaction_id, stage

    def publish_final(self, conversation, text, reply, *, parts=None):
        creation, context, interaction_id, stage = self.build_context(
            conversation, text
        )
        turn = self.store.publish_assistant_message(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            text=reply,
            parts=parts,
            retention="standard",
            sources=_artifact_sources(context),
            attempt_ref={
                "authorization_id": "authorization:rg02",
                "attempt_id": "99999999-9999-4999-8999-999999999999",
                "attempt_no": 1,
                "request_hash": "sha256:" + "0" * 64,
            },
        )
        return creation, context, interaction_id, turn

    # ------------------------------------------------------- conversations

    def test_create_get_list_and_pagination(self) -> None:
        first = self.create()
        second = self.create(mode="work")
        self.assertEqual("chat", first.mode)
        self.assertEqual("work", second.mode)
        self.assertEqual(first, self.store.get_conversation(
            namespace=NS, host_instance=HOST,
            conversation_id=first.conversation_id,
        ))
        with self.assertRaises(RecordNotFound):
            self.store.get_conversation(
                namespace=NS, host_instance="host:other",
                conversation_id=first.conversation_id,
            )
        page = self.store.list_conversations(
            namespace=NS, host_instance=HOST, limit=1
        )
        self.assertEqual(1, len(page.conversations))
        self.assertIsNotNone(page.next_cursor)
        rest = self.store.list_conversations(
            namespace=NS, host_instance=HOST, limit=1, cursor=page.next_cursor
        )
        self.assertEqual(1, len(rest.conversations))
        self.assertIsNone(rest.next_cursor)
        self.assertEqual(
            {first.conversation_id, second.conversation_id},
            {page.conversations[0].conversation.conversation_id,
             rest.conversations[0].conversation.conversation_id},
        )
        self.assertEqual(
            (), self.store.list_conversations(
                namespace=NS, host_instance="host:other"
            ).conversations
        )

    def test_create_rejects_invalid_mode_and_duplicate_id(self) -> None:
        with self.assertRaises(Exception):
            self.store.create_conversation(
                namespace=NS, host_instance=HOST, mode="other"
            )
        first = self.create()
        with self.assertRaises(Exception):
            self.store.create_conversation(
                namespace=NS, host_instance=HOST,
                conversation_id=first.conversation_id,
            )

    # --------------------------------------------------------------- turns

    def test_register_turn_is_durable_and_idempotent(self) -> None:
        conversation = self.create()
        first = self.register_turn(conversation, "第一个合成问题", "req-a")
        self.assertFalse(first.reused)
        self.assertEqual(1, first.turn.turn_no)
        self.assertEqual("accepted", first.turn.state)
        self.assertEqual(self.profile_ref, first.turn.profile_ref)
        self.assertEqual("chat", first.turn.mode)
        self.assertEqual("test", first.turn.strategy["renderer_version"])
        self.assertEqual(1, first.turn.last_event_seq)
        messages = self.store.read_messages(
            namespace=NS, host_instance=HOST,
            conversation_id=conversation.conversation_id, access=self.access,
        )
        self.assertEqual(1, len(messages.messages))
        self.assertEqual("user", messages.messages[0].kind)
        self.assertEqual("第一个合成问题", messages.messages[0].text)

        replay = self.register_turn(conversation, "第一个合成问题", "req-a")
        self.assertTrue(replay.reused)
        self.assertEqual(first.turn.turn_id, replay.turn.turn_id)
        self.assertEqual(first.message_id, replay.message_id)
        self.assertEqual(
            1,
            self.store.connection.execute(
                "SELECT COUNT(*) FROM collab_inputs"
            ).fetchone()[0],
        )

        second = self.register_turn(conversation, "第二个合成问题", "req-b")
        self.assertEqual(2, second.turn.turn_no)
        with self.assertRaises(ClientRequestConflict):
            self.register_turn(conversation, "不同内容", "req-a")
        self.assertEqual(
            2,
            self.store.connection.execute(
                "SELECT COUNT(*) FROM collab_turns"
            ).fetchone()[0],
        )
        # Another conversation may reuse the same client request id.
        other = self.create()
        third = self.register_turn(other, "第一个合成问题", "req-a")
        self.assertFalse(third.reused)

    def test_register_turn_rejects_foreign_instance_and_profile(self) -> None:
        other = self.create(host="host:other")
        with self.assertRaises(RecordNotFound):
            self.store.register_turn(
                namespace=NS,
                host_instance=HOST,
                conversation_id=other.conversation_id,
                client_request_id="req-foreign",
                text="外部实例的问题",
                privacy_class=PRIVACY,
                retention="standard",
                profile_ref=self.profile_ref,
                strategy={},
            )
        conversation = self.create()
        with self.assertRaises(Exception):
            self.store.register_turn(
                namespace=NS,
                host_instance=HOST,
                conversation_id=conversation.conversation_id,
                client_request_id="req-x",
                text="问题",
                privacy_class=PRIVACY,
                retention="standard",
                profile_ref={
                    "ref_type": "record",
                    "namespace": "example.test.other",
                    "record_id": "11111111-1111-4111-8111-111111111111",
                    "record_version": 1,
                },
                strategy={},
            )

    def test_begin_turn_moves_accepted_to_running_once(self) -> None:
        conversation = self.create()
        creation = self.register_turn(conversation, "开始")
        stage = self.store.begin_turn(namespace=NS, turn_id=creation.turn.turn_id)
        self.assertEqual("content", stage.kind)
        self.assertEqual("running", stage.status)
        turn = self.store.read_turns(
            namespace=NS, host_instance=HOST,
            conversation_id=conversation.conversation_id,
        )[0].turn
        self.assertEqual("running", turn.state)
        self.assertIsNotNone(turn.started_at)
        with self.assertRaises(TurnStateConflict):
            self.store.begin_turn(namespace=NS, turn_id=creation.turn.turn_id)

    # ----------------------------------------------------------- publishing

    def test_publish_final_is_one_transaction(self) -> None:
        conversation = self.create()
        creation, context, interaction_id, turn = self.publish_final(
            conversation, "问题一", "合成回答一"
        )
        self.assertEqual("completed", turn.state)
        self.assertIsNotNone(turn.final_message_ref)
        self.assertIsNotNone(turn.completed_at)
        message = self.store.read_messages(
            namespace=NS, host_instance=HOST,
            conversation_id=conversation.conversation_id, access=self.access,
        ).messages
        self.assertEqual(["user", "final"], [item.kind for item in message])
        self.assertEqual("合成回答一", message[1].text)
        interaction = self.store.interaction(
            namespace=NS, interaction_id=interaction_id
        )
        self.assertEqual("answered", interaction.result)
        self.assertEqual(
            "reply",
            self.store.connection.execute(
                "SELECT kind FROM collab_artifacts WHERE artifact_id = ?",
                (
                    self.store.connection.execute(
                        "SELECT artifact_id FROM collab_turn_messages "
                        "WHERE turn_id = ? AND kind = 'final'",
                        (creation.turn.turn_id,),
                    ).fetchone()[0],
                ),
            ).fetchone()[0],
        )
        result = self.store.turn_result(
            namespace=NS, host_instance=HOST,
            turn_id=creation.turn.turn_id, access=self.access,
        )
        self.assertEqual("completed", result.turn.state)
        self.assertEqual("合成回答一", result.final.text)
        self.assertIsNone(result.wait_question)
        # Publishing again is refused: the turn is not running.
        stage_id = self.store.connection.execute(
            "SELECT stage_id FROM collab_turn_stages WHERE turn_id = ?",
            (creation.turn.turn_id,),
        ).fetchone()[0]
        with self.assertRaises(TurnStateConflict):
            self.store.publish_assistant_message(
                namespace=NS,
                turn_id=creation.turn.turn_id,
                stage_id=stage_id,
                interaction_id=interaction_id,
                context_id=context.context_id,
                text="第二次回答",
                retention="standard",
                sources=_artifact_sources(context),
                attempt_ref=None,
            )

    def test_wait_question_moves_turn_to_waiting(self) -> None:
        conversation = self.create()
        creation = self.register_turn(conversation, "需要澄清")
        context = self.contexts.build_consult_context(
            namespace=NS, access=self.access,
            input_refs=[creation.input_ref], adapter_kind="direct-http",
        )
        interaction_id = self.store.register_interaction(
            namespace=NS, mode="consult", activation_kind="user_input",
            host_instance=HOST, access_ref=ACCESS_REF, policy_ref=POLICY_REF,
            input_refs=[creation.input_ref], context_id=context.context_id,
        )
        stage = self.store.begin_turn(namespace=NS, turn_id=creation.turn.turn_id)
        self.store.bind_stage_context(
            namespace=NS, stage_id=stage.stage_id,
            context_id=context.context_id,
        )
        turn = self.store.publish_assistant_message(
            namespace=NS, turn_id=creation.turn.turn_id, stage_id=stage.stage_id,
            interaction_id=interaction_id, context_id=context.context_id,
            text="你指的是哪一个？", retention="standard",
            sources=_artifact_sources(context), attempt_ref=None,
            kind="wait_question",
        )
        self.assertEqual("waiting", turn.state)
        self.assertIsNone(turn.final_message_ref)
        self.assertIsNone(turn.completed_at)
        result = self.store.turn_result(
            namespace=NS, host_instance=HOST,
            turn_id=creation.turn.turn_id, access=self.access,
        )
        self.assertEqual("waiting", result.turn.state)
        self.assertEqual("你指的是哪一个？", result.wait_question.text)
        self.assertIsNone(result.final)

    def test_fail_turn_is_terminal_and_reports_reason(self) -> None:
        conversation = self.create()
        creation = self.register_turn(conversation, "会失败的问题")
        stage = self.store.begin_turn(namespace=NS, turn_id=creation.turn.turn_id)
        turn = self.store.fail_turn(
            namespace=NS, turn_id=creation.turn.turn_id,
            reason="model:MODEL_TIMEOUT", stage_id=stage.stage_id,
        )
        self.assertEqual("failed", turn.state)
        self.assertIsNotNone(turn.completed_at)
        result = self.store.turn_result(
            namespace=NS, host_instance=HOST,
            turn_id=creation.turn.turn_id, access=self.access,
        )
        self.assertEqual("model:MODEL_TIMEOUT", result.failure_reason)
        self.assertIsNone(result.final)
        # A second failure is idempotent and does not rewrite the reason.
        again = self.store.fail_turn(
            namespace=NS, turn_id=creation.turn.turn_id, reason="other"
        )
        self.assertEqual("failed", again.state)
        self.assertEqual(
            "model:MODEL_TIMEOUT",
            self.store.turn_result(
                namespace=NS, host_instance=HOST,
                turn_id=creation.turn.turn_id, access=self.access,
            ).failure_reason,
        )

    def test_interrupt_stale_turns_marks_and_keeps_messages(self) -> None:
        conversation = self.create()
        creation, context, interaction_id, stage = self.build_context(
            conversation, "中断前的问题"
        )
        count = self.store.interrupt_stale_turns(namespace=NS)
        self.assertEqual(1, count)
        turn = self.store.read_turns(
            namespace=NS, host_instance=HOST,
            conversation_id=conversation.conversation_id,
        )[0].turn
        self.assertEqual("interrupted", turn.state)
        self.assertIsNone(turn.final_message_ref)
        messages = self.store.read_messages(
            namespace=NS, host_instance=HOST,
            conversation_id=conversation.conversation_id, access=self.access,
        ).messages
        self.assertEqual(["user"], [item.kind for item in messages])
        interaction = self.store.interaction(
            namespace=NS, interaction_id=interaction_id
        )
        self.assertEqual("incomplete", interaction.result)
        # Idempotent: a second scan finds nothing.
        self.assertEqual(0, self.store.interrupt_stale_turns(namespace=NS))

    # ------------------------------------------------------------- history

    def _publish_three(self, conversation):
        for index in range(1, 4):
            self.publish_final(
                conversation, f"问题{index}", f"回答{index}"
            )

    def test_read_history_roles_order_and_refs(self) -> None:
        conversation = self.create()
        self._publish_three(conversation)
        bundle = self.store.read_history(
            access=self.access, namespace=NS,
            conversation_id=conversation.conversation_id,
            before_turn_no=4, max_turns=8, max_bytes=8192,
        )
        self.assertEqual(
            ["user", "assistant", "user", "assistant", "user", "assistant"],
            [item.role for item in bundle.messages],
        )
        self.assertEqual(
            ["问题1", "回答1", "问题2", "回答2", "问题3", "回答3"],
            [item.text for item in bundle.messages],
        )
        self.assertEqual([1, 1, 2, 2, 3, 3], [item.turn_no for item in bundle.messages])
        self.assertEqual(
            {"collab_input", "collab_artifact"},
            {item.ref_kind for item in bundle.messages},
        )
        self.assertEqual(3, bundle.stats["history_turns"])
        self.assertEqual(6, bundle.stats["history_messages"])
        self.assertEqual((), bundle.omissions)
        # A turn in progress is never part of its own history.
        empty = self.store.read_history(
            access=self.access, namespace=NS,
            conversation_id=conversation.conversation_id,
            before_turn_no=1, max_turns=8, max_bytes=8192,
        )
        self.assertEqual((), empty.messages)

    def test_read_history_trims_whole_old_turns(self) -> None:
        conversation = self.create()
        self._publish_three(conversation)
        bundle = self.store.read_history(
            access=self.access, namespace=NS,
            conversation_id=conversation.conversation_id,
            before_turn_no=4, max_turns=2, max_bytes=8192,
        )
        self.assertEqual([2, 2, 3, 3], [item.turn_no for item in bundle.messages])
        self.assertEqual(1, bundle.stats["dropped_turns"])
        reasons = {item["reason"] for item in bundle.omissions}
        self.assertIn("history_turn_limit", reasons)
    def test_read_history_marks_unfinished_turns(self) -> None:
        conversation = self.create()
        creation = self.register_turn(conversation, "失败的问题")
        self.store.begin_turn(namespace=NS, turn_id=creation.turn.turn_id)
        self.store.fail_turn(
            namespace=NS, turn_id=creation.turn.turn_id, reason="model:timeout"
        )
        self.publish_final(conversation, "正常问题", "正常回答")
        bundle = self.store.read_history(
            access=self.access, namespace=NS,
            conversation_id=conversation.conversation_id,
            before_turn_no=3, max_turns=8, max_bytes=8192,
        )
        self.assertEqual(
            ["user", "user", "assistant"], [item.role for item in bundle.messages]
        )
        self.assertIn(
            {"reason": "turn_unfinished", "turn_no": 1, "state": "failed"},
            [dict(item) for item in bundle.omissions],
        )

    # ---------------------------------------------------- context history

    def test_context_history_roundtrip_and_liveness(self) -> None:
        conversation = self.create()
        first, first_context, _, _ = self.publish_final(
            conversation, "历史问题", "历史回答"
        )
        second_creation = self.register_turn(conversation, "当前问题", "req-cur")
        current_context = self.contexts.build_consult_context(
            namespace=NS, access=self.access,
            input_refs=[second_creation.input_ref], adapter_kind="direct-http",
        )
        artifact_id = self.store.connection.execute(
            "SELECT artifact_id FROM collab_turn_messages WHERE turn_id = ? "
            "AND kind = 'final'",
            (first.turn.turn_id,),
        ).fetchone()[0]
        history = (
            HistoryMessage(
                role="user", text="历史问题", ref_kind="collab_input",
                ref_id=first.input_ref["input_id"], ref_version=1,
                turn_no=1, seq=1,
            ),
            HistoryMessage(
                role="assistant", text="历史回答", ref_kind="collab_artifact",
                ref_id=artifact_id, ref_version=1, turn_no=1, seq=2,
            ),
        )
        with self.store.transaction():
            count = self.store.register_context_history(
                namespace=NS, context_id=current_context.context_id,
                messages=history,
            )
        self.assertEqual(2, count)
        rows = self.store.context_history_rows(
            namespace=NS, context_id=current_context.context_id
        )
        self.assertEqual([1, 2], [row["ordinal"] for row in rows])
        self.store.require_context_history_live(
            namespace=NS, context_id=current_context.context_id,
            access=self.access,
        )
        # Deleting the historical input makes the Context stale.
        self.store.delete_experience(
            namespace=NS, ref_type="collab_input",
            ref_id=first.input_ref["input_id"], ref_version=1,
            authorization_ref="authorization:delete:rg02",
        )
        self.store.cleanup_deletions()
        with self.assertRaises(ContextStale):
            self.store.require_context_history_live(
                namespace=NS, context_id=current_context.context_id,
                access=self.access,
            )

    # --------------------------------------------------- RG08 reply bubbles

    def test_split_final_persists_one_ordered_copy_and_restarts(self) -> None:
        conversation = self.create()
        parts = ("第一段。", "第二段。")
        joined = "第一段。\n\n第二段。"
        creation, _, _, _ = self.publish_final(
            conversation, "分气泡问题", joined, parts=parts
        )
        turn_id = creation.turn.turn_id
        result = self.store.turn_result(
            namespace=NS, host_instance=HOST, turn_id=turn_id,
            access=self.access,
        )
        self.assertIsNotNone(result.final)
        self.assertEqual(joined, result.final.text)
        self.assertEqual(parts, result.final.messages)
        dto = turn_result_dict(result)
        self.assertEqual(list(parts), dto["final"]["messages"])
        self.assertEqual(joined, dto["final"]["text"])

        # One artifact body only: the ordered parts envelope, not joined text.
        row = self.store.connection.execute(
            "SELECT p.body_json FROM collab_turn_messages m "
            "JOIN collab_artifacts a ON a.namespace = m.namespace "
            "AND a.artifact_id = m.artifact_id "
            "AND a.artifact_version = m.artifact_version "
            "JOIN private_payloads p ON p.payload_ref = a.body_payload_ref "
            "WHERE m.namespace = ? AND m.turn_id = ? AND m.kind = 'final'",
            (NS, turn_id),
        ).fetchone()
        stored = json.loads(row["body_json"])["body"]
        self.assertEqual(1, stored.count("第一段。"))
        self.assertEqual(1, stored.count("第二段。"))
        self.assertNotIn(joined, stored)
        self.assertNotIn('"text"', stored)

        # Memory projection reads the ordered joined text, not the envelope.
        refs = self.store.turn_experience_refs(
            self.access, namespace=NS, turn_id=turn_id
        )
        artifact_ref = [
            item for item in refs if item["ref_type"] == "collab_artifact"
        ][0]
        view = self.store.read_experience(
            self.access, ref=artifact_ref, purpose="consult"
        )
        self.assertEqual(joined, view.body)
        self.assertNotIn("schema", view.body)

        # Restart and read back in the same order.
        restarted = self.open_second_store()
        reopened = restarted.turn_result(
            namespace=NS, host_instance=HOST, turn_id=turn_id,
            access=self.access,
        )
        self.assertIsNotNone(reopened.final)
        self.assertEqual(joined, reopened.final.text)
        self.assertEqual(parts, reopened.final.messages)
        page = restarted.read_messages(
            namespace=NS, host_instance=HOST,
            conversation_id=conversation.conversation_id, access=self.access,
        )
        finals = [item for item in page.messages if item.kind == "final"]
        self.assertEqual(parts, finals[0].messages)
        self.assertEqual(joined, finals[0].text)

    def test_legacy_single_final_keeps_the_plain_body(self) -> None:
        conversation = self.create()
        creation, _, _, _ = self.publish_final(
            conversation, "旧问题", "旧单条回答。"
        )
        turn_id = creation.turn.turn_id
        result = self.store.turn_result(
            namespace=NS, host_instance=HOST, turn_id=turn_id,
            access=self.access,
        )
        self.assertIsNotNone(result.final)
        self.assertEqual("旧单条回答。", result.final.text)
        self.assertEqual((), result.final.messages)
        self.assertNotIn(
            "messages", turn_result_dict(result)["final"]
        )
        row = self.store.connection.execute(
            "SELECT p.body_json FROM collab_turn_messages m "
            "JOIN collab_artifacts a ON a.namespace = m.namespace "
            "AND a.artifact_id = m.artifact_id "
            "AND a.artifact_version = m.artifact_version "
            "JOIN private_payloads p ON p.payload_ref = a.body_payload_ref "
            "WHERE m.namespace = ? AND m.turn_id = ? AND m.kind = 'final'",
            (NS, turn_id),
        ).fetchone()
        self.assertEqual("旧单条回答。", json.loads(row["body_json"])["body"])

    def test_split_final_requires_parts_to_join_the_text(self) -> None:
        conversation = self.create()
        creation, context, interaction_id, stage = self.build_context(
            conversation, "不一致的问题"
        )
        with self.assertRaises(InvalidInput):
            self.store.publish_assistant_message(
                namespace=NS,
                turn_id=creation.turn.turn_id,
                stage_id=stage.stage_id,
                interaction_id=interaction_id,
                context_id=context.context_id,
                text="另一段。",
                parts=("第一段。", "第二段。"),
                retention="standard",
                sources=_artifact_sources(context),
                attempt_ref=None,
            )
        self.assertEqual(
            "running",
            self.store.connection.execute(
                "SELECT state FROM collab_turns WHERE namespace = ? "
                "AND turn_id = ?",
                (NS, creation.turn.turn_id),
            ).fetchone()[0],
        )


if __name__ == "__main__":
    unittest.main()
