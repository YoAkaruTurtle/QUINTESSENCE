"""PCS2-010 Phase 0: supplier metadata survives rejected/truncated responses.

The rule being pinned: an HTTP 200 from a real provider is a supplier call that
succeeded at the transport level.  Whether the *proposal* is usable is a
separate question.  If the envelope is parseable, its model, fingerprint, usage
and finish_reason must be persisted even when the response is rejected for
``finish_reason=length``, tool calls, empty content or an invalid proposal.

A network failure never invents usage.  A legal ``finish_reason=stop`` proposal
still commits normally.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS  # noqa: E402
from model_support import (  # noqa: E402
    DEFAULT_USAGE,
    ModelPortTestCase,
    make_manifest,
)

from quintessence_pcs.foundation.jsonutil import canonical_json_text  # noqa: E402
from quintessence_pcs.runtime import (  # noqa: E402
    CognitionRuntime,
    DirectHttpModel,
)
from quintessence_pcs.runtime.step import MODEL_FAILURE_CODE  # noqa: E402

LENGTH_USAGE = {
    "prompt_tokens": 4342,
    "completion_tokens": 512,
    "total_tokens": 4854,
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 4342,
}


class SupplierMetadataTests(ModelPortTestCase):
    def _trigger(self) -> str:
        return self.ingest(event_type="CapabilityObserved:file-read").event_id

    def test_finish_reason_length_records_usage_model_and_fingerprint(self) -> None:
        trigger = self._trigger()
        self.enqueue_json(
            self.completion(
                '{"operation":"propose_belief"',
                finish_reason="length",
                usage=LENGTH_USAGE,
                model="deepseek-flash",
                system_fingerprint="fp_length_test",
            )
        )

        result, context = self.run_and_context(trigger)

        self.assertEqual("failed", result.outcome)
        self.assertEqual(MODEL_FAILURE_CODE, result.error_code)
        self.assertIn("FINISH_REASON_LENGTH", result.error_message or "")
        self.assertEqual(1, self.provider.hits)
        assert context is not None
        attempts = self.attempts_for(context)
        self.assertEqual(1, len(attempts))
        attempt = attempts[0]
        self.assertEqual("finished", attempt.status)
        self.assertEqual("malformed", attempt.outcome)
        self.assertEqual("FINISH_REASON_LENGTH", attempt.error_label)
        self.assertEqual("length", attempt.finish_reason)
        self.assertTrue(attempt.usage_known)
        self.assertEqual(LENGTH_USAGE, attempt.usage)
        self.assertEqual("deepseek-flash", attempt.model_returned)
        self.assertEqual("fp_length_test", attempt.system_fingerprint)
        self.assertIsNotNone(attempt.response_hash)
        self.assertIsNotNone(attempt.request_hash)
        request_text = self.store.load_model_attempt_payload(
            attempt.attempt_id, kind="request"
        )
        response_text = self.store.load_model_attempt_payload(
            attempt.attempt_id, kind="response"
        )
        self.assertIn("propose_belief", response_text)
        self.assertIn("messages", request_text)
        self.assertEqual([], list(self.store.current_versions_of_kind(NS, "belief")))

    def test_http_200_invalid_proposal_keeps_usage_then_repair_commits(self) -> None:
        trigger = self._trigger()
        self.enqueue_content(
            "this is not json",
            usage=DEFAULT_USAGE,
            model="deepseek-flash",
            system_fingerprint="fp_invalid_proposal",
        )
        proposal = self.proposal(trigger)
        self.enqueue_content(canonical_json_text(proposal))

        result, context = self.run_and_context(trigger)

        self.assertEqual("committed", result.outcome, result.error_message)
        self.assertEqual(1, result.repairs)
        self.assertEqual(2, self.provider.hits)
        assert context is not None
        attempts = self.attempts_for(context)
        self.assertEqual(2, len(attempts))
        first = attempts[0]
        self.assertEqual("ok", first.outcome)
        self.assertEqual("stop", first.finish_reason)
        self.assertTrue(first.usage_known)
        self.assertEqual(DEFAULT_USAGE, first.usage)
        self.assertEqual("deepseek-flash", first.model_returned)
        self.assertEqual("fp_invalid_proposal", first.system_fingerprint)
        second = attempts[1]
        self.assertEqual("ok", second.outcome)
        self.assertIsNotNone(result.belief_ref)

    def test_network_failure_never_fabricates_usage(self) -> None:
        manifest = make_manifest(
            "http://127.0.0.1:9",
            api_key_env=self.ENV_NAME,
            max_calls=8,
            max_wall_seconds=30,
        )
        self.grant_authorization(manifest=manifest, max_calls=8)
        model = DirectHttpModel(self.store, manifest)
        runtime = CognitionRuntime(
            self.store,
            model=model,
            belief_service=self.beliefs,
            context_service=self.contexts,
            self_view_service=self.self_view,
        )
        trigger = self._trigger()

        result = runtime.run_step(namespace=NS, trigger_event_id=trigger)

        self.assertEqual("failed", result.outcome)
        self.assertEqual(MODEL_FAILURE_CODE, result.error_code)
        self.assertIn("NETWORK_ERROR", result.error_message or "")
        assert result.context_id is not None
        attempts = self.store.model_attempts_for_context(result.context_id)
        self.assertGreaterEqual(len(attempts), 1)
        for attempt in attempts:
            self.assertEqual("failed", attempt.outcome)
            self.assertEqual("NETWORK_ERROR", attempt.error_label)
            self.assertFalse(attempt.usage_known)
            self.assertIsNone(attempt.usage)
            self.assertIsNone(attempt.model_returned)
            self.assertIsNone(attempt.system_fingerprint)
            self.assertIsNone(attempt.response_hash)

    def test_finish_reason_stop_legal_proposal_commits(self) -> None:
        trigger = self._trigger()
        proposal = self.proposal(trigger)
        self.enqueue_content(canonical_json_text(proposal))

        result, context = self.run_and_context(trigger)

        self.assertEqual("committed", result.outcome, result.error_message)
        self.assertEqual(0, result.repairs)
        assert context is not None
        attempts = self.attempts_for(context)
        self.assertEqual(1, len(attempts))
        attempt = attempts[0]
        self.assertEqual("ok", attempt.outcome)
        self.assertEqual("stop", attempt.finish_reason)
        self.assertTrue(attempt.usage_known)
        self.assertEqual("deepseek-flash", attempt.model_returned)
        self.assertIsNotNone(attempt.system_fingerprint)
        self.assertIsNotNone(result.belief_ref)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
