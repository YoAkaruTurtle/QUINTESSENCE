"""J11: expression preferences across a real store restart.

RG05-F02 / RG07 kept a conversation-scoped *tentative* record and re-rendered it
after a restart.  J11 removed that mechanism; this file keeps the parts of the
F02-RESTART-01 contract that still apply:

* a **confirmed** preference (a ``user_preference`` Belief) is visible from a
  brand-new conversation after a restart;
* a **legacy** tentative row reads back (it is still a Record) but is never
  rendered and never influences the next Context;
* restart adds no record and loses none;
* the directive decision audit reads back unchanged.
"""

from __future__ import annotations

import json
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
from quintessence_pcs.domains.cognition import BeliefService, SelfViewService  # noqa: E402
from quintessence_pcs.domains.persona.expression_preferences import (  # noqa: E402
    EXPRESSION_PREFERENCE_KIND,
    EXPRESSION_PREFERENCE_SCHEMA,
    ExpressionPreferenceService,
)
from quintessence_pcs.domains.persona.profiles import PersonaProfileService  # noqa: E402
from quintessence_pcs.runtime import ContextService  # noqa: E402
from quintessence_pcs.runtime.expression_material import (  # noqa: E402
    build_expression_material,
)
from quintessence_pcs.store import CognitionStore  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402
from quintessence_pcs.store.governance import GovernanceStore  # noqa: E402

CONVERSATION = "33333333-3333-4333-8333-333333333333"
OTHER_CONVERSATION = "44444444-4444-4444-8444-444444444444"
HOST = "synthetic-host"


def _CHANGE_CLOCK() -> str:  # noqa: N802 - one frozen timestamp for review policy
    return "2026-10-17T12:00:00Z"


class ExpressionRestartTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conversation = CONVERSATION
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=_CHANGE_CLOCK
        )
        self.prefs = ExpressionPreferenceService(self.store)

    # ------------------------------------------------------------- services
    def rebuild(self) -> None:
        """Close and reopen the same database, then rebuild every service."""

        self.store.close()
        self.store = CognitionStore.open(self.db_path)
        self.addCleanup(self.store.close)
        self.profiles = PersonaProfileService(self.store)
        self.beliefs = BeliefService(self.store, clock=self.clock)
        self.self_view = SelfViewService(self.store)
        self.contexts = ContextService(
            self.store,
            self_view_service=self.self_view,
            profile_service=self.profiles,
        )
        self.prefs = ExpressionPreferenceService(self.store)
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=_CHANGE_CLOCK
        )

    def change_access(self) -> ChangeAccess:
        return ChangeAccess(
            namespace=NS,
            host_instance=HOST,
            operator="operator-a",
            access_ref="access:synthetic",
            permissions=frozenset(
                {"change.propose", "change.review", "change.commit", "change.result"}
            ),
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"cognition.change"}),
        )

    def experience(self) -> ExperienceAccess:
        return ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref="access:synthetic",
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
        )

    # ------------------------------------------------------------- helpers
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

    def material(self, ref: dict, *, conversation: str | None = None):
        return build_expression_material(
            self.store,
            namespace=NS,
            conversation_id=conversation or self.conversation,
            privacy_classes=frozenset({"private"}),
            plan=None,
            turn_input_ref=ref,
        )

    def document(self, ref: dict, *, conversation: str | None = None) -> dict:
        material = self.material(ref, conversation=conversation)
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.experience(),
            input_refs=[ref],
            adapter_kind="direct-http",
            renderer_version="collab.v2.renderer.5",
            expression_material=material,
        )
        snapshot = self.store.load_context_snapshot(context.context_id)
        return json.loads(snapshot.rendered_input)

    def legacy_tentative(self, text: str = "我其实更喜欢简短一点的回答。"):
        """Write the row the retired host path used to write."""

        row, ref = self.register(text)
        self.store.append_record_version(
            namespace=NS,
            record_id=self.prefs.record_id(NS, self.conversation, "length"),
            record_version=1,
            kind=EXPRESSION_PREFERENCE_KIND,
            body={
                "dimension": "length",
                "value": "concise",
                "quote": text,
                "source_ref": dict(ref),
                "applies_to": "conversation",
                "state": "tentative",
            },
            status="tentative",
            schema_version=EXPRESSION_PREFERENCE_SCHEMA,
            privacy_class="private",
            recorded_at="2026-11-05T00:00:00Z",
            scope={"session_id": self.conversation},
        )
        return row, ref

    def confirm(self, *, dimension: str = "length", value: str = "detailed", text="记住：以后回答尽量详细。") -> dict:
        row, ref = self.register(text)
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.experience(),
            input_refs=[ref],
            adapter_kind=None,
        )
        body = {
            "claim": f"用户希望 {dimension}={value}",
            "claim_type": "user_preference",
            "evidence_links": [
                {
                    "ref": ref,
                    "relation": "supports",
                    "origin_group_id": f"collab_input:{row.input_id}",
                    "applicability_reason": "用户在本轮表达了该偏好",
                }
            ],
            "strength": "limited",
            "strength_reason": "用户明确表达。",
            "inference_basis": "direct_report",
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": f"expression_preference:{dimension}={value}",
        }
        candidate = self.changes.propose(
            self.change_access(),
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            bundle={
                "summary": "synthetic preference",
                "changes": [
                    {"op": "propose_belief", "body": body, "scope": {"scene": "test.scene"}}
                ],
            },
        )
        binding = DirectiveBinding(
            namespace=NS,
            host_instance=HOST,
            statement_ref=ref,
            statement_digest=row.content_digest,
            directive_kind="state_preference",
            target={"dimension": dimension, "value": value},
            allowed_claim_types=frozenset({"user_preference"}),
            decision_scope={"scene": "test.scene"},
            bound_at="2026-10-17T12:00:00Z",
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
        return committed.record_refs[0]

    # ---------------------------------------------------------------- tests
    def test_confirmed_is_visible_from_a_new_conversation_after_restart(self) -> None:
        self.confirm()
        row, ref = self.register("这次的问题在这里。")
        self.rebuild()
        document = self.document(ref, conversation=OTHER_CONVERSATION)
        confirmed = document["expression"]["confirmed"]
        self.assertEqual(1, len(confirmed))
        self.assertEqual("detailed", confirmed[0]["value"])

    def test_legacy_tentative_reads_back_but_is_never_rendered(self) -> None:
        self.legacy_tentative()
        row, ref = self.register("这次的问题在这里。")
        self.rebuild()
        # Still readable as a Record …
        active = self.prefs.active(
            namespace=NS,
            conversation_id=self.conversation,
            privacy_classes=frozenset({"private"}),
        )
        self.assertEqual(1, len(active))
        self.assertEqual("concise", active[0].value)
        # … but it contributes no expression material at all.
        self.assertIsNone(self.material(ref))
        self.assertNotIn("expression", self.document(ref))

    def test_legacy_tentative_revoke_survives_restart(self) -> None:
        _, ref = self.legacy_tentative()
        self.prefs.revoke(
            namespace=NS,
            conversation_id=self.conversation,
            dimension="length",
            reason="host test revoke",
            revoked_by_ref=ref,
        )
        self.rebuild()
        self.assertEqual(
            (),
            self.prefs.active(
                namespace=NS,
                conversation_id=self.conversation,
                privacy_classes=frozenset({"private"}),
            ),
        )
        self.assertNotIn("expression", self.document(ref))

    def test_restart_adds_no_record_and_loses_none(self) -> None:
        self.legacy_tentative()
        before = self.store.connection.execute(
            "SELECT COUNT(*) FROM record_versions WHERE kind = 'expression_preference'"
        ).fetchone()[0]
        self.rebuild()
        after = self.store.connection.execute(
            "SELECT COUNT(*) FROM record_versions WHERE kind = 'expression_preference'"
        ).fetchone()[0]
        self.assertEqual(before, after)
        row, ref = self.register("再问一句。")
        self.document(ref)
        again = self.store.connection.execute(
            "SELECT COUNT(*) FROM record_versions WHERE kind = 'expression_preference'"
        ).fetchone()[0]
        self.assertEqual(after, again)

    def test_directive_decision_audit_reads_back_after_restart(self) -> None:
        self.confirm()
        before = GovernanceStore(self.store).list_decisions(NS)[-1]
        self.assertEqual("user_directive", before.authority_kind)
        self.assertEqual({"scene": "test.scene"}, before.scope)
        self.rebuild()
        after = GovernanceStore(self.store).list_decisions(NS)[-1]
        self.assertEqual(before.decision_id, after.decision_id)
        self.assertEqual(before.evidence_refs, after.evidence_refs)
        self.assertEqual(before.target_refs, after.target_refs)
        self.assertEqual(before.scope, after.scope)
        self.assertEqual(before.directive_digest, after.directive_digest)


if __name__ == "__main__":
    unittest.main()
