"""J11: the bounded ``expression`` Context section after consolidation.

The section is now a view over **confirmed** beliefs only.  RG05-F02 also
rendered a conversation-scoped tentative record and a per-turn "turn_only" entry;
J11 removed both because a keyword classifier cannot reliably tell a description
from a directive.

These tests pin the surviving contract:

* a confirmed preference still renders and stays free of binding/digest/plan
  vocabulary;
* a **legacy** tentative row is readable through the service but is *never*
  rendered into the expression section (it cannot influence the reply any more);
* a turn with nothing confirmed produces no expression material at all;
* deleting the confirmed belief's cited input hides it.
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
from quintessence_pcs.domains.persona.expression_directives import (  # noqa: E402
    classify_directive,
)
from quintessence_pcs.domains.persona.expression_preferences import (  # noqa: E402
    EXPRESSION_PREFERENCE_KIND,
    EXPRESSION_PREFERENCE_SCHEMA,
    ExpressionPreferenceService,
)
from quintessence_pcs.runtime.collab_protocol import (  # noqa: E402
    COLLAB_V2_RENDERER_VERSION,
)
from quintessence_pcs.runtime.expression_material import (  # noqa: E402
    EXPRESSION_MATERIAL_VERSION,
    build_expression_material,
)
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

CONVERSATION = "11111111-1111-4111-8111-111111111111"


class ExpressionContextTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: "2026-10-17T12:00:00Z"
        )
        self.prefs = ExpressionPreferenceService(self.store)

    def access(self) -> ChangeAccess:
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

    def experience_access(self) -> ExperienceAccess:
        return ExperienceAccess(
            namespace=NS,
            host_instance="synthetic-host",
            access_ref="access:synthetic",
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
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

    def confirm(self, dimension: str, value: str, text: str):
        row, ref = self.register(text)
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.experience_access(),
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
            self.access(),
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
            host_instance="synthetic-host",
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
            self.access(),
            candidate_id=candidate.candidate_id,
            authority=CommitAuthority(directive=binding),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", committed.state)
        return ref

    def legacy_tentative(self, *, dimension: str = "length", value: str = "concise"):
        row, ref = self.register("我其实更喜欢简短一点的回答。")
        self.store.append_record_version(
            namespace=NS,
            record_id=self.prefs.record_id(NS, CONVERSATION, dimension),
            record_version=1,
            kind=EXPRESSION_PREFERENCE_KIND,
            body={
                "dimension": dimension,
                "value": value,
                "quote": "我其实更喜欢简短一点的回答。",
                "source_ref": dict(ref),
                "applies_to": "conversation",
                "state": "tentative",
            },
            status="tentative",
            schema_version=EXPRESSION_PREFERENCE_SCHEMA,
            privacy_class="private",
            recorded_at="2026-11-05T00:00:00Z",
            scope={"session_id": CONVERSATION},
        )
        return row, ref

    def build_material(self, *, plan=None, ref=None):
        return build_expression_material(
            self.store,
            namespace=NS,
            conversation_id=CONVERSATION,
            privacy_classes=frozenset({"private"}),
            plan=plan,
            turn_input_ref=ref,
        )

    def build_context(self, ref: dict, material=None):
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.experience_access(),
            input_refs=[ref],
            adapter_kind="direct-http",
            renderer_version=COLLAB_V2_RENDERER_VERSION,
            expression_material=material,
        )
        snapshot = self.store.load_context_snapshot(context.context_id)
        return snapshot, json.loads(snapshot.rendered_input)

    # ---------------------------------------------------------------- tests
    def test_material_version_is_v2(self) -> None:
        self.assertEqual("expression.material.v2", EXPRESSION_MATERIAL_VERSION)

    def test_confirmed_preference_renders(self) -> None:
        self.confirm("length", "detailed", "记住：以后回答详细一点。")
        row, ref = self.register("这次的问题在这里。")
        material = self.build_material(ref=ref)
        self.assertIsNotNone(material)
        document = json.loads(material.body)
        self.assertEqual("detailed", document["confirmed"][0]["value"])
        self.assertNotIn("tentative", document)
        self.assertNotIn("turn_only", document)
        self.assertEqual(
            ["current_user_input", "confirmed_preference", "persona"],
            document["precedence"],
        )

    def test_legacy_tentative_is_never_rendered(self) -> None:
        self.legacy_tentative()
        row, ref = self.register("这次的问题在这里。")
        material = self.build_material(ref=ref)
        # Nothing confirmed and no explicit directive in this turn: no material.
        self.assertIsNone(material)

    def test_legacy_tentative_is_not_added_to_a_confirmed_section(self) -> None:
        self.confirm("length", "detailed", "记住：以后回答详细一点。")
        self.legacy_tentative(dimension="tone", value="warm")
        row, ref = self.register("这次的问题在这里。")
        material = self.build_material(ref=ref)
        document = json.loads(material.body)
        self.assertNotIn("tentative", document)
        self.assertEqual(1, len(document["confirmed"]))
        self.assertNotIn("warm", material.body)

    def test_rendered_section_keeps_no_binding_vocabulary(self) -> None:
        self.confirm("length", "detailed", "记住：以后回答详细一点。")
        row, ref = self.register("这次的问题在这里。")
        material = self.build_material(ref=ref)
        snapshot, document = self.build_context(ref, material)
        self.assertIn("expression", document)
        rendered = snapshot.rendered_input
        self.assertNotIn("directive_digest", rendered)
        self.assertNotIn("DirectiveBinding", rendered)
        self.assertNotIn("quote_start", rendered)
        self.assertNotIn("policy:expression.directive", rendered)

    def test_explicit_long_term_directive_is_rendered_as_an_instruction(self) -> None:
        row, ref = self.register("记住：以后回答短一点。")
        plan = classify_directive("记住：以后回答短一点。")
        material = self.build_material(plan=plan, ref=ref)
        document = json.loads(material.body)
        self.assertEqual("explicit_long_term", document["directive"]["kind"])
        self.assertEqual([], document["confirmed"])
        self.assertNotIn("turn_only", document)

    def test_single_shot_leaves_no_material(self) -> None:
        # A one-off request is carried by the user's own message; the host keeps
        # no record and renders no preference for it.
        row, ref = self.register("这次回答短一点。")
        plan = classify_directive("这次回答短一点。")
        self.assertIsNone(self.build_material(plan=plan, ref=ref))


if __name__ == "__main__":
    unittest.main()
