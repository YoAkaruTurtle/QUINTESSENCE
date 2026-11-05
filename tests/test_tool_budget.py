"""Unit tests for the tool-result budget."""

from __future__ import annotations

import unittest

from quintessence_pcs.runtime.budget import estimate_units
from quintessence_pcs.runtime.tool_budget import (
    EXACT_READ_TOOL,
    REASON_NOT_EXECUTED,
    REASON_PAGE_BUDGET,
    REASON_TOO_LARGE,
    ToolBudgetPolicy,
    ToolResultBudgeter,
)
from quintessence_pcs.foundation.jsonutil import canonical_json_text


def _payload(size: int, *, record_id: str | None = "r-1") -> dict:
    body = {"claim": "x" * size}
    if record_id is not None:
        body["record_id"] = record_id
        body["record_version"] = 3
    return body


class PolicyTests(unittest.TestCase):
    def test_defaults_are_positive_and_bytes_are_not_units(self):
        policy = ToolBudgetPolicy()
        self.assertEqual(policy.page_max_bytes, 8192)
        self.assertEqual(policy.page_max_units, 4096)
        # The two scales are different on purpose: 8192 ASCII bytes cost far
        # fewer units than 8192 CJK bytes.
        self.assertNotEqual(
            estimate_units("a" * 8192), estimate_units("字" * 8192)
        )

    def test_rejects_non_positive_caps(self):
        with self.assertRaises(ValueError):
            ToolBudgetPolicy(page_max_bytes=0)


class ProjectionTests(unittest.TestCase):
    def test_small_body_is_delivered_whole(self):
        budget = ToolResultBudgeter()
        budget.begin_round(("cognition.belief.read",))
        payload = _payload(10)
        projection = budget.project(tool="cognition.belief.read", payload=payload)
        self.assertTrue(projection.body_included)
        self.assertFalse(projection.truncated)
        self.assertEqual(projection.payload, payload)

    def test_body_over_page_cap_becomes_ref_only(self):
        budget = ToolResultBudgeter(ToolBudgetPolicy(page_max_bytes=512))
        budget.begin_round(("cognition.partition",))
        projection = budget.project(
            tool="cognition.partition", payload=_payload(2000)
        )
        self.assertFalse(projection.body_included)
        self.assertTrue(projection.truncated)
        self.assertEqual(projection.reason, REASON_PAGE_BUDGET)
        # The identity survives; the body does not.
        self.assertEqual(
            projection.payload["result"]["ref"]["record_id"], "r-1"
        )
        self.assertNotIn("x" * 100, canonical_json_text(projection.payload))

    def test_body_without_identity_is_refused_not_fragmented(self):
        budget = ToolResultBudgeter(ToolBudgetPolicy(page_max_bytes=64))
        budget.begin_round(("cognition.partition",))
        projection = budget.project(
            tool="cognition.partition", payload=_payload(500, record_id=None)
        )
        self.assertFalse(projection.body_included)
        self.assertFalse(projection.payload["ok"])
        self.assertEqual(projection.reason, REASON_TOO_LARGE)

    def test_round_cap_degrades_later_results(self):
        policy = ToolBudgetPolicy(
            page_max_bytes=100_000,
            page_max_units=100_000,
            round_max_bytes=2000,
            round_max_units=100_000,
        )
        budget = ToolResultBudgeter(policy)
        budget.begin_round(("cognition.partition", "attention.list"))
        first = budget.project(tool="cognition.partition", payload=_payload(1200))
        second = budget.project(tool="attention.list", payload=_payload(1200))
        self.assertTrue(first.body_included)
        self.assertFalse(second.body_included)

    def test_single_exact_read_gets_the_larger_allowance(self):
        policy = ToolBudgetPolicy(page_max_bytes=512, exact_max_bytes=16384)
        exact = ToolResultBudgeter(policy)
        exact.begin_round((EXACT_READ_TOOL,))
        mixed = ToolResultBudgeter(policy)
        mixed.begin_round((EXACT_READ_TOOL, "cognition.partition"))
        payload = _payload(2000)
        self.assertTrue(
            exact.project(tool=EXACT_READ_TOOL, payload=payload).body_included
        )
        self.assertFalse(
            mixed.project(tool=EXACT_READ_TOOL, payload=payload).body_included
        )

    def test_units_are_counted_for_wide_characters(self):
        policy = ToolBudgetPolicy(
            page_max_bytes=100_000, page_max_units=512, round_max_units=10_000
        )
        budget = ToolResultBudgeter(policy)
        budget.begin_round(("cognition.partition",))
        projection = budget.project(
            tool="cognition.partition", payload={"claim": "汉字" * 400}
        )
        self.assertFalse(projection.body_included)

    def test_refusal_marks_the_call_not_executed(self):
        budget = ToolResultBudgeter()
        budget.begin_round(("cognition.belief.read",))
        projection = budget.refusal(tool="cognition.belief.read")
        self.assertFalse(projection.payload["result"]["executed"])
        self.assertEqual(projection.payload["code"], REASON_NOT_EXECUTED)

    def test_round_accounting_resets_between_rounds(self):
        policy = ToolBudgetPolicy(round_max_bytes=1000, round_max_units=100_000)
        budget = ToolResultBudgeter(policy)
        budget.begin_round(("cognition.partition",))
        budget.project(tool="cognition.partition", payload=_payload(600))
        self.assertLess(budget.remaining_bytes, 1000)
        budget.begin_round(("cognition.partition",))
        self.assertEqual(budget.remaining_bytes, 1000)


if __name__ == "__main__":
    unittest.main()
