"""C05d: stable candidate request keys and work-control state transitions.

Every test uses a fresh migrated database and synthetic data.  The suite
exercises identity-based idempotency, the reject/rerun atomic transitions, the
internal receipt-bound commit primitive, and single-version candidate identity.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from test_collaboration_store import (  # noqa: E402
    NS,
    RECORD_ID,
    RERUN_GRANT_ID,
    TS,
    WORK_ID,
    CollaborationTestCase,
)

from quintessence_pcs.foundation.errors import (  # noqa: E402
    CommandConflict,
    InvalidInput,
)
from quintessence_pcs.store.errors import CandidateTransitionDenied  # noqa: E402

REQ_1 = "31111111-1111-4111-8111-111111111111"
REQ_2 = "32222222-2222-4222-8222-222222222222"
CAND_1 = "a1111111-1111-4111-8111-111111111111"
CAND_2 = "a2222222-2222-4222-8222-222222222222"
GRANT_A = RERUN_GRANT_ID
GRANT_B = "24242424-2424-4424-8424-242424242424"
GRANT_C = "25252525-2525-4525-8525-252525252525"
GRANT_D = "26262626-2626-4626-8626-262626262626"
DECISION = "decision:reject:1"


class WorkControlTests(CollaborationTestCase):
    # ------------------------------------------------------------- fixtures

    def make_review_interaction(self, *, context_id: str, generation: int = 0) -> str:
        return self.new_interaction(
            mode="review",
            context_id=context_id,
            trigger_ref=self.event_ref(),
            work_id=WORK_ID,
            work_generation=generation,
        )

    def candidate(
        self,
        *,
        request_id: str = REQ_1,
        artifact_id: str | None = None,
        context_id: str | None = None,
        interaction_id: str | None = None,
        generation: int = 0,
        proposal=None,
        body: str = "synthetic candidate body",
        sources=(),
        rerun_authorization_ref: str | None = None,
        retention: str = "standard",
        proposal_version: str = "v1",
        command_digest: str | None = None,
        artifact_version: int = 1,
        work_id: str = WORK_ID,
    ):
        context = context_id if context_id is not None else self.make_context()
        interaction = (
            interaction_id
            if interaction_id is not None
            else self.make_review_interaction(
                context_id=context, generation=generation
            )
        )
        if proposal is None:
            proposal = {"operation": "belief.propose", "reason": "synthetic"}
        return self.store.register_candidate(
            namespace=NS,
            interaction_id=interaction,
            context_id=context,
            body=body,
            retention=retention,
            proposal=proposal,
            proposal_version=proposal_version,
            work_id=work_id,
            work_generation=generation,
            request_id=request_id,
            command_digest=command_digest,
            sources=list(sources),
            rerun_authorization_ref=rerun_authorization_ref,
            created_at=TS,
            artifact_id=artifact_id,
            artifact_version=artifact_version,
        )

    def reject(self, candidate, decision_ref: str = DECISION):
        return self.store.reject_candidate(
            namespace=NS,
            candidate_id=candidate.artifact_id,
            decision_ref=decision_ref,
        )

    def txb_receipt(self, candidate, *, append: bool = False, payload=None):
        def apply(scope) -> None:
            if append:
                scope.append_record_version(
                    record_id=RECORD_ID,
                    record_version=1,
                    kind="Observation",
                    body={"text": "synthetic committed record"},
                )

        request_payload = (
            {"operation": "belief.propose", "reason": "synthetic"}
            if payload is None
            else payload
        )
        return self.store.commit_command(
            context_id=candidate.context_id,
            request_id=candidate.request_id,
            operation="belief.propose",
            request_payload=request_payload,
            apply=apply,
            namespace=NS,
        )

    def mark_committed(
        self,
        candidate,
        *,
        decision_ref: str = "decision:commit:1",
        commit_ref: str | None = None,
    ):
        with self.store.transaction():
            return self.store._mark_candidate_committed_locked(
                namespace=NS,
                candidate_id=candidate.artifact_id,
                decision_ref=decision_ref,
                commit_authorization_ref=commit_ref,
            )

    def reload(self, candidate):
        return self.store.load_artifact(
            namespace=NS, artifact_id=candidate.artifact_id, artifact_version=1
        )

    def get_control(self):
        return self.store.work_control(namespace=NS, work_id=WORK_ID)

    def candidate_count(self) -> int:
        return int(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM collab_artifacts "
                "WHERE namespace = ? AND kind = 'candidate'",
                (NS,),
            ).fetchone()[0]
        )

    def table_count(self, table: str) -> int:
        return int(
            self.store.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        )

    def grants(self, generation: int = 1, **overrides):
        base = self.grant_rerun(generation=generation)
        base.update(overrides)
        return base

    # ---------------------------------------------------------------- tests

    def test_register_candidate_creates_control_and_bumps_version(self) -> None:
        revision = self.store.current_state_revision(NS)
        candidate = self.candidate()
        control = self.get_control()
        self.assertEqual("candidate", candidate.kind)
        self.assertEqual("pending", candidate.status)
        self.assertEqual(1, candidate.artifact_version)
        self.assertEqual(0, control.generation)
        self.assertFalse(control.blocked_after_rejection)
        self.assertEqual(2, control.control_version)
        self.assertEqual(candidate.artifact_id, control.last_candidate_ref)
        self.assertIsNone(control.block_reason)
        self.assertEqual(revision, self.store.current_state_revision(NS))
        self.assertEqual(0, self.table_count("pending_work"))
        self.assertEqual(1, self.candidate_count())
        indexes = {
            row[0]
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        self.assertTrue(
            {
                "collab_artifacts_request_key_idx",
                "collab_candidates_single_version_idx",
            }.issubset(indexes)
        )

    def test_identical_retry_returns_the_same_candidate(self) -> None:
        context = self.make_context()
        interaction = self.make_review_interaction(context_id=context)
        first = self.candidate(
            context_id=context, interaction_id=interaction, request_id=REQ_1
        )
        control_before = self.get_control()
        second = self.candidate(
            context_id=context, interaction_id=interaction, request_id=REQ_1
        )
        control_after = self.get_control()
        self.assertEqual(first.artifact_id, second.artifact_id)
        self.assertEqual(first.status, second.status)
        self.assertEqual(1, self.candidate_count())
        self.assertEqual(
            control_before.control_version, control_after.control_version
        )
        self.assertEqual(2, control_after.control_version)
        self.assertEqual(first.artifact_id, control_after.last_candidate_ref)

    def test_same_key_different_proposal_digest_conflicts(self) -> None:
        context = self.make_context()
        interaction = self.make_review_interaction(context_id=context)
        first = self.candidate(
            context_id=context,
            interaction_id=interaction,
            request_id=REQ_1,
            proposal={"operation": "belief.propose", "reason": "one"},
        )
        with self.assertRaises(CommandConflict):
            self.candidate(
                context_id=context,
                interaction_id=interaction,
                request_id=REQ_1,
                proposal={"operation": "belief.propose", "reason": "two"},
            )
        self.assertEqual(1, self.candidate_count())
        self.assertEqual(2, self.get_control().control_version)
        self.assertEqual(first.artifact_id, self.get_control().last_candidate_ref)

    def test_same_key_different_context_conflicts(self) -> None:
        first_context = self.make_context()
        other_context = self.make_context()
        interaction = self.make_review_interaction(context_id=first_context)
        self.candidate(
            context_id=first_context,
            interaction_id=interaction,
            request_id=REQ_1,
        )
        with self.assertRaises(CommandConflict):
            self.candidate(
                context_id=other_context,
                interaction_id=interaction,
                request_id=REQ_1,
            )
        self.assertEqual(1, self.candidate_count())
        self.assertEqual(2, self.get_control().control_version)

    def test_same_key_different_generation_conflicts(self) -> None:
        context = self.make_context()
        interaction = self.make_review_interaction(context_id=context, generation=0)
        self.candidate(
            context_id=context,
            interaction_id=interaction,
            request_id=REQ_1,
            generation=0,
        )
        with self.assertRaises(CommandConflict):
            self.candidate(
                context_id=context,
                interaction_id=interaction,
                request_id=REQ_1,
                generation=1,
            )
        self.assertEqual(1, self.candidate_count())
        self.assertEqual(2, self.get_control().control_version)

    def test_same_key_different_source_closure_conflicts(self) -> None:
        source = self.new_input()
        context = self.make_context()
        interaction = self.make_review_interaction(context_id=context)
        self.candidate(
            context_id=context,
            interaction_id=interaction,
            request_id=REQ_1,
            sources=[self.input_ref(source)],
        )
        with self.assertRaises(CommandConflict):
            self.candidate(
                context_id=context,
                interaction_id=interaction,
                request_id=REQ_1,
                sources=[],
            )
        self.assertEqual(1, self.candidate_count())
        self.assertEqual(2, self.get_control().control_version)

    def test_candidate_version_must_be_one_and_artifact_id_is_unique(self) -> None:
        with self.assertRaises(InvalidInput):
            self.candidate(artifact_version=2)
        first = self.candidate(request_id=REQ_1, artifact_id=CAND_1)
        with self.assertRaises(CommandConflict):
            self.candidate(
                request_id=REQ_2,
                artifact_id=CAND_1,
                context_id=first.context_id,
                interaction_id=first.interaction_id,
            )
        self.assertEqual(1, self.candidate_count())
        self.assertEqual("pending", self.reload(first).status)

        payload = self.payload_ref(
            "collab_artifacts", "body_payload_ref", artifact_id=CAND_1
        )
        proposal_payload = self.payload_ref(
            "collab_artifacts", "proposal_payload_ref", artifact_id=CAND_1
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_artifact_row(
                kind="candidate",
                context_id=first.context_id,
                payload_ref=payload,
                digest=first.content_digest,
                artifact_id=CAND_1,
                artifact_version=2,
                interaction_id=first.interaction_id,
                proposal_payload_ref=proposal_payload,
                proposal_digest=first.proposal_digest,
                proposal_version="v1",
                work_id=WORK_ID,
                work_generation=0,
                request_id=REQ_2,
                command_digest=first.command_digest,
                structural_validation="passed",
                status="pending",
            )
        self.assertEqual(1, self.candidate_count())

    def test_new_key_with_stale_generation_is_invalid(self) -> None:
        self.store.register_work_control(
            namespace=NS, work_id=WORK_ID, generation=1, control_version=1
        )
        with self.assertRaises(InvalidInput):
            self.candidate(generation=0, request_id=REQ_1)
        self.assertEqual(0, self.candidate_count())
        self.assertEqual(1, self.get_control().generation)
        self.assertEqual(1, self.get_control().control_version)

    def test_blocked_work_without_registered_rerun_ref_is_denied(self) -> None:
        candidate = self.candidate()
        self.reject(candidate)
        version = self.get_control().control_version
        with self.assertRaises(CandidateTransitionDenied):
            self.candidate(generation=1, request_id=REQ_2)
        self.assertEqual(1, self.candidate_count())
        self.assertEqual(version, self.get_control().control_version)
        self.assertIsNone(self.get_control().rerun_authorization_ref)

    def test_blocked_work_with_a_different_valid_grant_is_denied(self) -> None:
        candidate = self.candidate()
        self.reject(candidate)
        self.store.register_grant(self.grants(grant_id=GRANT_A))
        self.store.register_grant(self.grants(grant_id=GRANT_B))
        self.store.authorize_rerun(
            namespace=NS, work_id=WORK_ID, grant_id=GRANT_A
        )
        version = self.get_control().control_version
        with self.assertRaises(CandidateTransitionDenied):
            self.candidate(
                generation=1,
                request_id=REQ_2,
                rerun_authorization_ref=GRANT_B,
            )
        self.assertEqual(1, self.candidate_count())
        self.assertEqual(GRANT_A, self.get_control().rerun_authorization_ref)
        self.assertEqual(version, self.get_control().control_version)

    def test_blocked_work_with_the_registered_grant_is_accepted(self) -> None:
        candidate = self.candidate()
        self.reject(candidate)
        self.store.register_grant(self.grants(grant_id=GRANT_A))
        authorized = self.store.authorize_rerun(
            namespace=NS, work_id=WORK_ID, grant_id=GRANT_A
        )
        registered = self.candidate(
            generation=1,
            request_id=REQ_2,
            rerun_authorization_ref=GRANT_A,
        )
        control = self.get_control()
        self.assertEqual("pending", registered.status)
        self.assertEqual(GRANT_A, registered.rerun_authorization_ref)
        self.assertEqual(1, control.generation)
        self.assertTrue(control.blocked_after_rejection)
        self.assertEqual(registered.artifact_id, control.last_candidate_ref)
        self.assertEqual(
            authorized.control_version + 1, control.control_version
        )

    def test_reject_updates_candidate_and_control(self) -> None:
        candidate = self.candidate()
        revision = self.store.current_state_revision(NS)
        rejected, control = self.reject(candidate)
        self.assertEqual("rejected", rejected.status)
        self.assertIsNotNone(rejected.decided_at)
        self.assertEqual(DECISION, rejected.decision_ref)
        self.assertEqual(1, control.generation)
        self.assertTrue(control.blocked_after_rejection)
        self.assertEqual("rejected", control.block_reason)
        self.assertIsNone(control.rerun_authorization_ref)
        self.assertEqual(3, control.control_version)
        self.assertEqual(candidate.artifact_id, control.last_candidate_ref)
        self.assertEqual(revision, self.store.current_state_revision(NS))
        self.assertEqual(0, self.table_count("work_progress"))

    def test_repeated_reject_with_same_decision_ref_is_idempotent(self) -> None:
        candidate = self.candidate()
        first = self.reject(candidate)
        second = self.reject(candidate)
        self.assertEqual(first, second)
        self.assertEqual(3, self.get_control().control_version)
        self.assertEqual(DECISION, self.reload(candidate).decision_ref)

    def test_repeated_reject_with_a_different_decision_ref_is_denied(self) -> None:
        candidate = self.candidate()
        self.reject(candidate)
        with self.assertRaises(CandidateTransitionDenied):
            self.reject(candidate, "decision:reject:2")
        loaded = self.reload(candidate)
        self.assertEqual("rejected", loaded.status)
        self.assertEqual(DECISION, loaded.decision_ref)
        self.assertEqual(3, self.get_control().control_version)

    def test_rejecting_a_committed_candidate_is_denied(self) -> None:
        candidate = self.candidate()
        self.txb_receipt(candidate)
        committed = self.mark_committed(candidate)
        self.assertEqual("committed", committed.status)
        with self.assertRaises(CandidateTransitionDenied):
            self.reject(candidate)
        self.assertEqual("committed", self.reload(candidate).status)
        self.assertEqual(0, self.get_control().generation)
        self.assertEqual(2, self.get_control().control_version)

    def test_rejection_stales_other_pending_candidates(self) -> None:
        first = self.candidate(request_id=REQ_1)
        second = self.candidate(request_id=REQ_2)
        rejected, control = self.reject(first)
        stale = self.reload(second)
        self.assertEqual("rejected", rejected.status)
        self.assertEqual("stale", stale.status)
        self.assertIsNone(stale.decision_ref)
        self.assertIsNone(stale.decided_at)
        self.assertEqual(1, control.generation)
        self.assertEqual(2, self.candidate_count())
        with self.assertRaises(CandidateTransitionDenied):
            with self.store.transaction():
                self.store._mark_candidate_committed_locked(
                    namespace=NS,
                    candidate_id=second.artifact_id,
                    decision_ref="decision:commit:stale",
                )
        self.assertEqual("stale", self.reload(second).status)

    def test_commit_primitive_without_a_receipt_is_denied(self) -> None:
        candidate = self.candidate()
        with self.assertRaises(RuntimeError):
            self.store._mark_candidate_committed_locked(
                namespace=NS,
                candidate_id=candidate.artifact_id,
                decision_ref="decision:commit:1",
            )
        with self.assertRaises(CandidateTransitionDenied):
            with self.store.transaction():
                self.store._mark_candidate_committed_locked(
                    namespace=NS,
                    candidate_id=candidate.artifact_id,
                    decision_ref="decision:commit:1",
                )
        self.assertEqual("pending", self.reload(candidate).status)

    def test_commit_primitive_with_a_txb_receipt_commits_once(self) -> None:
        candidate = self.candidate()
        receipt = self.txb_receipt(candidate, append=True)
        self.assertEqual("belief.propose", receipt.operation)
        self.assertEqual(candidate.context_id, receipt.context_id)
        self.assertEqual("committed", receipt.outcome)
        self.assertEqual(
            candidate.command_digest,
            self.store.lookup_command_request_digest(
                namespace=NS, request_id=candidate.request_id
            ),
        )
        committed = self.mark_committed(
            candidate, decision_ref="decision:commit:1", commit_ref=GRANT_A
        )
        self.assertEqual("committed", committed.status)
        self.assertIsNotNone(committed.decided_at)
        self.assertEqual("decision:commit:1", committed.decision_ref)
        self.assertEqual(GRANT_A, committed.commit_authorization_ref)
        control = self.get_control()
        self.assertEqual(0, control.generation)
        self.assertEqual(2, control.control_version)
        with self.assertRaises(CandidateTransitionDenied):
            with self.store.transaction():
                self.store._mark_candidate_committed_locked(
                    namespace=NS,
                    candidate_id=candidate.artifact_id,
                    decision_ref="decision:commit:2",
                )
        self.assertEqual("committed", self.reload(candidate).status)

    def test_authorize_rerun_sets_ref_and_is_idempotent(self) -> None:
        candidate = self.candidate()
        self.reject(candidate)
        self.store.register_grant(self.grants(grant_id=GRANT_A))
        first = self.store.authorize_rerun(
            namespace=NS, work_id=WORK_ID, grant_id=GRANT_A
        )
        self.assertEqual(GRANT_A, first.rerun_authorization_ref)
        self.assertEqual(4, first.control_version)
        self.assertEqual(1, first.generation)
        self.assertTrue(first.blocked_after_rejection)
        again = self.store.authorize_rerun(
            namespace=NS, work_id=WORK_ID, grant_id=GRANT_A
        )
        self.assertEqual(first, again)

        self.store.register_grant(
            self.grants(grant_id=GRANT_B, generation=2)
        )
        with self.assertRaises(CandidateTransitionDenied):
            self.store.authorize_rerun(
                namespace=NS, work_id=WORK_ID, grant_id=GRANT_B
            )
        self.store.register_grant(
            self.grants(grant_id=GRANT_C, revoked_at=TS)
        )
        with self.assertRaises(CandidateTransitionDenied):
            self.store.authorize_rerun(
                namespace=NS, work_id=WORK_ID, grant_id=GRANT_C
            )
        self.store.register_grant(
            self.grants(
                grant_id=GRANT_D, expires_at="2020-01-01T00:00:00Z"
            )
        )
        with self.assertRaises(CandidateTransitionDenied):
            self.store.authorize_rerun(
                namespace=NS, work_id=WORK_ID, grant_id=GRANT_D
            )
        control = self.get_control()
        self.assertEqual(GRANT_A, control.rerun_authorization_ref)
        self.assertEqual(4, control.control_version)

    def test_control_writes_do_not_advance_revision_and_survive_reopen(self) -> None:
        revision = self.store.current_state_revision(NS)
        candidate = self.candidate()
        self.reject(candidate)
        self.store.register_grant(self.grants(grant_id=GRANT_A))
        self.store.authorize_rerun(
            namespace=NS, work_id=WORK_ID, grant_id=GRANT_A
        )
        self.assertEqual(revision, self.store.current_state_revision(NS))
        self.assertEqual(0, self.table_count("work_progress"))
        self.assertEqual(0, self.table_count("pending_work"))

        self.reopen()
        control = self.get_control()
        loaded = self.reload(candidate)
        self.assertEqual(1, control.generation)
        self.assertTrue(control.blocked_after_rejection)
        self.assertEqual("rejected", control.block_reason)
        self.assertEqual(GRANT_A, control.rerun_authorization_ref)
        self.assertEqual(candidate.artifact_id, control.last_candidate_ref)
        self.assertEqual(4, control.control_version)
        self.assertEqual("rejected", loaded.status)
        self.assertEqual(DECISION, loaded.decision_ref)
        self.assertEqual(revision, self.store.current_state_revision(NS))

    def test_commit_primitive_cannot_use_another_candidates_receipt(self) -> None:
        context = self.make_context()
        interaction = self.make_review_interaction(context_id=context)
        first = self.candidate(
            context_id=context, interaction_id=interaction, request_id=REQ_1
        )
        second = self.candidate(
            context_id=context, interaction_id=interaction, request_id=REQ_2
        )
        self.txb_receipt(first)
        with self.assertRaises(CandidateTransitionDenied):
            with self.store.transaction():
                self.store._mark_candidate_committed_locked(
                    namespace=NS,
                    candidate_id=second.artifact_id,
                    decision_ref="decision:commit:other",
                )
        self.assertEqual("pending", self.reload(second).status)

        row = self.store.connection.execute(
            "SELECT receipt_json FROM processed_commands "
            "WHERE namespace = ? AND request_id = ?",
            (NS, first.request_id),
        ).fetchone()
        receipt = json.loads(row[0])
        receipt["context_id"] = str(uuid.uuid4())
        self.store.connection.execute(
            "UPDATE processed_commands SET receipt_json = ? "
            "WHERE namespace = ? AND request_id = ?",
            (json.dumps(receipt, sort_keys=True, separators=(",", ":")), NS, first.request_id),
        )
        with self.assertRaises(CandidateTransitionDenied):
            with self.store.transaction():
                self.store._mark_candidate_committed_locked(
                    namespace=NS,
                    candidate_id=first.artifact_id,
                    decision_ref="decision:commit:tampered",
                )
        self.assertEqual("pending", self.reload(first).status)

        self.store.connection.execute(
            "DELETE FROM processed_commands WHERE namespace = ? AND request_id = ?",
            (NS, first.request_id),
        )
        with self.assertRaises(CandidateTransitionDenied):
            with self.store.transaction():
                self.store._mark_candidate_committed_locked(
                    namespace=NS,
                    candidate_id=first.artifact_id,
                    decision_ref="decision:commit:missing",
                )
        self.assertEqual("pending", self.reload(first).status)

    def test_retry_without_artifact_id_returns_the_same_row(self) -> None:
        context = self.make_context()
        interaction = self.make_review_interaction(context_id=context)
        first = self.candidate(
            context_id=context,
            interaction_id=interaction,
            request_id=REQ_1,
            artifact_id=None,
        )
        second = self.candidate(
            context_id=context,
            interaction_id=interaction,
            request_id=REQ_1,
            artifact_id=None,
        )
        self.assertEqual(first.artifact_id, second.artifact_id)
        self.assertEqual(1, self.candidate_count())
        self.assertEqual(2, self.get_control().control_version)

    def test_retry_with_a_different_artifact_id_conflicts(self) -> None:
        context = self.make_context()
        interaction = self.make_review_interaction(context_id=context)
        first = self.candidate(
            context_id=context,
            interaction_id=interaction,
            request_id=REQ_1,
            artifact_id=None,
        )
        with self.assertRaises(CommandConflict):
            self.candidate(
                context_id=context,
                interaction_id=interaction,
                request_id=REQ_1,
                artifact_id=CAND_2,
            )
        self.assertEqual(first.artifact_id, self.reload(first).artifact_id)
        self.assertEqual(1, self.candidate_count())
        self.assertEqual(2, self.get_control().control_version)

    def test_old_generation_retry_returns_the_rejected_candidate_unchanged(
        self,
    ) -> None:
        context = self.make_context()
        interaction = self.make_review_interaction(context_id=context, generation=0)
        candidate = self.candidate(
            context_id=context,
            interaction_id=interaction,
            request_id=REQ_1,
            generation=0,
        )
        rejected, control = self.reject(candidate)
        retried = self.candidate(
            context_id=context,
            interaction_id=interaction,
            request_id=REQ_1,
            generation=0,
        )
        self.assertEqual(candidate.artifact_id, retried.artifact_id)
        self.assertEqual("rejected", retried.status)
        self.assertEqual(rejected.decided_at, retried.decided_at)
        self.assertEqual(rejected.decision_ref, retried.decision_ref)
        current = self.get_control()
        self.assertEqual(control.generation, current.generation)
        self.assertEqual(control.control_version, current.control_version)
        self.assertEqual(candidate.artifact_id, current.last_candidate_ref)
        self.assertEqual(1, self.candidate_count())

    def test_commit_primitive_rejects_a_different_command_digest(self) -> None:
        candidate = self.candidate()
        receipt = self.txb_receipt(
            candidate, payload={"operation": "belief.propose"}
        )
        self.assertEqual("noop", receipt.outcome)
        self.assertNotEqual(
            candidate.command_digest,
            self.store.lookup_command_request_digest(
                namespace=NS, request_id=candidate.request_id
            ),
        )
        control_before = self.get_control()
        commands_before = self.table_count("processed_commands")
        with self.assertRaises(CandidateTransitionDenied):
            with self.store.transaction():
                self.store._mark_candidate_committed_locked(
                    namespace=NS,
                    candidate_id=candidate.artifact_id,
                    decision_ref="decision:commit:digest",
                )
        loaded = self.reload(candidate)
        self.assertEqual("pending", loaded.status)
        self.assertIsNone(loaded.decided_at)
        self.assertIsNone(loaded.decision_ref)
        control_after = self.get_control()
        self.assertEqual(control_before.generation, control_after.generation)
        self.assertEqual(
            control_before.control_version, control_after.control_version
        )
        self.assertEqual(
            commands_before, self.table_count("processed_commands")
        )

    def test_savepoint_conflict_leaves_no_control_payload_or_link(self) -> None:
        source = self.new_input()
        context = self.make_context()
        interaction = self.make_review_interaction(context_id=context)
        first = self.store.register_artifact(
            namespace=NS,
            kind="candidate",
            interaction_id=interaction,
            context_id=context,
            origin_kind="model_output",
            body="synthetic candidate body",
            retention="standard",
            sources=[self.input_ref(source)],
            artifact_id=CAND_1,
            artifact_version=1,
            created_at=TS,
            proposal={"operation": "belief.propose", "reason": "synthetic"},
            proposal_version="v1",
            work_id=WORK_ID,
            work_generation=0,
            request_id=REQ_1,
            status="pending",
        )
        self.assertEqual(0, self.table_count("collab_work_control"))
        payloads = self.table_count("private_payloads")
        links = self.table_count("collab_source_links")
        real = self.store._load_candidate_by_request_locked
        calls = {"n": 0}

        def miss_once(namespace, request_id):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(namespace, request_id)

        with mock.patch.object(
            self.store,
            "_load_candidate_by_request_locked",
            side_effect=miss_once,
        ):
            again = self.candidate(
                request_id=REQ_1,
                context_id=context,
                interaction_id=interaction,
                sources=[self.input_ref(source)],
            )

        self.assertEqual(first.artifact_id, again.artifact_id)
        self.assertEqual(1, self.candidate_count())
        self.assertEqual(0, self.table_count("collab_work_control"))
        self.assertEqual(payloads, self.table_count("private_payloads"))
        self.assertEqual(links, self.table_count("collab_source_links"))

        calls["n"] = 0
        payloads = self.table_count("private_payloads")
        links = self.table_count("collab_source_links")
        with mock.patch.object(
            self.store,
            "_load_candidate_by_request_locked",
            side_effect=miss_once,
        ):
            with self.assertRaises(CommandConflict):
                self.candidate(
                    request_id=REQ_1,
                    context_id=context,
                    interaction_id=interaction,
                    sources=[self.input_ref(source)],
                    body="a different synthetic candidate body",
                )
        self.assertEqual(1, self.candidate_count())
        self.assertEqual(0, self.table_count("collab_work_control"))
        self.assertEqual(payloads, self.table_count("private_payloads"))
        self.assertEqual(links, self.table_count("collab_source_links"))


if __name__ == "__main__":
    unittest.main()
