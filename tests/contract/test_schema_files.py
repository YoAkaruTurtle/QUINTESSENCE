"""Schema-file invariants: field sets, enums, bounds, refs and line endings.

These tests read the public JSON Schema files directly. They pin the fields
named by appendix J.1-J.5 so a future edit cannot quietly drop a required
field, add a state_revision field, or let the error-code enum drift.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Iterator

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from contract_checker import (  # noqa: E402
    MAX_SAFE_INTEGER,
    ContractChecker,
    strict_loads_file,
)
from contract_errors import ERROR_CODES  # noqa: E402

REPO_ROOT = HERE.parents[1]
SCHEMA_DIR = REPO_ROOT / "contracts" / "v2"

PUBLIC_SCHEMAS = {
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

# Field lists below are copied from appendix J.1-J.5, not from the card summary.
EVENT_REQUIRED = {
    "protocol_version",
    "event_id",
    "namespace",
    "source_kind",
    "source_instance",
    "source_event_id",
    "source_position",
    "event_type",
    "observed_at",
    "recorded_at",
    "payload_ref",
    "causation_refs",
    "privacy_class",
}

RECORD_VERSION_REQUIRED = {
    "namespace",
    "record_id",
    "record_version",
    "kind",
    "schema_version",
    "status",
    "body",
    "scope",
    "source_refs",
    "created_by_event_ref",
    "committed_state_revision",
    "recorded_at",
    "privacy_class",
}

CONTEXT_REQUIRED = {
    "context_id",
    "namespace",
    "run_id",
    "trigger_ref",
    "state_revision",
    "context_event_seq",
    "privacy_epoch",
    "profile_ref",
    "read_set",
    "memory_hits",
    "belief_refs",
    "question_refs",
    "focus_refs",
    "unknown_execution_ids",
    "permission_view_ref",
    "budget_view",
    "pending_source_event_refs",
    "omissions",
    "index_generation",
    "index_coverage",
    "content_hash",
    "model_input_complete",
}

SCOPE_FIELDS = {"project_id", "session_id", "scene", "entity_ids", "goal_id"}

ERROR_REQUIRED = {"code", "message", "retryable", "details"}


def load(name: str) -> Any:
    return strict_loads_file(SCHEMA_DIR / name)


def common_defs() -> dict[str, Any]:
    return load("_common.json")["$defs"]


def iter_refs(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        if "$ref" in node:
            yield node["$ref"]
        for key, value in node.items():
            if key in {"$ref", "const", "enum", "default", "examples"}:
                continue
            yield from iter_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_refs(item)


class SchemaFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checker = ContractChecker(SCHEMA_DIR)

    def test_expected_public_schema_files_exist(self) -> None:
        on_disk = {path.name for path in SCHEMA_DIR.glob("*.json")}
        self.assertEqual(PUBLIC_SCHEMAS | {"_common.json"}, on_disk)

    def test_every_schema_file_parses_strictly_and_lints(self) -> None:
        for name in sorted(self.checker.schema_names()):
            with self.subTest(schema=name):
                self.checker.load(name)
                self.assertEqual(
                    "https://json-schema.org/draft/2020-12/schema",
                    self.checker.load(name)["$schema"],
                )

    def test_event_fields_match_j2(self) -> None:
        event = load("event.json")
        self.assertFalse(event["additionalProperties"])
        self.assertEqual(EVENT_REQUIRED, set(event["required"]))
        self.assertEqual(EVENT_REQUIRED, set(event["properties"]))
        self.assertIn("source_position", event["properties"])
        nullable = self.checker.resolve(
            "event.json", event["properties"]["source_position"]["$ref"]
        )[1]
        self.assertIn("null", nullable["type"])

    def test_record_version_fields_match_j2(self) -> None:
        record = load("record_version.json")
        self.assertFalse(record["additionalProperties"])
        self.assertEqual(RECORD_VERSION_REQUIRED, set(record["required"]))
        self.assertEqual(RECORD_VERSION_REQUIRED, set(record["properties"]))
        self.assertIn("committed_state_revision", record["properties"])
        self.assertNotIn("state_revision", record["properties"])

    def test_record_ref_has_no_state_revision(self) -> None:
        record_ref = common_defs()["record_ref"]
        self.assertFalse(record_ref["additionalProperties"])
        self.assertEqual(
            {"ref_type", "namespace", "record_id", "record_version"},
            set(record_ref["required"]),
        )
        self.assertNotIn("state_revision", record_ref["properties"])

    def test_context_envelope_fields_match_j5(self) -> None:
        envelope = load("context_envelope.json")
        self.assertFalse(envelope["additionalProperties"])
        self.assertEqual(CONTEXT_REQUIRED, set(envelope["required"]))
        self.assertEqual(
            CONTEXT_REQUIRED | {"source_refs", "profile_version"},
            set(envelope["properties"]),
        )
        self.assertEqual(22, len(CONTEXT_REQUIRED))

        read_set = envelope["properties"]["read_set"]["items"]
        self.assertFalse(read_set["additionalProperties"])
        self.assertEqual({"ref", "usage"}, set(read_set["required"]))
        self.assertEqual(
            {"current", "historical", "pinned_asset"},
            set(read_set["properties"]["usage"]["enum"]),
        )

    def test_scope_fields_are_closed(self) -> None:
        scope = load("scope.json")
        self.assertFalse(scope["additionalProperties"])
        self.assertEqual(SCOPE_FIELDS, set(scope["properties"]))
        self.assertNotIn("required", scope)

    def test_request_and_response_fields_match_j4(self) -> None:
        request = load("request.json")
        self.assertFalse(request["additionalProperties"])
        self.assertEqual(
            {
                "protocol_version",
                "request_id",
                "namespace",
                "operation",
                "expected_state_revision",
                "payload",
            },
            set(request["required"]),
        )
        revision = request["properties"]["expected_state_revision"]
        self.assertIn("null", revision["type"])

        response = load("response.json")
        self.assertFalse(response["additionalProperties"])
        self.assertEqual(
            {
                "protocol_version",
                "request_id",
                "ok",
                "state_revision",
                "data",
                "error",
                "warnings",
            },
            set(response["required"]),
        )

    def test_error_object_fields_and_enum_match_j4(self) -> None:
        error = load("error.json")
        self.assertFalse(error["additionalProperties"])
        self.assertEqual(ERROR_REQUIRED, set(error["required"]))
        self.assertEqual(ERROR_REQUIRED, set(error["properties"]))
        self.assertEqual(list(ERROR_CODES), error["properties"]["code"]["enum"])
        self.assertEqual(22, len(ERROR_CODES))

    def test_ref_type_discriminators(self) -> None:
        defs = common_defs()
        self.assertEqual(
            {"const": "event"}, defs["event_ref"]["properties"]["ref_type"]
        )
        self.assertEqual(
            {"const": "record"}, defs["record_ref"]["properties"]["ref_type"]
        )

    def test_numeric_bounds(self) -> None:
        defs = common_defs()
        record_version = defs["record_version_int"]
        self.assertEqual(1, record_version["minimum"])
        self.assertEqual(MAX_SAFE_INTEGER, record_version["maximum"])
        state_revision = defs["state_revision_int"]
        self.assertEqual(0, state_revision["minimum"])
        self.assertEqual(MAX_SAFE_INTEGER, state_revision["maximum"])

    def test_fixed_enums(self) -> None:
        defs = common_defs()
        self.assertEqual(
            [
                "host_observation",
                "user_report",
                "imported_memory",
                "model_inference",
                "synthetic_fixture",
                "system_internal",
            ],
            defs["source_kind"]["enum"],
        )
        self.assertEqual(
            ["private", "restricted", "public-example"],
            defs["privacy_class"]["enum"],
        )

    def test_all_dtos_reject_unknown_fields(self) -> None:
        defs = common_defs()
        for name in (
            "event.json",
            "record_version.json",
            "request.json",
            "response.json",
            "error.json",
            "context_envelope.json",
            "scope.json",
        ):
            with self.subTest(schema=name):
                self.assertIs(False, load(name)["additionalProperties"])
        self.assertIs(False, defs["event_ref"]["additionalProperties"])
        self.assertIs(False, defs["record_ref"]["additionalProperties"])

    def test_thin_ref_files_do_not_reimplement_shapes(self) -> None:
        for name, target in (
            ("event_ref.json", "_common.json#/$defs/event_ref"),
            ("record_ref.json", "_common.json#/$defs/record_ref"),
        ):
            with self.subTest(schema=name):
                doc = load(name)
                self.assertEqual(target, doc["$ref"])
                self.assertNotIn("required", doc)

    def test_every_ref_resolves(self) -> None:
        for name in sorted(self.checker.schema_names()):
            doc = self.checker.load(name)
            for ref in iter_refs(doc):
                with self.subTest(schema=name, ref=ref):
                    target_name, target = self.checker.resolve(name, ref)
                    self.assertTrue(target_name)
                    self.assertIsInstance(target, (dict, bool))

    def test_text_files_use_lf_and_gitattributes_pins_it(self) -> None:
        attribute_file = REPO_ROOT / ".gitattributes"
        paths = (
            list(SCHEMA_DIR.rglob("*"))
            + list(HERE.rglob("*"))
            + [REPO_ROOT / "README.md", REPO_ROOT / "pyproject.toml", attribute_file]
        )
        checked = 0
        for path in paths:
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            data = path.read_bytes()
            with self.subTest(path=str(path)):
                self.assertNotIn(b"\r", data, "contract files must keep LF endings")
                self.assertTrue(data.endswith(b"\n"), "file must end with a newline")
            checked += 1
        self.assertGreater(checked, 10)

        rules = {
            line.strip()
            for line in attribute_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        required_rules = {
            "* text=auto eol=lf",
            "*.py text eol=lf",
            "*.json text eol=lf",
            "*.md text eol=lf",
            "*.toml text eol=lf",
        }
        self.assertTrue(
            required_rules.issubset(rules),
            f".gitattributes must at least pin {sorted(required_rules - rules)}; "
            f"found {sorted(rules)}",
        )


if __name__ == "__main__":
    unittest.main()
