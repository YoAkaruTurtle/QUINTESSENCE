"""RG04 04.5: the transcript-search result obeys the deletion barrier."""

from __future__ import annotations

import json
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
from quintessence_pcs.conversation.tools import (  # noqa: E402
    collect_exact_refs,
    merge_tool_sources,
)
from quintessence_pcs.domains.memory.postprocess import (  # noqa: E402
    turn_postprocess_status,
)
from quintessence_pcs.foundation.errors import Redacted  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:rg04-deletion"
ACCESS_REF = "authorization:rg04-deletion"
POLICY_REF = "policy:collab.v1"
PRIVACY = "private"


class TranscriptDeletionTests(CognitionTestCase):
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

    def _running_turn(self, text: str):
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
        interaction = self.store.register_interaction(
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
        return creation, context, interaction, stage

    def test_search_result_artifact_is_cleared_after_source_deletion(self) -> None:
        creation, context, interaction, stage = self._running_turn(
            "读书会安排：周五"
        )
        page = self.store.search_transcript(
            namespace=NS,
            host_instance=HOST,
            access=self.access,
            query="读书会",
        )
        self.assertEqual(1, len(page.items))
        read_refs = collect_exact_refs(page.as_dict(), NS)
        self.assertEqual({"collab_input"}, {ref["ref_type"] for ref in read_refs})

        step = self.store.plan_tool_step(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            step_no=1,
            tool_name="conversation.search",
            arguments_digest="b" * 64,
            operation_key="tool:1",
        )
        step = self.store.start_tool_step(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            step_id=step.step_id,
        )
        body = json.dumps(
            {
                "tool": "conversation.search",
                "arguments": {"query": "读书会"},
                "status": "succeeded",
                "result": page.as_dict(),
            },
            ensure_ascii=False,
        )
        step = self.store.finish_tool_step(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            step_id=step.step_id,
            interaction_id=interaction,
            context_id=context.context_id,
            status="succeeded",
            duration_ms=1,
            body=body,
            sources=merge_tool_sources(_artifact_sources(context), read_refs),
            retention="standard",
        )
        self.store.publish_assistant_message(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction,
            context_id=context.context_id,
            text="读书会定在周五。",
            retention="standard",
            sources=_artifact_sources(context),
            attempt_ref=None,
        )
        artifact_id = step.result_artifact_id
        self.assertIsNotNone(artifact_id)
        loaded = self.store.load_artifact(
            namespace=NS, artifact_id=artifact_id, artifact_version=1
        )
        self.assertIn("读书会", loaded.body)

        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=creation.input_ref["input_id"],
            ref_version=1,
            authorization_ref="authorization:rg04-delete",
        )

        with self.assertRaises(Redacted):
            self.store.load_artifact(
                namespace=NS, artifact_id=artifact_id, artifact_version=1
            )
        steps = self.store.read_turn_steps(
            namespace=NS,
            host_instance=HOST,
            turn_id=creation.turn.turn_id,
            access=self.access,
        )
        self.assertEqual(1, len(steps.steps))
        self.assertFalse(steps.steps[0].result_available)
        self.assertIsNone(steps.steps[0].result_excerpt)

        after = self.store.search_transcript(
            namespace=NS,
            host_instance=HOST,
            access=self.access,
            query="读书会",
        )
        self.assertEqual(0, len(after.items))
        self.assertGreaterEqual(after.filtered, 1)

    def test_postprocess_status_never_carries_body_after_deletion(self) -> None:
        creation, context, interaction, stage = self._running_turn("删除状态")
        final = self.store.publish_assistant_message(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction,
            context_id=context.context_id,
            text="删除状态回答",
            retention="standard",
            sources=_artifact_sources(context),
            attempt_ref=None,
        )
        final_artifact_id = self.store.connection.execute(
            "SELECT artifact_id FROM collab_turn_messages WHERE turn_id = ? "
            "AND kind = 'final'",
            (final.turn_id,),
        ).fetchone()[0]
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_artifact",
            ref_id=final_artifact_id,
            ref_version=1,
            authorization_ref="authorization:rg04-delete-final",
        )
        status = turn_postprocess_status(
            self.store,
            namespace=NS,
            host_instance=HOST,
            turn_id=final.turn_id,
            access=self.access,
            configured=True,
        )
        payload = json.dumps(status.as_dict(), ensure_ascii=False)
        self.assertNotIn("删除状态回答", payload)
        self.assertNotIn("删除状态", payload)
        self.assertTrue(status.transcript_saved)
        self.assertGreaterEqual(len(status.refs), 1)
        for item in status.refs:
            self.assertIn("ref", item.as_dict())
            self.assertNotIn("text", item.as_dict())


if __name__ == "__main__":
    unittest.main()
