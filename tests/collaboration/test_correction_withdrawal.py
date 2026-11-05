"""RG05-F02: a corrected pending preference candidate is withdrawn.

The withdrawal is an append-only ``rejected`` decision: the old candidate can
never be approved later, its lifecycle becomes ``rejected``, and a repeated
withdrawal is idempotent.  A committed candidate is never withdrawn.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOMAIN = HERE.parent / "domain"
SRC = HERE.parents[1] / "src"
for _path in (HERE, DOMAIN, SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.collaboration.change_service import (  # noqa: E402
    ChangeAccess,
    CognitionChangeService,
)
from quintessence_pcs.foundation.errors import AuthorizationRequired  # noqa: E402
from quintessence_pcs.store.errors import CandidateTransitionDenied  # noqa: E402
from quintessence_pcs.store.governance import GovernanceStore  # noqa: E402


def access(**overrides: object) -> ChangeAccess:
    values: dict[str, object] = {
        "namespace": NS,
        "host_instance": "synthetic-host",
        "operator": "operator-a",
        "access_ref": "access:synthetic",
        "permissions": frozenset(
            {"change.propose", "change.review", "change.commit", "change.result"}
        ),
        "privacy_classes": frozenset({"private"}),
        "purposes": frozenset({"cognition.change"}),
    }
    values.update(overrides)
    return ChangeAccess(**values)  # type: ignore[arg-type]


class CorrectionWithdrawalTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: "2026-10-17T12:00:00Z"
        )
        self.governance = GovernanceStore(self.store)

    def pending_preference(self):
        body = self.belief_body(
            claim="用户喜欢简短的回答",
            claim_type="user_preference",
            strength="limited",
            review_condition="expression_preference:length=concise",
        )
        bundle = {
            "summary": "synthetic pending preference",
            "changes": [
                {"op": "propose_belief", "body": body, "scope": {"scene": "test.scene"}}
            ],
        }
        context = self.build_context()
        result = self.changes.propose(
            access(),
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            bundle=bundle,
        )
        self.assertEqual("PROPOSED", result.state)
        return result

    def test_pending_candidate_is_withdrawn_and_cannot_be_approved(self) -> None:
        result = self.pending_preference()
        self.assertEqual("pending", self.governance.lifecycle(NS, result.candidate_id))
        withdrawn = self.changes.withdraw_candidate(
            access(),
            candidate_id=result.candidate_id,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("REJECTED", withdrawn.state)
        self.assertEqual(
            "superseded_by_user_correction", withdrawn.reason_code
        )
        self.assertEqual("rejected", self.governance.lifecycle(NS, result.candidate_id))
        rows = [
            tuple(row)
            for row in self.store.connection.execute(
                "SELECT decision, reason_code, requires_human, authority_kind "
                "FROM cognition_review_decisions WHERE candidate_id = ?",
                (result.candidate_id,),
            ).fetchall()
        ]
        self.assertEqual(1, len(rows))
        self.assertEqual(("rejected", "superseded_by_user_correction", 0, "none"), rows[0])
        # A second withdrawal is idempotent and writes no second decision.
        again = self.changes.withdraw_candidate(
            access(),
            candidate_id=result.candidate_id,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("REJECTED", again.state)
        count = self.store.connection.execute(
            "SELECT COUNT(*) FROM cognition_review_decisions WHERE candidate_id = ?",
            (result.candidate_id,),
        ).fetchone()[0]
        self.assertEqual(1, int(count))

    def test_withdrawing_a_non_pending_candidate_is_refused(self) -> None:
        from unittest.mock import patch

        result = self.pending_preference()
        with patch.object(
            self.changes._governance, "lifecycle", return_value="committed"
        ):
            with self.assertRaises(CandidateTransitionDenied):
                self.changes.withdraw_candidate(
                    access(),
                    candidate_id=result.candidate_id,
                    decision_id=str(uuid.uuid4()),
                )

    def test_withdrawal_requires_review_permission(self) -> None:
        result = self.pending_preference()
        with self.assertRaises(AuthorizationRequired):
            self.changes.withdraw_candidate(
                access(permissions=frozenset({"change.propose"})),
                candidate_id=result.candidate_id,
                decision_id=str(uuid.uuid4()),
            )


if __name__ == "__main__":
    unittest.main()
