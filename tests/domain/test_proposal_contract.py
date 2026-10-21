"""PCS2-010 Phase 0 contract patch: complete proposal format + specific repair.

The authoritative acceptance rules did not change: ``beliefs.validate_belief_body``
and ``scope.normalize_scope`` still decide.  This module checks that the format
shown to the model and the format-repair feedback are generated from the same
constants and the frozen J.2 scope contract, that a legal proposal built from
the shown format commits through Tx B, and that illegal scope/enums are still
rejected.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
REPO_ROOT = HERE.parents[1]

from cognition_support import NS, CognitionTestCase  # noqa: E402
from model_support import ModelPortTestCase  # noqa: E402

from quintessence_pcs.domains.cognition.beliefs import (  # noqa: E402
    BELIEF_BODY_KEYS,
    CLAIM_TYPES,
    INFERENCE_BASES,
    INFERENCE_BASIS_BY_CLAIM_TYPE,
    RELATIONS,
    SCOPE_REQUIRED_CLAIM_TYPES,
    STRENGTHS,
)
from quintessence_pcs.domains.cognition.errors import (  # noqa: E402
    EvidenceMissing,
    UnsupportedOperation,
)
from quintessence_pcs.domains.errors import SourceUntrusted  # noqa: E402
from quintessence_pcs.domains.scope import SCOPE_FIELDS, SCOPE_ID_MAX_LENGTH  # noqa: E402
from quintessence_pcs.foundation.errors import InvalidInput, ScopeViolation  # noqa: E402
from quintessence_pcs.foundation.jsonutil import canonical_json_text  # noqa: E402
from quintessence_pcs.runtime.context import (  # noqa: E402
    PROPOSAL_CONTRACT,
    PROPOSAL_CONTRACT_COMPACT,
    proposal_format_hint,
)
from quintessence_pcs.runtime.step import repair_feedback  # noqa: E402


def _load_contract(name: str) -> dict:
    return json.loads(
        (REPO_ROOT / "contracts" / "v2" / name).read_text(encoding="utf-8")
    )


class ProposalContractSourceTests(unittest.TestCase):
    def test_enums_match_the_authoritative_constants(self) -> None:
        contract = PROPOSAL_CONTRACT
        self.assertEqual(list(CLAIM_TYPES), contract["claim_types"])
        self.assertEqual(list(INFERENCE_BASES), contract["inference_bases"])
        self.assertEqual(
            dict(INFERENCE_BASIS_BY_CLAIM_TYPE),
            contract["inference_basis_by_claim_type"],
        )
        self.assertEqual(list(STRENGTHS), contract["strengths"])
        self.assertEqual(list(RELATIONS), contract["relations"])
        self.assertEqual(list(SCOPE_FIELDS), contract["scope_fields"])
        self.assertEqual(
            set(SCOPE_REQUIRED_CLAIM_TYPES),
            set(contract["scope_required_for_claim_types"]),
        )
        self.assertEqual(set(BELIEF_BODY_KEYS), set(contract["belief_body_fields"]))
        self.assertEqual(set(BELIEF_BODY_KEYS), set(contract["field_types"]))
        self.assertEqual(
            {"propose_belief", "revise_belief", "retract_belief", "wait"},
            set(contract["operations"]),
        )

    def test_scope_structure_matches_the_frozen_j2_contract(self) -> None:
        schema = _load_contract("scope.json")
        properties = schema["properties"]
        self.assertEqual(list(SCOPE_FIELDS), list(properties))
        self.assertEqual(list(properties), PROPOSAL_CONTRACT["scope"]["allowed_fields"])
        self.assertFalse(schema["additionalProperties"])
        self.assertIs(False, PROPOSAL_CONTRACT["scope"]["additional_properties"])
        for field in SCOPE_FIELDS:
            rendered = PROPOSAL_CONTRACT["scope"]["field_types"][field]
            if field == "entity_ids":
                self.assertEqual("array", properties[field]["type"])
                self.assertIn("array", rendered)
            else:
                self.assertEqual(
                    "_common.json#/$defs/scope_id",
                    properties[field]["$ref"],
                )
                self.assertIn("string", rendered)
        common = _load_contract("_common.json")
        self.assertEqual(
            common["$defs"]["scope_id"]["maxLength"], SCOPE_ID_MAX_LENGTH
        )
        self.assertIn(str(SCOPE_ID_MAX_LENGTH), PROPOSAL_CONTRACT["scope"]["field_types"]["scene"])

    def test_field_types_and_scope_guidance_are_explicit(self) -> None:
        contract = PROPOSAL_CONTRACT
        self.assertEqual(
            "pcs2-010-p0.proposal-contract.v2", contract["format_version"]
        )
        self.assertEqual(
            list(CLAIM_TYPES), contract["field_types"]["claim_type"]["enum"]
        )
        self.assertEqual(
            list(INFERENCE_BASES), contract["field_types"]["inference_basis"]["enum"]
        )
        self.assertEqual(
            dict(INFERENCE_BASIS_BY_CLAIM_TYPE),
            contract["field_types"]["inference_basis"]["by_claim_type"],
        )
        self.assertEqual(
            list(STRENGTHS), contract["field_types"]["strength"]["enum"]
        )
        self.assertEqual(
            list(RELATIONS), contract["evidence_link_types"]["relation"]["enum"]
        )
        scope = contract["scope"]
        self.assertEqual("object", scope["type"])
        self.assertEqual("namespace-general", scope["empty_object_means"])
        self.assertEqual({"scene": "example.scene"}, scope["example"])
        self.assertTrue(set(scope["example"]).issubset(set(SCOPE_FIELDS)))
        self.assertIn("namespace", scope["note"])
        for operation in ("propose_belief", "revise_belief", "retract_belief", "wait"):
            self.assertIn(operation, contract["operations_detail"])

    def test_format_hint_contains_all_authoritative_forms(self) -> None:
        hint = proposal_format_hint()
        for claim_type in CLAIM_TYPES:
            self.assertIn(claim_type, hint)
        for basis in INFERENCE_BASES:
            self.assertIn(basis, hint)
        for claim_type, basis in INFERENCE_BASIS_BY_CLAIM_TYPE.items():
            self.assertIn(f"{claim_type}->{basis}", hint)
        for strength in STRENGTHS:
            self.assertIn(strength, hint)
        for relation in RELATIONS:
            self.assertIn(relation, hint)
        for field in SCOPE_FIELDS:
            self.assertIn(field, hint)
        self.assertIn("namespace", hint)
        self.assertIn("valid_until", hint)

    def test_repair_feedback_names_a_bad_scope_and_allowed_forms(self) -> None:
        feedback = repair_feedback(
            ScopeViolation("scope has unknown field(s): namespace")
        )
        self.assertIn("Erroneous field: scope", feedback)
        self.assertIn("namespace", feedback)
        for field in SCOPE_FIELDS:
            self.assertIn(field, feedback)
        for claim_type in CLAIM_TYPES:
            self.assertIn(claim_type, feedback)
        self.assertIn("observation", feedback)
        self.assertIn("strength", feedback)

    def test_repair_feedback_names_a_string_scope(self) -> None:
        feedback = repair_feedback(
            ScopeViolation(
                "scope must be a scope object mapping the five J.2 fields"
            )
        )
        self.assertIn("Erroneous field: scope", feedback)
        self.assertIn("not a string", feedback)
        self.assertIn("project_id", feedback)

    def test_repair_feedback_names_the_enum_and_operation(self) -> None:
        claim = repair_feedback(
            InvalidInput(
                "claim_type must be one of: "
                + ", ".join(CLAIM_TYPES)
            )
        )
        self.assertIn("Erroneous field: claim_type", claim)
        self.assertIn("capability", claim)
        inference = repair_feedback(
            InvalidInput(
                "inference_basis must be one of: " + ", ".join(INFERENCE_BASES)
            )
        )
        self.assertIn("Erroneous field: inference_basis", inference)
        operation = repair_feedback(
            UnsupportedOperation(
                "model output operation 'retract' is not allowed"
            )
        )
        self.assertIn("Erroneous field: operation", operation)
        self.assertIn("propose_belief", operation)

    def test_repair_feedback_keeps_evidence_errors_specific(self) -> None:
        evidence = repair_feedback(
            EvidenceMissing(
                "claim_type 'capability' cannot be supported by "
                "source_kind 'user_report'"
            )
        )
        self.assertNotIn("Erroneous field: claim_type", evidence)
        self.assertIn("evidence reference is not usable", evidence)
        self.assertIn("available_references", evidence)
        untrusted = repair_feedback(
            SourceUntrusted("evidence event x is a question; not a fact")
        )
        self.assertIn("evidence reference is not usable", untrusted)


class ProposalContractContextTests(CognitionTestCase):
    def test_complete_contract_is_only_rendered_for_a_model_bound_context(self) -> None:
        event = self.ingest()
        preview = self.build_context(event.event_id)
        document = json.loads(preview.rendered_input)
        self.assertEqual(PROPOSAL_CONTRACT_COMPACT, document["proposal_contract"])
        self.assertNotIn("claim_types", document["proposal_contract"])

        model_bound = self.build_context(event.event_id, adapter_kind="direct-http")
        bound_document = json.loads(model_bound.rendered_input)
        contract = bound_document["proposal_contract"]
        self.assertEqual("pcs2-010-p0.proposal-contract.v2", contract["format_version"])
        self.assertEqual(list(CLAIM_TYPES), contract["claim_types"])
        self.assertEqual(list(SCOPE_FIELDS), contract["scope"]["allowed_fields"])


class ProposalContractE2ETests(ModelPortTestCase):
    def _legal_proposal(self, event_id: str, *, claim: str = "capability:path_probe_dev"):
        return {
            "operation": "propose_belief",
            "scope": {"scene": "contract.test"},
            "body": {
                "claim": claim,
                "claim_type": "capability",
                "evidence_links": [
                    self.evidence_link(
                        self.event_ref(event_id),
                        applicability_reason="contract patch mock fixture",
                    )
                ],
                "strength": "moderate",
                "strength_reason": "one synthetic host observation",
                "inference_basis": "observation",
                "premise_refs": [],
                "alternative_group_id": None,
                "valid_until": None,
                "review_condition": "review when new independent evidence appears",
            },
        }

    def test_legal_proposal_from_the_contract_passes_tx_b(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        proposal = self._legal_proposal(event.event_id)
        self.enqueue_json(self.completion(canonical_json_text(proposal)))

        result, context = self.run_and_context(event.event_id)

        self.assertEqual("committed", result.outcome, result.error_message)
        self.assertEqual(0, result.repairs)
        self.assertEqual(1, self.provider.hits)
        assert context is not None
        document = json.loads(context.rendered_input)
        contract = document["proposal_contract"]
        self.assertEqual(list(CLAIM_TYPES), contract["claim_types"])
        self.assertEqual(list(SCOPE_FIELDS), contract["scope"]["allowed_fields"])

        self.assertIsNotNone(result.belief_ref)
        version = self.store.get_version(
            NS, result.belief_ref.record_id, result.belief_ref.record_version
        )
        self.assertEqual("belief", version.kind)
        self.assertEqual("supported", version.status)
        self.assertEqual("capability", version.body["claim_type"])
        self.assertEqual("observation", version.body["inference_basis"])
        self.assertEqual("limited", version.body["strength"])
        self.assertEqual(
            [self.event_ref(event.event_id)],
            [link["ref"] for link in version.body["evidence_links"]],
        )
        current = list(self.store.current_versions_of_kind(NS, "belief"))
        self.assertEqual(1, len(current))
        self.assertEqual(version.record_id, current[0].record_id)
        progress = self.store.work_progress(result.work_id)
        self.assertIsNotNone(progress)
        self.assertEqual("committed", progress.outcome)

    def test_illegal_scope_and_enums_are_still_rejected(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        context = self.build_context(event.event_id)
        before = self.count_rows("record_versions")
        legal_body = self.belief_body(
            evidence_links=[self.evidence_link(self.event_ref(event.event_id))]
        )

        illegal_cases = (
            ("scope-namespace", dict(legal_body), {"namespace": NS}),
            (
                "claim-type",
                dict(legal_body, claim_type="capability_assessment"),
                {"scene": "contract.test"},
            ),
            (
                "inference-basis",
                dict(legal_body, inference_basis="because the observation failed"),
                {"scene": "contract.test"},
            ),
            (
                "strength",
                dict(legal_body, strength="very_strong"),
                {"scene": "contract.test"},
            ),
            (
                "scope-missing-for-supported-claim",
                dict(legal_body),
                {},
            ),
        )
        for label, body, scope in illegal_cases:
            with self.subTest(label):
                with self.assertRaises(
                    (ScopeViolation, InvalidInput, EvidenceMissing)
                ):
                    self.beliefs.propose_belief(
                        namespace=NS,
                        context_id=context.context_id,
                        request_id=str(uuid.uuid4()),
                        body=body,
                        scope=scope,
                    )
                self.assertEqual(before, self.count_rows("record_versions"))

    def test_repair_feedback_targets_the_bad_scope_and_commits(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        invalid = self._legal_proposal(event.event_id)
        invalid["scope"] = {"namespace": NS}
        legal = self._legal_proposal(event.event_id)
        self.enqueue_json(self.completion(canonical_json_text(invalid)))
        self.enqueue_json(self.completion(canonical_json_text(legal)))

        result, context = self.run_and_context(event.event_id)

        self.assertEqual("committed", result.outcome, result.error_message)
        self.assertEqual(1, result.repairs)
        self.assertEqual(2, result.model_calls)
        self.assertEqual(2, self.provider.hits)
        assert context is not None
        attempts = self.attempts_for(context)
        self.assertEqual(2, len(attempts))
        self.assertEqual("ok", attempts[0].outcome)
        self.assertEqual("ok", attempts[1].outcome)
        bodies = [item["body"] for item in self.provider.requests]
        self.assertGreater(len(bodies[1]), len(bodies[0]))
        second_request = json.loads(bodies[1].decode("utf-8"))
        feedback = second_request["messages"][-1]["content"]
        self.assertIn("Erroneous field: scope", feedback)
        self.assertIn("namespace", feedback)
        for field in SCOPE_FIELDS:
            self.assertIn(field, feedback)
        self.assertIn("claim_type", feedback)
        self.assertIn("capability", feedback)
        self.assertIn("inference_basis", feedback)
        self.assertIn("observation", feedback)
        self.assertIn("strength", feedback)
        self.assertIn("Return exactly one JSON object", feedback)

    def test_repair_feedback_targets_a_bad_claim_type(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        invalid = self._legal_proposal(event.event_id)
        invalid["body"]["claim_type"] = "capability_assessment"
        legal = self._legal_proposal(event.event_id)
        self.enqueue_json(self.completion(canonical_json_text(invalid)))
        self.enqueue_json(self.completion(canonical_json_text(legal)))

        result, context = self.run_and_context(event.event_id)

        self.assertEqual("committed", result.outcome, result.error_message)
        self.assertEqual(1, result.repairs)
        assert context is not None
        bodies = [item["body"] for item in self.provider.requests]
        feedback = json.loads(bodies[1].decode("utf-8"))["messages"][-1]["content"]
        self.assertIn("Erroneous field: claim_type", feedback)
        self.assertIn("capability", feedback)
        self.assertNotIn("capability_assessment", feedback)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
