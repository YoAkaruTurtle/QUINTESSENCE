"""RG07: the off/on expression stage behind the real v2 chat loop.

The real ``DirectHttpModel`` pointed at the loopback scripted provider, the real
store and the real Context service are used throughout; no network or private
data is involved.  The tests prove the S01-S06 acceptance behaviour: off stays
single-stage, on produces a second generation with its own persisted Context,
drift is repaired once and then falls back to the draft, budget/wall/wait/auto
record real skips, pending cognition never becomes "remembered", and the final
artifact keeps both stages' sources.
"""

from __future__ import annotations

import json
import sys
import unittest
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

from quintessence_pcs.collaboration.consult import _artifact_sources  # noqa: E402
from quintessence_pcs.conversation.orchestrator import (  # noqa: E402
    ConversationOrchestrator,
    TurnLimits,
    resolve_turn_profile,
)
from quintessence_pcs.conversation.sdrpe import SdrpePlan  # noqa: E402
from quintessence_pcs.runtime import ConsultSendPolicy, DirectHttpModel  # noqa: E402
from quintessence_pcs.runtime.collab_protocol import (  # noqa: E402
    COLLAB_V2_PROTOCOL_VERSION,
)
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:rg07-expression"
ACCESS_REF = "authorization:rg07-expression"
POLICY_REF = "policy:collab.v1"
PRIVACY = "private"


def final(reply: str) -> dict:
    return {"action": "final", "reply": reply}


class FakeClock:
    """A monotonic clock driven by an explicit list (last value repeats)."""

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)
        self._last = self._values[0] if self._values else 0.0

    def __call__(self) -> float:
        if self._values:
            self._last = self._values.pop(0)
        return self._last


