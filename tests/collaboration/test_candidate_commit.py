"""C06a: the candidate commit execution point.

Every test runs on a fresh migrated database with a real PCS2-008
``ContextService``: ``ingest -> build_context -> register_candidate ->
commit_candidate``.  No test uses a bare PCS2-006 snapshot, because the commit
guard re-validates the stored Context and its exact sources.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for _path in (REPO_ROOT / "tests" / "domain", REPO_ROOT / "tests" / "store",
              REPO_ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.collaboration import (  # noqa: E402
    CandidateCommitOutcome,
    CollaborationService,
    CommitAuthority,
)
from quintessence_pcs.foundation.errors import (  # noqa: E402
    AuthorizationRequired,
    ContextStale,
    CorruptStore,
    InvalidInput,
)
from quintessence_pcs.foundation.jsonutil import (  # noqa: E402
    canonical_json_text,
    sha256_hex,
)
from quintessence_pcs.store.collaboration import Approval  # noqa: E402
from quintessence_pcs.store.errors import CandidateTransitionDenied  # noqa: E402

WORK_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CAND_A = "a1111111-1111-4111-8111-111111111111"
CAND_B = "a2222222-2222-4222-8222-222222222222"
REQ_A = "a1a1a1a1-1111-4111-8111-aaaaaaaaaaaa"
REQ_B = "b2b2b2b2-2222-4222-8222-bbbbbbbbbbbb"
GRANT_COMMIT = "15151515-1515-4515-8515-151515151515"
GRANT_COMMIT_2 = "15151516-1516-4516-8516-151515151516"
GRANT_COMMIT_3 = "15151517-1517-4517-8517-151515151517"
GRANT_COMMIT_4 = "15151518-1518-4518-8518-151515151518"
GRANT_ACCESS = "16161616-1616-4616-8616-161616161616"
GRANT_EGRESS = "18181818-1818-4818-8818-181818181818"
GRANT_RERUN = "14141414-1414-4414-8414-141414141414"
TS = "2026-10-22T00:00:00Z"
DECISION = "decision:commit:1"
MANIFEST = "sha256:" + "0" * 64

_ARTIFACT_COLUMNS = (
    "namespace",
    "artifact_id",
    "artifact_version",
    "schema_version",
    "kind",
    "interaction_id",
    "context_id",
    "attempt_ref_json",
    "origin_kind",
    "body_payload_ref",
    "content_digest",
    "retention_policy",
    "created_at",
    "proposal_payload_ref",
    "proposal_digest",
    "proposal_version",
    "work_id",
    "work_generation",
    "request_id",
    "command_digest",
    "structural_validation",
    "rerun_authorization_ref",
    "commit_authorization_ref",
    "status",
    "decided_at",
    "decision_ref",
)


class CandidateCommitTests(CognitionTestCase):
    """C06a: one trusted atomic commit path for one pending candidate."""

    def setUp(self) -> None:
        super().setUp()
        self.service = CollaborationService(self.store, belief_service=self.beliefs)

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
                raise AssertionError(f"unexpected context source {source.ref_kind!r}")
        return refs

    def input_ref(self, row) -> dict:
        return {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": row.content_version,
        }

    def default_proposal(self, event) -> dict:
        return {
            "operation": "propose_belief",
            "body": self.belief_body(
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))]
            ),
            "scope": self.default_scope("capability"),
        }

    def make_candidate(
        self,
        *,
        event=None,
        context=None,
        proposal=None,
        request_id=REQ_A,
        artifact_id=CAND_A,
        extra_sources=(),
        activation_kind="manual_review",
    ):
        event = self.ingest() if event is None else event
        context = (
            self.build_context(event.event_id) if context is None else context
        )
        interaction = self.store.register_interaction(
            namespace=NS,
            mode="review",
            activation_kind=activation_kind,
            host_instance="host:synthetic-1",
            access_ref="authorization:access:1",
            policy_ref="policy:collab.v1",
            context_id=context.context_id,
            trigger_ref=self.event_ref(event.event_id),
            work_id=WORK_ID,
            work_generation=0,
        )
        if proposal is None:
            proposal = self.default_proposal(event)
        candidate = self.store.register_candidate(
            namespace=NS,
            interaction_id=interaction,
            context_id=context.context_id,
            body="raw model output for a synthetic candidate",
            retention="standard",
            proposal=proposal,
            proposal_version="v1",
            work_id=WORK_ID,
            work_generation=0,
            request_id=request_id,
            artifact_id=artifact_id,
            sources=[
                *self.context_source_refs(context),
                *extra_sources,
            ],
        )
        return candidate, context, event

    def approval_for(self, candidate) -> Approval:
        return Approval(
            candidate_id=candidate.artifact_id,
            proposal_digest=candidate.proposal_digest,
            context_id=candidate.context_id,
            request_id=candidate.request_id,
        )

    def commit(
        self,
        candidate,
        *,
        authority=None,
        decision_ref=DECISION,
        service=None,
    ) -> CandidateCommitOutcome:
        if authority is None:
            authority = CommitAuthority(approval=self.approval_for(candidate))
        return (service or self.service).commit_candidate(
            namespace=NS,
            candidate_id=candidate.artifact_id,
            authority=authority,
            decision_ref=decision_ref,
        )

    def grant_common(self, *, grant_id: str, kind: str) -> dict:
        return {
            "grant_id": grant_id,
            "namespace": NS,
            "schema_version": "collab.v1",
            "kind": kind,
            "grant_version": 1,
            "subject": "host:synthetic-1",
            "authorization_ref": "authorization:commit:1",
            "issued_at": TS,
            "expires_at": None,
            "revoked_at": None,
        }

    def grant_commit_policy(
        self,
        *,
        grant_id: str = GRANT_COMMIT,
        operations=("propose_belief",),
        trigger_kinds=("manual_review",),
    ) -> dict:
        return dict(
            self.grant_common(grant_id=grant_id, kind="commit"),
            scope="policy",
            operations=list(operations),
            trigger_kinds=list(trigger_kinds),
        )

    def grant_commit_work(
        self, *, grant_id: str = GRANT_COMMIT, generation: int = 0
    ) -> dict:
        return dict(
            self.grant_common(grant_id=grant_id, kind="commit"),
            scope="work_generation",
            work_id=WORK_ID,
            generation=generation,
        )

    def grant_access(self) -> dict:
        return dict(
            self.grant_common(grant_id=GRANT_ACCESS, kind="access"),
            purposes=["consult"],
            data_classes=["private"],
        )

    def grant_egress(self) -> dict:
        return dict(
            self.grant_common(grant_id=GRANT_EGRESS, kind="egress"),
            provider="synthetic-provider",
            endpoint="https://model.invalid/v1",
            model="synthetic-model",
            manifest_hash=MANIFEST,
            privacy_classes=["private"],
            purposes=["consult"],
            model_authorization_ref="19191919-1919-4919-8919-191919191919",
        )

    def grant_rerun(self) -> dict:
        return dict(
            self.grant_common(grant_id=GRANT_RERUN, kind="rerun"),
            work_id=WORK_ID,
            generation=0,
        )

    # --------------------------------------------------------------- probes

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
            "work_progress": int(
                connection.execute(
                    "SELECT COUNT(*) FROM work_progress"
                ).fetchone()[0]
            ),
        }

    def candidate_status(self, candidate) -> str | None:
        row = self.store.connection.execute(
            "SELECT status FROM collab_artifacts "
            "WHERE namespace = ? AND artifact_id = ? AND artifact_version = 1",
            (NS, candidate.artifact_id),
        ).fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def assert_no_writes(self, baseline: dict[str, int]) -> None:
        self.assertEqual(baseline, self.counts())

    def assert_receipt_binds_proposal(self, candidate, receipt) -> None:
        """The command identity is exactly the candidate's original proposal."""

        proposal = candidate.proposal
        expected = sha256_hex(
            canonical_json_text(
                {
                    "context_id": candidate.context_id,
                    "namespace": NS,
                    "operation": proposal["operation"],
                    "request_payload": proposal,
                }
            )
        )
        self.assertEqual(candidate.command_digest, expected)
        self.assertEqual(
            expected,
            self.store.lookup_command_request_digest(
                namespace=NS, request_id=candidate.request_id
            ),
        )
        self.assertEqual(proposal["operation"], receipt.operation)

    def insert_artifact_row(
        self,
        *,
        kind: str,
        context_id: str,
        payload_ref: str,
        digest: str,
        **overrides,
    ) -> None:
        values = {
            "namespace": NS,
            "artifact_id": str(uuid.uuid4()),
            "artifact_version": 1,
            "schema_version": "collab.v1",
            "kind": kind,
            "interaction_id": str(uuid.uuid4()),
            "context_id": context_id,
            "attempt_ref_json": None,
            "origin_kind": "model_output",
            "body_payload_ref": payload_ref,
            "content_digest": digest,
            "retention_policy": "standard",
            "created_at": TS,
            "proposal_payload_ref": None,
            "proposal_digest": None,
            "proposal_version": None,
            "work_id": None,
            "work_generation": None,
            "request_id": None,
            "command_digest": None,
            "structural_validation": None,
            "rerun_authorization_ref": None,
            "commit_authorization_ref": None,
            "status": None,
            "decided_at": None,
            "decision_ref": None,
        }
        values.update(overrides)
        self.store.connection.execute(
            "INSERT INTO collab_artifacts ("
            + ", ".join(_ARTIFACT_COLUMNS)
            + ") VALUES ("
            + ", ".join("?" for _ in _ARTIFACT_COLUMNS)
            + ")",
            tuple(values[column] for column in _ARTIFACT_COLUMNS),
        )

    # ------------------------------------------------------------ the tests

    def test_propose_belief_commits_candidate(self) -> None:
        candidate, _context, _event = self.make_candidate()
        baseline = self.counts()
        outcome = self.commit(candidate)

        self.assertEqual("committed", outcome.candidate.status)
        self.assertTrue(outcome.appended)
        self.assertFalse(outcome.replayed)
        self.assertIsNone(outcome.candidate.commit_authorization_ref)
        self.assertEqual(DECISION, outcome.candidate.decision_ref)
        self.assert_receipt_binds_proposal(candidate, outcome.receipt)
        self.assertEqual(REQ_A, outcome.receipt.request_id)
        self.assertEqual(1, self.counts()["commands"])
        self.assertEqual(baseline["beliefs"] + 1, self.counts()["beliefs"])
        self.assertEqual(baseline["revision"] + 1, self.counts()["revision"])
        self.assertEqual(1, len(outcome.receipt.record_refs))
        ref = outcome.receipt.record_refs[0]
        version = self.store.get_version(NS, ref.record_id, ref.record_version)
        self.assertEqual("belief", version.kind)
        self.assertEqual("supported", version.status)

    def test_manual_approval_requires_exact_binding(self) -> None:
        candidate, _context, _event = self.make_candidate()
        baseline = self.counts()
        good = self.approval_for(candidate)
        tampered = [
            Approval(
                candidate_id=str(uuid.uuid4()),
                proposal_digest=good.proposal_digest,
                context_id=good.context_id,
                request_id=good.request_id,
            ),
            Approval(
                candidate_id=good.candidate_id,
                proposal_digest="sha256:" + "0" * 64,
                context_id=good.context_id,
                request_id=good.request_id,
            ),
            Approval(
                candidate_id=good.candidate_id,
                proposal_digest=good.proposal_digest,
                context_id=str(uuid.uuid4()),
                request_id=good.request_id,
            ),
            Approval(
                candidate_id=good.candidate_id,
                proposal_digest=good.proposal_digest,
                context_id=good.context_id,
                request_id=str(uuid.uuid4()),
            ),
        ]
        for approval in tampered:
            with self.assertRaises(AuthorizationRequired):
                self.commit(candidate, authority=CommitAuthority(approval=approval))
            self.assertEqual("pending", self.candidate_status(candidate))
            self.assert_no_writes(baseline)

    def test_approval_for_another_candidate_is_denied(self) -> None:
        candidate, context, event = self.make_candidate()
        other, _context, _event = self.make_candidate(
            event=event,
            context=context,
            request_id=REQ_B,
            artifact_id=CAND_B,
        )
        baseline = self.counts()
        with self.assertRaises(AuthorizationRequired):
            self.commit(
                candidate,
                authority=CommitAuthority(approval=self.approval_for(other)),
            )
        self.assertEqual("pending", self.candidate_status(candidate))
        self.assertEqual("pending", self.candidate_status(other))
        self.assert_no_writes(baseline)

    def test_commit_grant_work_generation_scope_commits(self) -> None:
        candidate, _context, _event = self.make_candidate()
        self.store.register_grant(self.grant_commit_work(generation=0))
        outcome = self.commit(
            candidate, authority=CommitAuthority(grant_id=GRANT_COMMIT)
        )
        self.assertEqual("committed", outcome.candidate.status)
        self.assertEqual(GRANT_COMMIT, outcome.candidate.commit_authorization_ref)
        self.assertEqual(DECISION, outcome.candidate.decision_ref)
        self.assertEqual("propose_belief", outcome.receipt.operation)

    def test_commit_grant_wrong_generation_is_denied(self) -> None:
        candidate, _context, _event = self.make_candidate()
        self.store.register_grant(self.grant_commit_work(generation=1))
        baseline = self.counts()
        with self.assertRaises(AuthorizationRequired):
            self.commit(
                candidate, authority=CommitAuthority(grant_id=GRANT_COMMIT)
            )
        self.assertEqual("pending", self.candidate_status(candidate))
        self.assert_no_writes(baseline)

    def test_commit_grant_policy_scope_checks_operation_and_trigger(self) -> None:
        candidate, _context, _event = self.make_candidate()
        baseline = self.counts()

        self.store.register_grant(
            self.grant_commit_policy(
                grant_id=GRANT_COMMIT,
                operations=("revise_belief", "retract_belief"),
                trigger_kinds=("manual_review",),
            )
        )
        with self.assertRaises(AuthorizationRequired):
            self.commit(
                candidate, authority=CommitAuthority(grant_id=GRANT_COMMIT)
            )
        self.assertEqual("pending", self.candidate_status(candidate))
        self.assert_no_writes(baseline)

        self.store.register_grant(
            self.grant_commit_policy(
                grant_id=GRANT_COMMIT_2,
                operations=("propose_belief",),
                trigger_kinds=("user_input",),
            )
        )
        with self.assertRaises(AuthorizationRequired):
            self.commit(
                candidate, authority=CommitAuthority(grant_id=GRANT_COMMIT_2)
            )
        self.assertEqual("pending", self.candidate_status(candidate))
        self.assert_no_writes(baseline)

        self.store.register_grant(
            self.grant_commit_policy(
                grant_id=GRANT_COMMIT_3,
                operations=("propose_belief",),
                trigger_kinds=("manual_review",),
            )
        )
        real_kind = self.store.interaction_activation_kind
        depths: list[int] = []

        def read_kind(*, namespace: str, interaction_id: str):
            # The guard must re-read the persisted activation_kind inside the
            # write transaction (depth 1), never only before it (depth 0).
            depths.append(self.store._tx_depth)
            return real_kind(namespace=namespace, interaction_id=interaction_id)

        with mock.patch.object(
            self.store, "interaction_activation_kind", side_effect=read_kind
        ):
            outcome = self.commit(
                candidate, authority=CommitAuthority(grant_id=GRANT_COMMIT_3)
            )
        self.assertEqual(0, depths[0])
        self.assertIn(1, depths)
        self.assertEqual("committed", outcome.candidate.status)
        self.assertEqual(GRANT_COMMIT_3, outcome.candidate.commit_authorization_ref)

    def test_revoked_or_expired_grant_is_denied(self) -> None:
        candidate, _context, _event = self.make_candidate()
        self.store.register_grant(
            dict(
                self.grant_commit_work(grant_id=GRANT_COMMIT),
                expires_at="2026-10-21T00:00:00Z",
            )
        )
        self.store.register_grant(
            dict(
                self.grant_commit_work(grant_id=GRANT_COMMIT_2),
                revoked_at=TS,
            )
        )
        baseline = self.counts()
        clock_calls: list[int] = []

        def trusted_clock() -> str:
            # The trusted time is read inside the Tx B write transaction, never
            # once at construction time.
            clock_calls.append(self.store._tx_depth)
            return TS

        service = CollaborationService(self.store, clock=trusted_clock)
        self.assertEqual([], clock_calls)
        with self.assertRaises(AuthorizationRequired):
            self.commit(
                candidate,
                authority=CommitAuthority(grant_id=GRANT_COMMIT),
                service=service,
            )
        with self.assertRaises(AuthorizationRequired):
            self.commit(
                candidate,
                authority=CommitAuthority(grant_id=GRANT_COMMIT_2),
                service=service,
            )
        self.assertEqual([1, 1], clock_calls)
        self.assertEqual("pending", self.candidate_status(candidate))
        self.assert_no_writes(baseline)

    def test_egress_or_access_grant_alone_is_denied(self) -> None:
        candidate, _context, _event = self.make_candidate()
        self.store.register_grant(self.grant_access())
        self.store.register_grant(self.grant_egress())
        self.store.register_grant(self.grant_rerun())
        baseline = self.counts()
        for grant_id in (GRANT_ACCESS, GRANT_EGRESS, GRANT_RERUN):
            with self.assertRaises(AuthorizationRequired):
                self.commit(
                    candidate, authority=CommitAuthority(grant_id=grant_id)
                )
            self.assertEqual("pending", self.candidate_status(candidate))
            self.assert_no_writes(baseline)

    def test_approval_and_grant_together_are_rejected(self) -> None:
        candidate, _context, _event = self.make_candidate()
        approval = self.approval_for(candidate)
        with self.assertRaises(InvalidInput):
            CommitAuthority(approval=approval, grant_id=GRANT_COMMIT)
        with self.assertRaises(InvalidInput):
            CommitAuthority()
        with self.assertRaises(InvalidInput):
            CommitAuthority(
                approval={
                    "candidate_id": candidate.artifact_id,
                    "proposal_digest": candidate.proposal_digest,
                    "context_id": candidate.context_id,
                    "request_id": candidate.request_id,
                }
            )
        baseline = self.counts()
        with self.assertRaises(InvalidInput):
            self.commit(
                candidate,
                authority={
                    "approval": approval,
                    "grant_id": GRANT_COMMIT,
                },
            )
        self.assertEqual("pending", self.candidate_status(candidate))
        self.assert_no_writes(baseline)

    def test_non_belief_or_invalid_proposal_is_rejected(self) -> None:
        wait_candidate, context, event = self.make_candidate(
            proposal={"operation": "wait", "reason": "synthetic wait"},
            request_id=REQ_A,
            artifact_id=CAND_A,
        )
        baseline = self.counts()
        with self.assertRaises(CandidateTransitionDenied):
            self.commit(wait_candidate)
        self.assertEqual("pending", self.candidate_status(wait_candidate))
        self.assert_no_writes(baseline)

        malformed, _context, _event = self.make_candidate(
            event=event,
            context=context,
            proposal={"operation": "propose_belief"},
            request_id=REQ_B,
            artifact_id=CAND_B,
        )
        with self.assertRaises(InvalidInput):
            self.commit(malformed)
        self.assertEqual("pending", self.candidate_status(malformed))
        self.assert_no_writes(baseline)

    def test_rejected_or_stale_candidate_is_denied(self) -> None:
        first, context, event = self.make_candidate(request_id=REQ_A, artifact_id=CAND_A)
        second, _context, _event = self.make_candidate(
            event=event,
            context=context,
            request_id=REQ_B,
            artifact_id=CAND_B,
        )
        self.store.reject_candidate(
            namespace=NS,
            candidate_id=first.artifact_id,
            decision_ref="decision:reject:1",
        )
        baseline = self.counts()
        with self.assertRaises(CandidateTransitionDenied):
            self.commit(first)
        self.assertEqual("rejected", self.candidate_status(first))
        with self.assertRaises(CandidateTransitionDenied):
            self.commit(second)
        self.assertEqual("stale", self.candidate_status(second))
        self.assert_no_writes(baseline)

    def test_duplicate_commit_returns_the_same_receipt(self) -> None:
        candidate, _context, _event = self.make_candidate()
        first = self.commit(candidate)
        baseline = self.counts()
        second = self.commit(candidate)

        self.assertTrue(second.replayed)
        self.assertEqual(first.receipt, second.receipt)
        self.assertEqual(DECISION, second.candidate.decision_ref)
        self.assert_receipt_binds_proposal(candidate, second.receipt)
        self.assertEqual(1, baseline["beliefs"])
        self.assertEqual(1, baseline["commands"])
        self.assert_no_writes(baseline)

    def test_commit_that_loses_to_a_rejection_writes_nothing(self) -> None:
        candidate, _context, _event = self.make_candidate()
        baseline = self.counts()
        real = self.store.lookup_command_receipt
        calls = {"count": 0}

        def lose_race(*, namespace: str, request_id: str):
            calls["count"] += 1
            if calls["count"] == 1:
                self.store.reject_candidate(
                    namespace=namespace,
                    candidate_id=candidate.artifact_id,
                    decision_ref="decision:reject:race",
                )
            return real(namespace=namespace, request_id=request_id)

        with mock.patch.object(
            self.store, "lookup_command_receipt", side_effect=lose_race
        ):
            with self.assertRaises(CandidateTransitionDenied):
                self.commit(candidate)

        self.assertEqual(1, calls["count"])
        self.assertEqual("rejected", self.candidate_status(candidate))
        self.assertEqual(baseline["revision"], self.counts()["revision"])
        self.assertEqual(0, self.counts()["beliefs"])
        self.assertEqual(0, self.counts()["commands"])

    def test_mark_failure_rolls_back_the_belief_append(self) -> None:
        candidate, _context, _event = self.make_candidate()
        baseline = self.counts()
        with mock.patch.object(
            self.store,
            "_mark_candidate_committed_locked",
            side_effect=CorruptStore("synthetic mark failure"),
        ):
            with self.assertRaises(CorruptStore):
                self.commit(candidate)
        self.assertEqual("pending", self.candidate_status(candidate))
        self.assert_no_writes(baseline)

    def test_domain_apply_failure_writes_nothing(self) -> None:
        candidate, _context, _event = self.make_candidate()
        baseline = self.counts()
        with mock.patch.object(
            self.beliefs,
            "_resolve_evidence_locked",
            side_effect=CorruptStore("synthetic domain failure"),
        ):
            with self.assertRaises(CorruptStore):
                self.commit(candidate)
        self.assertEqual("pending", self.candidate_status(candidate))
        self.assert_no_writes(baseline)

    def test_deleted_candidate_source_blocks_commit(self) -> None:
        source = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="synthetic collaboration input body",
            privacy_class="private",
            retention="standard",
        )
        candidate, _context, _event = self.make_candidate(
            extra_sources=[self.input_ref(source)]
        )
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=source.input_id,
            ref_version=source.content_version,
            authorization_ref="authorization:delete:1",
        )
        with self.assertRaises(ContextStale):
            self.commit(candidate)
        self.assertEqual("pending", self.candidate_status(candidate))
        self.assertEqual(0, self.counts()["beliefs"])
        self.assertEqual(0, self.counts()["commands"])

    def test_deleted_context_source_blocks_commit(self) -> None:
        event = self.ingest()
        context = self.build_context(event.event_id)
        candidate, _context, _event = self.make_candidate(
            event=event, context=context
        )
        self.store.request_deletion(
            target_kind="event",
            namespace=NS,
            target_id=event.event_id,
            authorization_ref="authorization:delete:1",
        )
        with self.assertRaises(ContextStale):
            self.commit(candidate)
        self.assertEqual("pending", self.candidate_status(candidate))
        self.assertEqual(0, self.counts()["beliefs"])
        self.assertEqual(0, self.counts()["commands"])

    def test_tampered_command_digest_is_rejected(self) -> None:
        candidate, context, _event = self.make_candidate()
        row = self.store.connection.execute(
            "SELECT body_payload_ref, proposal_payload_ref FROM collab_artifacts "
            "WHERE namespace = ? AND artifact_id = ? AND artifact_version = 1",
            (NS, candidate.artifact_id),
        ).fetchone()
        self.assertIsNotNone(row)
        body_ref = row["body_payload_ref"]
        proposal_ref = row["proposal_payload_ref"]
        self.insert_artifact_row(
            kind="candidate",
            context_id=candidate.context_id,
            payload_ref=body_ref,
            digest=candidate.content_digest,
            artifact_id=CAND_B,
            interaction_id=candidate.interaction_id,
            proposal_payload_ref=proposal_ref,
            proposal_digest=candidate.proposal_digest,
            proposal_version="v1",
            work_id=WORK_ID,
            work_generation=0,
            request_id=REQ_B,
            command_digest="f" * 64,
            structural_validation="passed",
            status="pending",
        )
        tampered = self.store.load_artifact(
            namespace=NS, artifact_id=CAND_B, artifact_version=1
        )
        baseline = self.counts()
        authority = CommitAuthority(
            approval=Approval(
                candidate_id=CAND_B,
                proposal_digest=tampered.proposal_digest,
                context_id=tampered.context_id,
                request_id=REQ_B,
            )
        )
        with self.assertRaises(CorruptStore):
            self.commit(tampered, authority=authority)
        self.assertEqual("pending", self.candidate_status(tampered))
        self.assert_no_writes(baseline)

    def test_commit_is_durable_across_reopen_and_replays(self) -> None:
        candidate, _context, _event = self.make_candidate()
        first = self.commit(candidate)
        second_store = self.open_second_store()
        service = CollaborationService(second_store)
        second = service.commit_candidate(
            namespace=NS,
            candidate_id=candidate.artifact_id,
            authority=CommitAuthority(approval=self.approval_for(candidate)),
            decision_ref="decision:commit:2",
        )
        self.assertTrue(second.replayed)
        self.assertEqual(first.receipt, second.receipt)
        self.assertEqual("committed", second.candidate.status)
        self.assertEqual(DECISION, second.candidate.decision_ref)
        self.assertEqual(1, self.counts()["beliefs"])
        self.assertEqual(1, self.counts()["commands"])

    def test_commit_does_not_write_single_step_work_tables(self) -> None:
        candidate, _context, _event = self.make_candidate()
        baseline = self.counts()
        pending_before = int(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM pending_work"
            ).fetchone()[0]
        )
        outcome = self.commit(candidate)
        self.assertEqual("committed", outcome.candidate.status)
        self.assertEqual(0, self.counts()["work_progress"])
        self.assertEqual(
            pending_before,
            int(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM pending_work"
                ).fetchone()[0]
            ),
        )
        self.assertEqual(
            0,
            int(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM pending_work WHERE work_id = ?",
                    (WORK_ID,),
                ).fetchone()[0]
            ),
        )
        self.assertEqual(baseline["revision"] + 1, self.counts()["revision"])

    def test_revise_belief_commits(self) -> None:
        event = self.ingest()
        link = self.evidence_link(self.event_ref(event.event_id))
        version = self.propose(body=self.belief_body(evidence_links=[link]))
        self.assertEqual(1, version.record_version)
        context = self.build_context(event.event_id)
        proposal = {
            "operation": "revise_belief",
            "target": self.record_ref(version),
            "body": self.belief_body(
                claim=version.body["claim"],
                evidence_links=[link],
                strength_reason="revised synthetic strength reason",
            ),
            "scope": self.default_scope("capability"),
        }
        candidate, _context, _event = self.make_candidate(
            event=event, context=context, proposal=proposal
        )
        baseline = self.counts()
        outcome = self.commit(candidate)

        self.assertEqual("committed", outcome.candidate.status)
        self.assertFalse(outcome.replayed)
        self.assert_receipt_binds_proposal(candidate, outcome.receipt)
        self.assertEqual("revise_belief", outcome.receipt.operation)
        current = self.store.get_current(NS, version.record_id)
        self.assertEqual(2, current.record_version)
        self.assertEqual(version.body["claim"], current.body["claim"])
        self.assertEqual(baseline["revision"] + 1, self.counts()["revision"])

    def test_retract_belief_commits(self) -> None:
        event = self.ingest()
        link = self.evidence_link(self.event_ref(event.event_id))
        version = self.propose(body=self.belief_body(evidence_links=[link]))
        context = self.build_context(event.event_id)
        proposal = {
            "operation": "retract_belief",
            "target": self.record_ref(version),
            "evidence_links": [link],
            "reason": "synthetic retraction",
        }
        candidate, _context, _event = self.make_candidate(
            event=event, context=context, proposal=proposal
        )
        outcome = self.commit(candidate)

        self.assertEqual("committed", outcome.candidate.status)
        self.assertFalse(outcome.replayed)
        self.assert_receipt_binds_proposal(candidate, outcome.receipt)
        self.assertEqual("retract_belief", outcome.receipt.operation)
        current = self.store.get_current(NS, version.record_id)
        self.assertEqual(2, current.record_version)
        self.assertEqual("retracted", current.status)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
