"""RG05-F02: a directive binding may name the current user input.

The positive case is the current turn's collab input, which is also a source of
the candidate's own Context.  Every other shape is refused with the frozen
directive codes: another turn's input, a fabricated digest, a deleted input, a
non-user origin, or a binding the candidate never saw.
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
from quintessence_pcs.domains.cognition.beliefs import belief_evidence_is_live  # noqa: E402
from quintessence_pcs.foundation.errors import ContextStale  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402
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


class PreferenceBindingTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: "2026-10-17T12:00:00Z"
        )

    def experience_access(self) -> ExperienceAccess:
        return ExperienceAccess(
            namespace=NS,
            host_instance="synthetic-host",
            access_ref="access:synthetic",
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
        )

    def register(self, text: str = "记住：以后回答尽量简短。", origin: str = "user_statement"):
        return self.store.register_input(
            namespace=NS,
            origin_kind=origin,
            body=text,
            privacy_class="private",
            retention="standard",
            content_version=1,
        )

    def ref(self, row) -> dict:
        return {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": row.content_version,
        }

    def consult_context(self, ref: dict):
        return self.contexts.build_consult_context(
            namespace=NS,
            access=self.experience_access(),
            input_refs=[ref],
            adapter_kind=None,
        )

    def body(self, ref: dict) -> dict:
        return {
            "claim": "用户在聊天中希望回答保持简短。",
            "claim_type": "user_preference",
            "evidence_links": [
                {
                    "ref": ref,
                    "relation": "supports",
                    "origin_group_id": f"collab_input:{ref['input_id']}",
                    "applicability_reason": "用户在本轮直接陈述了该表达偏好",
                }
            ],
            "strength": "limited",
            "strength_reason": "用户在本轮明确要求：尽量简短。",
            "inference_basis": "direct_report",
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": "expression_preference:length=concise",
        }

    def propose(self, ref: dict, context_id: str | None = None):
        context_id = context_id or self.consult_context(ref).context_id
        bundle = {
            "operation": "cognition.change",
            "summary": "synthetic preference",
            "changes": [
                {"op": "propose_belief", "body": self.body(ref), "scope": {"scene": "test.scene"}}
            ],
        }
        return self.changes.propose(
            access(),
            context_id=context_id,
            request_id=str(uuid.uuid4()),
            bundle=bundle,
        )

    def binding(
        self,
        ref: dict,
        row,
        *,
        digest: str | None = None,
        kind: str = "state_preference",
        claim_types: frozenset[str] = frozenset({"user_preference"}),
    ) -> DirectiveBinding:
        return DirectiveBinding(
            namespace=NS,
            host_instance="synthetic-host",
            statement_ref=ref,
            statement_digest=row.content_digest if digest is None else digest,
            directive_kind=kind,
            target={"dimension": "length", "value": "concise"},
            allowed_claim_types=claim_types,
            decision_scope={"scene": "test.scene"},
            bound_at="2026-10-17T12:00:00Z",
            binding_policy_id="policy:expression.directive.v1",
            binding_policy_version="1",
            access_ref="access:synthetic",
            classifier_policy_id="policy:expression.directive.v1",
            classifier_policy_version="1",
        )

    def commit(self, candidate_id: str, binding: DirectiveBinding):
        return self.changes.commit(
            access(),
            candidate_id=candidate_id,
            authority=CommitAuthority(directive=binding),
            decision_id=str(uuid.uuid4()),
        )

    def test_current_input_binding_commits_as_user_directive(self) -> None:
        row = self.register()
        ref = self.ref(row)
        result = self.propose(ref)
        committed = self.commit(result.candidate_id, self.binding(ref, row))
        self.assertEqual("COMMITTED", committed.state)
        self.assertEqual("user_directive", committed.authority_kind)
        self.assertEqual("E1", committed.impact)
        current = self.store.get_current(NS, committed.record_refs[0]["record_id"])
        self.assertEqual("supported", current.status)
        self.assertEqual(
            "expression_preference:length=concise",
            current.body["review_condition"],
        )
        rows = [
            tuple(row)
            for row in self.store.connection.execute(
                "SELECT decision, authority_kind, directive_digest, evidence_refs_json "
                "FROM cognition_review_decisions WHERE candidate_id = ?",
                (result.candidate_id,),
            ).fetchall()
        ]
        self.assertEqual(1, len(rows))
        self.assertEqual("committed", rows[0][0])
        self.assertEqual("user_directive", rows[0][1])
        self.assertTrue(rows[0][2].startswith("sha256:"))
        self.assertIn(row.input_id, rows[0][3])

        # F02-AUDIT-01: the decision row itself answers which input, which
        # target, which scope and which binding/classifier rule version was used.
        decision = GovernanceStore(self.store).decisions_for_candidate(
            NS, result.candidate_id
        )[0]
        self.assertEqual({"scene": "test.scene"}, decision.scope)
        self.assertEqual(
            [{"dimension": "length", "value": "concise"}], decision.target_refs
        )
        statement_ref, audit = decision.evidence_refs
        self.assertEqual(
            {
                "ref_type": "collab_input",
                "namespace": NS,
                "input_id": row.input_id,
                "content_version": 1,
            },
            statement_ref,
        )
        self.assertEqual("directive_binding_audit", audit["kind"])
        self.assertEqual("state_preference", audit["directive_kind"])
        self.assertEqual(row.content_digest, audit["statement_digest"])
        self.assertEqual("2026-10-17T12:00:00Z", audit["bound_at"])
        self.assertEqual(
            "policy:expression.directive.v1", audit["binding_policy_id"]
        )
        self.assertEqual("1", audit["binding_policy_version"])
        self.assertEqual(
            "policy:expression.directive.v1", audit["classifier_policy_id"]
        )
        self.assertEqual("1", audit["classifier_policy_version"])
        self.assertEqual(["user_preference"], audit["allowed_claim_types"])
        self.assertEqual(
            decision.directive_digest,
            self.binding(ref, row).digest,
        )

    def test_another_input_is_refused(self) -> None:
        row = self.register()
        ref = self.ref(row)
        context = self.consult_context(ref)
        other = self.register("这次回答短一点")
        other_ref = self.ref(other)
        # The candidate's Context names only the first input.
        result = self.propose(ref, context.context_id)
        refused = self.commit(result.candidate_id, self.binding(other_ref, other))
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        self.assertEqual("directive_source_not_user_report", refused.reason_code)

    def test_forged_digest_is_refused(self) -> None:
        row = self.register()
        ref = self.ref(row)
        result = self.propose(ref)
        refused = self.commit(
            result.candidate_id,
            self.binding(ref, row, digest="sha256:" + "0" * 64),
        )
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        self.assertEqual("directive_digest_mismatch", refused.reason_code)

    def test_deleted_candidate_input_cascades_to_the_candidate(self) -> None:
        row = self.register()
        ref = self.ref(row)
        result = self.propose(ref)
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=row.input_id,
            ref_version=1,
            authorization_ref="auth:test-delete",
        )
        # The candidate Context named the deleted input, so the existing source
        # closure already makes the candidate uncommittable.  The refusal is
        # stronger than the binding check but still refuses.
        with self.assertRaises(ContextStale) as raised:
            self.commit(result.candidate_id, self.binding(ref, row))
        self.assertIn("deletion barrier", str(raised.exception))

    def test_deleted_statement_is_refused_by_the_binding_check(self) -> None:
        row = self.register()
        ref = self.ref(row)
        result = self.propose(ref)
        other = self.register("这次回答短一点")
        other_ref = self.ref(other)
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=other.input_id,
            ref_version=1,
            authorization_ref="auth:test-delete",
        )
        refused = self.commit(result.candidate_id, self.binding(other_ref, other))
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        self.assertEqual("directive_source_not_user_report", refused.reason_code)

    def test_non_user_origin_is_refused(self) -> None:
        row = self.register(origin="host_supplied")
        ref = self.ref(row)
        result = self.propose(ref)
        refused = self.commit(result.candidate_id, self.binding(ref, row))
        self.assertEqual("NOT_AUTHORIZED", refused.state)
        self.assertEqual("directive_source_not_user_report", refused.reason_code)

    def test_belief_evidence_can_be_a_user_input(self) -> None:
        row = self.register()
        ref = self.ref(row)
        # A direct BeliefService write uses a review Context; the evidence link
        # itself is what this test proves can be a user input.
        context = self.build_context()
        version = self.beliefs.propose_belief(
            namespace=NS,
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            body=self.body(ref),
            scope={"scene": "test.scene"},
        )
        self.assertEqual("supported", version.status)
        self.assertTrue(
            belief_evidence_is_live(self.store, namespace=NS, body=version.body)
        )
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=row.input_id,
            ref_version=1,
            authorization_ref="auth:test-delete",
        )
        self.assertFalse(
            belief_evidence_is_live(self.store, namespace=NS, body=version.body)
        )


if __name__ == "__main__":
    unittest.main()
