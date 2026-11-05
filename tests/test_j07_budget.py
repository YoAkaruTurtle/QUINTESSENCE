"""Exercise the actual step gate and the user-facing loop, offline."""
import json
import os
import types
import unittest
from unittest import mock

from tests.test_request_budget import MANIFEST, MODEL_KEY_ENV, _Store, _Context, _SettleStore
from quintessence_pcs.runtime.agent_loop import run_agent_loop, STOP_BUDGET
from quintessence_pcs.runtime.agent_protocol import build_agent_request_body, StepOutcome, ToolCall
from quintessence_pcs.runtime.http_model import DirectHttpModel
from quintessence_pcs.runtime.manifest import ModelManifest
from quintessence_pcs.runtime.model_port import ModelBudgetExceededError
from quintessence_pcs.runtime.tool_catalog import ToolCatalog


class AgentBudgetTests(unittest.TestCase):
    def model(self, budget=200000):
        store = _Store()
        manifest = ModelManifest.from_mapping({**MANIFEST, "tool_mode": "agent"})
        return DirectHttpModel(store, manifest, request_budget_units=budget), store, manifest

    def test_actual_step_refuses_before_http_and_reservation(self):
        model, store, _ = self.model(300)
        with mock.patch.dict(os.environ, {MODEL_KEY_ENV: "test-key"}), \
             mock.patch.object(model, "_send") as send, \
             model.bind_run(namespace=_Context.namespace, context=_Context()):
            with self.assertRaises(ModelBudgetExceededError):
                model.step(messages=[{"role": "user", "content": "大" * 500}],
                           tools=ToolCatalog.cognition_and_web().definitions())
        send.assert_not_called()
        self.assertEqual(0, store.reserved)

    def test_three_tool_roundtrips_increase_whole_request_estimate(self):
        model, _, manifest = self.model()
        messages = [{"role": "user", "content": "query"}]
        values = []
        for index in range(4):
            text = build_agent_request_body(manifest, messages=messages,
                tools=ToolCatalog.cognition_and_web().definitions(), max_tokens=512)
            model._require_request_budget("query", text)
            values.append(model._pcs_estimate["request_units"])
            if index < 3:
                messages.extend([
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": f"call-{index}", "type": "function", "function": {
                            "name": "cognition.belief.read", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": f"call-{index}", "content": "证据" * 100},
                ])
        self.assertEqual(sorted(values), values)
        self.assertGreater(values[-1], values[0])

    def test_usage_is_not_overwritten_by_estimate(self):
        store = _SettleStore()
        model = DirectHttpModel(store, ModelManifest.from_mapping(MANIFEST), request_budget_units=200000)
        model._require_request_budget("query", "abc")
        estimate = dict(model._pcs_estimate)
        usage = {"prompt_tokens": 71, "completion_tokens": 9}
        model._finish(types.SimpleNamespace(attempt_id="offline-attempt"), outcome="ok", usage=usage, usage_known=True)
        self.assertEqual(usage, store.settled["usage"])
        self.assertEqual(estimate, store.settled["pcs_estimated"])
        self.assertNotEqual(usage["prompt_tokens"], estimate["body_units"])

    def test_budget_failure_after_tool_result_preserves_history_and_is_visible(self):
        class Port:
            count = 0
            def step(self, **kwargs):
                self.count += 1
                if self.count == 1:
                    return StepOutcome("tool_calls", "", "", (ToolCall("c1", "cognition.belief.read", "{}"),))
                raise ModelBudgetExceededError("request body needs 1234 U; budget is 1000 U")
        port = Port()
        result = run_agent_loop(port, ToolCatalog.cognition_and_web(),
            messages=[{"role": "user", "content": "original history"}],
            handlers={"cognition.belief.read": lambda args: {"result": "actual tool result"}})
        self.assertEqual(STOP_BUDGET, result.stopped_reason)
        self.assertIn("1234 U", result.content)
        self.assertIn("缩小工作集", result.content)
        self.assertEqual(2, port.count)
        self.assertEqual("original history", result.messages[0]["content"])
        self.assertEqual("tool", result.messages[-1]["role"])


if __name__ == "__main__":
    unittest.main()
