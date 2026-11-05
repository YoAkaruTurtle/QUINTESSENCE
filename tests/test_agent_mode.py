"""J07 M1-M3: agent mode protocol, catalog and bounded tool loop.

These are L1 (unit) tests.  The L2 integration case at the end uses an
explicitly labelled deterministic stub -- never a real provider, and never a
claim that a real model behaved this way.  The L3 case is registered and
marked NOT_RUN on purpose.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from quintessence_pcs.runtime.agent_loop import (
    MAX_REPEATED_CALLS,
    MAX_TOOL_STEPS,
    STOP_FINAL,
    STOP_MALFORMED,
    STOP_REPEATED,
    STOP_STEPS,
    WRAP_UP_MESSAGE,
    run_agent_loop,
)
from quintessence_pcs.runtime.agent_protocol import (
    from_wire_name,
    to_wire_name,
    StepOutcome,
    ToolCall,
    build_agent_request_body,
    tool_call_message,
    tool_result_message,
)
from quintessence_pcs.runtime.manifest import (
    TOOL_MODE_AGENT,
    TOOL_MODE_FREE,
    ModelManifest,
)
from quintessence_pcs.runtime.model_port import ModelConfigurationError
from quintessence_pcs.runtime.tool_catalog import (
    COGNITION_TOOLS,
    WEB_TOOLS,
    TOOL_ARGUMENTS_INVALID,
    TOOL_UNKNOWN,
    ToolCatalog,
    ToolSpec,
)

BASE_MANIFEST = {
    "provider": "synthetic",
    "base_url": "http://127.0.0.1:9/v1",
    "model": "agent-test-model",
    "api_key_env": "QUINTESSENCE_J07_TEST_KEY",
    "max_calls": 8,
    "max_output_tokens_per_call": 1024,
    "max_wall_seconds": 60,
    "thinking": "disabled",
    "temperature": 0,
}


def manifest(**overrides):
    payload = dict(BASE_MANIFEST)
    payload.update(overrides)
    return ModelManifest.from_mapping(payload)


class _StubPort:
    """A deterministic stand-in.  It is a stub, and every test says so."""

    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.seen: list[list[dict]] = []
        self.tool_counts: list[int] = []

    def step(self, *, messages, tools):
        self.seen.append([dict(item) for item in messages])
        self.tool_counts.append(len(tools))
        if not self._rounds:
            return StepOutcome("stop", "no more rounds", "")
        item = self._rounds.pop(0)
        if callable(item):
            return item(messages)
        return item


def _read_tool():
    return {"type": "function", "function": {"name": "cognition.belief.read", "description": "r", "parameters": {"type": "object", "properties": {}}}}


class ManifestModeTests(unittest.TestCase):
    def test_default_mode_is_tool_free(self) -> None:
        self.assertEqual(manifest().tool_mode, TOOL_MODE_FREE)

    def test_agent_mode_is_opt_in_and_hashes_differently(self) -> None:
        free = manifest()
        agent = manifest(tool_mode=TOOL_MODE_AGENT)
        self.assertNotEqual(free.manifest_hash, agent.manifest_hash)
        self.assertNotIn("tool_mode", free.to_dict())
        self.assertEqual(agent.to_dict()["tool_mode"], TOOL_MODE_AGENT)

    def test_unknown_mode_is_refused(self) -> None:
        with self.assertRaises(Exception):
            manifest(tool_mode="anything")


class WireNameTests(unittest.TestCase):
    """The catalog's dotted names must survive the provider's charset."""

    def test_every_catalog_tool_maps_to_a_legal_wire_name_and_back(self) -> None:
        catalog = ToolCatalog.cognition_and_web()
        definitions = catalog.definitions()
        self.assertEqual(13, len(definitions))
        seen: set[str] = set()
        for definition in definitions:
            name = definition["function"]["name"]
            wire = to_wire_name(name)
            self.assertRegex(wire, r"^[a-zA-Z0-9_-]+$")
            self.assertNotIn("__", name)          # the mapping must be reversible
            self.assertNotIn(wire, seen)          # and collision-free
            seen.add(wire)
            self.assertEqual(name, from_wire_name(wire))

    def test_a_name_that_cannot_round_trip_is_refused_locally(self) -> None:
        with self.assertRaises(ModelConfigurationError):
            to_wire_name("cognition__already_has_separator")


class BuilderTests(unittest.TestCase):
    def test_tool_free_manifest_cannot_build_an_agent_body(self) -> None:
        with self.assertRaises(ModelConfigurationError):
            build_agent_request_body(
                manifest(),
                messages=[{"role": "user", "content": "hi"}],
                tools=[_read_tool()],
                max_tokens=16,
            )

    def test_agent_body_carries_tools(self) -> None:
        body = json.loads(
            build_agent_request_body(
                manifest(tool_mode=TOOL_MODE_AGENT),
                messages=[{"role": "user", "content": "hi"}],
                tools=[_read_tool()],
                max_tokens=64,
            )
        )
        # The provider accepts only ``^[a-zA-Z0-9_-]+$``; a dotted catalog name
        # is mapped onto the wire form and back on the response side.
        self.assertEqual(
            body["tools"][0]["function"]["name"], "cognition__belief__read"
        )
        self.assertFalse(body["stream"])

    def test_proposal_builder_still_emits_no_tools(self) -> None:
        from quintessence_pcs.runtime.manifest import build_request_body

        body = json.loads(
            build_request_body(
                manifest(tool_mode=TOOL_MODE_AGENT),
                rendered_input="x",
                max_tokens=16,
            )
        )
        self.assertNotIn("tools", body)
        self.assertEqual(body["messages"], [{"role": "user", "content": "x"}])

    def test_tool_schema_must_not_ask_for_authority(self) -> None:
        with self.assertRaises(ModelConfigurationError):
            build_agent_request_body(
                manifest(tool_mode=TOOL_MODE_AGENT),
                messages=[{"role": "user", "content": "hi"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "x.y",
                            "parameters": {
                                "type": "object",
                                "properties": {"scope": {"type": "string"}},
                            },
                        },
                    }
                ],
                max_tokens=16,
            )

    def test_unknown_message_role_is_refused(self) -> None:
        with self.assertRaises(ModelConfigurationError):
            build_agent_request_body(
                manifest(tool_mode=TOOL_MODE_AGENT),
                messages=[{"role": "system", "content": "you are"}],
                tools=[_read_tool()],
                max_tokens=16,
            )


class CotEchoTests(unittest.TestCase):
    def test_assistant_turn_keeps_reasoning_content(self) -> None:
        outcome = StepOutcome(
            finish_reason="tool_calls",
            content="",
            reasoning_content="because",
            tool_calls=(ToolCall("call-1", "cognition.belief.read", "{}"),),
        )
        message = tool_call_message(outcome)
        self.assertEqual(message["reasoning_content"], "because")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "cognition.belief.read")

    def test_tool_result_needs_the_call_id(self) -> None:
        with self.assertRaises(ModelConfigurationError):
            tool_result_message(call_id="", content="x")


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = ToolCatalog.cognition_and_web()

    def test_catalog_shape(self) -> None:
        self.assertEqual(len(self.catalog.names()), 13)
        self.assertEqual(len(COGNITION_TOOLS), 11)
        self.assertEqual(len(WEB_TOOLS), 2)

    def test_dangerous_tools_are_absent(self) -> None:
        names = set(self.catalog.names())
        for forbidden in (
            "commit",
            "cognition.commit",
            "review.submit",
            "interact",
            "evaluate",
            "consume_trigger",
            "submit_proposal",
        ):
            self.assertNotIn(forbidden, names)

    def test_unknown_tool_is_refused_not_raised(self) -> None:
        result = self.catalog.execute(
            ToolCall("c1", "nope", "{}"), handlers={}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, TOOL_UNKNOWN)

    def test_bad_arguments_return_a_structured_error(self) -> None:
        result = self.catalog.execute(
            ToolCall("c1", "cognition.belief.read", '{"limit": 999}'), handlers={}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, TOOL_ARGUMENTS_INVALID)
        self.assertIn("limit", result.content())

    def test_authority_fields_are_stripped_before_the_handler(self) -> None:
        seen = {}

        def handler(args):
            seen.update(args)
            return {"ok": True}

        result = self.catalog.execute(
            ToolCall(
                "c1",
                "cognition.belief.read",
                json.dumps({"limit": 5, "scope": {"project_id": "forged"}, "authority_kind": "x"}),
            ),
            handlers={"cognition.belief.read": handler},
        )
        self.assertTrue(result.ok)
        self.assertNotIn("scope", seen)
        self.assertNotIn("authority_kind", seen)
        self.assertEqual(seen["limit"], 5)

    def test_a_schema_asking_for_authority_is_refused_at_construction(self) -> None:
        bad = ToolSpec(
            "bad.tool",
            "d",
            {"type": "object", "properties": {"approval_id": {"type": "string"}}},
            "bad.tool",
        )
        with self.assertRaises(ModelConfigurationError):
            ToolCatalog([bad])


class LoopTests(unittest.TestCase):
    def _catalog(self) -> ToolCatalog:
        return ToolCatalog.cognition_and_web()

    def test_final_round_is_driven_by_finish_reason(self) -> None:
        port = _StubPort([StepOutcome("stop", "done", "")])
        result = run_agent_loop(
            port,
            self._catalog(),
            messages=[{"role": "user", "content": "q"}],
            handlers={},
        )
        self.assertEqual(result.stopped_reason, STOP_FINAL)
        self.assertEqual(result.content, "done")
        self.assertEqual(result.steps, 1)

    def test_empty_content_with_tool_calls_does_not_end_the_loop(self) -> None:
        calls = (ToolCall("c1", "cognition.belief.read", "{}"),)
        port = _StubPort(
            [
                StepOutcome("tool_calls", "", "thinking", calls),
                StepOutcome("stop", "answer", ""),
            ]
        )
        result = run_agent_loop(
            port,
            self._catalog(),
            messages=[{"role": "user", "content": "q"}],
            handlers={"cognition.belief.read": lambda args: {"beliefs": []}},
        )
        self.assertEqual(result.stopped_reason, STOP_FINAL)
        self.assertEqual(result.steps, 2)
        self.assertEqual(len(result.calls), 1)
        self.assertTrue(result.calls[0].ok)

    def test_tool_failure_flows_back_to_the_model(self) -> None:
        calls = (ToolCall("c1", "cognition.belief.read", '{"limit": 0}'),)
        port = _StubPort(
            [
                StepOutcome("tool_calls", "", "", calls),
                StepOutcome("stop", "I could not read", ""),
            ]
        )
        result = run_agent_loop(
            port,
            self._catalog(),
            messages=[{"role": "user", "content": "q"}],
            handlers={},
        )
        self.assertEqual(result.calls[0].code, TOOL_ARGUMENTS_INVALID)
        second_round = port.seen[1]
        tool_turns = [item for item in second_round if item.get("role") == "tool"]
        self.assertEqual(len(tool_turns), 1)
        self.assertIn("tool_arguments_invalid", tool_turns[0]["content"])

    def test_steps_are_bounded_and_wrap_up_has_no_tools(self) -> None:
        # Distinct arguments each round, so the repetition guard is not what
        # stops the loop -- the step budget must be.
        rounds = [
            StepOutcome(
                "tool_calls",
                "",
                "",
                (ToolCall(f"c{index}", "cognition.belief.read", json.dumps({"limit": index + 1})),),
            )
            for index in range(MAX_TOOL_STEPS)
        ]
        rounds.append(StepOutcome("stop", "what I know", ""))
        port = _StubPort(rounds)
        result = run_agent_loop(
            port,
            self._catalog(),
            messages=[{"role": "user", "content": "q"}],
            handlers={"cognition.belief.read": lambda args: {"ok": True}},
        )
        self.assertEqual(result.stopped_reason, STOP_STEPS)
        self.assertEqual(result.steps, MAX_TOOL_STEPS)
        self.assertEqual(port.tool_counts[-1], 0)
        self.assertEqual(result.content, "what I know")
        self.assertEqual(port.seen[-1][-1]["content"], WRAP_UP_MESSAGE)

    def test_repeated_identical_call_stops_the_loop(self) -> None:
        same = (ToolCall("c1", "cognition.belief.read", "{}"),)
        rounds = [StepOutcome("tool_calls", "", "", same) for _ in range(MAX_REPEATED_CALLS + 1)]
        port = _StubPort(rounds)
        result = run_agent_loop(
            port,
            self._catalog(),
            messages=[{"role": "user", "content": "q"}],
            handlers={"cognition.belief.read": lambda args: {"ok": True}},
            max_steps=10,
        )
        self.assertEqual(result.stopped_reason, STOP_REPEATED)

    def test_tool_calls_without_a_finish_reason_is_malformed(self) -> None:
        port = _StubPort([StepOutcome("tool_calls", "", "")])
        result = run_agent_loop(
            port,
            self._catalog(),
            messages=[{"role": "user", "content": "q"}],
            handlers={},
        )
        self.assertEqual(result.stopped_reason, STOP_MALFORMED)


class AgentRoundTripStubTests(unittest.TestCase):
    """L2 (integration, deterministic stub -- not a real model)."""

    def test_stub_tool_roundtrip_carries_cot_back(self) -> None:
        calls = (ToolCall("c1", "web.search", '{"query": "x"}'),)
        port = _StubPort(
            [
                StepOutcome("tool_calls", "", "need current data", calls),
                StepOutcome("stop", "answer", ""),
            ]
        )
        result = run_agent_loop(
            port,
            ToolCatalog.cognition_and_web(),
            messages=[{"role": "user", "content": "search please"}],
            handlers={"web.search": lambda args: {"results": []}},
        )
        self.assertEqual(result.stopped_reason, STOP_FINAL)
        second = port.seen[1]
        assistant = [item for item in second if item.get("role") == "assistant"][0]
        self.assertEqual(assistant["reasoning_content"], "need current data")
        self.assertEqual(assistant["tool_calls"][0]["id"], "c1")


@unittest.skip("L3: requires a real DeepSeek call; collected by M8, NOT_RUN here")
class RealModelAgentTests(unittest.TestCase):
    def test_model_chooses_a_tool_and_answers(self) -> None:
        pass

class WrapUpTests(unittest.TestCase):
    """D3: the wrap-up turn must produce an answer, never an empty string."""

    def _loop(self, port, *, max_steps: int = 2):
        catalog = ToolCatalog.cognition_and_web()
        handlers = {name: (lambda _args: {"result": {}})
                    for name in ("cognition.partition", "cognition.belief.read")}
        return run_agent_loop(
            port, catalog,
            messages=[{"role": "user", "content": "查一下再回答"}],
            handlers=handlers, max_steps=max_steps,
        )

    def test_d3_empty_wrap_up_becomes_a_deterministic_answer(self) -> None:
        class SilentPort:
            def __init__(self):
                self.turns = 0

            def step(self, *, messages, tools):
                self.turns += 1
                if tools:
                    return StepOutcome(
                        finish_reason="tool_calls", content="",
                        reasoning_content="", tool_calls=(
                            ToolCall(call_id=f"c{self.turns}", name="cognition.partition",
                                     arguments="{}"),
                        ), usage={},
                    )
                return StepOutcome(
                    finish_reason="stop", content="", reasoning_content="",
                    tool_calls=(), usage={},
                )

        result = self._loop(SilentPort())
        self.assertEqual(STOP_STEPS, result.stopped_reason)
        self.assertTrue(result.content.strip())
        self.assertIn("工具调用预算已用尽", result.content)

    def test_d3_budget_is_eight_rounds(self) -> None:
        self.assertEqual(8, MAX_TOOL_STEPS)

