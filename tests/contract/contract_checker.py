"""Deterministic, dependency-free JSON Schema checker for the pcs.v2 contracts.

Why this module exists
----------------------
The project machine has no ``pytest`` and no ``jsonschema``, and PCS2-002 must
run offline.  The schemas in ``contracts/v2`` are the single source of truth;
this checker only *evaluates* them.  It therefore implements exactly the JSON
Schema keywords the contracts use and nothing else:

    $ref, $defs, type, required, properties, additionalProperties,
    enum, const, pattern, minLength, maxLength, minimum, maximum,
    items, minItems, maxItems, uniqueItems, oneOf

Every other keyword is a hard error (fail-closed).  Silently ignoring a
keyword would turn a constraint into no constraint and produce false passes.

Deliberate semantics (also documented in contracts/v2/README.md)
----------------------------------------------------------------
* ``type: integer`` means ``type(v) is int``; ``True``/``False`` are not
  integers.  ``1.0`` is a float and is also rejected as an integer.
* ``pattern`` is evaluated with ``re.fullmatch`` and every schema pattern must
  be anchored with ``^...$``.  This avoids the Python-only trap where ``$``
  also matches before a trailing newline (ECMA-262 ``$`` does not).
* Instances are checked to be genuine JSON values before validation: NaN,
  +/-Infinity (including ``1e999`` overflow), duplicate object keys and
  non-JSON Python types are rejected.  ``json.loads`` accepts NaN/Infinity by
  default, so the strict loader passes ``parse_constant``, re-scans for
  non-finite floats, and uses an ``object_pairs_hook`` that fails on
  duplicate keys (Python would otherwise silently keep the last one).
* ``enum``/``const``/``uniqueItems`` use a type-aware canonical form, so
  ``1`` and ``true`` are different values while ``1`` and ``1.0`` are equal
  per JSON Schema number semantics.
* Only explicitly nullable fields may be ``null``; a missing property is still
  a missing property.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

MAX_SAFE_INTEGER = 9007199254740991
MAX_DEPTH = 256

_ANNOTATIONS = frozenset(
    {
        "$schema",
        "$id",
        "title",
        "description",
        "$comment",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
    }
)

_CONSTRAINT_KEYWORDS = frozenset(
    {
        "$ref",
        "$defs",
        "type",
        "required",
        "properties",
        "additionalProperties",
        "enum",
        "const",
        "pattern",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "oneOf",
    }
)

_KNOWN_KEYWORDS = _ANNOTATIONS | _CONSTRAINT_KEYWORDS

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: type(v) is int,
    "number": lambda v: type(v) in (int, float) and math.isfinite(v),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class ContractCheckerError(Exception):
    """Base class for checker failures that must never be swallowed."""


class InvalidJSONError(ContractCheckerError):
    """The JSON text (instance, vector or schema) is not strict JSON."""


class SchemaLintError(ContractCheckerError):
    """A schema uses an unsupported keyword/form; fail closed."""


class RefResolutionError(ContractCheckerError):
    """A ``$ref`` points outside the schema directory or at a missing node."""


@dataclass(frozen=True)
class Failure:
    """One structural validation failure."""

    path: str
    keyword: str
    message: str

    def as_tuple(self) -> tuple[str, str, str]:
        return (self.path, self.keyword, self.message)


def _json_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def strict_loads(text: str, source: str = "<string>") -> Any:
    """Parse JSON, rejecting NaN/Infinity, non-finite overflow and duplicate keys."""

    def _bad_constant(name: str) -> Any:
        raise InvalidJSONError(f"{source}: non-finite JSON constant {name!r}")

    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise InvalidJSONError(
                    f"{source}: duplicate object key {key!r} is ambiguous"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            parse_constant=_bad_constant,
            object_pairs_hook=_unique_object,
        )
    except InvalidJSONError:
        raise
    except json.JSONDecodeError as exc:
        raise InvalidJSONError(f"{source}: invalid JSON: {exc}") from exc
    assert_json_value(value, source)
    return value


def strict_loads_file(path: Path) -> Any:
    return strict_loads(path.read_text(encoding="utf-8"), source=str(path))


def assert_json_value(value: Any, source: str = "<value>") -> None:
    """Fail unless ``value`` is a genuine JSON value (no NaN/Inf, no sets)."""

    stack = [value]
    while stack:
        item = stack.pop()
        if item is None or isinstance(item, (bool, str)):
            continue
        if type(item) is int:
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise InvalidJSONError(f"{source}: non-finite number {item!r}")
            continue
        if isinstance(item, list):
            stack.extend(item)
            continue
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise InvalidJSONError(f"{source}: non-string object key {key!r}")
                stack.append(child)
            continue
        raise InvalidJSONError(
            f"{source}: value of type {type(item).__name__} is not JSON"
        )


def _canonical(value: Any) -> Any:
    """Type-aware canonical form used for equality and uniqueItems.

    ``1`` and ``1.0`` compare equal (JSON numbers); ``true`` does not equal
    ``1``; arrays are ordered, objects are key-order independent.
    """

    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("boolean", value)
    if type(value) in (int, float):
        return ("number", value)
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_canonical(item) for item in value))
    if isinstance(value, dict):
        return (
            "object",
            tuple(sorted((key, _canonical(child)) for key, child in value.items())),
        )
    raise ContractCheckerError(f"non-JSON value in instance: {type(value).__name__}")


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _is_anchored(pattern: str) -> bool:
    return (
        pattern.startswith("^")
        and pattern.endswith("$")
        and not _is_escaped(pattern, len(pattern) - 1)
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SchemaLintError(message)


def lint_schema(schema: Any, label: str = "<schema>") -> None:
    """Recursively reject unknown keywords and unsupported schema forms."""

    _lint(schema, label, "")


def _lint(node: Any, label: str, path: str) -> None:
    if isinstance(node, bool):
        return
    _require(
        isinstance(node, dict),
        f"{label}{path}: schema must be an object or boolean, got {type(node).__name__}",
    )
    for key in node:
        _require(
            key in _KNOWN_KEYWORDS,
            f"{label}{path}: unsupported JSON Schema keyword {key!r} (fail-closed)",
        )

    if "type" in node:
        raw = node["type"]
        if isinstance(raw, str):
            names = [raw]
        else:
            _require(
                isinstance(raw, list) and raw,
                f"{label}{path}/type: must be a type name or a non-empty list",
            )
            names = raw
        _require(
            all(isinstance(name, str) for name in names),
            f"{label}{path}/type: type names must be strings",
        )
        _require(
            len(set(names)) == len(names),
            f"{label}{path}/type: duplicate type names",
        )
        for name in names:
            _require(
                name in _TYPE_CHECKS,
                f"{label}{path}/type: unsupported type {name!r}",
            )

    if "required" in node:
        required = node["required"]
        _require(
            isinstance(required, list) and all(isinstance(k, str) for k in required),
            f"{label}{path}/required: must be a list of property names",
        )
        _require(
            len(set(required)) == len(required),
            f"{label}{path}/required: duplicate property names",
        )

    for container in ("properties", "$defs"):
        if container in node:
            mapping = node[container]
            _require(
                isinstance(mapping, dict),
                f"{label}{path}/{container}: must be an object",
            )
            for name, child in mapping.items():
                _lint(child, label, f"{path}/{container}/{name}")

    if "additionalProperties" in node:
        extra = node["additionalProperties"]
        if not isinstance(extra, bool):
            _lint(extra, label, f"{path}/additionalProperties")

    if "items" in node:
        items = node["items"]
        _require(
            not isinstance(items, list),
            f"{label}{path}/items: tuple-form items is intentionally unsupported",
        )
        _lint(items, label, f"{path}/items")

    if "oneOf" in node:
        branches = node["oneOf"]
        _require(
            isinstance(branches, list) and len(branches) >= 1,
            f"{label}{path}/oneOf: must be a non-empty list",
        )
        for index, branch in enumerate(branches):
            _lint(branch, label, f"{path}/oneOf/{index}")

    if "pattern" in node:
        pattern = node["pattern"]
        _require(
            isinstance(pattern, str),
            f"{label}{path}/pattern: must be a string",
        )
        _require(
            _is_anchored(pattern),
            f"{label}{path}/pattern: patterns must be anchored with ^...$ "
            "(the checker uses fullmatch to avoid Python's trailing-newline trap)",
        )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SchemaLintError(
                f"{label}{path}/pattern: invalid regular expression: {exc}"
            ) from exc

    if "$ref" in node:
        _require(
            isinstance(node["$ref"], str) and node["$ref"],
            f"{label}{path}/$ref: must be a non-empty string",
        )

    if "enum" in node:
        enum = node["enum"]
        _require(
            isinstance(enum, list) and enum,
            f"{label}{path}/enum: must be a non-empty list",
        )
        for member in enum:
            assert_json_value(member, f"{label}{path}/enum")

    if "const" in node:
        assert_json_value(node["const"], f"{label}{path}/const")

    for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
        if keyword in node:
            value = node[keyword]
            _require(
                type(value) is int and value >= 0,
                f"{label}{path}/{keyword}: must be a non-negative integer",
            )

    for keyword in ("minimum", "maximum"):
        if keyword in node:
            value = node[keyword]
            _require(
                type(value) in (int, float) and math.isfinite(value),
                f"{label}{path}/{keyword}: must be a finite number",
            )

    if "uniqueItems" in node:
        _require(
            isinstance(node["uniqueItems"], bool),
            f"{label}{path}/uniqueItems: must be a boolean",
        )


class ContractChecker:
    """Validate instances against the JSON Schema files in one directory."""

    def __init__(self, schema_dir: Path | str) -> None:
        self.schema_dir = Path(schema_dir).resolve()
        if not self.schema_dir.is_dir():
            raise ContractCheckerError(f"schema directory not found: {self.schema_dir}")
        self._docs: dict[str, Any] = {}
        self._virtual: dict[str, Any] = {}

    # -- public API ---------------------------------------------------------

    def schema_names(self) -> list[str]:
        return sorted(path.name for path in self.schema_dir.glob("*.json"))

    def load(self, name: str) -> Any:
        return self._load(name)

    def validate(self, schema_name: str, instance: Any) -> list[Failure]:
        assert_json_value(instance, "instance")
        schema = self._load(schema_name)
        return self._val(schema_name, schema, instance, "", 0)

    def is_valid(self, schema_name: str, instance: Any) -> bool:
        return not self.validate(schema_name, instance)

    def validate_inline(self, schema: Any, instance: Any) -> list[Failure]:
        assert_json_value(instance, "instance")
        label = f"<inline:{id(schema)}>"
        lint_schema(schema, label)
        self._virtual[label] = schema
        try:
            return self._val(label, schema, instance, "", 0)
        finally:
            self._virtual.pop(label, None)

    def resolve(self, doc_name: str, ref: str) -> tuple[str, Any]:
        return self._resolve(doc_name, ref)

    # -- loading ------------------------------------------------------------

    def _load(self, name: str) -> Any:
        if name in self._virtual:
            return self._virtual[name]
        if name in self._docs:
            return self._docs[name]
        path = self._doc_path(name)
        doc = strict_loads_file(path)
        lint_schema(doc, name)
        self._docs[name] = doc
        return doc

    def _doc_path(self, name: str) -> Path:
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise RefResolutionError(f"illegal schema reference {name!r}")
        if pure.suffix != ".json":
            raise RefResolutionError(
                f"schema reference {name!r} must name a .json file"
            )
        path = (self.schema_dir / pure).resolve()
        try:
            path.relative_to(self.schema_dir)
        except ValueError as exc:
            raise RefResolutionError(
                f"schema reference {name!r} escapes {self.schema_dir}"
            ) from exc
        if not path.is_file():
            raise RefResolutionError(f"schema file not found: {name!r}")
        return path

    def _resolve(self, doc_name: str, ref: str) -> tuple[str, Any]:
        if not isinstance(ref, str) or not ref:
            raise RefResolutionError(f"invalid $ref {ref!r}")
        if ref.count("#") > 1:
            raise RefResolutionError(f"invalid $ref {ref!r}")
        if "#" in ref:
            file_part, fragment = ref.split("#", 1)
        else:
            file_part, fragment = ref, ""

        if file_part:
            target_name = str(PurePosixPath(file_part))
        else:
            target_name = doc_name

        node = self._load(target_name)
        if fragment:
            if not fragment.startswith("/"):
                raise RefResolutionError(
                    f"unsupported $ref fragment {fragment!r} in {ref!r}"
                )
            for raw_token in fragment[1:].split("/"):
                token = raw_token.replace("~1", "/").replace("~0", "~")
                if isinstance(node, dict) and token in node:
                    node = node[token]
                elif isinstance(node, list) and token.isdigit():
                    index = int(token)
                    if index >= len(node):
                        raise RefResolutionError(
                            f"$ref {ref!r} points past the end of an array"
                        )
                    node = node[index]
                else:
                    raise RefResolutionError(
                        f"$ref {ref!r}: pointer token {token!r} not found"
                    )
        return target_name, node

    # -- validation ---------------------------------------------------------

    def _val(
        self,
        doc_name: str,
        node: Any,
        instance: Any,
        path: str,
        depth: int,
    ) -> list[Failure]:
        if depth > MAX_DEPTH:
            raise ContractCheckerError(
                f"schema recursion exceeded {MAX_DEPTH} levels at {path}"
            )
        if node is True:
            return []
        if node is False:
            return [Failure(path, "false", "schema is false")]
        if not isinstance(node, dict):
            raise ContractCheckerError(
                f"schema node at {path} is not an object or boolean"
            )

        if "$ref" in node:
            target_name, target = self._resolve(doc_name, node["$ref"])
            failures = self._val(target_name, target, instance, path, depth + 1)
            siblings = {
                key: value
                for key, value in node.items()
                if key not in _ANNOTATIONS and key != "$ref"
            }
            if siblings:
                failures.extend(
                    self._val(doc_name, siblings, instance, path, depth + 1)
                )
            return failures

        failures: list[Failure] = []

        if "type" in node:
            raw = node["type"]
            names = [raw] if isinstance(raw, str) else raw
            if not any(_TYPE_CHECKS[name](instance) for name in names):
                failures.append(
                    Failure(
                        path,
                        "type",
                        f"expected {'/'.join(names)}, got {_type_name(instance)}",
                    )
                )
                return failures

        if "const" in node and not _json_equal(instance, node["const"]):
            failures.append(
                Failure(path, "const", f"must equal {node['const']!r}")
            )

        if "enum" in node and not any(
            _json_equal(instance, member) for member in node["enum"]
        ):
            failures.append(
                Failure(
                    path,
                    "enum",
                    f"must be one of {node['enum']!r}, got {instance!r}",
                )
            )

        if isinstance(instance, dict):
            self._val_object(doc_name, node, instance, path, depth, failures)
        elif isinstance(instance, list):
            self._val_array(doc_name, node, instance, path, depth, failures)
        elif isinstance(instance, str):
            self._val_string(node, instance, path, failures)
        elif type(instance) in (int, float) and not isinstance(instance, bool):
            self._val_number(node, instance, path, failures)

        if "oneOf" in node:
            matched = 0
            for branch in node["oneOf"]:
                if not self._val(doc_name, branch, instance, path, depth + 1):
                    matched += 1
            if matched != 1:
                failures.append(
                    Failure(
                        path,
                        "oneOf",
                        f"exactly one oneOf branch must match, matched {matched}",
                    )
                )

        return failures

    def _val_object(
        self,
        doc_name: str,
        node: dict[str, Any],
        instance: dict[str, Any],
        path: str,
        depth: int,
        failures: list[Failure],
    ) -> None:
        required = node.get("required", ())
        for name in required:
            if name not in instance:
                failures.append(
                    Failure(
                        path,
                        "required",
                        f"missing required property {name!r}",
                    )
                )

        properties = node.get("properties", {})
        for name, value in instance.items():
            child_path = f"{path}/{_json_pointer_token(name)}"
            if name in properties:
                failures.extend(
                    self._val(
                        doc_name,
                        properties[name],
                        value,
                        child_path,
                        depth + 1,
                    )
                )
            elif "additionalProperties" in node:
                extra = node["additionalProperties"]
                if extra is False:
                    failures.append(
                        Failure(
                            child_path,
                            "additionalProperties",
                            f"unknown property {name!r}",
                        )
                    )
                elif extra is not True:
                    failures.extend(
                        self._val(doc_name, extra, value, child_path, depth + 1)
                    )

    def _val_array(
        self,
        doc_name: str,
        node: dict[str, Any],
        instance: list[Any],
        path: str,
        depth: int,
        failures: list[Failure],
    ) -> None:
        if "items" in node:
            for index, value in enumerate(instance):
                failures.extend(
                    self._val(
                        doc_name,
                        node["items"],
                        value,
                        f"{path}/{index}",
                        depth + 1,
                    )
                )
        if "minItems" in node and len(instance) < node["minItems"]:
            failures.append(
                Failure(
                    path,
                    "minItems",
                    f"expected at least {node['minItems']} items, got {len(instance)}",
                )
            )
        if "maxItems" in node and len(instance) > node["maxItems"]:
            failures.append(
                Failure(
                    path,
                    "maxItems",
                    f"expected at most {node['maxItems']} items, got {len(instance)}",
                )
            )
        if node.get("uniqueItems") is True:
            seen: dict[Any, int] = {}
            for index, value in enumerate(instance):
                canonical = _canonical(value)
                if canonical in seen:
                    failures.append(
                        Failure(
                            f"{path}/{index}",
                            "uniqueItems",
                            f"duplicate of item {seen[canonical]}",
                        )
                    )
                else:
                    seen[canonical] = index

    def _val_string(
        self,
        node: dict[str, Any],
        instance: str,
        path: str,
        failures: list[Failure],
    ) -> None:
        if "minLength" in node and len(instance) < node["minLength"]:
            failures.append(
                Failure(
                    path,
                    "minLength",
                    f"expected at least {node['minLength']} characters, got {len(instance)}",
                )
            )
        if "maxLength" in node and len(instance) > node["maxLength"]:
            failures.append(
                Failure(
                    path,
                    "maxLength",
                    f"expected at most {node['maxLength']} characters, got {len(instance)}",
                )
            )
        if "pattern" in node and re.fullmatch(node["pattern"], instance) is None:
            failures.append(
                Failure(
                    path,
                    "pattern",
                    f"does not match {node['pattern']!r}",
                )
            )

    def _val_number(
        self,
        node: dict[str, Any],
        instance: int | float,
        path: str,
        failures: list[Failure],
    ) -> None:
        if "minimum" in node and instance < node["minimum"]:
            failures.append(
                Failure(
                    path,
                    "minimum",
                    f"expected >= {node['minimum']}, got {instance!r}",
                )
            )
        if "maximum" in node and instance > node["maximum"]:
            failures.append(
                Failure(
                    path,
                    "maximum",
                    f"expected <= {node['maximum']}, got {instance!r}",
                )
            )


def _json_equal(left: Any, right: Any) -> bool:
    return _canonical(left) == _canonical(right)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def validate_file_against_schema(
    schema_name: str,
    instance_path: Path,
    schema_dir: Path,
) -> list[Failure]:
    """Convenience helper; instance text is loaded with the strict parser."""

    checker = ContractChecker(schema_dir)
    return checker.validate(schema_name, strict_loads_file(instance_path))
