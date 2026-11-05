"""S11b: the acceptance gaps that had no dedicated assertion.

Each test names the matrix row it closes.  Where a limitation cannot be removed
without changing frozen semantics the test asserts the *current* behaviour and
says so in its docstring -- a known gap, not a product promise.
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
from test_change_service import access, governance_access  # noqa: E402

from quintessence_pcs.collaboration.change_service import (  # noqa: E402
    CognitionChangeService,
    QUESTION_OPERATIONS,
)
from quintessence_pcs.collaboration.policy import CommitAuthority  # noqa: E402
from quintessence_pcs.domains.cognition.changes import CHANGE_OPS  # noqa: E402
from quintessence_pcs.foundation.errors import CorruptStore  # noqa: E402
from quintessence_pcs.store.errors import CandidateTransitionDenied  # noqa: E402
from quintessence_pcs.store.governance import GovernanceStore  # noqa: E402

T0 = "2026-10-17T12:00:00Z"

#: Operations that would move data or authority outside the cognition plane.
FORBIDDEN_OP_FRAGMENTS = (
    "file",
    "fs.",
    "network",
    "http",
    "egress",
    "permission",
    "grant",
    "delete",
    "shell",
    "exec",
)


class S11GapTests(CognitionTestCase):
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

    def propose(self, bundle: dict | None = None):
        context = self.build_context()
        return self.changes.propose(
            access(),
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            bundle=bundle or self.hypothesis_bundle(),
        )

    def grant(self, *, max_impact: str = "E0") -> str:
        authorization_id = str(uuid.uuid4())
        self.governance.grant_commit_authorization(
            governance_access(),
            authorization_id=authorization_id,
            host_instance="synthetic-host",
            access_ref="access:synthetic",
            operations=("cognition.change",),
            claim_types=("general_hypothesis",),
            scope={"scene": "test.scene"},
            max_impact=max_impact,
            max_changes_per_bundle=2,
            max_targets_per_bundle=2,
            policy_id="policy:cognition.auto.v2",
            policy_version="1",
            authorization_source="host_config",
            issued_at="2026-10-17T00:00:00Z",
        )
        return authorization_id

    def next_decision_seq(self) -> int:
        row = self.store.connection.execute(
            "SELECT COALESCE(MAX(decision_seq), 0) + 1 "
            "FROM cognition_review_decisions WHERE namespace = ?",
            (NS,),
        ).fetchone()
        return int(row[0])

    def seed_decision(self, candidate_id: str, *, decision: str) -> None:
        context = self.build_context()
        fields = {
            "decision_id": str(uuid.uuid4()),
            "decision_seq": self.next_decision_seq(),
            "candidate_id": candidate_id,
            "artifact_version": 1,
            "decision": decision,
            "reason_code": "synthetic",
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
            "decision_request_id": str(uuid.uuid4()),
            "decided_at": T0,
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

    # ------------------------------------------------------------- H5B-1

    def test_h5b_1_a_written_rejection_blocks_a_later_commit(self) -> None:
        """H5B-1: the decision row is authoritative even before the candidate
        status cache catches up -- the commit path re-reads the lifecycle."""

        authorization_id = self.grant()
        result = self.propose()
        self.seed_decision(result.candidate_id, decision="rejected")

        self.assertEqual(
            "rejected", self.governance.lifecycle(NS, result.candidate_id)
        )
        with self.assertRaises(CandidateTransitionDenied):
            self.changes.commit(
                access(),
                candidate_id=result.candidate_id,
                authority=CommitAuthority(auto_authorization_id=authorization_id),
                decision_id=str(uuid.uuid4()),
            )

    # ------------------------------------------------------------- H5B-2

    def test_h5b_2_only_one_terminal_decision_survives(self) -> None:
        """H5B-2: the two orders are both safe and the first terminal wins.

        SQLite serialises the two write transactions, so the deterministic
        stand-in for "two connections race" is to run each order and assert the
        loser never produces a second terminal row.  True parallel scheduling is
        covered by the serialisation itself, not by this test.
        """

        authorization_id = self.grant()
        first = self.propose()
        self.seed_decision(first.candidate_id, decision="rejected")
        with self.assertRaises(CandidateTransitionDenied):
            self.changes.commit(
                access(),
                candidate_id=first.candidate_id,
                authority=CommitAuthority(auto_authorization_id=authorization_id),
                decision_id=str(uuid.uuid4()),
            )
        rows = self.store.connection.execute(
            "SELECT decision FROM cognition_review_decisions WHERE candidate_id = ?",
            (first.candidate_id,),
        ).fetchall()
        self.assertEqual(["rejected"], [row[0] for row in rows])

        second = self.propose()
        committed = self.changes.commit(
            access(),
            candidate_id=second.candidate_id,
            authority=CommitAuthority(auto_authorization_id=authorization_id),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", committed.state)
        self.seed_decision(second.candidate_id, decision="rejected")
        # A late rejection cannot silently coexist with a commit receipt: the
        # lifecycle fails closed instead of picking a winner.
        with self.assertRaises(CorruptStore):
            self.governance.lifecycle(NS, second.candidate_id)

    # ------------------------------------------------------------- B5

    def test_b5_a_revoked_authorization_is_refused(self) -> None:
        authorization_id = self.grant()
        self.governance.revoke_commit_authorization(
            governance_access(),
            authorization_id=authorization_id,
            reason="synthetic revocation",
        )
        result = self.propose()
        refused = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(auto_authorization_id=authorization_id),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        self.assertEqual("not_authorized", refused.reason_code)
        self.assertEqual(
            "pending", self.governance.lifecycle(NS, result.candidate_id)
        )

    # ------------------------------------------------------------- I5

    def test_i5_a_literal_scope_is_stored_verbatim(self) -> None:
        """I5: J06 never rewrites or normalises an entity literal."""

        bundle = self.hypothesis_bundle(scope={"project_id": "灰雀-47"})
        result = self.propose(bundle)
        candidate = self.store.load_artifact(
            namespace=NS, artifact_id=result.candidate_id, artifact_version=1
        )
        scope = candidate.proposal["changes"][0]["scope"]
        self.assertEqual("灰雀-47", scope["project_id"])

    # ------------------------------------------------------------- EA-05

    def test_ea_05_no_operation_moves_files_network_or_permissions(self) -> None:
        """EA-05: cognition autonomy never implies action authority."""

        for op in (*CHANGE_OPS, *QUESTION_OPERATIONS):
            for fragment in FORBIDDEN_OP_FRAGMENTS:
                self.assertNotIn(
                    fragment,
                    op,
                    f"operation {op!r} looks like an action capability",
                )

    # ------------------------------------------------------------- EA-10

    def test_ea_10_three_provisional_commits_need_no_human(self) -> None:
        authorization_id = self.grant()
        committing = []
        for index in range(3):
            body = self.belief_body(
                claim=f"synthetic provisional judgement {index}",
                claim_type="general_hypothesis",
                strength="limited",
            )
            bundle = {
                "summary": f"provisional {index}",
                "changes": [
                    {"op": "propose_belief", "body": body, "scope": {"scene": "test.scene"}}
                ],
            }
            result = self.propose(bundle)
            committed = self.changes.commit(
                access(),
                candidate_id=result.candidate_id,
                authority=CommitAuthority(auto_authorization_id=authorization_id),
                decision_id=str(uuid.uuid4()),
            )
            self.assertEqual("COMMITTED", committed.state)
            committing.append(result.candidate_id)

        human = self.store.connection.execute(
            "SELECT COUNT(*) FROM cognition_review_decisions "
            "WHERE namespace = ? AND decision = 'awaiting_human'",
            (NS,),
        ).fetchone()[0]
        self.assertEqual(0, human)
        for candidate_id in committing:
            rows = self.store.connection.execute(
                "SELECT COUNT(*) FROM cognition_review_decisions "
                "WHERE candidate_id = ? AND decision = 'committed'",
                (candidate_id,),
            ).fetchone()[0]
            self.assertEqual(1, rows)

    # ------------------------------------------------------------- NL-02b

    def test_nl_02b_a_split_into_a_different_target_is_not_blocked(self) -> None:
        """NL-02b: known gap.  The guard is fingerprint-based, so a rejection of
        target A does not block an equivalent change aimed at target B.  The
        contract never promised whole-bundle anti-splitting; this test records
        the current behaviour instead of pretending otherwise."""

        first = self.propose(self.hypothesis_bundle(scope={"scene": "test.scene"}))
        self.seed_decision(first.candidate_id, decision="rejected")

        other = self.propose(self.hypothesis_bundle(scope={"scene": "test.other-scene"}))
        reviewed = self.changes.review(
            access(), candidate_id=other.candidate_id, decision_id=str(uuid.uuid4())
        )
        self.assertEqual("PROPOSED", reviewed.state)


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
