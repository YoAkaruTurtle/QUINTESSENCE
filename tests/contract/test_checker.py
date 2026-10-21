"""Checker self-tests: Python traps, fail-closed behaviour, keyword semantics.

These tests do not depend on the fixtures in ``vectors/``; they pin the checker
itself so a future "harmless" simplification cannot silently weaken the gate.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from contract_checker import (  # noqa: E402
    ContractChecker,
    InvalidJSONError,
    RefResolutionError,
    SchemaLintError,
    assert_json_value,
    lint_schema,
    strict_loads,
)

SCHEMA_DIR = HERE.parents[1] / "contracts" / "v2"


class StrictJSONTests(unittest.TestCase):
    def test_accepts_plain_json(self) -> None:
        value = strict_loads('{"a":[1,true,null,"中文"]}')
        self.assertEqual(value, {"a": [1, True, None, "中文"]})

    def test_rejects_nan_literal(self) -> None:
        with self.assertRaises(InvalidJSONError):
            strict_loads('{"x": NaN}', "nan_case")

    def test_rejects_infinity_literal(self) -> None:
        with self.assertRaises(InvalidJSONError):
            strict_loads('{"x": Infinity}', "inf_case")

    def test_rejects_negative_infinity_literal(self) -> None:
        with self.assertRaises(InvalidJSONError):
            strict_loads('{"x": -Infinity}', "ninf_case")

    def test_rejects_overflow_that_parses_to_inf(self) -> None:
        import json

        self.assertEqual(json.loads("1e999"), float("inf"))
        # json.loads silently yields inf for overflow; the strict loader must
        # reject it both bare and inside a document.
        with self.assertRaises(InvalidJSONError):
            strict_loads("1e999", "overflow_bare")
        with self.assertRaises(InvalidJSONError):
            strict_loads('{"x": 1e999}', "overflow_case")

    def test_rejects_non_json_python_values(self) -> None:
        with self.assertRaises(InvalidJSONError):
            assert_json_value({"x": {1, 2}}, "set_case")
        with self.assertRaises(InvalidJSONError):
            assert_json_value({1: "x"}, "int_key_case")

    def test_rejects_duplicate_object_keys(self) -> None:
        with self.assertRaises(InvalidJSONError):
            strict_loads('{"a": 1, "a": 2}', "dup_case")
        with self.assertRaises(InvalidJSONError):
            strict_loads('{"outer": {"b": 1, "b": 2}}', "nested_dup_case")

    def test_rejects_malformed_json(self) -> None:
        with self.assertRaises(InvalidJSONError):
            strict_loads('{"x": }')


class CheckerSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checker = ContractChecker(SCHEMA_DIR)

    def assertInvalid(
        self, schema: object, instance: object, keyword: str | None = None
    ) -> None:
        failures = self.checker.validate_inline(schema, instance)
        self.assertTrue(failures, "expected validation to fail")
        if keyword is not None:
            self.assertIn(keyword, {failure.keyword for failure in failures})

    def assertValid(self, schema: object, instance: object) -> None:
        failures = self.checker.validate_inline(schema, instance)
        self.assertEqual([], failures)

    def test_boolean_is_not_integer(self) -> None:
        self.assertInvalid({"type": "integer"}, True, "type")
        self.assertInvalid({"type": "integer"}, False, "type")
        self.assertValid({"type": "boolean"}, True)

    def test_float_is_not_integer(self) -> None:
        self.assertInvalid({"type": "integer"}, 1.0, "type")
        self.assertValid({"type": "number"}, 1.0)

    def test_null_versus_missing(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["x"],
            "properties": {"x": {"type": ["string", "null"]}},
        }
        self.assertValid(schema, {"x": None})
        self.assertInvalid(schema, {}, "required")

    def test_enum_is_type_aware(self) -> None:
        schema = {"enum": [1]}
        self.assertValid(schema, 1)
        self.assertValid(schema, 1.0)
        self.assertInvalid(schema, True, "enum")

    def test_const_is_type_aware(self) -> None:
        schema = {"const": 1}
        self.assertValid(schema, 1.0)
        self.assertInvalid(schema, True, "const")

    def test_unique_items_is_type_aware(self) -> None:
        schema = {"type": "array", "uniqueItems": True}
        self.assertValid(schema, [1, True])
        self.assertInvalid(schema, [1, 1.0], "uniqueItems")
        self.assertValid(schema, [{"a": 1}, {"a": 2}])
        self.assertInvalid(schema, [{"a": 1}, {"a": 1.0}], "uniqueItems")
        self.assertInvalid(schema, [{"a": 1, "b": 2}, {"b": 2, "a": 1}], "uniqueItems")

    def test_oneof_requires_exactly_one(self) -> None:
        exclusive = {
            "oneOf": [{"type": "string"}, {"type": "integer"}]
        }
        self.assertValid(exclusive, "x")
        self.assertValid(exclusive, 1)
        self.assertInvalid(exclusive, True, "oneOf")
        overlapping = {
            "oneOf": [{"type": "integer"}, {"type": "number"}]
        }
        self.assertInvalid(overlapping, 1, "oneOf")

    def test_additional_properties_schema(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": {"type": "string"},
        }
        self.assertValid(schema, {"a": "b"})
        self.assertInvalid(schema, {"a": 1}, "type")

    def test_additional_properties_false(self) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "additionalProperties": False,
        }
        self.assertValid(schema, {"a": "b"})
        self.assertInvalid(schema, {"a": "b", "c": "d"}, "additionalProperties")

    def test_pattern_is_fullmatch(self) -> None:
        schema = {"type": "string", "pattern": "^a$"}
        self.assertValid(schema, "a")
        self.assertInvalid(schema, "a\n", "pattern")
        self.assertInvalid(schema, "ba", "pattern")

    def test_minimum_maximum(self) -> None:
        schema = {"type": "integer", "minimum": 1, "maximum": 3}
        self.assertValid(schema, 1)
        self.assertValid(schema, 3)
        self.assertInvalid(schema, 0, "minimum")
        self.assertInvalid(schema, 4, "maximum")

    def test_min_max_length_counts_characters(self) -> None:
        schema = {"type": "string", "minLength": 2, "maxLength": 3}
        self.assertValid(schema, "中文")
        self.assertInvalid(schema, "中", "minLength")
        self.assertInvalid(schema, "中文字符", "maxLength")

    def test_items_are_validated(self) -> None:
        schema = {"type": "array", "items": {"type": "integer"}}
        self.assertValid(schema, [1, 2])
        self.assertInvalid(schema, [1, "2"], "type")

    def test_local_refs_work_in_inline_schemas(self) -> None:
        schema = {
            "$defs": {"name": {"type": "string", "minLength": 2}},
            "$ref": "#/$defs/name",
        }
        self.assertValid(schema, "ab")
        self.assertInvalid(schema, "a", "minLength")

    def test_unresolvable_refs_fail_closed(self) -> None:
        with self.assertRaises(RefResolutionError):
            self.checker.resolve("record_ref.json", "_common.json#/$defs/nope")
        with self.assertRaises(RefResolutionError):
            self.checker.resolve("record_ref.json", "missing.json#/$defs/x")
        with self.assertRaises(RefResolutionError):
            self.checker.resolve("record_ref.json", "../v2/event.json")
        with self.assertRaises(RefResolutionError):
            self.checker.resolve("record_ref.json", "/etc/passwd")

    def test_unknown_keyword_fails_closed(self) -> None:
        with self.assertRaises(SchemaLintError):
            lint_schema({"type": "object", "if": {"type": "string"}})
        with self.assertRaises(SchemaLintError):
            self.checker.validate_inline({"type": "object", "if": {}}, {})

    def test_unknown_keyword_in_unreferenced_defs_fails_closed(self) -> None:
        schema = {
            "type": "object",
            "$defs": {"unused": {"type": "string", "format": "date-time"}},
        }
        with self.assertRaises(SchemaLintError):
            lint_schema(schema)

    def test_unknown_type_name_fails_closed(self) -> None:
        with self.assertRaises(SchemaLintError):
            lint_schema({"type": "uint64"})

    def test_tuple_form_items_fails_closed(self) -> None:
        with self.assertRaises(SchemaLintError):
            lint_schema({"type": "array", "items": [{"type": "string"}]})

    def test_unanchored_pattern_fails_closed(self) -> None:
        with self.assertRaises(SchemaLintError):
            lint_schema({"type": "string", "pattern": "[0-9]+"})
        with self.assertRaises(SchemaLintError):
            lint_schema({"type": "string", "pattern": "a$"})

    def test_bad_regex_fails_closed(self) -> None:
        with self.assertRaises(SchemaLintError):
            lint_schema({"type": "string", "pattern": "^($"})

    def test_required_failure_uses_object_path(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["a"],
            "properties": {"a": {"type": "string"}},
        }
        failures = self.checker.validate_inline(schema, {})
        self.assertEqual([""], [failure.path for failure in failures])
        self.assertEqual({"required"}, {failure.keyword for failure in failures})

    def test_non_finite_instance_is_rejected(self) -> None:
        with self.assertRaises(InvalidJSONError):
            self.checker.validate_inline({"type": "number"}, float("nan"))
        with self.assertRaises(InvalidJSONError):
            self.checker.validate_inline({"type": "number"}, float("inf"))

    def test_all_contract_schemas_lint(self) -> None:
        self.assertTrue(SCHEMA_DIR.is_dir())
        for name in self.checker.schema_names():
            with self.subTest(schema=name):
                self.checker.load(name)


if __name__ == "__main__":
    unittest.main()
