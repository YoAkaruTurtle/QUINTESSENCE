"""S12: the remaining assertion gaps from the S9 acceptance matrix.

Adds **only** assertions: no ``src/**`` change.  Where the dispatch card named a
reason code the implementation does not emit, the test asserts the *actual*
contract and the divergence is reported to the coordinator rather than hidden
behind a weaker assertion.
"""

from __future__ import annotations

import hashlib
import inspect
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
)
from quintessence_pcs.collaboration.facade import REVIEW_STATES, ReviewView  # noqa: E402
from quintessence_pcs.collaboration.policy import CommitAuthority  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    ContextStale,
    InvalidInput,
    StateRevisionConflict,
)
from quintessence_pcs.store.collaboration import Approval  # noqa: E402
from quintessence_pcs.store.governance import GovernanceAccess, GovernanceStore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "src/quintessence_pcs/resources/migrations"


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


def governance_access() -> GovernanceAccess:
    return GovernanceAccess(
        namespace=NS,
        host_instance="synthetic-host",
        operator="operator-a",
        access_ref="access:governance",
        permissions=frozenset({"governance.admin", "governance.read"}),
    )


def _run_public_closure(case: "S12TestCase") -> None:
    """Walk propose -> review -> commit using only the service surface."""

    authorization = case.grant_authorization(
        claim_types=("general_hypothesis",), max_impact="E0"
    )
    result = case.propose(case.hypothesis_bundle())
    case.changes.review(
        access(), candidate_id=result.candidate_id, decision_id=str(uuid.uuid4())
    )
    committed = case.commit(
        result.candidate_id, CommitAuthority(auto_authorization_id=authorization)
    )
    assert committed.state == "COMMITTED", committed.state


