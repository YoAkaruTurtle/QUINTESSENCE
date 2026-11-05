"""RG03: tool steps, tool-result artifacts, commentary, events and cancel.

Every test uses a temporary database with the real store and the real Context
service.  No model, network or private content is involved.
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

from quintessence_pcs.collaboration.generation import artifact_sources  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    CorruptStore,
    RecordNotFound,
    Redacted,
)
from quintessence_pcs.store.conversation import (  # noqa: E402
    MAX_STEP_DETAIL_CHARS,
    TurnStateConflict,
)
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:rg03-store"
OTHER_HOST = "host:rg03-store-other"
ACCESS_REF = "authorization:rg03-store"
POLICY_REF = "policy:collab.v1"
PRIVACY = "private"


class TurnStepStoreTests(CognitionTestCase):
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
        self.events = []

    # ------------------------------------------------------------- helpers

    def sink(self, row, live):
        self.events.append((row, live))

    def build_turn(self, *, text: str = "合成提问。"):
        conversation = self.store.create_conversation(
            namespace=NS, host_instance=HOST, mode="chat"
        )
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=conversation.conversation_id,
            client_request_id="req-rg03-store",
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
            namespace=NS, stage_id=stage.stage_id, context_id=context.context_id
        )
        return conversation, creation, context, interaction_id, stage

    def plan(self, creation, stage, *, step_no=1, tool="cognition.belief.read"):
        return self.store.plan_tool_step(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            step_no=step_no,
            tool_name=tool,
            arguments_digest="a" * 64,
            operation_key=f"tool:{step_no}",
            live_payload={"arguments": {"limit": 3}},
            on_event=self.sink,
        )

    def body(self, *, status="succeeded", result=None, arguments=None):
        from quintessence_pcs.foundation.jsonutil import canonical_json_text

        return canonical_json_text(
            {
                "tool": "cognition.belief.read",
                "arguments": {"limit": 3} if arguments is None else arguments,
                "status": status,
                "result": {"items": []} if result is None else result,
            }
        )

    # --------------------------------------------------------------- steps

    def test_plan_start_finish_and_read_back(self) -> None:
        _conversation, creation, context, interaction_id, stage = self.build_turn()
        self.events.clear()
        step = self.plan(creation, stage)
        self.assertEqual("planned", step.status)
        self.assertEqual(1, step.step_no)
        self.assertEqual("tool_planned", self.events[-1][0].kind)

        step = self.store.start_tool_step(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            step_id=step.step_id,
            on_event=self.sink,
        )
        self.assertEqual("running", step.status)
        self.assertEqual("tool_started", self.events[-1][0].kind)

        body = self.body()
        step = self.store.finish_tool_step(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            step_id=step.step_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            status="succeeded",
            duration_ms=12,
            body=body,
            sources=artifact_sources(context),
            retention="standard",
            live_payload={"result_excerpt": "{\"items\":[]}"},
            on_event=self.sink,
        )
        self.assertEqual("succeeded", step.status)
        self.assertIsNotNone(step.result_artifact_id)
        self.assertIsNotNone(step.result_artifact_version)
        self.assertTrue(step.result_digest.startswith("sha256:"))
        finished = self.events[-1]
        self.assertEqual("tool_finished", finished[0].kind)
        self.assertEqual("{\"items\":[]}", finished[1]["result_excerpt"])

        page = self.store.read_turn_steps(
            namespace=NS, host_instance=HOST, turn_id=creation.turn.turn_id,
            access=self.access,
        )
        self.assertEqual(1, len(page.steps))
        view = page.steps[0]
        self.assertEqual(step.step_id, view.step.step_id)
        self.assertTrue(view.result_available)
        self.assertEqual({"limit": 3}, view.arguments)
        self.assertIn("items", view.result_excerpt)
        self.assertFalse(view.result_truncated)

        events = self.store.read_turn_events(
            namespace=NS, host_instance=HOST, turn_id=creation.turn.turn_id
        )
        kinds = [event.kind for event in events.events]
        self.assertEqual(
            ["turn_accepted", "stage_started", "tool_planned", "tool_started", "tool_finished"],
            kinds,
        )
        self.assertFalse(events.terminal)
        self.assertEqual(5, events.next_seq)

    def test_reads_are_scoped_to_the_host(self) -> None:
        _conversation, creation, _context, _interaction, _stage = self.build_turn()
        with self.assertRaises(RecordNotFound):
            self.store.read_turn_events(
                namespace=NS, host_instance=OTHER_HOST, turn_id=creation.turn.turn_id
            )
        with self.assertRaises(RecordNotFound):
            self.store.read_turn_steps(
                namespace=NS, host_instance=OTHER_HOST,
                turn_id=creation.turn.turn_id, access=self.access,
            )

    def test_step_must_be_running_to_finish(self) -> None:
        _conversation, creation, context, interaction_id, stage = self.build_turn()
        step = self.plan(creation, stage)
        with self.assertRaises(TurnStateConflict):
            self.store.finish_tool_step(
                namespace=NS,
                turn_id=creation.turn.turn_id,
                step_id=step.step_id,
                interaction_id=interaction_id,
                context_id=context.context_id,
                status="succeeded",
                duration_ms=1,
                body=self.body(),
                sources=artifact_sources(context),
            )

    def test_outcome_unknown_has_no_artifact(self) -> None:
        _conversation, creation, context, interaction_id, stage = self.build_turn()
        step = self.store.start_tool_step(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            step_id=self.plan(creation, stage).step_id,
        )
        step = self.store.finish_tool_step(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            step_id=step.step_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            status="outcome_unknown",
            duration_ms=30_000,
            error_label="timeout",
        )
        self.assertEqual("outcome_unknown", step.status)
        self.assertIsNone(step.result_artifact_id)
        page = self.store.read_turn_steps(
            namespace=NS, host_instance=HOST, turn_id=creation.turn.turn_id,
            access=self.access,
        )
        self.assertFalse(page.steps[0].result_available)

    # ---------------------------------------------------------- commentary

    def test_commentary_is_published_without_ending_the_turn(self) -> None:
        _conversation, creation, context, interaction_id, stage = self.build_turn()
        message = self.store.publish_commentary(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            text="我翻一下之前的记录。",
            retention="standard",
            sources=artifact_sources(context),
            live_text="我翻一下之前的记录。",
            on_event=self.sink,
        )
        self.assertEqual("commentary", message.kind)
        self.assertEqual(2, message.seq)
        turn = self.store.turn_result(
            namespace=NS, host_instance=HOST,
            turn_id=creation.turn.turn_id, access=self.access,
        )
        self.assertEqual("running", turn.turn.state)
        self.assertIsNone(turn.final)
        self.assertEqual("message_published", self.events[-1][0].kind)
        self.assertEqual(
            {"text": "我翻一下之前的记录。"}, dict(self.events[-1][1])
        )

    # -------------------------------------------------------------- cancel

    def test_cancel_moves_to_cancelled_and_keeps_published_messages(self) -> None:
        _conversation, creation, context, interaction_id, stage = self.build_turn()
        self.store.publish_commentary(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            text="我先看一下。",
            retention="standard",
            sources=artifact_sources(context),
        )
        turn = self.store.cancel_turn(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction_id,
            on_event=self.sink,
        )
        self.assertEqual("cancelled", turn.state)
        self.assertIsNotNone(turn.completed_at)
        result = self.store.turn_result(
            namespace=NS, host_instance=HOST,
            turn_id=creation.turn.turn_id, access=self.access,
        )
        self.assertIsNone(result.final)
        self.assertEqual("turn_cancelled", self.events[-1][0].kind)
        interaction = self.store._require_connection().execute(
            "SELECT result FROM collab_interactions WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()
        self.assertEqual("incomplete", interaction["result"])
        # A second cancel is idempotent and returns the same terminal state.
        again = self.store.cancel_turn(
            namespace=NS, turn_id=creation.turn.turn_id
        )
        self.assertEqual("cancelled", again.state)

    def test_discard_late_result_only_writes_an_event(self) -> None:
        _conversation, creation, _context, _interaction, _stage = self.build_turn()
        self.store.discard_late_result(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            attempt_ref={
                "authorization_id": "authorization:x",
                "attempt_id": "11111111-1111-4111-8111-111111111111",
                "attempt_no": 1,
                "request_hash": "sha256:" + "0" * 64,
            },
            on_event=self.sink,
        )
        self.assertEqual("late_result_discarded", self.events[-1][0].kind)
        turn = self.store.turn_result(
            namespace=NS, host_instance=HOST,
            turn_id=creation.turn.turn_id, access=self.access,
        )
        self.assertEqual("running", turn.turn.state)

    # ------------------------------------------------- deletion / boundary

    def test_deleting_a_tool_read_record_clears_the_tool_result(self) -> None:
        _conversation, creation, context, interaction_id, stage = self.build_turn()
        belief = self.propose(body=self.belief_body())
        record = belief.record_id, belief.record_version
        record_ref = {
            "ref_type": "record",
            "namespace": NS,
            "record_id": record[0],
            "record_version": record[1],
        }
        step = self.plan(creation, stage)
        step = self.store.start_tool_step(
            namespace=NS, turn_id=creation.turn.turn_id, step_id=step.step_id
        )
        sources = artifact_sources(context) + [record_ref]
        step = self.store.finish_tool_step(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            step_id=step.step_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            status="succeeded",
            duration_ms=5,
            body=self.body(result={"claim": "synthetic"}),
            sources=sources,
            retention="standard",
        )
        # Before the deletion the detail is readable.
        page = self.store.read_turn_steps(
            namespace=NS, host_instance=HOST, turn_id=creation.turn.turn_id,
            access=self.access,
        )
        self.assertTrue(page.steps[0].result_available)

        self.delete_record(record[0])
        with self.assertRaises(Redacted):
            self.store.load_artifact(
                namespace=NS,
                artifact_id=step.result_artifact_id,
                artifact_version=step.result_artifact_version,
            )
        page = self.store.read_turn_steps(
            namespace=NS, host_instance=HOST, turn_id=creation.turn.turn_id,
            access=self.access,
        )
        self.assertFalse(page.steps[0].result_available)
        self.assertIsNone(page.steps[0].result_excerpt)

    def test_eight_artifact_levels_pass_and_the_ninth_is_rejected(self) -> None:
        _conversation, creation, context, interaction_id, _stage = self.build_turn()
        sources = artifact_sources(context)
        registered = []
        for index in range(8):
            artifact = self.store.register_artifact(
                namespace=NS,
                kind="query_result",
                interaction_id=interaction_id,
                context_id=context.context_id,
                origin_kind="deterministic_query",
                body=f'{{"result":{index}}}',
                retention="standard",
                sources=sources,
            )
            registered.append(artifact)
            sources = sources + [
                {
                    "ref_type": "collab_artifact",
                    "namespace": NS,
                    "artifact_id": artifact.artifact_id,
                    "artifact_version": artifact.artifact_version,
                }
            ]
        deepest = registered[-1]
        depth = self.store._artifact_depth_locked(
            NS, deepest.artifact_id, deepest.artifact_version, set()
        )
        self.assertEqual(7, depth)
        # The eighth level loads; the ninth (depth 8) cannot even be read back,
        # because its leaf sources would sit at depth 9 in the barrier walk.
        self.store.load_artifact(
            namespace=NS,
            artifact_id=deepest.artifact_id,
            artifact_version=deepest.artifact_version,
        )
        with self.assertRaises(CorruptStore):
            self.store.register_artifact(
                namespace=NS,
                kind="query_result",
                interaction_id=interaction_id,
                context_id=context.context_id,
                origin_kind="deterministic_query",
                body='{"result":"ninth"}',
                retention="standard",
                sources=sources,
            )

    def test_step_result_is_truncated_for_the_public_view(self) -> None:
        _conversation, creation, context, interaction_id, stage = self.build_turn()
        big = "x" * (MAX_STEP_DETAIL_CHARS + 50)
        step = self.plan(creation, stage)
        step = self.store.start_tool_step(
            namespace=NS, turn_id=creation.turn.turn_id, step_id=step.step_id
        )
        self.store.finish_tool_step(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            step_id=step.step_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            status="succeeded",
            duration_ms=1,
            body=self.body(result=big),
            sources=artifact_sources(context),
            retention="standard",
        )
        page = self.store.read_turn_steps(
            namespace=NS, host_instance=HOST, turn_id=creation.turn.turn_id,
            access=self.access,
        )
        view = page.steps[0]
        self.assertTrue(view.result_truncated)
        self.assertEqual(MAX_STEP_DETAIL_CHARS, len(view.result_excerpt))


if __name__ == "__main__":
    unittest.main()
