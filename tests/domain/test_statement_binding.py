"""Closure item B: ``statement_digest`` must be checked against the payload.

A directive binding asserts "the user said *this*".  Before this change the
binding check only proved that the referenced event existed, was not deleted and
came from a ``user_report`` source -- so pointing at a different user report than
the one the digest describes passed just as well as pointing at the right one.
The digest is now compared against the stored payload digest, and the comparison
is repeated inside the write transaction so a source deleted or rewritten
between resolution and commit cannot slip through.
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
from quintessence_pcs.foundation.errors import (  # noqa: E402
    AuthorizationRequired,
    ContextStale,
    InvalidInput,
    Redacted,
)
from quintessence_pcs.store.governance import GovernanceAccess, GovernanceStore  # noqa: E402


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


def governance_access() -> GovernanceAccess:
    return GovernanceAccess(
        namespace=NS,
        host_instance="synthetic-host",
        operator="operator-a",
        access_ref="access:governance",
        permissions=frozenset({"governance.admin", "governance.read"}),
    )


class StatementBindingTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: "2026-10-17T12:00:00Z"
        )
        self.governance = GovernanceStore(self.store)

    # ------------------------------------------------------------- helpers

    def proposal(self, *, claim: str = "synthetic preference") -> dict:
        body = self.belief_body(
            claim=claim, claim_type="user_preference", strength="limited"
        )
        return {
            "summary": "synthetic preference",
            "changes": [
                {"op": "propose_belief", "body": body, "scope": {"scene": "test.scene"}}
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

    def payload_digest(self, event_id: str) -> str:
        stored = self.store.event_payload_digest(NS, event_id)
        self.assertIsNotNone(stored)
        return "sha256:" + str(stored)

    def binding(
        self,
        statement_event_id: str,
        *,
        digest: str | None = None,
        scope=None,
    ) -> DirectiveBinding:
        return DirectiveBinding(
            namespace=NS,
            host_instance="synthetic-host",
            statement_ref=self.event_ref(statement_event_id),
            statement_digest=(
                self.payload_digest(statement_event_id) if digest is None else digest
            ),
            directive_kind="state_preference",
            target={"project_id": "synthetic"},
            allowed_claim_types=frozenset({"user_preference"}),
            decision_scope={"scene": "test.scene"} if scope is None else scope,
            bound_at="2026-10-17T12:00:00Z",
            binding_policy_id="policy:cognition.directive.v1",
            binding_policy_version="1",
            access_ref="access:synthetic",
        )

    def commit(self, candidate_id: str, binding: DirectiveBinding):
        return self.changes.commit(
            access(),
            candidate_id=candidate_id,
            authority=CommitAuthority(directive=binding),
            decision_id=str(uuid.uuid4()),
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

    # ------------------------------------------------------------- SB-01

    def test_sb_01_a_matching_digest_commits(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        result = self.propose(self.proposal())
        committed = self.commit(result.candidate_id, self.binding(statement.event_id))
        self.assertEqual("COMMITTED", committed.state)
        self.assertIsNotNone(committed.receipt)

    # ------------------------------------------------------------- SB-02

    def test_sb_02_a_different_user_report_is_refused(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        other = self.ingest(
            source_kind="user_report",
            event_type="UserStatement",
            payload={"text": "a different synthetic body"},
        )
        result = self.propose(self.proposal())
        before = self.count_rows("record_versions")
        refused = self.commit(
            result.candidate_id,
            self.binding(other.event_id, digest=self.payload_digest(statement.event_id)),
        )
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        self.assertEqual("directive_digest_mismatch", refused.reason_code)
        self.assertEqual(before, self.count_rows("record_versions"))
        # The audit row names the same check the result reported.
        self.assertEqual(
            ["directive_digest_mismatch"], self.decision_codes(result.candidate_id)
        )

    # ------------------------------------------------------------- SB-03

    def test_sb_03_malformed_digests_are_refused_at_construction(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        good = self.payload_digest(statement.event_id)
        bad_values = (
            "0" * 64,
            "sha256:" + good[7:].upper(),
            "sha256:" + good[7:-1],
            "sha256:" + "g" * 64,
            "md5:" + "0" * 64,
        )
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(InvalidInput):
                    self.binding(statement.event_id, digest=value)

    # ------------------------------------------------------------- SB-04

    def test_sb_04_a_missing_or_deleted_source_is_refused(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        digest = self.payload_digest(statement.event_id)
        self.delete_event(statement.event_id)
        result = self.propose(self.proposal())
        before = self.count_rows("record_versions")
        refused = self.commit(
            result.candidate_id, self.binding(statement.event_id, digest=digest)
        )
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        self.assertEqual("directive_source_not_user_report", refused.reason_code)
        self.assertEqual(before, self.count_rows("record_versions"))

    # ------------------------------------------------------------- SB-05

    def test_sb_05_a_non_user_report_source_is_refused(self) -> None:
        inference = self.ingest(
            source_kind="model_inference", event_type="UserStatement"
        )
        result = self.propose(self.proposal())
        refused = self.commit(result.candidate_id, self.binding(inference.event_id))
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        self.assertEqual("directive_source_not_user_report", refused.reason_code)

    # ------------------------------------------------------------- SB-06

    def test_sb_06_a_source_deleted_after_resolution_blocks_the_commit(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        result = self.propose(self.proposal())
        binding = self.binding(statement.event_id)
        self.delete_event(statement.event_id)
        before = self.count_rows("record_versions")
        # The deletion barrier sees the source first (the candidate inherits it
        # through its Context) and refuses with ContextStale; the binding
        # validator is the second line of defence inside the write transaction
        # and would catch a payload change the barrier does not model.
        with self.assertRaises((AuthorizationRequired, ContextStale, Redacted)):
            self.commit(result.candidate_id, binding)
        self.assertEqual(before, self.count_rows("record_versions"))

    # ------------------------------------------------------------- SB-07

    def test_sb_07_a_refused_binding_leaves_no_record_and_no_commit_row(self) -> None:
        statement = self.ingest(source_kind="user_report", event_type="UserStatement")
        other = self.ingest(
            source_kind="user_report",
            event_type="UserStatement",
            payload={"text": "a different synthetic body"},
        )
        result = self.propose(self.proposal())
        before = self.count_rows("record_versions")
        refused = self.commit(
            result.candidate_id,
            self.binding(other.event_id, digest=self.payload_digest(statement.event_id)),
        )
        self.assertEqual(before, self.count_rows("record_versions"))
        self.assertIsNone(refused.receipt)
        decisions = [
            row[0]
            for row in self.store.connection.execute(
                "SELECT decision FROM cognition_review_decisions "
                "WHERE candidate_id = ? ORDER BY decision_seq",
                (result.candidate_id,),
            )
        ]
        self.assertNotIn("committed", decisions)
        self.assertEqual("pending", self.governance.lifecycle(NS, result.candidate_id))

    # ------------------------------------------------------------- SB-08

    def test_sb_08_the_other_authority_paths_still_commit(self) -> None:
        self.ingest(source_kind="user_report", event_type="UserStatement")
        hypothesis = {
            "summary": "one synthetic hypothesis",
            "changes": [
                {
                    "op": "propose_belief",
                    "body": self.belief_body(
                        claim="synthetic hypothesis about a mechanism",
                        claim_type="general_hypothesis",
                        strength="limited",
                    ),
                    "scope": {"scene": "test.scene"},
                }
            ],
        }
        authorization_id = str(uuid.uuid4())
        self.governance.grant_commit_authorization(
            governance_access(),
            authorization_id=authorization_id,
            host_instance="synthetic-host",
            access_ref="access:synthetic",
            operations=("cognition.change",),
            claim_types=("general_hypothesis",),
            scope={"scene": "test.scene"},
            max_impact="E1",
            max_changes_per_bundle=2,
            max_targets_per_bundle=2,
            policy_id="policy:cognition.auto.v2",
            policy_version="1",
            authorization_source="host_config",
            issued_at="2026-10-17T00:00:00Z",
        )
        result = self.propose(hypothesis)
        committed = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(auto_authorization_id=authorization_id),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", committed.state)
        self.assertEqual("auto_authorization", committed.authority_kind)


if __name__ == "__main__":
    unittest.main()
