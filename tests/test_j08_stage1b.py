"""J08 stage-1b: request preview, delivered-body registry and 70 KB loads.

Three properties are under test here, all of them about a refusal that has to
happen *before* something irreversible:

* the next request is priced with the same builder the adapter uses, and a
  request that cannot fit stops the loop before any send;
* a ref-only projection never becomes citable evidence -- the model was told an
  id exists, not what it says;
* the two shapes of "70 KB" (many records vs one oversized record) are refused
  in different, honest ways.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from quintessence_pcs.runtime.agent_loop import (
    STOP_BUDGET,
    CallRecord,
    run_agent_loop,
)
from quintessence_pcs.runtime.agent_protocol import (
    AgentPort,
    StepOutcome,
    ToolCall,
)
from quintessence_pcs.runtime.manifest import ModelManifest
from quintessence_pcs.runtime.tool_budget import (
    AgentRequestPreview,
    REASON_REQUEST_BUDGET,
    REASON_TOO_LARGE,
    ToolBudgetPolicy,
    ToolResultBudgeter,
)
from quintessence_pcs.runtime.tool_catalog import ToolCatalog
from quintessence_pcs.runtime.tool_handlers import _remember_refs, _require_known_refs


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _manifest(**overrides) -> ModelManifest:
    base = dict(
        provider="deepseek",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
        max_calls=10,
        max_output_tokens_per_call=4096,
        max_wall_seconds=300,
        thinking="disabled",
        temperature=0,
        tool_mode="agent",
    )
    base.update(overrides)
    return ModelManifest(**base)


def _tool_schema(name: str = "cognition.belief.read") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "read",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


class _FakeCatalog:
    """Minimal catalog: definitions plus a fixed handler result."""

    def __init__(self, payload: dict, name: str = "cognition.belief.read"):
        self._payload = {"result": payload}
        self._name = name
        self.executed = 0

    def definitions(self):
        return (_tool_schema(self._name),)

    def execute(self, call, *, handlers):  # noqa: ARG002 - parity with real API
        self.executed += 1

        class _Result:
            ok = True
            code = "ok"
            payload = self._payload

            def content(self_inner) -> str:
                return json.dumps(
                    {"ok": True, "code": "ok", "result": self_inner.payload},
                    ensure_ascii=False,
                    sort_keys=True,
                )

        return _Result()


class _ScriptedPort(AgentPort):
    """Returns prepared outcomes and counts real sends."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0
        self.seen = []

    def step(self, *, messages, tools):
        self.calls += 1
        self.seen.append((tuple(messages), tuple(tools)))
        if not self._outcomes:
            raise AssertionError("port called more times than scripted")
        return self._outcomes.pop(0)


class _Namespaced:
    def __init__(self):
        self.known_refs: set[str] = set()


def _big_record(fill: int) -> dict:
    return {
        "record_id": "11111111-1111-1111-1111-111111111111",
        "record_version": 1,
        "claim_text": "x" * fill,
    }


# ---------------------------------------------------------------------------
# 1. same-source request preview
# ---------------------------------------------------------------------------


class RequestPreviewTests(unittest.TestCase):
    def test_a_small_request_fits(self):
        preview = AgentRequestPreview(
            manifest=_manifest(),
            max_tokens=512,
            output_reserve_units=1024,
            request_budget_units=65536,
        )
        decision = preview.check(
            [{"role": "user", "content": "hello"}], [_tool_schema()]
        )
        self.assertTrue(decision.fits)
        self.assertEqual(decision.budget_units, 65536)
        self.assertEqual(decision.admission_units, 65536 - 4096)

    def test_the_headroom_is_inside_the_budget(self):
        """A body that only fits by eating the continuation space is refused."""

        preview = AgentRequestPreview(
            manifest=_manifest(),
            max_tokens=512,
            output_reserve_units=0,
            request_budget_units=900,
        )
        # Tiny budget: even a short body plus the 4096 U headroom cannot fit.
        decision = preview.check([{"role": "user", "content": "hi"}], [_tool_schema()])
        self.assertFalse(decision.fits)
        self.assertEqual(decision.reason, REASON_REQUEST_BUDGET)
        self.assertEqual(decision.admission_units, 900 - 4096)

    def test_no_host_budget_is_not_guessed(self):
        preview = AgentRequestPreview(
            manifest=_manifest(),
            max_tokens=512,
            output_reserve_units=0,
            request_budget_units=None,
        )
        decision = preview.check([{"role": "user", "content": "hi"}], [_tool_schema()])
        self.assertTrue(decision.fits)
        self.assertIsNone(decision.budget_units)
        self.assertIsNone(decision.admission_units)


