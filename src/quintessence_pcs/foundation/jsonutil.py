"""Canonical JSON and hashing for private payloads.

Body storage rules used by PCS2-004 and extended by PCS2-006:

* the body is a JSON object;
* it is stored as canonical UTF-8 JSON text: ``sort_keys=True``,
  ``ensure_ascii=False`` and the compact separators used by the PCS2-002
  contract tests;
* ``NaN``/``Infinity`` are rejected on write (``allow_nan=False``) and on read
  (strict loader), because Python's ``json`` module accepts them by default
  (cross-card constraint 8);
* the digest of those exact bytes is stored in ``private_payloads.sha256`` and
  is re-verified on read, so tampering is detected instead of returned;
* from PCS2-006 on, the payload reference is an **opaque random ID** rather
  than the digest (handoff card B1).  Immutable ``record_versions`` /
  ``event_journal`` rows keep only that ID, so deleting a payload leaves no
  content hash pinned on a row that cannot be rewritten.  De-duplication now
  looks the digest up in ``private_payloads`` instead of deriving a reference
  from it.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Any

from .errors import InvalidInput

#: Prefix of an opaque payload reference (``payload:<uuid>``).
PAYLOAD_REF_PREFIX = "payload:"


def canonical_json_text(value: Any) -> str:
    """Serialize *value* to the byte-exact form stored in the database."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidInput(f"value is not canonical JSON: {exc}") from exc


def sha256_hex(text: str) -> str:
    """SHA-256 of the UTF-8 encoding of *text*."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_payload_ref() -> str:
    """Return a fresh opaque payload reference (PCS2-006 B1)."""

    return f"{PAYLOAD_REF_PREFIX}{uuid.uuid4()}"


def _reject_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant {token!r}")


def _assert_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, dict):
        for child in value.values():
            _assert_finite(child)
    elif isinstance(value, list):
        for child in value:
            _assert_finite(child)


def strict_json_loads(text: str) -> Any:
    """Parse JSON while rejecting NaN, Infinity and overflowed numbers."""

    value = json.loads(text, parse_constant=_reject_constant)
    _assert_finite(value)
    return value


__all__ = [
    "PAYLOAD_REF_PREFIX",
    "canonical_json_text",
    "new_payload_ref",
    "sha256_hex",
    "strict_json_loads",
]
