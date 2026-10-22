"""C02: the shared proposal parse/validate/repair module is the only copy.

Three checks, and nothing else: every old import path resolves to the same
function object, ``parse_proposal`` still rejects model-supplied identity
fields, and ``validate_proposal`` still refuses a target in another namespace.
No store is built and no network is touched.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import quintessence_pcs.runtime as runtime  # noqa: E402
import quintessence_pcs.runtime.fake_model as fake_model  # noqa: E402
import quintessence_pcs.runtime.proposal_generation as proposal_generation  # noqa: E402
import quintessence_pcs.runtime.step as step  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    InvalidInput,
    ScopeViolation,
)


class ProposalGenerationTests(unittest.TestCase):
    def test_there_is_exactly_one_implementation(self) -> None:
        self.assertIs(runtime.parse_proposal, fake_model.parse_proposal)
        self.assertIs(runtime.parse_proposal, proposal_generation.parse_proposal)
        self.assertIs(step.repair_feedback, proposal_generation.repair_feedback)

    def test_parse_rejects_model_supplied_identity_fields(self) -> None:
        with self.assertRaises(InvalidInput):
            proposal_generation.parse_proposal(
                '{"operation":"wait","reason":"x","state_revision":1}'
            )
        self.assertEqual(
            proposal_generation.parse_proposal('{"operation":"wait","reason":"x"}'),
            {"operation": "wait", "reason": "x"},
        )

    def test_validate_rejects_a_cross_namespace_target(self) -> None:
        proposal = {
            "operation": "retract_belief",
            "target": {
                "ref_type": "record",
                "namespace": "example.test.b",
                "record_id": "00000000-0000-4000-8000-000000000001",
                "record_version": 1,
            },
            "evidence_links": [
                {
                    "ref": {
                        "ref_type": "event",
                        "namespace": "example.test.a",
                        "event_id": "00000000-0000-4000-8000-000000000002",
                    },
                    "relation": "contradicts",
                    "origin_group_id": "example.test.a:group",
                    "applicability_reason": "the cited event contradicts the claim",
                }
            ],
            "reason": "the supporting evidence no longer holds",
        }
        with self.assertRaises(ScopeViolation):
            proposal_generation.validate_proposal("example.test.a", proposal)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
