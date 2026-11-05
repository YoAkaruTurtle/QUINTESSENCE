"""S7b: the cognition-change service end to end.

Every test drives the real commit path: one ``commit_command`` that appends the
bundle, the review-decision row and the provenance edges.  No model call, no
network, synthetic data only.
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
    DirectiveBinding,
    commit_change,  # noqa: F401  (imported for API completeness)
)
from quintessence_pcs.collaboration.policy import CommitAuthority  # noqa: E402
from quintessence_pcs.foundation.errors import AuthorizationRequired, InvalidInput  # noqa: E402
from quintessence_pcs.store.collaboration import Approval  # noqa: E402
from quintessence_pcs.store.governance import GovernanceAccess, GovernanceStore  # noqa: E402


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


def governance_access(permissions: frozenset[str] | None = None) -> GovernanceAccess:
    return GovernanceAccess(
        namespace=NS,
        host_instance="synthetic-host",
        operator="operator-a",
        access_ref="access:governance",
        permissions=permissions or frozenset({"governance.admin", "governance.read"}),
    )


class ChangeServiceTestCase(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: self.now_string()
        )
        self.governance = GovernanceStore(self.store)

    def now_string(self) -> str:
        return "2026-10-17T12:00:00Z"

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
        plan_bundle = bundle or self.hypothesis_bundle()
        result = self.changes.propose(
            access(),
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            bundle=plan_bundle,
        )
        return result, context

    def candidate(self, candidate_id: str):
        return self.store.load_artifact(
            namespace=NS, artifact_id=candidate_id, artifact_version=1
        )

    def record_count(self) -> int:
        return int(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM record_versions"
            ).fetchone()[0]
        )

    def decision_rows(self, candidate_id: str):
        return [
            tuple(row)
            for row in self.store.connection.execute(
                "SELECT decision, impact, reason_code, authority_kind, commit_request_id "
                "FROM cognition_review_decisions WHERE candidate_id = ? "
                "ORDER BY decision_seq",
                (candidate_id,),
            )
        ]

    def grant_authorization(
        self,
        *,
        claim_types: tuple[str, ...] = ("general_hypothesis",),
        scope: dict | None = None,
        max_impact: str = "E0",
    ) -> str:
        authorization_id = str(uuid.uuid4())
        self.governance.grant_commit_authorization(
            governance_access(),
            authorization_id=authorization_id,
            host_instance="synthetic-host",
            access_ref="access:synthetic",
            operations=("cognition.change",),
            claim_types=claim_types,
            scope=scope or {"scene": "test.scene"},
            max_impact=max_impact,
            max_changes_per_bundle=2,
            max_targets_per_bundle=2,
            policy_id="policy:cognition.auto.v2",
            policy_version="1",
            authorization_source="host_config",
            issued_at="2026-10-17T00:00:00Z",
        )
        return authorization_id

    # ------------------------------------------------------------- CS-01

    def test_cs_01_propose_persists_a_workless_candidate(self) -> None:
        before = self.record_count()
        result, _ = self.propose()
        self.assertEqual("PROPOSED", result.state)
        candidate = self.candidate(result.candidate_id)
        self.assertEqual("candidate", candidate.kind)
        self.assertIsNone(candidate.work_id)
        self.assertEqual("pending", candidate.status)
        self.assertEqual(before, self.record_count())

    # ------------------------------------------------------------- CS-02

    def test_cs_02_review_marks_a_provisional_bundle_auto_eligible(self) -> None:
        result, _ = self.propose()
        reviewed = self.changes.review(
            access(), candidate_id=result.candidate_id, decision_id=str(uuid.uuid4())
        )
        self.assertEqual("PROPOSED", reviewed.state)
        self.assertEqual("E0", reviewed.impact)
        self.assertFalse(reviewed.requires_human)
        self.assertEqual([], self.decision_rows(result.candidate_id))

    # ------------------------------------------------------------- CS-03

    def test_cs_03_commit_with_auto_authorization(self) -> None:
        authorization_id = self.grant_authorization()
        result, _ = self.propose()
        before = self.record_count()
        committed = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(auto_authorization_id=authorization_id),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", committed.state)
        self.assertIsNotNone(committed.receipt)
        self.assertEqual(1, len(committed.record_refs))
        self.assertEqual(before + 1, self.record_count())
        self.assertEqual("committed", self.candidate(result.candidate_id).status)
        rows = self.decision_rows(result.candidate_id)
        self.assertEqual(1, len(rows))
        self.assertEqual("committed", rows[0][0])
        self.assertEqual("auto_authorization", rows[0][3])
        provenance = self.store.connection.execute(
            "SELECT COUNT(*) FROM cognition_record_decisions"
        ).fetchone()[0]
        self.assertEqual(1, provenance)

    # ------------------------------------------------------------- CS-04

    def test_cs_04_commit_without_authority_is_refused(self) -> None:
        result, _ = self.propose()
        before = self.record_count()
        refused = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(approval=None, grant_id=None,
                                      auto_authorization_id=str(uuid.uuid4())),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        self.assertEqual(before, self.record_count())
        rows = self.decision_rows(result.candidate_id)
        self.assertEqual("not_authorized", rows[0][0])

    # ------------------------------------------------------------- CS-05

    def test_cs_05_high_impact_waits_for_human_then_commits(self) -> None:
        bundle = self.hypothesis_bundle(scope={})
        result, _ = self.propose(bundle)
        pending = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(auto_authorization_id=str(uuid.uuid4())),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("AWAITING_HUMAN", pending.state)
        self.assertEqual("awaiting_human", self.decision_rows(result.candidate_id)[0][0])

        candidate = self.candidate(result.candidate_id)
        approval = Approval(
            candidate_id=candidate.artifact_id,
            proposal_digest=candidate.proposal_digest,
            context_id=candidate.context_id,
            request_id=candidate.request_id,
        )
        committed = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(approval=approval),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", committed.state)
        self.assertEqual("manual_approval", committed.authority_kind)

    # ------------------------------------------------------------- CS-07

    def test_cs_07_directive_binding_commits_without_reapproval(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        body = self.belief_body(
            claim="the user prefers synthetic blue",
            claim_type="user_preference",
            strength="limited",
        )
        bundle = {
            "summary": "bound preference",
            "changes": [
                {
                    "op": "propose_belief",
                    "body": body,
                    "scope": {"scene": "test.scene"},
                }
            ],
        }
        result, _ = self.propose(bundle)
        binding = DirectiveBinding(
            namespace=NS,
            host_instance="synthetic-host",
            statement_ref=self.event_ref(statement.event_id),
            statement_digest="sha256:" + str(self.store.event_payload_digest(NS, statement.event_id)),
            directive_kind="state_preference",
            target={"project_id": "synthetic"},
            allowed_claim_types=frozenset({"user_preference"}),
            decision_scope={"scene": "test.scene"},
            bound_at="2026-10-17T12:00:00Z",
            binding_policy_id="policy:cognition.directive.v1",
            binding_policy_version="1",
            access_ref="access:synthetic",
        )
        committed = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(directive=binding),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", committed.state)
        self.assertEqual("user_directive", committed.authority_kind)

    # ------------------------------------------------------------- CS-08

    def test_cs_08_replaying_a_commit_returns_the_stored_receipt(self) -> None:
        authorization_id = self.grant_authorization()
        result, _ = self.propose()
        first = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(auto_authorization_id=authorization_id),
            decision_id=str(uuid.uuid4()),
        )
        second = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(auto_authorization_id=authorization_id),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", second.state)
        self.assertEqual(first.receipt.committed_at, second.receipt.committed_at)
        self.assertEqual(1, len(self.decision_rows(result.candidate_id)))

    # ------------------------------------------------------------ CS-12

    def test_cs_12_blocked_bundle_is_refused_even_with_approval(self) -> None:
        authorization_id = self.grant_authorization()
        premise, _ = self.propose()
        committed = self.changes.commit(
            access(),
            candidate_id=premise.candidate_id,
            authority=CommitAuthority(auto_authorization_id=authorization_id),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", committed.state)
        current = self.store.get_current(NS, committed.record_refs[0]["record_id"])
        premise_ref = self.record_ref(current)
        body = self.belief_body(
            claim="synthetic rule",
            claim_type="derived_rule",
            premise_refs=[premise_ref],
        )
        bundle = {
            "summary": "rule publication",
            "changes": [
                {"op": "propose_belief", "body": body, "scope": {"scene": "test.scene"}}
            ],
        }
        result, _ = self.propose(bundle)
        before = self.record_count()
        reviewed = self.changes.review(
            access(), candidate_id=result.candidate_id, decision_id=str(uuid.uuid4())
        )
        self.assertEqual("DEFERRED", reviewed.state)
        rows = self.decision_rows(result.candidate_id)
        self.assertEqual("BLOCKED", rows[0][1])
        self.assertEqual("rule_publication_not_supported", rows[0][2])

        candidate = self.candidate(result.candidate_id)
        approval = Approval(
            candidate_id=candidate.artifact_id,
            proposal_digest=candidate.proposal_digest,
            context_id=candidate.context_id,
            request_id=candidate.request_id,
        )
        refused = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(approval=approval),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("DEFERRED", refused.state)
        self.assertEqual(before, self.record_count())
        self.assertEqual("BLOCKED", self.decision_rows(result.candidate_id)[-1][1])

    # ------------------------------------------------------------ CS-14

    def test_cs_14_ordinary_review_still_requires_a_work_binding(self) -> None:
        context = self.build_context()
        statement = self.ingest()
        with self.assertRaises(InvalidInput):
            self.store.register_interaction(
                namespace=NS,
                mode="review",
                activation_kind="manual_review",
                host_instance="synthetic-host",
                access_ref="access:synthetic",
                policy_ref="policy:collab.v1",
                context_id=context.context_id,
                trigger_ref=self.event_ref(statement.event_id),
            )


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
