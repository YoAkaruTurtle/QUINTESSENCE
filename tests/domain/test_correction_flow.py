"""End-to-end two-stage Belief correction through the production runtime."""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.runtime import CognitionRuntime  # noqa: E402


class ScriptedCorrectionModel:
    """Return a fixed sequence of raw proposal strings; count real calls."""

    adapter_kind = "fake-model"

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = list(outputs)
        self.calls = 0

    def propose(self, *, rendered_input: str, references: object, feedback=None) -> str:
        self.calls += 1
        index = min(self.calls - 1, len(self._outputs) - 1)
        return self._outputs[index]


class TwoStageCorrectionTests(CognitionTestCase):
    def _supported_belief(self):
        event = self.ingest(event_type="CapabilityObserved:file-read")
        return (
            self.propose(
                body=self.belief_body(
                    evidence_links=[
                        self.evidence_link(self.event_ref(event.event_id))
                    ]
                )
            ),
            event,
        )

    def _runtime(self, model: ScriptedCorrectionModel) -> CognitionRuntime:
        return CognitionRuntime(
            self.store,
            model=model,
            belief_service=self.beliefs,
            context_service=self.contexts,
            self_view_service=self.self_view,
            clock=self.clock,
        )

    def _retract_and_replacement(
        self,
    ) -> tuple[object, object, object, dict, dict, ScriptedCorrectionModel]:
        old, first_event = self._supported_belief()
        contradict = self.ingest(event_type="CapabilityFailed:file-read")
        retract = {
            "operation": "retract_belief",
            "target": self.record_ref(old),
            "evidence_links": [
                self.evidence_link(
                    self.event_ref(contradict.event_id), relation="contradicts"
                )
            ],
            "reason": "new independent evidence contradicts the old claim",
        }
        replacement_body = self.belief_body(
            claim="capability:file-write",
            evidence_links=[
                self.evidence_link(self.event_ref(contradict.event_id))
            ],
        )
        replacement = {
            "operation": "propose_belief",
            "body": replacement_body,
            "scope": self.default_scope("capability"),
        }
        model = ScriptedCorrectionModel(
            [json.dumps(retract), json.dumps(replacement)]
        )
        return old, first_event, contradict, retract, replacement, model

    def _run_first_and_create_second(
        self, model: ScriptedCorrectionModel, first_event: object
    ):
        runtime = self._runtime(model)
        first = runtime.run_step(
            namespace=NS, trigger_event_id=first_event.event_id
        )
        self.assertEqual("committed", first.outcome)
        self.assertEqual("retracted", first.belief_status)
        first_context = self.contexts.load_context(first.context_id)
        source_event_id = self.store.work_source_event_id(first.work_id)
        self.assertIsNotNone(source_event_id)
        self.store.create_correction_work(
            namespace=NS,
            event_id=first_event.event_id,
            source_event_id=source_event_id,
        )
        return runtime, first, first_context

    def test_illegal_substantive_revise_gets_one_semantic_retry(self) -> None:
        old, first_event = self._supported_belief()
        contradict = self.ingest(event_type="CapabilityFailed:file-read")
        illegal = {
            "operation": "revise_belief",
            "target": self.record_ref(old),
            "body": self.belief_body(
                claim="capability:file-write",
                evidence_links=[
                    self.evidence_link(self.event_ref(contradict.event_id))
                ],
            ),
            "scope": self.default_scope("capability"),
        }
        retract = {
            "operation": "retract_belief",
            "target": self.record_ref(old),
            "evidence_links": [
                self.evidence_link(
                    self.event_ref(contradict.event_id), relation="contradicts"
                )
            ],
            "reason": "the revised claim cannot overwrite the frozen claim",
        }
        model = ScriptedCorrectionModel(
            [json.dumps(illegal), json.dumps(retract)]
        )
        runtime = self._runtime(model)
        result = runtime.run_step(
            namespace=NS, trigger_event_id=first_event.event_id
        )
        self.assertEqual("committed", result.outcome)
        self.assertEqual("retracted", result.belief_status)
        self.assertEqual(2, result.model_calls)
        self.assertEqual(1, len(result.validation_retries))
        self.assertIn("frozen", result.validation_retries[0])
        self.assertEqual(2, model.calls)
        self.assertEqual(
            "retracted", self.store.get_current(NS, old.record_id).status
        )

    def test_two_semantic_rejections_end_in_failed_without_a_retraction(
        self,
    ) -> None:
        old, first_event = self._supported_belief()
        contradict = self.ingest(event_type="CapabilityFailed:file-read")
        illegal_body = self.belief_body(
            claim="capability:file-write",
            evidence_links=[
                self.evidence_link(self.event_ref(contradict.event_id))
            ],
        )
        illegal = {
            "operation": "revise_belief",
            "target": self.record_ref(old),
            "body": illegal_body,
            "scope": self.default_scope("capability"),
        }
        model = ScriptedCorrectionModel(
            [json.dumps(illegal), json.dumps(illegal)]
        )
        runtime = self._runtime(model)
        result = runtime.run_step(
            namespace=NS, trigger_event_id=first_event.event_id
        )
        self.assertEqual("failed", result.outcome)
        self.assertEqual(2, result.model_calls)
        self.assertEqual(2, len(result.validation_retries))
        self.assertEqual(
            "supported", self.store.get_current(NS, old.record_id).status
        )

    def test_retract_then_propose_uses_new_context_and_preserves_history(self) -> None:
        old, first_event, _contradict, _retract, _replacement, model = (
            self._retract_and_replacement()
        )
        runtime, first, first_context = self._run_first_and_create_second(
            model, first_event
        )
        second = runtime.run_step(
            namespace=NS, trigger_event_id=first_event.event_id
        )
        self.assertEqual("committed", second.outcome)
        self.assertEqual("supported", second.belief_status)
        self.assertNotEqual(first.context_id, second.context_id)
        second_context = self.contexts.load_context(second.context_id)
        self.assertGreater(
            second_context.state_revision, first_context.state_revision
        )
        self.assertNotEqual(
            first.belief_ref.record_id, second.belief_ref.record_id
        )
        self.assertEqual(
            "retracted", self.store.get_current(NS, old.record_id).status
        )
        self.assertEqual(
            "supported",
            self.store.get_version(NS, old.record_id, 1).status,
        )
        self.assertEqual(2, model.calls)

    def test_second_wait_keeps_retraction_without_replacement(self) -> None:
        old, first_event = self._supported_belief()
        contradict = self.ingest(event_type="CapabilityFailed:file-read")
        retract = {
            "operation": "retract_belief",
            "target": self.record_ref(old),
            "evidence_links": [
                self.evidence_link(
                    self.event_ref(contradict.event_id), relation="contradicts"
                )
            ],
            "reason": "the old claim is contradicted",
        }
        model = ScriptedCorrectionModel(
            [
                json.dumps(retract),
                json.dumps({"operation": "wait", "reason": "no replacement yet"}),
            ]
        )
        runtime, _first, _first_context = self._run_first_and_create_second(
            model, first_event
        )
        second = runtime.run_step(
            namespace=NS, trigger_event_id=first_event.event_id
        )
        self.assertEqual("checked_no_belief", second.outcome)
        self.assertIsNone(second.belief_ref)
        self.assertEqual(
            "retracted", self.store.get_current(NS, old.record_id).status
        )
        self.assertEqual(2, model.calls)

    def test_second_failure_keeps_retraction_and_does_not_fabricate(self) -> None:
        old, first_event = self._supported_belief()
        contradict = self.ingest(event_type="CapabilityFailed:file-read")
        retract = {
            "operation": "retract_belief",
            "target": self.record_ref(old),
            "evidence_links": [
                self.evidence_link(
                    self.event_ref(contradict.event_id), relation="contradicts"
                )
            ],
            "reason": "the old claim is contradicted",
        }
        model = ScriptedCorrectionModel(
            [json.dumps(retract), "not-json", "not-json"]
        )
        runtime, _first, _first_context = self._run_first_and_create_second(
            model, first_event
        )
        second = runtime.run_step(
            namespace=NS, trigger_event_id=first_event.event_id
        )
        self.assertEqual("failed", second.outcome)
        self.assertIsNone(second.belief_ref)
        self.assertEqual(
            "retracted", self.store.get_current(NS, old.record_id).status
        )


if __name__ == "__main__":
    unittest.main()
