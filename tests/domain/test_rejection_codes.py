"""S13: directive-binding failures must name a frozen reason code.

Before this change ``_binding_problem`` returned an English sentence that the
caller threw away, so three very different refusals all landed in the decision
row as ``not_authorized``.  J07 has to answer "why wasn't this remembered?", so
each check now reports the code v0.5 §9.6 already froze.
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
    DirectiveBinding,
)
from quintessence_pcs.collaboration.policy import CommitAuthority  # noqa: E402
from quintessence_pcs.store.governance import GovernanceStore  # noqa: E402


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


class RejectionCodeTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: "2026-10-17T12:00:00Z"
        )
        self.governance = GovernanceStore(self.store)

    # ------------------------------------------------------------- helpers

    def proposal(self, *, claim: str = "synthetic preference", scope=None) -> dict:
        body = self.belief_body(
            claim=claim, claim_type="user_preference", strength="limited"
        )
        return {
            "summary": "synthetic preference",
            "changes": [
                {
                    "op": "propose_belief",
                    "body": body,
                    "scope": {"scene": "test.scene"} if scope is None else scope,
                }
            ],
        }

    def propose(self, bundle: dict):
        trigger = self.ingest().event_id
        context = self.build_context(trigger)
        return self.changes.propose(
            access(),
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            bundle=bundle,
        )

    def binding(
        self,
        statement_event_id: str,
        *,
        scope=None,
        claim_types=frozenset({"user_preference"}),
        namespace: str = NS,
        kind: str = "state_preference",
    ) -> DirectiveBinding:
        return DirectiveBinding(
            namespace=namespace,
            host_instance="synthetic-host",
            statement_ref=self.event_ref(statement_event_id),
            statement_digest="sha256:" + str(self.store.event_payload_digest(NS, statement_event_id)),
            directive_kind=kind,
            target={"project_id": "synthetic"},
            allowed_claim_types=claim_types,
            decision_scope={"scene": "test.scene"} if scope is None else scope,
            bound_at="2026-10-17T12:00:00Z",
            binding_policy_id="policy:cognition.directive.v1",
            binding_policy_version="1",
            access_ref="access:synthetic",
        )

    def decision_codes(self, candidate_id: str) -> list[str]:
        return [
            row[0]
            for row in self.store.connection.execute(
                "SELECT reason_code FROM cognition_review_decisions "
                "WHERE candidate_id = ? ORDER BY decision_seq",
                (candidate_id,),
            )
        ]

    def refuse(self, bundle: dict, binding: DirectiveBinding):
        result = self.propose(bundle)
        before = self.count_rows("record_versions")
        refused = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(directive=binding),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual(before, self.count_rows("record_versions"))
        return refused, result.candidate_id

    # ------------------------------------------------------------- RC-01/02

    def test_rc_01_a_non_user_report_statement_is_named(self) -> None:
        inference = self.ingest(source_kind="model_inference", event_type="UserStatement")
        refused, candidate_id = self.refuse(
            self.proposal(), self.binding(inference.event_id)
        )
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        self.assertEqual("directive_source_not_user_report", refused.reason_code)
        # The audit row has to carry the same code: J07 reads the row, not the
        # in-memory result.
        self.assertEqual(
            ["directive_source_not_user_report"], self.decision_codes(candidate_id)
        )

    def test_rc_02_a_deleted_statement_is_named(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        self.delete_event(statement.event_id)
        refused, _ = self.refuse(
            self.proposal(), self.binding(statement.event_id)
        )
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        self.assertEqual("directive_source_not_user_report", refused.reason_code)

    # ------------------------------------------------------------- RC-03/04

    def test_rc_03_a_scope_outside_the_binding_is_named(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        refused, _ = self.refuse(
            self.proposal(), self.binding(statement.event_id, scope={"scene": "other"})
        )
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        self.assertEqual("directive_scope_mismatch", refused.reason_code)

    def test_rc_04_a_claim_type_outside_the_binding_is_named(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        refused, _ = self.refuse(
            self.proposal(),
            self.binding(
                statement.event_id,
                kind="revoke_previous",
                claim_types=frozenset({"project_decision"}),
            ),
        )
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        self.assertEqual("directive_claim_type_forbidden", refused.reason_code)

    # ------------------------------------------------------------- RC-05

    def test_rc_05_a_binding_above_e1_reports_the_impact_ceiling(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        bundle = {
            "summary": "three targets raise the impact to E2",
            "changes": [
                {
                    "op": "propose_belief",
                    "body": self.belief_body(
                        claim=f"synthetic preference {index}",
                        claim_type="user_preference",
                        strength="limited",
                    ),
                    "scope": {"scene": "test.scene"},
                }
                for index in range(3)
            ],
        }
        result = self.propose(bundle)
        before = self.count_rows("record_versions")
        refused = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(directive=self.binding(statement.event_id)),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("impact_requires_human", refused.reason_code)
        self.assertEqual("AWAITING_HUMAN", refused.state)
        self.assertEqual(before, self.count_rows("record_versions"))

    # ------------------------------------------------------------- RC-06

    def test_rc_06_each_refusal_has_its_own_code(self) -> None:
        inference = self.ingest(source_kind="model_inference", event_type="UserStatement")
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")

        codes = set()
        for bundle, binding in (
            (self.proposal(), self.binding(inference.event_id)),
            (self.proposal(), self.binding(statement.event_id, scope={"scene": "other"})),
            (
                self.proposal(),
                self.binding(
                    statement.event_id,
                    kind="revoke_previous",
                    claim_types=frozenset({"project_decision"}),
                ),
            ),
            (self.proposal(), self.binding(statement.event_id, namespace="other-ns")),
        ):
            refused, _ = self.refuse(bundle, binding)
            self.assertIsNotNone(refused.reason_code)
            codes.add(refused.reason_code)
        self.assertEqual(4, len(codes))

    # ------------------------------------------------------------- RC-07

    def test_rc_07_an_unclassifiable_refusal_falls_back_without_swallowing(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        refused, _ = self.refuse(
            self.proposal(),
            self.binding(statement.event_id, namespace="other-ns"),
        )
        # The fallback is the generic code ...
        self.assertEqual("not_authorized", refused.reason_code)
        # ... and it is *not* used for the checks that do have a specific code.
        specific, _ = self.refuse(
            self.proposal(), self.binding(statement.event_id, scope={"scene": "other"})
        )
        self.assertNotEqual("not_authorized", specific.reason_code)
        self.assertEqual("directive_scope_mismatch", specific.reason_code)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
