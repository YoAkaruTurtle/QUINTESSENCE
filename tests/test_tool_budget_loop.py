"""Loop-level tests: the tool budget shapes what enters the history."""

from __future__ import annotations

import unittest

from quintessence_pcs.runtime.agent_loop import run_agent_loop
from quintessence_pcs.runtime.agent_protocol import StepOutcome, ToolCall
from quintessence_pcs.runtime.tool_budget import ToolBudgetPolicy, ToolResultBudgeter
from quintessence_pcs.foundation.jsonutil import canonical_json_text


class _Outcome:
    def __init__(self, ok: bool, code: str, payload: dict) -> None:
        self.ok = ok
        self.code = code
        self.payload = payload

    def content(self) -> str:
        return canonical_json_text(
            {"ok": self.ok, "code": self.code, "result": dict(self.payload)}
        )


class _Catalog:
    """Only the two methods the loop calls."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.executed = 0

    def definitions(self):
        return []

    def execute(self, call, *, handlers):
        self.executed += 1
        return _Outcome(True, "ok", self.payload)


class _Port:
    """Two rounds: one tool round of ``calls`` calls, then a final answer."""

    def __init__(self, calls: int = 1) -> None:
        self._calls = calls
        self.rounds = 0
        self.seen: list[list[dict]] = []

    def step(self, *, messages, tools):
        self.rounds += 1
        self.seen.append([dict(item) for item in messages])
        if self.rounds == 1:
            return StepOutcome(
                finish_reason="tool_calls",
                content="",
                reasoning_content="",
                tool_calls=tuple(
                    ToolCall(
                        call_id=f"c{index}",
                        name="cognition.belief.read",
                        arguments='{"limit": 20}',
                    )
                    for index in range(self._calls)
                ),
                usage={},
            )
        return StepOutcome(
            finish_reason="stop",
            content="done",
            reasoning_content="",
            tool_calls=(),
            usage={},
        )


class LoopBudgetTests(unittest.TestCase):
    def test_big_page_is_delivered_as_ref_only(self):
        """A page of records too large for the allowance becomes refs.

        Two calls in the round put this on the page allowance rather than the
        single-exact-read allowance -- the case a 20-record ``belief.read``
        actually hits.
        """

        payload = {
            "result": {
                "items": [{"claim": "x" * 50_000, "record_id": "rec-1", "record_version": 2}]
            }
        }
        catalog = _Catalog(payload)
        port = _Port(calls=2)
        budget = ToolResultBudgeter(ToolBudgetPolicy(page_max_bytes=1024))
        result = run_agent_loop(
            port,
            catalog,
            messages=[{"role": "user", "content": "q"}],
            handlers={},
            tool_budget=budget,
        )
        self.assertEqual(result.stopped_reason, "final")
        tool_message = next(
            item for item in result.messages if item.get("role") == "tool"
        )
        self.assertIn('"body_included":false', tool_message["content"].replace(" ", ""))
        self.assertIn("rec-1", tool_message["content"])
        self.assertNotIn("x" * 100, tool_message["content"])

    def test_an_oversized_single_exact_read_is_refused(self):
        """One record over the exact allowance is refused, not fragmented.

        Handing back a ref here would look like an answer to "read this one
        record"; handing back a fragment would be worse.  The structured
        refusal says the record did not fit and why.
        """

        payload = {"claim": "x" * 50_000, "record_id": "rec-exact", "record_version": 2}
        catalog = _Catalog(payload)
        budget = ToolResultBudgeter()
        result = run_agent_loop(
            _Port(),
            catalog,
            messages=[{"role": "user", "content": "q"}],
            handlers={},
            tool_budget=budget,
        )
        self.assertEqual(result.stopped_reason, "final")
        tool_message = next(
            item for item in result.messages if item.get("role") == "tool"
        )
        content = tool_message["content"].replace(" ", "")
        self.assertIn("record_too_large", content)
        self.assertIn('"executed":true', content)
        self.assertIn('"delivered":false', content)
        self.assertNotIn("x" * 100, tool_message["content"])

    def test_without_budget_the_history_is_unchanged(self):
        payload = {"claim": "y" * 50_000, "record_id": "rec-2"}
        catalog = _Catalog(payload)
        result = run_agent_loop(
            _Port(),
            catalog,
            messages=[{"role": "user", "content": "q"}],
            handlers={},
        )
        tool_message = next(
            item for item in result.messages if item.get("role") == "tool"
        )
        self.assertIn("y" * 1000, tool_message["content"])

    def test_exhausted_round_refuses_before_executing(self):
        payload = {"claim": "z" * 100, "record_id": "rec-3"}
        catalog = _Catalog(payload)
        budget = ToolResultBudgeter(
            ToolBudgetPolicy(
                round_max_bytes=1,
                round_max_units=1,
                single_exact_round_max_bytes=1,
                single_exact_round_max_units=1,
            )
        )
        result = run_agent_loop(
            _Port(),
            catalog,
            messages=[{"role": "user", "content": "q"}],
            handlers={},
            tool_budget=budget,
        )
        self.assertEqual(catalog.executed, 0)
        tool_message = next(
            item for item in result.messages if item.get("role") == "tool"
        )
        self.assertIn('"executed":false', tool_message["content"].replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
