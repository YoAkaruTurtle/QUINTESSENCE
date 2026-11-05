"""J06 S6b: impact classification, evidence linkage and the review policy.

RP-01..13 from the S6 dispatch, plus the "policy carries no authority" guard.
Everything here is pure: no store, no transaction, no payload text.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from quintessence_pcs.collaboration.review_policy import (  # noqa: E402
    BASE_POLICY,
    BASE_POLICY_ID,
    BASE_POLICY_VERSION,
    EVIDENCE_EVENT_TYPES,
    BaseReviewPolicy,
    ImpactDecision,
    PolicyStateView,
    RiskPolicyConfig,
    TargetFact,
    change_fingerprint,
    classify_impact,
    evaluate_policy,
    evidence_linkage,
)
from quintessence_pcs.domains.cognition.changes import ValidatedChange  # noqa: E402
from quintessence_pcs.foundation.errors import CorruptStore  # noqa: E402

NS = "synthetic.namespace"


def supports() -> dict[str, str]:
    return {"relation": "supports"}


def propose(
    claim_type: str,
    scope: dict[str, object] | None = None,
    *,
    links: tuple[dict[str, str], ...] = (),
    claim: str = "synthetic claim",
    valid_until: str | None = None,
) -> dict[str, object]:
    return {
        "op": "propose_belief",
        "body": {
            "claim_type": claim_type,
            "claim": claim,
            "evidence_links": list(links),
            "valid_until": valid_until,
        },
        "scope": dict(scope or {}),
    }


def revise(
    record_id: str,
    claim_type: str,
    scope: dict[str, object] | None = None,
    *,
    links: tuple[dict[str, str], ...] = (),
) -> dict[str, object]:
    return {
        "op": "revise_belief",
        "target": {"record_id": record_id, "record_version": 1},
        "body": {
            "claim_type": claim_type,
            "claim": "synthetic claim",
            "evidence_links": list(links),
            "valid_until": None,
        },
        "scope": dict(scope or {}),
    }


def retract(record_id: str) -> dict[str, object]:
    return {
        "op": "retract_belief",
        "target": {"record_id": record_id, "record_version": 1},
        "evidence_links": [supports()],
        "reason": "synthetic retraction",
    }


def resolve(question_id: str = "q1") -> dict[str, object]:
    return {
        "op": "resolve_question",
        "target": {"record_id": question_id, "record_version": 1},
        "resolution_refs": [{"record_type": "record", "record_id": "r1"}],
        "resolution_reason": "synthetic resolution",
    }


def bundle(*changes: dict[str, object]) -> ValidatedChange:
    return ValidatedChange(
        namespace=NS,
        summary="synthetic summary",
        changes=tuple(changes),
        request_payload={"summary": "synthetic summary"},
    )


def state(impact: ImpactDecision, **overrides: object) -> PolicyStateView:
    values: dict[str, object] = {
        "predicted_status": "proposed",
        "evidence_linkage": "not_applicable",
        "impact": impact,
    }
    values.update(overrides)
    return PolicyStateView(**values)  # type: ignore[arg-type]


class ImpactTests(unittest.TestCase):
    def test_rp_01_derived_rule_is_blocked_not_merely_risky(self) -> None:
        decision = classify_impact(bundle(propose("derived_rule", {"project_id": "A"})))
        self.assertEqual("BLOCKED", decision.level)
        self.assertTrue(decision.blocked)
        self.assertIn("rule_publication_not_supported", decision.reasons)

        policy = BASE_POLICY.evaluate(
            bundle(propose("derived_rule", {"project_id": "A"})), state(decision)
        )
        self.assertEqual("defer", policy.recommendation)
        self.assertEqual("rule_publication_not_supported", policy.reason_code)
        self.assertTrue(policy.requires_human)

    def test_rp_02_a_global_scope_is_e2(self) -> None:
        decision = classify_impact(bundle(propose("capability", {}, links=(supports(),))))
        self.assertEqual("E2", decision.level)
        self.assertIn("global_scope", decision.reasons)

    def test_rp_03_retracting_a_supported_fact_is_e2(self) -> None:
        decision = classify_impact(
            bundle(retract("r1")),
            target_facts={"r1": TargetFact(status="supported", claim_type="capability")},
        )
        self.assertEqual("E2", decision.level)
        self.assertIn("retracts_supported_fact", decision.reasons)

    def test_rp_03b_retracting_a_provisional_belief_is_not_e2(self) -> None:
        decision = classify_impact(
            bundle(retract("r1")),
            target_facts={"r1": TargetFact(status="proposed", claim_type="capability")},
        )
        self.assertEqual("E0", decision.level)

    def test_rp_04_question_resolution_is_e2(self) -> None:
        decision = classify_impact(bundle(resolve()))
        self.assertEqual("E2", decision.level)
        self.assertIn("question_resolution_requires_review", decision.reasons)

    def test_rp_05_three_targets_or_two_projects_are_e2(self) -> None:
        many = classify_impact(
            bundle(retract("r1"), retract("r2"), retract("r3")),
            target_facts={
                "r1": TargetFact("proposed", "capability"),
                "r2": TargetFact("proposed", "capability"),
                "r3": TargetFact("proposed", "capability"),
            },
        )
        self.assertEqual("E2", many.level)
        self.assertIn("too_many_targets", many.reasons)

        cross = classify_impact(
            bundle(
                propose("general_hypothesis", {"project_id": "A"}),
                propose("general_hypothesis", {"project_id": "B"}),
            )
        )
        self.assertEqual("E2", cross.level)
        self.assertIn("cross_project", cross.reasons)

    def test_rp_06_a_local_provisional_hypothesis_is_e0(self) -> None:
        decision = classify_impact(
            bundle(propose("general_hypothesis", {"project_id": "A"}))
        )
        self.assertEqual("E0", decision.level)

    def test_rp_07_a_supported_local_claim_with_matching_types_is_e1(self) -> None:
        decision = classify_impact(
            bundle(propose("capability", {"project_id": "A"}, links=(supports(),))),
            evidence_linkage_value="verified",
        )
        self.assertEqual("E1", decision.level)
        self.assertIn("evidence_type_matches", decision.reasons)

    def test_rp_08_unverified_linkage_downgrades_to_human_review(self) -> None:
        payload = bundle(propose("capability", {"project_id": "A"}, links=(supports(),)))
        decision = classify_impact(payload, evidence_linkage_value="unverified")
        self.assertEqual("E2", decision.level)
        self.assertIn("evidence_linkage_unverified", decision.reasons)

        policy = BASE_POLICY.evaluate(payload, state(decision, predicted_status="supported"))
        self.assertEqual("accept", policy.recommendation)
        self.assertTrue(policy.requires_human)
        self.assertEqual("impact_requires_human", policy.reason_code)

    def test_rp_09_a_matching_event_type_is_verified_linkage(self) -> None:
        self.assertEqual(
            "verified",
            evidence_linkage(
                predicted_status="supported",
                claim_type="capability",
                supporting_event_types=("CapabilityObserved:file-read",),
            ),
        )
        self.assertEqual(
            "unverified",
            evidence_linkage(
                predicted_status="supported",
                claim_type="capability",
                supporting_event_types=("EnvironmentObserved:disk",),
            ),
        )

    def test_rp_10_a_renamed_hypothesis_is_provisional_and_e0(self) -> None:
        self.assertEqual(
            "not_applicable",
            evidence_linkage(
                predicted_status="proposed",
                claim_type="general_hypothesis",
                supporting_event_types=("EnvironmentObserved:disk",),
            ),
        )
        decision = classify_impact(
            bundle(propose("general_hypothesis", {"project_id": "A"}, links=(supports(),)))
        )
        self.assertEqual("E0", decision.level)

    def test_rp_11_fingerprint_is_stable_and_sensitive(self) -> None:
        base = change_fingerprint(bundle(propose("general_hypothesis", {"project_id": "A"})))
        again = change_fingerprint(bundle(propose("general_hypothesis", {"project_id": "A"})))
        other_scope = change_fingerprint(
            bundle(propose("general_hypothesis", {"project_id": "B"}))
        )
        other_claim = change_fingerprint(
            bundle(
                propose(
                    "general_hypothesis",
                    {"project_id": "A"},
                    claim="a different claim",
                )
            )
        )
        self.assertEqual(base, again)
        self.assertNotEqual(base, other_scope)
        self.assertNotEqual(base, other_claim)

    def test_rp_12_the_policy_can_never_hand_out_authority(self) -> None:
        decision = BASE_POLICY.evaluate(
            bundle(propose("general_hypothesis", {"project_id": "A"})),
            state(ImpactDecision(level="E0", reasons=("local_provisional_change",))),
        )
        self.assertEqual("accept", decision.recommendation)
        forbidden = {"authority", "grant", "grant_id", "approval", "authorization_ref"}
        self.assertFalse(forbidden & set(decision.__dataclass_fields__))
        self.assertFalse(hasattr(decision, "authority"))

    def test_rp_13_a_policy_that_lies_about_its_identity_is_corrupt(self) -> None:
        class Impostor(BaseReviewPolicy):
            policy_id = "policy:cognition.review.base.v2"
            policy_version = "999"

        with self.assertRaises(CorruptStore):
            evaluate_policy(
                Impostor(),
                bundle(propose("general_hypothesis", {"project_id": "A"})),
                state(ImpactDecision(level="E0", reasons=("local",))),
            )

        good = evaluate_policy(
            BASE_POLICY,
            bundle(propose("general_hypothesis", {"project_id": "A"})),
            state(ImpactDecision(level="E0", reasons=("local",))),
        )
        self.assertEqual(BASE_POLICY_ID, good.policy_id)
        self.assertEqual(BASE_POLICY_VERSION, good.policy_version)

    def test_rp_14_escalation_precedence(self) -> None:
        payload = bundle(propose("general_hypothesis", {"project_id": "A"}))
        impact = ImpactDecision(level="E0", reasons=("local",))
        escalated = BASE_POLICY.evaluate(
            payload, state(impact, has_competing_current=True)
        )
        self.assertEqual("escalate", escalated.recommendation)
        self.assertEqual("competing_claim", escalated.reason_code)
        self.assertTrue(escalated.requires_human)

    def test_rp_15_an_unknown_additive_scope_fails_closed(self) -> None:
        config = RiskPolicyConfig()
        decision = classify_impact(
            bundle(propose("capability", {"project_id": "A"}, links=(supports(),))),
            authorized_scope={"project_id": "A", "scene": "x"},
            evidence_linkage_value="verified",
            config=config,
        )
        self.assertEqual("E2", decision.level)
        self.assertIn("scope_not_authorized", decision.reasons)

    # ------------------------------------------------------------------
    # G-S6 fix: a bare "ToolResult:" prefix must not verify anything.
    # The dispatch numbers these RP-14..17; the two tests above already
    # used those numbers for unrelated checks, so the names differ while
    # the ids stay traceable to the dispatch.
    # ------------------------------------------------------------------

    def test_rp_14_tool_result_does_not_verify_capability(self) -> None:
        linkage = evidence_linkage(
            predicted_status="supported",
            claim_type="capability",
            supporting_event_types=("ToolResult:fetched-weather",),
        )
        self.assertEqual("unverified", linkage)

        payload = bundle(propose("capability", {"project_id": "A"}, links=(supports(),)))
        decision = classify_impact(payload, evidence_linkage_value=linkage)
        self.assertEqual("E2", decision.level)
        self.assertIn("evidence_linkage_unverified", decision.reasons)

        policy = BASE_POLICY.evaluate(
            payload, state(decision, predicted_status="supported")
        )
        self.assertEqual("accept", policy.recommendation)
        self.assertTrue(policy.requires_human)
        self.assertEqual("impact_requires_human", policy.reason_code)

    def test_rp_15_capability_observed_still_verifies(self) -> None:
        linkage = evidence_linkage(
            predicted_status="supported",
            claim_type="capability",
            supporting_event_types=("CapabilityObserved:file-read",),
        )
        self.assertEqual("verified", linkage)

        payload = bundle(propose("capability", {"project_id": "A"}, links=(supports(),)))
        decision = classify_impact(payload, evidence_linkage_value=linkage)
        self.assertEqual("E1", decision.level)
        self.assertIn("evidence_type_matches", decision.reasons)

    def test_rp_16_no_bare_tool_result_prefix_in_the_default_registry(self) -> None:
        for claim_type, prefixes in EVIDENCE_EVENT_TYPES.items():
            with self.subTest(claim_type=claim_type):
                self.assertNotIn("ToolResult:", prefixes)
                self.assertFalse(
                    any(prefix.startswith("ToolResult:") for prefix in prefixes)
                )

    def test_rp_17_the_hypothesis_path_is_untouched(self) -> None:
        linkage = evidence_linkage(
            predicted_status="proposed",
            claim_type="general_hypothesis",
            supporting_event_types=("ToolResult:fetched-weather",),
        )
        self.assertEqual("not_applicable", linkage)

        payload = bundle(propose("general_hypothesis", {"project_id": "A"}, links=(supports(),)))
        decision = classify_impact(payload, evidence_linkage_value=linkage)
        self.assertEqual("E0", decision.level)

        policy = BASE_POLICY.evaluate(
            payload, state(decision, predicted_status="proposed")
        )
        self.assertEqual("accept", policy.recommendation)
        self.assertFalse(policy.requires_human)
        self.assertEqual("provisional_hypothesis", policy.support_basis)


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
