"""UI-F01: persisted usage and turn-trace reads (real store, real Contexts).

The tests build turns through the conversation store exactly as the
orchestrator does, reserve/settle attempts through the model ledger, publish a
final message and a repair marker, then read the projections.  No model call,
socket or private content is involved.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOMAIN = HERE.parent / "domain"
for _path in (HERE, DOMAIN):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.collaboration.consult import _artifact_sources  # noqa: E402
from quintessence_pcs.foundation.errors import RecordNotFound  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:ui-f01-usage"
OTHER_HOST = "host:ui-f01-other"
ACCESS_REF = "authorization:ui-f01-usage"
POLICY_REF = "policy:collab.v1"
PRIVACY = "private"
MANIFEST_HASH = "sha256:" + ("ab" * 32)
USAGE = {"prompt_tokens": 128, "completion_tokens": 32, "total_tokens": 160}


class ConversationUsageStoreTests(CognitionTestCase):
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
        self.authorization = self.store.grant_model_authorization(
            namespace=NS,
            authorization_ref="authorization:ui-f01-fixture",
            provider="deepseek",
            model="deepseek-flash",
            manifest_hash=MANIFEST_HASH,
            max_calls=64,
            max_output_tokens_per_call=512,
            max_wall_seconds=30,
        )
        self._request = 0
        self._runs: dict[str, str] = {}

    # ------------------------------------------------------------- helpers

    def build_turn(self, *, bind_context: bool = True):
        self._request += 1
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id=f"ui-f01-{self._request}",
            text="合成问题。",
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
        if bind_context:
            self.store.bind_stage_context(
                namespace=NS,
                stage_id=stage.stage_id,
                context_id=context.context_id,
            )
        return creation, context, interaction_id, stage

    def reserve(self, context, body: str, *, run_id: str | None = None):
        if run_id is None:
            run_id = self._runs.setdefault(context.context_id, str(uuid.uuid4()))
        reservation = self.store.reserve_model_attempt(
            run_id=run_id,
            namespace=NS,
            context_id=context.context_id,
            authorization_id=self.authorization.authorization_id,
            provider="deepseek",
            model_requested="deepseek-flash",
            manifest_hash=MANIFEST_HASH,
            adapter_kind="direct-http",
            policy_version="collab.v1.direct-http.v2",
            request_body=body,
            manifest_max_calls=64,
            manifest_max_output_tokens_per_call=512,
            manifest_max_wall_seconds=30,
        )
        return reservation.attempt.attempt_id

    def finish(
        self,
        attempt_id: str,
        *,
        usage: dict | None = None,
        usage_known: bool = True,
        outcome: str = "ok",
    ):
        return self.store.finish_model_attempt(
            attempt_id=attempt_id,
            outcome=outcome,
            response_body='{"content":"ok"}',
            finish_reason="stop" if outcome == "ok" else None,
            usage=usage if usage is not None else USAGE,
            usage_known=usage_known,
        )

    def marker(
        self,
        turn_id: str,
        attempt_id: str | None,
        *,
        stage: str,
        reason: str,
        attempt_no: int = 1,
    ) -> None:
        payload: dict = {"stage": stage, "reason": reason}
        if attempt_id is not None:
            payload["attempt_id"] = attempt_id
            payload["attempt_no"] = attempt_no
        self.store.record_turn_event(
            namespace=NS,
            turn_id=turn_id,
            kind="repair_attempt",
            payload=payload,
        )

    def usage(self, turn_id: str | None = None):
        return self.store.conversation_usage(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            turn_id=turn_id,
        )

    def trace(self, turn_id: str):
        return self.store.turn_trace(
            namespace=NS, host_instance=HOST, turn_id=turn_id
        )

    # --------------------------------------------------------------- tests

    def test_each_attempt_counts_once_and_unbound_contexts_are_ignored(self) -> None:
        creation, context, _iid, _stage = self.build_turn()
        self.finish(self.reserve(context, "body-a"))
        self.finish(self.reserve(context, "body-b"))
        stray, stray_context, _i, _s = self.build_turn(bind_context=False)
        self.finish(self.reserve(stray_context, "not-in-a-stage"))

        usage = self.usage(creation.turn.turn_id)
        self.assertEqual(2, usage["attempts_total"])
        self.assertEqual(2, usage["attempts_known"])
        self.assertEqual(320, usage["tokens"]["total"])
        self.assertTrue(usage["tokens"]["complete"])
        self.assertEqual(2, usage["turns_total"])
        self.assertEqual(1, usage["turns_with_attempts"])
        self.assertEqual(2, usage["turn"]["attempts_total"])
        self.assertEqual(320, usage["turn"]["tokens"]["total"])

    def test_unknown_usage_and_reservations_are_never_zero(self) -> None:
        creation, context, _iid, _stage = self.build_turn()
        self.finish(self.reserve(context, "body-unknown"), usage_known=False, usage={})
        self.reserve(context, "body-open")

        usage = self.usage(creation.turn.turn_id)
        self.assertEqual(2, usage["attempts_total"])
        self.assertEqual(1, usage["attempts_unknown"])
        self.assertEqual(1, usage["attempts_open"])
        self.assertFalse(usage["tokens"]["complete"])
        self.assertIsNone(usage["tokens"]["total"])
        self.assertEqual(0, usage["attempts_known"])

    def test_repair_marker_same_body_and_unmarked_attempts(self) -> None:
        creation, context, _iid, _stage = self.build_turn()
        first = self.reserve(context, "same-body")
        self.finish(first)
        resend = self.reserve(context, "same-body")
        self.finish(resend)
        repaired = self.reserve(context, "changed-body")
        self.finish(repaired)
        self.marker(
            creation.turn.turn_id,
            repaired,
            stage="content",
            reason="format",
            attempt_no=3,
        )

        markers = [
            dict(row.payload)
            for row in self.store.read_turn_events(
                namespace=NS, host_instance=HOST, turn_id=creation.turn.turn_id
            ).events
            if row.kind == "repair_attempt"
        ]
        self.assertEqual(1, len(markers))
        self.assertEqual(
            {"stage", "reason", "attempt_id", "attempt_no"}, set(markers[0])
        )
        self.assertEqual(repaired, markers[0]["attempt_id"])
        rendered = json.dumps(markers[0])
        self.assertNotIn("authorization_id", rendered)
        self.assertNotIn("request_hash", rendered)
        self.assertNotIn("sha256:", rendered)

        trace = self.trace(creation.turn.turn_id)
        by_id = {
            attempt["attempt_id"]: attempt
            for stage in trace["stages"]
            for context_view in stage["contexts"]
            for attempt in context_view["attempts"]
        }
        self.assertEqual("primary", by_id[first]["kind"])
        self.assertFalse(by_id[first]["repair"])
        self.assertEqual("transport_retry", by_id[resend]["kind"])
        self.assertFalse(by_id[resend]["repair"])
        self.assertEqual("repair", by_id[repaired]["kind"])
        self.assertTrue(by_id[repaired]["repair"])
        self.assertEqual("format", by_id[repaired]["repair_reason"])
        self.assertEqual(1, trace["counts"]["repairs"])
        self.assertEqual(0, trace["counts"]["repair_unknown"])
        self.assertEqual(1, trace["counts"]["transport_retries"])

    def test_unmarked_request_stays_unknown(self) -> None:
        creation, context, _iid, _stage = self.build_turn()
        self.finish(self.reserve(context, "body-a"))
        self.finish(self.reserve(context, "body-b"))

        trace = self.trace(creation.turn.turn_id)
        attempts = [a for stage in trace["stages"] for c in stage["contexts"] for a in c["attempts"]]
        self.assertEqual(["primary", "unmarked"], [item["kind"] for item in attempts])
        self.assertIsNone(attempts[1]["repair"])
        self.assertEqual(1, trace["counts"]["repair_unknown"])

    def test_final_source_marks_the_expression_fallback(self) -> None:
        creation, context, interaction_id, stage = self.build_turn()
        attempt_id = self.reserve(context, "content-body")
        self.finish(attempt_id)
        self.store.finish_stage(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            status="succeeded",
        )
        self.store.skip_expression_stage(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            reason="draft_too_large",
        )
        self.store.publish_assistant_message(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            text="草稿回答。",
            retention="standard",
            sources=_artifact_sources(context),
            attempt_ref={
                "authorization_id": self.authorization.authorization_id,
                "attempt_id": attempt_id,
                "attempt_no": 1,
                "request_hash": MANIFEST_HASH,
            },
            finish_stage=False,
        )

        trace = self.trace(creation.turn.turn_id)
        self.assertEqual("content", trace["final"]["source"])
        self.assertTrue(trace["final"]["fallback"])
        self.assertEqual("draft_too_large", trace["final"]["fallback_reason"])
        self.assertEqual("skipped", trace["stages"][1]["status"])
        self.assertEqual("draft_too_large", trace["stages"][1]["skip_reason"])

    def test_final_source_names_the_expression_stage(self) -> None:
        creation, content_context, interaction_id, stage = self.build_turn()
        content_attempt = self.reserve(content_context, "content-body")
        self.finish(content_attempt)
        self.store.finish_stage(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            status="succeeded",
        )
        expression_context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.access,
            input_refs=[creation.input_ref],
            adapter_kind="direct-http",
        )
        expression = self.store.begin_expression_stage(
            namespace=NS, turn_id=creation.turn.turn_id
        )
        self.store.bind_stage_context(
            namespace=NS,
            stage_id=expression.stage_id,
            context_id=expression_context.context_id,
            purpose="expression",
        )
        expression_attempt = self.reserve(expression_context, "expression-body")
        self.finish(expression_attempt)
        self.store.finish_stage(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=expression.stage_id,
            status="succeeded",
        )
        self.store.publish_assistant_message(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=expression.stage_id,
            interaction_id=interaction_id,
            context_id=expression_context.context_id,
            text="表达回答。",
            retention="standard",
            sources=_artifact_sources(expression_context),
            attempt_ref={
                "authorization_id": self.authorization.authorization_id,
                "attempt_id": expression_attempt,
                "attempt_no": 1,
                "request_hash": MANIFEST_HASH,
            },
        )

        trace = self.trace(creation.turn.turn_id)
        self.assertEqual("expression", trace["final"]["source"])
        self.assertFalse(trace["final"]["fallback"])
        self.assertEqual(320, trace["tokens"]["total"])
        self.assertEqual(2, len(trace["stages"]))
        self.assertEqual(1, trace["stages"][1]["contexts"][0]["ordinal"])

    def test_transport_retry_inside_a_repair_is_not_a_repair(self) -> None:
        creation, context, _iid, _stage = self.build_turn()
        self.finish(self.reserve(context, "first-body"))
        failed = self.reserve(context, "repair-body")
        self.finish(failed, outcome="failed")
        settled = self.reserve(context, "repair-body")
        self.finish(settled)
        self.marker(
            creation.turn.turn_id,
            settled,
            stage="content",
            reason="format",
            attempt_no=3,
        )

        trace = self.trace(creation.turn.turn_id)
        attempts = [
            item
            for stage in trace["stages"]
            for context_view in stage["contexts"]
            for item in context_view["attempts"]
        ]
        self.assertEqual(
            ["primary", "transport_retry", "repair"],
            [item["kind"] for item in attempts],
        )
        self.assertFalse(attempts[1]["repair"])
        self.assertTrue(attempts[1]["resend_same_body"])
        self.assertTrue(attempts[2]["repair"])
        self.assertEqual(1, trace["counts"]["repairs"])

    def test_attempt_less_repair_marker_is_counted_unbound(self) -> None:
        creation, context, _iid, _stage = self.build_turn()
        self.finish(self.reserve(context, "first-body"))
        self.marker(creation.turn.turn_id, None, stage="content", reason="format")

        trace = self.trace(creation.turn.turn_id)
        self.assertEqual(0, trace["counts"]["repairs"])
        self.assertEqual(1, trace["counts"]["repairs_unbound"])
        attempts = [
            item
            for stage in trace["stages"]
            for context_view in stage["contexts"]
            for item in context_view["attempts"]
        ]
        self.assertFalse(any(item["repair"] for item in attempts))
        usage = self.usage(creation.turn.turn_id)
        self.assertEqual(0, usage["counts"]["repairs"])
        self.assertEqual(1, usage["counts"]["repairs_unbound"])

    def test_restart_reads_the_same_projection(self) -> None:
        creation, context, _iid, _stage = self.build_turn()
        first = self.reserve(context, "body-a")
        self.finish(first)
        repaired = self.reserve(context, "body-b")
        self.finish(repaired)
        self.marker(creation.turn.turn_id, repaired, stage="content", reason="feedback_retry")
        before = self.trace(creation.turn.turn_id)
        before_usage = self.usage(creation.turn.turn_id)

        self.store.close()
        reopened = type(self.store).open(self.db_path)
        self.addCleanup(reopened.close)
        after = reopened.turn_trace(
            namespace=NS, host_instance=HOST, turn_id=creation.turn.turn_id
        )
        after_usage = reopened.conversation_usage(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            turn_id=creation.turn.turn_id,
        )
        self.assertEqual(before["tokens"], after["tokens"])
        self.assertEqual(before["counts"], after["counts"])
        self.assertEqual(before_usage["tokens"], after_usage["tokens"])

    def test_reads_are_scoped_to_the_host_instance(self) -> None:
        creation, _context, _iid, _stage = self.build_turn()
        with self.assertRaises(RecordNotFound):
            self.store.turn_trace(
                namespace=NS, host_instance=OTHER_HOST, turn_id=creation.turn.turn_id
            )
        with self.assertRaises(RecordNotFound):
            self.store.conversation_usage(
                namespace=NS,
                host_instance=OTHER_HOST,
                conversation_id=self.conversation.conversation_id,
            )


if __name__ == "__main__":
    unittest.main()
