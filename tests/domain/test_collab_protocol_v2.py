"""RG03: the ``collab.v2`` parser, declaration and request-body boundary.

These tests use the real consult Context fixture and never open a socket.  The
v1 branch is asserted separately in ``tests.domain.test_collab_protocol`` and
must stay byte-identical; here we prove the v2 shape, its refusals and the
one-parser dispatch.
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
from model_support import make_manifest  # noqa: E402

from quintessence_pcs.foundation.errors import InvalidInput  # noqa: E402
from quintessence_pcs.foundation.jsonutil import canonical_json_text  # noqa: E402
from quintessence_pcs.runtime.collab_protocol import (  # noqa: E402
    COLLAB_PROTOCOL_VERSION,
    COLLAB_V2_ACTIONS,
    COLLAB_V2_AUTHORITATIVE_FIELDS,
    COLLAB_V2_MODEL_POLICY_VERSION,
    COLLAB_V2_PROTOCOL_VERSION,
    COLLAB_V2_RENDERER_VERSION,
    MAX_COMMENTARY_CHARS,
    CollabOutput,
    ConsultSendPolicy,
    build_collab_request_body,
    parse_collab_output,
    protocol_policy_version,
    protocol_renderer_version,
    render_collab_messages,
    repair_feedback,
)
from quintessence_pcs.runtime.model_port import ModelConfigurationError  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:synthetic-rg03-protocol"
ACCESS_REF = "authorization:access:rg03-protocol"
CONSULT_BODY = "Synthetic RG03 question for the v2 protocol tests."


class CollabProtocolV2Tests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body=CONSULT_BODY,
            privacy_class="private",
            retention="standard",
            content_version=1,
        )
        self.ref = {
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
            input_refs=[self.ref],
        )
        self.access = ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
        )
        self.context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.access,
            input_refs=[self.ref],
            adapter_kind="direct-http",
        )
        self.manifest = make_manifest("https://api.deepseek.com")

    def parse(self, content, *, allowed=None):
        return parse_collab_output(
            content,
            mode="consult",
            allowed_references=[self.ref] if allowed is None else allowed,
            protocol_version=COLLAB_V2_PROTOCOL_VERSION,
        )

    # ---------------------------------------------------------------- parser

    def test_parser_accepts_final_call_tool_and_wait(self) -> None:
        final = self.parse('{"action":"final","reply":"合成回答。"}')
        self.assertEqual("final", final.action)
        self.assertEqual("合成回答。", final.reply)
        self.assertIsNone(final.commentary)
        self.assertIsNone(final.tool_call)
        self.assertIsNone(final.wait_question)

        call = self.parse(
            '{"action":"call_tool","commentary":"我翻一下。",'
            '"tool_call":{"name":"cognition.belief.read",'
            '"arguments":{"limit":3}}}'
        )
        self.assertEqual("call_tool", call.action)
        self.assertEqual("我翻一下。", call.commentary)
        self.assertEqual("cognition.belief.read", call.tool_call["name"])
        self.assertEqual({"limit": 3}, call.tool_call["arguments"])
        self.assertIsNone(call.reply)

        final_with_commentary = self.parse(
            '{"action":"final","commentary":"我核对完了。","reply":"合成回答。"}'
        )
        self.assertEqual("final", final_with_commentary.action)
        self.assertEqual("我核对完了。", final_with_commentary.commentary)

        wait = self.parse('{"action":"wait","wait_question":"你想先聊哪件事？"}')
        self.assertEqual("wait", wait.action)
        self.assertEqual("你想先聊哪件事？", wait.wait_question)
        self.assertIsNone(wait.reply)
        wait_with_commentary = self.parse(
            '{"action":"wait","commentary":"我需要先确认。",'
            '"wait_question":"你想先聊哪件事？"}'
        )
        self.assertEqual("我需要先确认。", wait_with_commentary.commentary)

    def test_empty_commentary_and_brief_reason_are_absent(self) -> None:
        # The live provider used "" to mean "no commentary"; it is an absent
        # optional field, not an over-short one.  reply/wait stay non-empty.
        out = self.parse(
            '{"action":"final","reply":"合成回答。",'
            '"commentary":"","brief_reason":""}'
        )
        self.assertEqual("final", out.action)
        self.assertIsNone(out.commentary)
        self.assertIsNone(out.brief_reason)
        with self.assertRaises(InvalidInput):
            self.parse('{"action":"final","reply":""}')
        with self.assertRaises(InvalidInput):
            self.parse('{"action":"wait","wait_question":""}')

    def test_final_accepts_explicit_ordered_messages(self) -> None:
        out = self.parse(
            json.dumps(
                {"action": "final", "messages": ["第一段。", "第二段。"]},
                ensure_ascii=False,
            )
        )
        self.assertEqual("final", out.action)
        self.assertEqual(("第一段。", "第二段。"), out.messages)
        self.assertIsNone(out.reply)
        self.assertEqual(("第一段。", "第二段。"), out.bubbles())

        legacy = self.parse('{"action":"final","reply":"单条。"}')
        self.assertIsNone(legacy.messages)
        self.assertEqual(("单条。",), legacy.bubbles())

        joined = "第一段。\n\n第二段。"
        both = self.parse(
            json.dumps(
                {
                    "action": "final",
                    "reply": joined,
                    "messages": ["第一段。", "第二段。"],
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual(joined, both.reply)
        self.assertEqual(("第一段。", "第二段。"), both.bubbles())

    def test_messages_are_bounded_and_final_only(self) -> None:
        cases = [
            {"action": "final", "messages": []},
            {"action": "final", "messages": ["a"] * 5},
            {"action": "final", "messages": ["a" * 4097]},
            {"action": "final", "messages": ["  "]},
            {"action": "final", "messages": "single"},
            {
                "action": "final",
                "reply": "different",
                "messages": ["第一段。", "第二段。"],
            },
            {
                "action": "call_tool",
                "messages": ["不该有"],
                "tool_call": {"name": "x.y", "arguments": {}},
            },
            {"action": "wait", "wait_question": "?", "messages": ["不该有"]},
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(InvalidInput):
                    self.parse(json.dumps(case, ensure_ascii=False))

        with self.assertRaises(InvalidInput):
            parse_collab_output(
                '{"action":"final","messages":["v1 无此字段"]}',
                mode="consult",
                allowed_references=[self.ref],
                protocol_version=COLLAB_PROTOCOL_VERSION,
            )

    def test_v1_and_v2_are_distinct_action_sets(self) -> None:
        with self.assertRaises(InvalidInput):
            self.parse('{"action":"none","reply":"v1 only"}')
        with self.assertRaises(InvalidInput):
            parse_collab_output(
                '{"action":"final","reply":"v2 only"}',
                mode="consult",
                allowed_references=[self.ref],
                protocol_version=COLLAB_PROTOCOL_VERSION,
            )
        with self.assertRaises(InvalidInput):
            parse_collab_output(
                '{"action":"final","reply":"x"}',
                mode="consult",
                allowed_references=[self.ref],
                protocol_version="collab.v9",
            )

    def test_action_invariants_are_enforced(self) -> None:
        cases = [
            '{"action":"final"}',
            '{"action":"final","reply":"x","wait_question":"不该有"}',
            '{"action":"call_tool"}',
            '{"action":"call_tool","tool_call":{"name":"x","arguments":{}},"reply":"x"}',
            '{"action":"call_tool","tool_call":{"name":"x","arguments":{}},"wait_question":"x"}',
            '{"action":"call_tool","tool_call":{"name":"x","arguments":{}},"references":[]}',
            '{"action":"wait","wait_question":"x","reply":"x"}',
            '{"action":"wait"}',
        ]
        for content in cases:
            with self.subTest(content=content):
                with self.assertRaises(InvalidInput):
                    self.parse(content)

    def test_tool_call_shape_is_closed(self) -> None:
        cases = [
            '{"action":"call_tool","tool_call":"question.list"}',
            '{"action":"call_tool","tool_call":{"name":"question.list"}}',
            '{"action":"call_tool","tool_call":{"name":"question.list","arguments":{},"extra":1}}',
            '{"action":"call_tool","tool_call":{"name":"Question.List","arguments":{}}}',
            '{"action":"call_tool","tool_call":{"name":"question.list","arguments":[]}}',
            '{"action":"call_tool","tool_call":{"name":"question.list","arguments":{}},"references":[{"ref_type":"collab_input","namespace":"%s","input_id":"%s","content_version":1}]}'
            % (NS, self.ref["input_id"]),
        ]
        for content in cases:
            with self.subTest(content=content):
                with self.assertRaises(InvalidInput):
                    self.parse(content)

    def test_authority_fields_are_refused(self) -> None:
        for field in COLLAB_V2_AUTHORITATIVE_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(InvalidInput):
                    self.parse(json.dumps({"action": "final", "reply": "x", field: "y"}))

    def test_text_bounds_and_commentary_policy_surface(self) -> None:
        empty = self.parse(
            '{"action":"call_tool","commentary":"","tool_call":{"name":"x.y","arguments":{}}}'
        )
        self.assertIsNone(empty.commentary)
        too_long = "a" * (MAX_COMMENTARY_CHARS + 1)
        with self.assertRaises(InvalidInput):
            self.parse(
                json.dumps(
                    {
                        "action": "call_tool",
                        "commentary": too_long,
                        "tool_call": {"name": "x.y", "arguments": {}},
                    }
                )
            )
        # 60-character policy is host-side: a long-but-legal commentary parses.
        legal = "a" * MAX_COMMENTARY_CHARS
        output = self.parse(
            json.dumps(
                {
                    "action": "call_tool",
                    "commentary": legal,
                    "tool_call": {"name": "x.y", "arguments": {}},
                }
            )
        )
        self.assertEqual(legal, output.commentary)

    def test_references_are_only_for_final_and_wait(self) -> None:
        final = self.parse(
            json.dumps(
                {
                    "action": "final",
                    "reply": "x",
                    "references": [self.ref],
                }
            )
        )
        self.assertEqual((self.ref,), final.references)
        wait = self.parse(
            json.dumps(
                {
                    "action": "wait",
                    "wait_question": "?",
                    "references": [self.ref],
                }
            )
        )
        self.assertEqual((self.ref,), wait.references)

    # ------------------------------------------------------------ declaration

    def test_v2_declaration_matches_the_protocol(self) -> None:
        messages = render_collab_messages(
            self.context, protocol_version=COLLAB_V2_PROTOCOL_VERSION
        )
        self.assertEqual(["system", "user"], [item["role"] for item in messages])
        self.assertEqual(self.context.rendered_input, messages[1]["content"])
        declaration = json.loads(messages[0]["content"])
        self.assertEqual(COLLAB_V2_PROTOCOL_VERSION, declaration["protocol_version"])
        self.assertEqual(COLLAB_V2_RENDERER_VERSION, declaration["renderer_version"])
        self.assertEqual(list(COLLAB_V2_ACTIONS), declaration["allowed_actions"])
        self.assertEqual(
            set(COLLAB_V2_AUTHORITATIVE_FIELDS),
            set(declaration["forbidden_output_fields"]),
        )
        props = set(declaration["output_schema"]["properties"])
        self.assertEqual(
            {
                "action",
                "reply",
                "commentary",
                "messages",
                "tool_call",
                "wait_question",
                "references",
                "brief_reason",
            },
            props,
        )
        self.assertIn("tools[]", declaration["tools_policy"])
        self.assertEqual(
            4, declaration["output_schema"]["properties"]["messages"]["maxItems"]
        )
        self.assertIn("final", declaration["bubble_policy"])

    def test_v2_declaration_carries_the_persona_and_shared_style_policy(self) -> None:
        declaration = json.loads(
            render_collab_messages(
                self.context, protocol_version=COLLAB_V2_PROTOCOL_VERSION
            )[0]["content"]
        )
        # RG06/RG08 pin the version strings so a silent policy change fails loudly.
        self.assertEqual("collab.v2.renderer.8", declaration["renderer_version"])
        self.assertEqual("collab.v2.direct-http.v2", COLLAB_V2_MODEL_POLICY_VERSION)
        persona_policy = declaration["persona_policy"]
        for phrase in (
            "host-owned persona expression guidance",
            "expression demonstrations",
            "pre_tool_commentary",
            "assistant_messages",
            "tool result is authoritative",
            "missing speaker as unknown",
            "never modify the persona",
        ):
            self.assertIn(phrase, persona_policy)
        # RG08 V1.5: ``chat_style_policy`` became ``shared_style_policy``.
        # The old key must not survive as an alias: two texts with the same
        # name and different meaning is exactly the trap this card removes.
        self.assertNotIn("chat_style_policy", declaration)
        style_policy = declaration["shared_style_policy"]
        # The shared policy keeps the user's own wording, minus the sentence
        # that told both stages their draft was already publishable.
        for phrase in (
            "先理解当前这句话怎样接续前文",
            "人格体现在你关注什么、怎样判断",
            "用户明确求助时认真处理；缺少关键情况时询问必要信息",
            "面向用户的语言写成自然可读的完整话语",
            "回复长度依照实际内容，不要求每轮建议、追问、总结或玩笑",
            "如果当前 Context 没有阶段任务说明，就按本说明直接给出最终回复",
            "事实判断依据实际材料",
            "没有列出工具的阶段不要输出工具调用",
            "每次调用工具前，提供一句简短自然的 commentary",
            "工作模式遵守既有工作流程",
            "遵守当前动作协议",
        ):
            self.assertIn(phrase, style_policy)
        # The stage-ownership sentence is gone.  Neither stage may be told
        # that its draft is the publishable final answer.
        for removed in (
            "生成可直接发布的草稿",
            "SDRPE 开启时，草稿仍应符合人格和关系",
            "不预先写成中性说明再等待下一阶段添加性格",
        ):
            self.assertNotIn(removed, style_policy)
        self.assertLess(len(style_policy), 1200)
        v11_fact_boundary = (
            "事实判断依据实际材料。区分用户报告、直接输入、推断与想象；不要把用户描述"
            "的环境扩写成自己的感官经历，也不要添加未经提供的具体细节。允许表达当下的"
            "态度和明确的想象，不为此虚构经历、记忆或原因。"
        )
        self.assertEqual(1, style_policy.count(v11_fact_boundary))
        for old in (
            "日常交流依据当前人格与关系参与话题",
            "回应对方分享的内容",
            "分享日常不必自动转成建议",
            "人格可以影响对同一件事关注什么",
            "事实判断仍依据材料",
            "事实、共同经历和操作状态以实际材料为准",
            "根据实际输入区分用户报告、直接观察和想象",
            "用户说“起雾了”",
            "用户描述的环境、天气和视觉细节是他的报告",
            "日常聊天既可以回应对方",
            "分享感受未必是在请求建议",
            "回复长短由具体内容决定",
            "SDRPE 开启时也不刻意写成中性的说明稿",
            "普通聊天直接回应当下的话",
            "不默认分析对方的措辞和心理",
            "人格可影响回应的关注点",
            "简单回应已经足够时就停下",
            "不把亲近交流写成对用户措辞的分析",
            "不要默认采用",
            "信息缺失仅在影响回答或操作时需要澄清",
            "最终回复接续已公开内容",
        ):
            self.assertNotIn(old, style_policy)
        # Generic host guidance names no persona, owner or private term (the
        # private name is composed so this file carries no such literal).
        for forbidden in ("SYNTHETIC_PERSONA_A", "SYNTHETIC_OWNER_A", "专属关系", "专属称呼"):
            self.assertNotIn(forbidden, style_policy)
        v1_declaration = json.loads(
            render_collab_messages(
                self.context, protocol_version=COLLAB_PROTOCOL_VERSION
            )[0]["content"]
        )
        self.assertNotIn("persona_policy", v1_declaration)
        self.assertNotIn("chat_style_policy", v1_declaration)
        self.assertNotIn("shared_style_policy", v1_declaration)

    def test_v2_declaration_pins_the_exact_format_and_tool_policy(self) -> None:
        declaration = json.loads(
            render_collab_messages(
                self.context, protocol_version=COLLAB_V2_PROTOCOL_VERSION
            )[0]["content"]
        )
        self.assertEqual(
            "输出必须是且仅是一个符合 output_schema 的 JSON 对象。面向用户的话"
            "必须放入 reply、messages、commentary 或 wait_question 对应字段。"
            "不要在 JSON 外输出文字、代码围栏、XML、DSML 或其他工具标记。"
            "工具描述采用 function schema，不代表可以输出原生工具标记；本轮"
            '工具调用必须封装为 action="call_tool" 的 JSON 对象。action="final" '
            "同样必须是 JSON 对象，不能直接输出普通回答。",
            declaration["response_format"],
        )
        self.assertEqual(
            '需要调用工具时，输出一个 action="call_tool" 的 JSON 对象，其中 '
            "tool_call.name 必须精确匹配当前 Context.tools[] 的工具名，"
            "tool_call.arguments 必须符合该工具的参数 schema。每个动作只请求一次"
            "工具调用。工具名和参数字段是请求，不代表工具已经执行；收到宿主提供的"
            "真实工具结果后，再决定继续调用、请求澄清或给出最终回答。不得用自然语言"
            "声称已完成尚未执行的查询或操作，不得输出 DSML/XML 工具语法。",
            declaration["tools_policy"],
        )
        self.assertIn(
            "每次调用工具前，提供一句简短自然的 commentary",
            declaration["shared_style_policy"],
        )
        self.assertNotIn(
            "可在实际行动前给出简短 commentary",
            declaration["shared_style_policy"],
        )

    def test_output_examples_are_conditional_on_a_real_web_search_tool(self) -> None:
        without_web = json.loads(
            render_collab_messages(
                self.context, protocol_version=COLLAB_V2_PROTOCOL_VERSION
            )[0]["content"]
        )
        self.assertEqual(
            {
                "final": {"action": "final", "reply": "早啊"},
                "wait": {
                    "action": "wait",
                    "wait_question": "你想查哪个时间段？",
                },
            },
            without_web["output_examples"],
        )
        with_web = self.contexts.build_consult_context(
            namespace=NS,
            access=self.access,
            input_refs=[self.ref],
            adapter_kind="direct-http",
            tool_definitions=(
                {
                    "type": "function",
                    "function": {
                        "name": "web.search",
                        "description": "synthetic",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "count": {"type": "integer"},
                            },
                            "required": ["query"],
                        },
                    },
                },
            ),
        )
        declaration = json.loads(
            render_collab_messages(
                with_web, protocol_version=COLLAB_V2_PROTOCOL_VERSION
            )[0]["content"]
        )
        self.assertEqual(
            {
                "final": {"action": "final", "reply": "早啊"},
                "call_tool": {
                    "action": "call_tool",
                    "commentary": "我查一下",
                    "tool_call": {
                        "name": "web.search",
                        "arguments": {"query": "示例查询"},
                    },
                },
                "wait": {
                    "action": "wait",
                    "wait_question": "你想查哪个时间段？",
                },
            },
            declaration["output_examples"],
        )

    def test_output_example_is_omitted_when_the_web_search_schema_does_not_match(
        self,
    ) -> None:
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.access,
            input_refs=[self.ref],
            adapter_kind="direct-http",
            tool_definitions=(
                {
                    "type": "function",
                    "function": {
                        "name": "web.search",
                        "description": "synthetic",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "integer"}},
                            "required": ["query"],
                        },
                    },
                },
            ),
        )
        declaration = json.loads(
            render_collab_messages(
                context, protocol_version=COLLAB_V2_PROTOCOL_VERSION
            )[0]["content"]
        )
        self.assertNotIn("call_tool", declaration["output_examples"])
        self.assertEqual(
            ["final", "wait"], list(declaration["output_examples"])
        )

    def test_v1_declaration_is_byte_identical_after_the_v2_change(self) -> None:
        """The v1 declaration must not gain a v2 key or reorder v1 keys."""

        v1 = json.loads(
            render_collab_messages(
                self.context, protocol_version=COLLAB_PROTOCOL_VERSION
            )[0]["content"]
        )
        self.assertEqual(COLLAB_PROTOCOL_VERSION, v1["protocol_version"])
        self.assertNotIn("tools_policy", v1)
        self.assertNotIn("commentary_policy", v1)
        self.assertEqual(
            {
                "protocol_version",
                "renderer_version",
                "mode",
                "output_schema",
                "allowed_actions",
                "consult_forbidden_action",
                "query_policy",
                "references_policy",
                "forbidden_output_fields",
                "response_format",
            },
            set(v1),
        )

    def test_repair_feedback_names_the_protocol_and_actions(self) -> None:
        feedback = repair_feedback(
            InvalidInput("x"), protocol_version=COLLAB_V2_PROTOCOL_VERSION
        )
        self.assertIn(COLLAB_V2_PROTOCOL_VERSION, feedback)
        for action in COLLAB_V2_ACTIONS:
            self.assertIn(action, feedback)
        for field in ("turn_id", "conversation_id", "stage_id", "attempt_id"):
            self.assertIn(field, feedback)
        self.assertIn("at most one tool_call", feedback)
        v1 = repair_feedback(InvalidInput("x"))
        self.assertIn("collab.v1", v1)
        self.assertIn("none, query, propose, wait", v1)

    def test_policy_and_renderer_are_version_scoped(self) -> None:
        self.assertEqual(
            COLLAB_V2_RENDERER_VERSION,
            protocol_renderer_version(COLLAB_V2_PROTOCOL_VERSION),
        )
        self.assertEqual(
            COLLAB_V2_MODEL_POLICY_VERSION,
            protocol_policy_version(COLLAB_V2_PROTOCOL_VERSION),
        )
        self.assertEqual(
            "collab.v1.renderer.3", protocol_renderer_version(COLLAB_PROTOCOL_VERSION)
        )
        with self.assertRaises(InvalidInput):
            protocol_renderer_version("collab.v9")

    # ----------------------------------------------------------- request body

    def test_v2_request_body_stays_tool_free_and_exact(self) -> None:
        messages = render_collab_messages(
            self.context, protocol_version=COLLAB_V2_PROTOCOL_VERSION
        )
        body = build_collab_request_body(
            self.manifest, messages=messages, max_tokens=64
        )
        document = json.loads(body)
        self.assertNotIn("tools", document)
        self.assertNotIn("functions", document)
        self.assertEqual(False, document["stream"])
        self.assertEqual(self.context.rendered_input, document["messages"][1]["content"])
        self.assertEqual(
            canonical_json_text(
                json.loads(
                    render_collab_messages(
                        self.context, protocol_version=COLLAB_V2_PROTOCOL_VERSION
                    )[0]["content"]
                )
            ),
            canonical_json_text(json.loads(document["messages"][0]["content"])),
        )

    def test_consult_policy_is_still_required(self) -> None:
        policy = ConsultSendPolicy(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
            disclose=True,
        )
        self.assertEqual(NS, policy.namespace)


if __name__ == "__main__":
    unittest.main()
