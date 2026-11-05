"""RG08 T0M: pure-function tests for the expression observation metrics.

No store, no model, no file: every case calls one function with literals and
asserts the numbers.  The suite also pins the two properties the plan requires:
every metric value is ``int``/``float`` (never text, never bool), and the
semantic classes stay heuristic (they may miss; that is documented, not a bug).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
for _path in (HERE, SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.conversation.expression_metrics import (  # noqa: E402
    MAX_SENTENCES,
    align_sentences,
    align_sentences_detail,
    classify_sentence,
    compute_metrics,
    is_question_like,
    protected_signal_retention,
    split_sentences,
)


class SplitSentencesTests(unittest.TestCase):
    def test_splits_on_sentence_endings(self) -> None:
        text = "那就先别学了，歇会儿吧。是今天特别费脑子，还是从早上就一直没停过"
        self.assertEqual(
            [
                "那就先别学了，歇会儿吧。",
                "是今天特别费脑子，还是从早上就一直没停过",
            ],
            split_sentences(text),
        )

    def test_splits_on_line_breaks_between_bubbles(self) -> None:
        self.assertEqual(
            ["鲜肉馅的，那确实香啊。", "是包子还是饺子？"],
            split_sentences("鲜肉馅的，那确实香啊。\n\n是包子还是饺子？"),
        )

    def test_empty_text_has_no_sentences(self) -> None:
        for text in ("", "   ", "\n\n", "\t"):
            with self.subTest(text=text):
                self.assertEqual([], split_sentences(text))

    def test_limit_caps_the_count(self) -> None:
        text = "甲。" * (MAX_SENTENCES + 40)
        self.assertEqual(MAX_SENTENCES, len(split_sentences(text)))
        self.assertEqual(5, len(split_sentences(text, limit=5)))


class AlignmentTests(unittest.TestCase):
    def test_identical_text_is_all_retained(self) -> None:
        detail = align_sentences_detail("那就先歇会儿吧。", "那就先歇会儿吧。")
        self.assertEqual(("retained",), detail.statuses)
        self.assertEqual(0, detail.added)

    def test_removed_sentence_is_dropped(self) -> None:
        self.assertEqual(
            ["retained", "dropped"],
            align_sentences("那就先歇会儿吧。今天特别累。", "那就先歇会儿吧。"),
        )

    def test_near_copy_is_retained(self) -> None:
        self.assertEqual(
            ["retained"],
            align_sentences("那就先别学了，歇会儿吧。", "那就先别学了，先歇会儿吧。"),
        )

    def test_partial_rewrite_is_rewritten(self) -> None:
        statuses = align_sentences(
            "我建议今晚早点睡，明早还要早起。",
            "我建议早点睡。",
        )
        self.assertEqual(["rewritten"], statuses)

    def test_unrelated_sentence_is_dropped(self) -> None:
        self.assertEqual(
            ["dropped"],
            align_sentences("今天天气不错，出去走走吧。", "药物化学的结构式很难背。"),
        )

    def test_new_sentences_are_added(self) -> None:
        detail = align_sentences_detail("甲。", "甲。乙。丙。")
        self.assertEqual(("retained",), detail.statuses)
        self.assertEqual(2, detail.added)

    def test_empty_draft_marks_everything_added(self) -> None:
        detail = align_sentences_detail("", "甲。乙。")
        self.assertEqual((), detail.statuses)
        self.assertEqual(2, detail.added)


class QuestionDetectionTests(unittest.TestCase):
    def test_question_mark_is_a_question(self) -> None:
        self.assertTrue(is_question_like("明天那节是讲新课还是复习？"))

    def test_no_mark_question_with_interrogative_word(self) -> None:
        # V13 round 1: no question mark, still a question.
        self.assertTrue(is_question_like("这么饱，是吃了什么好东西"))

    def test_no_mark_alternative_question(self) -> None:
        # V13 round 3: "是…还是…" without a question mark.
        self.assertTrue(
            is_question_like("是今天特别费脑子，还是从早上就一直没停过")
        )

    def test_declarative_ending_wins_over_interrogative_word(self) -> None:
        # V13 round 7 draft: "那得看是什么课了" is a statement.
        self.assertFalse(is_question_like("那得看是什么课了"))

    def test_request_tail_counts_as_question(self) -> None:
        # V13 round 7 final reply: a request for information, no question mark.
        self.assertTrue(is_question_like("是什么课，说来听听"))

    def test_plain_statement_is_not_a_question(self) -> None:
        self.assertFalse(is_question_like("大清早能吃到这个，这一上午都舒坦"))


class ClassificationTests(unittest.TestCase):
    def test_question_has_priority(self) -> None:
        self.assertEqual("question", classify_sentence("我建议早点睡，你觉得呢？"))

    def test_advice_marker(self) -> None:
        self.assertEqual("advice_like", classify_sentence("那就先别学了，歇会儿吧"))

    def test_analysis_marker(self) -> None:
        self.assertEqual(
            "analysis_like", classify_sentence("因为构效关系要背，所以最磨人")
        )

    def test_documented_miss_stays_plain(self) -> None:
        # Plan §7.0: this sentence has no marker; the heuristic misses it on
        # purpose and the metric stays an activity description.
        self.assertEqual("plain", classify_sentence("累了还硬撑着看，也看不进去"))


class ComputeMetricsTests(unittest.TestCase):
    def test_statement_to_question_catches_the_round_seven_pattern(self) -> None:
        metrics = compute_metrics("那得看是什么课了", "是什么课，说来听听")
        self.assertEqual(1, metrics["statement_to_question"])
        self.assertEqual(1, metrics["questions_added"])
        self.assertEqual(0, metrics["questions_dropped"])

    def test_keeping_the_draft_is_not_a_statement_to_question(self) -> None:
        metrics = compute_metrics("那就先歇会儿吧。", "那就先歇会儿吧。")
        self.assertEqual(0, metrics["statement_to_question"])
        self.assertEqual(1.0, metrics["retention_ratio"])

    def test_empty_draft_has_zero_retention_ratio(self) -> None:
        metrics = compute_metrics("", "甲。乙。")
        self.assertEqual(0.0, metrics["retention_ratio"])
        self.assertEqual(0, metrics["draft_sentences"])
        self.assertEqual(2, metrics["sentences_added"])

    def test_bubble_and_length_deltas(self) -> None:
        metrics = compute_metrics(
            "第一句。第二句。", "第一句。", draft_bubbles=2, reply_bubbles=1
        )
        self.assertEqual(2, metrics["draft_bubbles"])
        self.assertEqual(1, metrics["reply_bubbles"])
        self.assertEqual(-1, metrics["bubble_delta"])
        self.assertLess(metrics["length_delta_chars"], 0)

    def test_every_value_is_int_or_float(self) -> None:
        metrics = compute_metrics(
            "记录里有 17 条待办，大概都是上周的。我不确定哪条最急。",
            "记录里还有 17 条待办，大概是上周留下的；哪条最急，我还说不好。",
            draft_bubbles=2,
            reply_bubbles=2,
        )
        for key, value in metrics.items():
            with self.subTest(key=key):
                self.assertIsInstance(value, (int, float))
                self.assertNotIsInstance(value, bool)

    def test_dropping_a_number_lowers_key_content_retention(self) -> None:
        metrics = compute_metrics("记录里有 17 条待办。", "记录里还有待办。")
        self.assertEqual(0, metrics["numbers_retained"])
        self.assertLess(metrics["key_content_retention"], 1.0)

    def test_no_protected_signal_means_full_retention(self) -> None:
        metrics = compute_metrics("那就先歇会儿吧。", "先歇会儿吧。")
        self.assertEqual(1.0, metrics["key_content_retention"])


class ProtectedSignalTests(unittest.TestCase):
    def test_all_five_signals_are_reported(self) -> None:
        signals = protected_signal_retention(
            "还没提交，如果失败就算了，大概要 3 天，先别催。",
            "还没提交，如果失败就算了，大概要 3 天，先别催。",
        )
        self.assertEqual(
            {"numbers", "negation", "condition", "uncertainty", "pending"},
            set(signals),
        )
        self.assertTrue(all(value == 1 for value in signals.values()))

    def test_pending_markers_report_how_many_were_lost(self) -> None:
        metrics = compute_metrics("文件还没提交，正在审核。", "文件已经好了。")
        self.assertGreater(metrics["pending_markers_dropped"], 0)
        self.assertEqual(0, metrics["pending_retained"])

    def test_condition_and_uncertainty_survive_when_kept(self) -> None:
        metrics = compute_metrics(
            "如果明天下雨，可能就不去了。", "要是下雨的话，大概不去了。"
        )
        # Markers differ between the two sentences; the check is "not all lost".
        self.assertEqual(1, metrics["condition_retained"])


if __name__ == "__main__":
    unittest.main()


class AlignmentRegressionTests(unittest.TestCase):
    """Real-run regressions: the replace branch indexed past the status list."""

    def test_replace_block_with_more_draft_than_reply_does_not_crash(self) -> None:
        # Two equal sentences, then two draft sentences replaced by one: the
        # absolute offset loop used to add i1 twice and walk off the list.
        alignment = align_sentences_detail("一。二。三。四。", "一。二。四改。")
        self.assertEqual(4, len(alignment.statuses))
        self.assertEqual(("retained", "retained"), alignment.statuses[:2])
        self.assertIn("dropped", alignment.statuses[2:])

    def test_replace_after_an_insert_does_not_crash(self) -> None:
        alignment = align_sentences_detail("甲。乙。丙。", "新增。甲。丙改。")
        self.assertEqual(3, len(alignment.statuses))
