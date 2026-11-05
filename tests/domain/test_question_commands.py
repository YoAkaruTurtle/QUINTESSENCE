"""S10c: the eight single-operation Question commands.

One Tx B command each, through `CognitionChangeService.question`.  No model
call, no network, synthetic data only.
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
    ChangeAccess,
    CognitionChangeService,
)
from quintessence_pcs.domains.cognition.errors import InvalidTransition  # noqa: E402
from quintessence_pcs.store.governance import (  # noqa: E402
    GovernanceAccess,
    GovernanceStore,
)

NOW = "2026-10-17T12:00:00Z"


def access() -> ChangeAccess:
    return ChangeAccess(
        namespace=NS,
        host_instance="synthetic-host",
        operator="operator-a",
        access_ref="access:synthetic",
        permissions=frozenset(
            {"change.propose", "change.review", "change.commit", "change.result"}
        ),
        privacy_classes=frozenset({"private"}),
        purposes=frozenset({"cognition.change"}),
    )


def governance_access() -> GovernanceAccess:
    return GovernanceAccess(
        namespace=NS,
        host_instance="synthetic-host",
        operator="operator-a",
        access_ref="access:governance",
        permissions=frozenset({"governance.admin", "governance.read"}),
    )


class QuestionCommandTestCase(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: NOW
        )
        self.governance = GovernanceStore(self.store)

    # ------------------------------------------------------------- helpers

    def authorize(self, *operations: str, max_impact: str = "E1") -> str:
        authorization_id = str(uuid.uuid4())
        self.governance.grant_commit_authorization(
            governance_access(),
            authorization_id=authorization_id,
            host_instance="synthetic-host",
            access_ref="access:synthetic",
            operations=operations,
            claim_types=("general_hypothesis",),
            scope={"scene": "test.scene"},
            max_impact=max_impact,
            max_changes_per_bundle=1,
            max_targets_per_bundle=1,
            policy_id="policy:cognition.auto.v2",
            policy_version="1",
            authorization_source="host_config",
            issued_at="2026-10-17T00:00:00Z",
        )
        return authorization_id

    def create(
        self,
        statement: str = "what is the synthetic mechanism?",
        *,
        origin: str = "system_proposed",
    ) -> object:
        origin_refs = ()
        if origin == "user_requested":
            # The originating statement must exist before the Context is
            # captured, or the command would move the watermark it cites.
            origin_event = self.ingest(source_kind="user_report", event_type="UserStatement")
            origin_refs = (self.event_ref(origin_event.event_id),)
        context = self.build_context()
        return self.changes.question(
            access(),
            operation="question.create",
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            question=statement,
            origin=origin,
            origin_refs=origin_refs,
            scope={"scene": "test.scene"},
            privacy_class="private",
            auto_authorization_id=self.authorize("question.create"),
        )

    def decision_count(self) -> int:
        return int(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM cognition_review_decisions"
            ).fetchone()[0]
        )

    # ------------------------------------------------------------- EA-04a

    def test_ea_04a_a_question_is_proposed_without_a_human(self) -> None:
        before = self.decision_count()
        result = self.create()
        self.assertEqual("COMMITTED", result.state)
        self.assertEqual("E0", result.impact)
        record_id = result.question_ref["record_id"]
        version = self.store.get_current(NS, record_id)
        self.assertEqual("proposed", version.status)
        self.assertEqual(before + 1, self.decision_count())

    # ------------------------------------------------------------- EA-04b

    def test_ea_04b_the_same_question_is_deduped_without_a_write(self) -> None:
        first = self.create()
        decisions = self.decision_count()
        second = self.create()
        self.assertEqual("PROPOSED", second.state)
        self.assertTrue(second.deduped)
        self.assertEqual(first.question_ref["record_id"], second.question_ref["record_id"])
        self.assertEqual(decisions, self.decision_count())

    # ------------------------------------------------------------- EA-04c

    def test_ea_04c_the_question_survives_a_reopen(self) -> None:
        created = self.create()
        record_id = created.question_ref["record_id"]
        reopened = type(self.store).open(self.db_path)
        self.addCleanup(reopened.close)
        version = reopened.get_current(NS, record_id)
        self.assertEqual("proposed", version.status)
        self.assertEqual("what is the synthetic mechanism?", version.body["question"])

    # ------------------------------------------------------------- EA-04d

    def test_ea_04d_wait_moves_the_question_and_records_a_decision(self) -> None:
        created = self.create(origin="user_requested")
        ref = created.question_ref
        context = self.build_context()
        decisions = self.decision_count()
        result = self.changes.question(
            access(),
            operation="question.wait",
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            ref=ref,
            expected_version=1,
            waiting_for="a synthetic observation",
            auto_authorization_id=self.authorize("question.wait"),
        )
        self.assertEqual("COMMITTED", result.state)
        self.assertEqual(decisions + 1, self.decision_count())
        current = self.store.get_current(NS, ref["record_id"])
        self.assertEqual("waiting", current.status)
        self.assertEqual(2, current.record_version)

    # ------------------------------------------------------------- EA-04e

    def test_ea_04e_pause_records_the_host_as_the_actor(self) -> None:
        created = self.create(origin="user_requested")
        ref = created.question_ref
        context = self.build_context()
        waiting = self.changes.question(
            access(),
            operation="question.wait",
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            ref=ref,
            expected_version=1,
            waiting_for="a synthetic observation",
            auto_authorization_id=self.authorize("question.wait"),
        )
        self.assertEqual("COMMITTED", waiting.state)
        context = self.build_context()
        result = self.changes.question(
            access(),
            operation="question.pause",
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            ref=dict(ref, record_version=2),
            expected_version=2,
            paused_by="host",
            pause_reason="synthetic host pause",
            auto_authorization_id=self.authorize("question.pause"),
        )
        self.assertEqual("COMMITTED", result.state)
        current = self.store.get_current(NS, ref["record_id"])
        self.assertEqual("waiting", current.status)
        self.assertTrue(current.body["paused"])
        self.assertEqual("host", current.body["paused_by"])

    # ------------------------------------------------------------- EA-04f

    def test_ea_04f_update_triggers_changes_only_the_trigger_spec(self) -> None:
        created = self.create(origin="user_requested")
        ref = created.question_ref
        context = self.build_context()
        result = self.changes.question(
            access(),
            operation="question.update_triggers",
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            ref=ref,
            expected_version=1,
            trigger_spec={"source_refs": [], "topic_keys": ["synthetic"]},
            auto_authorization_id=self.authorize("question.update_triggers"),
        )
        self.assertEqual("COMMITTED", result.state)
        current = self.store.get_current(NS, ref["record_id"])
        self.assertEqual(["synthetic"], current.body["trigger_spec"]["topic_keys"])
        self.assertEqual("open", current.status)

    # ------------------------------------------------------------- EA-04g

    def test_ea_04g_waiting_on_a_proposed_question_is_an_invalid_transition(self) -> None:
        created = self.create()
        ref = created.question_ref
        context = self.build_context()
        with self.assertRaises(Exception) as caught:
            self.changes.question(
                access(),
                operation="question.wait",
                context_id=context.context_id,
                request_id=str(uuid.uuid4()),
                ref=ref,
                expected_version=1,
                waiting_for="a synthetic observation",
                auto_authorization_id=self.authorize("question.wait"),
            )
        self.assertIn("requires open", str(caught.exception).lower())
        current = self.store.get_current(NS, ref["record_id"])
        self.assertEqual(1, current.record_version)

    # ------------------------------------------------------------- EA-04h

    def test_ea_04h_without_an_authorization_nothing_is_written(self) -> None:
        created = self.create()
        ref = created.question_ref
        context = self.build_context()
        decisions = self.decision_count()
        result = self.changes.question(
            access(),
            operation="question.wait",
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            ref=ref,
            expected_version=1,
            waiting_for="a synthetic observation",
        )
        self.assertEqual("NOT_AUTHORIZED", result.state)
        self.assertEqual(decisions, self.decision_count())
        current = self.store.get_current(NS, ref["record_id"])
        self.assertEqual(1, current.record_version)

    # ------------------------------------------------------------- E2 floor

    def test_reopen_of_a_proposed_question_is_refused(self) -> None:
        created = self.create()
        ref = created.question_ref
        context = self.build_context()
        decisions = self.decision_count()
        with self.assertRaises(InvalidTransition):
            self.changes.question(
                access(),
                operation="question.reopen",
                context_id=context.context_id,
                request_id=str(uuid.uuid4()),
                ref=ref,
                expected_version=ref["record_version"],
                reason="new synthetic evidence",
                new_evidence_refs=(self.event_ref(created.question_ref["record_id"]),),
                auto_authorization_id=None,
            )
        self.assertEqual(decisions, self.decision_count())


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
