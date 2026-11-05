"""RG05: cognition candidates from a chat turn, and the turn's context.

Scripted provider only; no network, no private asset.  These tests exercise the
real v2 tool loop, the real Context service and the real governance store.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (
    REPO_ROOT / "src",
    REPO_ROOT / "tests" / "domain",
    REPO_ROOT / "tests" / "store",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import NS  # noqa: E402
from model_support import ModelPortTestCase  # noqa: E402

from quintessence_pcs.collaboration.change_service import (  # noqa: E402
    CHANGE_POLICY_REF,
    ChangeAccess,
)
from quintessence_pcs.collaboration.policy import CommitAuthority  # noqa: E402
from quintessence_pcs.conversation.orchestrator import (  # noqa: E402
    TOOL_FORMAT_REPAIR_FEEDBACK,
    ConversationOrchestrator,
    TurnLimits,
    resolve_turn_profile,
)
from quintessence_pcs.conversation.tools import (  # noqa: E402
    PROPOSE_TOOL,
    TOOL_WHITELIST,
    build_catalog,
    build_tool_runtime,
)
from quintessence_pcs.domains.cognition import BeliefCurrentQuery  # noqa: E402
from quintessence_pcs.domains.cognition.epistemic import EpistemicAccess  # noqa: E402
from quintessence_pcs.domains.cognition.query import (  # noqa: E402
    CognitionQueryService,
    QueryAccess,
)
from quintessence_pcs.domains.cognition.questions import (  # noqa: E402
    QuestionAccess,
    QuestionService,
)
from quintessence_pcs.domains.cognition.working_set import build_working_set  # noqa: E402
from quintessence_pcs.foundation.jsonutil import canonical_json_text  # noqa: E402
from quintessence_pcs.runtime import (  # noqa: E402
    ConsultSendPolicy,
    DirectHttpModel,
    cognition_material_from_working_set,
)
from quintessence_pcs.runtime.collab_protocol import (  # noqa: E402
    COLLAB_V2_PROTOCOL_VERSION,
)
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:rg05-loop"
ACCESS_REF = "authorization:rg05-loop"
POLICY_REF = "policy:collab.v1"
PRIVACY = "private"
SCOPE = {"project_id": "rg05.lab"}


class CognitionLoopTests(ModelPortTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.access = ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({PRIVACY}),
            purposes=frozenset({"consult"}),
        )
        self.policy = ConsultSendPolicy(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({PRIVACY}),
            purposes=frozenset({"consult"}),
            disclose=True,
        )
        self.model = DirectHttpModel(
            self.store,
            self.manifest,
            protocol=COLLAB_V2_PROTOCOL_VERSION,
            consult_policy=self.policy,
            request_budget_units=200_000,
            output_reserve_units=4096,
        )
        self.queries = CognitionQueryService(self.store)
        self.questions = QuestionService(self.store)
        self.change_access = ChangeAccess(
            namespace=NS,
            host_instance=HOST,
            operator="operator:rg05",
            access_ref=ACCESS_REF,
            permissions=frozenset({"change.propose", "change.review"}),
            privacy_classes=frozenset({PRIVACY}),
            purposes=frozenset({"cognition.change"}),
        )
        self.query_access = QueryAccess(
            namespace=NS,
            privacy_classes=frozenset({PRIVACY}),
            purposes=frozenset({"consult"}),
        )
        self.epistemic = EpistemicAccess(
            namespace=NS, privacy_classes=frozenset({PRIVACY})
        )
        self.question_access = QuestionAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({PRIVACY}),
            permissions=frozenset({"question.read", "question.attention"}),
            purposes=frozenset({"cognition.question"}),
        )
        self.plan = BeliefCurrentQuery(
            claim_types=("user_preference",), scope=dict(SCOPE), limit=5
        )
        self.auto_authorization_id: str | None = None
        self.change_scope = dict(SCOPE)

        def material_factory():
            ws = build_working_set(
                self.store,
                namespace=NS,
                epistemic_access=self.epistemic,
                query_plan=self.plan,
                experience_access=self.access,
                belief_limit=5,
                pending_limit=3,
            )
            if ws is None:
                return None, ()
            return cognition_material_from_working_set(ws), ws.collab_refs

        def auto_authorization(operation: str) -> str | None:
            if operation == "cognition.change":
                return self.auto_authorization_id
            return None

        def factory(context_id: str):
            return build_tool_runtime(
                self.store,
                queries=self.queries,
                questions=self.questions,
                access=self.change_access,
                query_access=self.query_access,
                question_access=self.question_access,
                epistemic_access=self.epistemic,
                change_scope=self.change_scope,
                context_id=context_id,
                auto_authorization=auto_authorization,
                transcript_access=self.access,
                transcript_host_instance=HOST,
                minimum_retention="standard",
                include_propose=True,
            )

        self.factory = factory
        self.material_factory = material_factory
        self.profile_ref = resolve_turn_profile(self.profiles, NS)
        self.conversation = self.store.create_conversation(
            namespace=NS, host_instance=HOST, mode="chat"
        )
        self.orchestrator = self.make_orchestrator()
        self._request = 0

    def make_orchestrator(self):
        return ConversationOrchestrator(
            self.store,
            context_service=self.contexts,
            model=self.model,
            protocol_version=COLLAB_V2_PROTOCOL_VERSION,
            limits=TurnLimits(),
            tool_runtime_factory=self.factory,
            cognition_material_factory=self.material_factory,
        )

    # ------------------------------------------------------------- helpers

    def seed_belief(
        self,
        claim: str,
        *,
        claim_type: str = "user_preference",
        recorded_at: str = "2026-10-17T00:00:00Z",
    ):
        basis = "observation" if claim_type == "capability" else "direct_report"
        return self.store.append_record_version(
            namespace=NS,
            record_id=str(uuid.uuid4()),
            record_version=1,
            kind="belief",
            body={
                "claim": claim,
                "claim_type": claim_type,
                "evidence_links": [],
                "strength": "insufficient",
                "strength_reason": "synthetic",
                "inference_basis": basis,
                "premise_refs": [],
                "alternative_group_id": None,
                "valid_until": None,
                "review_condition": "synthetic",
            },
            status="proposed",
            schema_version="1",
            privacy_class=PRIVACY,
            recorded_at=recorded_at,
            scope=dict(SCOPE),
        )

    def belief_body(self, claim: str) -> dict:
        return {
            "claim": claim,
            "claim_type": "user_preference",
            "evidence_links": [],
            "strength": "insufficient",
            "strength_reason": "synthetic",
            "inference_basis": "direct_report",
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": "synthetic",
        }

    def enqueue(self, payload: dict) -> None:
        self.enqueue_content(canonical_json_text(payload))

    def call(self, name: str, arguments: dict) -> dict:
        return {
            "action": "call_tool",
            "tool_call": {"name": name, "arguments": arguments},
        }

    def propose_call(
        self,
        claim: str,
        *,
        evidence: list[dict] | None = None,
        claim_type: str = "user_preference",
    ) -> dict:
        body = self.belief_body(claim)
        if evidence:
            body["evidence_links"] = evidence
        body["claim_type"] = claim_type
        if claim_type == "general_hypothesis":
            body["inference_basis"] = "inductive"
        return self.call(
            PROPOSE_TOOL,
            {
                "summary": "synthetic candidate",
                "changes": [{"op": "propose_belief", "body": body}],
            },
        )

    def send(self, text: str):
        self._request += 1
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id=f"rg05-req-{self._request}",
            text=text,
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": "chat"},
        )
        return creation, self.orchestrator.execute(
            conversation=self.conversation,
            turn=creation.turn,
            input_ref=creation.input_ref,
            profile_ref=self.profile_ref,
            access=self.access,
            retention="standard",
            policy_ref=POLICY_REF,
        )

    def snapshots(self, turn_id: str):
        rows = self.store.connection.execute(
            "SELECT sc.context_id FROM collab_stage_contexts sc "
            "JOIN collab_turn_stages st ON st.namespace = sc.namespace "
            "AND st.stage_id = sc.stage_id WHERE st.turn_id = ? "
            "ORDER BY sc.ordinal",
            (turn_id,),
        ).fetchall()
        return [self.store.load_context_snapshot(row["context_id"]) for row in rows]

    def steps(self, turn_id: str):
        return self.store.read_turn_steps(
            namespace=NS, host_instance=HOST, turn_id=turn_id, access=self.access
        ).steps

    def candidates(self):
        rows = self.store.pending_change_candidates(NS, limit=20)
        return rows

    def result(self, turn_id: str):
        return self.store.turn_result(
            namespace=NS, host_instance=HOST, turn_id=turn_id, access=self.access
        )

    def count(self, table: str) -> int:
        return int(
            self.store.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        )

    # -------------------------------------------------------------- checks

    def test_the_turn_publishes_seven_tools_and_the_working_set(self) -> None:
        belief = self.seed_belief("合成的用户偏好：回答简短")
        self.enqueue({"action": "final", "reply": "合成回答。"})
        creation, _ = self.send("你好")
        document = json.loads(self.snapshots(creation.turn.turn_id)[0].rendered_input)
        self.assertEqual(
            build_catalog(include_propose=True).names(),
            tuple(self.factory("").catalog.names()),
        )
        self.assertEqual(7, len(document["tools"]))
        self.assertIn("cognition", document)
        refs = [
            item["ref"]["record_id"]
            for item in document["cognition"]["provisional"]
        ]
        self.assertIn(belief.record_id, refs)

    def test_belief_read_then_propose_accepts_the_delivered_ref(self) -> None:
        belief = self.seed_belief("合成的能力观察：先记录", claim_type="capability")
        self.enqueue(self.call("cognition.belief.read", {"limit": 5}))
        self.enqueue(
            self.propose_call(
                "合成假设：用户偏好简短回答",
                evidence=[
                    {
                        "ref": {
                            "ref_type": "record",
                            "record_id": belief.record_id,
                            "record_version": belief.record_version,
                        },
                        "relation": "supports",
                        "origin_group_id": f"record:{belief.record_id}",
                        "applicability_reason": "synthetic",
                    }
                ],
                claim_type="general_hypothesis",
            )
        )
        self.enqueue({"action": "final", "reply": "合成回答。"})
        creation, execution = self.send("请据此形成候选")
        self.assertEqual("completed", execution.turn.state)
        steps = self.steps(creation.turn.turn_id)
        self.assertEqual(2, len(steps))
        self.assertEqual("succeeded", steps[1].step.status)
        self.assertEqual(1, len(self.candidates()))

    def test_an_unseen_ref_is_refused(self) -> None:
        self.seed_belief("合成的用户偏好")
        seen = str(uuid.uuid4())
        self.enqueue(
            self.propose_call(
                "合成假设：编造引用",
                evidence=[
                    {
                        "ref": {
                            "ref_type": "record",
                            "record_id": seen,
                            "record_version": 1,
                        },
                        "relation": "supports",
                        "origin_group_id": f"record:{seen}",
                        "applicability_reason": "synthetic",
                    }
                ],
                claim_type="general_hypothesis",
            )
        )
        self.enqueue({"action": "final", "reply": "合成回答。"})
        creation, execution = self.send("尝试编造引用")
        self.assertEqual("completed", execution.turn.state)
        steps = self.steps(creation.turn.turn_id)
        self.assertEqual("failed", steps[0].step.status)
        self.assertIn("has not returned", steps[0].result_excerpt)
        self.assertEqual((), self.candidates())

    def test_the_next_turn_starts_with_an_empty_ref_registry(self) -> None:
        belief = self.seed_belief("合成的能力观察", claim_type="capability")
        self.enqueue(self.call("cognition.belief.read", {"limit": 5}))
        self.enqueue({"action": "final", "reply": "第一轮。"})
        self.send("第一轮")
        # The second turn never reads the record, so its id must be unknown.
        self.enqueue(
            self.propose_call(
                "合成假设：跨轮引用",
                evidence=[
                    {
                        "ref": {
                            "ref_type": "record",
                            "record_id": belief.record_id,
                            "record_version": belief.record_version,
                        },
                        "relation": "supports",
                        "origin_group_id": f"record:{belief.record_id}",
                        "applicability_reason": "synthetic",
                    }
                ],
                claim_type="general_hypothesis",
            )
        )
        self.enqueue({"action": "final", "reply": "第二轮。"})
        creation, execution = self.send("第二轮")
        self.assertEqual("completed", execution.turn.state)
        self.assertEqual("failed", self.steps(creation.turn.turn_id)[0].step.status)
        self.assertEqual((), self.candidates())

    def test_identical_proposals_in_one_turn_are_deduped(self) -> None:
        self.enqueue(self.propose_call("合成偏好：简短回答"))
        self.enqueue(self.propose_call("合成偏好：简短回答"))
        self.enqueue({"action": "final", "reply": "合成回答。"})
        creation, execution = self.send("请记住偏好")
        self.assertEqual("completed", execution.turn.state)
        self.assertEqual(1, len(self.candidates()))
        steps = self.steps(creation.turn.turn_id)
        self.assertIn("deduped", steps[1].result_excerpt)

    def test_a_parse_rejection_can_be_repaired_into_the_same_propose(self) -> None:
        """One format repair may re-issue the exact tool the model attempted.

        The rejected output never started a step, so the repaired call is a
        first execution: one step, one candidate, and the host tells the model
        the previous output had no effect.
        """

        self.enqueue_content(
            canonical_json_text(
                {"action": "call_tool", "tool_call": {"name": "cognition.propose"}}
            )
        )
        self.enqueue(self.propose_call("合成偏好：简短回答"))
        self.enqueue({"action": "final", "reply": "合成回答。"})
        creation, execution = self.send("请记住偏好")
        self.assertEqual("completed", execution.turn.state)
        steps = self.steps(creation.turn.turn_id)
        self.assertEqual(1, len(steps))
        self.assertEqual("succeeded", steps[0].step.status)
        self.assertEqual(1, len(self.candidates()))
        events = self.store.read_turn_events(
            namespace=NS, host_instance=HOST, turn_id=creation.turn.turn_id
        ).events
        rejected = [event for event in events if event.kind == "tool_rejected"]
        self.assertEqual(1, len(rejected))
        self.assertEqual("parse", rejected[0].payload.get("reason"))
        self.assertNotIn("step_id", rejected[0].payload)
        self.assertEqual(
            1, len([event for event in events if event.kind == "repair_attempt"])
        )
        self.assertEqual(3, len(self.provider.requests))
        repair = json.loads(self.provider.requests[1]["body"])
        feedback = repair["messages"][-1]["content"]
        self.assertEqual(TOOL_FORMAT_REPAIR_FEEDBACK, feedback)
        self.assertIn("上次响应使用了工具标记", feedback)
        self.assertIn('{"action":"call_tool"', feedback)
        self.assertNotIn("直接输出 final", feedback)
        self.assertNotIn("cognition.propose", feedback)

    def test_two_different_claims_are_not_swallowed(self) -> None:
        self.enqueue(self.propose_call("合成偏好：简短回答"))
        self.enqueue(self.propose_call("合成偏好：先讲结论"))
        self.enqueue({"action": "final", "reply": "合成回答。"})
        creation, execution = self.send("请记住两条偏好")
        self.assertEqual("completed", execution.turn.state)
        self.assertEqual(2, len(self.candidates()))

    def test_chit_chat_creates_no_candidate(self) -> None:
        self.enqueue({"action": "final", "reply": "随便聊聊。"})
        creation, execution = self.send("今天天气不错")
        self.assertEqual("completed", execution.turn.state)
        self.assertEqual((), self.candidates())

    def grant_default(self, *, claim_types=("general_hypothesis",)):
        from quintessence_pcs.collaboration.review_policy import (
            BASE_POLICY_ID,
            BASE_POLICY_VERSION,
        )
        from quintessence_pcs.store.governance import (
            CHANGE_OPERATION,
            GovernanceAccess,
            GovernanceStore,
        )

        authorization = GovernanceStore(self.store).grant_commit_authorization(
            GovernanceAccess(
                namespace=NS,
                host_instance=HOST,
                operator="operator:rg05",
                access_ref=ACCESS_REF,
                permissions=frozenset({"governance.admin"}),
            ),
            authorization_id=str(uuid.uuid4()),
            host_instance=HOST,
            access_ref=ACCESS_REF,
            operations=(CHANGE_OPERATION,),
            claim_types=tuple(claim_types),
            scope=dict(SCOPE),
            max_impact="E1",
            max_changes_per_bundle=2,
            max_targets_per_bundle=2,
            policy_id=BASE_POLICY_ID,
            policy_version=BASE_POLICY_VERSION,
            authorization_source="host_config",
            issued_at="2026-10-17T00:00:00Z",
            issued_by="operator:rg05",
        )
        self.auto_authorization_id = authorization.authorization_id
        self.change_access = ChangeAccess(
            namespace=NS,
            host_instance=HOST,
            operator="operator:rg05",
            access_ref=ACCESS_REF,
            permissions=frozenset(
                {"change.propose", "change.review", "change.commit"}
            ),
            privacy_classes=frozenset({PRIVACY}),
            purposes=frozenset({"cognition.change"}),
        )
        return authorization

    def test_default_grant_commits_a_safe_candidate_without_a_human(self) -> None:
        self.grant_default(claim_types=("general_hypothesis",))
        self.enqueue(
            self.propose_call("合成假设偏好", claim_type="general_hypothesis")
        )
        self.enqueue({"action": "final", "reply": "已记录。"})
        creation, execution = self.send("请记住这条")
        self.assertEqual("completed", execution.turn.state)
        rows = self.store.connection.execute(
            "SELECT status FROM collab_artifacts WHERE kind = 'candidate'"
        ).fetchall()
        self.assertEqual(["committed"], [row["status"] for row in rows])

    def test_a_user_preference_stays_manual_with_the_default_grant(self) -> None:
        from quintessence_pcs.store.governance import GovernanceStore

        # Supplement 04: the grant *does* cover user_preference; the E2 impact
        # (no valid binding) is what keeps it manual.
        self.grant_default(
            claim_types=(
                "capability",
                "environment_state",
                "general_hypothesis",
                "user_preference",
            )
        )
        self.enqueue(self.propose_call("合成偏好：简短回答"))
        self.enqueue({"action": "final", "reply": "已记下。"})
        creation, execution = self.send("请记住偏好")
        self.assertEqual("completed", execution.turn.state)
        artifact = self.store.connection.execute(
            "SELECT * FROM collab_artifacts WHERE kind = 'candidate'"
        ).fetchone()
        self.assertEqual("pending", artifact["status"])
        decision = GovernanceStore(self.store).latest_decision(
            NS, artifact["artifact_id"], 1
        )
        self.assertEqual("awaiting_human", decision.decision)
        self.assertTrue(decision.requires_human)
        # A review that already says "needs a human" must not be followed by a
        # doomed auto-authorization attempt: that appended a second decision
        # row for the same candidate and made the audit trail look ambiguous.
        rows = self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM cognition_review_decisions "
            "WHERE candidate_id = ?",
            (artifact["artifact_id"],),
        ).fetchone()
        self.assertEqual(1, rows["n"])

    def test_a_claim_type_outside_the_grant_is_not_auto_committed(self) -> None:
        self.grant_default(claim_types=("capability",))
        self.enqueue(
            self.propose_call("合成假设偏好", claim_type="general_hypothesis")
        )
        self.enqueue({"action": "final", "reply": "已记下。"})
        creation, execution = self.send("请记住这条")
        self.assertEqual("completed", execution.turn.state)
        artifact = self.store.connection.execute(
            "SELECT * FROM collab_artifacts WHERE kind = 'candidate'"
        ).fetchone()
        self.assertEqual("pending", artifact["status"])

    def test_candidate_is_not_an_experience_source(self) -> None:
        self.enqueue(self.propose_call("合成偏好：简短回答"))
        self.enqueue({"action": "final", "reply": "合成回答。"})
        creation, execution = self.send("请记住偏好")
        self.assertEqual("completed", execution.turn.state)
        refs = self.store.turn_experience_refs(
            self.access, namespace=NS, turn_id=creation.turn.turn_id
        )
        kinds = {ref.get("ref_type") for ref in refs}
        self.assertNotIn("candidate", kinds)
        for ref in refs:
            if ref.get("ref_type") == "collab_artifact":
                artifact = self.store.load_artifact(
                    namespace=NS,
                    artifact_id=ref["artifact_id"],
                    artifact_version=ref["artifact_version"],
                )
                self.assertNotEqual("candidate", artifact.kind)

    def test_candidate_interaction_is_separate_and_isolated(self) -> None:
        from quintessence_pcs.collaboration.change_service import CHANGE_POLICY_REF

        claim_text = "合成偏好：回答尽量简短"
        self.enqueue(self.propose_call(claim_text))
        self.enqueue({"action": "final", "reply": "合成回答，不是候选正文。"})
        creation, execution = self.send("请记住偏好")
        self.assertEqual("completed", execution.turn.state)
        turn_id = creation.turn.turn_id

        # 1. The candidate is an artifact carried by its own consult interaction.
        candidate = self.store.connection.execute(
            "SELECT * FROM collab_artifacts WHERE kind = 'candidate'"
        ).fetchone()
        self.assertIsNotNone(candidate)
        interaction = self.store.connection.execute(
            "SELECT * FROM collab_interactions WHERE interaction_id = ?",
            (candidate["interaction_id"],),
        ).fetchone()
        self.assertEqual("consult", interaction["mode"])
        self.assertIsNone(interaction["trigger_ref_json"])
        self.assertEqual(0, interaction["work_less"])
        self.assertEqual(CHANGE_POLICY_REF, interaction["policy_ref"])
        turn_interaction = self.store.connection.execute(
            "SELECT interaction_id FROM collab_interactions WHERE policy_ref = ? "
            "AND context_id = ?",
            (POLICY_REF, self.snapshots(turn_id)[0].context_id),
        ).fetchone()
        self.assertIsNotNone(turn_interaction)
        self.assertNotEqual(
            interaction["interaction_id"], turn_interaction["interaction_id"]
        )

        # 2. It is never an assistant message or the final reply.
        kinds = [
            row["kind"]
            for row in self.store.connection.execute(
                "SELECT kind FROM collab_turn_messages WHERE turn_id = ? ORDER BY seq",
                (turn_id,),
            ).fetchall()
        ]
        self.assertEqual(["user", "commentary", "final"], kinds)
        result = self.result(turn_id)
        self.assertEqual("合成回答，不是候选正文。", result.final.text)
        self.assertIsNone(result.wait_question)
        commentary = self.store.connection.execute(
            "SELECT message_id FROM collab_turn_messages "
            "WHERE turn_id = ? AND kind = 'commentary'",
            (turn_id,),
        ).fetchone()
        self.assertIsNotNone(commentary)
        event_rows = self.store.connection.execute(
            "SELECT payload_json FROM collab_turn_events "
            "WHERE turn_id = ? AND kind = 'message_published'",
            (turn_id,),
        ).fetchall()
        sources = [
            json.loads(row["payload_json"])
            for row in event_rows
            if json.loads(row["payload_json"]).get("kind") == "commentary"
        ]
        self.assertEqual(1, len(sources))
        self.assertEqual("host_fallback", sources[0].get("commentary_source"))

        # 3. No memory_delivery row names it (no bridge configured here).
        self.assertEqual(
            0, self.count("memory_delivery")
        )
        # 4. The lifecycle starts pending and is visible to the review read.
        rows = self.store.pending_change_candidates(NS, limit=20)
        self.assertEqual([candidate["artifact_id"]], [row["artifact_id"] for row in rows])

    # ------------------------------------------------- RG08 R-D input evidence

    def input_link(self, input_ref: dict, *, relation: str = "supports") -> dict:
        return {
            "ref": {
                "ref_type": "collab_input",
                "input_id": input_ref["input_id"],
                "content_version": input_ref["content_version"],
            },
            "relation": relation,
            "origin_group_id": f"collab_input:{input_ref['input_id']}",
            "applicability_reason": "synthetic current user statement",
        }

    def run_propose_turn(
        self,
        text: str,
        *,
        claim: str,
        claim_type: str = "user_preference",
        evidence: list[dict] | None = None,
        strength: str = "limited",
    ):
        """Register a turn, then enqueue a propose call built for its own input."""

        self._request += 1
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id=f"rg08-d1-{self._request}",
            text=text,
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": "chat"},
        )
        body = self.belief_body(claim)
        body["claim_type"] = claim_type
        body["strength"] = strength
        if claim_type == "general_hypothesis":
            body["inference_basis"] = "inductive"
        if evidence is not None:
            body["evidence_links"] = evidence
        self.enqueue(
            self.call(
                PROPOSE_TOOL,
                {
                    "summary": "synthetic candidate",
                    "changes": [{"op": "propose_belief", "body": body}],
                },
            )
        )
        self.enqueue({"action": "final", "reply": "合成回答。"})
        execution = self.orchestrator.execute(
            conversation=self.conversation,
            turn=creation.turn,
            input_ref=creation.input_ref,
            profile_ref=self.profile_ref,
            access=self.access,
            retention="standard",
            policy_ref=POLICY_REF,
        )
        return creation, execution

    def candidate_body(self):
        rows = self.candidates()
        self.assertEqual(1, len(rows), rows)
        artifact = self.store.load_artifact(
            namespace=NS,
            artifact_id=str(rows[0]["artifact_id"]),
            artifact_version=int(rows[0]["artifact_version"]),
        )
        changes = artifact.proposal["changes"]
        return changes[0]["body"]

    def test_the_current_turn_input_is_citable_user_preference_evidence(self) -> None:
        self._request += 1
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id="rg08-d1-current",
            text="我喜欢红色。",
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": "chat"},
        )
        link = self.input_link(creation.input_ref)
        body = self.belief_body("用户喜欢红色。")
        body["strength"] = "limited"
        body["evidence_links"] = [link]
        self.enqueue(
            self.call(
                PROPOSE_TOOL,
                {"summary": "colour preference", "changes": [{"op": "propose_belief", "body": body}]},
            )
        )
        self.enqueue({"action": "final", "reply": "合成回答。"})
        execution = self.orchestrator.execute(
            conversation=self.conversation,
            turn=creation.turn,
            input_ref=creation.input_ref,
            profile_ref=self.profile_ref,
            access=self.access,
            retention="standard",
            policy_ref=POLICY_REF,
        )
        self.assertEqual("completed", execution.turn.state)
        steps = self.steps(creation.turn.turn_id)
        self.assertEqual(1, len(steps))
        self.assertEqual("succeeded", steps[0].step.status, steps[0].result_excerpt)
        stored = self.candidate_body()
        ref = stored["evidence_links"][0]["ref"]
        self.assertEqual("collab_input", ref["ref_type"])
        self.assertEqual(NS, ref["namespace"])
        self.assertEqual(creation.input_ref["input_id"], ref["input_id"])
        self.assertEqual(1, ref["content_version"])
        self.assertEqual([], [row for row in self.store.current_versions_of_kind(NS, "belief")])

    def test_a_previous_turn_input_is_not_citable(self) -> None:
        self.enqueue({"action": "final", "reply": "第一轮。"})
        first, _ = self.send("我喜欢红色。")
        self._request += 1
        second = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id="rg08-d1-previous",
            text="天气不错。",
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": "chat"},
        )
        body = self.belief_body("用户喜欢红色。")
        body["strength"] = "limited"
        body["evidence_links"] = [self.input_link(first.input_ref)]
        self.enqueue(
            self.call(
                PROPOSE_TOOL,
                {"summary": "stale input", "changes": [{"op": "propose_belief", "body": body}]},
            )
        )
        self.enqueue({"action": "final", "reply": "合成回答。"})
        execution = self.orchestrator.execute(
            conversation=self.conversation,
            turn=second.turn,
            input_ref=second.input_ref,
            profile_ref=self.profile_ref,
            access=self.access,
            retention="standard",
            policy_ref=POLICY_REF,
        )
        self.assertEqual("completed", execution.turn.state)
        step = self.steps(second.turn.turn_id)[0]
        self.assertEqual("failed", step.step.status, step.result_excerpt)
        self.assertIn("did not render", step.result_excerpt)
        self.assertEqual((), self.candidates())

    def test_a_wrong_input_version_is_refused(self) -> None:
        self._request += 1
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id="rg08-d1-version",
            text="我喜欢红色。",
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": "chat"},
        )
        link = self.input_link(creation.input_ref)
        link["ref"]["content_version"] = 7
        body = self.belief_body("用户喜欢红色。")
        body["strength"] = "limited"
        body["evidence_links"] = [link]
        self.enqueue(
            self.call(
                PROPOSE_TOOL,
                {"summary": "wrong version", "changes": [{"op": "propose_belief", "body": body}]},
            )
        )
        self.enqueue({"action": "final", "reply": "合成回答。"})
        execution = self.orchestrator.execute(
            conversation=self.conversation,
            turn=creation.turn,
            input_ref=creation.input_ref,
            profile_ref=self.profile_ref,
            access=self.access,
            retention="standard",
            policy_ref=POLICY_REF,
        )
        self.assertEqual("completed", execution.turn.state)
        step = self.steps(creation.turn.turn_id)[0]
        self.assertEqual("failed", step.step.status, step.result_excerpt)
        self.assertIn("did not render", step.result_excerpt)
        self.assertEqual((), self.candidates())

    def test_a_question_input_cannot_support_a_preference(self) -> None:
        self._request += 1
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id="rg08-d1-question",
            text="你记得我喜欢什么颜色吗？",
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": "chat"},
        )
        body = self.belief_body("用户喜欢红色。")
        body["strength"] = "limited"
        body["evidence_links"] = [self.input_link(creation.input_ref)]
        self.enqueue(
            self.call(
                PROPOSE_TOOL,
                {"summary": "question as evidence", "changes": [{"op": "propose_belief", "body": body}]},
            )
        )
        self.enqueue({"action": "final", "reply": "合成回答。"})
        execution = self.orchestrator.execute(
            conversation=self.conversation,
            turn=creation.turn,
            input_ref=creation.input_ref,
            profile_ref=self.profile_ref,
            access=self.access,
            retention="standard",
            policy_ref=POLICY_REF,
        )
        self.assertEqual("completed", execution.turn.state)
        # J11 consolidation: the host no longer vetoes the candidate because the
        # cited sentence is a question.  The proposal is filed as an ordinary
        # candidate and Review decides; nothing is committed on its own.
        step = self.steps(creation.turn.turn_id)[0]
        self.assertEqual("succeeded", step.step.status, step.result_excerpt)
        self.assertEqual(1, len(self.candidates()))
        self.assertIn('"impact":"E2"', step.result_excerpt)
        self.assertIn('"state":"AWAITING_HUMAN"', step.result_excerpt)

    def test_a_third_party_input_cannot_support_a_preference(self) -> None:
        self._request += 1
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id="rg08-d1-third-party",
            text="我朋友说他喜欢蓝色。",
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": "chat"},
        )
        body = self.belief_body("用户喜欢蓝色。")
        body["strength"] = "limited"
        body["evidence_links"] = [self.input_link(creation.input_ref)]
        self.enqueue(
            self.call(
                PROPOSE_TOOL,
                {"summary": "third party as evidence", "changes": [{"op": "propose_belief", "body": body}]},
            )
        )
        self.enqueue({"action": "final", "reply": "合成回答。"})
        execution = self.orchestrator.execute(
            conversation=self.conversation,
            turn=creation.turn,
            input_ref=creation.input_ref,
            profile_ref=self.profile_ref,
            access=self.access,
            retention="standard",
            policy_ref=POLICY_REF,
        )
        self.assertEqual("completed", execution.turn.state)
        # J11 consolidation: who said what is the model's reading and Review's
        # decision; the host only checks that the cited input is the user's own
        # statement and is readable.
        step = self.steps(creation.turn.turn_id)[0]
        self.assertEqual("succeeded", step.step.status, step.result_excerpt)
        self.assertEqual(1, len(self.candidates()))
        self.assertIn('"impact":"E2"', step.result_excerpt)
        self.assertIn('"state":"AWAITING_HUMAN"', step.result_excerpt)

    def test_no_evidence_is_never_filled_in_automatically(self) -> None:
        creation, execution = self.run_propose_turn(
            "我喜欢红色。", claim="用户喜欢红色。", evidence=[]
        )
        self.assertEqual("completed", execution.turn.state)
        self.assertEqual("succeeded", self.steps(creation.turn.turn_id)[0].step.status)
        stored = self.candidate_body()
        self.assertEqual([], stored["evidence_links"])

    def test_a_general_hypothesis_may_cite_the_current_input(self) -> None:
        self._request += 1
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id="rg08-d1-hypothesis",
            text="我最近经常熬夜。",
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": "chat"},
        )
        body = self.belief_body("用户近期的作息可能偏晚。")
        body["claim_type"] = "general_hypothesis"
        body["inference_basis"] = "inductive"
        body["strength"] = "limited"
        body["evidence_links"] = [self.input_link(creation.input_ref)]
        self.enqueue(
            self.call(
                PROPOSE_TOOL,
                {"summary": "bounded hypothesis", "changes": [{"op": "propose_belief", "body": body}]},
            )
        )
        self.enqueue({"action": "final", "reply": "合成回答。"})
        execution = self.orchestrator.execute(
            conversation=self.conversation,
            turn=creation.turn,
            input_ref=creation.input_ref,
            profile_ref=self.profile_ref,
            access=self.access,
            retention="standard",
            policy_ref=POLICY_REF,
        )
        self.assertEqual("completed", execution.turn.state)
        step = self.steps(creation.turn.turn_id)[0]
        self.assertEqual("succeeded", step.step.status, step.result_excerpt)
        stored = self.candidate_body()
        self.assertEqual("general_hypothesis", stored["claim_type"])
        self.assertEqual("collab_input", stored["evidence_links"][0]["ref"]["ref_type"])

    def send_with_input_evidence(
        self,
        text: str,
        *,
        claim: str,
        claim_type: str = "user_preference",
        relation: str = "supports",
    ):
        self._request += 1
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id=f"rg08-d1-send-{self._request}",
            text=text,
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": "chat"},
        )
        body = self.belief_body(claim)
        body["claim_type"] = claim_type
        body["strength"] = "limited"
        if claim_type == "general_hypothesis":
            body["inference_basis"] = "inductive"
        body["evidence_links"] = [self.input_link(creation.input_ref, relation=relation)]
        self.enqueue(
            self.call(
                PROPOSE_TOOL,
                {
                    "summary": "synthetic input evidence",
                    "changes": [{"op": "propose_belief", "body": body}],
                },
            )
        )
        self.enqueue({"action": "final", "reply": "合成回答。"})
        execution = self.orchestrator.execute(
            conversation=self.conversation,
            turn=creation.turn,
            input_ref=creation.input_ref,
            profile_ref=self.profile_ref,
            access=self.access,
            retention="standard",
            policy_ref=POLICY_REF,
        )
        return creation, execution

    def test_the_statement_guard_is_narrow_to_user_preference(self) -> None:
        # The D1 guard exists for "the user said this about themselves".  A
        # question input may still be material for a hypothesis; the existing
        # source-qualification/Review rules decide whether it can be used.
        creation, execution = self.send_with_input_evidence(
            "你记得我喜欢什么颜色吗？",
            claim="用户可能对颜色有偏好。",
            claim_type="general_hypothesis",
        )
        self.assertEqual("completed", execution.turn.state)
        step = self.steps(creation.turn.turn_id)[0]
        self.assertEqual("succeeded", step.step.status, step.result_excerpt)
        stored = self.candidate_body()
        self.assertEqual("general_hypothesis", stored["claim_type"])
        self.assertEqual("collab_input", stored["evidence_links"][0]["ref"]["ref_type"])

    def test_user_preference_guarded_inputs_become_reviewed_candidates(self) -> None:
        cases = {
            "“我喜欢红色”": "quoted",
            "如果我喜欢红色呢": "hypothetical_or_question",
            "我可能喜欢红色": "hedged",
        }
        for text, label in cases.items():
            creation, execution = self.send_with_input_evidence(
                text, claim="用户喜欢红色。"
            )
            self.assertEqual("completed", execution.turn.state)
            step = self.steps(creation.turn.turn_id)[0]
            # J11 consolidation: a quotation, a hypothetical or a hedged guess is
            # not a licence to commit, but it is not a reason to refuse the
            # candidate either.  It reaches Review at E2 like any other proposal,
            # and the host records the cited input as the user's own statement.
            self.assertEqual("succeeded", step.step.status, f"{label}: {step.result_excerpt}")
        self.assertEqual(3, len(self.candidates()))


class CognitionSchemaConsistencyTests(unittest.TestCase):
    """RG05 clarification 01: the model-visible surface matches what is stored."""

    def test_the_propose_schema_advertises_only_real_claim_types(self) -> None:
        from quintessence_pcs.domains.cognition.beliefs import (
            CLAIM_TYPES,
            CLAIM_TYPE_DESCRIPTIONS,
        )

        self.assertEqual(set(CLAIM_TYPES), set(CLAIM_TYPE_DESCRIPTIONS))
        self.assertNotIn("project_decision", CLAIM_TYPE_DESCRIPTIONS)
        definition = build_catalog(include_propose=True).definitions()
        propose = next(
            item
            for item in definition
            if item["function"]["name"] == PROPOSE_TOOL
        )
        text = json.dumps(propose, ensure_ascii=False)
        self.assertNotIn("project_decision", text)
        self.assertIn("user_preference", text)


if __name__ == "__main__":
    unittest.main()
