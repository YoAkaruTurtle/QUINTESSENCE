"""Read-only Scope Recall browse/search mapping (RG08).

The adapter speaks a small JSON envelope; this module turns that envelope into
typed PCS values and refuses malformed or over-wide answers.  It never imports
the upstream package, reads SQLite, calls a model or writes anything.  The host
owns the binding and the single configured scope; this layer only forwards a
query, a bound and an opaque continuation cursor.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Any, Mapping

DEFAULT_LIMIT = 10
MAX_LIMIT = 20
MAX_QUERY_CHARS = 256
MAX_CURSOR_CHARS = 2048
MAX_ITEMS = 20
MAX_CONTENT_CHARS = 2000
MAX_REF_CHARS = 320
MAX_LABEL_CHARS = 160
MAX_TIME_CHARS = 48

_ITEM_KINDS = frozenset({"event", "claim", "episode", "artifact", "reference"})
_FAILED = "memory recall did not complete"


class MemorySearchError(Exception):
    """A structured, content-free refusal of one memory search."""

    code = "MEMORY_SEARCH_FAILED"

    def __init__(
        self,
        message: str = _FAILED,
        *,
        detail: str | None = None,
        reason: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.detail = detail
        #: J10 W5: the adapter's own short error token, kept apart from the
        #: message so a diagnosis never has to parse prose.  Never free text.
        self.reason = reason


class MemorySearchForbidden(MemorySearchError):
    code = "MEMORY_FORBIDDEN"


class MemorySearchUnavailable(MemorySearchError):
    code = "MEMORY_UNAVAILABLE"


class MemorySearchCursorStale(MemorySearchError):
    code = "MEMORY_CURSOR_STALE"


class MemorySearchFailed(MemorySearchError):
    code = "MEMORY_SEARCH_FAILED"


@dataclass(frozen=True)
class MemorySource:
    """One declared source behind a memory item (metadata only)."""

    ref: str
    revision: int
    entry_id: str
    agent: str
    session: str
    occurred_at: str | None
    recorded_at: str | None


@dataclass(frozen=True)
class MemoryItem:
    """One current memory object, with its scope, state and citation.

    ``subject`` is attached by the host after this page is read: the upstream
    inventory names an event by its own opaque id and carries no speaker, so
    "who said this" comes from the PCS delivery row that produced it.  An empty
    subject means the host could not classify the speaker; it is never guessed.
    """

    kind: str
    ref: str
    revision: int
    scope_id: str
    state: str
    content: str
    content_truncated: bool
    sources: tuple[MemorySource, ...]
    subject: str = ""
    subject_ref: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "revision": self.revision,
            "scope_id": self.scope_id,
            "state": self.state,
            "subject": self.subject,
            "subject_ref": self.subject_ref,
            "content": self.content,
            "content_truncated": self.content_truncated,
            "sources": [
                {
                    "ref": source.ref,
                    "revision": source.revision,
                    "entry_id": source.entry_id,
                    "agent": source.agent,
                    "session": source.session,
                    "occurred_at": source.occurred_at,
                    "recorded_at": source.recorded_at,
                }
                for source in self.sources
            ],
        }


@dataclass(frozen=True)
class MemorySearchPage:
    items: tuple[MemoryItem, ...]
    examined: int
    scan_complete: bool
    stop_reason: str
    next_cursor: str | None
    memory_epoch: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [item.as_dict() for item in self.items],
            "examined": self.examined,
            "scan_complete": self.scan_complete,
            "stop_reason": self.stop_reason,
            "next_cursor": self.next_cursor,
            "memory_epoch": self.memory_epoch,
        }

    def with_subjects(
        self, subjects: Mapping[str, tuple[str, str]]
    ) -> "MemorySearchPage":
        """Label every item with ``(subject, subject_ref)`` from its event ref.

        A hit with no known origin keeps an empty subject: an unattributed
        memory item must stay visibly unattributed instead of silently becoming
        the user's own statement.
        """

        if not subjects:
            return self
        labelled = []
        for item in self.items:
            subject, subject_ref = subjects.get(item.ref, ("", ""))
            if subject:
                labelled.append(
                    replace(item, subject=subject, subject_ref=subject_ref)
                )
            else:
                labelled.append(item)
        return replace(self, items=tuple(labelled))


def search_scope_recall(
    adapter: Any,
    *,
    query: str,
    scope_id: str,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    timeout_seconds: float = 4.0,
    guard: Callable[[], None] | None = None,
) -> MemorySearchPage:
    """Run one bounded read through the adapter and validate its answer."""

    if guard is not None:
        guard()
    _require_query(query)
    _require_limit(limit)
    _require_cursor(cursor)
    if not isinstance(scope_id, str) or not scope_id or len(scope_id) > MAX_LABEL_CHARS:
        raise MemorySearchFailed(detail="scope")
    if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
        raise MemorySearchFailed(detail="timeout")

    try:
        outcome = adapter.search(
            query=query,
            limit=limit,
            cursor=cursor,
            timeout_seconds=float(timeout_seconds),
        )
    except Exception as exc:  # noqa: BLE001 - adapter failure is a state, not a crash
        raise MemorySearchFailed(detail=type(exc).__name__) from exc
    if not isinstance(outcome, Mapping):
        raise MemorySearchFailed(detail="response")
    kind = outcome.get("kind")
    if kind == "ok":
        return _page(outcome.get("result"))
    if kind == "blocked":
        if outcome.get("error") == "adapter_unavailable":
            raise MemorySearchUnavailable(
                "the memory adapter is not available", reason="adapter_unavailable"
            )
        raise MemorySearchForbidden(
            "memory recall is not permitted for this host",
            reason="blocked",
        )
    if kind == "conflict":
        raise MemorySearchCursorStale(
            "the memory search cursor is stale; repeat the search from the start",
            reason="conflict",
        )
    if kind in ("unavailable", "unknown"):
        # W5: keep the adapter's own token.  ``unknown`` with ``timeout`` is a
        # timeout; everything else stays an unavailable recall with its code.
        token = str(outcome.get("error") or "").strip()[:64]
        reason = token or ("timeout" if kind == "unknown" else "unavailable")
        raise MemorySearchUnavailable(
            "memory recall is currently unavailable", reason=reason
        )
    raise MemorySearchFailed(detail="kind")


def _require_query(query: Any) -> None:
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        raise MemorySearchFailed(detail="query")


def _require_limit(limit: Any) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise MemorySearchFailed(detail="limit")


def _require_cursor(cursor: Any) -> None:
    if cursor is None:
        return
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= MAX_CURSOR_CHARS:
        raise MemorySearchFailed(detail="cursor")


def _text(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _integer(value: Any) -> int:
    return value if type(value) is int else 0


def _source(value: Any) -> MemorySource | None:
    if not isinstance(value, Mapping):
        return None
    return MemorySource(
        ref=_text(value.get("ref"), MAX_REF_CHARS),
        revision=_integer(value.get("revision")),
        entry_id=_text(value.get("entry_id"), MAX_LABEL_CHARS),
        agent=_text(value.get("agent"), MAX_LABEL_CHARS),
        session=_text(value.get("session"), MAX_LABEL_CHARS),
        occurred_at=_text(value.get("occurred_at"), MAX_TIME_CHARS) or None,
        recorded_at=_text(value.get("recorded_at"), MAX_TIME_CHARS) or None,
    )


def _item(value: Any) -> MemoryItem | None:
    if not isinstance(value, Mapping):
        return None
    kind = _text(value.get("kind"), 32)
    if kind not in _ITEM_KINDS:
        return None
    content = value.get("content")
    content = content if isinstance(content, str) else ""
    sources = []
    raw_sources = value.get("sources")
    if isinstance(raw_sources, (list, tuple)):
        for raw in raw_sources[:8]:
            source = _source(raw)
            if source is not None:
                sources.append(source)
    return MemoryItem(
        kind=kind,
        ref=_text(value.get("ref"), MAX_REF_CHARS),
        revision=_integer(value.get("revision")),
        scope_id=_text(value.get("scope_id"), MAX_LABEL_CHARS),
        state=_text(value.get("state"), 48),
        content=content[:MAX_CONTENT_CHARS],
        content_truncated=value.get("content_truncated") is True
        or len(content) > MAX_CONTENT_CHARS,
        sources=tuple(sources),
    )


def _page(value: Any) -> MemorySearchPage:
    if not isinstance(value, Mapping) or not isinstance(value.get("items"), (list, tuple)):
        raise MemorySearchFailed(detail="result")
    raw_items = value["items"]
    if len(raw_items) > MAX_ITEMS:
        raise MemorySearchFailed(detail="items")
    items = tuple(item for item in (_item(raw) for raw in raw_items) if item is not None)
    cursor = value.get("next_cursor")
    if cursor is not None and (
        not isinstance(cursor, str) or not 1 <= len(cursor) <= MAX_CURSOR_CHARS
    ):
        raise MemorySearchFailed(detail="cursor")
    return MemorySearchPage(
        items=items,
        examined=_integer(value.get("examined")),
        scan_complete=value.get("scan_complete") is True,
        stop_reason=_text(value.get("stop_reason"), 48),
        next_cursor=cursor,
        memory_epoch=_integer(value.get("memory_epoch")),
    )


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_CONTENT_CHARS",
    "MAX_CURSOR_CHARS",
    "MAX_ITEMS",
    "MAX_LIMIT",
    "MAX_QUERY_CHARS",
    "MemoryItem",
    "MemorySearchCursorStale",
    "MemorySearchError",
    "MemorySearchFailed",
    "MemorySearchForbidden",
    "MemorySearchPage",
    "MemorySearchUnavailable",
    "MemorySource",
    "search_scope_recall",
]
