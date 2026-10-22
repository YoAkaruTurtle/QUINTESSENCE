"""C06b: review gating, candidate resolution and managed-work isolation.

Every test uses a fresh migrated database and the real PCS2-008 Context
service.  The fixture is the minimal subset of the C06a tests, not a subclass:
the 22 C06a tests must not run again from this module.
"""

from __future__ import annotations

import sys
import unittest
import uuid
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
    Resolution,
)
from quintessence_pcs.foundation.errors import InvalidInput  # noqa: E402
from quintessence_pcs.runtime import CognitionRuntime  # noqa: E402
from quintessence_pcs.runtime.fake_model import FakeModel  # noqa: E402
from quintessence_pcs.runtime.step import (  # noqa: E402
    OUTCOME_BLOCKED,
    OUTCOME_MANAGED,
)
from quintessence_pcs.store.collaboration import Approval  # noqa: E402
from quintessence_pcs.store.errors import CandidateTransitionDenied  # noqa: E402

WORK_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CAND_A = "a1111111-1111-4111-8111-111111111111"
CAND_B = "a2222222-2222-4222-8222-222222222222"
REQ_A = "a1a1a1a1-1111-4111-8111-aaaaaaaaaaaa"
REQ_B = "b2b2b2b2-2222-4222-8222-bbbbbbbbbbbb"
GRANT_RERUN = "14141414-1414-4414-8414-141414141414"
TS = "2026-10-22T00:00:00Z"


class _TakeoverModel:
    """A model that creates a WorkControl while generation is running.

    It deterministically covers the real race window: the pre-check saw no
    control, the model creates one, and the Tx B guard must refuse the commit.
    """

    adapter_kind = FakeModel.adapter_kind
    policy_version = FakeModel.policy_version
    model_ref = FakeModel.model_ref

    def __init__(self, store, *, work_id: str, blocked: bool) -> None:
        self._store = store
        self._work_id = work_id
        self._blocked = blocked
        self._delegate = FakeModel()
        self.calls = 0

    def propose(self, *, rendered_input, references, feedback=None):
        self.calls += 1
        if self.calls == 1:
            self._store.register_work_control(
                namespace=NS,
                work_id=self._work_id,
                generation=1 if self._blocked else 0,
                blocked_after_rejection=self._blocked,
                control_version=1,
                block_reason="rejected" if self._blocked else None,
            )
        return self._delegate.propose(
            rendered_input=rendered_input,
            references=references,
            feedback=feedback,
        )


class _WaitTakeoverModel(_TakeoverModel):
    """Same takeover, but the model answers WAIT instead of a proposal."""

    def propose(self, *, rendered_input, references, feedback=None):
        self.calls += 1
        if self.calls == 1:
            self._store.register_work_control(
                namespace=NS,
                work_id=self._work_id,
                generation=0,
                blocked_after_rejection=False,
                control_version=1,
            )
        return '{"operation": "wait", "reason": "synthetic takeover wait"}'


