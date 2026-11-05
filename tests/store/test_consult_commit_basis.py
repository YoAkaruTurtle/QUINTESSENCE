"""RG05 decision 01: a consult Context as the cognition commit basis.

Synthetic data only.  No model, no network, no private asset.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOMAIN = HERE.parent / "domain"
for _path in (HERE, DOMAIN):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import NS, NOW, CognitionTestCase  # noqa: E402

from quintessence_pcs.collaboration.change_service import (  # noqa: E402
    CHANGE_POLICY_REF,
    ApprovalAction,
    ChangeAccess,
    CognitionChangeService,
)
from quintessence_pcs.collaboration.policy import CommitAuthority  # noqa: E402
from quintessence_pcs.collaboration.review_policy import (  # noqa: E402
    BASE_POLICY_ID,
    BASE_POLICY_VERSION,
)
from quintessence_pcs.foundation.errors import (  # noqa: E402
    ContextStale,
    InvalidInput,
    Redacted,
)
from quintessence_pcs.store import CognitionStore  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402
from quintessence_pcs.store.collaboration import Approval  # noqa: E402
from quintessence_pcs.store.governance import (  # noqa: E402
    CHANGE_OPERATION,
    GovernanceAccess,
    GovernanceStore,
)

HOST = "host:synthetic-rg05-commit"
ACCESS_REF = "authorization:access:rg05-commit"
SCOPE = {"project_id": "rg05.lab"}


class ConsultCommitBasisTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, clock=lambda: "2026-10-17T12:00:00Z"
        )
        self.governance = GovernanceStore(self.store)
        self.experience = ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
        )

    # ------------------------------------------------------------- fixtures

    def change_access(self, permissions=("change.propose", "change.review", "change.commit")):
        return ChangeAccess(
            namespace=NS,
            host_instance=HOST,
            operator="operator:synthetic",
            access_ref=ACCESS_REF,
            permissions=frozenset(permissions),
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"cognition.change"}),
        )

    def body(self, claim: str, claim_type: str = "general_hypothesis") -> dict:
        basis = "inductive" if claim_type == "general_hypothesis" else "direct_report"
        return {
            "claim": claim,
            "claim_type": claim_type,
            "evidence_links": [],
            "strength": "insufficient",
            "strength_reason": "synthetic seed without evidence",
            "inference_basis": basis,
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": "synthetic review condition",
        }

    def consult_context(self):
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="合成陈述：记一下这条偏好。",
            privacy_class="private",
            retention="standard",
        )
        ref = {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": row.content_version,
        }
        self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:collab.v1",
            input_refs=[ref],
        )
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.experience,
            input_refs=[ref],
            adapter_kind="direct-http",
        )
        return context, ref

    def propose(self, context, claim: str = "合成假设偏好"):
        return self.changes.propose(
            self.change_access(),
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            bundle={
                "operation": "cognition.change",
                "summary": "synthetic",
                "changes": [
                    {"op": "propose_belief", "body": self.body(claim), "scope": dict(SCOPE)}
                ],
            },
        )

    def grant(self, *, claim_types=("general_hypothesis",), max_impact="E1"):
        return self.governance.grant_commit_authorization(
            GovernanceAccess(
                namespace=NS,
                host_instance=HOST,
                operator="operator:synthetic",
                access_ref=ACCESS_REF,
                permissions=frozenset({"governance.admin"}),
            ),
            authorization_id=str(uuid.uuid4()),
            host_instance=HOST,
            access_ref=ACCESS_REF,
            operations=(CHANGE_OPERATION,),
            claim_types=tuple(claim_types),
            scope=dict(SCOPE),
            max_impact=max_impact,
            max_changes_per_bundle=2,
            max_targets_per_bundle=2,
            policy_id=BASE_POLICY_ID,
            policy_version=BASE_POLICY_VERSION,
            authorization_source="host_config",
            issued_at="2026-10-17T00:00:00Z",
            issued_by="operator:synthetic",
        )

    # ---------------------------------------------------------------- checks

    def test_chat_candidate_is_carried_by_a_consult_interaction(self) -> None:
        context, input_ref = self.consult_context()
        result = self.propose(context)
        artifact = self.store.load_artifact(
            namespace=NS, artifact_id=result.candidate_id, artifact_version=1
        )
        self.assertEqual("consult", self.store.interaction(
            namespace=NS, interaction_id=artifact.interaction_id
        ).mode)
        self.assertEqual(CHANGE_POLICY_REF, self.store.interaction(
            namespace=NS, interaction_id=artifact.interaction_id
        ).policy_ref)
        # Decision 01: the candidate covers the Context's rendered input.
        refs = {ref.get("ref_type") for ref in artifact.source_refs}
        self.assertIn("collab_input", refs)
        self.assertIn(
            (input_ref["input_id"], input_ref["content_version"]),
            {
                (ref.get("input_id"), ref.get("content_version"))
                for ref in artifact.source_refs
                if ref.get("ref_type") == "collab_input"
            },
        )

    def test_a_review_interaction_still_requires_its_trigger(self) -> None:
        context, _ = self.consult_context()
        with self.assertRaises(InvalidInput):
            self.store.register_interaction(
                namespace=NS,
                mode="review",
                activation_kind="user_input",
                host_instance=HOST,
                access_ref=ACCESS_REF,
                policy_ref=CHANGE_POLICY_REF,
                context_id=context.context_id,
                trigger_ref=None,
                work_id=str(uuid.uuid4()),
                work_generation=1,
            )

    def test_narrow_grant_commits_and_persists_a_belief(self) -> None:
        context, _ = self.consult_context()
        authorization = self.grant()
        result = self.propose(context)
        reviewed = self.changes.review(
            self.change_access(),
            candidate_id=result.candidate_id,
            decision_id=str(uuid.uuid4()),
        )
        self.assertIn(reviewed.state, ("PROPOSED",))
        revision_before = self.store.current_state_revision(NS)
        committed = self.changes.commit(
            self.change_access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(
                auto_authorization_id=authorization.authorization_id
            ),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", committed.state)
        self.assertGreater(self.store.current_state_revision(NS), revision_before)
        self.assertEqual(
            1,
            len(
                [
                    version
                    for version in self.store.current_versions_of_kind(NS, "belief")
                    if version.body.get("claim") == "合成假设偏好"
                ]
            ),
        )

    def test_deleting_the_input_blocks_the_commit(self) -> None:
        context, input_ref = self.consult_context()
        authorization = self.grant()
        result = self.propose(context)
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=input_ref["input_id"],
            ref_version=input_ref["content_version"],
            authorization_ref=ACCESS_REF,
        )
        with self.assertRaises(ContextStale):
            self.changes.commit(
                self.change_access(),
                candidate_id=result.candidate_id,
                authority=CommitAuthority(
                    auto_authorization_id=authorization.authorization_id
                ),
                decision_id=str(uuid.uuid4()),
            )

    def test_grant_outside_its_claim_type_cannot_commit(self) -> None:
        context, _ = self.consult_context()
        authorization = self.grant(claim_types=("capability",))
        result = self.propose(context)
        committed = self.changes.commit(
            self.change_access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(
                auto_authorization_id=authorization.authorization_id
            ),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("NOT_AUTHORIZED", committed.state)

    def test_a_consult_context_passes_the_commit_guard(self) -> None:
        from quintessence_pcs.domains.cognition.context_guard import (
            require_live_context,
        )

        context, _ = self.consult_context()
        snapshot = require_live_context(
            self.store, context_id=context.context_id, namespace=NS
        )
        self.assertEqual(context.context_id, snapshot.context_id)
        self.assertFalse(snapshot.trigger_ref)


if __name__ == "__main__":
    unittest.main()


class ConsultCommitBoundaryTests(ConsultCommitBasisTests):
    """Supplement 01: the flag is an entry, never an authority substitute."""

    def second_context(self):
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="另一条合成陈述。",
            privacy_class="private",
            retention="standard",
        )
        ref = {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": row.content_version,
        }
        self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:collab.v1",
            input_refs=[ref],
        )
        return self.contexts.build_consult_context(
            namespace=NS,
            access=self.experience,
            input_refs=[ref],
            adapter_kind="direct-http",
        )

    def test_an_approval_bound_to_another_context_is_not_authorized(self) -> None:
        context, _ = self.consult_context()
        other = self.second_context()
        result = self.propose(context)
        artifact = self.store.load_artifact(
            namespace=NS, artifact_id=result.candidate_id, artifact_version=1
        )
        wrong = Approval(
            candidate_id=artifact.artifact_id,
            proposal_digest=str(artifact.proposal_digest),
            context_id=other.context_id,
            request_id=str(artifact.request_id),
        )
        outcome = self.changes.commit(
            self.change_access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(approval=wrong),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("NOT_AUTHORIZED", outcome.state)

    def approval_action(self, candidate_id: str, version: int, digest: str, decision: str):
        receipt = self.ingest(
            source_kind="user_report",
            event_type="UserStatement:review_decision",
            payload={"decision": decision, "candidate_id": candidate_id},
        )
        self._last_statement_event = receipt.event_id
        return ApprovalAction(
            approval_id=str(uuid.uuid4()),
            candidate_id=candidate_id,
            artifact_version=version,
            proposal_digest=digest,
            decision=decision,
            approval_scope={
                "operations": ["propose_belief"],
                "claim_types": ["user_preference"],
                "scope": dict(SCOPE),
            },
            statement_ref={
                "ref_type": "event",
                "namespace": NS,
                "event_id": receipt.event_id,
            },
            approved_at="2026-10-17T12:00:00Z",
            expires_at="2026-10-17T13:00:00Z",
            access_ref=ACCESS_REF,
        )

    def propose_preference(self, context):
        return self.changes.propose(
            self.change_access(),
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            bundle={
                "operation": "cognition.change",
                "summary": "synthetic preference",
                "changes": [
                    {
                        "op": "propose_belief",
                        "body": self.body("合成用户偏好：简短回答", "user_preference"),
                        "scope": dict(SCOPE),
                    }
                ],
            },
        )

    def test_approval_action_approves_and_rejects(self) -> None:
        context, _ = self.consult_context()
        result = self.propose_preference(context)
        reviewed = self.changes.review(
            self.change_access(),
            candidate_id=result.candidate_id,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("AWAITING_HUMAN", reviewed.state)
        artifact = self.store.load_artifact(
            namespace=NS, artifact_id=result.candidate_id, artifact_version=1
        )
        approved = self.changes.commit(
            self.change_access(),
            candidate_id=result.candidate_id,
            authority=self.approval_action(
                result.candidate_id, 1, str(artifact.proposal_digest), "approve"
            ),
            decision_id=str(uuid.uuid4()),
            appended_events=(self._last_statement_event,),
        )
        self.assertEqual("COMMITTED", approved.state)
        self.assertEqual(
            "committed", self.governance.lifecycle(NS, result.candidate_id)
        )

        second = self.propose_preference(context)
        self.changes.review(
            self.change_access(),
            candidate_id=second.candidate_id,
            decision_id=str(uuid.uuid4()),
        )
        artifact = self.store.load_artifact(
            namespace=NS, artifact_id=second.candidate_id, artifact_version=1
        )
        rejected = self.changes.commit(
            self.change_access(),
            candidate_id=second.candidate_id,
            authority=self.approval_action(
                second.candidate_id, 1, str(artifact.proposal_digest), "reject"
            ),
            decision_id=str(uuid.uuid4()),
            appended_events=(self._last_statement_event,),
        )
        self.assertEqual("REJECTED", rejected.state)
        self.assertEqual(
            "rejected", self.governance.lifecycle(NS, second.candidate_id)
        )

    def test_pending_candidate_survives_a_restart(self) -> None:
        context, _ = self.consult_context()
        result = self.propose_preference(context)
        self.store.close()
        reopened = CognitionStore.open(self.db_path)
        self.addCleanup(reopened.close)
        rows = reopened.pending_change_candidates(NS, limit=20)
        self.assertEqual([result.candidate_id], [row["artifact_id"] for row in rows])
        self.assertEqual(
            "pending", GovernanceStore(reopened).lifecycle(NS, result.candidate_id)
        )
