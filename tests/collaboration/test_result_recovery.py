"""C06c: result recovery, exact receipt binding and interaction labels.

Every test uses a fresh migrated database and the real PCS2-008 Context
service.  The fixture is the minimal subset of the C06b tests, not a subclass:
the 46 collaboration tests from C06a/C06b must not run again from here.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for _path in (
    REPO_ROOT / "tests" / "domain",
    REPO_ROOT / "tests" / "store",
    REPO_ROOT / "src",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.collaboration import (  # noqa: E402
    DECISION_APPROVE,
    DECISION_REJECT,
    CollaborationService,
    CommitAuthority,
    InteractionResult,
    InteractionRow,
    CandidateState,
)
from quintessence_pcs.foundation.errors import (  # noqa: E402
    CorruptStore,
    RecordNotFound,
    Redacted,
)
from quintessence_pcs.store.collaboration import Approval  # noqa: E402
from quintessence_pcs.store.experience import (  # noqa: E402
    ExperienceAccess,
    ExperienceUnavailable,
    READABILITY_AVAILABLE,
    READABILITY_UNAVAILABLE,
)
from quintessence_pcs.store.model import MODEL_ATTEMPT_RESERVED  # noqa: E402

WORK_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CAND_A = "a1111111-1111-4111-8111-111111111111"
CAND_B = "a2222222-2222-4222-8222-222222222222"
REQ_A = "a1a1a1a1-1111-4111-8111-aaaaaaaaaaaa"
REQ_B = "b2b2b2b2-2222-4222-8222-bbbbbbbbbbbb"
GRANT_COMMIT = "15151515-1515-4515-8515-151515151515"
TS = "2026-10-22T00:00:00Z"
MANIFEST = "sha256:" + "0" * 64


class ResultRecoveryTests(CognitionTestCase):
    """C06c: authoritative receipts, label repair and minimal results."""

    def setUp(self) -> None:
        super().setUp()
        self.service = CollaborationService(
            self.store, belief_service=self.beliefs
        )

    # ------------------------------------------------------------- fixtures

    def context_source_refs(self, context) -> list[dict]:
        refs: list[dict] = []
        for source in context.sources:
            if source.ref_kind == "event":
                refs.append(self.event_ref(source.ref_id))
            elif source.ref_kind == "record":
                refs.append(
                    {
                        "ref_type": "record",
                        "namespace": NS,
                        "record_id": source.ref_id,
                        "record_version": source.ref_version,
                    }
                )
            else:  # pragma: no cover - schema guarantees event/record
                raise AssertionError(
                    f"unexpected context source {source.ref_kind!r}"
                )
        return refs

    def default_proposal(self, event) -> dict:
        return {
            "operation": "propose_belief",
            "body": self.belief_body(
                evidence_links=[
                    self.evidence_link(self.event_ref(event.event_id))
                ]
            ),
            "scope": self.default_scope("capability"),
        }

    def candidate_fields(
        self,
        *,
        event,
        context,
        work_id: str = WORK_ID,
        work_generation: int = 0,
        proposal=None,
        request_id: str = REQ_A,
        artifact_id: str = CAND_A,
        rerun_authorization_ref: str | None = None,
    ) -> dict:
        interaction = self.store.register_interaction(
            namespace=NS,
            mode="review",
            activation_kind="manual_review",
            host_instance="host:synthetic-1",
            access_ref="authorization:access:1",
            policy_ref="policy:collab.v1",
            context_id=context.context_id,
            trigger_ref=self.event_ref(event.event_id),
            work_id=work_id,
            work_generation=work_generation,
        )
        return {
            "interaction_id": interaction,
            "context_id": context.context_id,
            "body": "raw model output for a synthetic candidate",
            "retention": "standard",
            "proposal": (
                self.default_proposal(event)
                if proposal is None
                else proposal
            ),
            "proposal_version": "v1",
            "request_id": request_id,
            "artifact_id": artifact_id,
            "sources": self.context_source_refs(context),
            "rerun_authorization_ref": rerun_authorization_ref,
        }

    def make_candidate(
        self,
        *,
        event=None,
        context=None,
        work_id: str = WORK_ID,
        work_generation: int = 0,
        proposal=None,
        request_id: str = REQ_A,
        artifact_id: str = CAND_A,
    ):
        event = self.ingest() if event is None else event
        context = (
            self.build_context(event.event_id) if context is None else context
        )
        fields = self.candidate_fields(
            event=event,
            context=context,
            work_id=work_id,
            work_generation=work_generation,
            proposal=proposal,
            request_id=request_id,
            artifact_id=artifact_id,
        )
        candidate = self.store.register_candidate(
            namespace=NS,
            work_id=work_id,
            work_generation=work_generation,
            **fields,
        )
        return candidate, context, event

    def approval_for(self, candidate) -> Approval:
        return Approval(
            candidate_id=candidate.artifact_id,
            proposal_digest=candidate.proposal_digest,
            context_id=candidate.context_id,
            request_id=candidate.request_id,
        )

    def approve(self, candidate, *, authority=None):
        if authority is None:
            authority = CommitAuthority(
                approval=self.approval_for(candidate)
            )
        return self.service.resolve_candidate(
            namespace=NS,
            candidate_id=candidate.artifact_id,
            decision=DECISION_APPROVE,
            decision_ref="decision:approve:1",
            authority=authority,
        )

    def reject(self, candidate, *, decision_ref: str = "decision:reject:1"):
        return self.service.resolve_candidate(
            namespace=NS,
            candidate_id=candidate.artifact_id,
            decision=DECISION_REJECT,
            decision_ref=decision_ref,
        )

    def interaction_of(self, interaction_id: str) -> InteractionRow | None:
        return self.store.interaction(
            namespace=NS, interaction_id=interaction_id
        )

    def candidate_status(self, candidate) -> str | None:
        row = self.store.connection.execute(
            "SELECT status FROM collab_artifacts "
            "WHERE namespace = ? AND artifact_id = ? AND artifact_version = 1",
            (NS, candidate.artifact_id),
        ).fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def counts(self) -> dict[str, int]:
        connection = self.store.connection
        return {
            "revision": self.store.current_state_revision(NS),
            "beliefs": int(
                connection.execute(
                    "SELECT COUNT(*) FROM record_versions "
                    "WHERE namespace = ? AND kind = 'belief'",
                    (NS,),
                ).fetchone()[0]
            ),
            "commands": int(
                connection.execute(
                    "SELECT COUNT(*) FROM processed_commands WHERE namespace = ?",
                    (NS,),
                ).fetchone()[0]
            ),
            "contexts": int(
                connection.execute(
                    "SELECT COUNT(*) FROM context_snapshots"
                ).fetchone()[0]
            ),
            "interactions": int(
                connection.execute(
                    "SELECT COUNT(*) FROM collab_interactions"
                ).fetchone()[0]
            ),
            "work_progress": int(
                connection.execute(
                    "SELECT COUNT(*) FROM work_progress"
                ).fetchone()[0]
            ),
        }

    def new_context(self):
        event = self.ingest(event_type="CapabilityObserved:other")
        return self.build_context(event.event_id), event

    def make_review_interaction(
        self,
        *,
        context=None,
        context_id: str | None = None,
        trigger_event=None,
        work_id: str = WORK_ID,
        work_generation: int = 0,
    ) -> tuple:
        if context is None:
            event = (
                self.ingest(event_type="CapabilityObserved:review")
                if trigger_event is None
                else trigger_event
            )
            context = self.build_context(event.event_id)
        else:
            event = trigger_event
        interaction = self.store.register_interaction(
            namespace=NS,
            mode="review",
            activation_kind="manual_review",
            host_instance="host:synthetic-1",
            access_ref="authorization:access:1",
            policy_ref="policy:collab.v1",
            context_id=(
                context.context_id if context_id is None else context_id
            ),
            trigger_ref=self.event_ref(event.event_id),
            work_id=work_id,
            work_generation=work_generation,
        )
        return context, interaction, event

    def wait_receipt(self, context_id: str, *, append: bool = False):
        def apply(tx) -> None:
            if append:
                tx.append_record_version(
                    record_id=str(uuid.uuid4()),
                    record_version=1,
                    kind="Observation",
                    body={"text": "synthetic wait append"},
                )

        return self.store.commit_command(
            context_id=context_id,
            request_id=str(uuid.uuid4()),
            operation="cognition.wait",
            request_payload={"reason": "synthetic wait"},
            apply=apply,
            namespace=NS,
        )

    def belief_receipt_for(
        self,
        candidate,
        *,
        context_id: str | None = None,
        request_id: str | None = None,
        operation: str | None = None,
        payload=None,
    ):
        proposal = candidate.proposal
        return self.store.commit_command(
            context_id=(
                candidate.context_id if context_id is None else context_id
            ),
            request_id=(
                candidate.request_id if request_id is None else request_id
            ),
            operation=(
                proposal["operation"] if operation is None else operation
            ),
            request_payload=(
                proposal if payload is None else payload
            ),
            apply=lambda tx: None,
            namespace=NS,
        )

    def candidate_ref(self, candidate) -> dict:
        return {
            "ref_type": "collab_artifact",
            "namespace": NS,
            "artifact_id": candidate.artifact_id,
            "artifact_version": 1,
        }

    def access(self, privacy_classes: frozenset[str]) -> ExperienceAccess:
        return ExperienceAccess(
            namespace=NS,
            host_instance="host:synthetic-1",
            access_ref="authorization:access:1",
            privacy_classes=privacy_classes,
            purposes=frozenset({"review"}),
        )

    def commit_grant(self, *, work_id: str = WORK_ID, generation: int = 0):
        return {
            "grant_id": GRANT_COMMIT,
            "namespace": NS,
            "schema_version": "collab.v1",
            "kind": "commit",
            "grant_version": 1,
            "subject": "host:synthetic-1",
            "authorization_ref": "authorization:commit:1",
            "issued_at": TS,
            "expires_at": None,
            "revoked_at": None,
            "scope": "work_generation",
            "work_id": work_id,
            "generation": generation,
        }

    def reserve_unknown_attempt(self, context_id: str):
        authorization = self.store.grant_model_authorization(
            namespace=NS,
            authorization_ref="the user allowed one synthetic call",
            provider="synthetic-provider",
            model="synthetic-model",
            manifest_hash=MANIFEST,
            max_calls=2,
            max_output_tokens_per_call=100,
            max_wall_seconds=10,
        )
        return self.store.reserve_model_attempt(
            run_id=str(uuid.uuid4()),
            namespace=NS,
            context_id=context_id,
            authorization_id=authorization.authorization_id,
            provider="synthetic-provider",
            model_requested="synthetic-model",
            manifest_hash=MANIFEST,
            adapter_kind="fake-model",
            policy_version="collab.v1",
            request_body="synthetic request body",
            manifest_max_calls=2,
            manifest_max_output_tokens_per_call=100,
            manifest_max_wall_seconds=10,
        )

    # ------------------------------------------------------- labels on write

    def test_register_review_candidate_labels_candidate_pending(self) -> None:
        event = self.ingest()
        context = self.build_context(event.event_id)
        fields = self.candidate_fields(event=event, context=context)

        candidate = self.service.register_review_candidate(
            namespace=NS,
            work_id=WORK_ID,
            work_generation=0,
            **fields,
        )

        self.assertEqual("pending", candidate.status)
        interaction = self.interaction_of(candidate.interaction_id)
        self.assertIsInstance(interaction, InteractionRow)
        states = self.store.candidate_states_for_interaction(
            namespace=NS, interaction_id=candidate.interaction_id
        )
        self.assertEqual(1, len(states))
        self.assertIsInstance(states[0], CandidateState)
        self.assertEqual("candidate_pending", interaction.result)
        self.assertIsNone(interaction.receipt_request_id)
        self.assertIsNone(interaction.completed_at)

    def test_resolve_approve_labels_committed(self) -> None:
        candidate, _context, _event = self.make_candidate()
        resolution = self.approve(candidate)
        self.assertEqual("committed", resolution.status)

        interaction = self.interaction_of(candidate.interaction_id)
        self.assertEqual("committed", interaction.result)
        self.assertEqual(
            resolution.receipt.request_id, interaction.receipt_request_id
        )
        self.assertIsNotNone(interaction.completed_at)

    def test_resolve_reject_labels_rejected(self) -> None:
        candidate, _context, _event = self.make_candidate()
        resolution = self.reject(candidate)
        self.assertEqual("rejected", resolution.status)

        interaction = self.interaction_of(candidate.interaction_id)
        self.assertEqual("rejected", interaction.result)
        self.assertIsNone(interaction.receipt_request_id)
        self.assertIsNotNone(interaction.completed_at)

    # ----------------------------------------------------------- get_result

    def test_get_result_before_txb_labels_candidate_pending(self) -> None:
        candidate, _context, _event = self.make_candidate()
        before = self.counts()

        result = self.service.get_result(
            namespace=NS, interaction_id=candidate.interaction_id
        )

        self.assertIsInstance(result, InteractionResult)
        self.assertEqual("candidate_pending", result.result)
        self.assertIsNone(result.receipt)
        self.assertEqual(candidate.artifact_id, result.candidate_id)
        self.assertEqual("pending", result.candidate_status)
        self.assertTrue(result.repaired)
        self.assertIsNone(result.completed_at)
        interaction = self.interaction_of(candidate.interaction_id)
        self.assertEqual("candidate_pending", interaction.result)
        self.assertIsNone(interaction.completed_at)
        after = self.counts()
        self.assertEqual(before["beliefs"], after["beliefs"])
        self.assertEqual(before["commands"], after["commands"])

    def test_get_result_repairs_missing_label_after_commit(self) -> None:
        candidate, _context, _event = self.make_candidate()
        with mock.patch.object(self.service, "_settle_interaction"):
            resolution = self.approve(candidate)
        self.assertEqual("committed", resolution.status)
        self.assertIsNone(
            self.interaction_of(candidate.interaction_id).result
        )
        before = self.counts()

        result = self.service.get_result(
            namespace=NS, interaction_id=candidate.interaction_id
        )

        self.assertEqual("committed", result.result)
        self.assertEqual(resolution.receipt, result.receipt)
        self.assertTrue(result.repaired)
        self.assertIsNotNone(result.completed_at)
        interaction = self.interaction_of(candidate.interaction_id)
        self.assertEqual("committed", interaction.result)
        self.assertEqual(
            resolution.receipt.request_id, interaction.receipt_request_id
        )
        self.assertIsNotNone(interaction.completed_at)
        after = self.counts()
        self.assertEqual(before["beliefs"], after["beliefs"])
        self.assertEqual(before["commands"], after["commands"])

    def test_get_result_is_idempotent(self) -> None:
        candidate, _context, _event = self.make_candidate()
        with mock.patch.object(self.service, "_settle_interaction"):
            self.approve(candidate)

        first = self.service.get_result(
            namespace=NS, interaction_id=candidate.interaction_id
        )
        self.assertTrue(first.repaired)
        first_interaction = self.interaction_of(candidate.interaction_id)
        baseline = self.counts()

        second = self.service.get_result(
            namespace=NS, interaction_id=candidate.interaction_id
        )

        self.assertEqual(first.result, second.result)
        self.assertEqual(first.receipt, second.receipt)
        self.assertEqual(first.completed_at, second.completed_at)
        self.assertFalse(second.repaired)
        self.assertEqual(
            first_interaction, self.interaction_of(candidate.interaction_id)
        )
        self.assertEqual(baseline, self.counts())

    def test_pending_candidate_with_receipt_is_corrupt(self) -> None:
        candidate, _context, _event = self.make_candidate()
        receipt = self.belief_receipt_for(candidate)
        self.assertEqual("noop", receipt.outcome)
        self.assertEqual("pending", self.candidate_status(candidate))

        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=candidate.interaction_id
            )

        self.assertEqual("pending", self.candidate_status(candidate))
        interaction = self.interaction_of(candidate.interaction_id)
        self.assertIsNone(interaction.result)
        self.assertIsNone(interaction.receipt_request_id)
        self.assertIsNone(interaction.completed_at)

    def test_committed_label_without_receipt_is_corrupt(self) -> None:
        _context, interaction, _event = self.make_review_interaction()
        self.store.complete_interaction(
            namespace=NS,
            interaction_id=interaction,
            result="committed",
        )

        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=interaction
            )

        label = self.interaction_of(interaction)
        self.assertEqual("committed", label.result)
        self.assertIsNone(label.receipt_request_id)

    def test_committed_candidate_without_receipt_is_corrupt(self) -> None:
        candidate, _context, _event = self.make_candidate()
        self.store.connection.execute(
            "UPDATE collab_artifacts SET status = 'committed' "
            "WHERE namespace = ? AND artifact_id = ? AND artifact_version = 1",
            (NS, candidate.artifact_id),
        )

        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=candidate.interaction_id
            )

        self.assertEqual("committed", self.candidate_status(candidate))
        self.assertIsNone(
            self.interaction_of(candidate.interaction_id).result
        )

    def test_get_result_reports_rejected_candidate(self) -> None:
        candidate, _context, _event = self.make_candidate()
        self.store.reject_candidate(
            namespace=NS,
            candidate_id=candidate.artifact_id,
            decision_ref="decision:reject:direct",
        )
        self.assertIsNone(self.interaction_of(candidate.interaction_id).result)

        result = self.service.get_result(
            namespace=NS, interaction_id=candidate.interaction_id
        )

        self.assertEqual("rejected", result.result)
        self.assertEqual("rejected", result.candidate_status)
        self.assertIsNone(result.receipt)
        self.assertTrue(result.repaired)
        self.assertIsNotNone(result.completed_at)
        label = self.interaction_of(candidate.interaction_id)
        self.assertEqual("rejected", label.result)
        self.assertIsNotNone(label.completed_at)

    def test_get_result_reports_stale_context_candidate(self) -> None:
        candidate, _context, event = self.make_candidate()
        self.store.request_deletion(
            target_kind="event",
            namespace=NS,
            target_id=event.event_id,
            authorization_ref="authorization:delete:1",
        )

        result = self.service.get_result(
            namespace=NS, interaction_id=candidate.interaction_id
        )

        self.assertEqual("stale", result.result)
        self.assertEqual("pending", result.candidate_status)
        self.assertIsNotNone(result.completed_at)

    def test_get_result_reports_deleted_candidate_as_stale(self) -> None:
        candidate, _context, _event = self.make_candidate()
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_artifact",
            ref_id=candidate.artifact_id,
            ref_version=1,
            authorization_ref="authorization:delete:1",
        )

        result = self.service.get_result(
            namespace=NS, interaction_id=candidate.interaction_id
        )

        self.assertEqual("stale", result.result)
        self.assertEqual("pending", result.candidate_status)
        self.assertEqual("pending", self.candidate_status(candidate))
        self.assertIsNotNone(result.completed_at)
        label = self.interaction_of(candidate.interaction_id)
        self.assertEqual("stale", label.result)
        self.assertIsNotNone(label.completed_at)

    def test_get_result_reports_waiting_receipt(self) -> None:
        context, interaction, _event = self.make_review_interaction()
        receipt = self.wait_receipt(context.context_id)
        self.assertEqual((), receipt.record_refs)
        self.store.complete_interaction(
            namespace=NS,
            interaction_id=interaction,
            result="waiting",
            receipt_request_id=receipt.request_id,
        )

        result = self.service.get_result(
            namespace=NS, interaction_id=interaction
        )

        self.assertEqual("waiting", result.result)
        self.assertEqual(receipt, result.receipt)
        self.assertIsNone(result.candidate_id)
        self.assertIsNone(result.candidate_status)
        self.assertIsNotNone(result.completed_at)
        self.assertFalse(result.repaired)

    def test_duplicate_resolve_and_get_result_return_the_same_receipt(self) -> None:
        candidate, _context, _event = self.make_candidate()
        first = self.approve(candidate)
        baseline = self.counts()

        again = self.approve(candidate)
        result = self.service.get_result(
            namespace=NS, interaction_id=candidate.interaction_id
        )

        self.assertEqual("already_committed", again.status)
        self.assertEqual(first.receipt, again.receipt)
        self.assertEqual(first.receipt, result.receipt)
        self.assertEqual("committed", result.result)
        self.assertFalse(result.repaired)
        self.assertEqual(baseline, self.counts())

    def test_get_result_after_committed_candidate_deletion_returns_minimal_receipt(
        self,
    ) -> None:
        candidate, _context, _event = self.make_candidate()
        approved = self.approve(candidate)
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_artifact",
            ref_id=candidate.artifact_id,
            ref_version=1,
            authorization_ref="authorization:delete:1",
        )
        self.store.cleanup_deletions()
        with self.assertRaises(Redacted):
            self.store.load_artifact(
                namespace=NS,
                artifact_id=candidate.artifact_id,
                artifact_version=1,
            )

        result = self.service.get_result(
            namespace=NS, interaction_id=candidate.interaction_id
        )

        self.assertEqual("committed", result.result)
        self.assertEqual(approved.receipt, result.receipt)
        self.assertEqual("committed", result.candidate_status)
        self.assertEqual(1, len(result.receipt.record_refs))
        self.assertEqual(
            approved.receipt.record_refs[0].record_id,
            result.receipt.record_refs[0].record_id,
        )
        view = self.store.read_experience(
            self.access(
                frozenset({"private", "restricted", "public-example"})
            ),
            ref=self.candidate_ref(candidate),
            purpose="review",
        )
        self.assertEqual(READABILITY_UNAVAILABLE, view.readability)

    def test_revoked_grant_does_not_deny_the_historical_commit(self) -> None:
        candidate, _context, _event = self.make_candidate()
        self.store.register_grant(self.commit_grant())
        resolution = self.approve(
            candidate,
            authority=CommitAuthority(grant_id=GRANT_COMMIT),
        )
        self.assertEqual("committed", resolution.status)
        self.store.revoke_grant(namespace=NS, grant_id=GRANT_COMMIT)

        result = self.service.get_result(
            namespace=NS, interaction_id=candidate.interaction_id
        )

        self.assertEqual("committed", result.result)
        self.assertEqual(resolution.receipt, result.receipt)
        self.assertEqual("committed", self.candidate_status(candidate))

    def test_current_body_read_still_requires_access(self) -> None:
        candidate, _context, _event = self.make_candidate()
        approved = self.approve(candidate)

        broad = self.store.read_experience(
            self.access(
                frozenset({"private", "restricted", "public-example"})
            ),
            ref=self.candidate_ref(candidate),
            purpose="review",
        )
        narrow = self.store.read_experience(
            self.access(frozenset({"public-example"})),
            ref=self.candidate_ref(candidate),
            purpose="review",
        )

        self.assertEqual(READABILITY_AVAILABLE, broad.readability)
        self.assertIsNotNone(broad.body)
        self.assertIsInstance(narrow, ExperienceUnavailable)
        self.assertEqual(READABILITY_UNAVAILABLE, narrow.readability)
        result = self.service.get_result(
            namespace=NS, interaction_id=candidate.interaction_id
        )
        self.assertEqual("committed", result.result)
        self.assertEqual(approved.receipt, result.receipt)

    def test_get_result_writes_no_belief_and_no_new_command(self) -> None:
        candidate, _context, _event = self.make_candidate()
        self.approve(candidate)
        before = self.counts()

        self.service.get_result(
            namespace=NS, interaction_id=candidate.interaction_id
        )
        self.service.get_result(
            namespace=NS, interaction_id=candidate.interaction_id
        )

        after = self.counts()
        self.assertEqual(before["beliefs"], after["beliefs"])
        self.assertEqual(before["commands"], after["commands"])
        self.assertEqual(before["interactions"], after["interactions"])
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual("committed", self.candidate_status(candidate))

    def test_get_result_leaves_unknown_model_attempt_unknown(self) -> None:
        context, event = self.new_context()
        interaction = self.store.register_interaction(
            namespace=NS,
            mode="review",
            activation_kind="manual_review",
            host_instance="host:synthetic-1",
            access_ref="authorization:access:1",
            policy_ref="policy:collab.v1",
            context_id=context.context_id,
            trigger_ref=self.event_ref(event.event_id),
            work_id=WORK_ID,
            work_generation=0,
        )
        reservation = self.reserve_unknown_attempt(context.context_id)
        attempt_id = reservation.attempt.attempt_id

        self.service.get_result(namespace=NS, interaction_id=interaction)

        attempts = self.store.model_attempts_for_context(
            context.context_id
        )
        self.assertEqual(1, len(attempts))
        attempt = attempts[0]
        self.assertEqual(attempt_id, attempt.attempt_id)
        self.assertEqual(MODEL_ATTEMPT_RESERVED, attempt.status)
        self.assertIsNone(attempt.outcome)
        self.assertIsNone(attempt.finished_at)
        self.assertFalse(attempt.usage_known)
        self.assertFalse(attempt.cost_known)
        self.assertIsNone(attempt.usage)
        self.assertIsNone(attempt.cost_estimate)

    def test_get_result_unknown_interaction_is_not_found(self) -> None:
        with self.assertRaises(RecordNotFound):
            self.service.get_result(
                namespace=NS, interaction_id=str(uuid.uuid4())
            )

    def test_get_result_rejects_ambiguous_candidates(self) -> None:
        first, context, event = self.make_candidate()
        self.store.register_candidate(
            namespace=NS,
            interaction_id=first.interaction_id,
            context_id=context.context_id,
            body="second synthetic candidate body",
            retention="standard",
            proposal=self.default_proposal(event),
            proposal_version="v1",
            work_id=WORK_ID,
            work_generation=0,
            request_id=REQ_B,
            artifact_id=CAND_B,
            sources=self.context_source_refs(context),
        )

        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=first.interaction_id
            )

        self.assertIsNone(self.interaction_of(first.interaction_id).result)

    # -------------------------------------------------------- exact binding

    def test_belief_receipt_must_bind_the_candidate_exactly(self) -> None:
        # (a) receipt context differs from the candidate context
        candidate_a, _context_a, _event_a = self.make_candidate()
        other_context, _other_event = self.new_context()
        self.belief_receipt_for(candidate_a, context_id=other_context.context_id)
        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=candidate_a.interaction_id
            )
        self.assertEqual("pending", self.candidate_status(candidate_a))
        self.assertIsNone(
            self.interaction_of(candidate_a.interaction_id).result
        )

        # (b) receipt operation differs from the candidate proposal operation
        candidate_b, _context_b, _event_b = self.make_candidate(
            request_id=str(uuid.uuid4()),
            artifact_id=str(uuid.uuid4()),
        )
        self.belief_receipt_for(candidate_b, operation="revise_belief")
        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=candidate_b.interaction_id
            )
        self.assertEqual("pending", self.candidate_status(candidate_b))
        self.assertIsNone(
            self.interaction_of(candidate_b.interaction_id).result
        )

        # (c) receipt payload (and therefore request digest) differs
        candidate_c, _context_c, _event_c = self.make_candidate(
            request_id=str(uuid.uuid4()),
            artifact_id=str(uuid.uuid4()),
        )
        altered = dict(
            candidate_c.proposal,
            scope={"scene": "a.different.scene"},
        )
        self.belief_receipt_for(candidate_c, payload=altered)
        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=candidate_c.interaction_id
            )
        self.assertEqual("pending", self.candidate_status(candidate_c))
        self.assertIsNone(
            self.interaction_of(candidate_c.interaction_id).result
        )

        # (d) candidate.context_id differs from interaction.context_id
        event_d = self.ingest(event_type="CapabilityObserved:bind")
        context_d1 = self.build_context(event_d.event_id)
        context_d2, _event_d2 = self.new_context()
        interaction_d = self.store.register_interaction(
            namespace=NS,
            mode="review",
            activation_kind="manual_review",
            host_instance="host:synthetic-1",
            access_ref="authorization:access:1",
            policy_ref="policy:collab.v1",
            context_id=context_d1.context_id,
            trigger_ref=self.event_ref(event_d.event_id),
            work_id=str(uuid.uuid4()),
            work_generation=0,
        )
        work_d = "d1d1d1d1-1111-4111-8111-dddddddddddd"
        request_d = str(uuid.uuid4())
        candidate_d = self.store.register_candidate(
            namespace=NS,
            interaction_id=interaction_d,
            context_id=context_d2.context_id,
            body="candidate with another context",
            retention="standard",
            proposal=self.default_proposal(event_d),
            proposal_version="v1",
            work_id=work_d,
            work_generation=0,
            request_id=request_d,
            artifact_id=str(uuid.uuid4()),
            sources=self.context_source_refs(context_d2),
        )
        self.belief_receipt_for(candidate_d)
        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=interaction_d
            )
        self.assertEqual("pending", self.candidate_status(candidate_d))
        self.assertIsNone(self.interaction_of(interaction_d).result)

        # (e) candidate.work_id differs from interaction.work_id
        work_e = str(uuid.uuid4())
        candidate_e, _context_e, _event_e = self.make_candidate(
            work_id=work_e,
            request_id=str(uuid.uuid4()),
            artifact_id=str(uuid.uuid4()),
        )
        self.belief_receipt_for(candidate_e)
        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=candidate_e.interaction_id
            )
        self.assertEqual("pending", self.candidate_status(candidate_e))
        self.assertIsNone(
            self.interaction_of(candidate_e.interaction_id).result
        )

        # (f) candidate.work_generation differs from interaction.work_generation
        work_f = str(uuid.uuid4())
        self.store.register_work_control(
            namespace=NS,
            work_id=work_f,
            generation=1,
            blocked_after_rejection=False,
            control_version=1,
        )
        event_f = self.ingest(event_type="CapabilityObserved:generation")
        context_f = self.build_context(event_f.event_id)
        interaction_f = self.store.register_interaction(
            namespace=NS,
            mode="review",
            activation_kind="manual_review",
            host_instance="host:synthetic-1",
            access_ref="authorization:access:1",
            policy_ref="policy:collab.v1",
            context_id=context_f.context_id,
            trigger_ref=self.event_ref(event_f.event_id),
            work_id=work_f,
            work_generation=0,
        )
        request_f = str(uuid.uuid4())
        candidate_f = self.store.register_candidate(
            namespace=NS,
            interaction_id=interaction_f,
            context_id=context_f.context_id,
            body="candidate at generation one",
            retention="standard",
            proposal=self.default_proposal(event_f),
            proposal_version="v1",
            work_id=work_f,
            work_generation=1,
            request_id=request_f,
            artifact_id=str(uuid.uuid4()),
            sources=self.context_source_refs(context_f),
        )
        self.belief_receipt_for(candidate_f)
        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=interaction_f
            )
        self.assertEqual("pending", self.candidate_status(candidate_f))
        self.assertIsNone(self.interaction_of(interaction_f).result)

    def test_cross_referenced_candidate_receipts_are_corrupt(self) -> None:
        candidate_a, _context_a, _event_a = self.make_candidate(
            request_id=REQ_A, artifact_id=CAND_A
        )
        approved_a = self.approve(candidate_a)
        # Build and commit B only after A committed: a new ingest moves the
        # event watermark, so an earlier Context may no longer be a live basis.
        candidate_b, _context_b, _event_b = self.make_candidate(
            request_id=REQ_B, artifact_id=CAND_B
        )
        approved_b = self.approve(candidate_b)
        receipt_a = approved_a.receipt.request_id
        receipt_b = approved_b.receipt.request_id

        self.store.connection.execute(
            "UPDATE collab_interactions SET receipt_request_id = ? "
            "WHERE namespace = ? AND interaction_id = ?",
            (receipt_a, NS, candidate_b.interaction_id),
        )
        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=candidate_b.interaction_id
            )
        self.assertEqual("committed", self.candidate_status(candidate_b))

        self.store.connection.execute(
            "UPDATE collab_interactions SET receipt_request_id = ? "
            "WHERE namespace = ? AND interaction_id = ?",
            (receipt_b, NS, candidate_a.interaction_id),
        )
        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=candidate_a.interaction_id
            )
        self.assertEqual("committed", self.candidate_status(candidate_a))

    def test_wait_receipt_must_bind_the_interaction(self) -> None:
        # (a) wait receipt context differs from the interaction context
        context_a, interaction_a, _event_a = self.make_review_interaction()
        other_context, _other_event = self.new_context()
        receipt_a = self.wait_receipt(other_context.context_id)
        self.store.complete_interaction(
            namespace=NS,
            interaction_id=interaction_a,
            result="waiting",
            receipt_request_id=receipt_a.request_id,
        )
        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=interaction_a
            )
        label = self.interaction_of(interaction_a)
        self.assertEqual("waiting", label.result)
        self.assertEqual(receipt_a.request_id, label.receipt_request_id)

        # (b) interaction.context_id is NULL
        event_b = self.ingest(event_type="CapabilityObserved:wait-null")
        interaction_b = self.store.register_interaction(
            namespace=NS,
            mode="review",
            activation_kind="manual_review",
            host_instance="host:synthetic-1",
            access_ref="authorization:access:1",
            policy_ref="policy:collab.v1",
            context_id=None,
            trigger_ref=self.event_ref(event_b.event_id),
            work_id=WORK_ID,
            work_generation=0,
        )
        context_b = self.build_context(event_b.event_id)
        receipt_b = self.wait_receipt(context_b.context_id)
        self.store.complete_interaction(
            namespace=NS,
            interaction_id=interaction_b,
            result="waiting",
            receipt_request_id=receipt_b.request_id,
        )
        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=interaction_b
            )
        self.assertEqual("waiting", self.interaction_of(interaction_b).result)

        # (c) a wait receipt with a record reference is illegal
        context_c, interaction_c, _event_c = self.make_review_interaction()
        receipt_c = self.wait_receipt(context_c.context_id, append=True)
        self.assertTrue(receipt_c.record_refs)
        self.store.complete_interaction(
            namespace=NS,
            interaction_id=interaction_c,
            result="waiting",
            receipt_request_id=receipt_c.request_id,
        )
        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=interaction_c
            )
        self.assertEqual("waiting", self.interaction_of(interaction_c).result)

        # (d) an interaction already committed cannot accept a wait receipt
        context_d, interaction_d, _event_d = self.make_review_interaction()
        receipt_d = self.wait_receipt(context_d.context_id)
        self.store.complete_interaction(
            namespace=NS,
            interaction_id=interaction_d,
            result="committed",
            receipt_request_id=receipt_d.request_id,
        )
        with self.assertRaises(CorruptStore):
            self.service.get_result(
                namespace=NS, interaction_id=interaction_d
            )
        label_d = self.interaction_of(interaction_d)
        self.assertEqual("committed", label_d.result)
        self.assertEqual(receipt_d.request_id, label_d.receipt_request_id)

    # ------------------------------------ two-phase concurrency consistency

    def test_get_result_derives_inside_one_read_snapshot(self) -> None:
        candidate, _context, _event = self.make_candidate()
        recorded: list[bool] = []
        real = self.service._derive_result

        def recording(*, namespace, interaction_id):
            recorded.append(self.store.connection.in_transaction)
            return real(namespace=namespace, interaction_id=interaction_id)

        with mock.patch.object(
            self.service, "_derive_result", side_effect=recording
        ):
            result = self.service.get_result(
                namespace=NS, interaction_id=candidate.interaction_id
            )

        self.assertEqual(2, len(recorded))
        self.assertEqual([True, True], recorded)
        self.assertTrue(result.repaired)
        self.assertEqual("candidate_pending", result.result)

    def test_get_result_serialized_phase_absorbs_a_concurrent_commit(self) -> None:
        candidate, _context, _event = self.make_candidate()
        approval = self.approval_for(candidate)
        second = self.open_second_store()
        service2 = CollaborationService(second)
        plans = []
        real_derive = self.service._derive_result

        def capture(*, namespace, interaction_id):
            plan = real_derive(
                namespace=namespace, interaction_id=interaction_id
            )
            plans.append(plan)
            return plan

        real_snapshot = self.store.read_snapshot

        @contextmanager
        def snap():
            with real_snapshot():
                yield
            # The phase-1 snapshot is closed; a real C06a commit lands before
            # phase 2 starts.  No sleep and no timing assumption.
            service2.commit_candidate(
                namespace=NS,
                candidate_id=candidate.artifact_id,
                authority=CommitAuthority(approval=approval),
                decision_ref="decision:race",
            )

        with mock.patch.object(
            self.service, "_derive_result", side_effect=capture
        ):
            with mock.patch.object(self.store, "read_snapshot", snap):
                result = self.service.get_result(
                    namespace=NS, interaction_id=candidate.interaction_id
                )

        self.assertEqual(2, len(plans))
        self.assertTrue(plans[0].need_label)
        self.assertEqual("candidate_pending", plans[0].result)
        self.assertIsNone(plans[0].receipt)
        self.assertTrue(plans[1].need_label)
        self.assertEqual("committed", plans[1].result)
        self.assertIsNotNone(plans[1].receipt)

        self.assertEqual("committed", result.result)
        self.assertIsNotNone(result.receipt)
        self.assertEqual("committed", result.candidate_status)
        self.assertTrue(result.repaired)
        self.assertIsNotNone(result.completed_at)
        label = self.interaction_of(candidate.interaction_id)
        self.assertEqual("committed", label.result)
        self.assertEqual(result.receipt.request_id, label.receipt_request_id)
        self.assertEqual(result.completed_at, label.completed_at)

    def test_get_result_serialized_phase_uses_the_actual_written_row(self) -> None:
        candidate, _context, _event = self.make_candidate()
        approval = self.approval_for(candidate)
        second = self.open_second_store()
        service2 = CollaborationService(second)
        plans = []
        written: dict = {}
        real_derive = self.service._derive_result

        def capture(*, namespace, interaction_id):
            plan = real_derive(
                namespace=namespace, interaction_id=interaction_id
            )
            plans.append(plan)
            return plan

        real_snapshot = self.store.read_snapshot

        @contextmanager
        def snap():
            with real_snapshot():
                yield
            # Commit and resolve, including the interaction label, before
            # phase 2 re-derives.
            written["resolution"] = service2.resolve_candidate(
                namespace=NS,
                candidate_id=candidate.artifact_id,
                decision=DECISION_APPROVE,
                decision_ref="decision:race",
                authority=CommitAuthority(approval=approval),
            )
            written["label"] = second.interaction(
                namespace=NS, interaction_id=candidate.interaction_id
            )

        with mock.patch.object(
            self.service, "_derive_result", side_effect=capture
        ):
            with mock.patch.object(self.store, "read_snapshot", snap):
                result = self.service.get_result(
                    namespace=NS, interaction_id=candidate.interaction_id
                )

        self.assertEqual(2, len(plans))
        self.assertTrue(plans[0].need_label)
        self.assertIsNone(plans[0].completed_at)
        self.assertFalse(plans[1].need_label)
        self.assertEqual("committed", plans[1].result)

        self.assertEqual("committed", result.result)
        self.assertFalse(result.repaired)
        self.assertEqual(written["resolution"].receipt, result.receipt)
        self.assertIsNotNone(result.completed_at)
        self.assertEqual(written["label"].completed_at, result.completed_at)
        self.assertEqual(written["label"].result, "committed")
        label = self.interaction_of(candidate.interaction_id)
        self.assertEqual(written["label"], label)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
