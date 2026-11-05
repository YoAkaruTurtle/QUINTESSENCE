"""RG02 M08: history deletion cascade and the projection guard.

The tests use the real store, real Contexts and synthetic bodies only.  They
prove three things:

* deleting a historical message clears every later Context that rendered it
  and every model_output artifact those Contexts produced, to a fixed point;
* a Context whose history was cleared is not deliverable/sendable;
* commentary and tool-result artifacts are not enumerated as independent
  experiences, while the user input and the final reply still are.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOMAIN = HERE.parent / "domain"
for _path in (HERE, DOMAIN):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.collaboration.consult import _artifact_sources  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    ContextStale,
    RecordNotFound,
    Redacted,
)
from quintessence_pcs.store.conversation import HistoryMessage  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:rg02-deletion"
ACCESS_REF = "authorization:rg02-deletion"
POLICY_REF = "policy:collab.v1"
PRIVACY = "private"


class ConversationDeletionTests(CognitionTestCase):
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

    # ------------------------------------------------------------- helpers

    def _turn(self, text: str):
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id=f"req-{text}",
            text=text,
            privacy_class=PRIVACY,
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
            namespace=NS, stage_id=stage.stage_id,
            context_id=context.context_id,
        )
        return creation, context, interaction_id, stage

    def _publish(self, text: str, reply: str, history=()):
        creation, context, interaction_id, stage = self._turn(text)
        if history:
            with self.store.transaction():
                self.store.register_context_history(
                    namespace=NS,
                    context_id=context.context_id,
                    messages=history,
                )
        turn = self.store.publish_assistant_message(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            text=reply,
            retention="standard",
            sources=_artifact_sources(context),
            attempt_ref=None,
        )
        artifact_id = self.store.connection.execute(
            "SELECT artifact_id FROM collab_turn_messages WHERE turn_id = ? "
            "AND kind = 'final'",
            (turn.turn_id,),
        ).fetchone()[0]
        return creation, context, turn, artifact_id

    def _payload(self, context_id: str) -> str:
        return self.store.connection.execute(
            "SELECT p.body_json FROM context_snapshots c JOIN private_payloads p "
            "ON p.payload_ref = c.rendered_input_payload_ref "
            "WHERE c.context_id = ?",
            (context_id,),
        ).fetchone()[0]

    def _artifact_payload(self, artifact_id: str) -> str:
        return self.store.connection.execute(
            "SELECT p.body_json FROM collab_artifacts a JOIN private_payloads p "
            "ON p.payload_ref = a.body_payload_ref WHERE a.artifact_id = ?",
            (artifact_id,),
        ).fetchone()[0]

    def _delete(self, ref_type: str, ref_id: str, version: int) -> None:
        self.store.delete_experience(
            namespace=NS,
            ref_type=ref_type,
            ref_id=ref_id,
            ref_version=version,
            authorization_ref="authorization:delete:rg02",
        )
        self.store.cleanup_deletions()

    # --------------------------------------------------------------- M08

    def test_history_deletion_cascade_reaches_a_fixed_point(self) -> None:
        first, c1, t1, r1 = self._publish("问题一", "回答一")
        second, c2, t2, r2 = self._publish("问题二", "回答二")
        history_one = (
            HistoryMessage(
                role="user", text="问题一", ref_kind="collab_input",
                ref_id=first.input_ref["input_id"], ref_version=1,
                turn_no=1, seq=1,
            ),
            HistoryMessage(
                role="assistant", text="回答一", ref_kind="collab_artifact",
                ref_id=r1, ref_version=1, turn_no=1, seq=2,
            ),
        )
        third, c3, t3, r3 = self._publish("问题三", "回答三", history_one)
        history_three = (
            HistoryMessage(
                role="assistant", text="回答三", ref_kind="collab_artifact",
                ref_id=r3, ref_version=1, turn_no=3, seq=2,
            ),
        )
        fourth, c4, t4, r4 = self._publish("问题四", "回答四", history_three)

        # Before deletion everything is readable.
        self.assertTrue(self._payload(c1.context_id))
        self.assertTrue(self._payload(c3.context_id))
        self.assertTrue(self._payload(c4.context_id))
        self.assertFalse(
            self.store.context_barred(namespace=NS, context_id=c4.context_id)
        )

        self._delete("collab_input", first.input_ref["input_id"], 1)

        # The deleted input's own Context/reply are cleared by the existing
        # material closure; later Contexts and their replies by the new cascade.
        for context_id in (c1.context_id, c3.context_id, c4.context_id):
            self.assertFalse(self._payload(context_id), context_id)
            with self.assertRaises(Redacted):
                self.store.load_context_snapshot(context_id)
        for artifact_id in (r1, r3, r4):
            self.assertFalse(self._artifact_payload(artifact_id), artifact_id)
            with self.assertRaises(Redacted):
                self.store.load_artifact(
                    namespace=NS, artifact_id=artifact_id,
                    artifact_version=1,
                )
        # Turn two did not render the deleted message: it stays intact.
        self.assertTrue(self._payload(c2.context_id))
        self.assertTrue(self._artifact_payload(r2))
        # The history-bearing Contexts are barred and not sendable.
        for context_id in (c3.context_id, c4.context_id):
            self.assertTrue(
                self.store.context_barred(namespace=NS, context_id=context_id)
            )
            with self.assertRaises((Redacted, ContextStale)):
                self.store.require_context_deliverable(context_id)
            with self.assertRaises(ContextStale):
                self.store.require_context_history_live(
                    namespace=NS, context_id=context_id, access=self.access
                )
        # The public transcript of the cleared turns reports redaction, not text.
        messages = self.store.read_messages(
            namespace=NS, host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            access=self.access, limit=200,
        ).messages
        by_turn = {}
        for message in messages:
            by_turn.setdefault(message.turn_id, []).append(message)
        for turn in (t1.turn_id, t3.turn_id, t4.turn_id):
            self.assertTrue(any(item.redacted for item in by_turn[turn]), turn)

    def test_deleting_a_reply_artifact_cascades_through_history(self) -> None:
        first, c1, t1, r1 = self._publish("问题一", "回答一")
        history = (
            HistoryMessage(
                role="assistant", text="回答一", ref_kind="collab_artifact",
                ref_id=r1, ref_version=1, turn_no=1, seq=2,
            ),
        )
        second, c2, t2, r2 = self._publish("问题二", "回答二", history)
        self._delete("collab_artifact", r1, 1)
        self.assertFalse(self._artifact_payload(r1))
        self.assertFalse(self._payload(c2.context_id))
        self.assertFalse(self._artifact_payload(r2))

    def test_history_absent_database_keeps_old_deletion_behaviour(self) -> None:
        # No row ever registered a history link: the new cascade is a no-op and
        # the old material closure still clears exactly its own derived copies.
        first, c1, t1, r1 = self._publish("问题一", "回答一")
        self._delete("collab_input", first.input_ref["input_id"], 1)
        self.assertFalse(self._payload(c1.context_id))
        self.assertFalse(self._artifact_payload(r1))
        self.assertEqual(
            0,
            len(
                self.store.connection.execute(
                    "SELECT * FROM collab_context_history"
                ).fetchall()
            ),
        )

    # --------------------------------------------------- projection guard

    def test_projection_guard_excludes_commentary_and_tool_results(self) -> None:
        creation, context, turn, final_artifact = self._publish(
            "投影问题", "最终回答"
        )
        interaction_id = self.store.connection.execute(
            "SELECT interaction_id FROM collab_artifacts WHERE artifact_id = ?",
            (final_artifact,),
        ).fetchone()[0]
        sources = _artifact_sources(context)
        # A commentary artifact with a published commentary message.
        commentary = self.store.register_artifact(
            namespace=NS,
            kind="reply",
            interaction_id=interaction_id,
            context_id=context.context_id,
            origin_kind="model_output",
            body="我去翻一下记录。",
            retention="standard",
            sources=sources,
        )
        self.store.connection.execute(
            "INSERT INTO collab_turn_messages (message_id, namespace, turn_id, "
            "seq, kind, input_ref_json, artifact_id, artifact_version, "
            "attempt_ref_json, status, published_at, created_at) VALUES "
            "(?, ?, ?, 3, 'commentary', NULL, ?, 1, NULL, 'published', ?, ?)",
            (
                "abababab-1111-4111-8111-abababababab",
                NS,
                turn.turn_id,
                commentary.artifact_id,
                "2026-10-28T00:00:00Z",
                "2026-10-28T00:00:00Z",
            ),
        )
        # A deterministic tool result referenced by a step row.
        tool = self.store.register_artifact(
            namespace=NS,
            kind="query_result",
            interaction_id=interaction_id,
            context_id=context.context_id,
            origin_kind="deterministic_query",
            body="工具结果正文",
            retention="standard",
            sources=sources,
        )
        stage_id = self.store.connection.execute(
            "SELECT stage_id FROM collab_turn_stages WHERE turn_id = ?",
            (turn.turn_id,),
        ).fetchone()[0]
        self.store.connection.execute(
            "INSERT INTO collab_turn_steps (step_id, namespace, turn_id, "
            "stage_id, step_no, tool_name, arguments_payload_ref, "
            "arguments_digest, result_artifact_id, result_artifact_version, "
            "result_digest, operation_key, status, started_at, finished_at, "
            "error_label) VALUES (?, ?, ?, ?, 1, 'conversation.search', NULL, "
            "NULL, ?, 1, NULL, 'op-1', 'succeeded', ?, ?, NULL)",
            (
                "cdcdcdcd-2222-4222-8222-cdcdcdcdcdcd",
                NS,
                turn.turn_id,
                stage_id,
                tool.artifact_id,
                "2026-10-28T00:00:00Z",
                "2026-10-28T00:00:00Z",
            ),
        )
        page = self.store.list_experience_refs(
            self.access, namespace=NS, limit=100
        )
        ids = {
            ref["input_id"] if ref["ref_type"] == "collab_input"
            else ref["artifact_id"]
            for ref in page.refs
        }
        self.assertIn(creation.input_ref["input_id"], ids)
        self.assertIn(final_artifact, ids)
        self.assertNotIn(commentary.artifact_id, ids)
        self.assertNotIn(tool.artifact_id, ids)


if __name__ == "__main__":
    unittest.main()
