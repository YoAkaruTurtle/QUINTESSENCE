"""RG04: the bounded transcript-archive read surface.

This is **not** a second memory system and not a semantic index.  It reads the
public transcript that RG02/RG03 already persist:

* v2 turns: ``collab_turn_messages`` (published only) with the exact input or
  reply artifact behind each message;
* legacy single-turn consult interactions: their linked inputs and their
  ``reply`` artifacts.  Those rows predate 0017 and have no conversation/turn
  provenance, so they are returned as read-only "legacy" items instead of
  inventing an ownership relation.

Every returned text is loaded through the existing ``load_input`` /
``load_artifact`` path, so the deletion barrier and digest checks run.  Privacy
is decided per row with the same closure as ``read_experience``; retention is
filtered against the current host retention so a shorter-lived message is not
copied into a longer-lived artifact by a model turn.  There is no vector search
and no dependency on Scope Recall: an unavailable index cannot hide a just-made
correction.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

from ..foundation import identifiers
from ..foundation.errors import CorruptStore, InvalidInput, Redacted, RecordNotFound
from ..foundation.jsonutil import canonical_json_text
from ..reply_bubbles import decode_reply_body
from .collaboration import RETENTION_RANK

DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 10
MAX_SEARCH_QUERY = 256
SEARCH_SCAN_WINDOW = 200
SNIPPET_CONTEXT = 60
SNIPPET_TAIL = 220
SNIPPET_MAX = 320
FULL_TEXT_MAX = 1500
_SEARCH_SOURCES = ("turn", "legacy")


@dataclass(frozen=True)
class TranscriptSearchPage:
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None
    scanned: int
    scan_window: int
    filtered: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [dict(item) for item in self.items],
            "next_cursor": self.next_cursor,
            "scanned": self.scanned,
            "scan_window": self.scan_window,
            "filtered": self.filtered,
        }


class TranscriptMixin:
    """Bounded search over the public transcript archive."""

    def search_transcript(
        self,
        *,
        namespace: str,
        host_instance: str,
        access: Any,
        query: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        before: str | None = None,
        conversation_id: str | None = None,
        include_full: bool = False,
        minimum_retention: str = "standard",
    ) -> TranscriptSearchPage:
        ns = identifiers.require_namespace(namespace)
        host = _require_text(host_instance, "host_instance", 256)
        if access is None or not hasattr(access, "privacy_classes"):
            raise InvalidInput("a transcript access object is required")
        if ns != access.namespace:
            raise InvalidInput("the access namespace does not match the request")
        text = _require_query(query)
        if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise InvalidInput(f"limit must be an integer between 1 and {MAX_SEARCH_LIMIT}")
        if type(include_full) is not bool:
            raise InvalidInput("include_full must be a boolean")
        if minimum_retention not in RETENTION_RANK:
            raise InvalidInput("minimum_retention is not a frozen value")
        cid = None if conversation_id is None else _require_uuid(
            conversation_id, "conversation_id"
        )
        cursor = None if before is None else _decode_search_cursor(before)

        parameters: list[Any] = [ns, host]
        turn_where = (
            "m.namespace = ? AND c.host_instance = ? AND m.status = 'published'"
        )
        if cid is not None:
            turn_where += " AND t.conversation_id = ?"
            parameters.append(cid)
        base_sql = (
            "SELECT 'turn' AS source, m.created_at AS created_at, "
            "m.message_id AS row_id, t.conversation_id AS conversation_id, "
            "t.turn_no AS turn_no, m.message_id AS message_id, m.kind AS kind, "
            "CASE WHEN m.kind = 'user' THEN 'user' ELSE 'assistant' END AS role, "
            "CASE WHEN m.kind = 'user' "
            "THEN json_extract(m.input_ref_json, '$.input_id') "
            "ELSE m.artifact_id END AS ref_id, "
            "CASE WHEN m.kind = 'user' "
            "THEN json_extract(m.input_ref_json, '$.content_version') "
            "ELSE m.artifact_version END AS ref_version, "
            "NULL AS legacy_interaction_id "
            "FROM collab_turn_messages AS m "
            "JOIN collab_turns AS t ON t.namespace = m.namespace "
            "AND t.turn_id = m.turn_id "
            "JOIN collab_conversations AS c ON c.namespace = t.namespace "
            "AND c.conversation_id = t.conversation_id "
            "WHERE " + turn_where + " "
            "UNION ALL "
            "SELECT 'legacy' AS source, i.recorded_at AS created_at, "
            "i.input_id AS row_id, NULL AS conversation_id, NULL AS turn_no, "
            "NULL AS message_id, 'user' AS kind, 'user' AS role, "
            "i.input_id AS ref_id, i.content_version AS ref_version, "
            "it.interaction_id AS legacy_interaction_id "
            "FROM collab_inputs AS i "
            "JOIN collab_interaction_inputs AS l ON l.namespace = i.namespace "
            "AND l.input_id = i.input_id AND l.content_version = i.content_version "
            "JOIN collab_interactions AS it ON it.namespace = l.namespace "
            "AND it.interaction_id = l.interaction_id "
            "WHERE it.mode = 'consult' AND it.host_instance = ? "
            "AND i.namespace = ? AND NOT EXISTS (SELECT 1 FROM "
            "collab_stage_contexts AS sc WHERE sc.namespace = it.namespace "
            "AND sc.context_id = it.context_id) "
            "UNION ALL "
            "SELECT 'legacy' AS source, a.created_at AS created_at, "
            "a.artifact_id AS row_id, NULL AS conversation_id, NULL AS turn_no, "
            "NULL AS message_id, 'assistant' AS kind, 'assistant' AS role, "
            "a.artifact_id AS ref_id, a.artifact_version AS ref_version, "
            "it.interaction_id AS legacy_interaction_id "
            "FROM collab_artifacts AS a "
            "JOIN collab_interactions AS it ON it.namespace = a.namespace "
            "AND it.interaction_id = a.interaction_id "
            "WHERE a.kind = 'reply' AND a.origin_kind = 'model_output' "
            "AND it.mode = 'consult' AND it.host_instance = ? "
            "AND NOT EXISTS (SELECT 1 FROM collab_stage_contexts AS sc "
            "WHERE sc.namespace = it.namespace AND sc.context_id = it.context_id)"
        )
        parameters.extend([host, ns, host])
        outer = "SELECT * FROM (" + base_sql + ") "
        if cursor is not None:
            outer += (
                "WHERE (created_at, source, row_id) < (?, ?, ?) "
            )
            parameters.extend(
                [cursor["created_at"], cursor["source"], cursor["row_id"]]
            )
        outer += "ORDER BY created_at DESC, source DESC, row_id DESC LIMIT ?"
        parameters.append(SEARCH_SCAN_WINDOW + 1)
        rows = self._require_connection().execute(outer, tuple(parameters)).fetchall()
        has_more = len(rows) > SEARCH_SCAN_WINDOW
        window = rows[:SEARCH_SCAN_WINDOW]

        items: list[dict[str, Any]] = []
        scanned = 0
        filtered = 0
        last_scanned: Any = None
        for row in window:
            scanned += 1
            last_scanned = row
            item = self._search_row_visible(
                ns=ns,
                host=host,
                access=access,
                row=row,
                query_lower=text.casefold(),
                include_full=include_full,
                minimum_retention=minimum_retention,
            )
            if item is None:
                filtered += 1
                continue
            items.append(item)
            if len(items) >= limit:
                break
        next_cursor = None
        if last_scanned is not None and (has_more or len(items) >= limit):
            next_cursor = _encode_search_cursor(
                str(last_scanned["created_at"]),
                str(last_scanned["source"]),
                str(last_scanned["row_id"]),
            )
        return TranscriptSearchPage(
            items=tuple(items),
            next_cursor=next_cursor,
            scanned=scanned,
            scan_window=SEARCH_SCAN_WINDOW,
            filtered=filtered,
        )

    def _search_row_visible(
        self,
        *,
        ns: str,
        host: str,
        access: Any,
        row: Any,
        query_lower: str,
        include_full: bool,
        minimum_retention: str,
    ) -> dict[str, Any] | None:
        try:
            if row["source"] == "turn":
                raw = self._require_connection().execute(
                    "SELECT * FROM collab_turn_messages WHERE namespace = ? "
                    "AND message_id = ?",
                    (ns, str(row["message_id"])),
                ).fetchone()
                if raw is None:
                    return None
                view = self._message_view_locked(ns, raw, access)
                if view.redacted or view.text is None:
                    return None
                message = self._message_row_locked(ns, str(row["message_id"]))
                retention = self._message_retention(ns, message)
                if retention is None:
                    return None
                if RETENTION_RANK[retention] < RETENTION_RANK[minimum_retention]:
                    return None
                text_value = view.text
                if message.kind == "user":
                    ref = {
                        "ref_type": "collab_input",
                        "namespace": ns,
                        "input_id": str(row["ref_id"]),
                        "content_version": int(row["ref_version"]),
                    }
                    role = "user"
                else:
                    ref = {
                        "ref_type": "collab_artifact",
                        "namespace": ns,
                        "artifact_id": str(row["ref_id"]),
                        "artifact_version": int(row["ref_version"]),
                    }
                    role = "assistant"
                conversation_id = str(row["conversation_id"])
                turn_no = int(row["turn_no"])
                message_id: str | None = str(message.message_id)
                legacy_interaction_id = None
                kind = str(message.kind)
            else:
                retention = None
                if row["role"] == "user":
                    loaded = self.load_input(
                        namespace=ns,
                        input_id=str(row["ref_id"]),
                        content_version=int(row["ref_version"]),
                    )
                    if loaded.privacy_class not in set(access.privacy_classes):
                        return None
                    text_value = loaded.body
                    retention = str(loaded.retention)
                    ref = {
                        "ref_type": "collab_input",
                        "namespace": ns,
                        "input_id": loaded.input_id,
                        "content_version": loaded.content_version,
                    }
                    role = "user"
                    kind = "user"
                else:
                    loaded = self.load_artifact(
                        namespace=ns,
                        artifact_id=str(row["ref_id"]),
                        artifact_version=int(row["ref_version"]),
                    )
                    privacy = self._artifact_privacy_classes(
                        namespace=ns,
                        artifact_id=loaded.artifact_id,
                        artifact_version=loaded.artifact_version,
                        depth=0,
                        seen=set(),
                    )
                    if privacy is None or not privacy.issubset(
                        set(access.privacy_classes)
                    ):
                        return None
                    _, text_value = decode_reply_body(loaded.body)
                    retention = str(loaded.retention)
                    ref = {
                        "ref_type": "collab_artifact",
                        "namespace": ns,
                        "artifact_id": loaded.artifact_id,
                        "artifact_version": loaded.artifact_version,
                    }
                    role = "assistant"
                    kind = "assistant"
                if retention is None:
                    return None
                if RETENTION_RANK[retention] < RETENTION_RANK[minimum_retention]:
                    return None
                conversation_id = None
                turn_no = None
                message_id = None
                legacy_interaction_id = (
                    None
                    if row["legacy_interaction_id"] is None
                    else str(row["legacy_interaction_id"])
                )
        except (Redacted, RecordNotFound, CorruptStore, InvalidInput):
            return None
        if query_lower not in text_value.casefold():
            return None
        snippet, truncated = _snippet(text_value, query_lower, include_full)
        return {
            "source": str(row["source"]),
            "created_at": str(row["created_at"]),
            "conversation_id": conversation_id,
            "turn_no": turn_no,
            "message_id": message_id,
            "legacy_interaction_id": legacy_interaction_id,
            "role": role,
            "kind": kind,
            "text": snippet,
            "truncated": truncated,
            "ref": ref,
        }

    def _message_retention(self, ns: str, message: Any) -> str | None:
        if message.kind == "user":
            row = self._require_connection().execute(
                "SELECT retention_policy FROM collab_inputs WHERE namespace = ? "
                "AND input_id = ? AND content_version = ?",
                (
                    ns,
                    str((message.input_ref or {}).get("input_id")),
                    int((message.input_ref or {}).get("content_version") or 0),
                ),
            ).fetchone()
        else:
            row = self._require_connection().execute(
                "SELECT retention_policy FROM collab_artifacts WHERE namespace = ? "
                "AND artifact_id = ? AND artifact_version = ?",
                (ns, message.artifact_id, message.artifact_version),
            ).fetchone()
        return None if row is None else str(row["retention_policy"])


def _require_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise InvalidInput(f"{field} is too long")
    return value


def _require_uuid(value: Any, field: str) -> str:
    text = _require_text(value, field, 36)
    if identifiers.UUID_PATTERN.fullmatch(text) is None:
        raise InvalidInput(f"{field} must be a canonical UUID")
    return text


def _require_query(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidInput("query must be a string")
    text = " ".join(value.split())
    if not text or len(text) > MAX_SEARCH_QUERY:
        raise InvalidInput(
            f"query must be 1..{MAX_SEARCH_QUERY} characters after whitespace folding"
        )
    return text


def _snippet(text: str, query_lower: str, include_full: bool) -> tuple[str, bool]:
    if include_full:
        if len(text) <= FULL_TEXT_MAX:
            return text, False
        return text[:FULL_TEXT_MAX], True
    index = text.casefold().find(query_lower)
    if index < 0:  # caller matched, so this only guards a case-fold oddity
        index = 0
    start = max(0, index - SNIPPET_CONTEXT)
    end = min(len(text), index + len(query_lower) + SNIPPET_TAIL)
    body = text[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    snippet = prefix + body + suffix
    if len(snippet) <= SNIPPET_MAX:
        return snippet, bool(prefix or suffix)
    keep = SNIPPET_MAX - len(prefix) - len(suffix)
    return prefix + body[: max(keep, 0)] + suffix, True


def _encode_search_cursor(created_at: str, source: str, row_id: str) -> str:
    value = canonical_json_text([created_at, source, row_id])
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def _decode_search_cursor(value: Any) -> dict[str, str]:
    if not isinstance(value, str) or not value:
        raise InvalidInput("before is not a valid transcript cursor")
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
        raise InvalidInput("before is not a valid transcript cursor") from None
    if (
        not isinstance(document, list)
        or len(document) != 3
        or not all(isinstance(item, str) and item for item in document)
        or document[1] not in _SEARCH_SOURCES
        or identifiers.TIMESTAMP_PATTERN.fullmatch(document[0]) is None
    ):
        raise InvalidInput("before is not a valid transcript cursor")
    return {"created_at": document[0], "source": document[1], "row_id": document[2]}


__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "MAX_SEARCH_LIMIT",
    "MAX_SEARCH_QUERY",
    "SEARCH_SCAN_WINDOW",
    "TranscriptMixin",
    "TranscriptSearchPage",
]
