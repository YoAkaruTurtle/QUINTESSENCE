"""RG07: the deterministic expression checks and their observation metrics.

Pure unit tests: no store, no model.  Each hard rule has one positive and one
negative case, and the metrics are asserted to be counts only (never text), so
the event payload cannot leak the draft.
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

from quintessence_pcs.conversation.expression_check import (  # noqa: E402
    FAILURE_CERTAINTY_UPGRADE,
    FAILURE_EMPTY_REPLY,
    FAILURE_MEMORY_CLAIM,
    FAILURE_NEW_NUMBERS,
    FAILURE_REPEATS_COMMENTARY,
    FAILURE_TOO_LONG,
    FAILURE_UNCERTAINTY_DROPPED,
    check_expression,
)
from quintessence_pcs.conversation.expression_metrics import (  # noqa: E402
    compute_metrics,
)

DRAFT = "记录里有 17 条待办，大概都是上周的。我不确定哪条最急。"


def check(reply: str, **overrides):
    values = {
        "published_commentary": (),
        "expression_material": None,
        "pending_cognition_changes": 0,
        "examples": (),
    }
    values.update(overrides)
    return check_expression(DRAFT, reply, **values)


class HardRuleTests(unittest.TestCase):
    def test_natural_rewrite_passes(self) -> None:
        verdict = check("记录里还有 17 条待办，大概是上周留下的；哪条最急，我还说不好。")
        self.assertTrue(verdict.ok, verdict.failures)
        self.assertEqual({}, {
            key: value for key, value in verdict.metrics.items() if isinstance(value, str)
        })

    def test_empty_reply_is_refused(self) -> None:
        for reply in ("", "   "):
            with self.subTest(reply=reply):
                verdict = check(reply)
                self.assertFalse(verdict.ok)
                self.assertIn(FAILURE_EMPTY_REPLY, verdict.failures)

    def test_new_number_is_refused(self) -> None:
        verdict = check("记录里有 43 条待办，大概都是上周的。我不确定哪条最急。")
        self.assertIn(FAILURE_NEW_NUMBERS, verdict.failures)
        self.assertEqual(1, verdict.metrics["new_numbers"])

    def test_dropping_all_uncertainty_is_refused(self) -> None:
        verdict = check("记录里有 17 条待办，都是上周的，最急的是第一条。")
        self.assertIn(FAILURE_UNCERTAINTY_DROPPED, verdict.failures)

    def test_adding_certainty_is_refused(self) -> None:
        verdict = check("记录里有 17 条待办，一定是上周的，我不确定哪条最急。")
        self.assertIn(FAILURE_CERTAINTY_UPGRADE, verdict.failures)

    def test_memory_claim_is_only_refused_while_something_is_pending(self) -> None:
        reply = "好，我已经记住。"
        self.assertIn(
            FAILURE_MEMORY_CLAIM, check(reply, pending_cognition_changes=1).failures
        )
        self.assertNotIn(
            FAILURE_MEMORY_CLAIM, check(reply, pending_cognition_changes=0).failures
        )

    def test_verbatim_commentary_is_refused(self) -> None:
        verdict = check(
            "我翻一下。记录里有 17 条待办，大概都是上周的。我不确定哪条最急。",
            published_commentary=("我翻一下。",),
        )
        self.assertIn(FAILURE_REPEATS_COMMENTARY, verdict.failures)

    def test_runaway_growth_is_refused(self) -> None:
        reply = DRAFT * 40
        self.assertIn(FAILURE_TOO_LONG, check(reply).failures)

    def test_long_draft_may_shrink_without_a_length_floor(self) -> None:
        draft = "这是一段很长的解释。" * 80
        reply = "简短回答。"
        verdict = check_expression(draft, reply)
        self.assertTrue(verdict.ok, verdict.failures)
        self.assertLess(verdict.metrics["length_ratio"], 0.1)
        self.assertNotIn("too_short", verdict.failures)


class MetricTests(unittest.TestCase):
    def test_metrics_are_counts_and_ratios_only(self) -> None:
        verdict = check(
            "记录里有 17 条待办呀，大概都是上周的。我不确定哪条最急呢！先歇一会儿吧。",
            published_commentary=(),
        )
        self.assertTrue(verdict.ok, verdict.failures)
        metrics = verdict.metrics
        for key, value in metrics.items():
            self.assertIsInstance(value, (int, float), key)
        self.assertGreaterEqual(metrics["warm_markers_per_100"], 0)
        self.assertGreaterEqual(metrics["comfort_template_hits"], 1)
        self.assertGreaterEqual(metrics["new_questions"], 0)
        self.assertGreaterEqual(metrics["draft_lcs_chars"], 0)
        self.assertEqual(0, metrics["example_lcs_chars"])
        self.assertGreater(metrics["reply_chars"], 0)
        self.assertEqual(len(DRAFT), metrics["draft_chars"])

    def test_example_overlap_is_measured_not_gated(self) -> None:
        example = {"assistant": "还是信你一回吧。"}
        verdict = check(
            "记录里有 17 条待办，大概都是上周的。我不确定哪条最急。还是信你一回吧。",
            examples=(example,),
        )
        self.assertTrue(verdict.ok, verdict.failures)
        self.assertGreaterEqual(verdict.metrics["example_lcs_chars"], 5)

    def test_new_question_is_measured(self) -> None:
        verdict = check("记录里有 17 条待办，大概都是上周的。我不确定哪条最急。要不要我列出来？")
        self.assertEqual(1, verdict.metrics["new_questions"])


class SharedMetricBundleTests(unittest.TestCase):
    """RG08 T0B: one implementation, merged verbatim, never promoted to a gate."""

    REPLY = "记录里有 17 条待办，大概是上周留下的；哪条最急，我还说不好。"

    def test_metrics_include_every_shared_module_key(self) -> None:
        verdict = check(self.REPLY)
        shared = compute_metrics(DRAFT, self.REPLY)
        self.assertEqual([], sorted(set(shared) - set(verdict.metrics)))
        for key, value in verdict.metrics.items():
            self.assertIsInstance(value, (int, float), key)

    def test_checker_metrics_match_the_shared_module(self) -> None:
        # The two callers of the bundle must agree key for key; a drift here
        # would mean a second implementation exists again.
        verdict = check(self.REPLY)
        shared = compute_metrics(DRAFT, self.REPLY)
        for key, expected in shared.items():
            self.assertEqual(expected, verdict.metrics[key], key)

    def test_metric_changes_do_not_rescue_a_hard_failure(self) -> None:
        # Metrics say "the reply got shorter" (sentences dropped, low
        # retention), but a new number is a hard failure.  The observation
        # half may never overrule the gate half.
        verdict = check("记录里有 43 条待办。")
        self.assertIn(FAILURE_NEW_NUMBERS, verdict.failures)
        self.assertFalse(verdict.ok)
        self.assertGreaterEqual(verdict.metrics["sentences_dropped"], 1)
        self.assertLess(verdict.metrics["retention_ratio"], 1.0)

    def test_empty_reply_still_carries_the_shared_bundle(self) -> None:
        verdict = check("")
        self.assertIn(FAILURE_EMPTY_REPLY, verdict.failures)
        for key in compute_metrics(DRAFT, ""):
            self.assertIn(key, verdict.metrics)


if __name__ == "__main__":
    unittest.main()
