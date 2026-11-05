"""Closure item C: ``question.reopen`` gets a persistent approval path.

Re-opening a resolved question is E2, so it must not run on its own.  Before
this change the single-operation command simply reported ``AWAITING_HUMAN`` and
wrote nothing: the request was suspended, but there was no candidate to approve,
so "approved then executed" was not supported at all.  The command now registers
an ordinary ``cognition.change`` candidate carrying one ``reopen_question``
change; the existing ``ApprovalAction`` envelope then commits it like any other
E2 candidate.
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
from quintessence_pcs.domains.cognition.questions import (  # noqa: E402
    QuestionAccess,
    QuestionService,
)
from quintessence_pcs.domains.cognition.errors import InvalidTransition  # noqa: E402
from quintessence_pcs.store.governance import (  # noqa: E402
    GovernanceAccess,
    GovernanceStore,
)

QUESTION_PERMISSIONS = frozenset(
    {"question.create", "question.read", "question.transition"}
)


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


def question_access() -> QuestionAccess:
    return QuestionAccess(
        NS,
        "synthetic-host",
        "access:questions",
        frozenset({"private"}),
        QUESTION_PERMISSIONS,
        frozenset({"cognition.question"}),
    )


def governance_access() -> GovernanceAccess:
    return GovernanceAccess(
        namespace=NS,
        host_instance="synthetic-host",
        operator="operator-a",
        access_ref="access:governance",
        permissions=frozenset({"governance.admin", "governance.read"}),
    )


class QuestionReopenTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: "2026-10-17T12:00:00Z"
        )
        self.governance = GovernanceStore(self.store)
        self.questions = QuestionService(self.store)

    # ------------------------------------------------------------- helpers

    def resolved_question(self) -> dict:
        """A Question already in ``resolved`` through the J05 domain service."""

        service = self.questions
        qaccess = question_access()
        origin = self.ingest(source_kind="user_report", event_type="UserStatement")
        evidence = self.ingest(source_kind="host_observation", event_type="TestTrigger")
        created = service.create(
            qaccess,
            context_id=self.store.capture_snapshot(NS).context_id,
            request_id=str(uuid.uuid4()),
            question="what is the synthetic mechanism?",
            origin="user_requested",
            origin_refs=(self.event_ref(origin.event_id),),
            scope={"scene": "test.scene"},
            privacy_class="private",
        )
        resolved = service.transition(
            qaccess,
            context_id=self.store.capture_snapshot(NS).context_id,
            request_id=str(uuid.uuid4()),
            ref=created.view.ref,
            expected_version=created.view.ref.record_version,
            action="resolve",
            reason="synthetic resolution",
            resolution_refs=(self.event_ref(evidence.event_id),),
        )
        self.statement_ref = self.event_ref(origin.event_id)
        return resolved.ref.to_dict()

    def reopen(self, ref: dict, *, request: str | None = None):
        # The new evidence must exist before the Context is captured, or the
        # command would cite a watermark it has already moved.
        evidence = self.ingest(source_kind="host_observation", event_type="TestTrigger")
        context = self.build_context()
        return self.changes.question(
            access(),
            operation="question.reopen",
            context_id=context.context_id,
            request_id=request or str(uuid.uuid4()),
            ref=ref,
            expected_version=ref["record_version"],
            reason="a new synthetic observation",
            new_evidence_refs=(self.event_ref(evidence.event_id),),
            auto_authorization_id=None,
        )

    def approval(self, candidate_id: str, *, digest: str):
        return ApprovalAction(
            approval_id=str(uuid.uuid4()),
            candidate_id=candidate_id,
            artifact_version=1,
            proposal_digest=digest,
            decision="approve",
            approval_scope={
                "operations": ("reopen_question",),
                "claim_types": (),
                "scope": {},
            },
            statement_ref=self.statement_ref,
            approved_at="2026-10-17T12:00:00Z",
            expires_at="2026-10-18T12:00:00Z",
            access_ref="access:synthetic",
        )

    # ------------------------------------------------------------- QR-01

    def test_qr_01_a_resolved_question_gets_a_persistent_candidate(self) -> None:
        ref = self.resolved_question()
        result = self.reopen(ref)
        self.assertEqual("AWAITING_HUMAN", result.state)
        self.assertTrue(result.requires_human)
        self.assertIsNotNone(result.candidate_id)
        self.assertEqual("E2", result.impact)
        self.assertEqual(
            "pending", self.governance.lifecycle(NS, result.candidate_id)
        )
        # Nothing about the Question moved yet.
        version = self.store.get_current(NS, ref["record_id"])
        self.assertEqual("resolved", version.status)

    # ------------------------------------------------------------- QR-02

    def test_qr_02_a_question_that_is_not_resolved_is_refused(self) -> None:
        qaccess = question_access()
        created = self.questions.create(
            qaccess,
            context_id=self.store.capture_snapshot(NS).context_id,
            request_id=str(uuid.uuid4()),
            question="what is another synthetic mechanism?",
            origin="system_proposed",
            scope={"scene": "test.scene"},
            privacy_class="private",
        )
        ref = created.view.ref.to_dict()
        with self.assertRaises(InvalidTransition):
            self.reopen(ref)

    # ------------------------------------------------------------- QR-03

    def test_qr_03_approving_the_candidate_reopens_the_question(self) -> None:
        ref = self.resolved_question()
        result = self.reopen(ref)
        candidate = self.governance.require_current_change_candidate(
            NS, result.candidate_id
        )
        action = self.approval(
            result.candidate_id, digest=candidate.proposal_digest
        )
        committed = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=action,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", committed.state)
        self.assertIsNotNone(committed.receipt)
        version = self.store.get_current(NS, ref["record_id"])
        self.assertEqual("open", version.status)

    # ------------------------------------------------------------- QR-04

    def test_qr_04_without_approval_nothing_moves(self) -> None:
        ref = self.resolved_question()
        before = self.count_rows("record_versions")
        result = self.reopen(ref)
        self.assertEqual("AWAITING_HUMAN", result.state)
        self.assertEqual(before, self.count_rows("record_versions"))
        self.assertEqual("resolved", self.store.get_current(NS, ref["record_id"]).status)

    # ------------------------------------------------------------- QR-05

    def test_qr_05_an_expired_approval_does_not_reopen(self) -> None:
        ref = self.resolved_question()
        result = self.reopen(ref)
        candidate = self.governance.require_current_change_candidate(
            NS, result.candidate_id
        )
        action = self.approval(
            result.candidate_id, digest=candidate.proposal_digest
        )
        stale = ApprovalAction(
            approval_id=action.approval_id,
            candidate_id=action.candidate_id,
            artifact_version=action.artifact_version,
            proposal_digest=action.proposal_digest,
            decision="approve",
            approval_scope=action.approval_scope,
            statement_ref=action.statement_ref,
            approved_at=action.approved_at,
            expires_at="2026-10-16T12:00:00Z",
            access_ref=action.access_ref,
        )
        before = self.count_rows("record_versions")
        refused = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=stale,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("CONFLICT", refused.state)
        self.assertEqual(before, self.count_rows("record_versions"))
        self.assertEqual("resolved", self.store.get_current(NS, ref["record_id"]).status)

    # ------------------------------------------------------------- QR-06

    def test_qr_06_repeating_the_same_approval_does_not_write_twice(self) -> None:
        ref = self.resolved_question()
        result = self.reopen(ref)
        candidate = self.governance.require_current_change_candidate(
            NS, result.candidate_id
        )
        action = self.approval(
            result.candidate_id, digest=candidate.proposal_digest
        )
        self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=action,
            decision_id=str(uuid.uuid4()),
        )
        after_first = self.count_rows("record_versions")
        replay = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=action,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", replay.state)
        self.assertEqual(after_first, self.count_rows("record_versions"))

    # ------------------------------------------------------------- QR-07

    def test_qr_07_the_approval_request_names_the_target(self) -> None:
        ref = self.resolved_question()
        result = self.reopen(ref)
        self.assertIsNotNone(result.approval_request)
        request = result.approval_request
        self.assertEqual(result.candidate_id, request["candidate_id"])
        self.assertEqual("E2", request["impact"])
        self.assertEqual(["reopen_question"], request["changes"])
        self.assertEqual(ref["record_id"], request["targets"][0]["record_id"])
        decisions = [
            row[0]
            for row in self.store.connection.execute(
                "SELECT decision FROM cognition_review_decisions "
                "WHERE candidate_id = ? ORDER BY decision_seq",
                (result.candidate_id,),
            )
        ]
        self.assertEqual(["awaiting_human"], decisions)


if __name__ == "__main__":
    unittest.main()
