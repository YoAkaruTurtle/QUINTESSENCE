"""J06 S8: the read-only EpistemicPartition projection."""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, RECORDED_AT, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains.cognition.epistemic import (  # noqa: E402
    EpistemicAccess,
    build_epistemic_partition,
)
from quintessence_pcs.domains.cognition.questions import QuestionAccess  # noqa: E402
from quintessence_pcs.domains.cognition.questions import QuestionService  # noqa: E402
from quintessence_pcs.domains.cognition.query import (  # noqa: E402
    ALL_PRIVACY,
    CURRENT_BELIEF_STATUSES,
    BeliefCurrentQuery,
    CognitionQueryService,
    QueryAccess,
)
from quintessence_pcs.foundation.errors import InvalidInput  # noqa: E402
from quintessence_pcs.store.governance import GovernanceStore  # noqa: E402
from quintessence_pcs.store.tx_b import OUTCOME_NOOP  # noqa: E402


def new_id() -> str:
    return str(uuid.uuid4())


def decision_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "decision_id": new_id(),
        "decision_seq": 1,
        "candidate_id": new_id(),
        "artifact_version": 1,
        "decision": "awaiting_human",
        "reason_code": "impact_requires_human",
        "impact": "E2",
        "derived_status": "supported",
        "support_basis": "evidence_backed",
        "requires_human": 1,
        "evidence_linkage": "verified",
        "policy_id": "policy:cognition.review.base.v2",
        "policy_version": "1",
        "impact_policy_id": "policy:cognition.risk.v1",
        "impact_policy_version": "1",
        "authority_kind": "none",
        "change_digest": "sha256:" + "a" * 64,
        "decision_request_id": new_id(),
        "decided_at": RECORDED_AT,
    }
    fields.update(overrides)
    return fields


