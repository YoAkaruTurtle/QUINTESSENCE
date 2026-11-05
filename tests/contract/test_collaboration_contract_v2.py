"""RG03: the collab.v2 model-output contract (incremental to frozen v1).

Layer S only: the dependency-free ``ContractChecker`` validates instances
against ``contracts/collaboration/v2``.  v1 files must stay byte-identical; the
v2 directory must be self-contained (its ``_common.json`` is a frozen copy).
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.contract.contract_checker import ContractChecker

REPO_ROOT = Path(__file__).resolve().parents[2]
V1_DIR = REPO_ROOT / "contracts" / "collaboration" / "v1"
V2_DIR = REPO_ROOT / "contracts" / "collaboration" / "v2"

ACTION = "final"


def sample(**overrides):
    document = {"action": "final", "reply": "合成回答。"}
    document.update(overrides)
    return document


class CollabV2ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.checker = ContractChecker(V2_DIR)
        cls.v1_checker = ContractChecker(V1_DIR)

    def test_directory_is_self_contained_and_v1_is_frozen(self) -> None:
        names = {path.name for path in V2_DIR.glob("*.json")}
        self.assertEqual({"_common.json", "model_output.json"}, names)
        self.assertTrue((V2_DIR / "README.md").is_file())
        self.assertEqual(
            (V1_DIR / "_common.json").read_bytes(),
            (V2_DIR / "_common.json").read_bytes(),
        )
        v1_output = json.loads((V1_DIR / "model_output.json").read_text("utf-8"))
        self.assertEqual(
            ["none", "query", "propose", "wait"],
            v1_output["properties"]["action"]["enum"],
        )
        v2_output = json.loads((V2_DIR / "model_output.json").read_text("utf-8"))
        self.assertEqual(
            ["final", "call_tool", "wait"],
            v2_output["properties"]["action"]["enum"],
        )
        self.assertIn(
            "turn_id", v2_output["description"] + json.dumps(v2_output)
        )

    def test_valid_final_call_tool_and_wait(self) -> None:
        self.assertTrue(self.checker.is_valid("model_output.json", sample()))
        call = {
            "action": "call_tool",
            "commentary": "我翻一下之前的问题。",
            "tool_call": {
                "name": "cognition.belief.read",
                "arguments": {"limit": 3},
            },
        }
        self.assertTrue(self.checker.is_valid("model_output.json", call))
        wait = {"action": "wait", "wait_question": "你想先聊哪件事？"}
        self.assertTrue(self.checker.is_valid("model_output.json", wait))
        bubbles = {"action": "final", "messages": ["第一段。", "第二段。"]}
        self.assertTrue(self.checker.is_valid("model_output.json", bubbles))
        self.assertTrue(
            self.checker.is_valid(
                "model_output.json",
                {"action": "final", "reply": "单条旧回复。"},
            )
        )

    def test_invalid_shapes_are_refused(self) -> None:
        cases = [
            {"action": "none", "reply": "v1 only"},
            {"action": "propose", "reply": "x"},
            {},
            {"action": "final", "reply": "x", "extra": 1},
            {"action": "final", "reply": "x", "context_id": "x"},
            {"action": "call_tool", "tool_call": {"name": "x"}},
            {
                "action": "call_tool",
                "tool_call": {"name": "X.Y", "arguments": {}},
            },
            {
                "action": "call_tool",
                "tool_call": {"name": "x.y", "arguments": []},
            },
            {"action": "final", "reply": ""},
            {"action": "wait", "wait_question": ""},
            {"action": "final", "messages": []},
            {"action": "final", "messages": ["a"] * 5},
            {"action": "final", "messages": ["a" * 4097]},
            {"action": "final", "messages": [""]},
            {"action": "final", "messages": "single"},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(self.checker.is_valid("model_output.json", case))

    def test_v1_schema_still_rejects_v2_actions(self) -> None:
        self.assertFalse(
            self.v1_checker.is_valid(
                "model_output.json", {"action": "final", "reply": "x"}
            )
        )
        self.assertTrue(
            self.v1_checker.is_valid(
                "model_output.json", {"action": "none", "reply": "x"}
            )
        )


if __name__ == "__main__":
    unittest.main()
