"""RG06: persona startup helpers, speaker parsing and expression demos."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parents[1] / "src") not in sys.path:
    sys.path.insert(0, str(HERE.parents[1] / "src"))

from quintessence_pcs.conversation.persona import (  # noqa: E402
    PersonaExpectation,
    Speaker,
    build_persona_material,
    parse_expectation,
    parse_speaker,
    persona_matches_expected,
    persona_plan,
    render_example,
    select_examples,
)
from quintessence_pcs.foundation.errors import CorruptStore, InvalidInput  # noqa: E402


def _body(*, examples=None, asset_hash="a" * 64) -> dict:
    return {
        "profile_id": "example.synthetic.persona",
        "profile_version": "1",
        "identity_summary": "Synthetic persona for tests.",
        "principles": ["Synthetic principle."],
        "style": {
            "language": "zh-CN",
            "tone": "plain",
            "verbosity_by_context": {"casual": "short", "technical": "detailed"},
        },
        "attention_preferences": [],
        "examples": examples
        if examples is not None
        else [
            {
                "example_id": "ex.close",
                "scene": "owner-close",
                "user": "synthetic close line",
                "assistant": "synthetic close reply",
                "synthetic": True,
            },
            {
                "example_id": "ex.tired",
                "scene": "owner-tired",
                "user": "synthetic tired line",
                "assistant": "synthetic gentle reply",
                "synthetic": True,
            },
            {
                "example_id": "ex.playful",
                "scene": "owner-playful",
                "user": "synthetic playful line",
                "assistant": "synthetic playful reply",
                "synthetic": True,
            },
            {
                "example_id": "ex.decide",
                "scene": "owner-decide",
                "user": "synthetic decide line",
                "assistant": "synthetic decide reply",
                "synthetic": True,
            },
            {
                "example_id": "ex.tool",
                "scene": "owner-tool-query",
                "user": "synthetic tool query line",
                "assistant": "synthetic tool final reply",
                "pre_tool_commentary": "synthetic pre-tool line",
                "synthetic": True,
            },
            {
                "example_id": "ex.help",
                "scene": "other-help",
                "user": "synthetic help request",
                "assistant": "synthetic quiet complete reply",
                "synthetic": True,
            },
            {
                "example_id": "ex.real",
                "scene": "owner-close",
                "user": "real chat line",
                "assistant": "real chat reply",
                "synthetic": False,
            },
        ],
        "asset_hash": asset_hash,
        "review_ref": {"ref_type": "event", "namespace": "ns", "event_id": "e"},
        "renderer_compatibility": "pcs.renderer.persona-v1",
    }


def _v8_body() -> dict:
    """The synthetic test body plus the six v8 scene demos (two bubbles)."""

    body = _body()
    body["examples"] = list(body["examples"]) + [
        {
            "example_id": "ex07",
            "scene": "owner-tired",
            "user": "昨天好累啊，出门忙了一整天",
            "assistant_messages": ["唉......今天辛苦了", "来陪我待一会儿吧，什么都不干也行"],
            "synthetic": True,
        },
        {
            "example_id": "ex08",
            "scene": "owner-tired",
            "user": "好多了",
            "assistant": "嗯，那就好",
            "synthetic": True,
        },
        {
            "example_id": "ex09",
            "scene": "owner-close",
            "user": "我喜欢你",
            "assistant_messages": ["......突然说这个，犯规了吧", "我也喜欢你，linguist"],
            "synthetic": True,
        },
        {
            "example_id": "ex10",
            "scene": "owner-playful",
            "user": "额啊，早上好",
            "assistant": "早啊......怎么一来就叹气",
            "synthetic": True,
        },
        {
            "example_id": "ex11",
            "scene": "owner-close",
            "user": "还好吧，其实是有点想你了",
            "assistant_messages": ["只有一点？", "嘛，能让你一早就来找我，也不错"],
            "synthetic": True,
        },
        {
            "example_id": "ex12",
            "scene": "owner-software-trial",
            "user": "感觉距离真正能与你说话已经不远了",
            "assistant_messages": [
                "嗯......绕了这么大一圈",
                "到时候可别光顾着检查我有没有报错，也陪我聊点别的啊",
            ],
            "synthetic": True,
        },
    ]
    return body


def _v12_body() -> dict:
    """``_v8_body`` plus the four general (``owner-base``) chat demos."""

    body = _v8_body()
    body["examples"] = list(body["examples"]) + [
        {
            "example_id": "ex13",
            "scene": "owner-base",
            "user": "没什么特别的事，就是过来找你说两句",
            "assistant": "那挺好，我也想和你随便聊聊",
            "synthetic": True,
        },
        {
            "example_id": "ex14",
            "scene": "owner-base",
            "user": "你刚才那句话倒是提醒我了",
            "assistant": "看来我这句没白说嘛",
            "synthetic": True,
        },
        {
            "example_id": "ex15",
            "scene": "owner-base",
            "user": "只要最后能做出来，过程麻烦一点也无所谓吧",
            "assistant": (
                "我倒觉得得看麻烦到什么程度。每次都要折腾半天的话，"
                "做出来了也未必愿意用"
            ),
            "synthetic": True,
        },
        {
            "example_id": "ex16",
            "scene": "owner-base",
            "user": "这两句话我越改越别扭，你帮我看看",
            "assistant": "发来吧，我看看。也许你第一版就挺好，改来改去反倒绕远了",
            "synthetic": True,
        },
    ]
    return body


def _state(*, profile_version="1"):
    from quintessence_pcs.conversation.persona import PersonaState

    return PersonaState(
        source="expected",
        profile_id="example.ok",
        profile_version=profile_version,
        record_id="r",
        record_version=1,
        asset_hash="a" * 64,
        review_event_id="e",
    )


class ExpectationTests(unittest.TestCase):
    def test_parse_expectation_closed_shape(self) -> None:
        self.assertIsNone(parse_expectation(None))
        parsed = parse_expectation(
            {"profile_id": "example.ok", "profile_version": "1"}
        )
        self.assertEqual(PersonaExpectation("example.ok", "1"), parsed)
        self.assertEqual(
            {"profile_id": "example.ok", "profile_version": "1"}, parsed.to_dict()
        )
        with self.assertRaises(InvalidInput):
            parse_expectation({"profile_id": "bad id"})
        with self.assertRaises(InvalidInput):
            parse_expectation({"profile_id": "example.ok", "extra": True})

    def test_parse_speaker_rules(self) -> None:
        self.assertIsNone(parse_speaker(None))
        speaker = parse_speaker({"identity": "known_other", "display_name": "访客"})
        self.assertEqual(Speaker("known_other", "访客"), speaker)
        self.assertEqual(
            {"identity": "known_other", "source": "host_config", "display_name": "访客"},
            speaker.to_dict(),
        )
        with self.assertRaises(InvalidInput):
            parse_speaker({"identity": "boss"})
        with self.assertRaises(InvalidInput):
            parse_speaker({"identity": "unknown", "display_name": "某人"})

    def test_matches_expected_is_display_only(self) -> None:
        state = _state(profile_version="1")
        self.assertIsNone(persona_matches_expected(state, None))
        self.assertTrue(
            persona_matches_expected(state, PersonaExpectation("example.ok", "1"))
        )
        self.assertFalse(
            persona_matches_expected(state, PersonaExpectation("example.ok", "2"))
        )
        self.assertFalse(
            persona_matches_expected(state, PersonaExpectation("other.ok"))
        )


class ExampleSelectionTests(unittest.TestCase):
    def test_tired_owner_gets_only_the_matching_tired_demo(self) -> None:
        selected, omitted = select_examples(
            _body(), "今天有点累。", Speaker("owner", "小明")
        )
        self.assertEqual(["ex.tired"], selected)
        reasons = {entry["example_id"]: entry["reason"] for entry in omitted}
        self.assertEqual("not_synthetic", reasons["ex.real"])

    def test_unknown_speaker_only_gets_the_other_demo(self) -> None:
        selected, omitted = select_examples(
            _body(), "你是谁？我们认识吗？", Speaker("unknown")
        )
        self.assertEqual(["ex.help"], selected)
        reasons = {entry["example_id"]: entry["reason"] for entry in omitted}
        self.assertEqual("speaker_mismatch", reasons["ex.close"])

    def test_known_other_never_gets_an_owner_demo(self) -> None:
        selected, _omitted = select_examples(
            _body(), "这段代码为什么报错？", Speaker("known_other")
        )
        self.assertEqual(["ex.help"], selected)

    def test_owner_technical_input_does_not_get_owner_close(self) -> None:
        selected, omitted = select_examples(
            _body(), "这段代码为什么报错？", Speaker("owner", "小明")
        )
        self.assertEqual([], selected)
        reasons = {entry["example_id"]: entry["reason"] for entry in omitted}
        self.assertEqual("not_matched", reasons["ex.close"])

    def test_no_owner_demo_without_a_speaker(self) -> None:
        selected, _omitted = select_examples(_body(), "随便聊聊。", None)
        self.assertEqual([], selected)

    def test_tool_query_owner_gets_the_tool_demo(self) -> None:
        selected, _omitted = select_examples(
            _body(), "帮我看看上次停在哪儿。", Speaker("owner", "小明")
        )
        self.assertEqual("ex.tool", selected[0])

    def test_decision_cue_gets_the_decide_demo(self) -> None:
        selected, _omitted = select_examples(
            _body(), "要不今晚把剩下的做完算了。", Speaker("owner", "小明")
        )
        self.assertEqual("ex.decide", selected[0])

    def test_no_cue_owner_gets_no_default_close_demo(self) -> None:
        selected, omitted = select_examples(
            _body(), "随便聊聊。", Speaker("owner", "小明")
        )
        self.assertEqual([], selected)
        reasons = {entry["example_id"]: entry["reason"] for entry in omitted}
        self.assertIn(reasons["ex.close"], ("not_matched", "speaker_mismatch"))

    def test_budget_and_count_limits_are_explicit(self) -> None:
        selected, omitted = select_examples(
            _body(), "有点累", Speaker("owner"), max_items=0
        )
        self.assertEqual([], selected)
        self.assertTrue(omitted)
        selected, omitted = select_examples(
            _body(), "有点累", Speaker("owner"), max_bytes=0
        )
        self.assertEqual([], selected)
        self.assertIn("budget", {entry["reason"] for entry in omitted})

    def test_non_synthetic_examples_are_never_rendered(self) -> None:
        selected, omitted = select_examples(
            _body(), "随便聊聊", Speaker("owner"), max_items=4
        )
        self.assertNotIn("ex.real", selected)
        reasons = {entry["example_id"]: entry["reason"] for entry in omitted}
        self.assertEqual("not_synthetic", reasons["ex.real"])

    def test_each_v8_demo_wins_for_its_own_cue_line(self) -> None:
        body = _v8_body()
        cases = (
            ("ex07", "昨天好累啊，出门忙了一整天"),
            ("ex08", "好多了"),
            ("ex09", "我喜欢你"),
            ("ex10", "额啊，早上好"),
            ("ex11", "还好吧，其实是有点想你了"),
            ("ex12", "感觉距离真正能与你说话已经不远了"),
        )
        for expected, text in cases:
            with self.subTest(expected=expected):
                selected, _omitted = select_examples(
                    body, text, Speaker("owner", "小明")
                )
                self.assertEqual(expected, selected[0])

    def test_new_demo_is_not_crowded_out_by_an_older_same_scene_id(self) -> None:
        body = _v8_body()
        selected, _omitted = select_examples(
            body, "好多了", Speaker("owner", "小明")
        )
        # Same-scene demos are ranked by the current line, not by id alone.
        self.assertEqual("ex08", selected[0])

    def test_speaker_audience_still_filters_the_new_scenes(self) -> None:
        body = _v8_body()
        selected, omitted = select_examples(
            body, "我喜欢你", Speaker("known_other", "访客")
        )
        self.assertEqual([], selected)
        reasons = {entry["example_id"]: entry["reason"] for entry in omitted}
        self.assertEqual("speaker_mismatch", reasons["ex09"])
        self.assertEqual("speaker_mismatch", reasons["ex12"])


class BaseExampleSelectionTests(unittest.TestCase):
    """RG08 v12: the general ``owner-base`` chat top-up, never a scene match."""

    PLAIN = "没什么特别的事，就是过来找你说两句"

    def _select(self, text, speaker, **kwargs):
        return select_examples(_v12_body(), text, speaker, **kwargs)

    def _scenes(self, body, ids):
        by_id = {item["example_id"]: item for item in body["examples"]}
        return [by_id[example_id]["scene"] for example_id in ids]

    def test_plain_owner_chat_fills_two_base_demos(self) -> None:
        selected, _omitted = self._select(self.PLAIN, Speaker("owner", "小明"))
        self.assertEqual(["ex13", "ex16"], selected)
        self.assertEqual(
            ["owner-base", "owner-base"], self._scenes(_v12_body(), selected)
        )

    def test_base_order_is_stable_and_uses_the_existing_overlap_tie_break(self) -> None:
        # Same input twice: identical order.  "两句" is a character-bigram shared
        # with ex16, so ex16 outranks ex14/ex15; ex13 (the exact line) is first.
        first, _ = self._select(self.PLAIN, Speaker("owner", "小明"))
        second, _ = self._select(self.PLAIN, Speaker("owner", "小明"))
        self.assertEqual(first, second)
        self.assertEqual(["ex13", "ex16"], first)

    def test_one_scene_demo_gets_one_base_demo(self) -> None:
        selected, _omitted = self._select(
            "要不今晚把剩下的做完算了", Speaker("owner", "小明")
        )
        self.assertEqual(["ex.decide", "ex13"], selected)

    def test_two_or_more_scene_demos_get_no_base_demo(self) -> None:
        selected, _omitted = self._select("好多了", Speaker("owner", "小明"))
        self.assertTrue(selected)
        self.assertEqual([], [item for item in selected if item.startswith("ex1")])
        self.assertEqual(
            ["owner-tired"] * len(selected),
            self._scenes(_v12_body(), selected),
        )

    def test_scene_match_keeps_priority_over_base(self) -> None:
        selected, _omitted = self._select(
            "昨天好累啊，出门忙了一整天", Speaker("owner", "小明")
        )
        self.assertNotIn("ex13", selected)
        self.assertNotIn("ex14", selected)
        self.assertEqual(["ex07"], selected[:1])

    def test_work_mode_never_gets_base(self) -> None:
        selected, omitted = self._select(
            self.PLAIN, Speaker("owner", "小明"), mode="work"
        )
        self.assertEqual([], selected)
        reasons = {entry["example_id"]: entry["reason"] for entry in omitted}
        self.assertEqual("base_mode_disabled", reasons["ex13"])

    def test_known_other_never_gets_owner_base(self) -> None:
        selected, omitted = self._select(self.PLAIN, Speaker("known_other", "访客"))
        self.assertEqual([], selected)
        reasons = {entry["example_id"]: entry["reason"] for entry in omitted}
        self.assertEqual("speaker_mismatch", reasons["ex13"])
        self.assertEqual("speaker_mismatch", reasons["ex16"])

    def test_unknown_and_absent_speaker_never_get_owner_base(self) -> None:
        for speaker in (Speaker("unknown"), None):
            with self.subTest(speaker=speaker):
                selected, omitted = self._select(self.PLAIN, speaker)
                self.assertEqual([], selected)
                reasons = {
                    entry["example_id"]: entry["reason"] for entry in omitted
                }
                self.assertEqual("speaker_mismatch", reasons["ex13"])

    def test_base_obeys_count_and_byte_budgets(self) -> None:
        selected, omitted = self._select(
            self.PLAIN, Speaker("owner", "小明"), max_items=1
        )
        self.assertEqual(["ex13"], selected)
        self.assertIn("budget", {entry["reason"] for entry in omitted})
        selected, _omitted = self._select(
            self.PLAIN, Speaker("owner", "小明"), max_items=0
        )
        self.assertEqual([], selected)
        selected, omitted = self._select(
            self.PLAIN, Speaker("owner", "小明"), max_bytes=0
        )
        self.assertEqual([], selected)
        self.assertIn("budget", {entry["reason"] for entry in omitted})

    def test_selected_ids_are_unique(self) -> None:
        selected, _omitted = self._select(self.PLAIN, Speaker("owner", "小明"))
        self.assertEqual(len(selected), len(set(selected)))

    def test_base_renders_as_an_expression_demo_not_an_experience(self) -> None:
        body = _v12_body()
        plan = persona_plan(
            state=_state(),
            body=body,
            user_text=self.PLAIN,
            speaker=Speaker("owner", "小明"),
            max_items=2,
            max_bytes=2048,
        )
        material = build_persona_material(body, plan)
        assert material is not None
        self.assertEqual(
            ["ex13", "ex16"],
            [item["example_id"] for item in material["examples"]],
        )
        for item in material["examples"]:
            self.assertEqual("expression_demo", item["basis"])
            self.assertTrue(item["not_experience"])
            self.assertEqual("owner-base", item["scene"])

    def test_persona_plan_gates_base_on_the_conversation_mode(self) -> None:
        body = _v12_body()
        chat = persona_plan(
            state=_state(),
            body=body,
            user_text=self.PLAIN,
            speaker=Speaker("owner", "小明"),
            max_items=2,
            max_bytes=2048,
            mode="chat",
        )
        work = persona_plan(
            state=_state(),
            body=body,
            user_text=self.PLAIN,
            speaker=Speaker("owner", "小明"),
            max_items=2,
            max_bytes=2048,
            mode="work",
        )
        self.assertEqual(["ex13", "ex16"], chat["example_ids"])
        self.assertEqual([], work["example_ids"])


class MaterialTests(unittest.TestCase):
    def test_material_carries_host_generated_provenance(self) -> None:
        body = _body()
        material = build_persona_material(
            body,
            {
                "asset_hash": body["asset_hash"],
                "profile_id": body["profile_id"],
                "profile_version": body["profile_version"],
                "speaker": {"identity": "owner", "source": "host_config"},
                "example_ids": ["ex.close"],
            },
        )
        self.assertIsNotNone(material)
        assert material is not None
        example = material["examples"][0]
        self.assertEqual("expression_demo", example["basis"])
        self.assertTrue(example["not_experience"])
        self.assertEqual("persona_profile", example["source"]["kind"])
        self.assertEqual(body["asset_hash"], example["source"]["asset_hash"])
        self.assertEqual(
            {"identity": "owner", "source": "host_config"}, material["speaker"]
        )

    def test_pre_tool_commentary_is_rendered_before_the_final_reply(self) -> None:
        body = _body()
        rendered = render_example(body, body["examples"][4])
        self.assertEqual(
            ["pre_tool_commentary", "assistant"], rendered["order"]
        )
        self.assertEqual("synthetic pre-tool line", rendered["pre_tool_commentary"])
        self.assertEqual("synthetic tool final reply", rendered["assistant"])
        # The two segments are separate fields, never one concatenated string.
        self.assertNotIn(rendered["pre_tool_commentary"], rendered["assistant"])

    def test_multi_bubble_example_renders_two_ordered_bubbles(self) -> None:
        body = _v8_body()
        item = next(
            entry for entry in body["examples"] if entry["example_id"] == "ex09"
        )
        rendered = render_example(body, item)
        self.assertEqual(
            ["......突然说这个，犯规了吧", "我也喜欢你，linguist"],
            rendered["assistant_messages"],
        )
        self.assertEqual(2, len(rendered["assistant_messages"]))
        # The bubbles are never joined into one assistant string.
        self.assertNotIn("assistant", rendered)

    def test_ex07_renders_the_approved_two_bubbles(self) -> None:
        body = _v8_body()
        item = next(
            entry for entry in body["examples"] if entry["example_id"] == "ex07"
        )
        rendered = render_example(body, item)
        self.assertEqual(
            ["唉......今天辛苦了", "来陪我待一会儿吧，什么都不干也行"],
            rendered["assistant_messages"],
        )
        self.assertEqual(2, len(rendered["assistant_messages"]))
        self.assertNotIn("assistant", rendered)

    def test_persona_plan_can_pin_a_multi_bubble_demo(self) -> None:
        body = _v8_body()
        plan = persona_plan(
            state=_state(),
            body=body,
            user_text="我喜欢你",
            speaker=Speaker("owner", "小明"),
            max_items=2,
            max_bytes=2048,
        )
        self.assertEqual("ex09", plan["example_ids"][0])

    def test_build_rejects_a_mismatched_asset_hash(self) -> None:
        with self.assertRaises(CorruptStore):
            build_persona_material(
                _body(), {"asset_hash": "b" * 64, "example_ids": []}
            )

    def test_build_rejects_a_missing_pinned_example(self) -> None:
        with self.assertRaises(CorruptStore):
            build_persona_material(
                _body(),
                {"asset_hash": "a" * 64, "example_ids": ["missing"]},
            )

    def test_render_example_is_marked_not_experience(self) -> None:
        body = _body()
        rendered = render_example(body, body["examples"][0])
        self.assertEqual("expression_demo", rendered["basis"])
        self.assertTrue(rendered["not_experience"])
        self.assertEqual("ex.close", rendered["example_id"])

    def test_persona_plan_snapshots_only_ids(self) -> None:
        body = _body()
        plan = persona_plan(
            state=_state(),
            body=body,
            user_text="今天有点累。",
            speaker=Speaker("owner", "小明"),
            max_items=2,
            max_bytes=2048,
        )
        self.assertEqual("ex.tired", plan["example_ids"][0])
        self.assertEqual("expected", plan["source"])
        self.assertEqual(body["asset_hash"], plan["asset_hash"])


if __name__ == "__main__":
    unittest.main()