class EpistemicPartitionTests(CognitionTestCase):
    """One fresh store per test; every Belief is written through the real path."""

    def access(self, *, privacy: frozenset[str] = frozenset({"private"})) -> EpistemicAccess:
        return EpistemicAccess(namespace=NS, privacy_classes=privacy)

    def partition(self, *, limit: int = 20, include_questions: bool = True,
                  question_access: object = None):
        return build_epistemic_partition(
            self.store,
            namespace=NS,
            access=self.access(),
            limit=limit,
            include_questions=include_questions,
            question_access=question_access,
        )

    def supported_belief(self, *, claim: str = "capability:file-read") -> object:
        event = self.ingest()
        body = self.belief_body(
            claim=claim,
            claim_type="capability",
            evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
        )
        return self.propose(body=body)

    def provisional_belief(self, *, claim: str = "hypothesis:races") -> object:
        body = self.belief_body(claim=claim, claim_type="general_hypothesis")
        return self.propose(body=body)

    # ------------------------------------------------------------ EP-01..05

    def test_ep_01_supported_lands_in_accepted(self) -> None:
        version = self.supported_belief()
        partition = self.partition()
        accepted = [item.ref.record_id for item in partition.accepted]
        provisional = [item.ref.record_id for item in partition.provisional]
        self.assertIn(version.record_id, accepted)
        self.assertNotIn(version.record_id, provisional)
        self.assertFalse(partition.accepted[0].provisional)

    def test_ep_02_proposed_never_reaches_accepted(self) -> None:
        version = self.provisional_belief()
        partition = self.partition()
        accepted = [item.ref.record_id for item in partition.accepted]
        provisional = {item.ref.record_id: item for item in partition.provisional}
        self.assertNotIn(version.record_id, accepted)
        self.assertIn(version.record_id, provisional)
        self.assertTrue(provisional[version.record_id].provisional)
        self.assertEqual("proposed", provisional[version.record_id].status)

    def test_ep_03_contested_and_contradicted_share_the_conflict_bucket(self) -> None:
        support = self.ingest()
        against = self.ingest()
        contested = self.propose(
            body=self.belief_body(
                claim="capability:contested",
                claim_type="capability",
                evidence_links=[
                    self.evidence_link(self.event_ref(support.event_id)),
                    self.evidence_link(
                        self.event_ref(against.event_id), relation="contradicts"
                    ),
                ],
            )
        )
        contradicted = self.propose(
            body=self.belief_body(
                claim="capability:contradicted",
                claim_type="capability",
                evidence_links=[
                    self.evidence_link(
                        self.event_ref(against.event_id), relation="contradicts"
                    )
                ],
            )
        )
        partition = self.partition()
        contested_ids = {item.ref.record_id for item in partition.contested}
        self.assertIn(contested.record_id, contested_ids)
        self.assertIn(contradicted.record_id, contested_ids)
        statuses = {item.ref.record_id: item.status for item in partition.contested}
        self.assertEqual("contested", statuses[contested.record_id])
        self.assertEqual("contradicted", statuses[contradicted.record_id])

    def test_ep_04_expired_claim_lands_in_stale(self) -> None:
        event = self.ingest()
        version = self.propose(
            body=self.belief_body(
                claim="capability:stale",
                claim_type="capability",
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
                valid_until="2020-01-01T00:00:00Z",
            )
        )
        partition = self.partition()
        self.assertEqual(
            [version.record_id], [item.ref.record_id for item in partition.stale]
        )
        self.assertEqual("stale", partition.stale[0].status)

    def test_ep_05_retracted_is_in_no_bucket_and_is_counted(self) -> None:
        version = self.provisional_belief(claim="hypothesis:retracted")
        event = self.ingest()
        retracted = self.revise(
            self.record_ref(version),
            body=self.belief_body(
                claim="hypothesis:retracted",
                claim_type="general_hypothesis",
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
            ),
            retract=True,
        )
        partition = self.partition()
        for bucket in (
            partition.accepted,
            partition.provisional,
            partition.contested,
            partition.stale,
        ):
            self.assertNotIn(
                retracted.record_id, [item.ref.record_id for item in bucket]
            )
        self.assertEqual(1, partition.retracted_count)

    # ---------------------------------------------------------------- EP-06

    def test_ep_06_buckets_are_disjoint_and_cover_every_live_current_belief(
        self,
    ) -> None:
        supported = self.supported_belief()
        provisional = self.provisional_belief()
        partition = self.partition()
        buckets = [
            {item.ref.record_id for item in partition.accepted},
            {item.ref.record_id for item in partition.provisional},
            {item.ref.record_id for item in partition.contested},
            {item.ref.record_id for item in partition.stale},
        ]
        union: set[str] = set()
        for bucket in buckets:
            self.assertEqual(set(), union & bucket)
            union |= bucket
        self.assertEqual({supported.record_id, provisional.record_id}, union)

    # ----------------------------------------------------------- EP-07 / 08

    def test_ep_07_real_append_advances_revision_and_shows_up(self) -> None:
        before = self.store.current_state_revision(NS)
        record_id = new_id()
        body = self.belief_body(claim="hypothesis:appended", claim_type="general_hypothesis")
        snapshot = self.store.capture_snapshot(NS)

        def apply(scope: object) -> None:
            scope.append_record_version(  # type: ignore[attr-defined]
                record_id=record_id,
                record_version=1,
                kind="belief",
                body=body,
                status="proposed",
                scope={},
            )

        self.store.commit_command(
            context_id=snapshot.context_id,
            request_id=new_id(),
            operation="cognition.change",
            request_payload={"change": "ep-07"},
            apply=apply,
            namespace=NS,
        )
        self.assertEqual(before + 1, self.store.current_state_revision(NS))
        partition = self.partition()
        self.assertIn(
            record_id, {item.ref.record_id for item in partition.provisional}
        )
        self.assertNotIn(
            record_id, {item.ref.record_id for item in partition.accepted}
        )

    def test_ep_08_decision_only_command_keeps_revision_but_is_readable(self) -> None:
        before = self.store.current_state_revision(NS)
        fields = decision_fields()
        snapshot = self.store.capture_snapshot(NS)

        def apply(scope: object) -> None:
            scope.record_review_decision(  # type: ignore[attr-defined]
                namespace=NS, fields=fields
            )

        receipt = self.store.commit_command(
            context_id=snapshot.context_id,
            request_id=new_id(),
            operation="cognition.decision",
            request_payload={"decision_id": fields["decision_id"]},
            apply=apply,
            namespace=NS,
        )
        self.assertEqual(OUTCOME_NOOP, receipt.outcome)
        self.assertEqual(before, self.store.current_state_revision(NS))
        governance = GovernanceStore(self.store)
        decisions = governance.list_decisions(NS, after_seq=0)
        self.assertEqual(1, len(decisions))
        self.assertEqual(fields["decision_id"], decisions[0].decision_id)
        # the projection reads the same store without inventing a bucket entry
        partition = self.partition()
        self.assertEqual(0, len(partition.accepted))
        self.assertEqual(0, len(partition.provisional))

    # ---------------------------------------------------------------- EP-09

    def test_ep_09_provenance_is_a_point_lookup_and_never_guessed(self) -> None:
        committed = new_id()
        decision = decision_fields(decision="committed", decision_seq=1)
        body = self.belief_body(claim="hypothesis:provenance", claim_type="general_hypothesis")
        snapshot = self.store.capture_snapshot(NS)

        def apply(scope: object) -> None:
            scope.append_record_version(  # type: ignore[attr-defined]
                record_id=committed,
                record_version=1,
                kind="belief",
                body=body,
                status="proposed",
                scope={},
            )
            scope.record_review_decision(  # type: ignore[attr-defined]
                namespace=NS, fields=decision
            )
            scope.link_record_decision(  # type: ignore[attr-defined]
                namespace=NS,
                record_id=committed,
                record_version=1,
                decision_id=decision["decision_id"],
                decided_at=RECORDED_AT,
            )

        self.store.commit_command(
            context_id=snapshot.context_id,
            request_id=new_id(),
            operation="cognition.change",
            request_payload={"change": "ep-09"},
            apply=apply,
            namespace=NS,
        )
        linked = self.provisional_belief(claim="hypothesis:no-provenance")
        partition = self.partition()
        provenance = {
            item.ref.record_id: item.provenance for item in partition.provisional
        }
        self.assertIsNotNone(provenance[committed])
        self.assertEqual(decision["decision_id"], provenance[committed].decision_id)
        self.assertEqual(decision["candidate_id"], provenance[committed].candidate_id)
        self.assertEqual("none", provenance[committed].authority_kind)
        self.assertEqual("E2", provenance[committed].impact)
        self.assertEqual(RECORDED_AT, provenance[committed].decided_at)
        self.assertIsNone(provenance[linked.record_id])

    # ---------------------------------------------------------------- EP-10

    def test_ep_10_belief_whose_evidence_was_deleted_disappears(self) -> None:
        event = self.ingest()
        version = self.propose(
            body=self.belief_body(
                claim="capability:deleted-evidence",
                claim_type="capability",
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
            )
        )
        self.assertIn(
            version.record_id,
            {item.ref.record_id for item in self.partition().accepted},
        )
        self.delete_event(event.event_id)
        partition = self.partition()
        for bucket in (
            partition.accepted,
            partition.provisional,
            partition.contested,
            partition.stale,
        ):
            self.assertNotIn(
                version.record_id, [item.ref.record_id for item in bucket]
            )

    # ---------------------------------------------------------------- EP-11

    def test_ep_11_limit_truncates_output_without_moving_buckets(self) -> None:
        for index in range(3):
            self.provisional_belief(claim=f"hypothesis:limit-{index}")
        full = self.partition(limit=20)
        self.assertFalse(full.truncated)
        self.assertEqual(3, len(full.provisional))

        small = self.partition(limit=1)
        self.assertTrue(small.truncated)
        total = (
            len(small.accepted)
            + len(small.provisional)
            + len(small.contested)
            + len(small.stale)
        )
        self.assertEqual(1, total)

    # ---------------------------------------------------------------- EP-12

    def test_ep_12_projection_is_deterministic(self) -> None:
        self.supported_belief()
        self.provisional_belief()
        first = self.partition()
        second = self.partition()
        self.assertEqual(first, second)

    # ---------------------------------------------------------------- EP-13

    def test_ep_13_c10_current_query_is_unchanged_and_still_excludes_proposed(
        self,
    ) -> None:
        self.assertEqual(
            ("supported", "insufficient", "contradicted"), CURRENT_BELIEF_STATUSES
        )
        version = self.provisional_belief()
        access = QueryAccess(
            namespace=NS,
            privacy_classes=ALL_PRIVACY,
            purposes=frozenset(("consult", "reasoning", "history")),
        )
        result = CognitionQueryService(self.store, clock=self.clock).query_current_beliefs(
            access, BeliefCurrentQuery(limit=20)
        )
        self.assertNotIn(version.record_id, {item.ref.record_id for item in result.items})
        self.assertIn(
            version.record_id,
            {item.ref.record_id for item in self.partition().provisional},
        )

    # ---------------------------------------------------------------- EP-14

    def test_ep_14_questions_are_opt_in(self) -> None:
        self.provisional_belief()
        without = self.partition(include_questions=False)
        self.assertEqual((), without.questions)
        self.assertEqual(1, len(without.provisional))

        silent = self.partition(include_questions=True)
        self.assertEqual((), silent.questions)

    def test_ep_14b_questions_are_surfaced_when_access_is_supplied(self) -> None:
        context = self.build_context()
        question_access = QuestionAccess(
            namespace=NS,
            host_instance="cognition-tests",
            access_ref="access:epistemic",
            privacy_classes=frozenset({"private"}),
            permissions=frozenset({"question.read", "question.create"}),
            purposes=frozenset({"cognition.question"}),
        )
        created = QuestionService(self.store).create(
            question_access,
            context_id=context.context_id,
            request_id=new_id(),
            question="why did the synthetic query fail?",
            origin="system_proposed",
            privacy_class="private",
        )
        partition = self.partition(
            include_questions=True, question_access=question_access
        )
        self.assertEqual(1, len(partition.questions))
        self.assertEqual(created.view.ref.record_id, partition.questions[0].ref.record_id)

    # ------------------------------------------------------------- guards

    def test_rejects_access_from_another_namespace(self) -> None:
        with self.assertRaises(InvalidInput):
            build_epistemic_partition(
                self.store,
                namespace=NS,
                access=EpistemicAccess(
                    namespace="other.namespace", privacy_classes=frozenset({"private"})
                ),
            )

    # ---------------------------------------------------------------- EA-14

    def test_ea_14_linkage_reports_the_committing_decision(self) -> None:
        committed = new_id()
        decision = decision_fields(decision="committed", decision_seq=1)
        body = self.belief_body(claim="hypothesis:linkage", claim_type="general_hypothesis")
        snapshot = self.store.capture_snapshot(NS)

        def apply(scope: object) -> None:
            scope.append_record_version(  # type: ignore[attr-defined]
                record_id=committed,
                record_version=1,
                kind="belief",
                body=body,
                status="proposed",
                scope={},
            )
            scope.record_review_decision(  # type: ignore[attr-defined]
                namespace=NS, fields=decision
            )
            scope.link_record_decision(  # type: ignore[attr-defined]
                namespace=NS,
                record_id=committed,
                record_version=1,
                decision_id=decision["decision_id"],
                decided_at=RECORDED_AT,
            )

        self.store.commit_command(
            context_id=snapshot.context_id,
            request_id=new_id(),
            operation="cognition.change",
            request_payload={"change": "ea-14"},
            apply=apply,
            namespace=NS,
        )
        unlinked = self.provisional_belief(claim="hypothesis:no-linkage")
        items = {
            item.ref.record_id: item for item in self.partition().provisional
        }
        self.assertEqual("verified", items[committed].linkage)
        self.assertEqual("not_applicable", items[unlinked.record_id].linkage)

    def test_ea_14_unverified_linkage_is_reported_as_such(self) -> None:
        committed = new_id()
        decision = decision_fields(
            decision="committed",
            decision_seq=1,
            evidence_linkage="unverified",
        )
        body = self.belief_body(
            claim="claim:unverified", claim_type="capability"
        )
        snapshot = self.store.capture_snapshot(NS)

        def apply(scope: object) -> None:
            scope.append_record_version(  # type: ignore[attr-defined]
                record_id=committed,
                record_version=1,
                kind="belief",
                body=body,
                status="proposed",
                scope={},
            )
            scope.record_review_decision(  # type: ignore[attr-defined]
                namespace=NS, fields=decision
            )
            scope.link_record_decision(  # type: ignore[attr-defined]
                namespace=NS,
                record_id=committed,
                record_version=1,
                decision_id=decision["decision_id"],
                decided_at=RECORDED_AT,
            )

        self.store.commit_command(
            context_id=snapshot.context_id,
            request_id=new_id(),
            operation="cognition.change",
            request_payload={"change": "ea-14b"},
            apply=apply,
            namespace=NS,
        )
        items = {item.ref.record_id: item for item in self.partition().provisional}
        self.assertEqual("unverified", items[committed].linkage)

    def test_rejects_out_of_range_limit(self) -> None:
        for bad in (0, 51, "5"):
            with self.subTest(limit=bad):
                with self.assertRaises(InvalidInput):
                    self.partition(limit=bad)  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