class S12TestCase(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: "2026-10-17T12:00:00Z"
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

    def preference_bundle(self, claim: str) -> dict:
        body = self.belief_body(
            claim=claim, claim_type="user_preference", strength="limited"
        )
        return {
            "summary": "synthetic preference",
            "changes": [
                {
                    "op": "propose_belief",
                    "body": body,
                    "scope": {"scene": "test.scene"},
                }
            ],
        }

    def propose(self, bundle: dict, *, event_id: str | None = None):
        trigger = event_id or self.ingest().event_id
        context = self.build_context(trigger)
        return self.changes.propose(
            access(),
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            bundle=bundle,
        )

    def candidate(self, candidate_id: str):
        return self.store.load_artifact(
            namespace=NS, artifact_id=candidate_id, artifact_version=1
        )

    def record_count(self) -> int:
        return self.count_rows("record_versions")

    def decision_rows(self, candidate_id: str):
        return [
            tuple(row)
            for row in self.store.connection.execute(
                "SELECT decision, impact, reason_code, authority_kind, "
                "directive_digest, approval_id, change_fingerprint "
                "FROM cognition_review_decisions WHERE candidate_id = ? "
                "ORDER BY decision_seq",
                (candidate_id,),
            )
        ]

    def grant_authorization(
        self,
        *,
        claim_types: tuple[str, ...] = ("user_preference",),
        scope: dict | None = None,
        max_impact: str = "E1",
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

    def binding(self, statement_event_id: str, *, scope: dict | None = None):
        return DirectiveBinding(
            namespace=NS,
            host_instance="synthetic-host",
            statement_ref=self.event_ref(statement_event_id),
            statement_digest="sha256:" + str(
                self.store.event_payload_digest(NS, statement_event_id)
            ),
            directive_kind="state_preference",
            target={"project_id": "synthetic"},
            allowed_claim_types=frozenset({"user_preference"}),
            decision_scope={"scene": "test.scene"} if scope is None else scope,
            bound_at="2026-10-17T12:00:00Z",
            binding_policy_id="policy:cognition.directive.v1",
            binding_policy_version="1",
            access_ref="access:synthetic",
        )

    def commit(self, candidate_id: str, authority):
        return self.changes.commit(
            access(),
            candidate_id=candidate_id,
            authority=authority,
            decision_id=str(uuid.uuid4()),
        )

    def approval_action(self, candidate_id: str) -> ApprovalAction:
        candidate = self.candidate(candidate_id)
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        return ApprovalAction(
            approval_id=str(uuid.uuid4()),
            candidate_id=candidate_id,
            artifact_version=1,
            proposal_digest=candidate.proposal_digest,
            decision="approve",
            approval_scope={
                "operations": ["propose_belief"],
                "claim_types": ["general_hypothesis"],
                "scope": {},
            },
            statement_ref=self.event_ref(statement.event_id),
            approved_at="2026-10-17T12:00:00Z",
            expires_at="2026-10-18T12:00:00Z",
            access_ref="access:synthetic",
        )

    # ------------------------------------------------------------- H3-2

    def test_h3_2_commit_authority_branches_are_mutually_exclusive(self) -> None:
        approval = Approval(
            candidate_id=str(uuid.uuid4()),
            proposal_digest="sha256:" + "1" * 64,
            context_id=str(uuid.uuid4()),
            request_id=str(uuid.uuid4()),
        )
        with self.assertRaises(InvalidInput):
            CommitAuthority(approval=approval, grant_id=str(uuid.uuid4()))
        with self.assertRaises(InvalidInput):
            CommitAuthority(
                auto_authorization_id=str(uuid.uuid4()), directive=object()
            )
        with self.assertRaises(InvalidInput):
            CommitAuthority()

    def test_h3_2_a_non_binding_directive_is_refused_at_commit(self) -> None:
        result = self.propose(self.preference_bundle("synthetic preference"))
        before = self.record_count()
        with self.assertRaises(InvalidInput):
            self.commit(result.candidate_id, CommitAuthority(directive=object()))
        self.assertEqual(before, self.record_count())

    # ------------------------------------------------------------- A2

    def test_a2_unknown_directive_kinds_are_refused_at_construction(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        for kind in ("quoted", "hypothetical", "", "state_preference "):
            with self.subTest(kind=kind):
                with self.assertRaises(InvalidInput):
                    DirectiveBinding(
                        namespace=NS,
                        host_instance="synthetic-host",
                        statement_ref=self.event_ref(statement.event_id),
                        statement_digest="sha256:" + "0" * 64,
                        directive_kind=kind,
                        target={"project_id": "synthetic"},
                        allowed_claim_types=frozenset({"user_preference"}),
                        decision_scope={"scene": "test.scene"},
                        bound_at="2026-10-17T12:00:00Z",
                        binding_policy_id="policy:cognition.directive.v1",
                        binding_policy_version="1",
                        access_ref="access:synthetic",
                    )

    # ------------------------------------------------------------- A2b

    def test_a2b_a_binding_needs_a_user_report_statement(self) -> None:
        inference = self.ingest(
            source_kind="model_inference", event_type="UserStatement"
        )
        result = self.propose(self.preference_bundle("synthetic preference"))
        before = self.record_count()
        refused = self.commit(
            result.candidate_id,
            CommitAuthority(directive=self.binding(inference.event_id)),
        )
        # S13: the frozen contract (v0.5 §9.6) names this check instead of
        # collapsing every refusal into "not_authorized".
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        self.assertEqual("directive_source_not_user_report", refused.reason_code)
        self.assertEqual("none", refused.authority_kind)
        self.assertEqual(before, self.record_count())
        self.assertEqual("pending", self.candidate(result.candidate_id).status)

    def test_a2b_an_empty_decision_scope_is_refused_at_construction(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        with self.assertRaises(InvalidInput):
            self.binding(statement.event_id, scope={})

    # ------------------------------------------------------------- H3-3/H3-4

    def test_h3_3_a_binding_whose_scope_excludes_the_candidate_is_refused(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        result = self.propose(self.preference_bundle("synthetic preference"))
        before = self.record_count()
        refused = self.commit(
            result.candidate_id,
            CommitAuthority(
                directive=self.binding(statement.event_id, scope={"scene": "other"})
            ),
        )
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        # S13: the row now names the scope check itself (§9.6), rather than the
        # generic "not_authorized" the earlier implementation wrote.
        self.assertEqual("directive_scope_mismatch", refused.reason_code)
        self.assertEqual("none", refused.authority_kind)
        self.assertEqual(before, self.record_count())
        self.assertEqual("pending", self.candidate(result.candidate_id).status)

    def test_h3_4_a_binding_that_does_not_cover_the_claim_type_is_refused(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        body = self.belief_body(
            claim="synthetic observation",
            claim_type="environment_state",
            strength="limited",
            valid_until="2027-01-01T00:00:00Z",
        )
        bundle = {
            "summary": "uncovered claim type",
            "changes": [
                {
                    "op": "propose_belief",
                    "body": body,
                    "scope": {"scene": "test.scene"},
                }
            ],
        }
        result = self.propose(bundle)
        before = self.record_count()
        refused = self.commit(
            result.candidate_id,
            CommitAuthority(directive=self.binding(statement.event_id)),
        )
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        # S13: the claim-type refusal has its own frozen code (§9.6).
        self.assertEqual("directive_claim_type_forbidden", refused.reason_code)
        self.assertEqual("none", refused.authority_kind)
        self.assertEqual(before, self.record_count())

    # ------------------------------------------------------------- A1

    def test_a1_retract_and_replace_under_one_preference_binding(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        binding = self.binding(statement.event_id)

        first = self.propose(self.preference_bundle("the user prefers synthetic blue"))
        committed = self.commit(first.candidate_id, CommitAuthority(directive=binding))
        self.assertEqual("COMMITTED", committed.state)
        old_version = self.store.get_current(
            NS, committed.record_refs[0]["record_id"]
        )
        old_ref = self.record_ref(old_version)

        body = self.belief_body(
            claim="the user prefers synthetic green",
            claim_type="user_preference",
            strength="limited",
        )
        bundle = {
            "summary": "replace the bound preference",
            "changes": [
                {
                    "op": "retract_belief",
                    "target": old_ref,
                    "evidence_links": [
                        self.evidence_link(self.event_ref(statement.event_id))
                    ],
                    "reason": "the user restated the preference",
                },
                {
                    "op": "propose_belief",
                    "body": body,
                    "scope": {"scene": "test.scene"},
                },
            ],
        }
        second = self.propose(bundle)
        replaced = self.commit(second.candidate_id, CommitAuthority(directive=binding))
        self.assertEqual("COMMITTED", replaced.state)
        self.assertEqual(2, len(replaced.record_refs))
        self.assertEqual(
            "retracted",
            self.store.get_version(
                NS, old_ref["record_id"], old_ref["record_version"] + 1
            ).status,
        )
        new_version = self.store.get_current(NS, replaced.record_refs[1]["record_id"])
        self.assertEqual({"scene": "test.scene"}, dict(new_version.scope or {}))

    # ------------------------------------------------------------- C2-2

    def test_c2_2_a_new_decision_id_records_a_new_attempt_row(self) -> None:
        result = self.propose(self.hypothesis_bundle(scope={}))
        before = self.record_count()
        first = self.changes.review(
            access(), candidate_id=result.candidate_id, decision_id=str(uuid.uuid4())
        )
        second = self.changes.review(
            access(), candidate_id=result.candidate_id, decision_id=str(uuid.uuid4())
        )
        self.assertEqual("AWAITING_HUMAN", first.state)
        self.assertEqual("AWAITING_HUMAN", second.state)
        rows = self.decision_rows(result.candidate_id)
        self.assertEqual(2, len(rows))
        self.assertEqual(["awaiting_human", "awaiting_human"], [row[0] for row in rows])
        self.assertEqual(before, self.record_count())

    # ------------------------------------------------------------- C6

    def test_c6_a_consistent_but_wrong_binding_is_only_shape_checked(self) -> None:
        statement = self.ingest(
            source_kind="user_report",
            event_type="UserStatement",
            payload={"text": "translate the phrase 'approve the change' into Japanese"},
        )
        binding = self.binding(statement.event_id)
        result = self.propose(self.preference_bundle("synthetic preference"))
        committed = self.commit(result.candidate_id, CommitAuthority(directive=binding))
        self.assertEqual("COMMITTED", committed.state)
        self.assertEqual("user_directive", committed.authority_kind)
        self.assertEqual(binding.digest, self.decision_rows(result.candidate_id)[0][4])
        self.assertFalse(
            any(
                "semantic" in name or "verified" in name
                for name in type(committed).__dataclass_fields__
            )
        )

    # ------------------------------------------------------------- NL-01

    def test_nl01_an_approval_action_for_another_candidate_commits_nothing(self) -> None:
        trigger = self.ingest().event_id
        target = self.propose(self.hypothesis_bundle(scope={}), event_id=trigger)
        other = self.propose(self.hypothesis_bundle(scope={}), event_id=trigger)
        for candidate in (target, other):
            self.changes.review(
                access(),
                candidate_id=candidate.candidate_id,
                decision_id=str(uuid.uuid4()),
            )
        before = self.record_count()
        action = self.approval_action(other.candidate_id)
        refused = self.changes.commit(
            access(),
            candidate_id=target.candidate_id,
            authority=action,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("CONFLICT", refused.state)
        self.assertEqual("approval_superseded", refused.reason_code)
        self.assertEqual(before, self.record_count())
        self.assertEqual("pending", self.candidate(target.candidate_id).status)
        self.assertEqual("pending", self.candidate(other.candidate_id).status)

    # ------------------------------------------------------------- I3

    def test_i3_review_states_and_view_shape_are_unchanged(self) -> None:
        self.assertEqual(
            (
                "structural_only",
                "pending",
                "committed",
                "waiting",
                "rejected",
                "stale",
                "denied",
                "failed",
                "incomplete",
            ),
            REVIEW_STATES,
        )
        self.assertEqual(
            (
                "state",
                "interaction_id",
                "candidate_id",
                "receipt",
                "redacted",
                "detail",
                "model_adapter",
            ),
            tuple(ReviewView.__dataclass_fields__),
        )

    def test_i3_frozen_migrations_keep_their_bytes(self) -> None:
        expected = {
            "0011_memory_delivery.sql": (
                "45e2c75763cc781ed5760deeec28eff2543005027f45ba226f2ec0852c5c331b"
            ),
            "0012_bridge_gaps.sql": (
                "443ce926eaf0367c8ca345801dc4e5509936f1b455b72dd83735f41628594ef6"
            ),
        }
        for name, digest in expected.items():
            with self.subTest(migration=name):
                actual = hashlib.sha256((MIGRATIONS / name).read_bytes()).hexdigest()
                self.assertEqual(digest, actual)

    # ------------------------------------------------------------- I1

    def test_i1_the_public_closure_path_needs_no_store_calls(self) -> None:
        source = inspect.getsource(_run_public_closure)
        for forbidden in ("connection.execute(", ".transaction(", "commit_command("):
            self.assertNotIn(forbidden, source)
        _run_public_closure(self)

    def test_i1_the_change_service_never_reads_question_evaluation_history(self) -> None:
        module = sys.modules["quintessence_pcs.collaboration.change_service"]
        self.assertNotIn("question.evaluate", inspect.getsource(module))

    # ------------------------------------------------------------- B3

    def test_b3_state_revision_and_event_seq_move_independently(self) -> None:
        trigger = self.ingest().event_id
        authorization = self.grant_authorization(
            claim_types=("general_hypothesis",), max_impact="E0"
        )
        first = self.propose(self.hypothesis_bundle(), event_id=trigger)
        second = self.propose(self.hypothesis_bundle(), event_id=trigger)
        committed = self.commit(
            second.candidate_id,
            CommitAuthority(auto_authorization_id=authorization),
        )
        self.assertEqual("COMMITTED", committed.state)
        with self.assertRaises(StateRevisionConflict):
            self.commit(
                first.candidate_id,
                CommitAuthority(auto_authorization_id=authorization),
            )

        third = self.propose(self.hypothesis_bundle(), event_id=trigger)
        self.ingest()
        with self.assertRaises(ContextStale):
            self.commit(
                third.candidate_id,
                CommitAuthority(auto_authorization_id=authorization),
            )

    def test_b3_a_privacy_epoch_move_alone_also_stales_the_context(self) -> None:
        context = self.build_context()
        throwaway = self.beliefs.propose_belief(
            namespace=NS,
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            body=self.belief_body(
                claim="throwaway", claim_type="general_hypothesis", strength="limited"
            ),
            scope={"scene": "test.scene"},
        )
        authorization = self.grant_authorization(
            claim_types=("general_hypothesis",), max_impact="E0"
        )
        result = self.propose(self.hypothesis_bundle())
        epoch_before = self.watermarks()[2]
        self.delete_record(throwaway.record_id)
        self.assertGreater(self.watermarks()[2], epoch_before)
        with self.assertRaises(ContextStale):
            self.commit(
                result.candidate_id,
                CommitAuthority(auto_authorization_id=authorization),
            )

    # ------------------------------------------------------------- B5

    def test_b5_deleting_a_context_source_blocks_the_commit(self) -> None:
        trigger = self.ingest().event_id
        authorization = self.grant_authorization(
            claim_types=("general_hypothesis",), max_impact="E0"
        )
        result = self.propose(self.hypothesis_bundle(), event_id=trigger)
        before = self.record_count()
        self.delete_event(trigger)
        with self.assertRaises(ContextStale):
            self.commit(
                result.candidate_id,
                CommitAuthority(auto_authorization_id=authorization),
            )
        self.assertEqual(before, self.record_count())

    def test_b5_a_late_candidate_loses_to_the_newer_version(self) -> None:
        trigger = self.ingest().event_id
        authorization = self.grant_authorization(
            claim_types=("general_hypothesis",), max_impact="E0"
        )
        first = self.propose(self.hypothesis_bundle(), event_id=trigger)
        committed = self.commit(
            first.candidate_id,
            CommitAuthority(auto_authorization_id=authorization),
        )
        self.assertEqual("COMMITTED", committed.state)
        current = self.store.get_current(NS, committed.record_refs[0]["record_id"])

        revision = {
            "summary": "revise the stored hypothesis",
            "changes": [
                {
                    "op": "revise_belief",
                    "target": self.record_ref(current),
                    "body": self.belief_body(
                        claim="synthetic hypothesis about a mechanism",
                        claim_type="general_hypothesis",
                        strength="limited",
                    ),
                    "scope": {"scene": "test.scene"},
                }
            ],
        }
        late = self.propose(revision, event_id=trigger)

        # A later bundle retracts the target the late candidate still names.
        retraction = {
            "summary": "retract the stored hypothesis",
            "changes": [
                {
                    "op": "retract_belief",
                    "target": self.record_ref(current),
                    "evidence_links": [self.evidence_link(self.event_ref(trigger))],
                    "reason": "synthetic retraction",
                }
            ],
        }
        withdraw = self.propose(retraction, event_id=trigger)
        withdrawn = self.commit(
            withdraw.candidate_id,
            CommitAuthority(auto_authorization_id=authorization),
        )
        self.assertEqual("COMMITTED", withdrawn.state)
        self.assertEqual(
            "retracted",
            self.store.get_current(NS, current.record_id).status,
        )

        before = self.record_count()
        # The stale basis is refused before any new version is written.  The
        # card calls this "the target moved"; the observable refusal is the
        # state-revision conflict, which is the same guard.
        with self.assertRaises(StateRevisionConflict):
            self.commit(
                late.candidate_id,
                CommitAuthority(auto_authorization_id=authorization),
            )
        self.assertEqual(before, self.record_count())
        self.assertEqual(
            "retracted",
            self.store.get_current(NS, current.record_id).status,
        )


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
