"""Bounded native DSML tool-call normalisation, offline.

Every fixture here is shape-faithful to a real provider response but uses
synthetic tool names, queries and commentary.  The two ``FAILED_TURN_RAW_*``
constants are the exact content of the two failed-turn attempts and are replayed
without a provider; the query is a public test query.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quintessence_pcs.foundation.errors import InvalidInput  # noqa: E402
from quintessence_pcs.runtime.collab_protocol import (  # noqa: E402
    CollabOutputError,
    parse_collab_output,
)

BAR = "｜｜DSML｜｜"

#: The two raw content strings of turn 6602bdab (finish_reason=stop each).
FAILED_TURN_RAW_ONE = (
    "我查一下最近的消息。\n\n"
    f"<{BAR} calls>\n"
    f'<{BAR} invoke name="tool_call">\n'
    f'<{BAR} parameter name="name" string="true">web.search</{BAR} parameter>\n'
    f'<{BAR} parameter name="arguments" string="false">'
    '{"query": "明日方舟 最近 活动 新闻", "count": 10}'
    f"</{BAR} parameter>\n"
    f"</{BAR} invoke>\n"
    f"</{BAR} calls>"
)
FAILED_TURN_RAW_TWO = (
    "\n\n"
    f"<{BAR} calls>\n"
    f'<{BAR} invoke name="tool_call">\n'
    f'<{BAR} parameter name="arguments" string="false">'
    '{"count": 10, "query": "明日方舟 最近 活动 新闻"}'
    f"</{BAR} parameter>\n"
    f'<{BAR} parameter name="name" string="true">web.search</{BAR} parameter>\n'
    f"</{BAR} invoke>\n"
    f"</{BAR} calls>"
)


def parse(raw: str):
    return parse_collab_output(
        raw, mode="consult", allowed_references=(), protocol_version="collab.v2"
    )


def param(name: str, body: str, *, string: str | None = None) -> str:
    attr = "" if string is None else f' string="{string}"'
    return f'<{BAR} parameter name="{name}"{attr}>{body}</{BAR} parameter>'


def invoke(body: str, *, name: str = "call_tool") -> str:
    return f'<{BAR} invoke name="{name}">\n{body}\n</{BAR} invoke>'


def dsml(body: str) -> str:
    return f"<{BAR} calls>\n{body}\n</{BAR} calls>"


class NativeDsmlTests(unittest.TestCase):
    def test_tool_call_parameter_with_json_is_accepted(self) -> None:
        out = parse(
            "我先查一下\n\n"
            + dsml(
                invoke(
                    param(
                        "tool_call",
                        '{"name": "cognition.partition", '
                        '"arguments": {"limit": 2}}',
                        string="false",
                    )
                )
            )
        )
        self.assertEqual("call_tool", out.action)
        self.assertEqual("cognition.partition", out.tool_call["name"])
        self.assertEqual({"limit": 2}, out.tool_call["arguments"])
        self.assertEqual("我先查一下", out.commentary)

    def test_call_tool_parameter_with_json_is_accepted(self) -> None:
        out = parse(
            dsml(
                invoke(
                    param(
                        "call_tool",
                        '{"name": "memory.search", "arguments": {"query": "q"}}',
                        string="false",
                    )
                )
            )
        )
        self.assertEqual("memory.search", out.tool_call["name"])
        self.assertEqual({"query": "q"}, out.tool_call["arguments"])

    def test_sibling_name_and_arguments_are_accepted(self) -> None:
        out = parse(
            dsml(
                invoke(
                    param("name", "cognition.partition", string="true")
                    + "\n"
                    + param("arguments", '{"limit": 3}', string="false")
                )
            )
        )
        self.assertEqual("cognition.partition", out.tool_call["name"])
        self.assertEqual({"limit": 3}, out.tool_call["arguments"])

    def test_tool_call_wrapper_alias_with_sibling_parameters_is_accepted(self) -> None:
        out = parse(FAILED_TURN_RAW_ONE)
        self.assertEqual("call_tool", out.action)
        self.assertEqual("web.search", out.tool_call["name"])
        self.assertEqual(
            {"query": "明日方舟 最近 活动 新闻", "count": 10},
            out.tool_call["arguments"],
        )
        self.assertEqual("我查一下最近的消息。", out.commentary)

    def test_tool_call_wrapper_alias_is_order_independent(self) -> None:
        out = parse(FAILED_TURN_RAW_TWO)
        self.assertEqual("call_tool", out.action)
        self.assertEqual("web.search", out.tool_call["name"])
        self.assertEqual(
            {"count": 10, "query": "明日方舟 最近 活动 新闻"},
            out.tool_call["arguments"],
        )
        self.assertIsNone(out.commentary)

    def test_tool_call_alias_with_the_single_wrapper_parameter_is_accepted(self) -> None:
        out = parse(
            dsml(
                invoke(
                    param(
                        "tool_call",
                        '{"name": "web.search", "arguments": {"query": "q"}}',
                        string="false",
                    ),
                    name="tool_call",
                )
            )
        )
        self.assertEqual("web.search", out.tool_call["name"])
        self.assertEqual({"query": "q"}, out.tool_call["arguments"])

    def test_tool_call_alias_still_rejects_unknown_tool_shape(self) -> None:
        # The alias is only a wrapper spelling.  A payload that names no tool
        # and no arguments still fails the same closed shape check.
        raw = dsml(invoke(param("other", "x"), name="tool_call"))
        with self.assertRaises(CollabOutputError) as caught:
            parse(raw)
        self.assertEqual("unknown_parameter", caught.exception.reason)

    def test_invoke_named_with_the_tool_is_accepted(self) -> None:
        # Live shape from turn 2ce8e638: the tool name sits on the invoke and the
        # sibling parameters *are* the arguments, with no call_tool wrapper.
        raw = (
            "我先按你这次的原话提交一条候选。\n\n"
            f"<{BAR} calls>\n"
            f'<{BAR} invoke name="cognition.propose">\n'
            + param("changes", '[{"op": "propose_belief"}]', string="false")
            + "\n"
            + param("summary", "synthetic summary", string="true")
            + "\n"
            f"</{BAR} invoke>\n"
            f"</{BAR} calls>"
        )
        out = parse(raw)
        self.assertEqual("cognition.propose", out.tool_call["name"])
        self.assertEqual(
            {"changes": [{"op": "propose_belief"}], "summary": "synthetic summary"},
            out.tool_call["arguments"],
        )
        self.assertEqual("我先按你这次的原话提交一条候选。", out.commentary)

    def test_invoke_named_with_the_tool_still_rejects_a_wrapper_mix(self) -> None:
        raw = (
            f"<{BAR} calls>\n"
            f'<{BAR} invoke name="memory.search">\n'
            + param("name", "web.search", string="true")
            + "\n"
            + param("arguments", '{"query": "q"}', string="false")
            + "\n"
            f"</{BAR} invoke>\n"
            f"</{BAR} calls>"
        )
        with self.assertRaises(CollabOutputError) as caught:
            parse(raw)
        self.assertEqual("ambiguous_call", caught.exception.reason)

    def test_raw_tool_call_with_parameters_is_accepted(self) -> None:
        raw = (
            "我查一下，看看最近有什么。\n\n"
            '<tool_call name="web.search">\n'
            '<parameter name="query">synthetic query</'
            + BAR
            + " parameter>\n"
            '<parameter name="count">10</'
            + BAR
            + " parameter>\n"
            "</tool_call>"
        )
        out = parse(raw)
        self.assertEqual("web.search", out.tool_call["name"])
        self.assertEqual(
            {"query": "synthetic query", "count": 10},
            out.tool_call["arguments"],
        )
        self.assertEqual("我查一下，看看最近有什么。", out.commentary)

    def test_nested_tool_call_children_are_accepted(self) -> None:
        out = parse(
            dsml(
                invoke(
                    param(
                        "tool_call",
                        "\n"
                        + param("name", "web.search")
                        + "\n"
                        + param(
                            "arguments",
                            "\n"
                            + param("query", "synthetic query", string="true")
                            + "\n"
                            + param("count", "10", string="false")
                            + "\n",
                        )
                        + "\n",
                    )
                    + "\n"
                    + param("commentary", "我查一下")
                )
            )
        )
        self.assertEqual("web.search", out.tool_call["name"])
        self.assertEqual(
            {"query": "synthetic query", "count": 10},
            out.tool_call["arguments"],
        )
        self.assertEqual("我查一下", out.commentary)

    def test_unclosed_nested_call_is_rejected(self) -> None:
        # The observed 2dfb3cfc attempt-2 shape: neither arguments nor
        # tool_call is closed and the block ends at </invoke>.
        raw = (
            f"<{BAR} calls>\n"
            f'<{BAR} invoke name="call_tool">\n'
            f'<{BAR} parameter name="tool_call">\n'
            f'<{BAR} parameter name="name">web.search</{BAR} parameter>\n'
            f'<{BAR} parameter name="arguments">\n'
            f'<{BAR} parameter name="query" string="true">synthetic query</{BAR} parameter>\n'
            f'<{BAR} parameter name="count" string="false">10</{BAR} parameter>\n'
            f"</{BAR} invoke>\n"
            f'<{BAR} parameter name="commentary">我查一下</{BAR} parameter>\n'
            f"</{BAR} invoke>\n"
            f"</{BAR} calls>"
        )
        with self.assertRaises(CollabOutputError) as caught:
            parse(raw)
        self.assertEqual("envelope_unclosed", caught.exception.reason)

    def test_multiple_invokes_are_rejected_not_truncated(self) -> None:
        raw = dsml(
            invoke(
                param("name", "cognition.partition", string="true")
                + "\n"
                + param("arguments", "{}", string="false")
            )
            + "\n"
            + invoke(
                param("name", "conversation.search", string="true")
                + "\n"
                + param("arguments", '{"query": "q"}', string="false")
            )
        )
        with self.assertRaises(CollabOutputError) as caught:
            parse(raw)
        self.assertEqual("multiple_invokes", caught.exception.reason)

    def test_trailing_text_is_rejected(self) -> None:
        raw = (
            dsml(
                invoke(
                    param("name", "cognition.partition", string="true")
                    + "\n"
                    + param("arguments", "{}", string="false")
                )
            )
            + "\n后面还有话"
        )
        with self.assertRaises(CollabOutputError) as caught:
            parse(raw)
        self.assertEqual("trailing_text", caught.exception.reason)

    def test_unknown_invoke_is_rejected(self) -> None:
        raw = (
            f'<{BAR} calls><{BAR} invoke name="other">'
            f"</{BAR} invoke></{BAR} calls>"
        )
        with self.assertRaises(CollabOutputError) as caught:
            parse(raw)
        self.assertEqual("unknown_invoke", caught.exception.reason)

    def test_missing_arguments_is_rejected(self) -> None:
        raw = dsml(invoke(param("name", "cognition.partition", string="true")))
        with self.assertRaises(CollabOutputError) as caught:
            parse(raw)
        self.assertEqual("missing_arguments", caught.exception.reason)

    def test_non_object_arguments_is_rejected(self) -> None:
        raw = dsml(
            invoke(
                param(
                    "tool_call",
                    '{"name": "cognition.partition", "arguments": [1]}',
                    string="false",
                )
            )
        )
        with self.assertRaises(CollabOutputError) as caught:
            parse(raw)
        self.assertEqual("arguments_shape", caught.exception.reason)

    def test_plain_text_still_fails(self) -> None:
        with self.assertRaises(CollabOutputError) as caught:
            parse("我先看看，然后告诉你。")
        self.assertEqual("no_envelope", caught.exception.reason)

    def test_empty_optional_text_is_absent(self) -> None:
        out = parse(
            '{"action": "final", "reply": "好的", '
            '"commentary": "", "brief_reason": ""}'
        )
        self.assertEqual("final", out.action)
        self.assertIsNone(out.commentary)
        self.assertIsNone(out.brief_reason)

    def test_reply_stays_non_empty(self) -> None:
        with self.assertRaises(CollabOutputError) as caught:
            parse('{"action": "final", "reply": ""}')
        self.assertEqual("protocol_shape", caught.exception.reason)

    def test_parser_does_not_apply_the_tool_whitelist(self) -> None:
        out = parse(
            dsml(
                invoke(
                    param("name", "made.up", string="true")
                    + "\n"
                    + param("arguments", "{}", string="false")
                )
            )
        )
        self.assertEqual("made.up", out.tool_call["name"])

    def test_plain_json_and_v1_are_unchanged(self) -> None:
        out = parse('{"action": "final", "messages": ["一", "二"]}')
        self.assertEqual(("一", "二"), out.bubbles())
        v1 = parse_collab_output(
            '{"action": "none", "reply": "好的"}',
            mode="consult",
            allowed_references=(),
            protocol_version="collab.v1",
        )
        self.assertEqual("none", v1.action)
        with self.assertRaises(InvalidInput):
            parse_collab_output(
                "not json",
                mode="consult",
                allowed_references=(),
                protocol_version="collab.v1",
            )


if __name__ == "__main__":
    unittest.main()
