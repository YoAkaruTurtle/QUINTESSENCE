"""Identifier and scalar checks for values that enter the store.

The regexes and bounds below mirror ``contracts/v2/_common.json`` (the frozen
PCS2-002 contracts).  The JSON Schemas stay the authority for wire DTOs; this
module exists so the store rejects out-of-contract identifiers before they
reach SQL, and both copies are pinned together so they cannot drift silently:
``tests/store/test_record_store.py`` ->
``InputValidationTests.test_identifier_constants_match_the_frozen_contract``
covers the PCS2-004 constants, and
``tests/store/test_event_store.py`` ->
``SourceFieldValidationTests.test_event_constants_match_the_frozen_contract``
covers the PCS2-005 event fields.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import InvalidInput, InvalidRecordVersion

MAX_SAFE_INTEGER = 9007199254740991

NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TIMESTAMP_PATTERN = re.compile(
    r"^(?:19|20)[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?Z$"
)
PRIVACY_CLASSES = ("private", "restricted", "public-example")

# J.2: the source kind is bound by the trusted entry point, never self-declared
# by a model.  The order matches ``contracts/v2/_common.json``.
SOURCE_KINDS = (
    "host_observation",
    "user_report",
    "imported_memory",
    "model_inference",
    "synthetic_fixture",
    "system_internal",
)

KIND_MAX_LENGTH = 128
SCHEMA_VERSION_MAX_LENGTH = 128
STATUS_MAX_LENGTH = 64
INSTANCE_MAX_LENGTH = 256
SOURCE_EVENT_ID_MAX_LENGTH = 512
EVENT_TYPE_MAX_LENGTH = 128


def require_namespace(value: Any) -> str:
    if not isinstance(value, str) or NAMESPACE_PATTERN.fullmatch(value) is None:
        raise InvalidInput("namespace must match ^[a-z][a-z0-9._-]{0,127}$")
    return value


def _require_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str) or UUID_PATTERN.fullmatch(value) is None:
        raise InvalidInput(f"{field} must be a canonical lower-case UUID")
    return value


def require_record_id(value: Any) -> str:
    return _require_uuid(value, "record_id")


def require_event_id(value: Any) -> str:
    return _require_uuid(value, "event_id")


def require_record_version(value: Any) -> int:
    # ``isinstance(True, int)`` is true in Python; booleans must not pass here.
    if type(value) is not int:
        raise InvalidRecordVersion("record_version must be an integer")
    if value < 1:
        raise InvalidRecordVersion("record_version starts at 1")
    if value > MAX_SAFE_INTEGER:
        raise InvalidRecordVersion("record_version exceeds the cross-language safe integer limit")
    return value


def _require_bounded_text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise InvalidInput(f"{field} must be at most {max_length} characters")
    return value


def require_kind(value: Any) -> str:
    return _require_bounded_text(value, "kind", KIND_MAX_LENGTH)


def require_schema_version(value: Any) -> str:
    return _require_bounded_text(value, "schema_version", SCHEMA_VERSION_MAX_LENGTH)


def require_status(value: Any) -> str:
    return _require_bounded_text(value, "status", STATUS_MAX_LENGTH)


def require_privacy_class(value: Any) -> str:
    if value not in PRIVACY_CLASSES:
        raise InvalidInput(f"privacy_class must be one of {', '.join(PRIVACY_CLASSES)}")
    return value


def require_recorded_at(value: Any) -> str:
    if not isinstance(value, str) or TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise InvalidInput("recorded_at must be a UTC RFC 3339 timestamp ending in Z")
    return value


def require_observed_at(value: Any) -> str | None:
    """``observed_at`` is nullable: an unknown observation time stays null."""

    if value is None:
        return None
    if not isinstance(value, str) or TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise InvalidInput("observed_at must be null or a UTC RFC 3339 timestamp ending in Z")
    return value


def require_source_position(value: Any) -> int | None:
    """Optional external source sequence number (J.1/J.2); null means unknown."""

    if value is None:
        return None
    if type(value) is not int:
        raise InvalidInput("source_position must be an integer or null")
    if value < 0:
        raise InvalidInput("source_position must not be negative")
    if value > MAX_SAFE_INTEGER:
        raise InvalidInput("source_position exceeds the cross-language safe integer limit")
    return value


def require_source_kind(value: Any) -> str:
    if value not in SOURCE_KINDS:
        raise InvalidInput(f"source_kind must be one of {', '.join(SOURCE_KINDS)}")
    return value


def require_instance_id(value: Any) -> str:
    return _require_bounded_text(value, "source_instance", INSTANCE_MAX_LENGTH)


def require_source_event_id(value: Any) -> str:
    return _require_bounded_text(value, "source_event_id", SOURCE_EVENT_ID_MAX_LENGTH)


def require_event_type(value: Any) -> str:
    return _require_bounded_text(value, "event_type", EVENT_TYPE_MAX_LENGTH)


#: Frozen request-envelope shape (``contracts/v2/request.json``): request IDs are
#: UUIDs and operations are lower-case dotted names.
OPERATION_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
CONTEXT_ID_MAX_LENGTH = 128


def require_request_id(value: Any) -> str:
    return _require_uuid(value, "request_id")


def require_operation(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidInput("operation must be a string")
    if len(value) > 128:
        raise InvalidInput("operation must be at most 128 characters")
    if OPERATION_PATTERN.fullmatch(value) is None:
        raise InvalidInput("operation must match ^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$")
    return value


def require_context_id(value: Any) -> str:
    """Server-generated snapshot ID; opaque but bounded, never caller-chosen."""

    return _require_bounded_text(value, "context_id", CONTEXT_ID_MAX_LENGTH)


__all__ = [
    "CONTEXT_ID_MAX_LENGTH",
    "EVENT_TYPE_MAX_LENGTH",
    "INSTANCE_MAX_LENGTH",
    "KIND_MAX_LENGTH",
    "MAX_SAFE_INTEGER",
    "NAMESPACE_PATTERN",
    "OPERATION_PATTERN",
    "PRIVACY_CLASSES",
    "SCHEMA_VERSION_MAX_LENGTH",
    "SOURCE_EVENT_ID_MAX_LENGTH",
    "SOURCE_KINDS",
    "STATUS_MAX_LENGTH",
    "TIMESTAMP_PATTERN",
    "UUID_PATTERN",
    "require_context_id",
    "require_event_id",
    "require_event_type",
    "require_instance_id",
    "require_kind",
    "require_namespace",
    "require_observed_at",
    "require_operation",
    "require_privacy_class",
    "require_record_id",
    "require_record_version",
    "require_recorded_at",
    "require_request_id",
    "require_schema_version",
    "require_source_event_id",
    "require_source_kind",
    "require_source_position",
    "require_status",
]
