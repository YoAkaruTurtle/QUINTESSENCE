"""RG08: the controlled plain-text final fallback is format-only and narrow.

The pure classifier is tested without a store or a provider: every rule here is
about the text and the provider's finish reason, never about guessing intent.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (REPO_ROOT / "src",):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.conversation.plain_text_final import (  # noqa: E402
    classify_parse_rejection,
    plain_text_final,
)
from quintessence_pcs.runtime.collab_protocol import (  # noqa: E402
    COLLAB_V2_PROTOCOL_VERSION,
    parse_collab_output,
)

BAR = "｜｜DSML｜｜"

#: The two content strings stored for turn
#: ``4e853344-4719-4f2d-8cae-dd4a0c36ac6d`` (finish_reason=stop each).  They are
#: replayed offline here: no provider is contacted, so this proves the fallback
#: classification with zero model calls.
FAILED_TURN_FIRST = (
    "那我就在这边等着，不着急。你已经忙了一阵吧，别为了赶进度把自己熬太狠。"
)
FAILED_TURN_REPAIR = (
    "那我等着。既然你这么说，我就当是真的快了，别让我等太久，"
    "也别熬到忘了自己在准备什么"
)


def admit(raw, *, finish_reason="stop", protocol=COLLAB_V2_PROTOCOL_VERSION):
    return plain_text_final(
        raw, finish_reason=finish_reason, protocol_version=protocol
    )


class PlainTextFinalTest(unittest.TestCase):
    def test_a_complete_stop_answer_is_admitted_and_trimmed(self) -> None:
        verdict = admit("  那就等你忙完，别熬太晚。\n")
        self.assertIsNotNone(verdict)
        self.assertEqual("那就等你忙完，别熬太晚。", verdict.text)
        self.assertEqual("plain_text_final", verdict.reason)

    def test_only_stop_is_admitted(self) -> None:
        for finish_reason in (None, "length", "content_filter", "tool_calls"):
            with self.subTest(finish_reason=finish_reason):
                self.assertIsNone(admit("完整回答。", finish_reason=finish_reason))

    def test_v1_is_never_admitted(self) -> None:
        self.assertIsNone(admit("完整回答。", protocol="collab.v1"))

    def test_empty_blank_and_punctuation_only_are_refused(self) -> None:
        for raw in ("", "   ", "\n\n", "……", "？！", "---"):
            with self.subTest(raw=raw):
                self.assertIsNone(admit(raw))

    def test_overlong_text_is_refused(self) -> None:
        self.assertIsNone(admit("好" * 16385))

    def test_json_action_envelopes_are_refused(self) -> None:
        for raw in (
            '{"action":"final","reply":"好的"}',
            '{"action": "final", "messages": ["好的"]}',
            '{"action":"call_tool","tool_call":{"name":"x","arguments":{}}}',
            '{"action":"final","reply":',
            '["action", "final"]',
            '"just a JSON string"',
            "12345",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(admit(raw))

    def test_mixed_protocol_json_is_refused(self) -> None:
        for raw in (
            '好的。 {"action":"final","reply":"好的。"}',
            '先查一下 "tool_call": {"name": "web.search"}',
            "回答如下：\n```json\n{\"action\":\"final\"}\n```",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(admit(raw))

    def test_dsml_and_tool_markers_are_refused(self) -> None:
        for raw in (
            f"<{BAR} calls></{BAR} calls>",
            "<tool_call name=\"web.search\"></tool_call>",
            "先看看 <parameter name=\"query\">x</parameter>",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(admit(raw))

    def test_operation_claims_are_refused(self) -> None:
        for raw in (
            "已经保存好了。",
            "我已查询过这条记录。",
            "我查到了，是这个。",
            "已经完成。",
            "已写入文件。",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(admit(raw))

    def test_second_person_encouragement_is_not_the_assistants_claim(self) -> None:
        verdict = admit("你已经完成了很多，先歇一会儿吧。")
        self.assertIsNotNone(verdict)

    def test_control_characters_are_refused(self) -> None:
        self.assertIsNone(admit("好的\x00。"))

    def test_classifier_separates_tool_attempts_from_reply_format(self) -> None:
        for raw in (
            f"<{BAR} calls><{BAR} invoke name=\"call_tool\"></{BAR} invoke></{BAR} calls>",
            "<tool_call name=\"web.search\"></tool_call>",
            '{"action":"call_tool","tool_call":{"name":"web.search"}}',
            '{"action":"call_tool"',
            '{"tool_call":{"name":"web.search"}}',
        ):
            with self.subTest(raw=raw):
                self.assertEqual("tool", classify_parse_rejection(raw))
        for raw in (
            "普通的一句话。",
            '{"action":"final","reply":',
            '{"action":"final","tool_call":{"name":"web.search","arguments":{}}}',
            '{"action":"wait"}',
            None,
        ):
            with self.subTest(raw=raw):
                self.assertEqual("reply", classify_parse_rejection(raw))


class FailedTurnReplayTest(unittest.TestCase):
    """Replay the two stored outputs of the failed turn without a provider."""

    def test_both_original_outputs_are_non_envelopes(self) -> None:
        for text in (FAILED_TURN_FIRST, FAILED_TURN_REPAIR):
            with self.subTest(text=text):
                with self.assertRaises(Exception) as caught:
                    parse_collab_output(
                        text,
                        mode="consult",
                        allowed_references=(),
                        protocol_version=COLLAB_V2_PROTOCOL_VERSION,
                    )
                self.assertEqual("no_envelope", getattr(caught.exception, "reason"))

    def test_the_repair_answer_is_admitted_as_the_plain_text_final(self) -> None:
        verdict = admit(FAILED_TURN_REPAIR, finish_reason="stop")
        self.assertIsNotNone(verdict)
        self.assertEqual(FAILED_TURN_REPAIR, verdict.text)
        # Both stored responses were complete answers; the last one is the one
        # the exhausted generation actually returned.
        self.assertIsNotNone(admit(FAILED_TURN_FIRST, finish_reason="stop"))


if __name__ == "__main__":
    unittest.main()
