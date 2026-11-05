"""RG04 04.4: bounded transcript search over v2 turns and legacy records."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOMAIN = HERE.parent / "domain"
SRC = HERE.parents[1] / "src"
for _path in (HERE, DOMAIN, SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import CognitionTestCase, NS  # noqa: E402

from quintessence_pcs.collaboration.consult import _artifact_sources  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:rg04-search"
ACCESS_REF = "authorization:rg04-search"
POLICY_REF = "policy:collab.v1"
PRIVACY = "private"


class TranscriptSearchTests(CognitionTestCase):
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
        self.conversation = self.store.create_conversation(
            namespace=NS, host_instance=HOST, mode="chat"
        )

    def _start_turn(self, text: str, *, privacy: str = PRIVACY):
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id=f"req-{text}",
            text=text,
            privacy_class=privacy,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": "chat"},
        )
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
        stage = self.store.begin_turn(namespace=NS, turn_id=creation.turn.turn_id)
        self.store.bind_stage_context(
            namespace=NS,
            stage_id=stage.stage_id,
            context_id=context.context_id,
        )
        return creation, context, interaction_id, stage

    def _publish(
        self,
        text: str,
        reply: str,
        *,
        privacy: str = PRIVACY,
        commentary: str | None = None,
    ):
        creation, context, interaction_id, stage = self._start_turn(
            text, privacy=privacy
        )
        sources = _artifact_sources(context)
        if commentary is not None:
            message = self.store.publish_commentary(
                namespace=NS,
                turn_id=creation.turn.turn_id,
                stage_id=stage.stage_id,
                interaction_id=interaction_id,
                context_id=context.context_id,
                text=commentary,
                retention="standard",
                sources=sources,
            )
            sources = sources + [
                {
                    "ref_type": "collab_artifact",
                    "namespace": NS,
                    "artifact_id": message.artifact_id,
                    "artifact_version": message.artifact_version,
                }
            ]
        turn = self.store.publish_assistant_message(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            text=reply,
            retention="standard",
            sources=sources,
            attempt_ref=None,
        )
        return creation, turn

    def _legacy(self, text: str = "旧单轮问题", reply: str = "旧单轮回答"):
        input_row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body=text,
            privacy_class=PRIVACY,
            retention="standard",
        )
        interaction = self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref=POLICY_REF,
            input_refs=[
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": input_row.input_id,
                    "content_version": input_row.content_version,
                }
            ],
            result="answered",
        )
        context_id = self.store.capture_snapshot(NS).context_id
        artifact = self.store.register_artifact(
            namespace=NS,
            kind="reply",
            interaction_id=interaction,
            context_id=context_id,
            origin_kind="model_output",
            body=reply,
            retention="standard",
        )
        return input_row, artifact

    def _search(self, query: str, **kwargs):
        return self.store.search_transcript(
            namespace=NS,
            host_instance=HOST,
            access=self.access,
            query=query,
            **kwargs,
        )

    def test_search_finds_v2_user_and_final_with_source_refs(self) -> None:
        self._publish("读书会安排第一版", "读书会第二版更短一些")
        page = self._search("读书会")
        self.assertEqual(2, page.scanned)
        self.assertEqual(2, len(page.items))
        roles = {item["role"] for item in page.items}
        self.assertEqual({"user", "assistant"}, roles)
        for item in page.items:
            self.assertIn(item["ref"]["ref_type"], ("collab_input", "collab_artifact"))
            self.assertIsNotNone(item["conversation_id"])
            self.assertIsInstance(item["turn_no"], int)
            self.assertTrue(item["created_at"].endswith("Z"))
        final = [item for item in page.items if item["role"] == "assistant"][0]
        self.assertEqual("final", final["kind"])

    def test_search_finds_legacy_records_without_inventing_a_conversation(self) -> None:
        input_row, artifact = self._legacy("旧单轮问题：读书会", "旧单轮回答：读书会")
        page = self._search("读书会")
        self.assertEqual(2, len(page.items))
        for item in page.items:
            self.assertEqual("legacy", item["source"])
            self.assertIsNone(item["conversation_id"])
            self.assertIsNone(item["turn_no"])
            self.assertIsNotNone(item["legacy_interaction_id"])
        refs = {item["ref"]["ref_type"] for item in page.items}
        self.assertEqual({"collab_input", "collab_artifact"}, refs)
        self.assertIn(
            artifact.artifact_id,
            {
                item["ref"].get("artifact_id")
                for item in page.items
                if item["ref"]["ref_type"] == "collab_artifact"
            },
        )
        self.assertIn(
            input_row.input_id,
            {
                item["ref"].get("input_id")
                for item in page.items
                if item["ref"]["ref_type"] == "collab_input"
            },
        )

    def test_deleted_message_is_not_returned(self) -> None:
        creation, turn = self._publish("会被删除的问题", "会被删除的回答")
        final_id = self.store.connection.execute(
            "SELECT artifact_id FROM collab_turn_messages WHERE turn_id = ? "
            "AND kind = 'final'",
            (turn.turn_id,),
        ).fetchone()[0]
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_artifact",
            ref_id=final_id,
            ref_version=1,
            authorization_ref="authorization:test-delete",
        )
        page = self._search("会被删除")
        self.assertEqual(1, len(page.items))
        self.assertEqual("user", page.items[0]["role"])
        self.assertGreaterEqual(page.filtered, 1)

    def test_unauthorized_privacy_rows_are_filtered(self) -> None:
        restricted = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="私密问题：读书会",
            privacy_class="restricted",
            retention="standard",
        )
        interaction = self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref=POLICY_REF,
            input_refs=[
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": restricted.input_id,
                    "content_version": restricted.content_version,
                }
            ],
            result="answered",
        )
        context_id = self.store.capture_snapshot(NS).context_id
        self.store.register_artifact(
            namespace=NS,
            kind="reply",
            interaction_id=interaction,
            context_id=context_id,
            origin_kind="model_output",
            body="私密回答：读书会",
            retention="standard",
            sources=[
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": restricted.input_id,
                    "content_version": restricted.content_version,
                }
            ],
        )
        page = self._search("私密")
        self.assertEqual(0, len(page.items))
        self.assertGreaterEqual(page.filtered, 2)

    def test_retention_shorter_than_host_is_filtered(self) -> None:
        short = self.store.register_input(
            namespace=NS,
            origin_kind="host_supplied",
            body="会话级材料：读书会",
            privacy_class=PRIVACY,
            retention="session",
        )
        self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref=POLICY_REF,
            input_refs=[
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": short.input_id,
                    "content_version": short.content_version,
                }
            ],
            result="answered",
        )
        page = self._search("读书会", minimum_retention="standard")
        self.assertEqual(0, len(page.items))
        self.assertGreaterEqual(page.filtered, 1)

    def test_cursor_pages_do_not_duplicate(self) -> None:
        self._publish("甲问题", "甲回答")
        self._publish("乙问题", "乙回答")
        first = self._search("回答", limit=1)
        self.assertEqual(1, len(first.items))
        self.assertIsNotNone(first.next_cursor)
        second = self._search("回答", limit=1, before=first.next_cursor)
        self.assertEqual(1, len(second.items))
        self.assertNotEqual(first.items[0]["ref"], second.items[0]["ref"])
        third = self._search("回答", limit=1, before=second.next_cursor)
        self.assertEqual(0, len(third.items))
        self.assertIsNone(third.next_cursor)

    def test_empty_result_is_honest(self) -> None:
        self._publish("只谈天气", "今天晴")
        page = self._search("完全不存在的词")
        self.assertEqual(0, len(page.items))
        self.assertGreater(page.scanned, 0)
        self.assertIsNone(page.next_cursor)

    def test_commentary_is_searchable_but_tool_results_are_not_transcript(self) -> None:
        self._publish("最初问题", "最终回答", commentary="我翻一下旧记录。")
        page = self._search("旧记录")
        self.assertEqual(1, len(page.items))
        self.assertEqual("commentary", page.items[0]["kind"])


if __name__ == "__main__":
    unittest.main()