class ReviewControlTests(CognitionTestCase):
    """C06b review gate, resolution and old-path isolation."""

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
        extra_sources=(),
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
            "sources": [
                *self.context_source_refs(context),
                *extra_sources,
            ],
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
        extra_sources=(),
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
            extra_sources=extra_sources,
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

    def decide(self, candidate, decision: str, *, decision_ref: str = "decision:1",
               authority=None) -> Resolution:
        return self.service.resolve_candidate(
            namespace=NS,
            candidate_id=candidate.artifact_id,
            decision=decision,
            decision_ref=decision_ref,
            authority=authority,
        )

    def approve(self, candidate, *, decision_ref: str = "decision:approve:1"):
        return self.decide(
            candidate,
            DECISION_APPROVE,
            decision_ref=decision_ref,
            authority=CommitAuthority(approval=self.approval_for(candidate)),
        )

    def reject(self, candidate, *, decision_ref: str = "decision:reject:1"):
        return self.decide(
            candidate, DECISION_REJECT, decision_ref=decision_ref
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
            "work_progress": int(
                connection.execute(
                    "SELECT COUNT(*) FROM work_progress"
                ).fetchone()[0]
            ),
            "pending_work": int(
                connection.execute(
                    "SELECT COUNT(*) FROM pending_work"
                ).fetchone()[0]
            ),
        }

    def assert_no_writes(self, baseline: dict[str, int]) -> None:
        self.assertEqual(baseline, self.counts())

    def real_work(self, *, event_type: str = "CapabilityObserved:file-read"):
        event = self.ingest(event_type=event_type)
        works = self.store.unfinished_work_for_event(NS, event.event_id)
        self.assertTrue(works)
        return event, works[0]

    def register_rerun(self, work) -> None:
        self.store.register_grant(
            {
                "grant_id": GRANT_RERUN,
                "namespace": NS,
                "schema_version": "collab.v1",
                "kind": "rerun",
                "grant_version": 1,
                "subject": "host:synthetic-1",
                "authorization_ref": "authorization:rerun:1",
                "issued_at": TS,
                "expires_at": None,
                "revoked_at": None,
                "work_id": work.work_id,
                "generation": 1,
            }
        )
        self.store.authorize_rerun(
            namespace=NS, work_id=work.work_id, grant_id=GRANT_RERUN
        )

    def rejected_work(self):
        event, work = self.real_work()
        candidate, _context, _event = self.make_candidate(
            event=event,
            work_id=work.work_id,
            work_generation=0,
        )
        rejection = self.reject(candidate)
        self.assertEqual("rejected", rejection.status)
        self.assertEqual(1, rejection.control.generation)
        return event, work, candidate

    def work_progress_for(self, work_id: str):
        return self.store.work_progress(work_id)

    # ---------------------------------------------------------- resolution

    def test_resolve_approve_pending_commits(self) -> None:
        candidate, _context, _event = self.make_candidate()
        resolution = self.approve(candidate)

        self.assertIsInstance(resolution, Resolution)
        self.assertEqual("committed", resolution.status)
        self.assertIsNotNone(resolution.receipt)
        self.assertEqual("propose_belief", resolution.receipt.operation)
        self.assertEqual("committed", self.candidate_status(candidate))
        self.assertIsNone(resolution.control)
        self.assertEqual(1, self.counts()["commands"])
        self.assertEqual(1, self.counts()["beliefs"])

    def test_resolve_approve_rejected_returns_rejected_without_writing(self) -> None:
        candidate, _context, _event = self.make_candidate()
        rejection = self.reject(candidate)
        self.assertEqual("rejected", rejection.status)
        baseline = self.counts()

        resolution = self.approve(candidate, decision_ref="decision:approve:late")

        self.assertEqual("rejected", resolution.status)
        self.assertIsNone(resolution.receipt)
        self.assertIsNone(resolution.control)
        self.assertEqual("rejected", self.candidate_status(candidate))
        self.assert_no_writes(baseline)

    def test_resolve_approve_committed_replays(self) -> None:
        candidate, _context, _event = self.make_candidate()
        first = self.approve(candidate)
        self.assertEqual("committed", first.status)
        baseline = self.counts()

        second = self.approve(candidate, decision_ref="decision:approve:2")

        self.assertEqual("already_committed", second.status)
        self.assertEqual(first.receipt, second.receipt)
        self.assertTrue(second.receipt is not None)
        self.assertEqual("committed", self.candidate_status(candidate))
        self.assert_no_writes(baseline)

    def test_resolve_reject_pending_blocks_work(self) -> None:
        candidate, _context, _event = self.make_candidate()
        resolution = self.reject(candidate)

        self.assertEqual("rejected", resolution.status)
        self.assertIsNone(resolution.receipt)
        control = resolution.control
        self.assertIsNotNone(control)
        self.assertTrue(control.blocked_after_rejection)
        self.assertEqual(1, control.generation)
        self.assertEqual(3, control.control_version)
        self.assertIsNone(control.rerun_authorization_ref)
        self.assertEqual("rejected", self.candidate_status(candidate))
        rejected = self.store.load_artifact(
            namespace=NS,
            artifact_id=candidate.artifact_id,
            artifact_version=1,
        )
        self.assertEqual("rejected", rejected.status)
        self.assertEqual("decision:reject:1", rejected.decision_ref)

    def test_resolve_reject_is_idempotent_for_same_decision_ref(self) -> None:
        candidate, _context, _event = self.make_candidate()
        first = self.reject(candidate)
        baseline = self.counts()

        second = self.reject(candidate)

        self.assertEqual("rejected", second.status)
        self.assertEqual(first.control.generation, second.control.generation)
        self.assertEqual(
            first.control.control_version, second.control.control_version
        )
        self.assertEqual("rejected", self.candidate_status(candidate))
        with self.assertRaises(CandidateTransitionDenied):
            self.reject(candidate, decision_ref="decision:reject:different")
        self.assert_no_writes(baseline)

    def test_resolve_reject_committed_returns_already_committed(self) -> None:
        candidate, _context, _event = self.make_candidate()
        approval = self.approve(candidate)
        self.assertEqual("committed", approval.status)
        control_before = self.store.work_control(
            namespace=NS, work_id=WORK_ID
        )
        self.assertIsNotNone(control_before)

        resolution = self.reject(candidate, decision_ref="decision:reject:late")

        self.assertEqual("already_committed", resolution.status)
        self.assertEqual(approval.receipt, resolution.receipt)
        self.assertIsNone(resolution.control)
        control_after = self.store.work_control(
            namespace=NS, work_id=WORK_ID
        )
        self.assertEqual(control_before, control_after)
        self.assertEqual(0, control_after.generation)
        self.assertFalse(control_after.blocked_after_rejection)
        self.assertEqual("committed", self.candidate_status(candidate))

    def test_resolve_reject_first_then_approve_is_refused(self) -> None:
        candidate, _context, _event = self.make_candidate()
        rejection = self.reject(candidate)
        self.assertEqual("rejected", rejection.status)
        baseline = self.counts()

        resolution = self.approve(candidate)

        self.assertEqual("rejected", resolution.status)
        self.assertIsNone(resolution.receipt)
        self.assertEqual("rejected", self.candidate_status(candidate))
        self.assert_no_writes(baseline)

    def test_resolve_unknown_decision_is_rejected(self) -> None:
        candidate, _context, _event = self.make_candidate()
        baseline = self.counts()
        with self.assertRaises(InvalidInput):
            self.decide(candidate, "maybe")
        with self.assertRaises(InvalidInput):
            self.decide(
                candidate,
                DECISION_REJECT,
                authority=CommitAuthority(approval=self.approval_for(candidate)),
            )
        self.assertEqual("pending", self.candidate_status(candidate))
        self.assert_no_writes(baseline)

    # ---------------------------------------------------------- review gate

    def test_review_start_allows_unmanaged_work(self) -> None:
        _event, work = self.real_work()
        decision = self.service.review_start(
            namespace=NS, work_id=work.work_id
        )
        self.assertTrue(decision.allowed)
        self.assertEqual("not_blocked", decision.reason)
        self.assertEqual(0, decision.generation)
        self.assertIsNone(
            self.store.work_control(namespace=NS, work_id=work.work_id)
        )

    def test_review_start_denied_after_rejection(self) -> None:
        _event, work, _candidate = self.rejected_work()
        decision = self.service.review_start(
            namespace=NS, work_id=work.work_id
        )
        self.assertFalse(decision.allowed)
        self.assertEqual("blocked_after_rejection", decision.reason)
        self.assertEqual(1, decision.generation)

    def test_review_start_allowed_with_registered_rerun(self) -> None:
        _event, work, _candidate = self.rejected_work()
        self.register_rerun(work)

        decision = self.service.review_start(
            namespace=NS, work_id=work.work_id
        )

        self.assertTrue(decision.allowed)
        self.assertEqual("rerun_grant", decision.reason)
        self.assertEqual(1, decision.generation)

    def test_review_start_denied_after_restart(self) -> None:
        _event, work, _candidate = self.rejected_work()
        second = self.open_second_store()
        service = CollaborationService(second)

        decision = service.review_start(namespace=NS, work_id=work.work_id)

        self.assertFalse(decision.allowed)
        self.assertEqual("blocked_after_rejection", decision.reason)
        self.assertEqual(1, decision.generation)

    def test_register_review_candidate_denied_for_inflight_result(self) -> None:
        _event, work, _candidate = self.rejected_work()
        baseline = self.counts()

        with self.assertRaises(CandidateTransitionDenied):
            self.service.register_review_candidate(
                namespace=NS,
                work_id=work.work_id,
                work_generation=1,
            )

        self.assert_no_writes(baseline)
        self.assertEqual(
            1,
            int(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM collab_artifacts "
                    "WHERE namespace = ? AND kind = 'candidate'",
                    (NS,),
                ).fetchone()[0]
            ),
        )

    def test_register_review_candidate_denies_generation_mismatch_with_rerun(
        self,
    ) -> None:
        _event, work, _candidate = self.rejected_work()
        self.register_rerun(work)
        baseline = self.counts()

        with self.assertRaises(CandidateTransitionDenied):
            self.service.register_review_candidate(
                namespace=NS,
                work_id=work.work_id,
                work_generation=0,
            )

        self.assert_no_writes(baseline)

    def test_register_review_candidate_accepts_authorized_new_generation(
        self,
    ) -> None:
        event, work, _candidate = self.rejected_work()
        self.register_rerun(work)
        context = self.build_context(event.event_id)
        fields = self.candidate_fields(
            event=event,
            context=context,
            work_id=work.work_id,
            work_generation=1,
            request_id=REQ_B,
            artifact_id=CAND_B,
            rerun_authorization_ref=GRANT_RERUN,
        )

        candidate = self.service.register_review_candidate(
            namespace=NS,
            work_id=work.work_id,
            work_generation=1,
            **fields,
        )

        self.assertEqual("pending", candidate.status)
        self.assertEqual(1, candidate.work_generation)
        control = self.store.work_control(
            namespace=NS, work_id=work.work_id
        )
        self.assertEqual(1, control.generation)
        self.assertTrue(control.blocked_after_rejection)
        self.assertEqual(GRANT_RERUN, control.rerun_authorization_ref)
        self.assertEqual(
            2,
            int(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM collab_artifacts "
                    "WHERE namespace = ? AND kind = 'candidate'",
                    (NS,),
                ).fetchone()[0]
            ),
        )

    # -------------------------------------------------------- old run_step

    def test_run_step_blocked_before_model_call(self) -> None:
        event, work = self.real_work()
        self.store.register_work_control(
            namespace=NS,
            work_id=work.work_id,
            generation=1,
            blocked_after_rejection=True,
            control_version=1,
            block_reason="rejected",
        )
        baseline = self.counts()
        pending_before = len(
            self.store.unfinished_work_for_event(NS, event.event_id)
        )

        with mock.patch.object(self.runtime._model, "propose") as propose:
            result = self.runtime.run_step(
                namespace=NS, trigger_event_id=event.event_id
            )

        self.assertEqual(OUTCOME_BLOCKED, result.outcome)
        self.assertFalse(result.ok)
        self.assertEqual(0, result.model_calls)
        propose.assert_not_called()
        self.assert_no_writes(baseline)
        self.assertEqual(
            pending_before,
            len(self.store.unfinished_work_for_event(NS, event.event_id)),
        )

    def test_run_step_keeps_legacy_behaviour_for_unmanaged_work(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")

        with mock.patch.object(
            self.runtime._model,
            "propose",
            wraps=self.runtime._model.propose,
        ) as propose:
            result = self.runtime.run_step(
                namespace=NS, trigger_event_id=event.event_id
            )

        self.assertIn(
            result.outcome, ("committed", "checked_no_belief")
        )
        self.assertTrue(result.ok)
        self.assertEqual(1, propose.call_count)
        self.assertEqual(0, self.count_rows("collab_work_control"))

    def test_duplicate_ingest_still_hits_the_block(self) -> None:
        source_id = f"source-{uuid.uuid4()}"
        first = self.ingest(
            event_type="CapabilityObserved:file-read",
            source_event_id=source_id,
        )
        work = self.store.unfinished_work_for_event(NS, first.event_id)[0]
        self.store.register_work_control(
            namespace=NS,
            work_id=work.work_id,
            generation=1,
            blocked_after_rejection=True,
            control_version=1,
            block_reason="rejected",
        )
        pending_before = self.counts()["pending_work"]
        snapshots_before = self.counts()["contexts"]

        duplicate = self.ingest(
            event_type="CapabilityObserved:file-read",
            source_event_id=source_id,
        )
        self.assertEqual("duplicate", duplicate.outcome)
        self.assertEqual(first.event_id, duplicate.event_id)
        self.assertEqual(pending_before, self.counts()["pending_work"])

        with mock.patch.object(self.runtime._model, "propose") as propose:
            result = self.runtime.run_step(
                namespace=NS, trigger_event_id=first.event_id
            )

        self.assertEqual(OUTCOME_BLOCKED, result.outcome)
        self.assertEqual(0, result.model_calls)
        propose.assert_not_called()
        self.assertEqual(snapshots_before, self.counts()["contexts"])

    def test_run_step_refuses_managed_work_even_with_rerun_grant(self) -> None:
        event, work, candidate = self.rejected_work()
        self.register_rerun(work)
        decision = self.service.review_start(
            namespace=NS, work_id=work.work_id
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(1, decision.generation)
        baseline = self.counts()

        with mock.patch.object(self.runtime._model, "propose") as propose:
            result = self.runtime.run_step(
                namespace=NS, trigger_event_id=event.event_id
            )

        self.assertEqual(OUTCOME_BLOCKED, result.outcome)
        self.assertEqual(0, result.model_calls)
        propose.assert_not_called()
        self.assertEqual(0, self.counts()["beliefs"])
        self.assertEqual(0, self.counts()["commands"])
        self.assertEqual("rejected", self.candidate_status(candidate))
        self.assert_no_writes(baseline)

    def test_run_step_refuses_unblocked_managed_work(self) -> None:
        event, work = self.real_work()
        self.store.register_work_control(
            namespace=NS,
            work_id=work.work_id,
            generation=0,
            blocked_after_rejection=False,
            control_version=1,
        )
        baseline = self.counts()

        with mock.patch.object(self.runtime._model, "propose") as propose:
            result = self.runtime.run_step(
                namespace=NS, trigger_event_id=event.event_id
            )

        self.assertEqual(OUTCOME_MANAGED, result.outcome)
        self.assertFalse(result.ok)
        self.assertEqual(0, result.model_calls)
        propose.assert_not_called()
        self.assertEqual(0, self.counts()["beliefs"])
        self.assertEqual(0, self.counts()["commands"])
        self.assert_no_writes(baseline)

    # -------------------------------------------- take-over during generation

    def test_run_step_blocks_when_control_appears_during_generation(self) -> None:
        event, work = self.real_work()
        model = _TakeoverModel(self.store, work_id=work.work_id, blocked=False)
        runtime = CognitionRuntime(
            self.store,
            model=model,
            belief_service=self.beliefs,
            context_service=self.contexts,
            self_view_service=self.self_view,
            clock=self.clock,
        )

        result = runtime.run_step(
            namespace=NS, trigger_event_id=event.event_id
        )

        self.assertEqual(OUTCOME_MANAGED, result.outcome)
        self.assertEqual(1, model.calls)
        self.assertEqual(0, self.counts()["beliefs"])
        self.assertEqual(0, self.counts()["commands"])
        self.assertEqual(0, self.counts()["work_progress"])
        self.assertIsNone(self.work_progress_for(work.work_id))
        self.assertTrue(
            self.store.unfinished_work_for_event(NS, event.event_id)
        )

    def test_run_step_blocks_when_blocked_control_appears_during_generation(
        self,
    ) -> None:
        event, work = self.real_work()
        model = _TakeoverModel(self.store, work_id=work.work_id, blocked=True)
        runtime = CognitionRuntime(
            self.store,
            model=model,
            belief_service=self.beliefs,
            context_service=self.contexts,
            self_view_service=self.self_view,
            clock=self.clock,
        )

        result = runtime.run_step(
            namespace=NS, trigger_event_id=event.event_id
        )

        self.assertEqual(OUTCOME_BLOCKED, result.outcome)
        self.assertEqual(1, model.calls)
        self.assertEqual(0, self.counts()["beliefs"])
        self.assertEqual(0, self.counts()["commands"])
        self.assertEqual(0, self.counts()["work_progress"])
        self.assertTrue(
            self.store.unfinished_work_for_event(NS, event.event_id)
        )

    def test_wait_commit_is_blocked_when_control_appears(self) -> None:
        event, work = self.real_work()
        model = _WaitTakeoverModel(
            self.store, work_id=work.work_id, blocked=False
        )
        runtime = CognitionRuntime(
            self.store,
            model=model,
            belief_service=self.beliefs,
            context_service=self.contexts,
            self_view_service=self.self_view,
            clock=self.clock,
        )

        result = runtime.run_step(
            namespace=NS, trigger_event_id=event.event_id
        )

        self.assertEqual(OUTCOME_MANAGED, result.outcome)
        self.assertEqual(1, model.calls)
        self.assertEqual(0, self.counts()["work_progress"])
        self.assertEqual(0, self.counts()["commands"])
        self.assertIsNone(self.work_progress_for(work.work_id))
        self.assertTrue(
            self.store.unfinished_work_for_event(NS, event.event_id)
        )

    # ------------------------------------------------------ host sweep rule

    def test_blocked_work_does_not_hide_other_legacy_work(self) -> None:
        # The fixture itself leaves one unfinished work item from the synthetic
        # profile review event; keep it in the baseline instead of assuming an
        # empty pending table.
        baseline_work_ids = {
            work.work_id for work in self.store.unfinished_work(NS)
        }
        event_a, work_a = self.real_work()
        self.store.register_work_control(
            namespace=NS,
            work_id=work_a.work_id,
            generation=1,
            blocked_after_rejection=True,
            control_version=1,
            block_reason="rejected",
        )
        event_b = self.ingest(event_type="CapabilityObserved:file-read")
        works_b = self.store.unfinished_work_for_event(NS, event_b.event_id)
        self.assertTrue(works_b)
        work_b = works_b[0]

        works = self.store.unfinished_work(NS)
        present = {work.work_id for work in works}
        self.assertTrue({work_a.work_id, work_b.work_id} <= present)
        legacy = [
            work
            for work in works
            if self.store.work_control(
                namespace=NS, work_id=work.work_id
            )
            is None
        ]
        # Only the unmanaged work is in the host's legacy scan set: the blocked
        # item stays pending but is filtered out, never discarded.
        self.assertEqual(
            baseline_work_ids | {work_b.work_id},
            {work.work_id for work in legacy},
        )

        with mock.patch.object(
            self.runtime._model,
            "propose",
            wraps=self.runtime._model.propose,
        ) as propose:
            result_b = self.runtime.run_step(
                namespace=NS, trigger_event_id=event_b.event_id
            )
            result_a = self.runtime.run_step(
                namespace=NS, trigger_event_id=event_a.event_id
            )

        self.assertIn(result_b.outcome, ("committed", "checked_no_belief"))
        self.assertEqual(OUTCOME_BLOCKED, result_a.outcome)
        self.assertEqual(1, propose.call_count)

        remaining = {
            work.work_id for work in self.store.unfinished_work(NS)
        }
        self.assertEqual(baseline_work_ids | {work_a.work_id}, remaining)
        progress = self.store.connection.execute(
            "SELECT work_id FROM work_progress"
        ).fetchall()
        self.assertEqual([work_b.work_id], [row[0] for row in progress])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
