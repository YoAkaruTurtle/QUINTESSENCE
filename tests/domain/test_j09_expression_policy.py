"""J09 WP2: what a piece of input may honestly become, without widening policy.

The registry is the contract: ``review_policy.EVIDENCE_EVENT_TYPES`` says which
event type may support which claim type.  These tests pin the expression policy
to that table (AC-16) and pin D1's line between a *direct* statement and a
*relayed* report (AC-15): the first is never downgraded for lack of a citation,
the second never becomes a fact.
"""

from __future__ import annotations

import unittest

from quintessence_pcs.collaboration.review_policy import EVIDENCE_EVENT_TYPES
from quintessence_pcs.domains.cognition.beliefs import CLAIM_TYPES
from quintessence_pcs.domains.cognition.expression_policy import (
    INPUT_CLASSES,
    ROUTE_CLAIM,
    ROUTE_DECISION,
    ROUTE_NONE,
    ROUTE_QUESTION,
    decide,
    eligible_claim_types,
    evidence_types_for,
)


class RegistryContractTests(unittest.TestCase):
    """AC-16: the policy may not loosen the evidence registry."""

    def test_every_claim_type_keeps_its_registered_sources(self) -> None:
        for claim_type in CLAIM_TYPES:
            self.assertEqual(
                evidence_types_for(claim_type),
                tuple(EVIDENCE_EVENT_TYPES[claim_type]),
                f"{claim_type} drifted from the registry",
            )

    def test_project_decision_is_a_source_not_a_claim(self) -> None:
        self.assertIn("project_decision", EVIDENCE_EVENT_TYPES)
        self.assertNotIn("project_decision", CLAIM_TYPES)

    def test_eligible_types_are_the_exact_inverse(self) -> None:
        self.assertEqual(eligible_claim_types(("WebPageFetched:abc",)), ("external_report",))
        self.assertEqual(eligible_claim_types(("UserStatement:abc",)), ("user_preference",))
        self.assertEqual(
            eligible_claim_types(("CapabilityTested:abc",)), ("capability",)
        )
        # Evidence-driven types never appear: they require no source at all.
        self.assertNotIn("general_hypothesis", eligible_claim_types(()))
        self.assertNotIn("derived_rule", eligible_claim_types(("UserStatement:x",)))

    def test_an_unknown_evidence_type_supports_nothing(self) -> None:
        self.assertEqual(eligible_claim_types(("SomethingElse:x",)), ())


class DirectFactTests(unittest.TestCase):
    """D1: a direct statement keeps its own eligibility; no automatic downgrade."""

    def test_a_direct_fact_with_observation_becomes_its_own_claim(self) -> None:
        decision = decide(
            "direct_fact", evidence_event_types=("EnvironmentChanged:1",)
        )
        self.assertEqual(decision.route, ROUTE_CLAIM)
        self.assertEqual(decision.claim_type, "environment_state")

    def test_a_direct_fact_without_evidence_stays_a_question(self) -> None:
        for evidence in ((), ("UserStatement:1",)):
            decision = decide("direct_fact", evidence_event_types=evidence)
            self.assertEqual(decision.route, ROUTE_QUESTION, evidence)
            self.assertIsNone(decision.claim_type)

    def test_a_direct_fact_is_never_turned_into_a_hypothesis(self) -> None:
        decision = decide("direct_fact")
        self.assertNotEqual(decision.claim_type, "general_hypothesis")
        self.assertFalse(decision.requires_provenance)


class RelayedReportTests(unittest.TestCase):
    """D1: only a relay worth keeping takes the low-strength route."""

    def test_a_relay_kept_for_recall_becomes_a_limited_hypothesis(self) -> None:
        decision = decide("relayed_report", keep_relay=True)
        self.assertEqual(decision.route, ROUTE_CLAIM)
        self.assertEqual(decision.claim_type, "general_hypothesis")
        self.assertEqual(decision.strength, "limited")
        self.assertTrue(decision.requires_provenance)

    def test_a_relay_not_worth_keeping_is_dropped(self) -> None:
        decision = decide("relayed_report", keep_relay=False)
        self.assertEqual(decision.route, ROUTE_NONE)
        self.assertIsNone(decision.claim_type)

    def test_a_relay_never_becomes_an_external_report(self) -> None:
        decision = decide(
            "relayed_report", keep_relay=True, evidence_event_types=("UserStatement:1",)
        )
        self.assertNotEqual(decision.claim_type, "external_report")


class OtherInputTests(unittest.TestCase):
    def test_a_preference_with_a_user_statement_is_a_preference(self) -> None:
        decision = decide("preference", evidence_event_types=("UserStatement:1",))
        self.assertEqual(decision.claim_type, "user_preference")

    def test_a_preference_without_a_statement_is_a_question(self) -> None:
        self.assertEqual(decide("preference").route, ROUTE_QUESTION)

    def test_a_project_decision_is_a_decision(self) -> None:
        decision = decide("project_decision")
        self.assertEqual(decision.route, ROUTE_DECISION)
        self.assertIsNone(decision.claim_type)

    def test_a_correction_follows_its_evidence(self) -> None:
        self.assertEqual(
            decide("correction", evidence_event_types=("UserStatement:1",)).claim_type,
            "user_preference",
        )
        self.assertEqual(decide("correction").route, ROUTE_QUESTION)

    def test_an_unknown_class_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            decide("guess")

    def test_every_class_has_a_defined_behaviour(self) -> None:
        for input_class in INPUT_CLASSES:
            decision = decide(input_class)
            self.assertIn(
                decision.route,
                (ROUTE_CLAIM, ROUTE_DECISION, ROUTE_QUESTION, ROUTE_NONE),
                input_class,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
