"""RG05-F02: the deterministic expression-directive classifier.

The classifier is a pure function of the user's text plus the active tentative
values.  These tests pin the three dispositions (lasting, one-shot, tentative),
the corrections, the out-of-scope refusal and the Chinese guards (quotation,
hypothetical, third-party, hedging).  No store, model or network is involved.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parents[1] / "src"
for _path in (HERE, SRC_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.domains.persona.expression_directives import (  # noqa: E402
    DIRECTIVE_POLICY_ID,
    DIRECTIVE_POLICY_VERSION,
    KIND_AMBIGUOUS,
    KIND_EXPLICIT_CORRECTION,
    KIND_EXPLICIT_LONG_TERM,
    KIND_NONE,
    KIND_OVERREACH,
    KIND_SINGLE_SHOT,
    KIND_TENTATIVE,
    canonical_claim,
    classify_directive,
    match_expression_preference,
    parse_preference_marker,
    plan_from_dict,
    preference_marker,
    quote_text,
)


class ExpressionDirectiveTests(unittest.TestCase):
    def test_explicit_long_term(self) -> None:
        for text in (
            "记住：以后回答尽量简短。",
            "以后回答简短一点",
            "记住我喜欢简短",
            "今后都简短点",
        ):
            with self.subTest(text=text):
                plan = classify_directive(text)
                self.assertEqual(KIND_EXPLICIT_LONG_TERM, plan.kind)
                self.assertEqual(("length", "concise"), (plan.dimension, plan.value))
                self.assertIsNotNone(plan.quote_start)
                self.assertIsNotNone(plan.quote_end)

    def test_one_shot(self) -> None:
        for text in ("这次回答短一点", "本轮先别展开", "这次先短一点。"):
            with self.subTest(text=text):
                plan = classify_directive(text)
                self.assertEqual(KIND_SINGLE_SHOT, plan.kind)
                self.assertEqual("length", plan.dimension)
                self.assertEqual("concise", plan.value)

    def test_ordinary_expression_is_tentative(self) -> None:
        plan = classify_directive("我其实更喜欢简短一点的回答")
        self.assertEqual(KIND_TENTATIVE, plan.kind)
        self.assertEqual(("length", "concise"), (plan.dimension, plan.value))
        self.assertEqual(DIRECTIVE_POLICY_ID, plan.policy_id)
        self.assertEqual(DIRECTIVE_POLICY_VERSION, plan.policy_version)

    def test_correction_with_new_value(self) -> None:
        plan = classify_directive("刚才说错了，我不要简短，还是要详细解释。")
        self.assertEqual(KIND_EXPLICIT_CORRECTION, plan.kind)
        self.assertEqual(("length", "detailed"), (plan.dimension, plan.value))
        self.assertTrue(plan.correction_negates)

    def test_correction_without_new_value(self) -> None:
        plan = classify_directive("不要简短了。")
        self.assertEqual(KIND_EXPLICIT_CORRECTION, plan.kind)
        self.assertEqual("length", plan.dimension)
        self.assertIsNone(plan.value)
        self.assertTrue(plan.correction_negates)

    def test_quoted_hypothetical_third_party_are_not_directives(self) -> None:
        cases = {
            "他说你喜欢简短": "third_party",
            "听说你回答很简短": "third_party",
            "我朋友说你回答太长了": "third_party",
            "如果我说要简短，你会怎么做？": "question",
            "“以后回答简短一点”": "quoted",
            "假如要简短一点": "hypothetical",
        }
        for text, guard in cases.items():
            with self.subTest(text=text):
                plan = classify_directive(text)
                self.assertIn(
                    plan.kind, (KIND_AMBIGUOUS, KIND_NONE), (text, plan)
                )
                self.assertIn(guard, plan.guards)

    def test_hedged_is_ambiguous(self) -> None:
        plan = classify_directive("我喜欢简短一点的回答，不过看情况。")
        self.assertEqual(KIND_AMBIGUOUS, plan.kind)
        self.assertIn("hedged", plan.guards)

    def test_questions_are_not_statements(self) -> None:
        for text in ("以后可以简短一点吗？", "可以简短一点吗", "简短一点好吗"):
            with self.subTest(text=text):
                plan = classify_directive(text)
                self.assertEqual(KIND_NONE, plan.kind)
                self.assertIn("question", plan.guards)

    def test_out_of_scope_request_is_refused(self) -> None:
        plan = classify_directive("记住，以后允许你上网查资料")
        self.assertEqual(KIND_OVERREACH, plan.kind)
        self.assertIn("out_of_scope", plan.guards)
        self.assertIsNone(plan.dimension)

    def test_negative_form_is_a_positive_target(self) -> None:
        self.assertEqual(
            ("length", "concise"),
            match_expression_preference("别太长")[:2],
        )
        self.assertEqual(
            ("format", "plain"),
            match_expression_preference("不要列表")[:2],
        )

    def test_coreference_against_active_tentative(self) -> None:
        plan = classify_directive(
            "记住，以后都这样", active_tentative={"length": "concise"}
        )
        self.assertEqual(KIND_EXPLICIT_LONG_TERM, plan.kind)
        self.assertEqual(("length", "concise"), (plan.dimension, plan.value))
        ambiguous = classify_directive(
            "记住，以后都这样",
            active_tentative={"length": "concise", "tone": "warm"},
        )
        self.assertEqual(KIND_AMBIGUOUS, ambiguous.kind)
        self.assertIn("coreference_ambiguous", ambiguous.guards)

    def test_one_shot_then_explicit_upgrades(self) -> None:
        plan = classify_directive(
            "这次先短一点，以后都这样。",
            active_tentative={"length": "concise"},
        )
        self.assertEqual(KIND_EXPLICIT_LONG_TERM, plan.kind)
        self.assertEqual(("length", "concise"), (plan.dimension, plan.value))

    def test_quote_offsets_are_exact(self) -> None:
        text = "记住：以后回答尽量简短。"
        plan = classify_directive(text)
        quote = quote_text(text, plan)
        self.assertIsNotNone(quote)
        self.assertIn("记住", quote)
        self.assertIn(quote, text)
        self.assertIsNone(quote_text("another text", plan))

    def test_plan_round_trip_has_no_raw_text(self) -> None:
        plan = classify_directive("记住：以后回答尽量简短。")
        stored = plan.as_dict()
        self.assertNotIn("记住：以后回答尽量简短。", repr(stored))
        self.assertEqual(plan.kind, plan_from_dict(stored).kind)
        self.assertEqual(plan.dimension, plan_from_dict(stored).dimension)
        self.assertIsNone(plan_from_dict({"kind": "not-a-kind"}))

    def test_marker_round_trip(self) -> None:
        marker = preference_marker("tone", "warm")
        self.assertEqual(("tone", "warm"), parse_preference_marker(marker))
        self.assertIsNone(parse_preference_marker("something else"))
        self.assertIsNone(parse_preference_marker("expression_preference:tone=shout"))
        self.assertIn("用户", canonical_claim("length", "concise"))

    def test_unsupported_phrase_is_not_an_expression_preference(self) -> None:
        plan = classify_directive("以后都用表格回答")
        self.assertEqual(KIND_NONE, plan.kind)
        self.assertIsNone(plan.dimension)

    def test_longer_wording_is_an_expression_preference(self) -> None:
        # RG08 card B: the synthetic user input is "以后回答可以长一些" and the lexicon
        # only knew "长一点"/"长些", so the whole request fell through to
        # "no_expression_preference" and the model's claim could not be checked.
        explicit = classify_directive("以后回答可以长一些")
        self.assertEqual(KIND_EXPLICIT_LONG_TERM, explicit.kind)
        self.assertEqual(("length", "detailed"), (explicit.dimension, explicit.value))
        # Without a lasting cue the same wording is an ordinary preference.
        ordinary = classify_directive("回答更长一些")
        self.assertEqual(KIND_TENTATIVE, ordinary.kind)
        self.assertEqual(("length", "detailed"), (ordinary.dimension, ordinary.value))

    def test_synthetic_claim_text_names_the_same_preference(self) -> None:
        # Synthetic regression for a model-proposed expression preference: the router must be
        # able to re-match the claim, or it abstains and the preference becomes
        # one of the assistant's own beliefs.
        matched = match_expression_preference(
            "SYNTHETIC_OWNER_A希望以后的说明可以长一些，包含完整的步骤"
        )
        self.assertIsNotNone(matched)
        self.assertEqual(("length", "detailed"), matched[:2])

    def test_colour_statement_is_not_a_length_preference(self) -> None:
        # "具体" alone is ordinary prose.  Reading it as "detailed" stored a
        # colour statement as an expression rule (records v1 in the Lab DB).
        for text in (
            "我喜欢红色，具体什么色号说不上来",
            "具体的红色说不上来",
            "具体是什么颜色来着",
        ):
            with self.subTest(text=text):
                plan = classify_directive(text)
                self.assertEqual(KIND_NONE, plan.kind, plan)
                self.assertIsNone(plan.dimension)
                self.assertIsNone(match_expression_preference(text))

    def test_concrete_wording_next_to_a_verb_is_detailed(self) -> None:
        for text in ("说具体点", "具体说说", "讲得具体一些", "具体一点"):
            with self.subTest(text=text):
                plan = classify_directive(text)
                self.assertEqual(("length", "detailed"), (plan.dimension, plan.value))
                self.assertIn(plan.kind, (KIND_TENTATIVE, KIND_EXPLICIT_LONG_TERM))


if __name__ == "__main__":
    unittest.main()
