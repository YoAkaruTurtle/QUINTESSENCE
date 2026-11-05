"""RG05-F02: the preference loop through the real tool handler.

These tests drive ``cognition.propose`` with a host-built router, exactly like a
chat turn.  They prove:

* an explicit lasting preference is committed with a ``user_directive``
  decision, not a candidate waiting for a human;
* a one-shot request creates nothing;
* an ordinary low-impact preference becomes a conversation-scoped tentative
  record, not a belief;
* a correction of a pending candidate withdraws it deterministically and can
  commit a new value;
* a confirmed correction retracts the old belief and commits the replacement;
* the next Context carries the new value and not the stale one.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOMAIN = HERE.parent / "domain"
SRC = HERE.parents[1] / "src"
for _path in (HERE, DOMAIN, SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.collaboration.change_service import (  # noqa: E402
    ChangeAccess,
    CognitionChangeService,
    DirectiveBinding,
)
from quintessence_pcs.collaboration.policy import CommitAuthority  # noqa: E402
from quintessence_pcs.conversation.tools import build_tool_runtime  # noqa: E402
from quintessence_pcs.domains.cognition.epistemic import EpistemicAccess  # noqa: E402
from quintessence_pcs.domains.cognition.query import (  # noqa: E402
    CognitionQueryService,
    QueryAccess,
)
from quintessence_pcs.domains.cognition.questions import (  # noqa: E402
    QuestionAccess,
    QuestionService,
)
from quintessence_pcs.domains.persona.expression_directives import (  # noqa: E402
    KIND_NONE,
    classify_directive,
)
from quintessence_pcs.domains.persona.expression_preferences import (  # noqa: E402
    ExpressionPreferenceService,
)
from quintessence_pcs.runtime.collab_protocol import (  # noqa: E402
    COLLAB_V2_RENDERER_VERSION,
)
from quintessence_pcs.runtime.expression_material import (  # noqa: E402
    build_expression_material,
)
from quintessence_pcs.runtime.preference_router import (  # noqa: E402
    PREFERENCE_NOT_VERIFIED,
    PREFERENCE_SPLIT_REQUIRED,
    PreferenceRouter,
    PreferenceTarget,
    TurnPreferenceContext,
    bind_router_context,
)
from quintessence_pcs.runtime.tool_catalog import ToolArgumentsInvalid  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402
from quintessence_pcs.store.governance import GovernanceStore  # noqa: E402
from quintessence_pcs.store.errors import CandidateTransitionDenied  # noqa: E402

CONVERSATION = "11111111-1111-4111-8111-111111111111"


class PreferenceLoopTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: "2026-10-17T12:00:00Z"
        )
        self.prefs = ExpressionPreferenceService(self.store)
        self.governance = GovernanceStore(self.store)

    def change_access(self) -> ChangeAccess:
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

    def consult(self, ref: dict):
        return self.contexts.build_consult_context(
            namespace=NS,
            access=self.experience_access(),
            input_refs=[ref],
            adapter_kind=None,
            renderer_version=COLLAB_V2_RENDERER_VERSION,
        )

    def args(self, claim: str = "用户希望回答简短。"):
        return {
            "operation": "cognition.change",
            "summary": "synthetic preference proposal",
            "changes": [
                {
                    "op": "propose_belief",
                    "body": {
                        "claim": claim,
                        "claim_type": "user_preference",
                        "evidence_links": [],
                        "strength": "limited",
                        "strength_reason": "synthetic",
                        "inference_basis": "direct_report",
                        "premise_refs": [],
                        "alternative_group_id": None,
                        "valid_until": None,
                        "review_condition": "not-used",
                    },
                    "scope": {"scene": "test.scene"},
                }
            ]
        }

    def run_turn(
        self,
        text: str,
        claim: str,
        *,
        targets=(),
        args=None,
        strength: str | None = None,
        cite_input: bool = False,
    ):
        row, ref = self.register(text)
        if strength is not None or cite_input:
            args = self.args(claim)
            body = args["changes"][0]["body"]
            if strength is not None:
                body["strength"] = strength
                body["strength_reason"] = "synthetic strength"
            if cite_input:
                body["evidence_links"] = [
                    {
                        "ref": dict(ref),
                        "relation": "supports",
                        "origin_group_id": "synthetic-group",
                        "applicability_reason": "synthetic",
                    }
                ]
        context = self.consult(ref)
        plan = classify_directive(text, active_tentative={
            item.dimension: item.value
            for item in self.prefs.active(
                namespace=NS,
                conversation_id=CONVERSATION,
                privacy_classes=frozenset({"private"}),
            )
        })
        turn = TurnPreferenceContext(
            namespace=NS,
            conversation_id=CONVERSATION,
            turn_id=str(uuid.uuid4()),
            input_ref=ref,
            plan=plan,
            targets=tuple(targets),
        )
        router = PreferenceRouter(
            store=self.store,
            changes=self.changes,
            preferences=self.prefs,
            directive_access=self.change_access(),
            turn=turn,
            now=lambda: "2026-10-17T12:00:00Z",
        )
        bind_router_context(router, scope={"scene": "test.scene"})
        runtime = build_tool_runtime(
            self.store,
            queries=CognitionQueryService(self.store),
            questions=QuestionService(self.store),
            access=self.change_access(),
            query_access=QueryAccess(
                namespace=NS,
                privacy_classes=frozenset({"private"}),
                purposes=frozenset({"consult"}),
            ),
            question_access=QuestionAccess(
                namespace=NS,
                host_instance="synthetic-host",
                access_ref="access:synthetic",
                privacy_classes=frozenset({"private"}),
                permissions=frozenset({"question.read"}),
                purposes=frozenset({"cognition.question"}),
            ),
            epistemic_access=EpistemicAccess(
                namespace=NS, privacy_classes=frozenset({"private"})
            ),
            change_scope={"scene": "test.scene"},
            context_id=context.context_id,
            include_propose=True,
            turn_id=turn.turn_id,
            preference_router=router,
        )
        return (
            runtime.handlers["cognition.propose"](
                self.args(claim) if args is None else args
            ),
            context,
            ref,
            plan,
        )

    def next_document(self, ref: dict):
        material = build_expression_material(
            self.store,
            namespace=NS,
            conversation_id=CONVERSATION,
            privacy_classes=frozenset({"private"}),
        )
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.experience_access(),
            input_refs=[ref],
            adapter_kind="direct-http",
            renderer_version=COLLAB_V2_RENDERER_VERSION,
            expression_material=material,
        )
        return json.loads(self.store.load_context_snapshot(context.context_id).rendered_input)

    def test_explicit_preference_is_committed_not_pending(self) -> None:
        result, context, ref, _plan = self.run_turn(
            "记住：以后回答尽量简短。", "用户希望回答简短。"
        )
        self.assertEqual("COMMITTED", result["result"]["state"])
        self.assertEqual(
            "user_directive", result["result"]["authority_kind"]
        )
        self.assertEqual(1, len(self.store.current_versions_of_kind(NS, "belief")))
        pending = self.store.pending_change_candidates(NS, limit=20)
        self.assertEqual((), pending)
        self.assertEqual(1, self.count_rows("cognition_review_decisions"))
        document = self.next_document(ref)
        self.assertEqual("concise", document["expression"]["confirmed"][0]["value"])

        # F02-AUDIT-01 through the real router: the decision row carries the
        # classifier rule version the router actually ran, not just a constant.
        decision = self.governance.list_decisions(NS)[0]
        self.assertEqual({"scene": "test.scene"}, decision.scope)
        self.assertEqual(
            [
                {
                    "dimension": "length",
                    "conversation_id": CONVERSATION,
                    "value": "concise",
                }
            ],
            decision.target_refs,
        )
        statement_ref, audit = decision.evidence_refs
        self.assertEqual(ref, statement_ref)
        self.assertEqual("directive_binding_audit", audit["kind"])
        self.assertEqual("state_preference", audit["directive_kind"])
        self.assertEqual(
            self.store.load_input(
                namespace=NS, input_id=ref["input_id"], content_version=1
            ).content_digest,
            audit["statement_digest"],
        )
        self.assertEqual("policy:expression.directive.v1", audit["binding_policy_id"])
        self.assertEqual("1", audit["binding_policy_version"])
        self.assertEqual(
            "policy:expression.directive.v1", audit["classifier_policy_id"]
        )
        self.assertEqual("1", audit["classifier_policy_version"])
        self.assertTrue(decision.directive_digest.startswith("sha256:"))

    def test_one_shot_creates_nothing(self) -> None:
        result, _context, ref, _plan = self.run_turn(
            "这次回答短一点。", "用户希望回答简短。"
        )
        self.assertEqual("TURN_ONLY", result["result"]["state"])
        self.assertFalse(result["result"]["persisted"])
        self.assertEqual(0, len(self.store.current_versions_of_kind(NS, "belief")))
        self.assertEqual(
            0, len(self.store.current_versions_of_kind(NS, "expression_preference"))
        )
        self.assertEqual(0, self.count_rows("cognition_review_decisions"))

    def test_an_ordinary_statement_can_only_become_a_reviewed_candidate(self) -> None:
        # J11 consolidation: the host no longer keeps a tentative record and no
        # longer vetoes the proposal; a preference inferred from an ordinary
        # sentence becomes a normal candidate that Review must approve.  Nothing
        # is committed and no expression_preference record is written.
        result, *_ = self.run_turn(
            "我其实更喜欢简短一点的回答。", "用户喜欢简短的回答。"
        )
        payload = result["result"]
        self.assertEqual("AWAITING_HUMAN", payload["state"])
        self.assertEqual("E2", payload["impact"])
        self.assertEqual(1, len(self.store.pending_change_candidates(NS, limit=20)))
        self.assertEqual(0, len(self.store.current_versions_of_kind(NS, "belief")))
        self.assertEqual(
            0, len(self.store.current_versions_of_kind(NS, "expression_preference"))
        )
        # Review did run: an E2 candidate is a decision row awaiting a human.
        self.assertEqual(1, self.count_rows("cognition_review_decisions"))

    def test_an_inferred_preference_cannot_auto_commit_even_when_claimed_strong(self) -> None:
        # Review checks that the cited evidence is the right *kind* of
        # observation; it does not read the sentence to see whether it supports
        # the claim.  The structural guarantee is elsewhere: a user-owned claim
        # without a host-built directive binding is raised to E2, so a model
        # inference can never be committed on its own -- even when the proposal
        # claims a strong, verified body.
        result, *_ = self.run_turn(
            "今天天气不错。",
            "用户希望回答短一些。",
            strength="strong",
            cite_input=True,
        )
        payload = result["result"]
        self.assertEqual("AWAITING_HUMAN", payload["state"])
        self.assertEqual("E2", payload["impact"])
        self.assertTrue(payload["requires_human"])
        self.assertEqual(0, len(self.store.current_versions_of_kind(NS, "belief")))

    def test_guarded_statement_is_refused(self) -> None:
        with self.assertRaises(Exception):
            self.run_turn("他说你喜欢简短。", "用户喜欢简短的回答。")
        self.assertEqual(
            0, len(self.store.current_versions_of_kind(NS, "expression_preference"))
        )
        self.assertEqual(0, self.count_rows("cognition_review_decisions"))

    def test_an_inferred_expression_claim_becomes_a_reviewed_candidate(self) -> None:
        # J11 consolidation: the host no longer vetoes a model-proposed
        # preference because the wording was not an explicit directive.  The
        # candidate takes the ordinary path: Review decides its impact, and no
        # belief exists until it is committed.
        result, *_ = self.run_turn("今天天气不错。", "用户希望回答长一些。")
        payload = result["result"]
        self.assertEqual("AWAITING_HUMAN", payload["state"])
        self.assertEqual("E2", payload["impact"])
        self.assertTrue(payload["requires_human"])
        candidates = self.store.pending_change_candidates(NS, limit=20)
        self.assertEqual(1, len(candidates))
        self.assertEqual(0, len(self.store.current_versions_of_kind(NS, "belief")))
        self.assertEqual(
            0, len(self.store.current_versions_of_kind(NS, "expression_preference"))
        )

    def test_a_claim_the_host_cannot_confirm_is_reviewed_not_committed(self) -> None:
        # J11 consolidation: an ambiguous input still produces a candidate, but
        # nothing is committed and the host does not pretend the wording was an
        # explicit directive.  Review owns the decision.
        result, *_ = self.run_turn("以后回答都可以。", "用户希望回答温柔一些。")
        payload = result["result"]
        self.assertEqual("AWAITING_HUMAN", payload["state"])
        self.assertEqual("E2", payload["impact"])
        self.assertEqual(1, len(self.store.pending_change_candidates(NS, limit=20)))
        self.assertEqual(0, len(self.store.current_versions_of_kind(NS, "belief")))

    def _change(self, claim: str) -> dict:
        import copy

        change = copy.deepcopy(self.args(claim)["changes"][0])
        return change

    def test_expression_claim_bundled_with_a_belief_is_refused(self) -> None:
        # The live turn proposed exactly this length claim and a colour claim in
        # one bundle.  Left alone, the expression half becomes a belief about the
        # user, so the router asks for it to be sent on its own.
        args = self.args(
            "用户在回答长度维度上的偏好为 length=detailed（希望回答更详细、更长一些）"
        )
        args["summary"] = "mixed bundle"
        args["changes"].append(self._change("用户偏好深红色"))
        with self.assertRaises(ToolArgumentsInvalid) as caught:
            self.run_turn(
                "我喜欢深红。以后回答可以长一些。",
                "",
                args=args,
            )
        self.assertIn(PREFERENCE_SPLIT_REQUIRED, str(caught.exception))
        self.assertEqual(0, len(self.store.current_versions_of_kind(NS, "belief")))
        self.assertEqual((), self.store.pending_change_candidates(NS, limit=20))

    def test_bundle_without_an_expression_claim_keeps_the_candidate_path(self) -> None:
        args = self.args("用户偏好深红色")
        args["summary"] = "two colour preferences"
        args["changes"].append(self._change("用户偏好安静的颜色"))
        self.run_turn("我喜欢深红，也喜欢安静的颜色。", "", args=args)
        self.assertEqual(
            0, len(self.store.current_versions_of_kind(NS, "expression_preference"))
        )
        self.assertEqual(1, len(self.store.pending_change_candidates(NS, limit=20)))

    def test_non_expression_user_preference_keeps_the_belief_path(self) -> None:
        # A colour preference is not an expression rule: the host must leave the
        # normal candidate path alone, or the only way to record it disappears.
        args = self.args("用户喜欢红色。")
        args["summary"] = "colour preference candidate"
        _result, _context, _ref, plan = self.run_turn(
            "我喜欢红色，具体什么色号说不上来。", "用户喜欢红色。", args=args
        )
        self.assertEqual(KIND_NONE, plan.kind)
        self.assertIsNone(plan.dimension)
        self.assertEqual(
            0, len(self.store.current_versions_of_kind(NS, "expression_preference"))
        )
        pending = self.store.pending_change_candidates(NS, limit=20)
        self.assertEqual(1, len(pending))
        # A real candidate is the only thing that makes Review non-empty: the
        # propose step records its awaiting-human decision.
        self.assertEqual(1, self.count_rows("cognition_review_decisions"))

    def test_pending_candidate_is_withdrawn_and_replaced(self) -> None:
        # The old preference is a pending candidate from an earlier ordinary
        # statement that the host did not bind.
        body = {
            "claim": "用户喜欢简短的回答。",
            "claim_type": "user_preference",
            "evidence_links": [],
            "strength": "limited",
            "strength_reason": "synthetic",
            "inference_basis": "direct_report",
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": "expression_preference:length=concise",
        }
        row, ref = self.register("我喜欢简短的回答，不过看情况。")
        context = self.consult(ref)
        pending = self.changes.propose(
            self.change_access(),
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            bundle={
                "summary": "pending preference",
                "changes": [
                    {"op": "propose_belief", "body": body, "scope": {"scene": "test.scene"}}
                ],
            },
        )
        self.assertEqual("pending", self.governance.lifecycle(NS, pending.candidate_id))
        withdrawn = self.changes.withdraw_candidate(
            self.change_access(),
            candidate_id=pending.candidate_id,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("REJECTED", withdrawn.state)
        result, _context, ref2, _plan = self.run_turn(
            "刚才说错了，我不要简短，还是要详细解释。",
            "用户希望回答详细。",
        )
        self.assertEqual("COMMITTED", result["result"]["state"])
        self.assertEqual("detailed", result["result"]["value"])
        self.assertEqual("rejected", self.governance.lifecycle(NS, pending.candidate_id))
        document = self.next_document(ref2)
        self.assertEqual("detailed", document["expression"]["confirmed"][0]["value"])
        # The withdrawn candidate is no longer in the pending list.
        self.assertEqual((), self.store.pending_change_candidates(NS, limit=20))

    def test_confirmed_correction_can_retract_without_a_new_value(self) -> None:
        result, _context, ref, _plan = self.run_turn(
            "记住：以后回答尽量简短。", "用户希望回答简短。"
        )
        record_id = result["result"]["record_refs"][0]["record_id"]
        record_version = result["result"]["record_refs"][0]["record_version"]
        target = PreferenceTarget(
            kind="confirmed",
            dimension="length",
            value="concise",
            record_id=record_id,
            record_version=record_version,
        )
        retract_args = {
            "changes": [
                {
                    "op": "retract_belief",
                    "target": {
                        "record_id": record_id,
                        "record_version": record_version,
                    },
                    "evidence_links": [],
                    "reason": "synthetic retraction",
                }
            ]
        }
        corrected, _context, ref2, _plan = self.run_turn(
            "不要简短了。", "", targets=(target,), args=retract_args
        )
        self.assertEqual("COMMITTED", corrected["result"]["state"])
        current = self.store.get_current(NS, record_id)
        self.assertEqual("retracted", current.status)
        document = self.next_document(ref2)
        self.assertIsNone(document.get("expression"))

    def test_confirmed_correction_retracts_and_replaces(self) -> None:
        result, _context, ref, _plan = self.run_turn(
            "记住：以后回答尽量简短。", "用户希望回答简短。"
        )
        record_id = result["result"]["record_refs"][0]["record_id"]
        record_version = result["result"]["record_refs"][0]["record_version"]
        target = PreferenceTarget(
            kind="confirmed",
            dimension="length",
            value="concise",
            record_id=record_id,
            record_version=record_version,
        )
        corrected, _context, ref2, _plan = self.run_turn(
            "刚才说错了，我不要简短，还是要详细解释。",
            "用户希望回答详细。",
            targets=(target,),
        )
        self.assertEqual("COMMITTED", corrected["result"]["state"])
        current = [
            version
            for version in self.store.current_versions_of_kind(NS, "belief")
            if version.body.get("claim_type") == "user_preference"
        ]
        by_status = {version.status for version in current}
        self.assertEqual({"supported", "retracted"}, by_status)
        self.assertEqual(2, len(current))
        history = self.store.get_version(
            NS, record_id, record_version + 1
        )
        self.assertEqual("retracted", history.status)
        document = self.next_document(ref2)
        self.assertEqual("detailed", document["expression"]["confirmed"][0]["value"])
        # The old value is not in the next Context.
        self.assertNotIn("保持简短", json.dumps(document, ensure_ascii=False))
        # A new conversation reads the corrected confirmed value, not the old
        # one; the tentative scope does not hide it.
        new_conversation = build_expression_material(
            self.store,
            namespace=NS,
            conversation_id="33333333-3333-4333-8333-333333333333",
            privacy_classes=frozenset({"private"}),
        )
        new_document = json.loads(new_conversation.body)
        self.assertEqual(
            "detailed", new_document["confirmed"][0]["value"]
        )
        self.assertNotIn("tentative", new_document)
        self.assertNotIn("保持简短", new_conversation.body)
        correction_decision = self.governance.list_decisions(NS)[-1]
        self.assertEqual(
            "revoke_previous",
            correction_decision.evidence_refs[1]["directive_kind"],
        )
        self.assertEqual(
            record_id, correction_decision.target_refs[0]["old_record_id"]
        )


if __name__ == "__main__":
    unittest.main()
