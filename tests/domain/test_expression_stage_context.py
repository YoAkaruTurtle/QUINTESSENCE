"""RG07: the ``content_handoff`` and ``expression_task`` Context sections.

The host builds both sections; these tests prove they render only when
supplied, that the legacy (``None``) document is unchanged apart from its
run id, that they never carry tools or a cognition working set, and that an
oversized or malformed handoff is refused instead of silently trimmed.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.conversation.expression_host_policy import (  # noqa: E402
    HOST_EXPRESSION_STAGE_POLICY,
)
from quintessence_pcs.conversation.expression_stage import (  # noqa: E402
    HANDOFF_VERSION,
    MAX_HANDOFF_BYTES,
    MUST_PRESERVE,
    TASK_VERSION,
    build_content_handoff,
    build_expression_task,
    handoff_bytes,
)
from quintessence_pcs.conversation.persona import build_persona_material  # noqa: E402
from quintessence_pcs.conversation.policy_bundle import (  # noqa: E402
    PolicyBundle,
    current_bundle,
)
from quintessence_pcs.domains.persona.policy import PersonaPolicyAsset  # noqa: E402
from quintessence_pcs.foundation.errors import InvalidInput  # noqa: E402
from quintessence_pcs.runtime.collab_protocol import (  # noqa: E402
    COLLAB_V2_PROTOCOL_VERSION,
    COLLAB_V2_RENDERER_VERSION,
    render_collab_messages,
)
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

CONVERSATION = "22222222-2222-4222-8222-222222222222"

#: The fact/material boundary the RG08 v11 host policies carry, verbatim.
NEW_CONTENT_BOUNDARY = (
    "事实判断依据实际材料。区分用户报告、直接输入、推断与想象；不要把用户描述的环境"
    "扩写成自己的感官经历，也不要添加未经提供的具体细节。允许表达当下的态度和明确的"
    "想象，不为此虚构经历、记忆或原因。"
)
NEW_EXPRESSION_BOUNDARY = (
    "发现草稿含未经支持的环境细节、感官经历或其他事实扩写时，不把它们当作已核实事实"
    "继续润色。\n\n删除无依据细节，或按实际情况保留不确定性。\n\n本阶段不调用工具、"
    "不重新查证、不执行记忆或认知写入。"
)
OLD_CONTENT_BOUNDARY = (
    "根据实际输入区分用户报告、直接观察和想象。用户说“起雾了”，只提供了起雾这一信息；"
    "雾的浓淡、周围建筑、能见度和用户是否准备出门仍未明确。可以自然表达偏好、好奇，"
    "也可以用明确的假设或比喻接话，但不要把未提供的细节说成眼前事实，或说成你自己的"
    "感官经历。无需为了说明这个边界，每轮强调自己没有身体或看不见。"
)
OLD_EXPRESSION_BOUNDARY = (
    "草稿中的环境与感官描写也需要有依据。若草稿把用户的简短报告扩写成未经支持的具体场景，"
    "不要将其视为必须保留的事实，也不要进一步具体化；应删除这些细节，或明确改成想象、可能性或询问。"
    "保留用户确实提供的信息，以及符合人格的态度与反应。该规则判断的是语义，不禁止“窗外”“远处”等词语。"
)

#: RG08 V1.5: the expression task policy is assembled per turn from the frozen
#: ``PolicyBundle``.  Source carries only the persona-neutral host layer; the
#: persona-owned text lives in an asset record.  The pins below therefore cover
#: the *module source* (it must not regain persona text) and the *host layer*
#: (it must stay generic), never a persona policy string.
PERSONA_NEUTRAL_ANCHORS = (
    "**宿主规则：最终表达（与具体人格无关，适用于所有人格）**",
    "**最终表达权**",
    "**三类必须留下的内容**",
    "**提问不是默认动作**",
    "**输出形状**",
    "**你不是第二个内容阶段**",
    "本阶段没有工具执行能力",
)
#: Words that only ever belonged to a persona's own expression policy.  A
#: generic module that ships them has been re-coupled to a private asset.
FORBIDDEN_PERSONA_TOKENS = (
    "傲娇",
    "SYNTHETIC_OWNER_A",
    "synthetic_owner_alias",
    "专属称呼",
)
#: SHA-256 of the shared style policy (RG08 V1.5: ``chat_style_policy`` was
#: renamed to ``shared_style_policy`` and the stage-ownership sentence removed).
SHARED_POLICY_SHA256 = (
    "102d9dd0dea657be00a3368ba173dd516e83fa110ce77bc6dfe05bb5a8850e8b"
)
#: Sentences that used to tell both stages the draft was already publishable.
#: They must never come back: they are the root of the responsibility conflict.
REMOVED_STAGE_OWNERSHIP = (
    "生成可直接发布的草稿",
    "SDRPE 开启时，草稿仍应符合人格和关系",
    "不预先写成中性说明再等待下一阶段添加性格",
)


class ExpressionStageContextTests(CognitionTestCase):
    def access(self) -> ExperienceAccess:
        return ExperienceAccess(
            namespace=NS,
            host_instance="synthetic-host",
            access_ref="access:synthetic",
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
        )

    def register(self, text: str) -> dict:
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body=text,
            privacy_class="private",
            retention="standard",
            content_version=1,
        )
        return {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": 1,
        }

    def build(self, ref: dict, **extra):
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.access(),
            input_refs=[ref],
            adapter_kind="direct-http",
            renderer_version=COLLAB_V2_RENDERER_VERSION,
            **extra,
        )
        snapshot = self.store.load_context_snapshot(context.context_id)
        return snapshot, json.loads(snapshot.rendered_input)

    def bundle(self) -> PolicyBundle:
        """A generic-only bundle: the shape every non-persona path sees."""

        return current_bundle()

    def test_handoff_and_task_render_without_tools_or_cognition(self) -> None:
        ref = self.register("今天又折腾了半天，感觉什么都没做出来。")
        draft = "折腾半天却觉得什么都没落下来，这种空转最磨人。想说说今天都忙了些什么吗？"
        handoff = build_content_handoff(
            draft=draft, already_public_refs=("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",)
        )
        task = build_expression_task(pending_cognition_changes=2, bundle=self.bundle())
        snapshot, document = self.build(
            ref, content_handoff=handoff, expression_task=task
        )
        self.assertEqual(HANDOFF_VERSION, document["content_handoff"]["version"])
        self.assertEqual(draft, document["content_handoff"]["public_draft"])
        self.assertEqual(
            list(MUST_PRESERVE), document["content_handoff"]["must_preserve"]
        )
        self.assertNotIn("structure", document["content_handoff"]["must_preserve"])
        self.assertIn("judgments", document["content_handoff"]["must_preserve"])
        self.assertIn("operation_state", document["content_handoff"]["must_preserve"])
        self.assertEqual(TASK_VERSION, document["expression_task"]["version"])
        self.assertEqual(2, document["expression_task"]["pending_cognition_changes"])
        self.assertIn("本阶段没有工具执行能力", document["expression_task"]["policy"])
        self.assertEqual(
            COLLAB_V2_RENDERER_VERSION, snapshot.renderer_version
        )
        self.assertNotIn("tools", document)
        self.assertNotIn("tool_results", document)
        self.assertNotIn("cognition", document)
        self.assertEqual(
            handoff_bytes(handoff), snapshot.budget_view["handoff_bytes"]
        )
        self.assertTrue(snapshot.budget_view["expression_stage"])
        # The handoff is host text: no binding, digest, grant or reasoning key.
        rendered = snapshot.rendered_input
        for forbidden in (
            "directive_digest",
            "DirectiveBinding",
            "authorization",
            "reasoning",
            "chain_of_thought",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_environment_boundary_renders_in_content_and_expression(self) -> None:
        ref = self.register("今天起雾了啊")
        handoff = build_content_handoff(draft="雾天啊，出门慢一点。")
        task = build_expression_task(
            pending_cognition_changes=0, bundle=self.bundle()
        )
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.access(),
            input_refs=[ref],
            adapter_kind="direct-http",
            renderer_version=COLLAB_V2_RENDERER_VERSION,
            content_handoff=handoff,
            expression_task=task,
        )
        snapshot = self.store.load_context_snapshot(context.context_id)
        document = json.loads(snapshot.rendered_input)
        declaration = json.loads(
            render_collab_messages(
                context, protocol_version=COLLAB_V2_PROTOCOL_VERSION
            )[0]["content"]
        )
        cases = (
            (
                "content",
                declaration["shared_style_policy"],
                NEW_CONTENT_BOUNDARY,
                OLD_CONTENT_BOUNDARY,
                (
                    "先理解当前这句话怎样接续前文",
                    "每次调用工具前，提供一句简短自然的 commentary",
                    "不输出内部思考、括号动作、舞台指示或表达策略说明",
                ),
            ),
        )
        for side, policy, new, old, anchors in cases:
            with self.subTest(side=side):
                self.assertEqual(1, policy.count(new))
                self.assertNotIn(old, policy)
                for anchor in anchors:
                    self.assertIn(anchor, policy)
        # RG08 V1.5: with no persona asset loaded, the expression task carries
        # the generic host layer and nothing persona-owned.  The V13-era
        # fact-boundary sentences now live in a persona asset, so this side is
        # asserted against the host layer's own anchors instead.
        expression_policy = document["expression_task"]["policy"]
        self.assertEqual(HOST_EXPRESSION_STAGE_POLICY, expression_policy)
        for anchor in PERSONA_NEUTRAL_ANCHORS:
            self.assertIn(anchor, expression_policy)
        self.assertNotIn(NEW_EXPRESSION_BOUNDARY, expression_policy)
        self.assertNotIn(OLD_EXPRESSION_BOUNDARY, expression_policy)
        # RG08 V1.5 renames the host-owned text to ``shared_style_policy`` and
        # deletes the stage-ownership sentence; the digest makes that loud.
        self.assertNotIn("chat_style_policy", declaration)
        self.assertEqual(
            SHARED_POLICY_SHA256,
            hashlib.sha256(
                declaration["shared_style_policy"].encode("utf-8")
            ).hexdigest(),
        )
        for removed in REMOVED_STAGE_OWNERSHIP:
            self.assertNotIn(removed, declaration["shared_style_policy"])
        self.assertEqual(TASK_VERSION, document["expression_task"]["version"])
        self.assertEqual(COLLAB_V2_RENDERER_VERSION, snapshot.renderer_version)

    def test_persona_examples_are_identical_in_content_and_expression(self) -> None:
        ref = self.register("没什么特别的事，就是过来找你说两句")
        body = {
            "profile_id": "example.synthetic.persona",
            "profile_version": "12",
            "asset_hash": "b" * 64,
            "examples": [
                {
                    "example_id": "synthetic-base-1",
                    "scene": "owner-base",
                    "user": "没什么特别的事，就是过来找你说两句",
                    "assistant": "那挺好，我也想和你随便聊聊",
                    "synthetic": True,
                },
                {
                    "example_id": "synthetic-base-2",
                    "scene": "owner-base",
                    "user": "你刚才那句话倒是提醒我了",
                    "assistant": "看来我这句没白说嘛",
                    "synthetic": True,
                },
            ],
        }
        material = build_persona_material(
            body,
            {
                "asset_hash": body["asset_hash"],
                "example_ids": ["synthetic-base-1", "synthetic-base-2"],
                "speaker": {"identity": "owner", "source": "host_config"},
            },
        )
        assert material is not None
        # The same pinned material is what the orchestrator hands to both
        # stages; each stage renders the same ordered group, never its own.
        _, content = self.build(ref, persona=material)
        handoff = build_content_handoff(draft="嗯，我在，想聊什么都可以。")
        task = build_expression_task(
            pending_cognition_changes=0, bundle=self.bundle()
        )
        _, expression = self.build(
            ref, persona=material, content_handoff=handoff, expression_task=task
        )
        content_ids = [
            item["example_id"] for item in content["profile"]["examples"]
        ]
        expression_ids = [
            item["example_id"] for item in expression["profile"]["examples"]
        ]
        self.assertEqual(["synthetic-base-1", "synthetic-base-2"], content_ids)
        self.assertEqual(content_ids, expression_ids)
        for document in (content, expression):
            for item in document["profile"]["examples"]:
                self.assertEqual("expression_demo", item["basis"])
                self.assertTrue(item["not_experience"])
            # Demos are profile material only: never history or memory evidence.
            self.assertNotIn("history", document)
            self.assertNotIn("memory_recall", document)
            self.assertNotIn("cognition", document)

    def test_sections_are_absent_by_default_and_the_document_is_unchanged(self) -> None:
        ref = self.register("普通一句话。")
        _, legacy = self.build(ref)
        self.assertNotIn("content_handoff", legacy)
        self.assertNotIn("expression_task", legacy)
        _, explicit_none = self.build(
            ref, content_handoff=None, expression_task=None
        )
        legacy.pop("run_id")
        explicit_none.pop("run_id")
        self.assertEqual(legacy, explicit_none)

    def test_empty_draft_is_refused(self) -> None:
        for draft in ("", "   ", "\n\t"):
            with self.subTest(draft=draft):
                with self.assertRaises(InvalidInput):
                    build_content_handoff(draft=draft)

    def test_oversized_handoff_is_refused_not_trimmed(self) -> None:
        too_big = "甲" * (MAX_HANDOFF_BYTES // 3 + 32)
        with self.assertRaises(InvalidInput):
            build_content_handoff(draft=too_big)

    def test_malformed_sections_are_refused(self) -> None:
        ref = self.register("一句话。")
        for value in ("text", 1, [], {"public_draft": "x"}):
            if isinstance(value, dict):
                continue
            with self.subTest(value=value):
                with self.assertRaises(InvalidInput):
                    self.build(ref, content_handoff=value)
        with self.assertRaises(InvalidInput):
            build_expression_task(pending_cognition_changes=-1, bundle=self.bundle())
        with self.assertRaises(InvalidInput):
            build_expression_task(pending_cognition_changes=True, bundle=self.bundle())

    def test_expression_task_policy_comes_from_the_bundle(self) -> None:
        """The task text is exactly the bundle's composed text, never a constant."""

        bundle = self.bundle()
        task = build_expression_task(pending_cognition_changes=0, bundle=bundle)
        self.assertEqual(TASK_VERSION, task["version"])
        self.assertEqual(bundle.expression_text, task["policy"])
        self.assertEqual(bundle.version, task["policy_version"])
        self.assertEqual(bundle.digest, task["policy_digest"])
        # Generic-only turn: the persona slot is honestly reported as absent.
        self.assertEqual("absent", task["persona_policy_state"])
        self.assertIsNone(task["persona_policy_sha256"])
        self.assertIsNone(task["persona_profile_id"])
        self.assertIsNone(task["persona_profile_version"])
        # The host layer is the whole policy when no persona asset is loaded.
        self.assertEqual(HOST_EXPRESSION_STAGE_POLICY, task["policy"])

    def test_persona_text_is_reported_but_never_sourced_here(self) -> None:
        """A present persona asset is concatenated by the bundle, not by source."""

        persona = PersonaPolicyAsset(
            state="present",
            profile_id="example.synthetic.persona",
            profile_version="12",
            text="**合成人格表达偏好**\n\n先回应对方眼前那句话。",
            sha256="a" * 64,
            ref={"ref_type": "record", "namespace": NS, "record_id": "x", "record_version": 1},
        )
        bundle = PolicyBundle(
            version=current_bundle().version,
            shared_text=current_bundle().shared_text,
            content_text=current_bundle().content_text,
            host_expression_text=HOST_EXPRESSION_STAGE_POLICY,
            persona=persona,
        )
        task = build_expression_task(pending_cognition_changes=1, bundle=bundle)
        self.assertEqual("present", task["persona_policy_state"])
        self.assertEqual("a" * 64, task["persona_policy_sha256"])
        self.assertEqual("example.synthetic.persona", task["persona_profile_id"])
        self.assertEqual("12", task["persona_profile_version"])
        self.assertTrue(task["policy"].startswith(persona.text))
        self.assertTrue(task["policy"].endswith(HOST_EXPRESSION_STAGE_POLICY))

    def test_host_layer_is_persona_neutral_and_bounded(self) -> None:
        """The only shipped expression text is mechanism, not a personality."""

        host = HOST_EXPRESSION_STAGE_POLICY
        self.assertLess(len(host), 4000)
        for anchor in PERSONA_NEUTRAL_ANCHORS:
            self.assertIn(anchor, host)
        self.assertNotIn("@", host)
        self.assertNotIn("persona", host.lower())
        private_name = "SYNTHETIC_PERSONA_A"
        self.assertNotIn(private_name, host)
        for token in FORBIDDEN_PERSONA_TOKENS:
            self.assertNotIn(token, host)
        # Style prescriptions are not mechanism: they belong to a persona asset.
        for style_word in ("温柔", "俏皮", "傲娇", "少女感"):
            self.assertNotIn(style_word, host)
        self.assertEqual(
            HOST_EXPRESSION_STAGE_POLICY,
            build_expression_task(
                pending_cognition_changes=0, bundle=self.bundle()
            )["policy"],
        )

    def test_module_source_carries_no_persona_policy(self) -> None:
        """The decoupling is structural: the module file itself has no asset text."""

        source = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "quintessence_pcs"
            / "conversation"
            / "expression_stage.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("EXPRESSION_TASK_POLICY", source)
        private_name = "SYNTHETIC_PERSONA_A"
        for token in (private_name, "SYNTHETIC_OWNER_A", "synthetic_owner_alias", "傲娇"):
            self.assertNotIn(token, source)
        # The persona-owned text is not smuggled back in as a big literal.
        self.assertLess(len(source), 20000)


if __name__ == "__main__":
    unittest.main()