class LoopPreviewTests(unittest.TestCase):
    def test_a_request_that_cannot_fit_stops_before_the_second_send(self):
        catalog = _FakeCatalog({"items": [{"record_id": "r1", "claim_text": "a"}]})
        port = _ScriptedPort(
            [
                StepOutcome(
                    finish_reason="tool_calls",
                    content="",
                    reasoning_content="",
                    tool_calls=(ToolCall("c1", "cognition.belief.read", "{}"),),
                    usage={},
                )
            ]
        )

        class _RefuseAfterFirst:
            def __init__(self):
                self.calls = 0

            def check(self, messages, tools):
                self.calls += 1

                class _D:
                    fits = self.calls == 1
                    reason = None if self.calls == 1 else REASON_REQUEST_BUDGET
                    required_units = 70000
                    admission_units = 61440
                    budget_units = 65536

                return _D()

        preview = _RefuseAfterFirst()
        result = run_agent_loop(
            port,
            catalog,
            messages=({"role": "user", "content": "q"},),
            handlers={},
            request_preview=preview,
            tool_budget=ToolResultBudgeter(),
        )
        self.assertEqual(result.stopped_reason, STOP_BUDGET)
        self.assertEqual(port.calls, 1)  # the second send never happened
        self.assertIn("61440", result.content)
        # the un-sendable round was not committed
        self.assertEqual(len(result.messages), 1)

    def test_a_round_is_reshaped_to_refs_when_the_request_does_not_fit(self):
        catalog = _FakeCatalog(
            {
                "items": [
                    {
                        "record_id": "22222222-2222-2222-2222-222222222222",
                        "claim_text": "b",
                    }
                ]
            }
        )
        port = _ScriptedPort(
            [
                StepOutcome(
                    finish_reason="tool_calls",
                    content="",
                    reasoning_content="",
                    tool_calls=(ToolCall("c1", "cognition.belief.read", "{}"),),
                    usage={},
                ),
                StepOutcome(
                    finish_reason="stop",
                    content="done",
                    reasoning_content="",
                    tool_calls=(),
                    usage={},
                ),
            ]
        )

        class _FitOnlyAfterShrink:
            """Fits only once the round has been re-shaped to identity-only."""

            def __init__(self):
                self.calls = 0

            def check(self, messages, tools):  # noqa: ARG002
                self.calls += 1
                shrunk = False
                for message in messages:
                    if message.get("role") != "tool":
                        continue
                    try:
                        payload = json.loads(str(message.get("content") or ""))
                    except (TypeError, ValueError):
                        continue
                    result = payload.get("result") if isinstance(payload, dict) else None
                    if isinstance(result, dict) and result.get("body_included") is False:
                        shrunk = True
                fits = self.calls == 1 or shrunk

                class _D:
                    pass

                decision = _D()
                decision.fits = fits
                decision.reason = None if fits else REASON_REQUEST_BUDGET
                decision.required_units = 70000
                decision.admission_units = 61440
                decision.budget_units = 65536
                return decision

        result = run_agent_loop(
            port,
            catalog,
            messages=({"role": "user", "content": "q"},),
            handlers={},
            request_preview=_FitOnlyAfterShrink(),
            tool_budget=ToolResultBudgeter(),
        )
        self.assertEqual(result.stopped_reason, "final")
        tool_messages = [m for m in result.messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("body_included", tool_messages[0]["content"])
        self.assertIn("false", tool_messages[0]["content"])


# ---------------------------------------------------------------------------
# 2. identity is not body
# ---------------------------------------------------------------------------


class KnownRefsTests(unittest.TestCase):
    def test_a_ref_only_descriptor_does_not_register_the_id(self):
        toolkit = _Namespaced()
        _remember_refs(
            toolkit,
            {
                "ok": True,
                "result": {
                    "body_included": False,
                    "ref": {"record_id": "abc"},
                },
            },
        )
        self.assertEqual(toolkit.known_refs, set())

    def test_a_delivered_body_registers_the_id(self):
        toolkit = _Namespaced()
        _remember_refs(
            toolkit,
            {"ok": True, "result": {"items": [{"record_id": "abc", "claim_text": "c"}]}},
        )
        self.assertIn("abc", toolkit.known_refs)

    def test_citing_a_ref_only_id_is_refused(self):
        toolkit = _Namespaced()
        with self.assertRaises(Exception) as ctx:
            _require_known_refs(
                toolkit,
                {
                    "evidence_links": [
                        {"ref": {"record_id": "abc", "record_version": 1}, "relation": "supports"}
                    ]
                },
            )
        self.assertIn("has not returned", str(ctx.exception))

    def test_after_the_exact_read_the_same_id_is_allowed(self):
        toolkit = _Namespaced()
        # exact read delivered the body -> registered
        _remember_refs(
            toolkit,
            {"ok": True, "result": {"items": [{"record_id": "abc", "claim_text": "full"}]}},
        )
        _require_known_refs(
            toolkit,
            {
                "evidence_links": [
                    {"ref": {"record_id": "abc", "record_version": 1}, "relation": "supports"}
                ]
            },
        )

    def test_the_projection_payload_is_what_gets_walked(self):
        """End-to-end through the budgeter: a ref-only projection stays uncitable."""

        budgeter = ToolResultBudgeter(
            ToolBudgetPolicy(page_max_bytes=64, page_max_units=64)
        )
        # Two calls in the round: the page allowance applies, not the exact one.
        budgeter.begin_round(["cognition.belief.read", "cognition.belief.read"])
        inner = {"items": [{"record_id": "zzz", "claim_text": "y" * 400}]}
        projection = budgeter.project(
            tool="cognition.belief.read",
            payload={"result": inner},
            content=json.dumps({"ok": True, "code": "ok", "result": inner}),
        )
        self.assertFalse(projection.body_included)
        toolkit = _Namespaced()
        _remember_refs(toolkit, projection.payload)
        self.assertNotIn("zzz", toolkit.known_refs)


# ---------------------------------------------------------------------------
# 3. truncation metadata and the two 70 KB loads
# ---------------------------------------------------------------------------


class TruncationMetadataTests(unittest.TestCase):
    def test_a_large_page_keeps_whole_records_and_describes_the_rest(self):
        items = [
            {"record_id": f"00000000-0000-0000-0000-{i:012d}", "claim_text": "z" * 3000}
            for i in range(20)
        ]
        inner = {"items": items, "total": 20}
        budgeter = ToolResultBudgeter()  # 8 KiB page cap
        budgeter.begin_round(["cognition.belief.read"])
        projection = budgeter.project(
            tool="cognition.belief.read",
            payload={"result": inner},
            content=json.dumps({"ok": True, "code": "ok", "result": inner}),
        )
        result = projection.payload["result"]
        self.assertTrue(result["truncated"])
        self.assertIn("caps", result)
        self.assertIn("page_bytes", result["caps"])
        self.assertLessEqual(len(result["items"]), 20)
        # at least one record was delivered whole, the rest are identified
        self.assertGreaterEqual(len(result["items"]), 1)
        if len(result["items"]) < 20:
            self.assertIn("ref_only_items", result)
            self.assertEqual(len(result["items"]) + len(result["ref_only_items"]), 20)
            self.assertFalse(result["ref_only_items"][0]["body_included"])

    def test_one_oversized_exact_read_is_refused_not_fragmented(self):
        inner = {
            "items": [
                {"record_id": "33333333-3333-3333-3333-333333333333", "claim_text": "q" * 40000}
            ]
        }
        budgeter = ToolResultBudgeter()
        budgeter.begin_round(["cognition.belief.read"])
        projection = budgeter.project(
            tool="cognition.belief.read",
            payload={"result": inner},
            content=json.dumps({"ok": True, "code": "ok", "result": inner}),
        )
        self.assertFalse(projection.body_included)
        self.assertEqual(projection.reason, REASON_TOO_LARGE)
        self.assertEqual(projection.payload["code"], REASON_TOO_LARGE)
        self.assertFalse(projection.payload["result"]["executed"] is False)
        self.assertEqual(
            projection.payload["result"]["body_bytes"],
            len(json.dumps({"ok": True, "code": "ok", "result": inner})),
        )

    def test_a_round_of_many_reads_degrades_the_later_ones(self):
        items = [{"record_id": f"r{i}", "claim_text": "w" * 2000} for i in range(8)]
        inner = {"items": [items[0]]}
        budgeter = ToolResultBudgeter()
        budgeter.begin_round(["cognition.belief.read"] * 8)
        first = budgeter.project(
            tool="cognition.belief.read",
            payload={"result": inner},
            content=json.dumps({"ok": True, "code": "ok", "result": inner}),
        )
        self.assertTrue(first.body_included)
        # Exhaust the round deliberately, then a further call must be refused
        # rather than executed.
        while budgeter.can_execute():
            budgeter.project(
                tool="cognition.belief.read",
                payload={"result": inner},
                content=json.dumps({"ok": True, "code": "ok", "result": inner}),
            )
        self.assertFalse(budgeter.can_execute())
        refusal = budgeter.refusal(tool="cognition.belief.read")
        self.assertFalse(refusal.payload["result"]["executed"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
