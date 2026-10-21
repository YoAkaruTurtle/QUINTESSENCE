"""Golden-vector execution: every valid case passes, every invalid case fails
with the error code declared up front.

Raw-JSON cases exercise the strict parse layer (NaN / Infinity / 1e999); they
cannot be expressed as JSON instances by design.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from contract_checker import (  # noqa: E402
    ContractChecker,
    InvalidJSONError,
    strict_loads,
)
from contract_errors import (  # noqa: E402
    ERROR_CODE_SET,
    INVALID_SCHEMA,
    SCOPE_VIOLATION,
    STRUCTURAL_CODES,
    UNSUPPORTED_VERSION,
    classify_failures,
)

SCHEMA_DIR = HERE.parents[1] / "contracts" / "v2"
VECTOR_DIR = HERE / "vectors"

UUID_KEYS = {"event_id", "record_id", "request_id", "context_id"}


def load_vectors(name: str) -> dict[str, Any]:
    path = VECTOR_DIR / name
    return strict_loads(path.read_text(encoding="utf-8"), source=str(path))


def all_cases() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid = load_vectors("valid.json")
    invalid = load_vectors("invalid.json")
    return valid["cases"], invalid["cases"]


def pointer_get(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise AssertionError(f"unsupported test pointer {pointer!r}")
    node = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(token)]
        else:
            node = node[token]
    return node


def walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


class VectorIntegrityTests(unittest.TestCase):
    def test_vector_files_declare_the_same_format(self) -> None:
        for name in ("valid.json", "invalid.json"):
            with self.subTest(file=name):
                doc = load_vectors(name)
                self.assertEqual("pcs.v2-contract-vectors-1", doc["format"])
                self.assertEqual("pcs.v2", doc["protocol_version"])
                self.assertIsInstance(doc["cases"], list)

    def test_case_ids_are_unique(self) -> None:
        valid, invalid = all_cases()
        identifiers = [case["id"] for case in valid + invalid]
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_case_payload_shape(self) -> None:
        for case in all_cases()[0] + all_cases()[1]:
            with self.subTest(case=case["id"]):
                self.assertTrue(case["why"].strip())
                self.assertIn("schema", case)
                self.assertIn("expect", case)
                has_instance = "instance" in case
                has_raw = "raw_json" in case
                self.assertNotEqual(has_instance, has_raw)
                if case["expect"]["valid"]:
                    self.assertTrue(has_instance)
                    self.assertNotIn("code", case["expect"])
                else:
                    self.assertIn("code", case["expect"])

    def test_every_case_references_a_schema_file(self) -> None:
        schemas = {path.name for path in SCHEMA_DIR.glob("*.json")}
        for case in all_cases()[0] + all_cases()[1]:
            with self.subTest(case=case["id"]):
                self.assertIn(case["schema"], schemas)

    def test_every_schema_has_both_valid_and_invalid_vectors(self) -> None:
        valid, invalid = all_cases()
        valid_schemas = {case["schema"] for case in valid}
        invalid_schemas = {case["schema"] for case in invalid}
        expected = {
            "event_ref.json",
            "record_ref.json",
            "scope.json",
            "event.json",
            "record_version.json",
            "request.json",
            "response.json",
            "error.json",
            "context_envelope.json",
        }
        self.assertEqual(expected, valid_schemas)
        self.assertEqual(expected, invalid_schemas)

    def test_invalid_cases_declare_structural_codes(self) -> None:
        _, invalid = all_cases()
        declared = set()
        for case in invalid:
            with self.subTest(case=case["id"]):
                code = case["expect"]["code"]
                self.assertIn(code, STRUCTURAL_CODES)
                self.assertIn(code, ERROR_CODE_SET)
                declared.add(code)
        self.assertEqual(
            {INVALID_SCHEMA, SCOPE_VIOLATION, UNSUPPORTED_VERSION}, declared
        )

    def test_raw_json_cases_cover_nonfinite_and_ambiguous_json(self) -> None:
        _, invalid = all_cases()
        raw_cases = [case for case in invalid if "raw_json" in case]
        identifiers = {case["id"] for case in raw_cases}
        self.assertIn("raw_json.nan_record_version", identifiers)
        self.assertIn("raw_json.infinity_source_position", identifiers)
        self.assertIn("raw_json.overflow_1e999", identifiers)
        self.assertIn("raw_json.duplicate_key", identifiers)


class GoldenVectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checker = ContractChecker(SCHEMA_DIR)

    def test_valid_vectors_pass(self) -> None:
        valid, _ = all_cases()
        for case in valid:
            with self.subTest(case=case["id"]):
                failures = self.checker.validate(case["schema"], case["instance"])
                self.assertEqual(
                    [],
                    [failure.as_tuple() for failure in failures],
                    f"{case['id']} should be valid",
                )

    def test_invalid_vectors_fail_with_declared_code(self) -> None:
        _, invalid = all_cases()
        for case in invalid:
            if "raw_json" in case:
                # Parse-layer rejection is asserted separately.
                continue
            with self.subTest(case=case["id"]):
                failures = self.checker.validate(case["schema"], case["instance"])
                self.assertTrue(failures, f"{case['id']} should be rejected")
                actual = classify_failures(failures, case["schema"])
                self.assertEqual(
                    case["expect"]["code"],
                    actual,
                    f"{case['id']}: failures {[f.as_tuple() for f in failures]}",
                )

    def test_raw_json_vectors_fail_at_parse_layer(self) -> None:
        _, invalid = all_cases()
        for case in invalid:
            if "raw_json" not in case:
                continue
            with self.subTest(case=case["id"]):
                with self.assertRaises(InvalidJSONError):
                    strict_loads(case["raw_json"], source=case["id"])
                self.assertEqual(INVALID_SCHEMA, case["expect"]["code"])

    def test_key_rejections_have_the_intended_cause(self) -> None:
        """A right error code from the wrong constraint is still a false pass."""

        _, invalid = all_cases()
        by_id = {case["id"]: case for case in invalid}
        causes = {
            "record_ref.version_zero": ("/record_version", "minimum"),
            "record_ref.version_too_large": ("/record_version", "maximum"),
            "record_ref.version_bool": ("/record_version", "type"),
            "record_ref.unknown_state_revision": (
                "/state_revision",
                "additionalProperties",
            ),
            "record_version.unknown_state_revision": (
                "/state_revision",
                "additionalProperties",
            ),
            "event.missing_source_instance": ("", "required"),
            "event.event_seq_unknown_top_field": (
                "/event_seq",
                "additionalProperties",
            ),
            "event.source_kind_unknown": ("/source_kind", "enum"),
            "event.causation_ref_bare_id": ("/causation_refs/0", "oneOf"),
            "record_version.scope_unknown_key": (
                "/scope/tenant_id",
                "additionalProperties",
            ),
            "scope.unknown_key": ("/tenant_id", "additionalProperties"),
            "request.protocol_version_v1": ("/protocol_version", "const"),
            "request.unknown_authorization_field": (
                "/role",
                "additionalProperties",
            ),
            "error.code_unknown": ("/code", "enum"),
            "context_envelope.content_hash_bad": ("/content_hash", "pattern"),
            "context_envelope.read_set_usage_unknown": (
                "/read_set/0/usage",
                "enum",
            ),
        }
        for case_id, (path, keyword) in causes.items():
            with self.subTest(case=case_id):
                case = by_id[case_id]
                failures = self.checker.validate(case["schema"], case["instance"])
                tuples = {(failure.path, failure.keyword) for failure in failures}
                self.assertIn((path, keyword), tuples, failures)

    def test_validation_is_deterministic(self) -> None:
        _, invalid = all_cases()
        fresh = ContractChecker(SCHEMA_DIR)
        for case in invalid:
            if "raw_json" in case:
                continue
            with self.subTest(case=case["id"]):
                first = [f.as_tuple() for f in self.checker.validate(
                    case["schema"], case["instance"]
                )]
                second = [f.as_tuple() for f in self.checker.validate(
                    case["schema"], case["instance"]
                )]
                third = [f.as_tuple() for f in fresh.validate(
                    case["schema"], case["instance"]
                )]
                self.assertEqual(first, second)
                self.assertEqual(first, third)

    def test_fixed_uuid_fixtures_are_valid_uuids(self) -> None:
        valid, _ = all_cases()
        for case in valid:
            for key, value in (
                (k, v) for k, v in self._pairs(case["instance"]) if k in UUID_KEYS
            ):
                if not isinstance(value, str):
                    continue
                with self.subTest(case=case["id"], key=key, value=value):
                    parsed = uuid.UUID(value)
                    self.assertEqual(value, str(parsed))

    def _pairs(self, value: Any):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key, child
                yield from self._pairs(child)
        elif isinstance(value, list):
            for child in value:
                yield from self._pairs(child)

    def test_chinese_and_long_path_length_assertions(self) -> None:
        valid, _ = all_cases()
        case = next(
            case
            for case in valid
            if case["id"] == "record_version.valid.chinese_long_path"
        )
        assertions = case["length_assertions"]
        self.assertEqual(3, len(assertions))
        for assertion in assertions:
            with self.subTest(pointer=assertion["pointer"]):
                text = pointer_get(case["instance"], assertion["pointer"])
                self.assertIsInstance(text, str)
                self.assertEqual(assertion["chars"], len(text))
                self.assertEqual(
                    assertion["utf8_bytes"], len(text.encode("utf-8"))
                )
        long_path = pointer_get(case["instance"], "/body/attachment_path")
        self.assertGreater(len(long_path), 260)

    def test_canonical_dump_rule(self) -> None:
        valid, _ = all_cases()
        case = next(
            case
            for case in valid
            if case["id"] == "record_version.valid.chinese_long_path"
        )
        rule = case["canonical_dump"]
        body = pointer_get(case["instance"], f"/{rule['output']}")
        dump = json.dumps(
            body,
            ensure_ascii=rule["ensure_ascii"],
            separators=tuple(rule["separators"]),
            allow_nan=False,
        )
        self.assertEqual(rule["chars"], len(dump))
        self.assertEqual(rule["utf8_bytes"], len(dump.encode("utf-8")))
        self.assertEqual(
            rule["sha256"], hashlib.sha256(dump.encode("utf-8")).hexdigest()
        )
        self.assertNotIn("\\u", dump)
        self.assertNotIn("\r", dump)
        escaped = json.dumps(
            body, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        )
        self.assertNotEqual(dump, escaped)
        self.assertIn("\\u4e2d", escaped)

    def test_valid_shapes_round_trip_through_strict_json(self) -> None:
        valid, _ = all_cases()
        for case in valid:
            with self.subTest(case=case["id"]):
                text = json.dumps(
                    case["instance"], ensure_ascii=False, allow_nan=False
                )
                self.assertEqual(case["instance"], strict_loads(text, case["id"]))


if __name__ == "__main__":
    unittest.main()
