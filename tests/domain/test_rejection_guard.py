"""S11a: the rejection fingerprint guard.

A bundle whose fingerprint was *rejected* inside the guard window must not be
silently re-submitted.  These tests seed real ``rejected`` decision rows through
the same ``TxScope`` seam the service uses, then drive the real ``review`` path.

The guard is deliberately a window, not a ban: RG-04 records the known limitation
that changing the scope produces a new fingerprint, and RG-03 proves the window
expires.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.collaboration.change_service import (  # noqa: E402
    ChangeAccess,
    CognitionChangeService,
)
from quintessence_pcs.collaboration.review_policy import change_fingerprint  # noqa: E402
from quintessence_pcs.domains.cognition.changes import validate_change_bundle  # noqa: E402
from quintessence_pcs.store.governance import GovernanceStore  # noqa: E402

T0 = "2026-10-17T12:00:00Z"


def access() -> ChangeAccess:
    return ChangeAccess(
        namespace=NS,
        host_instance="synthetic-host",
        operator="operator-a",
        access_ref="access:synthetic",
        permissions=frozenset(
            {"change.propose", "change.review", "change.commit", "change.result"}
        ),
        privacy_classes=frozenset({"private"}),
        purposes=frozenset({"cognition.change"}),
    )


class RejectionGuardTests(CognitionTestCase):
    """RG-01..05 -- the guard fires only for a rejected fingerprint in window."""

    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: T0
        )
        self.governance = GovernanceStore(self.store)

    # ------------------------------------------------------------- helpers

    def hypothesis_bundle(self, *, scope: dict | None = None) -> dict:
        body = self.belief_body(
            claim="synthetic hypothesis about a mechanism",
            claim_type="general_hypothesis",
            strength="limited",
        )
        return {
            "summary": "one synthetic hypothesis",
            "changes": [
                {
                    "op": "propose_belief",
                    "body": body,
                    "scope": {"scene": "test.scene"} if scope is None else scope,
                }
            ],
        }

    def fingerprint_of(self, bundle: dict) -> str:
        return change_fingerprint(validate_change_bundle(NS, bundle))

    def record_count(self) -> int:
        return int(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM record_versions"
            ).fetchone()[0]
        )

    def next_decision_seq(self) -> int:
        row = self.store.connection.execute(
            "SELECT COALESCE(MAX(decision_seq), 0) + 1 "
            "FROM cognition_review_decisions WHERE namespace = ?",
            (NS,),
        ).fetchone()
        return int(row[0])

    def seed_decision(
        self,
        *,
        fingerprint: str,
        decision: str = "rejected",
        decided_at: str = T0,
        reason_code: str = "synthetic_rejection",
    ) -> None:
        """Write one decision row straight through the S4 TxScope seam."""

        context = self.build_context()
        fields = {
            "decision_id": str(uuid.uuid4()),
            "decision_seq": self.next_decision_seq(),
            "candidate_id": str(uuid.uuid4()),
            "artifact_version": 1,
            "decision": decision,
            "reason_code": reason_code,
            "impact": "E2",
            "derived_status": "proposed",
            "support_basis": "provisional_hypothesis",
            "requires_human": 1,
            "evidence_linkage": "not_applicable",
            "policy_id": "policy:cognition.review.base.v2",
            "policy_version": "1",
            "impact_policy_id": "policy:cognition.risk.v1",
            "impact_policy_version": "1",
            "authority_kind": "none",
            "change_digest": "sha256:" + "a" * 64,
            "change_fingerprint": fingerprint,
            "decision_request_id": str(uuid.uuid4()),
            "decided_at": decided_at,
        }

        def apply(scope) -> None:
            scope.record_review_decision(namespace=NS, fields=fields)

        self.store.commit_command(
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            operation="cognition.decision",
            request_payload={"decision_id": fields["decision_id"]},
            apply=apply,
            namespace=NS,
        )

    def review_bundle(self, bundle: dict) -> tuple[str, list[tuple]]:
        context = self.build_context()
        proposed = self.changes.propose(
            access(),
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            bundle=bundle,
        )
        candidate_id = proposed.candidate_id
        reviewed = self.changes.review(
            access(), candidate_id=candidate_id, decision_id=str(uuid.uuid4())
        )
        rows = [
            tuple(row)
            for row in self.store.connection.execute(
                "SELECT decision, impact, reason_code FROM cognition_review_decisions "
                "WHERE candidate_id = ? ORDER BY decision_seq",
                (candidate_id,),
            )
        ]
        return reviewed.state, rows

    # --------------------------------------------------------------- RG-01

    def test_rg_01_a_rejected_fingerprint_inside_the_window_is_escalated(self) -> None:
        bundle = self.hypothesis_bundle()
        self.seed_decision(fingerprint=self.fingerprint_of(bundle))

        records_before = self.record_count()
        state, rows = self.review_bundle(bundle)

        self.assertEqual("ESCALATED", state)
        self.assertEqual(1, len(rows))
        decision, impact, reason = rows[0]
        self.assertEqual("escalated", decision)
        self.assertEqual("E2", impact)
        self.assertEqual("previously_rejected", reason)
        self.assertEqual(records_before, self.record_count())

    # --------------------------------------------------------------- RG-02

    def test_rg_02_a_different_fingerprint_is_not_affected(self) -> None:
        self.seed_decision(fingerprint="b" * 64)

        state, rows = self.review_bundle(self.hypothesis_bundle())

        self.assertEqual("PROPOSED", state)
        self.assertEqual([], rows)

    # --------------------------------------------------------------- RG-03

    def test_rg_03_the_window_expires(self) -> None:
        bundle = self.hypothesis_bundle()
        # T0 is 2026-10-17; 30 days earlier is 2026-09-17.  2026-08-01 is outside.
        self.seed_decision(
            fingerprint=self.fingerprint_of(bundle), decided_at="2026-08-01T00:00:00Z"
        )

        state, rows = self.review_bundle(bundle)

        self.assertEqual("PROPOSED", state)
        self.assertEqual([], rows)

    # --------------------------------------------------------------- RG-04

    def test_rg_04_changing_the_scope_escapes_the_guard(self) -> None:
        """Known limitation: a re-submission under a new scope has a new
        fingerprint, so the guard does not see it.  This is recorded, not
        celebrated -- the contract never claimed whole-bundle anti-splitting."""

        original = self.hypothesis_bundle(scope={"scene": "test.scene"})
        self.seed_decision(fingerprint=self.fingerprint_of(original))

        moved = self.hypothesis_bundle(scope={"scene": "test.other-scene"})
        self.assertNotEqual(
            self.fingerprint_of(original), self.fingerprint_of(moved)
        )

        state, rows = self.review_bundle(moved)
        self.assertEqual("PROPOSED", state)
        self.assertEqual([], rows)

    # --------------------------------------------------------------- RG-05

    def test_rg_05_a_non_terminal_decision_does_not_arm_the_guard(self) -> None:
        bundle = self.hypothesis_bundle()
        self.seed_decision(
            fingerprint=self.fingerprint_of(bundle),
            decision="deferred",
            reason_code="insufficient_basis",
        )

        state, rows = self.review_bundle(bundle)

        self.assertEqual("PROPOSED", state)
        self.assertEqual([], rows)


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
