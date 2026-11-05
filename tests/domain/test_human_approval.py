"""S10a: the ``ApprovalAction`` envelope is consumed, not just defined.

Every test drives the real service: propose a candidate, let it reach
``AWAITING_HUMAN``, then approve or reject it the way J07's trusted host would —
with an envelope carrying an approval scope, an expiry and the user statement it
was derived from.  No model call, no network, synthetic data only.
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
    ApprovalAction,
    ChangeAccess,
    CognitionChangeService,
)
from quintessence_pcs.collaboration.policy import CommitAuthority  # noqa: E402
from quintessence_pcs.store.collaboration import Approval  # noqa: E402
from quintessence_pcs.store.governance import GovernanceStore  # noqa: E402

NOW = "2026-10-17T12:00:00Z"
LATER = "2026-10-17T13:00:00Z"


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


class ApprovalActionTestCase(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: NOW
        )
        self.governance = GovernanceStore(self.store)

    # ------------------------------------------------------------- helpers

    def bundle(self) -> dict:
        """A global hypothesis: unambiguously E2, so it can only be approved."""

        body = self.belief_body(
            claim="synthetic hypothesis about a mechanism",
            claim_type="general_hypothesis",
            strength="limited",
        )
        return {
            "summary": "one synthetic hypothesis",
            "changes": [
                {"op": "propose_belief", "body": body, "scope": {}},
            ],
        }

    def awaiting_candidate(self) -> str:
        # The user statement must exist *before* the Context is captured: it is
        # the source the approval will point at, and a later event would move
        # the Context watermark out from under the candidate.
        self._statement = self.statement_ref()
        context = self.build_context()
        proposed = self.changes.propose(
            access(),
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            bundle=self.bundle(),
        )
        self.assertEqual("PROPOSED", proposed.state)
        blocked = self.changes.commit(
            access(),
            candidate_id=proposed.candidate_id,
            authority=CommitAuthority(auto_authorization_id=str(uuid.uuid4())),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("AWAITING_HUMAN", blocked.state)
        return proposed.candidate_id

    def action(
        self,
        candidate_id: str,
        *,
        decision: str = "approve",
        approval_scope: dict | None = None,
        expires_at: str = LATER,
        statement_ref: dict | None = None,
        proposal_digest: str | None = None,
        artifact_version: int = 1,
    ) -> ApprovalAction:
        candidate = self.store.load_artifact(
            namespace=NS, artifact_id=candidate_id, artifact_version=1
        )
        return ApprovalAction(
            approval_id=str(uuid.uuid4()),
            candidate_id=candidate_id,
            artifact_version=artifact_version,
            proposal_digest=proposal_digest or candidate.proposal_digest,
            decision=decision,
            approval_scope=approval_scope
            if approval_scope is not None
            else {
                "operations": ["propose_belief"],
                "claim_types": ["general_hypothesis"],
                "scope": {},
            },
            statement_ref=statement_ref or self._bound_statement(),
            approved_at=NOW,
            expires_at=expires_at,
            access_ref="access:synthetic",
        )

    def statement_ref(self) -> dict:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        return self.event_ref(statement.event_id)

    def _bound_statement(self) -> dict:
        try:
            return self._statement
        except AttributeError:
            self._statement = self.statement_ref()
            return self._statement

    def decisions(self, candidate_id: str) -> list[tuple]:
        return [
            tuple(row)
            for row in self.store.connection.execute(
                "SELECT decision, reason_code, authority_kind, approval_id "
                "FROM cognition_review_decisions WHERE candidate_id = ? "
                "ORDER BY decision_seq",
                (candidate_id,),
            )
        ]

    def record_count(self) -> int:
        return int(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM record_versions"
            ).fetchone()[0]
        )

    # ------------------------------------------------------------- HA-01

    def test_ha_01_a_correct_approval_commits(self) -> None:
        candidate = self.awaiting_candidate()
        action = self.action(candidate)
        result = self.changes.commit(
            access(), candidate_id=candidate, authority=action,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", result.state)
        self.assertEqual("manual_approval", result.authority_kind)
        self.assertIsNotNone(result.receipt)
        committed = [
            row for row in self.decisions(candidate) if row[0] == "committed"
        ]
        self.assertEqual(1, len(committed))
        self.assertEqual(action.approval_id, committed[0][3])

    # ------------------------------------------------------------- HA-02

    def test_ha_02_scope_without_the_claim_type_is_refused(self) -> None:
        candidate = self.awaiting_candidate()
        before = self.record_count()
        action = self.action(
            candidate,
            approval_scope={
                "operations": ["propose_belief"],
                "claim_types": ["capability"],
                "scope": {},
            },
        )
        result = self.changes.commit(
            access(), candidate_id=candidate, authority=action,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("NOT_AUTHORIZED", result.state)
        self.assertEqual("approval_scope_insufficient", result.reason_code)
        self.assertEqual(before, self.record_count())
        self.assertEqual("not_authorized", self.decisions(candidate)[-1][0])

    # ------------------------------------------------------------- HA-03

    def test_ha_03_an_expired_approval_is_refused(self) -> None:
        candidate = self.awaiting_candidate()
        before = self.record_count()
        action = self.action(candidate, expires_at="2026-10-17T11:00:00Z")
        result = self.changes.commit(
            access(), candidate_id=candidate, authority=action,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("CONFLICT", result.state)
        self.assertEqual("approval_expired", result.reason_code)
        self.assertEqual(before, self.record_count())
        self.assertEqual("approval_expired", self.decisions(candidate)[-1][0])

    # ------------------------------------------------------------- HA-04

    def test_ha_04_a_digest_mismatch_is_refused(self) -> None:
        candidate = self.awaiting_candidate()
        before = self.record_count()
        action = self.action(
            candidate, proposal_digest="sha256:" + "0" * 64
        )
        result = self.changes.commit(
            access(), candidate_id=candidate, authority=action,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("CONFLICT", result.state)
        self.assertEqual("approval_superseded", result.reason_code)
        self.assertEqual(before, self.record_count())

    # ------------------------------------------------------------- HA-05

    def test_ha_05_a_rejection_is_a_terminal_decision(self) -> None:
        candidate = self.awaiting_candidate()
        before = self.record_count()
        action = self.action(candidate, decision="reject")
        result = self.changes.commit(
            access(), candidate_id=candidate, authority=action,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("REJECTED", result.state)
        self.assertEqual(before, self.record_count())
        self.assertEqual("rejected", self.decisions(candidate)[-1][0])
        self.assertEqual("rejected", self.governance.lifecycle(NS, candidate))

        later = self.changes.commit(
            access(), candidate_id=candidate, authority=self.action(candidate),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("CONFLICT", later.state)
        self.assertEqual("candidate_rejected", later.reason_code)

    # ------------------------------------------------------------- HA-06

    def test_ha_06_the_same_approval_replays_instead_of_committing_twice(self) -> None:
        candidate = self.awaiting_candidate()
        action = self.action(candidate)
        first = self.changes.commit(
            access(), candidate_id=candidate, authority=action,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", first.state)
        records = self.record_count()
        second = self.changes.commit(
            access(), candidate_id=candidate, authority=action,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", second.state)
        self.assertEqual(first.receipt.request_id, second.receipt.request_id)
        self.assertEqual(records, self.record_count())

    # ------------------------------------------------------------- HA-07

    def test_ha_07_a_candidate_past_awaiting_human_is_conflict(self) -> None:
        candidate = self.awaiting_candidate()
        # The first decision is awaiting_human; a second rejection replaces the
        # pending state, and a later approval must not slip past it.
        rejected = self.changes.commit(
            access(), candidate_id=candidate,
            authority=self.action(candidate, decision="reject"),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("REJECTED", rejected.state)
        outcome = self.changes.commit(
            access(), candidate_id=candidate, authority=self.action(candidate),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("CONFLICT", outcome.state)

    # ------------------------------------------------------------- HA-08

    def test_ha_08_approval_does_not_raise_the_derived_strength(self) -> None:
        candidate = self.awaiting_candidate()
        result = self.changes.commit(
            access(), candidate_id=candidate, authority=self.action(candidate),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", result.state)
        self.assertEqual("proposed", result.derived_status)
        self.assertEqual(1, len(result.record_refs))
        version = self.store.get_current(NS, result.record_refs[0]["record_id"])
        self.assertEqual("proposed", version.status)
        body = version.body
        self.assertEqual("insufficient", body["strength"])


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
