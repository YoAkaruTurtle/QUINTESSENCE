"""General domain/adapter regressions with synthetic events and loopback only."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cognition_support import CognitionTestCase, NS
from model_support import ModelPortTestCase, make_manifest
from quintessence_pcs.domains.cognition.errors import EvidenceMissing
from quintessence_pcs.foundation.jsonutil import canonical_json_text
from quintessence_pcs.runtime.step import MODEL_FAILURE_CODE


class EnvironmentValidityTests(CognitionTestCase):
    def environment_body(self, deadline):
        event = self.ingest(event_type='tool.synthetic.scan')
        return self.belief_body(
            claim='environment:synthetic-workspace', claim_type='environment_state',
            evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
            strength='moderate', inference_basis='observation', valid_until=deadline)

    def test_missing_deadline_rejected_without_record_write(self):
        body = self.environment_body(None)
        context = self.build_context()
        before = self.count_rows('record_versions')
        with self.assertRaises(EvidenceMissing) as caught:
            self.propose(body=body, context_id=context.context_id)
        self.assertEqual('EVIDENCE_MISSING', caught.exception.code)
        self.assertIn('valid_until', str(caught.exception))
        self.assertEqual(before, self.count_rows('record_versions'))

    def test_explicit_deadline_is_preserved(self):
        deadline = '2026-12-31T00:00:00Z'
        version = self.propose(body=self.environment_body(deadline))
        self.assertEqual('environment_state', version.body['claim_type'])
        self.assertEqual(deadline, version.body['valid_until'])
        self.assertEqual('supported', version.status)


class BudgetRefusalTests(ModelPortTestCase):
    def setUp(self):
        super().setUp()
        # Use the actual generic model ledger budget, no experiment quota wrapper.
        self.reconfigure(make_manifest(self.provider.url, max_calls=1))

    def test_budget_refusal_preserves_initial_validation_error(self):
        event = self.ingest(event_type='tool.synthetic.scan')
        invalid = self.proposal(event.event_id)
        invalid['scope'] = {'namespace': NS}
        self.enqueue_json(self.completion(canonical_json_text(invalid)))
        self.enqueue_proposal(event.event_id)
        before = self.count_rows('record_versions')
        result = self.runtime.run_step(namespace=NS, trigger_event_id=event.event_id)
        self.assertEqual('failed', result.outcome)
        self.assertEqual('SCOPE_VIOLATION', result.error_code)
        self.assertIn('namespace', result.error_message)
        self.assertIsNotNone(result.repair_error)
        self.assertIn('ModelBudgetExceededError', result.repair_error)
        self.assertNotIn('SCOPE_VIOLATION', result.repair_error)
        self.assertEqual(1, self.provider.hits)
        self.assertEqual('quintessence-pcs/0.0.0 (tool-free direct-http)',
                         self.provider.requests[0]['headers']['User-Agent'])
        self.assertEqual(before, self.count_rows('record_versions'))
        attempts = self.store.model_attempts_for_context(result.context_id)
        self.assertEqual(1, len(attempts))
        self.assertEqual('ok', attempts[0].outcome)
        self.assertEqual(2, result.model_calls)
        self.assertEqual(1, result.repairs)

    def test_budget_refusal_prevents_transport_resend(self):
        event = self.ingest(event_type='tool.synthetic.scan')
        self.provider.enqueue(self.response({'error': 'synthetic failure'}, status=500))
        result = self.runtime.run_step(namespace=NS, trigger_event_id=event.event_id)
        self.assertEqual('failed', result.outcome)
        self.assertEqual(MODEL_FAILURE_CODE, result.error_code)
        self.assertIn('ModelBudgetExceededError', result.error_message)
        self.assertIsNone(result.repair_error)
        self.assertEqual(1, self.provider.hits)
        attempts = self.store.model_attempts_for_context(result.context_id)
        self.assertEqual(1, len(attempts))
        self.assertEqual('failed', attempts[0].outcome)

if __name__ == '__main__':
    unittest.main()
