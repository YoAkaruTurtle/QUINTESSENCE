"""C09: the frozen collab.v1 ``model_output`` schema.

The schema is the authority for the wire shape.  Every invalid sample declares
the exact ``(path, keyword)`` pair it expects, so a right verdict from the
wrong constraint is a failure.  The semantics that JSON Schema cannot express
(consult cannot propose, a reference must match a Context-supplied reference)
are covered by ``tests/domain/test_collab_protocol.py``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from contract_checker import ContractChecker, strict_loads_file  # noqa: E402

REPO_ROOT = HERE.parents[1]
CONTRACT_DIR = REPO_ROOT / "contracts" / "collaboration" / "v1"
SCHEMA = "model_output.json"
NAMESPACE = "example.private"


def collab_input_ref(index: int = 0) -> dict[str, Any]:
    return {
        "ref_type": "collab_input",
        "namespace": NAMESPACE,
        "input_id": f"00000000-0000-4000-8000-{index:012x}",
        "content_version": 1,
    }


class ModelOutputContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checker = ContractChecker(CONTRACT_DIR)

    def failures(self, instance: Any) -> set[tuple[str, str]]:
        return {
            (failure.path, failure.keyword)
            for failure in self.checker.validate(SCHEMA, instance)
        }

    # ---------------------------------------------------------------- shape

    def test_valid_output_shapes_are_accepted(self) -> None:
        cases = [
            {"action": "none", "reply": "synthetic consult reply"},
            {"action": "none", "brief_reason": "synthetic brief reason"},
            {
                "action": "none",
                "reply": "synthetic reply",
                "brief_reason": "synthetic reason",
            },
            {"action": "wait"},
            {"action": "query", "brief_reason": "needs a deterministic lookup"},
            {"action": "propose", "reply": "synthetic proposal shape"},
            {"action": "propose", "references": [collab_input_ref()]},
            {"action": "wait", "references": []},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(set(), self.failures(case))

    def test_action_must_be_present_and_known(self) -> None:
        self.assertIn(("", "required"), self.failures({}))
        self.assertIn(("", "required"), self.failures({"reply": "no action"}))
        self.assertEqual(
            {("/action", "enum")},
            self.failures({"action": "execute"}),
        )
        self.assertEqual(
            {("/action", "type")},
            self.failures({"action": None}),
        )

    def test_hidden_authoritative_fields_are_rejected(self) -> None:
        for field, value in (
            ("request_id", "00000000-0000-4000-8000-000000000000"),
            ("read_set", []),
            ("state_revision", 7),
            ("authorization", "auth:model"),
            ("context_id", "00000000-0000-4000-8000-000000000000"),
        ):
            with self.subTest(field=field):
                instance = {"action": "none", "reply": "synthetic reply"}
                instance[field] = value
                self.assertIn(
                    (f"/{field}", "additionalProperties"),
                    self.failures(instance),
                )

    # ---------------------------------------------------------------- bounds

    def test_text_bounds_are_exact(self) -> None:
        self.assertIn(
            ("/reply", "minLength"),
            self.failures({"action": "none", "reply": ""}),
        )
        self.assertIn(
            ("/reply", "maxLength"),
            self.failures({"action": "none", "reply": "x" * 16385}),
        )
        self.assertEqual(
            {("/reply", "type")},
            self.failures({"action": "none", "reply": 5}),
        )
        self.assertIn(
            ("/brief_reason", "minLength"),
            self.failures({"action": "wait", "brief_reason": ""}),
        )
        self.assertEqual(
            {("/brief_reason", "maxLength")},
            self.failures({"action": "wait", "brief_reason": "x" * 2049}),
        )

    # ------------------------------------------------------------ references

    def test_references_must_be_unique_typed_and_bounded(self) -> None:
        duplicate = [collab_input_ref(0), collab_input_ref(0)]
        self.assertIn(
            ("/references/1", "uniqueItems"),
            self.failures({"action": "query", "references": duplicate}),
        )

        forged = collab_input_ref(1)
        forged["input_id"] = "not-a-uuid"
        self.assertEqual(
            {("/references/0", "oneOf")},
            self.failures({"action": "query", "references": [forged]}),
        )

        extra = collab_input_ref(2)
        extra["forged"] = True
        self.assertEqual(
            {("/references/0", "oneOf")},
            self.failures({"action": "query", "references": [extra]}),
        )

        too_many = [collab_input_ref(index) for index in range(33)]
        self.assertEqual(
            {("/references", "maxItems")},
            self.failures({"action": "query", "references": too_many}),
        )

        # The schema file itself keeps its identity and closed field set.
        schema = strict_loads_file(CONTRACT_DIR / SCHEMA)
        self.assertEqual(SCHEMA, schema["$id"])
        self.assertIs(False, schema["additionalProperties"])
        self.assertEqual({"action"}, set(schema["required"]))
        self.assertEqual(
            {"reply", "action", "references", "brief_reason"},
            set(schema["properties"]),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