class ExpressionStageTests(ModelPortTestCase):
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
        self.profile_ref = resolve_turn_profile(self.profiles, NS)
        self.conversation = self.store.create_conversation(
            namespace=NS, host_instance=HOST, mode="chat"
        )
        self._request = 0

    # ------------------------------------------------------------- helpers

    def orchestrator(self, **overrides) -> ConversationOrchestrator:
        values = {
            "store": self.store,
            "context_service": self.contexts,
            "model": self.model,
            "protocol_version": COLLAB_V2_PROTOCOL_VERSION,
            "limits": TurnLimits(),
        }
        values.update(overrides)
        return ConversationOrchestrator(**values)

    def send(
        self,
        text: str,
        *,
        sdrpe: str = "on",
        orchestrator: ConversationOrchestrator | None = None,
        conversation=None,
    ):
        self._request += 1
        plan = SdrpePlan.resolve(sdrpe, protocol_version=COLLAB_V2_PROTOCOL_VERSION)
        target = conversation or self.conversation
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=target.conversation_id,
            client_request_id=f"rg07-expr-{self._request}",
            text=text,
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={
                "mode": target.mode,
                "protocol_version": COLLAB_V2_PROTOCOL_VERSION,
                "sdrpe": plan.to_strategy(),
            },
        )
        execution = (orchestrator or self.orchestrator()).execute(
            conversation=target,
            turn=creation.turn,
            input_ref=creation.input_ref,
            profile_ref=self.profile_ref,
            access=self.access,
            retention="standard",
            policy_ref=POLICY_REF,
        )
        return creation, execution

    def work_conversation(self):
        return self.store.create_conversation(
            namespace=NS, host_instance=HOST, mode="work"
        )

    def stages(self, turn_id: str):
        return self.store.stages(namespace=NS, turn_id=turn_id)

    def events(self, turn_id: str):
        page = self.store.read_turn_events(
            namespace=NS, host_instance=HOST, turn_id=turn_id
        )
        return [(row.kind, dict(row.payload)) for row in page.events]

    def final_text(self, turn_id: str):
        view = self.store.turn_result(
            namespace=NS, host_instance=HOST, turn_id=turn_id, access=self.access
        )
        return None if view.final is None else view.final.text

    def context_rows(self, turn_id: str):
        return self.store.connection.execute(
            "SELECT sc.context_id, sc.purpose FROM collab_stage_contexts sc "
            "JOIN collab_turn_stages st ON st.namespace = sc.namespace "
            "AND st.stage_id = sc.stage_id WHERE st.turn_id = ? "
            "ORDER BY sc.ordinal",
            (turn_id,),
        ).fetchall()

    def snapshots(self, turn_id: str):
        return [
            self.store.load_context_snapshot(row["context_id"])
            for row in self.context_rows(turn_id)
        ]

    def request_bodies(self):
        return [json.loads(item["body"].decode("utf-8")) for item in self.provider.requests]

    def last_user_message(self, index: int = -1) -> str:
        body = self.request_bodies()[index]
        return body["messages"][1]["content"]

    def published_artifact(self, turn_id: str):
        view = self.store.turn_result(
            namespace=NS, host_instance=HOST, turn_id=turn_id, access=self.access
        )
        message_id = view.final.message_id
        row = self.store.connection.execute(
            "SELECT artifact_id, artifact_version FROM collab_turn_messages "
            "WHERE namespace = ? AND message_id = ?",
            (NS, message_id),
        ).fetchone()
        return self.store.load_artifact(
            namespace=NS,
            artifact_id=row["artifact_id"],
            artifact_version=row["artifact_version"],
        )

    # ------------------------------------------------------------- S01

    def test_off_publishes_explicit_content_bubbles_once(self) -> None:
        parts = ["第一句。", "第二句。"]
        self.enqueue_content(
            json.dumps({"action": "final", "messages": parts}, ensure_ascii=False)
        )
        creation, execution = self.send("分两段说。", sdrpe="off")
        turn_id = creation.turn.turn_id
        self.assertEqual(1, len(self.provider.requests))
        view = self.store.turn_result(
            namespace=NS, host_instance=HOST, turn_id=turn_id, access=self.access
        )
        self.assertEqual("第一句。\n\n第二句。", view.final.text)
        self.assertEqual(tuple(parts), view.final.messages)
        self.assertEqual(
            ["content"], [stage.kind for stage in self.stages(turn_id)]
        )
        self.assertEqual("completed", execution.turn.state)

    def test_on_expression_decides_the_final_bubbles(self) -> None:
        self.enqueue_content(
            json.dumps(
                {"action": "final", "messages": ["草稿甲。", "草稿乙。"]},
                ensure_ascii=False,
            )
        )
        self.enqueue_content(
            json.dumps(
                {"action": "final", "messages": ["表达甲。", "表达乙。"]},
                ensure_ascii=False,
            )
        )
        creation, execution = self.send("按你的方式说。", sdrpe="on")
        turn_id = creation.turn.turn_id
        self.assertEqual(2, len(self.provider.requests))
        view = self.store.turn_result(
            namespace=NS, host_instance=HOST, turn_id=turn_id, access=self.access
        )
        self.assertEqual("表达甲。\n\n表达乙。", view.final.text)
        self.assertEqual(("表达甲。", "表达乙。"), view.final.messages)
        self.assertEqual("completed", execution.turn.state)
        expression_document = json.loads(self.snapshots(turn_id)[1].rendered_input)
        self.assertEqual(
            "草稿甲。\n\n草稿乙。",
            expression_document["content_handoff"]["public_draft"],
        )

    def test_on_expression_single_reply_overrides_content_bubbles(self) -> None:
        self.enqueue_content(
            json.dumps(
                {"action": "final", "messages": ["草稿甲。", "草稿乙。"]},
                ensure_ascii=False,
            )
        )
        self.enqueue_content(json.dumps(final("合成一条最终回答。")))
        creation, _ = self.send("合成一条。", sdrpe="on")
        view = self.store.turn_result(
            namespace=NS,
            host_instance=HOST,
            turn_id=creation.turn.turn_id,
            access=self.access,
        )
        self.assertEqual("合成一条最终回答。", view.final.text)
        self.assertEqual((), view.final.messages)

    def test_off_publishes_once_without_an_expression_stage(self) -> None:
        self.enqueue_content(json.dumps(final("只答一次。")))
        creation, execution = self.send("普通问题。", sdrpe="off")
        self.assertEqual(1, len(self.provider.requests))
        self.assertEqual("只答一次。", self.final_text(creation.turn.turn_id))
        self.assertEqual(
            ["content"], [stage.kind for stage in self.stages(creation.turn.turn_id)]
        )
        kinds = [kind for kind, _ in self.events(creation.turn.turn_id)]
        self.assertNotIn("stage_skipped", kinds)
        self.assertNotIn("expression_checked", kinds)
        self.assertEqual("completed", execution.turn.state)

    def test_on_runs_a_second_generation_with_its_own_context(self) -> None:
        self.enqueue_content(json.dumps(final("折腾半天，什么都没落下来。")))
        self.enqueue_content(json.dumps(final("折腾半天，什么也没落下来。")))
        creation, execution = self.send("今天又折腾了半天。", sdrpe="on")
        turn_id = creation.turn.turn_id
        self.assertEqual(2, len(self.provider.requests))
        self.assertEqual("completed", execution.turn.state)
        self.assertEqual("折腾半天，什么也没落下来。", self.final_text(turn_id))
        stages = self.stages(turn_id)
        self.assertEqual(["content", "expression"], [item.kind for item in stages])
        self.assertEqual(["succeeded", "succeeded"], [item.status for item in stages])
        rows = self.context_rows(turn_id)
        self.assertEqual(
            ["content_initial", "expression"], [row["purpose"] for row in rows]
        )
        content_snapshot, expression_snapshot = self.snapshots(turn_id)
        content_document = json.loads(content_snapshot.rendered_input)
        expression_document = json.loads(expression_snapshot.rendered_input)
        self.assertNotIn("content_handoff", content_document)
        self.assertEqual(
            "折腾半天，什么都没落下来。",
            expression_document["content_handoff"]["public_draft"],
        )
        self.assertEqual(0, expression_document["expression_task"]["pending_cognition_changes"])
        self.assertNotIn("tools", expression_document)
        self.assertNotIn("cognition", expression_document)
        self.assertNotIn("tool_results", expression_document)
        # The second request really carried that persisted Context.
        self.assertEqual(
            expression_snapshot.rendered_input, self.last_user_message()
        )
        self.assertEqual(
            "collab.v2.renderer.8", expression_snapshot.renderer_version
        )

    # ------------------------------------------------------------- S02

    def test_drift_is_repaired_once(self) -> None:
        self.enqueue_content(json.dumps(final("记录里有 17 条待办。")))
        self.enqueue_content(json.dumps(final("记录里有 43 条待办。")))
        self.enqueue_content(json.dumps(final("记录里有 17 条待办，都是旧的。")))
        creation, _ = self.send("记录里还有多少条？", sdrpe="on")
        turn_id = creation.turn.turn_id
        self.assertEqual(3, len(self.provider.requests))
        self.assertEqual("记录里有 17 条待办，都是旧的。", self.final_text(turn_id))
        self.assertEqual(
            ["succeeded", "succeeded"],
            [stage.status for stage in self.stages(turn_id)],
        )
        repair = self.request_bodies()[2]["messages"]
        self.assertEqual("assistant", repair[2]["role"])
        self.assertIn("new_numbers", repair[3]["content"])

    def test_drift_that_persists_falls_back_to_the_draft(self) -> None:
        self.enqueue_content(json.dumps(final("记录里有 17 条待办。")))
        self.enqueue_content(json.dumps(final("记录里有 43 条待办。")))
        self.enqueue_content(json.dumps(final("记录里有 99 条待办。")))
        creation, execution = self.send("记录里还有多少条？", sdrpe="on")
        turn_id = creation.turn.turn_id
        self.assertEqual(3, len(self.provider.requests))
        self.assertEqual("记录里有 17 条待办。", self.final_text(turn_id))
        self.assertEqual("completed", execution.turn.state)
        stages = self.stages(turn_id)
        self.assertEqual(["succeeded", "failed"], [item.status for item in stages])
        kinds = [kind for kind, _ in self.events(turn_id)]
        self.assertIn("stage_failed", kinds)
        self.assertNotIn("expression_checked", kinds)
        failed = [payload for kind, payload in self.events(turn_id) if kind == "stage_failed"]
        self.assertEqual("expression", failed[0]["stage"])
        self.assertIn("new_numbers", failed[0]["reason"])

    # ------------------------------------------------------------- S03

    def test_invalid_expression_output_falls_back(self) -> None:
        self.enqueue_content(json.dumps(final("草稿在这里。")))
        self.enqueue_content("not json")
        self.enqueue_content("still not json")
        creation, execution = self.send("随便问。", sdrpe="on")
        turn_id = creation.turn.turn_id
        self.assertEqual("草稿在这里。", self.final_text(turn_id))
        self.assertEqual("completed", execution.turn.state)
        self.assertEqual(
            ["succeeded", "failed"], [stage.status for stage in self.stages(turn_id)]
        )

    def test_wait_question_skips_the_expression_stage(self) -> None:
        self.enqueue_content(
            json.dumps({"action": "wait", "wait_question": "你指哪一部分？"})
        )
        creation, execution = self.send("这个地方没懂。", sdrpe="on")
        turn_id = creation.turn.turn_id
        self.assertEqual(1, len(self.provider.requests))
        self.assertEqual("waiting", execution.turn.state)
        stages = self.stages(turn_id)
        self.assertEqual(["content", "expression"], [item.kind for item in stages])
        self.assertEqual(["succeeded", "skipped"], [item.status for item in stages])
        self.assertEqual("wait_question", stages[1].skip_reason)

    def test_no_reserve_skips_before_the_second_request(self) -> None:
        self.enqueue_content(json.dumps(final("一次就好。")))
        creation, _ = self.send(
            "短问题。",
            sdrpe="on",
            orchestrator=self.orchestrator(
                limits=TurnLimits(max_model_requests=1)
            ),
        )
        turn_id = creation.turn.turn_id
        self.assertEqual(1, len(self.provider.requests))
        stages = self.stages(turn_id)
        self.assertEqual("skipped", stages[1].status)
        self.assertEqual("budget_no_reserve", stages[1].skip_reason)

    def test_wall_clock_gate_skips_the_stage(self) -> None:
        self.enqueue_content(json.dumps(final("先回答。")))
        clock = FakeClock([0.0, 999.0, 999.0, 999.0])
        creation, _ = self.send(
            "问题。", sdrpe="on", orchestrator=self.orchestrator(clock=clock)
        )
        turn_id = creation.turn.turn_id
        self.assertEqual(1, len(self.provider.requests))
        stages = self.stages(turn_id)
        self.assertEqual("skipped", stages[1].status)
        self.assertEqual("wall_clock", stages[1].skip_reason)

    def test_cancel_between_stages_never_publishes(self) -> None:
        self.enqueue_content(json.dumps(final("会被取消的草稿。")))
        creation, execution = self.send(
            "取消测试。",
            sdrpe="on",
            orchestrator=self.orchestrator(
                cancel_requested=lambda _turn_id: True
            ),
        )
        turn_id = creation.turn.turn_id
        self.assertEqual("cancelled", execution.turn.state)
        self.assertIsNone(self.final_text(turn_id))
        self.assertEqual(1, len(self.provider.requests))

    # ------------------------------------------------------------- S05

    def test_natural_rewrite_is_not_forced_back_to_the_draft(self) -> None:
        draft = "折腾半天什么都没落下来，先歇一会儿吧。想说说今天忙了些什么吗？"
        rewritten = "折腾半天，什么也没落下来。要不要说说今天忙了些什么？"
        self.enqueue_content(json.dumps(final(draft)))
        self.enqueue_content(json.dumps(final(rewritten)))
        creation, _ = self.send("今天又折腾了半天。", sdrpe="on")
        turn_id = creation.turn.turn_id
        self.assertEqual(rewritten, self.final_text(turn_id))
        checked = [
            payload
            for kind, payload in self.events(turn_id)
            if kind == "expression_checked"
        ]
        self.assertEqual(1, len(checked))
        metrics = checked[0]["metrics"]
        self.assertLess(metrics["length_ratio"], 1.0)
        self.assertGreater(metrics["draft_lcs_chars"], 0)
        self.assertEqual(0, metrics["example_lcs_chars"])
        self.assertNotIn("public_draft", json.dumps(checked[0]))

    # ------------------------------------------------------------- S06

    def test_auto_never_runs_and_records_the_reason(self) -> None:
        self.enqueue_content(json.dumps(final("自动模式的回答。")))
        creation, _ = self.send("你好。", sdrpe="auto")
        turn_id = creation.turn.turn_id
        self.assertEqual(1, len(self.provider.requests))
        stages = self.stages(turn_id)
        self.assertEqual("skipped", stages[1].status)
        self.assertEqual("auto_not_implemented", stages[1].skip_reason)
        self.assertFalse(stages[1].status == "succeeded")

    def test_work_mode_never_runs_the_stage(self) -> None:
        self.enqueue_content(json.dumps(final("工作模式回答。")))
        creation, _ = self.send(
            "列一下步骤。", sdrpe="on", conversation=self.work_conversation()
        )
        turn_id = creation.turn.turn_id
        self.assertEqual(1, len(self.provider.requests))
        stages = self.stages(turn_id)
        self.assertEqual("skipped", stages[1].status)
        self.assertEqual("work_mode", stages[1].skip_reason)

    # ------------------------------------------------------------- H4 / Q5

    def test_pending_cognition_cannot_be_described_as_remembered(self) -> None:
        self.enqueue_content(json.dumps(final("好。")))
        self.enqueue_content(json.dumps(final("好的，我已记住。")))
        self.enqueue_content(json.dumps(final("好的。")))
        creation, _ = self.send(
            "我其实想简短一点。",
            sdrpe="on",
            orchestrator=self.orchestrator(cognition_status_factory=lambda: 2),
        )
        turn_id = creation.turn.turn_id
        self.assertEqual("好的。", self.final_text(turn_id))
        expression_snapshot = self.snapshots(turn_id)[1]
        document = json.loads(expression_snapshot.rendered_input)
        self.assertEqual(
            2, document["expression_task"]["pending_cognition_changes"]
        )
        repair = self.request_bodies()[2]["messages"]
        self.assertIn("memory_claim_while_pending", repair[3]["content"])

    def test_final_artifact_keeps_both_stages_sources(self) -> None:
        self.enqueue_content(json.dumps(final("草稿。")))
        self.enqueue_content(json.dumps(final("改好的草稿。")))
        creation, _ = self.send("问题。", sdrpe="on")
        turn_id = creation.turn.turn_id
        content_snapshot, expression_snapshot = self.snapshots(turn_id)
        artifact = self.published_artifact(turn_id)

        def keys(refs):
            return {
                (
                    item["ref_type"],
                    item.get("input_id")
                    or item.get("record_id")
                    or item.get("artifact_id"),
                    item.get("content_version")
                    or item.get("record_version")
                    or item.get("artifact_version"),
                )
                for item in refs
            }

        artifact_keys = keys(artifact.source_refs)
        self.assertTrue(
            keys(
                self.store.context_collab_sources(
                    namespace=NS, context_id=content_snapshot.context_id
                )
            ).issubset(artifact_keys)
        )
        self.assertTrue(
            keys(
                self.store.context_collab_sources(
                    namespace=NS, context_id=expression_snapshot.context_id
                )
            ).issubset(artifact_keys)
        )
        self.assertEqual(expression_snapshot.context_id, artifact.context_id)
        self.assertIsNotNone(artifact.attempt_ref)


if __name__ == "__main__":
    unittest.main()
