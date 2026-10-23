"""C12c-PRE: safe review candidate reads, exact refs and redaction.

Every test uses a fresh migrated database, the real PCS2-008 Context service
and the real C06/C07 collaboration services.  Candidate bodies and proposals
are synthetic fixtures; no model, network or private instance is used.

The tests cover the four PRE boundaries:

* candidate enumeration reads metadata only and hides invisible candidates;
* the detail read binds the exact ``(candidate_id, artifact_version)``;
* candidate, current-Belief and source redaction stay separate;
* ``can_approve``/``can_reject`` are hints, never a replacement for C07
  ``resolve``.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from dataclasses import fields
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

from cognition_support import NS, NS_B, CognitionTestCase  # noqa: E402

from quintessence_pcs.collaboration import (  # noqa: E402
    FACADE_PERMISSIONS,
    HostAccess,
    ReviewCandidateSummary,
    ReviewFacade,
)
from quintessence_pcs.collaboration.service import CollaborationService  # noqa: E402
from quintessence_pcs.foundation import identifiers  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    AuthorizationRequired,
    RecordNotFound,
)

HOST = "host:synthetic-c12c"
OPERATOR = "operator:synthetic-c12c"
ACCESS_REF = "authorization:access:c12c"
READ_PERMISSIONS = frozenset({"work.scan", "experience.read"})
ALL_PRIVACY = frozenset(identifiers.PRIVACY_CLASSES)
#: Every review candidate carries the private active-profile source; a caller
#: therefore needs ``private`` to see any normal review candidate.
REVIEW_PRIVACY = frozenset({"private", "public-example"})
NARROW_PRIVACY = frozenset({"public-example"})
PURPOSES = frozenset({"review"})

CAND_A = "a1111111-1111-4111-8111-111111111111"
CAND_B = "a2222222-2222-4222-8222-222222222222"


class ScriptedModel:
    """Queue-style model double that never touches the network."""

    adapter_kind = "fake-model"

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def propose(self, *, rendered_input, references, feedback=None):
        self.calls += 1
        value = self._outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class ReviewCandidateReadTests(CognitionTestCase):
    """C12c-PRE candidate enumeration, exact detail and redaction."""

    def setUp(self) -> None:
        super().setUp()
        self.service = CollaborationService(
            self.store, belief_service=self.beliefs
        )
        self.facade = ReviewFacade(
            self.store,
            context_service=self.contexts,
            model=ScriptedModel([]),
        )

    # ------------------------------------------------------------- fixtures

    def access(
        self,
        *,
        permissions=READ_PERMISSIONS,
        privacy_classes=REVIEW_PRIVACY,
        purposes=PURPOSES,
        host_instance: str = HOST,
    ) -> HostAccess:
        return HostAccess(
            namespace=NS,
            host_instance=host_instance,
            operator=OPERATOR,
            permissions=frozenset(permissions),
            privacy_classes=frozenset(privacy_classes),
            purposes=frozenset(purposes),
            access_ref=ACCESS_REF,
        )

    def full_access(self) -> HostAccess:
        return self.access(permissions=FACADE_PERMISSIONS)

    def ingest_event(
        self,
        *,
        privacy_class: str = "private",
        namespace: str = NS,
        event_type: str = "CapabilityObserved:file-read",
    ):
        return self.store.ingest_event(
            namespace=namespace,
            source_kind="host_observation",
            source_instance="c12c-tests",
            source_event_id=f"source-{uuid.uuid4()}",
            event_type=event_type,
            payload={"text": "synthetic c12c event"},
            privacy_class=privacy_class,
        )

    def pending_event(self, *, privacy_class: str = "private"):
        event = self.ingest_event(privacy_class=privacy_class)
        work = self.work_for(event.event_id)
        self.assertIsNotNone(work)
        return event, work

    @staticmethod
    def raw(value: dict) -> str:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @staticmethod
    def context_source_refs(context) -> list[dict]:
        refs: list[dict] = []
        for source in context.sources:
            if source.ref_kind == "event":
                refs.append(
                    {
                        "ref_type": "event",
                        "namespace": NS,
                        "event_id": source.ref_id,
                    }
                )
            elif source.ref_kind == "record":
                refs.append(
                    {
                        "ref_type": "record",
                        "namespace": NS,
                        "record_id": source.ref_id,
                        "record_version": source.ref_version,
                    }
                )
            else:  # pragma: no cover - review contexts are event/record only
                raise AssertionError(
                    f"unexpected context source {source.ref_kind!r}"
                )
        return refs

    def proposal_propose(self, event, *, claim: str = "capability:file-read") -> dict:
        body = self.belief_body(
            claim=claim,
            evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
        )
        return {
            "operation": "propose_belief",
            "body": body,
            "scope": self.default_scope("capability"),
        }

    def proposal_revise(
        self,
        target_ref: dict,
        evidence_event,
        *,
        claim: str = "capability:file-read",
        strength: str = "moderate",
        valid_until: str | None = None,
    ) -> dict:
        body = self.belief_body(
            claim=claim,
            evidence_links=[
                self.evidence_link(self.event_ref(evidence_event.event_id))
            ],
            strength=strength,
            valid_until=valid_until,
        )
        return {
            "operation": "revise_belief",
            "target": dict(target_ref),
            "body": body,
            "scope": self.default_scope("capability"),
        }

    def register_candidate(
        self,
        context,
        proposal: dict,
        *,
        work,
        artifact_id: str | None = None,
        extra_sources=(),
        input_refs=(),
        host_instance: str = HOST,
        generation: int = 0,
    ):
        interaction_id = self.store.register_interaction(
            namespace=NS,
            mode="review",
            activation_kind="manual_review",
            host_instance=host_instance,
            access_ref=ACCESS_REF,
            policy_ref="policy:collab.v1",
            context_id=context.context_id,
            trigger_ref=self.event_ref(context.trigger_ref.event_id),
            work_id=work.work_id,
            work_generation=generation,
            input_refs=input_refs,
        )
        return self.service.register_review_candidate(
            namespace=NS,
            work_id=work.work_id,
            work_generation=generation,
            interaction_id=interaction_id,
            context_id=context.context_id,
            body=self.raw(proposal),
            retention="standard",
            proposal=proposal,
            proposal_version="v1",
            request_id=str(uuid.uuid4()),
            artifact_id=artifact_id,
            sources=[*self.context_source_refs(context), *extra_sources],
        )

    def make_facade(self, model) -> ReviewFacade:
        return ReviewFacade(
            self.store, context_service=self.contexts, model=model
        )

    def approve(self, candidate_id: str, decision_id: str = "decision:approve:c12c"):
        return self.facade.resolve(
            self.full_access(),
            candidate_id=candidate_id,
            decision="approve",
            decision_id=decision_id,
        )

    def reject(self, candidate_id: str, decision_id: str = "decision:reject:c12c"):
        return self.facade.resolve(
            self.full_access(),
            candidate_id=candidate_id,
            decision="reject",
            decision_id=decision_id,
        )

    def make_belief(self, *, claim: str = "capability:file-read"):
        event = self.ingest_event()
        version = self.propose(
            body=self.belief_body(
                claim=claim,
                evidence_links=[
                    self.evidence_link(self.event_ref(event.event_id))
                ],
            ),
            scope=self.default_scope("capability"),
        )
        return version

    # -------------------------------------------------------- 1. enumeration

    def test_candidates_empty_for_work_without_candidate(self) -> None:
        event, work = self.pending_event()
        del event
        self.assertEqual(
            (),
            self.facade.candidates(self.access(), work_id=work.work_id),
        )

    def test_candidates_are_stable_metadata_only_rows(self) -> None:
        event, work = self.pending_event()
        context = self.build_context(event.event_id)
        first = self.register_candidate(
            context,
            self.proposal_propose(event),
            work=work,
            artifact_id=CAND_A,
        )
        second = self.register_candidate(
            context,
            self.proposal_propose(event),
            work=work,
            artifact_id=CAND_B,
        )

        with mock.patch.object(
            self.store,
            "_load_collab_payload",
            side_effect=AssertionError("enumeration must not load a payload"),
        ):
            summaries = self.facade.candidates(
                self.access(), work_id=work.work_id
            )

        names = {field.name for field in fields(ReviewCandidateSummary)}
        self.assertNotIn("body", names)
        self.assertNotIn("proposal", names)
        self.assertEqual(2, len(summaries))
        self.assertEqual(
            [CAND_A, CAND_B],
            [summary.candidate.candidate_id for summary in summaries],
        )
        by_id = {summary.candidate.candidate_id: summary for summary in summaries}
        for candidate, summary in zip((first, second), summaries):
            self.assertEqual(candidate.artifact_id, summary.candidate.candidate_id)
            self.assertEqual(1, summary.candidate.artifact_version)
            self.assertEqual(work.work_id, summary.candidate.work_id)
            self.assertEqual(0, summary.candidate.work_generation)
            self.assertEqual("pending", summary.status)
            self.assertIsNone(summary.decided_at)
            self.assertIsNone(summary.decision_ref)
            self.assertIsNone(summary.attempt_ref)
            self.assertTrue(summary.created_at.endswith("Z"))
        self.assertIn(CAND_A, by_id)

    def test_candidates_filter_exact_generation_and_keep_terminal_status(self) -> None:
        event, work = self.pending_event()
        context = self.build_context(event.event_id)
        first = self.register_candidate(
            context,
            self.proposal_propose(event),
            work=work,
            artifact_id=CAND_A,
        )
        second = self.register_candidate(
            context,
            self.proposal_propose(event),
            work=work,
            artifact_id=CAND_B,
        )

        resolved = self.reject(first.artifact_id)
        self.assertEqual("rejected", resolved.state)

        generation_zero = self.facade.candidates(
            self.access(), work_id=work.work_id, generation=0
        )
        self.assertEqual(2, len(generation_zero))
        statuses = {
            summary.candidate.candidate_id: summary.status
            for summary in generation_zero
        }
        self.assertEqual("rejected", statuses[CAND_A])
        self.assertEqual("stale", statuses[CAND_B])
        self.assertEqual(
            (),
            self.facade.candidates(
                self.access(), work_id=work.work_id, generation=1
            ),
        )

        detail = self.facade.candidate_detail(
            self.access(),
            candidate_id=second.artifact_id,
            artifact_version=1,
        )
        self.assertFalse(detail.can_approve)
        self.assertFalse(detail.can_reject)
        self.assertEqual("terminal", detail.eligibility_reason)

    def test_candidates_hide_invisible_and_deleted_candidates(self) -> None:
        event, work = self.pending_event()
        context = self.build_context(event.event_id)
        candidate = self.register_candidate(
            context,
            self.proposal_propose(event),
            work=work,
            artifact_id=CAND_A,
        )

        # The private profile source makes this candidate invisible to an
        # access that may only read public-example content.
        hidden_access = self.access(privacy_classes=NARROW_PRIVACY)
        self.assertEqual(
            (),
            self.facade.candidates(hidden_access, work_id=work.work_id),
        )
        hidden_detail = self.facade.candidate_detail(
            hidden_access,
            candidate_id=candidate.artifact_id,
            artifact_version=1,
        )
        self.assertTrue(hidden_detail.redacted)
        self.assertIsNone(hidden_detail.proposal)

        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_artifact",
            ref_id=candidate.artifact_id,
            ref_version=1,
            authorization_ref="authorization:delete:c12c",
        )
        self.assertEqual(
            (),
            self.facade.candidates(self.access(), work_id=work.work_id),
        )

    def test_host_b_can_read_but_cannot_approve_or_recover(self) -> None:
        """C12c audit: the read boundary and the approval boundary differ.

        C07's formal read boundary is namespace + privacy classes + purpose;
        its ``resolve``/``result`` boundary additionally requires the
        interaction's ``host_instance`` and the matching host permission.  The
        C12c desktop bridge adds no host check of its own, so this test pins
        both halves: host B may read the exact candidate, and C07 refuses every
        host-B write/recovery.
        """

        event, work = self.pending_event()
        context = self.build_context(event.event_id)
        candidate = self.register_candidate(
            context, self.proposal_propose(event), work=work
        )

        host_b = self.access(host_instance="host:desktop-lab-b")
        self.assertEqual(
            1, len(self.facade.candidates(host_b, work_id=work.work_id))
        )
        host_b_detail = self.facade.candidate_detail(
            host_b,
            candidate_id=candidate.artifact_id,
            artifact_version=1,
        )
        self.assertFalse(host_b_detail.redacted)
        self.assertIsNotNone(host_b_detail.proposal)
        self.assertTrue(host_b_detail.can_approve)

        with self.assertRaises(AuthorizationRequired):
            self.facade.resolve(
                host_b,
                candidate_id=candidate.artifact_id,
                decision="approve",
                decision_id="decision:audit:host-b:approve",
            )
        with self.assertRaises(AuthorizationRequired):
            self.facade.resolve(
                host_b,
                candidate_id=candidate.artifact_id,
                decision="reject",
                decision_id="decision:audit:host-b:reject",
            )
        with self.assertRaises(AuthorizationRequired):
            self.facade.result(
                host_b, interaction_id=candidate.interaction_id
            )

        # ``can_approve`` is a content hint, never a permission: a host that
        # may only read still gets AuthorizationRequired from resolve.
        read_only = self.access(
            permissions=frozenset({"work.scan", "experience.read"})
        )
        read_only_detail = self.facade.candidate_detail(
            read_only,
            candidate_id=candidate.artifact_id,
            artifact_version=1,
        )
        self.assertTrue(read_only_detail.can_approve)
        with self.assertRaises(AuthorizationRequired):
            self.facade.resolve(
                read_only,
                candidate_id=candidate.artifact_id,
                decision="approve",
                decision_id="decision:audit:read-only",
            )

        # The same candidate remains valid for its own host and permission.
        approved = self.approve(candidate.artifact_id)
        self.assertEqual("committed", approved.state)
        self.assertIsNotNone(approved.receipt)

    def test_candidates_are_scoped_to_the_access_namespace(self) -> None:
        other_event = self.ingest_event(namespace=NS_B)
        other_work = self.work_for(other_event.event_id, namespace=NS_B)
        self.assertIsNotNone(other_work)
        self.assertEqual(
            (),
            self.facade.candidates(
                self.access(), work_id=other_work.work_id
            ),
        )

    # -------------------------------------------------------- 2. exact detail

    def test_detail_binds_exact_candidate_version(self) -> None:
        event, work = self.pending_event()
        context = self.build_context(event.event_id)
        candidate = self.register_candidate(
            context,
            self.proposal_propose(event),
            work=work,
            artifact_id=CAND_A,
        )
        row = self.store.candidate_summary_for_ref(
            namespace=NS,
            artifact_id=CAND_A,
            artifact_version=1,
        )
        self.assertIsNotNone(row)
        self.assertEqual(1, row.artifact_version)
        self.assertEqual("passed", row.structural_validation)

        detail = self.facade.candidate_detail(
            self.access(),
            candidate_id=candidate.artifact_id,
            artifact_version=1,
        )
        self.assertEqual(1, detail.candidate.candidate.artifact_version)
        self.assertEqual("passed", detail.structural_validation)

        with self.assertRaises(RecordNotFound):
            self.facade.candidate_detail(
                self.access(),
                candidate_id=candidate.artifact_id,
                artifact_version=2,
            )

    def test_detail_projects_proposal_and_propose_diff(self) -> None:
        event, work = self.pending_event()
        context = self.build_context(event.event_id)
        proposal = self.proposal_propose(event)
        candidate = self.register_candidate(
            context, proposal, work=work, artifact_id=CAND_A
        )

        detail = self.facade.candidate_detail(
            self.access(),
            candidate_id=candidate.artifact_id,
            artifact_version=1,
        )
        self.assertFalse(detail.redacted)
        self.assertIsNotNone(detail.proposal)
        self.assertEqual("propose_belief", detail.proposal.operation)
        self.assertIsNone(detail.proposal.target_ref)
        self.assertEqual(proposal["body"]["claim"], detail.proposal.claim)
        self.assertEqual(proposal["body"]["claim_type"], detail.proposal.claim_type)
        self.assertEqual(proposal["scope"], detail.proposal.scope)
        self.assertEqual(1, len(detail.proposal.evidence_links))
        self.assertEqual(proposal["body"]["strength"], detail.proposal.strength)
        self.assertEqual(
            proposal["body"]["strength_reason"], detail.proposal.strength_reason
        )
        self.assertIsNone(detail.proposal.valid_until)
        self.assertIsNone(detail.current)
        self.assertEqual("not_applicable", detail.current_state)
        self.assertEqual(6, len(detail.diff))
        self.assertTrue(all(item.changed for item in detail.diff))
        self.assertTrue(all(item.before is None for item in detail.diff))
        self.assertTrue(detail.sources)
        self.assertTrue(detail.can_approve)
        self.assertTrue(detail.can_reject)
        self.assertIsNone(detail.eligibility_reason)
        self.assertIsNone(detail.detail)

    def test_detail_revise_has_current_and_field_diff(self) -> None:
        # The Context is built before the target Belief exists, so the target
        # is not one of the candidate's source links.
        context = self.build_context()
        work = self.work_for(context.trigger_ref.event_id)
        self.assertIsNotNone(work)
        version = self.make_belief()
        evidence = self.ingest_event(privacy_class="public-example")
        proposal = self.proposal_revise(
            self.record_ref(version),
            evidence,
            strength="moderate",
            valid_until="2026-11-01T00:00:00Z",
        )
        candidate = self.register_candidate(
            context, proposal, work=work, artifact_id=CAND_A
        )

        detail = self.facade.candidate_detail(
            self.access(),
            candidate_id=candidate.artifact_id,
            artifact_version=1,
        )
        self.assertIsNotNone(detail.current)
        self.assertEqual("available", detail.current_state)
        self.assertEqual(version.record_id, detail.current.ref.record_id)
        self.assertEqual(1, detail.current.ref.record_version)
        self.assertEqual(version.status, detail.current.status)
        self.assertEqual("scene=test.scene", detail.current.applicability)
        self.assertFalse(detail.current.status in ("retracted", "contradicted"))
        self.assertTrue(detail.can_approve)
        self.assertTrue(detail.can_reject)

        diff = {item.field: item for item in detail.diff}
        self.assertEqual(
            {
                "claim",
                "claim_type",
                "scope",
                "evidence_links",
                "strength",
                "valid_until",
            },
            set(diff),
        )
        self.assertFalse(diff["claim"].changed)
        self.assertFalse(diff["claim_type"].changed)
        self.assertFalse(diff["scope"].changed)
        self.assertTrue(diff["strength"].changed)
        self.assertTrue(diff["valid_until"].changed)
        self.assertTrue(diff["evidence_links"].changed)

    def test_detail_retract_projects_reason_and_evidence_only(self) -> None:
        context = self.build_context()
        work = self.work_for(context.trigger_ref.event_id)
        version = self.make_belief()
        evidence = self.ingest_event(privacy_class="public-example")
        proposal = {
            "operation": "retract_belief",
            "target": self.record_ref(version),
            "evidence_links": [
                self.evidence_link(self.event_ref(evidence.event_id))
            ],
            "reason": "synthetic retraction reason",
        }
        candidate = self.register_candidate(
            context, proposal, work=work, artifact_id=CAND_A
        )

        detail = self.facade.candidate_detail(
            self.access(),
            candidate_id=candidate.artifact_id,
            artifact_version=1,
        )
        self.assertEqual("retract_belief", detail.proposal.operation)
        self.assertEqual(version.record_id, detail.proposal.target_ref.record_id)
        self.assertIsNone(detail.proposal.claim)
        self.assertIsNone(detail.proposal.claim_type)
        self.assertIsNone(detail.proposal.scope)
        self.assertIsNone(detail.proposal.strength)
        self.assertEqual(
            "synthetic retraction reason", detail.proposal.strength_reason
        )
        self.assertIsNone(detail.proposal.valid_until)
        self.assertEqual(1, len(detail.proposal.evidence_links))
        self.assertIsNotNone(detail.current)
        self.assertEqual("available", detail.current_state)
        self.assertEqual(
            ["evidence_links"], [item.field for item in detail.diff]
        )
        self.assertTrue(detail.can_approve)
        self.assertTrue(detail.can_reject)

    def test_detail_revise_current_redacted_blocks_only_approve(self) -> None:
        context = self.build_context()
        work = self.work_for(context.trigger_ref.event_id)
        version = self.make_belief()
        evidence = self.ingest_event(privacy_class="public-example")
        proposal = self.proposal_revise(self.record_ref(version), evidence)
        candidate = self.register_candidate(
            context, proposal, work=work, artifact_id=CAND_A
        )
        self.delete_record(version.record_id)

        detail = self.facade.candidate_detail(
            self.access(),
            candidate_id=candidate.artifact_id,
            artifact_version=1,
        )
        self.assertIsNone(detail.current)
        self.assertEqual("redacted", detail.current_state)
        self.assertEqual((), detail.diff)
        self.assertFalse(detail.can_approve)
        self.assertTrue(detail.can_reject)
        self.assertEqual("current_redacted", detail.eligibility_reason)

    def test_detail_revise_current_missing_blocks_only_approve(self) -> None:
        context = self.build_context()
        work = self.work_for(context.trigger_ref.event_id)
        evidence = self.ingest_event(privacy_class="public-example")
        missing = {
            "ref_type": "record",
            "namespace": NS,
            "record_id": str(uuid.uuid4()),
            "record_version": 1,
        }
        proposal = self.proposal_revise(missing, evidence)
        candidate = self.register_candidate(
            context, proposal, work=work, artifact_id=CAND_A
        )

        detail = self.facade.candidate_detail(
            self.access(),
            candidate_id=candidate.artifact_id,
            artifact_version=1,
        )
        self.assertIsNone(detail.current)
        self.assertEqual("missing", detail.current_state)
        self.assertEqual((), detail.diff)
        self.assertFalse(detail.can_approve)
        self.assertTrue(detail.can_reject)
        self.assertEqual("current_missing", detail.eligibility_reason)

    def test_detail_target_moved_blocks_only_approve(self) -> None:
        context = self.build_context()
        work = self.work_for(context.trigger_ref.event_id)
        version = self.make_belief()
        evidence = self.ingest_event(privacy_class="public-example")
        self.revise(
            self.record_ref(version),
            body=self.belief_body(
                claim="capability:file-read",
                evidence_links=[
                    self.evidence_link(self.event_ref(evidence.event_id))
                ],
                strength="moderate",
            ),
            scope=self.default_scope("capability"),
        )
        proposal = self.proposal_revise(self.record_ref(version), evidence)
        candidate = self.register_candidate(
            context, proposal, work=work, artifact_id=CAND_A
        )

        detail = self.facade.candidate_detail(
            self.access(),
            candidate_id=candidate.artifact_id,
            artifact_version=1,
        )
        self.assertIsNotNone(detail.current)
        self.assertEqual("available", detail.current_state)
        self.assertEqual(1, detail.current.ref.record_version)
        self.assertFalse(detail.can_approve)
        self.assertTrue(detail.can_reject)
        self.assertEqual("target_moved", detail.eligibility_reason)

    def test_detail_candidate_redacted_has_no_projection_or_actions(self) -> None:
        event, work = self.pending_event()
        context = self.build_context(event.event_id)
        candidate = self.register_candidate(
            context,
            self.proposal_propose(event),
            work=work,
            artifact_id=CAND_A,
        )
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_artifact",
            ref_id=candidate.artifact_id,
            ref_version=1,
            authorization_ref="authorization:delete:c12c",
        )

        detail = self.facade.candidate_detail(
            self.access(),
            candidate_id=candidate.artifact_id,
            artifact_version=1,
        )
        self.assertTrue(detail.redacted)
        self.assertIsNone(detail.proposal)
        self.assertIsNone(detail.structural_validation)
        self.assertIsNone(detail.current)
        self.assertEqual((), detail.diff)
        self.assertEqual((), detail.sources)
        self.assertFalse(detail.can_approve)
        self.assertFalse(detail.can_reject)
        self.assertEqual("candidate_redacted", detail.eligibility_reason)
        self.assertEqual("candidate_redacted", detail.detail)

    # ------------------------------------------------------ 3. source redaction

    def test_detail_source_availability_is_three_state(self) -> None:
        context = self.build_context()
        work = self.work_for(context.trigger_ref.event_id)
        trigger = context.trigger_ref.event_id
        private_event = self.ingest_event(privacy_class="restricted")
        missing_event_id = str(uuid.uuid4())
        body = self.belief_body(
            evidence_links=[
                self.evidence_link(self.event_ref(trigger)),
                self.evidence_link(self.event_ref(private_event.event_id)),
                self.evidence_link(
                    {
                        "ref_type": "event",
                        "namespace": NS,
                        "event_id": missing_event_id,
                    }
                ),
            ]
        )
        proposal = {
            "operation": "propose_belief",
            "body": body,
            "scope": self.default_scope("capability"),
        }
        candidate = self.register_candidate(
            context, proposal, work=work, artifact_id=CAND_A
        )

        detail = self.facade.candidate_detail(
            self.access(), candidate_id=candidate.artifact_id, artifact_version=1
        )
        self.assertIsNotNone(detail.proposal)
        self.assertEqual(3, len(detail.proposal.evidence_links))
        sources = {
            source.ref.get("event_id"): source
            for source in detail.sources
            if source.kind == "event"
        }
        self.assertEqual("available", sources[trigger].availability)
        self.assertIsNone(sources[trigger].body)
        self.assertEqual(
            "redacted", sources[private_event.event_id].availability
        )
        self.assertEqual("privacy", sources[private_event.event_id].reason)
        self.assertEqual(
            "unavailable", sources[missing_event_id].availability
        )
        self.assertEqual("not_found", sources[missing_event_id].reason)
        self.assertFalse(detail.can_approve)
        self.assertTrue(detail.can_reject)
        self.assertEqual("source_redacted", detail.eligibility_reason)

    def test_detail_input_source_body_only_when_read_experience_allows(self) -> None:
        event, work = self.pending_event(privacy_class="public-example")
        source = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="synthetic c12c input body",
            privacy_class="public-example",
            retention="standard",
        )
        context = self.build_context(event.event_id)
        input_ref = {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": source.input_id,
            "content_version": source.content_version,
        }
        proposal = self.proposal_propose(event)
        candidate = self.register_candidate(
            context,
            proposal,
            work=work,
            artifact_id=CAND_A,
            extra_sources=[input_ref],
            input_refs=[input_ref],
        )

        detail = self.facade.candidate_detail(
            self.access(),
            candidate_id=candidate.artifact_id,
            artifact_version=1,
        )
        input_sources = [
            source
            for source in detail.sources
            if source.kind == "collab_input"
        ]
        self.assertEqual(1, len(input_sources))
        self.assertEqual("available", input_sources[0].availability)
        self.assertEqual("synthetic c12c input body", input_sources[0].body)
        self.assertEqual(1, input_sources[0].ref_version)
        self.assertTrue(detail.can_approve)

    # -------------------------------------------------- 4. terminal/stale state

    def test_terminal_candidates_disable_both_actions(self) -> None:
        event, work = self.pending_event()
        context = self.build_context(event.event_id)
        candidate = self.register_candidate(
            context,
            self.proposal_propose(event),
            work=work,
            artifact_id=CAND_A,
        )
        decision_id = "decision:approve:c12c:committed"
        resolved = self.approve(candidate.artifact_id, decision_id=decision_id)
        self.assertEqual("committed", resolved.state)
        self.assertIsNotNone(resolved.receipt)
        self.assertFalse(resolved.redacted)

        summary = self.facade.candidates(
            self.access(), work_id=work.work_id
        )[0]
        self.assertEqual("committed", summary.status)
        self.assertEqual(decision_id, summary.decision_ref)
        self.assertIsNotNone(summary.decided_at)

        detail = self.facade.candidate_detail(
            self.access(),
            candidate_id=candidate.artifact_id,
            artifact_version=1,
        )
        self.assertFalse(detail.can_approve)
        self.assertFalse(detail.can_reject)
        self.assertEqual("terminal", detail.eligibility_reason)

    def test_stale_generation_disables_both_actions(self) -> None:
        event, work = self.pending_event()
        context = self.build_context(event.event_id)
        candidate = self.register_candidate(
            context,
            self.proposal_propose(event),
            work=work,
            artifact_id=CAND_A,
        )
        with self.store.transaction():
            self.store.connection.execute(
                "UPDATE collab_work_control SET generation = 1, "
                "control_version = control_version + 1 "
                "WHERE namespace = ? AND work_id = ?",
                (NS, work.work_id),
            )

        detail = self.facade.candidate_detail(
            self.access(),
            candidate_id=candidate.artifact_id,
            artifact_version=1,
        )
        self.assertFalse(detail.can_approve)
        self.assertFalse(detail.can_reject)
        self.assertEqual("stale_generation", detail.eligibility_reason)

    # --------------------------------------------------------- 5. permissions

    def test_candidate_reads_require_work_scan_and_experience_read(self) -> None:
        event, work = self.pending_event()
        context = self.build_context(event.event_id)
        candidate = self.register_candidate(
            context,
            self.proposal_propose(event),
            work=work,
            artifact_id=CAND_A,
        )
        for permissions in (
            frozenset({"experience.read"}),
            frozenset({"work.scan"}),
        ):
            access = self.access(permissions=permissions)
            with self.assertRaises(AuthorizationRequired):
                self.facade.candidates(access, work_id=work.work_id)
            with self.assertRaises(AuthorizationRequired):
                self.facade.candidate_detail(
                    access,
                    candidate_id=candidate.artifact_id,
                    artifact_version=1,
                )

    def test_candidate_reads_do_not_depend_on_review_generate(self) -> None:
        event, work = self.pending_event()
        context = self.build_context(event.event_id)
        candidate = self.register_candidate(
            context,
            self.proposal_propose(event),
            work=work,
            artifact_id=CAND_A,
        )
        read_only = self.access(permissions=READ_PERMISSIONS)
        self.assertEqual(
            1,
            len(self.facade.candidates(read_only, work_id=work.work_id)),
        )
        detail = self.facade.candidate_detail(
            read_only,
            candidate_id=candidate.artifact_id,
            artifact_version=1,
        )
        self.assertIsNotNone(detail.proposal)
        self.assertTrue(detail.can_approve)
        self.assertTrue(detail.can_reject)

    def test_approve_and_reject_permissions_stay_with_resolve(self) -> None:
        event, work = self.pending_event()
        context = self.build_context(event.event_id)
        candidate = self.register_candidate(
            context,
            self.proposal_propose(event),
            work=work,
            artifact_id=CAND_A,
        )
        no_approve = self.access(
            permissions=FACADE_PERMISSIONS - {"review.approve"}
        )
        with self.assertRaises(AuthorizationRequired):
            self.facade.resolve(
                no_approve,
                candidate_id=candidate.artifact_id,
                decision="approve",
                decision_id="decision:approve:denied",
            )
        no_reject = self.access(
            permissions=FACADE_PERMISSIONS - {"review.reject"}
        )
        with self.assertRaises(AuthorizationRequired):
            self.facade.resolve(
                no_reject,
                candidate_id=candidate.artifact_id,
                decision="reject",
                decision_id="decision:reject:denied",
            )
        self.assertEqual(
            "pending",
            self.store.candidate_summary_for_ref(
                namespace=NS,
                artifact_id=candidate.artifact_id,
                artifact_version=1,
            ).status,
        )

    # -------------------------------------------------------- 6. WAIT/Legacy

    def test_wait_creates_no_candidate_and_no_approval_target(self) -> None:
        event, work = self.pending_event()
        facade = self.make_facade(
            ScriptedModel(
                [self.raw({"operation": "wait", "reason": "synthetic wait"})]
            )
        )
        view = facade.review(
            self.full_access(),
            work_id=work.work_id,
            trigger_event_id=event.event_id,
        )
        self.assertEqual("waiting", view.state)
        self.assertIsNone(view.candidate_id)
        self.assertEqual(
            (),
            facade.candidates(self.access(), work_id=work.work_id),
        )
        self.assertEqual(
            (),
            self.store.candidate_states_for_interaction(
                namespace=NS, interaction_id=view.interaction_id
            ),
        )

    def test_legacy_work_has_no_candidate_projection(self) -> None:
        event, work = self.pending_event()
        access = self.access()
        self.assertIn(
            work.work_id,
            {item.work_id for item in self.facade.legacy_work(access)},
        )
        self.assertNotIn(
            work.work_id,
            {item.work_id for item in self.facade.managed_work(access)},
        )
        self.assertEqual(
            (), self.facade.candidates(access, work_id=work.work_id)
        )
