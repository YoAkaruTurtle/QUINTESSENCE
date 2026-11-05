"""Authorized keyset enumeration, separate from the frozen current query.

Each page has a read snapshot; a sequence of pages has no snapshot guarantee.
Deleting the anchor is harmless: continuation compares keys, not row existence.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from ...foundation.errors import InvalidInput
from ...foundation.identifiers import TIMESTAMP_PATTERN, UUID_PATTERN
from ...foundation.jsonutil import canonical_json_text
from ..refs import RecordRef
from .beliefs import BELIEF_KIND, parse_belief_body
from .epistemic import is_expression_preference_body
from .query import BeliefExactQuery, QueryAccess

STATUSES = ("supported", "proposed", "contested", "contradicted", "stale", "insufficient")


class CursorError(InvalidInput):
    def __init__(self, code: str):
        super().__init__(code)
        self.cursor_code = code


def _encode(keys, digest):
    raw = canonical_json_text({"v": 1, "keys": list(keys), "filters_digest": digest})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(cursor, digest):
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
            raise ValueError()
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"v", "keys", "filters_digest"}:
            raise ValueError()
        if type(data["v"]) is not int:
            raise ValueError()
        if data["v"] != 1:
            raise CursorError("cursor_version_mismatch")
        keys = data["keys"]
        if (not isinstance(keys, list) or len(keys) != 2
                or not all(isinstance(k, str) and k for k in keys)
                or not isinstance(data["filters_digest"], str)
                or TIMESTAMP_PATTERN.fullmatch(keys[0]) is None
                or UUID_PATTERN.fullmatch(keys[1]) is None):
            raise ValueError()
        if data["filters_digest"] != digest:
            raise CursorError("cursor_filters_mismatch")
        return tuple(keys)
    except CursorError:
        raise
    except (ValueError, TypeError, binascii.Error, UnicodeError) as exc:
        raise CursorError("cursor_invalid") from exc


def read_belief_page(queries, access: QueryAccess, *, status: str | None = None,
                     scope: Mapping[str, Any] | None = None, limit: int = 5,
                     cursor: str | None = None) -> dict[str, Any]:
    """Use exact-query privacy/source/applicability gates before any counting."""
    if not isinstance(access, QueryAccess) or "consult" not in access.purposes:
        raise InvalidInput("belief pagination requires consult QueryAccess")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise InvalidInput("limit must be 1..20")
    if status is not None and status not in STATUSES:
        raise InvalidInput("invalid belief status")
    scope = dict(scope or {})
    digest = hashlib.sha256(canonical_json_text({
        "namespace": access.namespace, "privacy": sorted(access.privacy_classes),
        "purposes": sorted(access.purposes), "status": status, "scope": scope,
    }).encode()).hexdigest()
    anchor = None if cursor is None else _decode(cursor, digest)
    store = queries._store
    now = queries._clock()
    visible = []
    with store.read_snapshot():
        for version in store.current_versions_of_kind(access.namespace, BELIEF_KIND):
            if version.status not in (STATUSES if status is None else (status,)):
                continue
            if version.privacy_class not in access.privacy_classes:
                continue
            if is_expression_preference_body(parse_belief_body(version)):
                # Expression preferences are not cognition; the settings and
                # expression-material paths keep reading them directly.
                continue
            query = BeliefExactQuery(RecordRef(access.namespace, version.record_id, version.record_version), scope)
            result = queries._exact_locked(access, query, now=now)
            if result.items:
                item = json.loads(result.to_body())["items"][0]
                item["status"] = version.status
                item["provisional"] = version.status == "proposed"
                visible.append(((version.recorded_at, version.record_id), item))
        visible.sort(key=lambda pair: pair[0], reverse=True)
        total = len(visible)
        remaining = [pair for pair in visible if anchor is None or pair[0] < anchor]
        page = remaining[:limit]
        truncated = len(remaining) > len(page)
        return {"query_kind": "belief_page", "items": [item for _, item in page],
                "total": total, "truncated": truncated,
                "next_cursor": _encode(page[-1][0], digest) if truncated else None,
                "consistency": "per_page_snapshot_only"}
