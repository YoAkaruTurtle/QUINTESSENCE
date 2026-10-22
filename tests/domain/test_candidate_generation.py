"""C03: candidate generation is reusable, bounded and never commits.

Four checks: a generated proposal is not a submission, a preflight refusal
builds no Context and sends no model call, a WAIT skips domain validation, and
one shared ``GenerationBudget`` caps calls and counts format repairs.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parents[1] / "src"
for _path in (HERE, SRC_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.runtime.candidate import (  # noqa: E402
    GenerationBudget,
    generate_candidate,
)


class ScriptedModel:
    """Test double implementing the same interface as ``FakeModel``."""

    adapter_kind = "fake-model"

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def propose(self, *, rendered_input, references, feedback=None):
        self.calls += 1
        value = self._outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class RefusingPreflightModel(ScriptedModel):
    """Adapter that refuses locally, before any Context or model call."""

    def preflight(self, *, namespace):
        raise RuntimeError("synthetic adapter refusal")


class CandidateGenerationTests(CognitionTestCase):
    def test_generate_candidate_without_pending_work_does_not_commit(self):
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self.complete_work_as_wait(event.event_id)
        proposal = {
            "operation": "propose_belief",
            "body": self.belief_body(
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))]
            ),
            "scope": self.default_scope("capability"),
        }
        model = ScriptedModel([json.dumps(proposal)])
        progress_before = self.count_rows("work_progress")
        revision_before = self.revision()

        result = generate_candidate(
            namespace=NS,
            trigger_ref=self.event_ref(event.event_id),
            context_service=self.contexts,
            model=model,
        )

        self.assertTrue(result.generated)
        self.assertEqual(proposal, result.proposal)
        self.assertIsNotNone(result.context)
        self.assertEqual(1, result.model_calls)
        self.assertEqual(0, result.repairs)
        self.assertEqual(progress_before, self.count_rows("work_progress"))
        self.assertEqual(revision_before, self.revision())

    def test_preflight_refusal_builds_no_context_and_calls_no_model(self):
        event = self.ingest(event_type="CapabilityObserved:file-read")
        model = RefusingPreflightModel(
            [AssertionError("propose must not be called")]
        )
        contexts_before = self.count_rows("context_snapshots")

        result = generate_candidate(
            namespace=NS,
            trigger_ref=self.event_ref(event.event_id),
            context_service=self.contexts,
            model=model,
        )

        self.assertIsNotNone(result.failure)
        self.assertEqual("preflight", result.failure.phase)
        self.assertIsNone(result.context)
        self.assertEqual(0, result.model_calls)
        self.assertEqual(0, model.calls)
        self.assertEqual(contexts_before, self.count_rows("context_snapshots"))

    def test_wait_proposal_is_accepted_without_domain_validation(self):
        event = self.ingest(event_type="NotACapability")
        wait = {"operation": "wait", "reason": "nothing to do"}
        model = ScriptedModel([json.dumps(wait)])

        result = generate_candidate(
            namespace=NS,
            trigger_ref=self.event_ref(event.event_id),
            context_service=self.contexts,
            model=model,
        )

        self.assertTrue(result.generated)
        self.assertEqual(wait, result.proposal)

    def test_generation_budget_caps_calls_and_counts_repairs(self):
        event = self.ingest(event_type="CapabilityObserved:file-read")
        budget = GenerationBudget()
        repaired = {"operation": "wait", "reason": "repaired"}
        model = ScriptedModel(["not json", json.dumps(repaired)])

        result = generate_candidate(
            namespace=NS,
            trigger_ref=self.event_ref(event.event_id),
            context_service=self.contexts,
            model=model,
            generation_budget=budget,
        )

        self.assertTrue(result.generated)
        self.assertEqual(2, budget.calls)
        self.assertEqual(1, budget.repairs)

        capped = GenerationBudget(max_calls=1)
        second_event = self.ingest(event_type="NotACapability")
        capped_model = ScriptedModel(["not json", json.dumps(repaired)])
        failed = generate_candidate(
            namespace=NS,
            trigger_ref=self.event_ref(second_event.event_id),
            context_service=self.contexts,
            model=capped_model,
            generation_budget=capped,
        )

        self.assertIsNotNone(failed.failure)
        self.assertEqual("proposal", failed.failure.phase)
        self.assertEqual(1, capped.calls)
        self.assertEqual(0, capped.repairs)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
