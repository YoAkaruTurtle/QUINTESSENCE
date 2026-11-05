"""RG05: the bounded cognition working set and its Context budget proof.

Synthetic data only.  No model, no network, no private asset.
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
)
from quintessence_pcs.domains.cognition import BeliefCurrentQuery  # noqa: E402
from quintessence_pcs.domains.cognition.epistemic import EpistemicAccess  # noqa: E402
from quintessence_pcs.domains.cognition.working_set import (  # noqa: E402
    MAX_PENDING_CORRECTIONS,
    MAX_WORKING_SET_ITEMS,
    WORKING_SET_POLICY,
    build_working_set,
)
from quintessence_pcs.foundation.jsonutil import canonical_json_text  # noqa: E402
from quintessence_pcs.runtime import (  # noqa: E402
    ContextService,
    cognition_material_from_working_set,
)
from quintessence_pcs.store.conversation import HistoryBundle, HistoryMessage  # noqa: E402
from quintessence_pcs.store.errors import ContextTooLarge  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:synthetic-rg05"
ACCESS_REF = "authorization:access:rg05"
SCOPE = {"project_id": "rg05.lab"}
PROFILE_ID = "example.synthetic.rg05"
_UNSET = object()


class WorkingSetTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.epistemic = EpistemicAccess(
            namespace=NS, privacy_classes=frozenset({"private"})
        )
        self.experience = ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
        )

    # ------------------------------------------------------------- fixtures

    def belief_body(
        self,
        claim: str,
        *,
        claim_type: str = "user_preference",
        inference_basis: str = "direct_report",
    ) -> dict:
        return {
            "claim": claim,
            "claim_type": claim_type,
            "evidence_links": [],
            "strength": "insufficient",
            "strength_reason": "synthetic seed without evidence",
            "inference_basis": inference_basis,
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": "synthetic review condition",
        }

    def seed(
        self,
        claim: str,
        *,
        claim_type: str = "user_preference",
        status: str = "proposed",
        scope: dict | None = None,
        privacy: str = "private",
        recorded_at: str = "2026-10-16T00:00:00Z",
        inference_basis: str = "direct_report",
        review_condition: str | None = None,
    ):
        body = self.belief_body(
            claim, claim_type=claim_type, inference_basis=inference_basis
        )
        if review_condition is not None:
            body["review_condition"] = review_condition
        return self.store.append_record_version(
            namespace=NS,
            record_id=str(uuid.uuid4()),
            record_version=1,
            kind="belief",
            body=body,
            status=status,
            schema_version="1",
            privacy_class=privacy,
            recorded_at=recorded_at,
            scope=scope if scope is not None else dict(SCOPE),
        )

    def seed_qualifying(
        self,
        source_text: str,
        *,
        review_condition: str = "若能在控制换土、温度等变量的条件下继续观察，再评估。",
        relation: str = "qualifies",
        claim: str = "浇水频率改变后的长势可能改善，但归因仍不确定。",
    ):
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body=source_text,
            privacy_class="private",
            retention="standard",
        )
        ref = {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": row.content_version,
        }
        self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:collab.v1",
            input_refs=[ref],
        )
        body = self.belief_body(
            claim, claim_type="general_hypothesis", inference_basis="inductive"
        )
        body["evidence_links"] = [
            {
                "ref": ref,
                "relation": relation,
                "origin_group_id": f"collab_input:{row.input_id}",
                "applicability_reason": (
                    "用户指出换土与浇水、降温同期发生，属于额外混杂因素。"
                ),
            }
        ]
        body["review_condition"] = review_condition
        body["strength_reason"] = "换土与浇水、降温同时作用，无法分离。"
        version = self.store.append_record_version(
            namespace=NS,
            record_id=str(uuid.uuid4()),
            record_version=1,
            kind="belief",
            body=body,
            status="proposed",
            schema_version="1",
            privacy_class="private",
            recorded_at="2026-10-16T00:00:00Z",
            scope=dict(SCOPE),
        )
        return version, ref

    def plan(
        self,
        *,
        claim_types=("user_preference",),
        scope: dict | None = None,
        limit: int = 5,
    ) -> BeliefCurrentQuery:
        return BeliefCurrentQuery(
            claim_types=tuple(claim_types),
            scope=dict(SCOPE if scope is None else scope),
            limit=limit,
        )

    def working_set(self, *, plan=_UNSET, pending_limit: int = 3, belief_limit: int = 5):
        if plan is _UNSET:
            plan = self.plan()
        return build_working_set(
            self.store,
            namespace=NS,
            epistemic_access=self.epistemic,
            query_plan=plan,
            experience_access=self.experience,
            belief_limit=belief_limit,
            pending_limit=pending_limit,
        )

    def propose_candidate(self, claim: str = "回答尽量简短些", *, scope: dict | None = None) -> str:
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="合成陈述：以后回答简短些。",
            privacy_class="private",
            retention="standard",
        )
        ref = {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": row.content_version,
        }
        self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:collab.v1",
            input_refs=[ref],
        )
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.experience,
            input_refs=[ref],
            adapter_kind="direct-http",
        )
        change_scope = dict(SCOPE if scope is None else scope)
        access = ChangeAccess(
            namespace=NS,
            host_instance=HOST,
            operator="operator:synthetic",
            access_ref=ACCESS_REF,
            permissions=frozenset({"change.propose", "change.review"}),
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"cognition.change"}),
        )
        result = CognitionChangeService(self.store).propose(
            access,
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            bundle={
                "operation": "cognition.change",
                "summary": "synthetic preference",
                "changes": [
                    {
                        "op": "propose_belief",
                        "body": self.belief_body(claim),
                        "scope": change_scope,
                    }
                ],
            },
        )
        return result.candidate_id

    # ---------------------------------------------------------------- checks

    def test_no_plan_and_no_pending_returns_none(self) -> None:
        self.assertIsNone(self.working_set(plan=None, pending_limit=0))

    def test_claim_type_and_scope_filter(self) -> None:
        matching = self.seed("匹配的合成偏好", recorded_at="2026-10-16T01:00:00Z")
        self.seed(
            "类型不匹配的合成假设",
            claim_type="general_hypothesis",
            inference_basis="inductive",
        )
        self.seed("范围不匹配的合成偏好", scope={"project_id": "other.project"})
        ws = self.working_set()
        self.assertIsNotNone(ws)
        body = json.loads(ws.body)
        refs = [item["ref"]["record_id"] for item in body["provisional"]]
        self.assertEqual([matching.record_id], refs)
        self.assertEqual([(matching.record_id, matching.record_version)], [
            (ref.record_id, ref.record_version) for ref, usage in ws.records
        ])
        self.assertEqual("current", ws.records[0][1])

    def test_expression_preference_marker_is_not_cognition(self) -> None:
        marker_claim = "用户在聊天中希望回答更详细。"
        self.seed(
            marker_claim,
            status="supported",
            review_condition="expression_preference:length=detailed",
        )
        normal = self.seed("用户喜欢红色。", status="supported")
        ws = self.working_set()
        body = json.loads(ws.body)
        accepted = [item["claim"] for item in body["accepted"]]
        provisional = [item["claim"] for item in body["provisional"]]
        contested = [item["claim"] for item in body["contested"]]
        self.assertNotIn(marker_claim, accepted)
        self.assertNotIn(marker_claim, provisional)
        self.assertNotIn(marker_claim, contested)
        self.assertIn("用户喜欢红色。", accepted)
        self.assertEqual([normal.record_id], [item["ref"]["record_id"] for item in body["accepted"]])
        # The same record is still the expression layer's input.
        from quintessence_pcs.runtime.expression_material import build_expression_material

        material = build_expression_material(
            self.store,
            namespace=NS,
            conversation_id=str(uuid.uuid4()),
            privacy_classes=frozenset({"private"}),
        )
        self.assertIsNotNone(material)
        document = json.loads(material.body)
        self.assertEqual(
            [("length", "detailed")],
            [(item["dimension"], item["value"]) for item in document["confirmed"]],
        )
        self.assertEqual(marker_claim, document["confirmed"][0]["claim"])

    def test_privacy_class_filters_beliefs(self) -> None:
        self.seed("私密偏好")
        self.seed("受限偏好", privacy="restricted")
        ws = self.working_set()
        body = json.loads(ws.body)
        self.assertEqual(1, len(body["provisional"]))
        self.assertNotIn("受限偏好", ws.body)

    def test_empty_match_is_an_empty_array_not_an_error(self) -> None:
        ws = self.working_set()
        self.assertIsNotNone(ws)
        body = json.loads(ws.body)
        self.assertEqual([], body["accepted"])
        self.assertEqual([], body["provisional"])
        self.assertEqual(WORKING_SET_POLICY, body["policy"])

    def test_working_set_is_bounded_and_reports_visible_truncation(self) -> None:
        for index in range(10):
            self.seed(f"合成偏好 {index}", recorded_at=f"2026-10-16T0{index}:00:00Z")
        ws = self.working_set(belief_limit=5)
        body = json.loads(ws.body)
        total = (
            len(body["accepted"])
            + len(body["provisional"])
            + len(body["contested"])
        )
        self.assertLessEqual(total, 5)
        self.assertGreater(body["omitted"]["beliefs_beyond_limit"], 0)
        self.assertLessEqual(len(ws.records), 5)

    def test_newest_first_inside_provisional(self) -> None:
        older = self.seed("旧偏好", recorded_at="2026-10-15T00:00:00Z")
        newer = self.seed("新偏好", recorded_at="2026-10-17T00:00:00Z")
        body = json.loads(self.working_set().body)
        order = [item["ref"]["record_id"] for item in body["provisional"]]
        self.assertEqual([newer.record_id, older.record_id], order)

    def test_pending_correction_is_visible_and_never_effective(self) -> None:
        candidate_id = self.propose_candidate()
        ws = self.working_set(plan=self.plan())
        body = json.loads(ws.body)
        pending = body["pending_corrections"]
        self.assertEqual(1, len(pending))
        self.assertEqual(candidate_id, pending[0]["candidate_ref"]["artifact_id"])
        self.assertFalse(pending[0]["effective"])
        self.assertEqual("PROPOSED", pending[0]["state"])
        self.assertEqual(1, len(ws.collab_refs))
        self.assertEqual(candidate_id, ws.collab_refs[0]["artifact_id"])

    def test_pending_correction_still_visible_without_a_belief_plan(self) -> None:
        candidate_id = self.propose_candidate()
        ws = self.working_set(plan=None)
        self.assertIsNotNone(ws)
        body = json.loads(ws.body)
        self.assertEqual([], body["provisional"])
        self.assertEqual(candidate_id, body["pending_corrections"][0]["candidate_ref"]["artifact_id"])

    def test_pending_limit_is_bounded(self) -> None:
        self.propose_candidate("偏好一")
        self.propose_candidate("偏好二")
        ws = self.working_set(plan=self.plan(), pending_limit=1)
        body = json.loads(ws.body)
        self.assertEqual(1, len(body["pending_corrections"]))
        self.assertGreaterEqual(body["omitted"]["pending_beyond_limit"], 1)

    def test_material_wrapper_rejects_non_working_set_values(self) -> None:
        from quintessence_pcs.foundation.errors import InvalidInput

        with self.assertRaises(InvalidInput):
            cognition_material_from_working_set(object())

    def test_provisional_carries_exact_quote_and_future_condition(self) -> None:
        source = (
            "我后来发现那盆换过土，之前浇水改变和降温的影响可能被换土盖过了；"
            "现在叶子还是蔫。"
        )
        version, ref = self.seed_qualifying(
            source,
            review_condition="若能控制换土、温度等变量后再观察，再评估该假设。",
        )
        plan = self.plan(claim_types=("general_hypothesis",))
        ws = self.working_set(plan=plan)
        body = json.loads(ws.body)
        item = body["provisional"][0]
        self.assertEqual(version.record_id, item["ref"]["record_id"])
        evidence = item["evidence"][0]
        self.assertEqual(ref, evidence["ref"])
        self.assertEqual("qualifies", evidence["relation"])
        self.assertEqual("user", evidence["subject"])
        self.assertFalse(evidence["subject_omitted"])
        self.assertEqual("exact_source", evidence["quote_kind"])
        self.assertEqual(source, evidence["quote"])
        self.assertFalse(evidence["quote_omitted"])
        self.assertEqual("future_recheck", item["review_condition"]["kind"])
        self.assertEqual(
            "若能控制换土、温度等变量后再观察，再评估该假设。",
            item["review_condition"]["text"],
        )
        self.assertFalse(item["review_condition"]["omitted"])
        # The overreaching model prose is never rendered; the exact user quote
        # carries the limitation instead.
        self.assertNotIn("applicability_reason", ws.body)
        self.assertNotIn("strength_reason", ws.body)
        self.assertNotIn("同期发生", ws.body)

    def test_review_condition_uncertainty_about_cooccurrence_is_not_banned(self) -> None:
        condition = "尚不确定换土与降温是否同期，待确认时间关系后再评估。"
        self.seed_qualifying("合成来源：换过土，可能影响归因。", review_condition=condition)
        ws = self.working_set(plan=self.plan(claim_types=("general_hypothesis",)))
        item = json.loads(ws.body)["provisional"][0]
        self.assertEqual(condition, item["review_condition"]["text"])
        self.assertFalse(item["review_condition"]["omitted"])

    def test_a_declarative_cooccurrence_claim_is_omitted_not_asserted(self) -> None:
        condition = "换土与浇水、降温同期发生。"
        self.seed_qualifying("合成来源：换过土，可能影响归因。", review_condition=condition)
        ws = self.working_set(plan=self.plan(claim_types=("general_hypothesis",)))
        item = json.loads(ws.body)["provisional"][0]
        self.assertIsNone(item["review_condition"]["text"])
        self.assertTrue(item["review_condition"]["omitted"])
        self.assertNotIn("同期发生", ws.body)

    def test_long_source_or_condition_is_omitted_whole_not_cut(self) -> None:
        long_source = "换过土。" + ("额外来源内容" * 40)
        long_condition = "若能确认。" + ("继续观察" * 80)
        self.seed_qualifying(long_source, review_condition=long_condition)
        ws = self.working_set(plan=self.plan(claim_types=("general_hypothesis",)))
        item = json.loads(ws.body)["provisional"][0]
        evidence = item["evidence"][0]
        self.assertIsNone(evidence["quote"])
        self.assertTrue(evidence["quote_omitted"])
        self.assertEqual("collab_input", evidence["ref"]["ref_type"])
        self.assertIsNone(item["review_condition"]["text"])
        self.assertTrue(item["review_condition"]["omitted"])

    def test_policy_and_origin_are_explicit(self) -> None:
        self.seed("合成偏好")
        body = json.loads(self.working_set().body)
        self.assertEqual("host_working_set", body["origin"])
        self.assertIn("pending_corrections", body)
        self.assertNotIn("state_revision", body)
        forbidden = json.dumps(body, ensure_ascii=False)
        for internal in ("not_authorized", "redacted", "unavailable"):
            self.assertNotIn(internal, forbidden)


class WorstCaseContextTests(CognitionTestCase):
    """RG05/Q4: the worst realistic v2 Context must fit 32 KiB, and need it."""

    def setUp(self) -> None:
        super().setUp()
        self.epistemic = EpistemicAccess(
            namespace=NS, privacy_classes=frozenset({"private"})
        )
        self.experience = ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
        )

    def build(
        self,
        max_bytes: int,
        *,
        expression_material=None,
        expression_material_factory=None,
    ):
        from quintessence_pcs.conversation.tools import build_catalog

        service = ContextService(
            self.store,
            self_view_service=self.self_view,
            profile_service=self.profiles,
            pcs_context_max_bytes=max_bytes,
        )
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="合成当前输入：" + "请根据我的偏好回答。" * 20,
            privacy_class="private",
            retention="standard",
        )
        ref = {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": row.content_version,
        }
        self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:collab.v1",
            input_refs=[ref],
        )
        # 8 KiB of history: 16 complete synthetic turns.
        # One real input per synthetic history row (the ledger requires each
        # exact ref to be live and unique).  This measures bytes, not plumbing.
        history_messages = []
        for index in range(8):
            for role in ("user", "assistant"):
                text = f"合成历史 {index} {role} " + ("历史内容" * 55)
                history_row = self.store.register_input(
                    namespace=NS,
                    origin_kind="user_statement",
                    body=text,
                    privacy_class="private",
                    retention="standard",
                )
                history_messages.append(
                    HistoryMessage(
                        role=role,
                        text=text,
                        ref_kind="collab_input",
                        ref_id=history_row.input_id,
                        ref_version=history_row.content_version,
                        turn_no=index + 1,
                        seq=len(history_messages) + 1,
                    )
                )
        history = HistoryBundle(
            messages=tuple(history_messages),
            omissions=(),
            stats={
                "limit_turns": 8,
                "limit_bytes": 8192,
                "history_bytes": sum(len(m.text.encode()) for m in history_messages),
                "history_turns": 8,
                "history_messages": 16,
                "dropped_turns": 0,
                "dropped_messages": 0,
            },
        )
        # Persona: full profile + two expression demos (~2 KiB cap).
        persona = {
            "asset_hash": "a" * 64,
            "speaker": {"role": "owner", "display_name": "合成用户"},
            "examples": [
                {
                    "example_id": "ex-1",
                    "user": "合成示例问题" * 20,
                    "assistant": "合成示例回答" * 30,
                    "pre_tool_commentary": "我查一下。",
                },
                {
                    "example_id": "ex-2",
                    "user": "第二个合成示例问题" * 20,
                    "assistant": "第二个合成示例回答" * 20,
                },
            ],
            "examples_omitted": [],
        }
        # Seven tools, including the 10.5 KiB propose definition.
        definitions = build_catalog(include_propose=True).definitions()
        # Three pending corrections are part of the RG05/Q4 worst case (the
        # decision requires them, not an empty pending list).
        small_context = service.build_consult_context(
            namespace=NS,
            access=self.experience,
            input_refs=[ref],
            adapter_kind="direct-http",
            renderer_version="collab.v2.renderer.5",
        )
        access = ChangeAccess(
            namespace=NS,
            host_instance=HOST,
            operator="operator:synthetic",
            access_ref=ACCESS_REF,
            permissions=frozenset({"change.propose", "change.review"}),
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"cognition.change"}),
        )
        for index in range(3):
            CognitionChangeService(self.store).propose(
                access,
                context_id=small_context.context_id,
                request_id=str(uuid.uuid4()),
                bundle={
                    "operation": "cognition.change",
                    "summary": f"synthetic pending {index}",
                    "changes": [
                        {
                            "op": "propose_belief",
                            "body": {
                                "claim": f"待审偏好 {index} " + "用户刚说的纠正" * 8,
                                "claim_type": "user_preference",
                                "evidence_links": [],
                                "strength": "insufficient",
                                "strength_reason": "synthetic",
                                "inference_basis": "direct_report",
                                "premise_refs": [],
                                "alternative_group_id": None,
                                "valid_until": None,
                                "review_condition": "synthetic",
                            },
                            "scope": dict(SCOPE),
                        }
                    ],
                },
            )
        # Working set: five beliefs + three pending corrections.
        for index in range(5):
            self.store.append_record_version(
                namespace=NS,
                record_id=str(uuid.uuid4()),
                record_version=1,
                kind="belief",
                body={
                    "claim": f"合成工作集偏好 {index} " + ("需要保留的上下文" * 12),
                    "claim_type": "user_preference",
                    "evidence_links": [],
                    "strength": "insufficient",
                    "strength_reason": "synthetic seed",
                    "inference_basis": "direct_report",
                    "premise_refs": [],
                    "alternative_group_id": None,
                    "valid_until": None,
                    "review_condition": "synthetic",
                },
                status="proposed",
                schema_version="1",
                privacy_class="private",
                recorded_at=f"2026-10-17T0{index}:00:00Z",
                scope=dict(SCOPE),
            )
        if expression_material_factory is not None:
            # The factory writes the tentative records and builds the material;
            # it must run before the working set so both materials share the
            # same current state revision.
            expression_material = expression_material_factory()
        ws = build_working_set(
            self.store,
            namespace=NS,
            epistemic_access=self.epistemic,
            query_plan=BeliefCurrentQuery(
                claim_types=("user_preference",), scope=dict(SCOPE), limit=5
            ),
            experience_access=self.experience,
            belief_limit=5,
            pending_limit=3,
        )
        self.assertIsNotNone(ws)
        material = cognition_material_from_working_set(ws)
        context = service.build_consult_context(
            namespace=NS,
            access=self.experience,
            input_refs=[ref],
            adapter_kind="direct-http",
            profile_ref=None,
            history=history,
            tool_definitions=definitions,
            renderer_version="collab.v2.renderer.5",
            persona=persona,
            cognition_material=material,
            cognition_collab_refs=ws.collab_refs,
            expression_material=expression_material,
        )
        return context

    def test_worst_case_context_fits_64_kib_and_needs_more_than_24(self) -> None:
        context = self.build(65536)
        used = len(context.rendered_input.encode("utf-8"))
        document = json.loads(context.rendered_input)
        self.assertIn("cognition", document)
        self.assertIn("tools", document)
        self.assertIn("history", document)
        self.assertEqual(7, len(document["tools"]))
        self.assertIn("cognition", document)
        self.assertEqual(3, len(document["cognition"]["pending_corrections"]))
        self.assertGreater(used, 24576, "the measured worst case must need >24 KiB")
        self.assertLess(used, 65536)
        # The policy is explicit that core persona is never dropped; expression
        # examples are the first thing the renderer sheds, and the omission is
        # recorded rather than hidden.
        persona_section = document["profile"]
        self.assertIn("principles", persona_section)
        self.assertIn("identity_summary", persona_section)
        if persona_section.get("examples_omitted"):
            self.assertLessEqual(len(persona_section.get("examples") or ()), 2)
        # Nothing was silently dropped: the current input is intact.
        self.assertIn("合成当前输入", context.rendered_input)

    def test_same_build_is_refused_at_the_default_budget(self) -> None:
        with self.assertRaises(ContextTooLarge):
            self.build(24576)


if __name__ == "__main__":
    unittest.main()
