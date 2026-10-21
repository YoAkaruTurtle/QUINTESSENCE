"""pcs.v2 error-code enum and the contract-checker's deterministic mapping.

The enum is fixed from appendix J.4.  This module does not decide *when* a
server must raise each code -- that is state-layer semantics (later cards).

Structural failures are mapped to exactly one protocol code by the policy
documented in ``contracts/v2/README.md``:

1. every violation of the standalone ``scope`` schema, and every violation
   located inside a ``scope`` object, is ``SCOPE_VIOLATION``;
2. a ``protocol_version`` const mismatch is ``UNSUPPORTED_VERSION``;
3. every other structural violation (including unparsable JSON) is
   ``INVALID_SCHEMA``.

The mapping is total and ordered, so each golden vector can declare its code
up front and the test asserts equality instead of merely "some error".
"""

from __future__ import annotations

from typing import Any, Iterable

INVALID_SCHEMA = "INVALID_SCHEMA"
UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
SCOPE_VIOLATION = "SCOPE_VIOLATION"

#: Full enum fixed by J.4 (22 codes).  Order is the order used in the appendix.
ERROR_CODES = (
    "INVALID_SCHEMA",
    "UNSUPPORTED_VERSION",
    "UNSUPPORTED_OPERATION",
    "CONTEXT_TOO_LARGE",
    "NOT_FOUND",
    "PARTIAL_HISTORY",
    "ID_CONFLICT",
    "REVISION_CONFLICT",
    "CONTEXT_STALE",
    "EVIDENCE_MISSING",
    "SOURCE_UNTRUSTED",
    "SOURCE_REDACTED",
    "SCOPE_VIOLATION",
    "REDACTED",
    "HISTORICAL_SEARCH_UNSUPPORTED",
    "NEEDS_CONTEXT",
    "INVALID_TRANSITION",
    "BUDGET_EXCEEDED",
    "EXECUTION_UNKNOWN",
    "DB_BUSY",
    "CORRUPT_STORE",
    "MIGRATION_REQUIRED",
)

ERROR_CODE_SET = frozenset(ERROR_CODES)

#: Codes this card is allowed to produce for structural failures.
STRUCTURAL_CODES = frozenset({INVALID_SCHEMA, UNSUPPORTED_VERSION, SCOPE_VIOLATION})


def classify_failures(failures: Iterable[Any], schema_name: str) -> str:
    """Map checker failures to one predeclared pcs.v2 error code."""

    collected = list(failures)
    if schema_name == "scope.json":
        return SCOPE_VIOLATION

    for failure in collected:
        path = failure.path
        if path == "/scope" or path.startswith("/scope/"):
            return SCOPE_VIOLATION

    for failure in collected:
        if failure.keyword == "const" and failure.path == "/protocol_version":
            return UNSUPPORTED_VERSION

    return INVALID_SCHEMA
