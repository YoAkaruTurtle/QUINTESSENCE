"""UI-F02: the preference projection keeps confirmed/tentative/inactive honest.

The settings view may only show a preference as *confirmed* when the store would
actually hand it to the expression layer: a supported ``user_preference`` Belief
with a live source.  Everything else is reported with its real state.  These
tests build every record through the public services; no fixture writes a
database row directly.
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
from quintessence_pcs.domains.persona.expression_preferences import (  # noqa: E402
    ExpressionPreferenceService,
)
from quintessence_pcs.domains.persona.preference_view import (  # noqa: E402
    STATE_EVIDENCE_DELETED,
    list_preference_groups,
)
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

CONVERSATION = "33333333-3333-4333-8333-333333333333"
OTHER_CONVERSATION = "44444444-4444-4444-8444-444444444444"
HOST = "synthetic-host"
CHANGE_CLOCK = "2026-10-17T12:00:00Z"


class PreferenceViewTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: CHANGE_CLOCK
        )

    def experience(self) -> ExperienceAccess:
        return ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref="access:synthetic",
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
        )

    def change_access(self) -> ChangeAccess:
        return ChangeAccess(
            namespace=NS,
            host_instance=HOST,
            operator="operator-a",
            access_ref="access:synthetic",
            permissions=frozenset(
                {"change.propose", "change.review", "change.commit"}
            ),
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"cognition.change"}),
        )

    def register(self, text: str):
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body=text,
            privacy_class="private",
            retention="standard",
            content_version=1,
        )
        return row, {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": 1,
        }

    def context_for(self, ref: dict):
        return self.contexts.build_consult_context(
            namespace=NS,
            access=self.experience(),
            input_refs=[ref],
            adapter_kind=None,
        )

    def preference_body(
        self,
        ref: dict,
        *,
        dimension: str = "length",
        value: str = "concise",
        with_evidence: bool = True,
    ) -> dict:
        evidence = (
            [
                {
                    "ref": dict(ref),
                    "relation": "supports",
                    "origin_group_id": (
                        f"collab_input:{ref['input_id']}"
                        if ref.get("ref_type") == "collab_input"
                        else f"event:{ref['event_id']}"
                    ),
                    "applicability_reason": "用户在本轮表达了该偏好",
                }
            ]
            if with_evidence
            else []
        )
        return {
            "claim": f"用户在聊天中希望回答保持{dimension}={value}。",
            "claim_type": "user_preference",
            "evidence_links": evidence,
            "strength": "limited",
            "strength_reason": "用户在本轮明确表达。",
            "inference_basis": "direct_report",
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": f"expression_preference:{dimension}={value}",
        }

    def confirmed(
        self,
        text: str = "以后回答简短一点。",
        *,
        dimension: str = "length",
        value: str = "concise",
        with_evidence: bool = True,
        ref: dict | None = None,
        statement_digest: str | None = None,
        context_input_ref: dict | None = None,
    ) -> dict:
        if ref is None:
            row, ref = self.register(text)
            statement_digest = row.content_digest
        if statement_digest is None:
            raise AssertionError("a confirmed preference needs a statement digest")
        context = self.context_for(context_input_ref or ref)
        body = self.preference_body(
            ref, dimension=dimension, value=value, with_evidence=with_evidence
        )
        candidate = self.changes.propose(
            self.change_access(),
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            bundle={
                "summary": "synthetic expression preference",
                "changes": [
                    {
                        "op": "propose_belief",
                        "body": body,
                        "scope": {"scene": "test.scene"},
                    }
                ],
            },
        )
        binding = DirectiveBinding(
            namespace=NS,
            host_instance=HOST,
            statement_ref=ref,
            statement_digest=statement_digest,
            directive_kind="state_preference",
            target={"dimension": dimension, "value": value},
            allowed_claim_types=frozenset({"user_preference"}),
            decision_scope={"scene": "test.scene"},
            bound_at=CHANGE_CLOCK,
            binding_policy_id="policy:expression.directive.v1",
            binding_policy_version="1",
            access_ref="access:synthetic",
        )
        committed = self.changes.commit(
            self.change_access(),
            candidate_id=candidate.candidate_id,
            authority=CommitAuthority(directive=binding),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", committed.state)
        return committed.record_refs[-1]

    def tentative(self, text: str = "今天先简短一点吧。", *, status: str = "tentative"):
        """Write a *legacy* tentative row (the host no longer creates one)."""

        _row, ref = self.register(text)
        self.store.append_record_version(
            namespace=NS,
            record_id=ExpressionPreferenceService(self.store).record_id(
                NS, CONVERSATION, "length"
            ),
            record_version=1,
            kind="expression_preference",
            body={
                "dimension": "length",
                "value": "concise",
                "quote": text,
                "source_ref": dict(ref),
                "applies_to": "conversation",
                "state": "tentative",
            },
            status=status,
            schema_version="1",
            privacy_class="private",
            recorded_at="2026-11-05T00:00:00Z",
            scope={"session_id": CONVERSATION},
        )

    def groups(self, conversation: str | None = CONVERSATION):
        service = ExpressionPreferenceService(self.store)
        return list_preference_groups(
            self.store,
            service,
            namespace=NS,
            conversation_id=conversation,
            privacy_classes=frozenset({"private"}),
        )

    # ------------------------------------------------------------- confirmed

    def test_supported_live_preference_is_confirmed_without_user_text(self) -> None:
        record = self.confirmed()
        groups = self.groups()
        self.assertEqual(1, len(groups.confirmed))
        item = groups.confirmed[0]
        self.assertEqual(("length", "concise"), (item["dimension"], item["value"]))
        self.assertEqual("简短", item["label"])
        self.assertNotIn("quote", item)
        self.assertNotIn("strength_reason", item)
        self.assertEqual("user_statement", item["source"]["kind"])
        self.assertEqual({"scene": "test.scene"}, item["scope"])
        self.assertEqual(record["record_id"], item["record"]["record_id"])

    def test_deleted_evidence_moves_a_preference_out_of_confirmed(self) -> None:
        receipt = self.ingest(
            source_kind="user_report",
            event_type="UserStatement:preference",
            payload={"text": "synthetic user statement"},
        )
        _row, input_ref = self.register("synthetic user statement")
        stored = self.store.event_payload_digest(NS, receipt.event_id)
        self.confirmed(
            ref=self.event_ref(receipt.event_id),
            statement_digest="sha256:" + str(stored),
            context_input_ref=input_ref,
        )
        self.assertEqual(1, len(self.groups().confirmed))
        self.delete_event(receipt.event_id)
        groups = self.groups()
        self.assertEqual(0, len(groups.confirmed))
        self.assertEqual(1, len(groups.inactive))
        self.assertEqual(STATE_EVIDENCE_DELETED, groups.inactive[0]["state"])

    # ------------------------------------------------------------- tentative

    def test_tentative_stays_in_its_conversation(self) -> None:
        self.tentative()
        mine = self.groups(CONVERSATION)
        other = self.groups(OTHER_CONVERSATION)
        self.assertEqual(1, len(mine.tentative))
        self.assertEqual("tentative", mine.tentative[0]["status"])
        # J11: the host no longer creates these rows, so the view marks them as
        # history the user can still see and revoke.
        self.assertIs(True, mine.tentative[0]["legacy"])
        self.assertEqual({"session_id": CONVERSATION}, mine.tentative[0]["scope"])
        self.assertEqual(0, len(other.tentative))
        # Without a conversation the confirmed group is still readable.
        self.assertEqual(0, len(self.groups(None).tentative))

    def test_revoke_turns_tentative_into_inactive_revoked(self) -> None:
        self.tentative()
        ExpressionPreferenceService(self.store).revoke(
            namespace=NS,
            conversation_id=CONVERSATION,
            dimension="length",
            reason="用户在设置中撤销",
        )
        groups = self.groups()
        self.assertEqual(0, len(groups.tentative))
        self.assertEqual(1, len(groups.inactive))
        item = groups.inactive[0]
        self.assertEqual(("tentative", "revoked"), (item["kind"], item["state"]))
        self.assertEqual("用户在设置中撤销", item["reason"])

    def test_superseded_tentative_is_inactive_and_names_the_new_record(self) -> None:
        record = self.confirmed()
        self.tentative()
        self.store.append_record_version(
            namespace=NS,
            record_id=ExpressionPreferenceService(self.store).record_id(
                NS, CONVERSATION, "length"
            ),
            record_version=2,
            kind="expression_preference",
            body={
                "dimension": "length",
                "value": "concise",
                "quote": "今天先简短一点吧。",
                "source_ref": self.register("今天先简短一点吧。")[1],
                "applies_to": "conversation",
                "state": "superseded",
                "superseded_by": {
                    "record_id": record["record_id"],
                    "record_version": record["record_version"],
                },
            },
            status="superseded",
            schema_version="1",
            privacy_class="private",
            recorded_at="2026-11-05T00:00:01Z",
            scope={"session_id": CONVERSATION},
        )
        groups = self.groups()
        self.assertEqual(1, len(groups.confirmed))
        self.assertEqual(0, len(groups.tentative))
        self.assertEqual(1, len(groups.inactive))
        self.assertEqual("superseded", groups.inactive[0]["state"])
        self.assertEqual(
            record["record_id"], groups.inactive[0]["superseded_by"]["record_id"]
        )

    def test_non_preference_beliefs_are_not_listed(self) -> None:
        receipt = self.ingest()
        context = self.build_context(receipt.event_id)
        self.beliefs.propose_belief(
            namespace=NS,
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            body=self.belief_body(
                claim_type="capability",
                evidence_links=[self.evidence_link(self.event_ref(receipt.event_id))],
            ),
            scope={"scene": "test.scene"},
        )
        groups = self.groups()
        self.assertEqual(0, len(groups.confirmed))
        self.assertEqual(0, len(groups.tentative))
        self.assertEqual(0, len(groups.inactive))


if __name__ == "__main__":
    unittest.main()
