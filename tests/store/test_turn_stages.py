"""RG07: the expression stage lifecycle in the conversation store.

Every test uses the real store and the real Context service on a temporary
database.  No model, network or private content is involved.  The tests prove
that a skipped/started/failed stage is recorded honestly, that a stage can never
run twice, and that a terminal turn never leaves an open stage behind.
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

from quintessence_pcs.collaboration.consult import (  # noqa: E402
    _artifact_sources,
)
from quintessence_pcs.foundation.errors import InvalidInput  # noqa: E402
from quintessence_pcs.store.conversation import TurnStateConflict  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:rg07-stages"
ACCESS_REF = "authorization:rg07-stages"
POLICY_REF = "policy:collab.v1"
PRIVACY = "private"


class ExpressionStageStoreTests(CognitionTestCase):
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
        self._request = 0

    # ------------------------------------------------------------- helpers

    def build_turn(self, text: str = "你好。"):
        self._request += 1
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id=f"rg07-stage-{self._request}",
            text=text,
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": "chat", "renderer_version": "test"},
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

    def events(self, turn_id: str):
        page = self.store.read_turn_events(
            namespace=NS, host_instance=HOST, turn_id=turn_id
        )
        return [(row.kind, dict(row.payload)) for row in page.events]

    def publish_reply(
        self, creation, context, interaction_id, stage, *, finish_stage=True
    ):
        return self.store.publish_assistant_message(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            text="合成回答。",
            retention="standard",
            sources=_artifact_sources(context),
            attempt_ref=None,
            finish_stage=finish_stage,
        )

    # ---------------------------------------------------------------- tests

    def test_begin_expression_stage_is_started_and_unique(self) -> None:
        creation, _, _, content = self.build_turn()
        stage = self.store.begin_expression_stage(
            namespace=NS, turn_id=creation.turn.turn_id
        )
        self.assertEqual("expression", stage.kind)
        self.assertEqual("running", stage.status)
        self.assertEqual("sdrpe.v1", stage.policy_version)
        kinds = [kind for kind, _ in self.events(creation.turn.turn_id)]
        self.assertIn("stage_started", kinds)
        started = [
            payload
            for kind, payload in self.events(creation.turn.turn_id)
            if kind == "stage_started"
        ]
        self.assertEqual(["content", "expression"], [item["stage"] for item in started])
        with self.assertRaises(InvalidInput):
            self.store.begin_expression_stage(
                namespace=NS, turn_id=creation.turn.turn_id
            )

    def test_finish_stage_records_one_terminal_transition(self) -> None:
        creation, _, _, content = self.build_turn()
        expression = self.store.begin_expression_stage(
            namespace=NS, turn_id=creation.turn.turn_id
        )
        settled = self.store.finish_stage(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=expression.stage_id,
            status="succeeded",
        )
        self.assertEqual("succeeded", settled.status)
        self.assertIsNotNone(settled.completed_at)
        kinds = [kind for kind, _ in self.events(creation.turn.turn_id)]
        self.assertIn("stage_succeeded", kinds)
        with self.assertRaises(TurnStateConflict):
            self.store.finish_stage(
                namespace=NS,
                turn_id=creation.turn.turn_id,
                stage_id=expression.stage_id,
                status="failed",
            )
        self.assertEqual(
            ["content", "expression"],
            [item.kind for item in self.store.stages(namespace=NS, turn_id=creation.turn.turn_id)],
        )

    def test_skipped_stage_writes_one_row_and_never_a_started_event(self) -> None:
        creation, _, _, content = self.build_turn()
        skipped = self.store.skip_expression_stage(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            reason="mode_off",
        )
        self.assertEqual("skipped", skipped.status)
        self.assertEqual("mode_off", skipped.skip_reason)
        self.assertIsNotNone(skipped.completed_at)
        events = self.events(creation.turn.turn_id)
        self.assertNotIn(
            {"stage": "expression"},
            [payload for kind, payload in events if kind == "stage_started"],
        )
        skipped_events = [
            payload for kind, payload in events if kind == "stage_skipped"
        ]
        self.assertEqual(
            [{"stage": "expression", "reason": "mode_off"}], skipped_events
        )
        with self.assertRaises(InvalidInput):
            self.store.skip_expression_stage(
                namespace=NS, turn_id=creation.turn.turn_id, reason="again"
            )

    def test_finish_requires_a_reason_only_when_skipped(self) -> None:
        creation, _, _, content = self.build_turn()
        expression = self.store.begin_expression_stage(
            namespace=NS, turn_id=creation.turn.turn_id
        )
        with self.assertRaises(InvalidInput):
            self.store.finish_stage(
                namespace=NS,
                turn_id=creation.turn.turn_id,
                stage_id=expression.stage_id,
                status="skipped",
            )
        with self.assertRaises(InvalidInput):
            self.store.finish_stage(
                namespace=NS,
                turn_id=creation.turn.turn_id,
                stage_id=expression.stage_id,
                status="succeeded",
                skip_reason="not here",
            )
        with self.assertRaises(InvalidInput):
            self.store.finish_stage(
                namespace=NS,
                turn_id=creation.turn.turn_id,
                stage_id=expression.stage_id,
                status="cancelled",
            )

    def test_publish_can_keep_a_finished_content_stage(self) -> None:
        creation, context, interaction_id, content = self.build_turn()
        settled = self.store.finish_stage(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=content.stage_id,
            status="succeeded",
        )
        self.publish_reply(
            creation, context, interaction_id, content, finish_stage=False
        )
        stages = self.store.stages(namespace=NS, turn_id=creation.turn.turn_id)
        self.assertEqual(["content"], [stage.kind for stage in stages])
        self.assertEqual("succeeded", stages[0].status)
        self.assertEqual(settled.completed_at, stages[0].completed_at)
        self.assertEqual(
            "completed",
            self.store.turn_result(
                namespace=NS, host_instance=HOST,
                turn_id=creation.turn.turn_id, access=self.access,
            ).turn.state,
        )

    def test_cancel_settles_every_open_stage(self) -> None:
        creation, context, interaction_id, content = self.build_turn()
        expression = self.store.begin_expression_stage(
            namespace=NS, turn_id=creation.turn.turn_id
        )
        cancelled = self.store.cancel_turn(
            namespace=NS, turn_id=creation.turn.turn_id
        )
        self.assertEqual("cancelled", cancelled.state)
        statuses = {
            stage.kind: stage.status
            for stage in self.store.stages(namespace=NS, turn_id=creation.turn.turn_id)
        }
        self.assertEqual(
            {"content": "cancelled", "expression": "cancelled"}, statuses
        )

    def test_failed_turn_settles_the_other_stage(self) -> None:
        creation, context, interaction_id, content = self.build_turn()
        expression = self.store.begin_expression_stage(
            namespace=NS, turn_id=creation.turn.turn_id
        )
        failed = self.store.fail_turn(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            reason="expression:publish",
            stage_id=expression.stage_id,
            interaction_id=interaction_id,
        )
        self.assertEqual("failed", failed.state)
        statuses = {
            stage.kind: stage.status
            for stage in self.store.stages(namespace=NS, turn_id=creation.turn.turn_id)
        }
        self.assertEqual({"content": "failed", "expression": "failed"}, statuses)

    def test_restart_interrupt_settles_open_stages(self) -> None:
        creation, context, interaction_id, content = self.build_turn()
        self.store.begin_expression_stage(
            namespace=NS, turn_id=creation.turn.turn_id
        )
        self.assertEqual(1, self.store.interrupt_stale_turns(namespace=NS))
        statuses = {
            stage.kind: stage.status
            for stage in self.store.stages(namespace=NS, turn_id=creation.turn.turn_id)
        }
        self.assertEqual(
            {"content": "cancelled", "expression": "cancelled"}, statuses
        )
        kinds = [kind for kind, _ in self.events(creation.turn.turn_id)]
        self.assertIn("turn_interrupted", kinds)


if __name__ == "__main__":
    unittest.main()
