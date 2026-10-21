"""The restricted retrieval scope (J.2) and its minimal matching rule.

J.2 fixes five optional fields: ``project_id``, ``session_id``, ``scene``,
``entity_ids``, ``goal_id``.  An empty scope means "general within the
namespace".  Scope is an *applicability* hint only: it never grants file,
network or record access, and an authorized read is limited by the deletion
barrier, not by this module.

The matching rule implemented here is deliberately conservative and exactly the
one the handoff card states:

* a record with no scope is visible to every query;
* for every field present in the record scope, the query must name that field
  explicitly and with an equal value; a missing query field means "not
  applicable";
* ``entity_ids`` is a set: every entity ID of the record must be contained in
  the query's ``entity_ids``;
* no substring or out-of-range inference is performed.

Scope structure errors are ``SCOPE_VIOLATION``, matching the contract layer's
error-code mapping for everything inside a ``scope`` object.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..foundation.errors import ScopeViolation

#: The closed field set from ``contracts/v2/scope.json``.
SCOPE_FIELDS = ("project_id", "session_id", "scene", "entity_ids", "goal_id")

#: ``scope_id`` bound in ``contracts/v2/_common.json``.
SCOPE_ID_MAX_LENGTH = 256


def normalize_scope(value: Any, *, field: str = "scope") -> dict[str, Any]:
    """Validate a scope object and return the fields that were given.

    The returned dictionary keeps the canonical ``SCOPE_FIELDS`` order and
    preserves the caller's ``entity_ids`` order (canonical JSON preserves array
    order).  It contains only explicitly supplied fields; it never fills
    defaults.
    """

    if not isinstance(value, Mapping):
        raise ScopeViolation(
            f"{field} must be a scope object mapping the five J.2 fields"
        )
    unknown = sorted(str(key) for key in value if key not in SCOPE_FIELDS)
    if unknown:
        raise ScopeViolation(
            f"{field} has unknown field(s): {', '.join(unknown)}"
        )

    normalized: dict[str, Any] = {}
    for key in SCOPE_FIELDS:
        if key not in value:
            continue
        item = value[key]
        if key == "entity_ids":
            normalized[key] = _normalize_entity_ids(item, field)
        else:
            normalized[key] = _require_scope_id(item, f"{field}.{key}")
    return normalized


def _require_scope_id(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ScopeViolation(f"{field} must be a string")
    if not value:
        raise ScopeViolation(f"{field} must be a non-empty string")
    if len(value) > SCOPE_ID_MAX_LENGTH:
        raise ScopeViolation(
            f"{field} must be at most {SCOPE_ID_MAX_LENGTH} characters"
        )
    return value


def _normalize_entity_ids(value: Any, field: str) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ScopeViolation(f"{field}.entity_ids must be an array of strings")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        entity = _require_scope_id(item, f"{field}.entity_ids[{index}]")
        if entity in seen:
            raise ScopeViolation(
                f"{field}.entity_ids contains the duplicate {entity!r}"
            )
        seen.add(entity)
        result.append(entity)
    return result


def scope_matches(record_scope: Mapping[str, Any] | None, query_scope: Mapping[str, Any]) -> bool:
    """Apply the J.2 minimal rule: the record's fields must all be in the query."""

    if not record_scope:
        return True
    for key in SCOPE_FIELDS:
        if key not in record_scope:
            continue
        if key not in query_scope:
            return False
        if key == "entity_ids":
            wanted = set(query_scope[key])
            if not all(entity in wanted for entity in record_scope[key]):
                return False
        elif query_scope[key] != record_scope[key]:
            return False
    return True


__all__ = [
    "SCOPE_FIELDS",
    "SCOPE_ID_MAX_LENGTH",
    "normalize_scope",
    "scope_matches",
]
