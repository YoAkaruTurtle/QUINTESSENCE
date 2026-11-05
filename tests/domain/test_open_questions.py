"""J05 A group: OpenQuestion lifecycle, Tx B, dedupe, pause, deletion and due."""

from __future__ import annotations

import sys
import unittest
from unittest import mock
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains.cognition.questions import (  # noqa: E402
    ACTIVE_STATUSES,
    DuplicateQuestion,
    EvaluationReceipt,
    QuestionAccess,
    QuestionInvalid,
    QuestionService,
    QuestionUnavailable,
    TriggerLimitReached,
)
from quintessence_pcs.domains.cognition.errors import InvalidTransition  # noqa: E402
from quintessence_pcs.domains.refs import RecordRef  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    CommandConflict,
    InvalidInput,
    RecordVersionConflict,
    Redacted,
    ScopeViolation,
    SnapshotNotFound,
    SourceRedacted,
    StateRevisionConflict,
)
from quintessence_pcs.store.errors import ConsultContextNotCommittable  # noqa: E402

ALL_PERMISSIONS = frozenset(
    {
        "question.read",
        "question.create",
        "question.transition",
        "question.evaluate",
        "question.attention",
    }
)


class OpenQuestionTests(CognitionTestCase):
    def access(
        self,
        *,
        permissions: frozenset[str] = ALL_PERMISSIONS,
        privacy: frozenset[str] = frozenset({"private"}),
        namespace: str = NS,
    ) -> QuestionAccess:
        return QuestionAccess(
            namespace,
            "host:j05",
            "authorization:j05",
            privacy,
            permissions,
            frozenset({"cognition.question"}),
        )

    def event(
        self,
        *,
        privacy: str = "private",
        event_type: str = "TestTrigger",
        text: str = "synthetic event",
        namespace: str = NS,
    ):
        return self.store.ingest_event(
            namespace=namespace,
            source_kind="host_observation",
            source_instance="j05-tests",
            source_event_id=f"j05-{uuid.uuid4()}",
            event_type=event_type,
            payload={"text": text},
            privacy_class=privacy,
        )

    def snapshot(self, store=None) -> str:
        return (store or self.store).capture_snapshot(NS).context_id

    def service(self, store=None) -> QuestionService:
        return QuestionService(store or self.store)

    def create(
        self,
        *,
        question: str = "Why is the service unstable?",
        origin: str = "user_requested",
        scope: dict | None = None,
        refs: list[dict] | None = None,
        privacy: str = "private",
        priority: str = "normal",
        trigger_spec: dict | None = None,
        review_after: str | None = None,
        service: QuestionService | None = None,
        access: QuestionAccess | None = None,
        context_id: str | None = None,
    ):
        ev = self.event()
        return (service or self.service()).create(
            access or self.access(),
            context_id=context_id or self.snapshot(),
            request_id=str(uuid.uuid4()),
            question=question,
            origin=origin,
            scope={"project_id": "p1"} if scope is None else scope,
            origin_refs=[self.event_ref(ev.event_id)] if refs is None else refs,
            privacy_class=privacy,
            priority=priority,
            trigger_spec=trigger_spec,
            review_after=review_after,
        )

    def close_question(self, view):
        return self.service().transition(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=view.ref,
            expected_version=view.ref.record_version,
            action="close",
            reason="synthetic close",
        )

    # ------------------------------------------------------------- AC-01/02

    def test_create_read_and_restart_without_goal(self):
        result = self.create()
        self.assertFalse(result.deduped)
        self.assertEqual("open", result.view.status)
        self.assertEqual("Why is the service unstable?", result.view.question)
        self.assertEqual(1, len(result.view.origin_refs))

        reopened = self.open_second_store()
        view = self.service(reopened).get(
            self.access(), ref=result.view.ref, scope={"project_id": "p1"}
        )
        self.assertEqual(result.view.ref, view.ref)
        self.assertEqual("open", view.status)

    def test_legal_lifecycle_and_no_in_place_overwrite(self):
        result = self.create()
        ref = result.view.ref
        svc = self.service()
        access = self.access()

        waiting = svc.transition(
            access,
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=ref,
            expected_version=1,
            action="wait",
            waiting_for="need logs",
            review_after="2026-10-25T00:00:00Z",
        )
        paused = svc.transition(
            access,
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=waiting.ref,
            expected_version=2,
            action="pause",
            pause_reason="user asked",
            paused_by="user",
        )
        resumed = svc.transition(
            access,
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=paused.ref,
            expected_version=3,
            action="resume",
        )
        resolution_event = self.event()
        resolved = svc.transition(
            access,
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=resumed.ref,
            expected_version=4,
            action="resolve",
            resolution_refs=[self.event_ref(resolution_event.event_id)],
            reason="evidence sufficient",
        )
        reopen_event = self.event()
        reopened = svc.transition(
            access,
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=resolved.ref,
            expected_version=5,
            action="reopen",
            new_evidence_refs=[self.event_ref(reopen_event.event_id)],
            reason="new contradictory evidence",
        )
        closed = svc.transition(
            access,
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=reopened.ref,
            expected_version=6,
            action="close",
            reason="no longer applicable",
        )
        self.assertEqual("waiting", waiting.status)
        self.assertTrue(paused.paused)
        self.assertEqual("open", resumed.status)
        self.assertEqual("resolved", resolved.status)
        self.assertEqual("open", reopened.status)
        self.assertEqual("closed", closed.status)
        self.assertEqual(7, closed.ref.record_version)
        # The original version is immutable and still readable.
        original = svc.get(access, ref=ref, scope={"project_id": "p1"})
        self.assertEqual("open", original.status)
        self.assertEqual(1, original.ref.record_version)

    def test_illegal_transitions_and_action_fields(self):
        result = self.create()
        svc = self.service()
        access = self.access()
        with self.assertRaises(InvalidTransition):
            svc.transition(
                access,
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                ref=result.view.ref,
                expected_version=1,
                action="accept",
            )
        with self.assertRaises(QuestionInvalid):
            svc.transition(
                access,
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                ref=result.view.ref,
                expected_version=1,
                action="wait",
            )
        with self.assertRaises(QuestionInvalid):
            svc.transition(
                access,
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                ref=result.view.ref,
                expected_version=1,
                action="resolve",
                resolution_refs=[],
                reason="nothing",
            )
        with self.assertRaises(QuestionInvalid):
            svc.transition(
                access,
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                ref=result.view.ref,
                expected_version=1,
                action="wait",
                waiting_for="x",
                reason="not allowed here",
            )

    def test_closed_is_terminal(self):
        result = self.create()
        closed = self.close_question(result.view)
        with self.assertRaises(InvalidTransition):
            self.service().transition(
                self.access(),
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                ref=closed.ref,
                expected_version=2,
                action="reopen",
                new_evidence_refs=[self.event_ref(self.event().event_id)],
                reason="no",
            )

    # --------------------------------------------------------------- AC-03/04

    def test_pause_and_waiting_are_not_auto_resumed(self):
        result = self.create()
        waiting = self.service().transition(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=result.view.ref,
            expected_version=1,
            action="wait",
            waiting_for="logs",
            review_after="2026-10-25T00:00:00Z",
        )
        paused = self.service().transition(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=waiting.ref,
            expected_version=2,
            action="pause",
            pause_reason="user pause",
            paused_by="user",
        )
        self.event(text="unrelated")
        after = self.service().get(
            self.access(), ref=paused.ref, scope={"project_id": "p1"}
        )
        self.assertEqual("waiting", after.status)
        self.assertTrue(after.paused)
        attention = self.service().attention(
            self.access(),
            request=self.attention_request(include_paused=True),
        )
        self.assertEqual(1, len(attention.items))
        self.assertFalse(attention.items[0].actionable)

    def test_investigation_end_does_not_resolve(self):
        result = self.create()
        # A normal read/attention/event does not change status.
        self.service().get(self.access(), ref=result.view.ref, scope={"project_id": "p1"})
        self.service().attention(self.access(), request=self.attention_request())
        self.event(text="unrelated")
        current = self.store.get_current(NS, result.view.ref.record_id)
        self.assertEqual("open", current.status)
        self.assertIsNone(current.body["last_evaluated_at"])

    # --------------------------------------------------------------- AC-05/20

    def test_evaluate_requires_result_and_writes_watermark(self):
        result = self.create()
        svc = self.service()
        access = self.access()
        ev = self.event()
        with self.assertRaises(QuestionInvalid):
            svc.evaluate(
                access,
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                ref=result.view.ref,
                expected_version=1,
                evaluation_result="bogus",
                reason="x",
            )
        with self.assertRaises(QuestionInvalid):
            svc.evaluate(
                access,
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                ref=result.view.ref,
                expected_version=1,
                evaluation_result="no_change",
                reason=" ",
            )
        receipt = svc.evaluate(
            access,
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=result.view.ref,
            expected_version=1,
            evaluation_result="no_change",
            reason="checked sources",
            evaluated_source_refs=[self.event_ref(ev.event_id)],
        )
        self.assertIsInstance(receipt, EvaluationReceipt)
        self.assertEqual(2, receipt.new_version)
        current = self.store.get_current(NS, result.view.ref.record_id)
        self.assertEqual(2, current.record_version)
        self.assertIsNotNone(current.body["last_evaluated_at"])
        self.assertEqual(2, current.body["last_evaluated_revision"])

    @mock.patch("quintessence_pcs.domains.cognition.questions.utc_now_rfc3339",
                new=lambda: "2026-10-26T12:00:00Z")
    def test_due_clears_after_evaluate_and_rearms(self):
        result = self.create(review_after="2026-10-25T00:00:00Z")
        svc = self.service()
        access = self.access()
        attention = svc.attention(access, request=self.attention_request())
        self.assertEqual(["review_due"], list(attention.items[0].reason_codes))
        evaluated_event = self.event()
        receipt = svc.evaluate(
            access,
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=result.view.ref,
            expected_version=1,
            evaluation_result="evidence_reviewed",
            reason="checked",
            evaluated_source_refs=[self.event_ref(evaluated_event.event_id)],
            review_after="2026-10-27T00:00:00Z",
        )
        # The old due was consumed; a new future review_after is due only after
        # the supplied Attention clock crosses it.
        rearmed = svc.attention(
            access,
            request=self.attention_request(now="2026-10-27T00:00:01Z"),
        )
        self.assertEqual(1, len(rearmed.items))
        self.assertEqual(receipt.new_version, 2)

    # ----------------------------------------------------------------- AC-07/08

    def test_dedupe_same_scope_and_isolation(self):
        result = self.create(question="Same   question")
        again = self.create(question="  same question ")
        self.assertTrue(again.deduped)
        self.assertEqual(result.view.ref, again.view.ref)

        other_scope = self.create(
            question="Same question", scope={"project_id": "p2"}
        )
        self.assertFalse(other_scope.deduped)
        self.assertNotEqual(result.view.ref, other_scope.view.ref)

        similar = self.create(question="Same question but different")
        self.assertFalse(similar.deduped)
        self.assertNotEqual(result.view.ref, similar.view.ref)

    def test_closed_rebuild_gets_new_identity_and_relation(self):
        result = self.create()
        closed = self.close_question(result.view)
        rebuilt = self.create()
        self.assertFalse(rebuilt.deduped)
        self.assertNotEqual(closed.ref.record_id, rebuilt.view.ref.record_id)
        self.assertIn(
            closed.ref,
            [
                RecordRef.from_value(item, field="related")
                for item in rebuilt.view.related_question_refs
            ],
        )

    # --------------------------------------------------------------- AC-09/10/11

    def test_origin_deletion_blocks_current_exact_and_attention(self):
        ev = self.event()
        result = self.service().create(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            question="Will this source disappear?",
            origin="user_requested",
            scope={"project_id": "p1"},
            origin_refs=[self.event_ref(ev.event_id)],
            privacy_class="private",
        )
        self.service().transition(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=result.view.ref,
            expected_version=1,
            action="wait",
            waiting_for="logs",
        )
        self.store.request_deletion(
            target_kind="event",
            namespace=NS,
            target_id=ev.event_id,
            authorization_ref="authorization:test",
        )
        with self.assertRaises(QuestionUnavailable):
            self.service().get(
                self.access(),
                ref=RecordRef(NS, result.view.ref.record_id, 2),
                scope={"project_id": "p1"},
            )
        with self.assertRaises(Redacted):
            self.service().get(
                self.access(),
                ref=RecordRef(NS, result.view.ref.record_id, 1),
                scope={"project_id": "p1"},
            )
        self.assertEqual(
            0,
            len(self.service().attention(self.access(), request=self.attention_request()).items),
        )

    def test_resolution_deletion_is_not_delivered_as_resolved(self):
        ev = self.event()
        result = self.service().create(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            question="Resolved by evidence?",
            origin="user_requested",
            scope={"project_id": "p1"},
            origin_refs=[self.event_ref(ev.event_id)],
            privacy_class="private",
        )
        resolved = self.service().transition(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=result.view.ref,
            expected_version=1,
            action="resolve",
            resolution_refs=[self.event_ref(ev.event_id)],
            reason="single source",
        )
        self.store.request_deletion(
            target_kind="event",
            namespace=NS,
            target_id=ev.event_id,
            authorization_ref="authorization:test",
        )
        with self.assertRaises(QuestionUnavailable):
            self.service().get(
                self.access(), ref=resolved.ref, scope={"project_id": "p1"}
            )
        with self.assertRaises(Redacted):
            self.service().get(
                self.access(),
                ref=RecordRef(NS, resolved.ref.record_id, 1),
                scope={"project_id": "p1"},
            )
        stored = self.store.get_current(NS, resolved.ref.record_id)
        self.assertEqual("resolved", stored.status)

    # ------------------------------------------------------------- AC-12/17

    def test_request_id_receipt_recovery(self):
        ev = self.event()
        request_id = str(uuid.uuid4())
        context = self.snapshot()
        svc = self.service()
        first = svc.create(
            self.access(),
            context_id=context,
            request_id=request_id,
            question="Recovered receipt?",
            origin="user_requested",
            scope={"project_id": "p1"},
            origin_refs=[self.event_ref(ev.event_id)],
            privacy_class="private",
        )
        second = svc.create(
            self.access(),
            context_id=context,
            request_id=request_id,
            question="Recovered receipt?",
            origin="user_requested",
            scope={"project_id": "p1"},
            origin_refs=[self.event_ref(ev.event_id)],
            privacy_class="private",
        )
        self.assertFalse(second.deduped)
        self.assertIsNotNone(second.receipt)
        self.assertEqual(first.view.ref, second.view.ref)

    def test_snapshot_not_found_and_consult_context(self):
        svc = self.service()
        with self.assertRaises(SnapshotNotFound):
            svc.create(
                self.access(),
                context_id=str(uuid.uuid4()),
                request_id=str(uuid.uuid4()),
                question="No snapshot",
                origin="user_requested",
                scope={"project_id": "p1"},
                origin_refs=[self.event_ref(self.event().event_id)],
                privacy_class="private",
            )
        snap = self.store.capture_snapshot(NS)
        self.store.connection.execute(
            "UPDATE context_snapshots SET context_kind = 'consult' WHERE context_id = ?",
            (snap.context_id,),
        )
        self.store.connection.commit()
        with self.assertRaises(ConsultContextNotCommittable):
            svc.create(
                self.access(),
                context_id=snap.context_id,
                request_id=str(uuid.uuid4()),
                question="Consult cannot commit",
                origin="user_requested",
                scope={"project_id": "p1"},
                origin_refs=[self.event_ref(self.event().event_id)],
                privacy_class="private",
            )

    def test_stale_snapshot_transition_rejected(self):
        result = self.create()
        second_event = self.event()
        stale = self.snapshot()
        self.service().create(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            question="another question",
            origin="user_requested",
            scope={"project_id": "p1"},
            origin_refs=[self.event_ref(second_event.event_id)],
            privacy_class="private",
        )
        with self.assertRaises(StateRevisionConflict):
            self.service().transition(
                self.access(),
                context_id=stale,
                request_id=str(uuid.uuid4()),
                ref=result.view.ref,
                expected_version=1,
                action="wait",
                waiting_for="logs",
            )

    def test_expected_version_conflict(self):
        result = self.create()
        with self.assertRaises(RecordVersionConflict):
            self.service().transition(
                self.access(),
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                ref=result.view.ref,
                expected_version=2,
                action="wait",
                waiting_for="logs",
            )

    # ----------------------------------------------------------------- AC-18

    def test_real_snapshot_conflict_returns_deduped(self):
        a = self.store
        b = self.open_second_store()
        acc = self.access()
        ev = self.event()
        ref = self.event_ref(ev.event_id)
        snapshot_b = b.capture_snapshot(NS)
        first = QuestionService(a).create(
            acc,
            context_id=a.capture_snapshot(NS).context_id,
            request_id=str(uuid.uuid4()),
            question="Concurrent question",
            origin="user_requested",
            scope={"project_id": "p1"},
            origin_refs=[ref],
            privacy_class="private",
        )
        second = QuestionService(b).create(
            acc,
            context_id=snapshot_b.context_id,
            request_id=str(uuid.uuid4()),
            question="Concurrent question",
            origin="user_requested",
            scope={"project_id": "p1"},
            origin_refs=[ref],
            privacy_class="private",
        )
        self.assertTrue(second.deduped)
        self.assertIsNone(second.receipt)
        self.assertEqual(first.view.ref, second.view.ref)
        active = [
            item
            for item in a.current_versions_of_kind(NS, "cognition.open_question")
            if item.status in ACTIVE_STATUSES
        ]
        self.assertEqual(1, len(active))

    def test_closed_rebuild_real_snapshot_conflict(self):
        first = self.create()
        self.close_question(first.view)
        a = self.store
        b = self.open_second_store()
        ev = self.event()
        ref = self.event_ref(ev.event_id)
        snapshot_b = b.capture_snapshot(NS)
        rebuilt = QuestionService(a).create(
            self.access(),
            context_id=a.capture_snapshot(NS).context_id,
            request_id=str(uuid.uuid4()),
            question="Why is the service unstable?",
            origin="user_requested",
            scope={"project_id": "p1"},
            origin_refs=[ref],
            privacy_class="private",
        )
        second = QuestionService(b).create(
            self.access(),
            context_id=snapshot_b.context_id,
            request_id=str(uuid.uuid4()),
            question="Why is the service unstable?",
            origin="user_requested",
            scope={"project_id": "p1"},
            origin_refs=[ref],
            privacy_class="private",
        )
        self.assertTrue(second.deduped)
        self.assertEqual(rebuilt.view.ref, second.view.ref)
        self.assertNotEqual(first.view.ref.record_id, rebuilt.view.ref.record_id)

    # --------------------------------------------------------------- AC-19/22

    def test_trigger_cap_and_idempotent_reconsume(self):
        triggers = [self.event() for _ in range(16)]
        result = self.create(
            trigger_spec={
                "source_refs": [self.event_ref(ev.event_id) for ev in triggers],
                "topic_keys": [],
            }
        )
        svc = self.service()
        ref = result.view.ref
        expected = 1
        for ev in triggers:
            view = svc.consume_trigger(
                self.access(),
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                ref=ref,
                expected_version=expected,
                trigger_ref=self.event_ref(ev.event_id),
            )
            expected += 1
            ref = view.ref
        full = self.store.get_current(NS, ref.record_id)
        self.assertEqual(17, full.record_version)
        self.assertEqual(16, len(full.body["consumed_trigger_refs"]))
        extra = self.event()
        updated = svc.transition(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=ref,
            expected_version=17,
            action="update_triggers",
            trigger_spec={
                "source_refs": [self.event_ref(extra.event_id)],
                "topic_keys": [],
            },
        )
        with self.assertRaises(TriggerLimitReached):
            svc.consume_trigger(
                self.access(),
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                ref=updated.ref,
                expected_version=18,
                trigger_ref=self.event_ref(extra.event_id),
            )
        unchanged = self.store.get_current(NS, ref.record_id)
        self.assertEqual(18, unchanged.record_version)
        reconsume = svc.consume_trigger(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=updated.ref,
            expected_version=18,
            trigger_ref=self.event_ref(triggers[0].event_id),
        )
        self.assertEqual(18, reconsume.ref.record_version)

    def test_consume_rejects_unrelated_trigger(self):
        result = self.create()
        unrelated = self.event()
        with self.assertRaises(QuestionInvalid):
            self.service().consume_trigger(
                self.access(),
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                ref=result.view.ref,
                expected_version=1,
                trigger_ref=self.event_ref(unrelated.event_id),
            )

    # ----------------------------------------------------------------- AC-21

    def test_multi_privacy_sources_fail_closed(self):
        private = self.event(privacy="private")
        restricted = self.event(privacy="restricted")
        with self.assertRaises(QuestionInvalid):
            self.service().create(
                self.access(privacy=frozenset({"private", "restricted"})),
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                question="Mixed privacy",
                origin="user_requested",
                scope={"project_id": "p1"},
                origin_refs=[self.event_ref(private.event_id), self.event_ref(restricted.event_id)],
                privacy_class="private",
            )

    def test_j04_bridge_event_requires_projection_gate(self):
        bridge = self.event(event_type="experience.observed")
        with self.assertRaises(SourceRedacted):
            self.service().create(
                self.access(),
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                question="Bridge event?",
                origin="user_requested",
                scope={"project_id": "p1"},
                origin_refs=[self.event_ref(bridge.event_id)],
                privacy_class="private",
            )

    def test_j04_invalidated_projection_is_rejected(self):
        bridge = self.event(event_type="experience.observed")
        ref = self.event_ref(bridge.event_id)
        self.store.connection.execute(
            "INSERT INTO experience_source_mapping ("
            "namespace, ref_type, ref_id, ref_version, event_id, source_event_key, "
            "source_revision, privacy_classes_json, retention, origin, role, "
            "principal_kind, root_source_key, root_members_json, root_verified, "
            "source_dependencies_json, delivery_id, format_version, created_at, "
            "invalidated_at, invalidation_reason"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                NS, "collab_input", str(uuid.uuid4()), 1, bridge.event_id,
                "bridge-key", 1, '["private"]', "standard", "human_direct", "user",
                "host_bound_user", "a" * 64, "[]", 0, "[]", str(uuid.uuid4()), "1",
                "2026-10-26T00:00:00Z", "2026-10-26T00:00:00Z", "source_deleted:collab_input",
            ),
        )
        self.store.connection.commit()
        with self.assertRaises(SourceRedacted):
            self.service().create(
                self.access(),
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                question="Invalidated bridge event?",
                origin="user_requested",
                scope={"project_id": "p1"},
                origin_refs=[ref],
                privacy_class="private",
            )

    def test_proposed_requires_host_accept(self):
        ev = self.event()
        result = self.service().create(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            question="System suggested a question",
            origin="system_proposed",
            scope={"project_id": "p1"},
            origin_refs=[self.event_ref(ev.event_id)],
            privacy_class="private",
        )
        self.assertEqual("proposed", result.view.status)
        accepted = self.service().transition(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=result.view.ref,
            expected_version=1,
            action="accept",
        )
        self.assertEqual("open", accepted.status)

    def test_update_triggers_and_link_related_question(self):
        first = self.create(question="first")
        second = self.create(question="second")
        trigger = self.event()
        updated = self.service().transition(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=first.view.ref,
            expected_version=1,
            action="update_triggers",
            trigger_spec={
                "source_refs": [self.event_ref(trigger.event_id)],
                "topic_keys": ["synthetic"],
            },
        )
        self.assertEqual(
            [self.event_ref(trigger.event_id)], updated.trigger_spec["source_refs"]
        )
        linked = self.service().transition(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=updated.ref,
            expected_version=2,
            action="link_related_question",
            related_question_ref=second.view.ref.to_dict(),
        )
        self.assertIn(second.view.ref, [
            RecordRef.from_value(item, field="related")
            for item in linked.related_question_refs
        ])
        with self.assertRaises(QuestionInvalid):
            self.service().transition(
                self.access(),
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                ref=linked.ref,
                expected_version=3,
                action="link_related_question",
                related_question_ref=linked.ref.to_dict(),
            )

    def test_question_own_deletion_blocks_reads(self):
        result = self.create()
        self.service().transition(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=result.view.ref,
            expected_version=1,
            action="wait",
            waiting_for="logs",
        )
        self.store.request_deletion(
            target_kind="record",
            namespace=NS,
            target_id=result.view.ref.record_id,
            authorization_ref="authorization:test",
        )
        with self.assertRaises(QuestionUnavailable):
            self.service().get(
                self.access(),
                ref=RecordRef(NS, result.view.ref.record_id, 2),
                scope={"project_id": "p1"},
            )
        with self.assertRaises(Redacted):
            self.service().get(
                self.access(),
                ref=RecordRef(NS, result.view.ref.record_id, 1),
                scope={"project_id": "p1"},
            )

    def test_cross_namespace_and_permission_are_rejected(self):
        other = self.event(namespace="example.test.cognition.other")
        with self.assertRaises(ScopeViolation):
            self.service().create(
                self.access(),
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                question="cross namespace",
                origin="user_requested",
                scope={"project_id": "p1"},
                origin_refs=[self.event_ref(other.event_id, namespace="example.test.cognition.other")],
                privacy_class="private",
            )
        with self.assertRaises(ScopeViolation):
            self.create(access=self.access(permissions=frozenset({"question.read"})))

    def test_related_question_scope_mismatch_is_rejected(self):
        inside = self.create(question="inside", scope={"project_id": "p1"})
        outside = self.create(question="outside", scope={"project_id": "p2"})
        ev = self.event()
        with self.assertRaises(QuestionInvalid):
            self.service().create(
                self.access(),
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                question="links to outside",
                origin="user_requested",
                scope={"project_id": "p1"},
                origin_refs=[self.event_ref(ev.event_id)],
                privacy_class="private",
                related_question_refs=[outside.view.ref.to_dict()],
            )

    def test_question_privacy_must_be_authorized(self):
        ev = self.event(privacy="restricted")
        with self.assertRaises(QuestionInvalid):
            self.service().create(
                self.access(privacy=frozenset({"private"})),
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                question="restricted question",
                origin="user_requested",
                scope={"project_id": "p1"},
                origin_refs=[self.event_ref(ev.event_id)],
                privacy_class="restricted",
            )

    def test_request_id_digest_covers_full_create_payload(self):
        request_id = str(uuid.uuid4())
        first_event = self.event()
        first = self.service().create(
            self.access(),
            context_id=self.snapshot(),
            request_id=request_id,
            question="digest question",
            origin="user_requested",
            scope={"project_id": "p1"},
            origin_refs=[self.event_ref(first_event.event_id)],
            privacy_class="private",
            priority="normal",
        )
        second_event = self.event()
        with self.assertRaises(CommandConflict):
            self.service().create(
                self.access(),
                context_id=self.snapshot(),
                request_id=request_id,
                question="digest question",
                origin="user_requested",
                scope={"project_id": "p1"},
                origin_refs=[self.event_ref(second_event.event_id)],
                privacy_class="private",
                priority="high",
            )
        with self.assertRaises(CommandConflict):
            self.service().create(
                self.access(),
                context_id=self.snapshot(),
                request_id=request_id,
                question="different question text",
                origin="user_requested",
                scope={"project_id": "p1"},
                origin_refs=[self.event_ref(first_event.event_id)],
                privacy_class="private",
                priority="normal",
            )
        current = self.store.get_current(NS, first.view.ref.record_id)
        self.assertEqual(first.view.ref.record_version, current.record_version)

    def test_request_id_digest_covers_transition_fields(self):
        result = self.create()
        request_id = str(uuid.uuid4())
        waiting = self.service().transition(
            self.access(),
            context_id=self.snapshot(),
            request_id=request_id,
            ref=result.view.ref,
            expected_version=1,
            action="wait",
            waiting_for="logs",
        )
        with self.assertRaises(CommandConflict):
            self.service().transition(
                self.access(),
                context_id=self.snapshot(),
                request_id=request_id,
                ref=waiting.ref,
                expected_version=2,
                action="close",
                reason="different action",
            )

    def test_transition_receipt_recovery_after_current_advances(self):
        result = self.create()
        context = self.snapshot()
        request_id = str(uuid.uuid4())
        waiting = self.service().transition(
            self.access(),
            context_id=context,
            request_id=request_id,
            ref=result.view.ref,
            expected_version=1,
            action="wait",
            waiting_for="logs",
        )
        replay = self.service().transition(
            self.access(),
            context_id=context,
            request_id=request_id,
            ref=result.view.ref,
            expected_version=1,
            action="wait",
            waiting_for="logs",
        )
        self.assertEqual(waiting.ref, replay.ref)
        self.assertEqual(2, self.store.get_current(NS, result.view.ref.record_id).record_version)

    def test_evaluate_receipt_recovery_after_current_advances(self):
        result = self.create()
        ev = self.event()
        context = self.snapshot()
        request_id = str(uuid.uuid4())
        first = self.service().evaluate(
            self.access(),
            context_id=context,
            request_id=request_id,
            ref=result.view.ref,
            expected_version=1,
            evaluation_result="no_change",
            reason="checked",
            evaluated_source_refs=[self.event_ref(ev.event_id)],
        )
        replay = self.service().evaluate(
            self.access(),
            context_id=context,
            request_id=request_id,
            ref=result.view.ref,
            expected_version=1,
            evaluation_result="no_change",
            reason="checked",
            evaluated_source_refs=[self.event_ref(ev.event_id)],
        )
        self.assertEqual(first.new_version, replay.new_version)
        self.assertEqual(first.evaluated_at, replay.evaluated_at)
        self.assertEqual("no_change", replay.evaluation_result)

    def test_consume_receipt_recovery_after_current_advances(self):
        ev = self.event()
        result = self.create(
            trigger_spec={
                "source_refs": [self.event_ref(ev.event_id)],
                "topic_keys": [],
            }
        )
        context = self.snapshot()
        request_id = str(uuid.uuid4())
        first = self.service().consume_trigger(
            self.access(),
            context_id=context,
            request_id=request_id,
            ref=result.view.ref,
            expected_version=1,
            trigger_ref=self.event_ref(ev.event_id),
        )
        replay = self.service().consume_trigger(
            self.access(),
            context_id=context,
            request_id=request_id,
            ref=result.view.ref,
            expected_version=1,
            trigger_ref=self.event_ref(ev.event_id),
        )
        self.assertEqual(first.ref, replay.ref)
        self.assertEqual(2, self.store.get_current(NS, result.view.ref.record_id).record_version)

    def test_evaluate_digest_conflict(self):
        result = self.create()
        ev = self.event()
        request_id = str(uuid.uuid4())
        self.service().evaluate(
            self.access(),
            context_id=self.snapshot(),
            request_id=request_id,
            ref=result.view.ref,
            expected_version=1,
            evaluation_result="no_change",
            reason="first",
            evaluated_source_refs=[self.event_ref(ev.event_id)],
        )
        with self.assertRaises(CommandConflict):
            self.service().evaluate(
                self.access(),
                context_id=self.snapshot(),
                request_id=request_id,
                ref=result.view.ref,
                expected_version=1,
                evaluation_result="no_change",
                reason="different",
                evaluated_source_refs=[self.event_ref(ev.event_id)],
            )

    def test_consume_digest_conflict(self):
        first_trigger = self.event()
        second_trigger = self.event()
        result = self.create(
            trigger_spec={
                "source_refs": [self.event_ref(first_trigger.event_id)],
                "topic_keys": [],
            }
        )
        request_id = str(uuid.uuid4())
        self.service().consume_trigger(
            self.access(),
            context_id=self.snapshot(),
            request_id=request_id,
            ref=result.view.ref,
            expected_version=1,
            trigger_ref=self.event_ref(first_trigger.event_id),
        )
        with self.assertRaises(CommandConflict):
            self.service().consume_trigger(
                self.access(),
                context_id=self.snapshot(),
                request_id=request_id,
                ref=result.view.ref,
                expected_version=1,
                trigger_ref=self.event_ref(second_trigger.event_id),
            )

    def test_invalid_active_question_blocks_recreation(self):
        ev = self.event()
        first = self.service().create(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            question="source will be deleted",
            origin="user_requested",
            scope={"project_id": "p1"},
            origin_refs=[self.event_ref(ev.event_id)],
            privacy_class="private",
        )
        self.store.request_deletion(
            target_kind="event",
            namespace=NS,
            target_id=ev.event_id,
            authorization_ref="authorization:test",
        )
        replacement = self.event()
        with self.assertRaises(QuestionUnavailable):
            self.service().create(
                self.access(),
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                question="source will be deleted",
                origin="user_requested",
                scope={"project_id": "p1"},
                origin_refs=[self.event_ref(replacement.event_id)],
                privacy_class="private",
            )
        self.assertEqual(
            1, len(self.store.current_versions_of_kind(NS, "cognition.open_question"))
        )
        self.assertEqual(first.view.ref.record_id, self.store.current_versions_of_kind(
            NS, "cognition.open_question"
        )[0].record_id)

    def test_dedupe_does_not_cross_privacy_classes(self):
        private = self.event(privacy="private")
        restricted = self.event(privacy="restricted")
        access = self.access(privacy=frozenset({"private", "restricted"}))
        first = self.service().create(
            access,
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            question="same words",
            origin="user_requested",
            scope={"project_id": "p1"},
            origin_refs=[self.event_ref(private.event_id)],
            privacy_class="private",
        )
        with self.assertRaises(QuestionInvalid):
            self.service().create(
                access,
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                question="same words",
                origin="user_requested",
                scope={"project_id": "p1"},
                origin_refs=[self.event_ref(restricted.event_id)],
                privacy_class="restricted",
            )
        self.assertEqual(first.view.ref, self.service().get(
            access, ref=first.view.ref, scope={"project_id": "p1"}
        ).ref)
        self.assertEqual(
            1, len(self.store.current_versions_of_kind(NS, "cognition.open_question"))
        )

    def test_dedupe_privacy_mismatch_on_snapshot_conflict(self):
        a = self.store
        b = self.open_second_store()
        private = self.event(privacy="private")
        restricted = self.event(privacy="restricted")
        access = self.access(privacy=frozenset({"private", "restricted"}))
        snapshot_b = b.capture_snapshot(NS)
        first = QuestionService(a).create(
            access,
            context_id=a.capture_snapshot(NS).context_id,
            request_id=str(uuid.uuid4()),
            question="snapshot privacy",
            origin="user_requested",
            scope={"project_id": "p1"},
            origin_refs=[self.event_ref(private.event_id)],
            privacy_class="private",
        )
        with self.assertRaises(QuestionInvalid):
            QuestionService(b).create(
                access,
                context_id=snapshot_b.context_id,
                request_id=str(uuid.uuid4()),
                question="snapshot privacy",
                origin="user_requested",
                scope={"project_id": "p1"},
                origin_refs=[self.event_ref(restricted.event_id)],
                privacy_class="restricted",
            )
        self.assertEqual(first.view.ref, self.service().get(
            access, ref=first.view.ref, scope={"project_id": "p1"}
        ).ref)

    def test_resolution_and_reopen_reasons_are_persisted(self):
        result = self.create()
        ev = self.event()
        resolved = self.service().transition(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=result.view.ref,
            expected_version=1,
            action="resolve",
            resolution_refs=[self.event_ref(ev.event_id)],
            reason="three independent checks agree",
        )
        self.assertEqual("three independent checks agree", resolved.resolution_reason)
        reopen_event = self.event()
        reopened = self.service().transition(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=resolved.ref,
            expected_version=2,
            action="reopen",
            new_evidence_refs=[self.event_ref(reopen_event.event_id)],
            reason="new contradictory measurement",
        )
        self.assertIsNone(reopened.resolution_reason)
        self.assertEqual("new contradictory measurement", reopened.reopen_reason)

    def test_attention_does_not_truncate_before_sort(self):
        ev = self.event()
        ref = self.event_ref(ev.event_id)
        for index in range(50):
            self.service().create(
                self.access(),
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                question=f"low priority {index}",
                origin="user_requested",
                scope={"project_id": "p1"},
                origin_refs=[ref],
                privacy_class="private",
                priority="low",
                review_after="2026-10-25T00:00:00Z",
            )
        high = self.service().create(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            question="high priority last",
            origin="user_requested",
            scope={"project_id": "p1"},
            origin_refs=[ref],
            privacy_class="private",
            priority="high",
            review_after="2026-10-25T00:00:00Z",
        )
        attention = self.service().attention(
            self.access(), request=self.attention_request(limit=1)
        )
        self.assertEqual(1, len(attention.items))
        self.assertEqual(high.view.ref, attention.items[0].ref)

    def test_invalid_request_id_and_no_migration(self):
        ev = self.event()
        with self.assertRaises(InvalidInput):
            self.service().create(
                self.access(),
                context_id=self.snapshot(),
                request_id="not-a-uuid",
                question="invalid request",
                origin="user_requested",
                scope={"project_id": "p1"},
                origin_refs=[self.event_ref(ev.event_id)],
                privacy_class="private",
            )
        row = self.store.connection.execute(
            "SELECT MAX(migration_id) FROM schema_migrations"
        ).fetchone()
        # The pin tracks the schema head; what the test guards is that *this*
        # operation adds no migration, not that the head stays at 15.
        self.assertEqual(17, row[0])

    def test_evaluate_rejects_deleted_evaluated_source(self):
        result = self.create()
        ev = self.event()
        self.store.request_deletion(
            target_kind="event",
            namespace=NS,
            target_id=ev.event_id,
            authorization_ref="authorization:test",
        )
        with self.assertRaises(SourceRedacted):
            self.service().evaluate(
                self.access(),
                context_id=self.snapshot(),
                request_id=str(uuid.uuid4()),
                ref=result.view.ref,
                expected_version=1,
                evaluation_result="no_change",
                reason="checked",
                evaluated_source_refs=[self.event_ref(ev.event_id)],
            )

    # --------------------------------------------------------------- helper

    def attention_request(self, **kwargs):
        from quintessence_pcs.domains.cognition.state_view import AttentionRequest

        return AttentionRequest(scope={"project_id": "p1"}, **kwargs)
