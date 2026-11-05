"""RG08 R-E: the settle helper never auto-commits an E2 decision."""

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
    settle_candidate,
)


class SettleCandidateTests(CognitionTestCase):
    def access(self) -> ChangeAccess:
        return ChangeAccess(
            namespace=NS,
            host_instance="host:rg08-r-e-settle",
            operator="operator:rg08-r-e-settle",
            access_ref="authorization:rg08-r-e-settle",
            permissions=frozenset({"change.propose", "change.review", "change.commit"}),
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"cognition.change"}),
        )

    def test_global_scope_is_e2_and_is_not_committed(self) -> None:
        context = self.build_context()
        changes = CognitionChangeService(self.store, beliefs=self.beliefs)
        body = self.belief_body(
            claim="a synthetic global hypothesis",
            claim_type="general_hypothesis",
            strength="limited",
        )
        result = changes.propose(
            self.access(),
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            bundle={
                "summary": "synthetic E2",
                "changes": [
                    {
                        "op": "propose_belief",
                        "scope": {},
                        "body": body,
                    }
                ],
            },
            activation_kind="manual_review",
        )
        settled = settle_candidate(
            changes,
            self.access(),
            candidate_id=result.candidate_id,
            review_decision_id=str(uuid.uuid4()),
            auto_authorization_id=None,
        )
        self.assertEqual(settled.state, "AWAITING_HUMAN", settled)
        self.assertTrue(settled.requires_human)
        count = self.store.connection.execute(
            "SELECT COUNT(*) FROM record_versions WHERE namespace = ? "
            "AND kind = 'belief'",
            (NS,),
        ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
