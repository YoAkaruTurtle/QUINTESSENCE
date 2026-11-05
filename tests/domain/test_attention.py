"""J05 Attention: bounded, derived, scope/pause/source matching."""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains.cognition.questions import (  # noqa: E402
    QuestionAccess,
    QuestionInvalid,
    QuestionService,
)
from quintessence_pcs.domains.cognition.state_view import AttentionRequest  # noqa: E402

PERMISSIONS = frozenset(
    {
        "question.read",
        "question.create",
        "question.transition",
        "question.evaluate",
        "question.attention",
    }
)


class AttentionTests(CognitionTestCase):
    def access(self) -> QuestionAccess:
        return QuestionAccess(
            NS,
            "host:j05",
            "authorization:j05",
            frozenset({"private"}),
            PERMISSIONS,
            frozenset({"cognition.question"}),
        )

    def event(self, *, text: str = "synthetic", event_type: str = "TestTrigger"):
        return self.store.ingest_event(
            namespace=NS,
            source_kind="host_observation",
            source_instance="j05-attention",
            source_event_id=f"j05-att-{uuid.uuid4()}",
            event_type=event_type,
            payload={"text": text},
            privacy_class="private",
        )

    def snapshot(self) -> str:
        return self.store.capture_snapshot(NS).context_id

    def create(
        self,
        question: str,
        *,
        scope: dict | None = None,
        priority: str = "normal",
        review_after: str | None = None,
        trigger_spec: dict | None = None,
    ):
        ev = self.event()
        return QuestionService(self.store).create(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            question=question,
            origin="user_requested",
            scope={"project_id": "p1"} if scope is None else scope,
            origin_refs=[
                {"ref_type": "event", "namespace": NS, "event_id": ev.event_id}
            ],
            privacy_class="private",
            priority=priority,
            review_after=review_after,
            trigger_spec=trigger_spec,
        )

    def attention(self, **kwargs):
        return QuestionService(self.store).attention(
            self.access(), request=AttentionRequest(**kwargs)
        )

    def test_due_is_bounded_sorted_and_truncated(self):
        self.create("high", priority="high", review_after="2026-10-25T00:00:00Z")
        self.create("normal", priority="normal", review_after="2026-10-25T00:00:00Z")
        self.create("low", priority="low", review_after="2026-10-25T00:00:00Z")
        view = self.attention(scope={"project_id": "p1"}, limit=2)
        self.assertTrue(view.truncated)
        self.assertEqual(2, len(view.items))
        self.assertEqual("high", view.items[0].priority)
        self.assertEqual("normal", view.items[1].priority)
        self.assertEqual(("review_due",), view.items[0].reason_codes)

    def test_scope_isolation(self):
        self.create("inside", scope={"project_id": "p1"},
                    review_after="2026-10-25T00:00:00Z")
        self.create("outside", scope={"project_id": "p2"},
                    review_after="2026-10-25T00:00:00Z")
        view = self.attention(scope={"project_id": "p1"})
        self.assertEqual(1, len(view.items))
        self.assertEqual("inside", self.store.get_current(NS, view.items[0].ref.record_id).body["question"])

    def test_source_match_is_exact_and_consumed_once(self):
        trigger = self.event(text="the trigger")
        self.create(
            "source matched",
            trigger_spec={
                "source_refs": [
                    {"ref_type": "event", "namespace": NS, "event_id": trigger.event_id}
                ],
                "topic_keys": [],
            },
        )
        ref = {"ref_type": "event", "namespace": NS, "event_id": trigger.event_id}
        matched = self.attention(scope={"project_id": "p1"}, new_source_refs=[ref])
        self.assertEqual(1, len(matched.items))
        self.assertEqual(("source_match",), matched.items[0].reason_codes)
        svc = QuestionService(self.store)
        view = svc.consume_trigger(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=matched.items[0].ref,
            expected_version=1,
            trigger_ref=ref,
        )
        after = self.attention(scope={"project_id": "p1"}, new_source_refs=[ref])
        self.assertEqual(0, len(after.items))
        self.assertEqual(2, view.ref.record_version)

    def test_unrelated_source_does_not_wake(self):
        self.create("not due")
        unrelated = self.event(text="unrelated")
        view = self.attention(
            scope={"project_id": "p1"},
            new_source_refs=[
                {"ref_type": "event", "namespace": NS, "event_id": unrelated.event_id}
            ],
        )
        self.assertEqual(0, len(view.items))

    def test_pause_default_excluded_and_visible_when_asked(self):
        result = self.create("paused")
        svc = QuestionService(self.store)
        waiting = svc.transition(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=result.view.ref,
            expected_version=1,
            action="wait",
            waiting_for="logs",
            review_after="2026-10-25T00:00:00Z",
        )
        paused = svc.transition(
            self.access(),
            context_id=self.snapshot(),
            request_id=str(uuid.uuid4()),
            ref=waiting.ref,
            expected_version=2,
            action="pause",
            pause_reason="user pause",
            paused_by="user",
        )
        self.assertTrue(paused.paused)
        self.assertEqual(0, len(self.attention(scope={"project_id": "p1"}).items))
        visible = self.attention(scope={"project_id": "p1"}, include_paused=True)
        self.assertEqual(1, len(visible.items))
        self.assertFalse(visible.items[0].actionable)

    def test_limit_and_request_validation(self):
        with self.assertRaises(QuestionInvalid):
            AttentionRequest(limit=0)
        with self.assertRaises(QuestionInvalid):
            AttentionRequest(limit=51)
        many = [
            {"ref_type": "event", "namespace": NS, "event_id": str(uuid.uuid4())}
            for _ in range(33)
        ]
        with self.assertRaises(QuestionInvalid):
            AttentionRequest(new_source_refs=many)

    def test_no_attention_write(self):
        result = self.create("due", review_after="2026-10-25T00:00:00Z")
        before = self.store.get_current(NS, result.view.ref.record_id)
        view = self.attention(scope={"project_id": "p1"})
        self.assertEqual(1, len(view.items))
        after = self.store.get_current(NS, result.view.ref.record_id)
        self.assertEqual(before.record_version, after.record_version)
        self.assertEqual(before.body["last_evaluated_at"], after.body["last_evaluated_at"])
