"""Minimal Scope Recall adapter CLI (JSON stdin/stdout, no HTTP).

Three frozen actions share one entry point so the host needs no second pinned
path:

``capture``
    The bridge sends one capture request on stdin; this process imports the
    pinned upstream package from the configured interpreter/PYTHONPATH, opens
    its synthetic instance and calls the public ``record_event`` API.  Body
    text only travels on stdin and is never echoed to stdout/stderr or argv.

``search``
    A read-only browse/search through the upstream local-owner management API
    (``MemoryManagement.list`` + ``MemoryFilter``).  It never calls
    ``initialize``, never writes, never calls a model and never touches SQLite
    directly.  Labels are bounded and content is clipped before it leaves the
    subprocess.

``recall``
    A read-only automatic recall through the upstream public
    ``MemoryCore.recall_packet`` API.  The host sends the current user input as
    one bounded request; the result is a validated, clipped packet.  No
    ``initialize``, no write, no model, no direct SQLite access.

The adapter is deliberately unaware of PCS: the host owns binding, scope and
timeouts, and the request is the only channel.
"""

from __future__ import annotations

import json
import re
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

MAX_STDIN_BYTES = 8 * 1024 * 1024
MAX_STDOUT_BYTES = 1024 * 1024

#: Search answers stay small enough for one desktop frame.  Content is clipped
#: with an explicit flag instead of being silently shortened.
MAX_SEARCH_ITEMS = 20
MAX_SEARCH_CONTENT_CHARS = 480
MAX_SEARCH_SOURCES = 8
MAX_SEARCH_QUERY_CHARS = 512
MAX_SEARCH_CURSOR_CHARS = 2048
MAX_REF_CHARS = 320
MAX_LABEL_CHARS = 160
MAX_TIME_CHARS = 48

#: RG08 R-B: automatic recall is one bounded, read-only packet.  The host
#: approves 4 items / 2048 units / at most two seconds for the whole turn.
MAX_RECALL_ITEMS = 4
MAX_RECALL_BUDGET_UNITS = 2048
MAX_RECALL_QUERY_CHARS = 8192
MAX_RECALL_CONTENT_CHARS = 640
MAX_RECALL_GAPS = 8
MAX_RECALL_GAP_CHARS = 80
MAX_RECALL_NEEDS = 8
MAX_RECALL_EVIDENCE_REFS = 8
MAX_RECALL_ORIGIN_CHARS = 80
MAX_RECALL_APPLICABILITY_CHARS = 256
MAX_RECALL_BASIS_CHARS = 32

#: RG08 R-C1: a bounded inventory page over the public time filter.  ``limit``
#: is the upstream match limit per page, not the delivery bound; the host keeps
#: its own final 4-item bound and its own event-time post-filter.
MAX_TIME_WINDOW_ITEMS = 20
MAX_TIME_WINDOW_PAGES = 3
MAX_TIME_WINDOW_TOTAL = 60

_RECALL_ITEMS = ("event", "claim", "episode", "procedure", "artifact")
_RECALL_STATUS = ("ok", "no_match", "partial", "unavailable")
_RECALL_COVERAGE = ("complete_for_query", "partial", "unknown")
_RECALL_ANSWERABILITY = ("supported", "partial", "ambiguous", "unknown")
_RECALL_TEMPORAL = ("current", "historical", "disputed", "unknown")
_RECALL_GAP = re.compile(r"[a-z0-9_:.-]{1,%d}" % MAX_RECALL_GAP_CHARS)

_ACTIONS = ("capture", "search", "recall", "time_window")


def _fail(kind: str, error: str) -> int:
    sys.stdout.write(json.dumps({"kind": kind, "error": error}, ensure_ascii=False))
    return 0


def _contract_failure(exc: BaseException) -> int:
    """Map an upstream contract error without echoing content or paths."""

    code = str(getattr(exc, "code", "CONTRACT_ERROR"))
    if code == "VERSION_CONFLICT":
        return _fail("conflict", code)
    if code in ("ACCESS_DENIED", "IDENTITY_UNBOUND"):
        return _fail("blocked", code)
    if code in ("STORAGE_UNAVAILABLE", "SOURCE_MISSING"):
        return _fail("unavailable", code)
    return _fail("error", code)


def _binding(request: dict):
    from scope_recall.contracts import InstanceBinding

    spec = request["binding"]
    return InstanceBinding(
        spec["agent_id"],
        spec["installation_id"],
        Path(spec["data_directory"]),
        frozenset(spec["scope_ids"]),
        spec.get("test_mode", True),
    )


def _capture(request: dict) -> int:
    from scope_recall.contracts import ContractError, TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore

    binding = _binding(request)
    context_spec = request["context"]
    try:
        core = MemoryCore(
            CoreConfig(binding, write_timeout_seconds=float(request["timeout_seconds"]))
        )
        core.initialize()
        context = TrustedContext(
            binding,
            context_spec["session_id"],
            frozenset([request["scope_id"]]),
            context_spec["actor_origin"],
        )
        receipt = core.record_event(
            context,
            request["event"],
            scope_id=request["scope_id"],
            remaining_seconds=float(request["timeout_seconds"]),
        )
    except ContractError as exc:
        return _contract_failure(exc)
    except Exception as exc:  # noqa: BLE001 - return a fixed class, never content
        return _fail("error", type(exc).__name__)

    text = json.dumps({"kind": "ok", "receipt": asdict(receipt)}, ensure_ascii=False)
    if len(text.encode("utf-8")) > MAX_STDOUT_BYTES:
        return _fail("error", "response_too_large")
    sys.stdout.write(text)
    return 0


def _text(value: object, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _integer(value: object) -> int:
    return value if type(value) is int else 0


def _source_view(source: object) -> dict | None:
    if not isinstance(source, dict):
        return None
    return {
        "ref": _text(source.get("ref"), MAX_REF_CHARS),
        "revision": _integer(source.get("revision")),
        "entry_id": _text(source.get("entry_id"), MAX_LABEL_CHARS),
        "agent": _text(source.get("agent"), MAX_LABEL_CHARS),
        "session": _text(source.get("session"), MAX_LABEL_CHARS),
        "occurred_at": _text(source.get("occurred_at"), MAX_TIME_CHARS) or None,
        "recorded_at": _text(source.get("recorded_at"), MAX_TIME_CHARS) or None,
    }


def _page_view(page: object) -> dict:
    """Validate and bound the upstream inventory page before it leaves here."""

    if not isinstance(page, dict) or not isinstance(page.get("items"), (list, tuple)):
        raise ValueError("page")
    items = []
    for raw in page["items"][:MAX_SEARCH_ITEMS]:
        if not isinstance(raw, dict):
            continue
        content = raw.get("content")
        content = content if isinstance(content, str) else ""
        clipped = content[:MAX_SEARCH_CONTENT_CHARS]
        sources = []
        raw_sources = raw.get("sources")
        if isinstance(raw_sources, (list, tuple)):
            for source in raw_sources[:MAX_SEARCH_SOURCES]:
                view = _source_view(source)
                if view is not None:
                    sources.append(view)
        items.append(
            {
                "kind": _text(raw.get("kind"), 32),
                "ref": _text(raw.get("ref"), MAX_REF_CHARS),
                "revision": _integer(raw.get("revision")),
                "scope_id": _text(raw.get("scope_id"), MAX_LABEL_CHARS),
                "state": _text(raw.get("state"), 48),
                "content": clipped,
                "content_truncated": len(content) > MAX_SEARCH_CONTENT_CHARS,
                "sources": sources,
            }
        )
    cursor = page.get("next_cursor")
    return {
        "items": items,
        "examined": _integer(page.get("examined")),
        "scan_complete": page.get("scan_complete") is True,
        "stop_reason": _text(page.get("stop_reason"), 48),
        "next_cursor": cursor if isinstance(cursor, str) else None,
        "memory_epoch": _integer(page.get("memory_epoch")),
    }


def _search(request: dict) -> int:
    from scope_recall.contracts import ContractError, TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.management_query import MemoryFilter
    from scope_recall.core.restore import InstallationMaintenance

    binding = _binding(request)
    query = request.get("query", "")
    limit = request.get("limit", 10)
    cursor = request.get("cursor")
    scope_id = request.get("scope_id")
    timeout_seconds = request.get("timeout_seconds")
    if (
        not isinstance(query, str)
        or len(query) > MAX_SEARCH_QUERY_CHARS
        or type(limit) is not int
        or not 1 <= limit <= MAX_SEARCH_ITEMS
        or (
            cursor is not None
            and (
                not isinstance(cursor, str)
                or not 1 <= len(cursor) <= MAX_SEARCH_CURSOR_CHARS
            )
        )
        or (
            scope_id is not None
            and (not isinstance(scope_id, str) or scope_id not in binding.scope_ids)
        )
        or type(timeout_seconds) not in (int, float)
        or timeout_seconds <= 0
    ):
        return _fail("error", "invalid_request")

    data_directory = Path(request["binding"]["data_directory"])
    if not (data_directory / "memory.sqlite3").is_file():
        return _fail("unavailable", "SOURCE_MISSING")

    try:
        core = MemoryCore(
            CoreConfig(binding, write_timeout_seconds=float(timeout_seconds))
        )
        context = TrustedContext(
            binding,
            "local-management",
            binding.scope_ids,
            "host_generated",
        )
        management = MemoryManagement(core, InstallationMaintenance(context))
        page = management.list(
            MemoryFilter(query=query, scope=scope_id),
            limit=limit,
            cursor=cursor,
        )
        result = _page_view(page)
    except ContractError as exc:
        return _contract_failure(exc)
    except (ValueError, TypeError):
        return _fail("error", "invalid_result")
    except Exception as exc:  # noqa: BLE001 - return a fixed class, never content
        return _fail("error", type(exc).__name__)

    text = json.dumps({"kind": "ok", "result": result}, ensure_ascii=False)
    if len(text.encode("utf-8")) > MAX_STDOUT_BYTES:
        return _fail("error", "response_too_large")
    sys.stdout.write(text)
    return 0


def _recall_gap(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    code = value[:MAX_RECALL_GAP_CHARS]
    return code if _RECALL_GAP.fullmatch(code) else None


def _recall_item(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    ref = _text(raw.get("ref"), MAX_REF_CHARS)
    revision = _integer(raw.get("revision"))
    kind = _text(raw.get("kind"), 32)
    if not ref or revision < 1 or kind not in _RECALL_ITEMS:
        raise ValueError("item")
    content = raw.get("content")
    content = content if isinstance(content, str) else ""
    sources = []
    raw_sources = raw.get("sources")
    if isinstance(raw_sources, (list, tuple)):
        for source in raw_sources[:MAX_SEARCH_SOURCES]:
            view = _source_view(source)
            if view is not None:
                sources.append(view)
    temporal = _text(raw.get("temporal_status"), 32)
    if temporal not in _RECALL_TEMPORAL:
        temporal = "unknown"
    evidence = []
    raw_evidence = raw.get("evidence_refs")
    if isinstance(raw_evidence, (list, tuple)):
        for value in raw_evidence[:MAX_RECALL_EVIDENCE_REFS]:
            if isinstance(value, str) and value:
                evidence.append(value[:MAX_REF_CHARS])
    occurred_at = raw.get("occurred_at")
    return {
        "ref": ref,
        "revision": revision,
        "kind": kind,
        "content": content[:MAX_RECALL_CONTENT_CHARS],
        "content_truncated": bool(raw.get("content_truncated"))
        or len(content) > MAX_RECALL_CONTENT_CHARS,
        "temporal_status": temporal,
        "origin": _text(raw.get("origin"), MAX_RECALL_ORIGIN_CHARS),
        "applicability": _text(raw.get("applicability"), MAX_RECALL_APPLICABILITY_CHARS),
        "evidence_refs": evidence,
        "expandable": raw.get("expandable") is True,
        "basis": _text(raw.get("basis"), MAX_RECALL_BASIS_CHARS),
        "occurred_at": _text(occurred_at, MAX_TIME_CHARS) or None,
        "sources": sources,
    }


def _recall_view(packet: object, max_items: int) -> dict:
    """Validate and bound the upstream RecallPacket before it leaves here."""

    if not isinstance(packet, dict):
        raise ValueError("packet")
    status = packet.get("status")
    if status not in _RECALL_STATUS:
        raise ValueError("status")
    raw_items = packet.get("items")
    if not isinstance(raw_items, (list, tuple)):
        raise ValueError("items")
    if len(raw_items) > max_items:
        raise ValueError("items_over_limit")
    items = []
    for raw in raw_items:
        view = _recall_item(raw)
        if view is not None:
            items.append(view)
    gaps = []
    raw_gaps = packet.get("gaps")
    if isinstance(raw_gaps, (list, tuple)):
        for value in raw_gaps[:MAX_RECALL_GAPS]:
            code = _recall_gap(value)
            if code is not None:
                gaps.append(code)
    needs = []
    raw_needs = packet.get("unmet_needs")
    if isinstance(raw_needs, (list, tuple)):
        for value in raw_needs[:MAX_RECALL_NEEDS]:
            code = _recall_gap(value)
            if code is not None:
                needs.append(code)
    coverage = packet.get("coverage")
    if coverage not in _RECALL_COVERAGE:
        coverage = "unknown"
    answerability = packet.get("answerability")
    if answerability not in _RECALL_ANSWERABILITY:
        answerability = "unknown"
    epoch = _integer(packet.get("memory_epoch")) if packet.get("memory_epoch") is not None else None
    if epoch is not None and epoch < 0:
        epoch = None
    return {
        "status": status,
        "items": items,
        "gaps": gaps,
        "coverage": coverage,
        "answerability": answerability,
        "memory_epoch": epoch,
        "unmet_needs": needs,
    }


def _recall_zone(name: object):
    if not isinstance(name, str) or not name or len(name) > 64:
        raise ValueError("timezone")
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - fixed-offset fallback when tzdata is absent
        if name == "Asia/Shanghai":
            return dt_timezone(timedelta(hours=8))
        if name == "UTC":
            return dt_timezone.utc
        raise ValueError("timezone") from None


def _recall_call(
    core,
    context,
    *,
    query: str,
    request_id: str,
    max_items: int,
    budget_tokens: int,
    timeout_seconds: float,
    zone,
) -> dict:
    packet = core.recall_packet(
        context,
        {
            "protocol_version": "1.1",
            "request_id": request_id,
            "query": query,
            "mode": "auto",
            "max_items": max_items,
            "budget_tokens": budget_tokens,
        },
        deadline_seconds=float(timeout_seconds),
        background_without_evidence=False,
        zone=zone,
    )
    return _recall_view(packet, max_items)


def _recall(request: dict) -> int:
    from scope_recall.contracts import ContractError, TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore

    binding = _binding(request)
    query = request.get("query")
    scope_id = request.get("scope_id")
    max_items = request.get("max_items", MAX_RECALL_ITEMS)
    budget_tokens = request.get("budget_tokens", MAX_RECALL_BUDGET_UNITS)
    timeout_seconds = request.get("timeout_seconds")
    timezone_name = request.get("timezone", "Asia/Shanghai")
    request_id = request.get("request_id")
    since = request.get("since")
    until = request.get("until")
    context_spec = request.get("context") or {}
    session_id = context_spec.get("session_id") if isinstance(context_spec, dict) else None
    if (
        not isinstance(query, str)
        or not 1 <= len(query) <= MAX_RECALL_QUERY_CHARS
        or not isinstance(scope_id, str)
        or not 1 <= len(scope_id) <= 240
        or type(max_items) is not int
        or not 1 <= max_items <= MAX_RECALL_ITEMS
        or type(budget_tokens) is not int
        or not 64 <= budget_tokens <= MAX_RECALL_BUDGET_UNITS
        or type(timeout_seconds) not in (int, float)
        or not 0 < timeout_seconds <= 2.0
        or since is not None
        or until is not None
        or (
            request_id is not None
            and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 100)
        )
        or (
            session_id is not None
            and (not isinstance(session_id, str) or not 1 <= len(session_id) <= 240)
        )
    ):
        return _fail("error", "invalid_request")
    try:
        zone = _recall_zone(timezone_name)
    except ValueError:
        return _fail("error", "invalid_request")

    data_directory = Path(request["binding"]["data_directory"])
    if not (data_directory / "memory.sqlite3").is_file():
        return _fail("unavailable", "SOURCE_MISSING")

    request_id = request_id or ("recall-" + uuid.uuid4().hex)
    started = time.monotonic()
    try:
        core = MemoryCore(
            CoreConfig(binding, write_timeout_seconds=float(timeout_seconds))
        )
        context = TrustedContext(
            binding,
            session_id or "auto-recall",
            binding.scope_ids,
            "host_generated",
        )
        result = _recall_call(
            core,
            context,
            query=query,
            request_id=request_id,
            max_items=max_items,
            budget_tokens=budget_tokens,
            timeout_seconds=float(timeout_seconds),
            zone=zone,
        )
        result["elapsed_ms"] = max(0, int((time.monotonic() - started) * 1000))
    except ContractError as exc:
        return _contract_failure(exc)
    except (ValueError, TypeError):
        return _fail("error", "invalid_result")
    except Exception as exc:  # noqa: BLE001 - fixed class, never content or paths
        return _fail("error", type(exc).__name__)

    text = json.dumps({"kind": "ok", "result": result}, ensure_ascii=False)
    if len(text.encode("utf-8")) > MAX_STDOUT_BYTES:
        return _fail("error", "response_too_large")
    sys.stdout.write(text)
    return 0


def _window_stamp(value: object) -> bool:
    """A time bound the upstream ``MemoryFilter`` will accept: aware RFC3339."""

    if not isinstance(value, str) or not 1 <= len(value) <= MAX_TIME_CHARS:
        return False
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return moment.utcoffset() is not None


def _time_window(request: dict) -> int:
    """One bounded, read-only inventory read through the public time filter.

    Only ``since``/``until`` are forwarded to ``MemoryFilter``; the host owns the
    window, the final delivery bound and the event-time post-filter.  An
    event-time caller passes no ``until``: the source occurrence the upstream
    filter reads is the message time, which can be later than the event.  Paging
    is bounded here because upstream ``browse`` has no caller deadline; the
    subprocess timeout stays the outer hard stop.
    """

    from scope_recall.contracts import ContractError, TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.management_query import MemoryFilter
    from scope_recall.core.restore import InstallationMaintenance

    binding = _binding(request)
    since = request.get("since")
    until = request.get("until")
    limit = request.get("limit", MAX_TIME_WINDOW_ITEMS)
    pages = request.get("pages", MAX_TIME_WINDOW_PAGES)
    scope_id = request.get("scope_id")
    timeout_seconds = request.get("timeout_seconds")
    if (
        not isinstance(scope_id, str)
        or not 1 <= len(scope_id) <= 240
        or scope_id not in binding.scope_ids
        or type(limit) is not int
        or not 1 <= limit <= MAX_TIME_WINDOW_ITEMS
        or type(pages) is not int
        or not 1 <= pages <= MAX_TIME_WINDOW_PAGES
        or type(timeout_seconds) not in (int, float)
        or not 0 < timeout_seconds <= 2.0
        or (since is None and until is None)
        or (since is not None and not _window_stamp(since))
        or (until is not None and not _window_stamp(until))
    ):
        return _fail("error", "invalid_request")

    data_directory = Path(request["binding"]["data_directory"])
    if not (data_directory / "memory.sqlite3").is_file():
        return _fail("unavailable", "SOURCE_MISSING")

    started = time.monotonic()
    items: list[dict] = []
    seen: set[tuple[str, int]] = set()
    examined = 0
    pages_used = 0
    scan_complete = False
    stop_reason = ""
    cursor = None
    try:
        core = MemoryCore(
            CoreConfig(binding, write_timeout_seconds=float(timeout_seconds))
        )
        context = TrustedContext(
            binding,
            "local-time-window",
            binding.scope_ids,
            "host_generated",
        )
        management = MemoryManagement(core, InstallationMaintenance(context))
        while pages_used < pages:
            page = management.list(
                MemoryFilter(
                    kind="event",
                    scope=scope_id,
                    since=since,
                    until=until,
                ),
                limit=limit,
                cursor=cursor,
            )
            view = _page_view(page)
            pages_used += 1
            examined += view["examined"]
            for item in view["items"]:
                key = (item["ref"], item["revision"])
                if key in seen:
                    continue
                seen.add(key)
                if len(items) < MAX_TIME_WINDOW_TOTAL:
                    items.append(item)
            scan_complete = view["scan_complete"]
            stop_reason = view["stop_reason"]
            cursor = view["next_cursor"]
            if scan_complete or cursor is None:
                break
    except ContractError as exc:
        return _contract_failure(exc)
    except (ValueError, TypeError):
        return _fail("error", "invalid_result")
    except Exception as exc:  # noqa: BLE001 - fixed class, never content or paths
        return _fail("error", type(exc).__name__)

    result = {
        "items": items,
        "examined": examined,
        "scan_complete": scan_complete,
        "stop_reason": stop_reason,
        "pages": pages_used,
        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
    }
    text = json.dumps({"kind": "ok", "result": result}, ensure_ascii=False)
    if len(text.encode("utf-8")) > MAX_STDOUT_BYTES:
        return _fail("error", "response_too_large")
    sys.stdout.write(text)
    return 0


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        return _fail("error", "request_too_large")
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _fail("error", "invalid_json")
    if not isinstance(request, dict):
        return _fail("error", "invalid_request")
    action = request.get("action", "capture")
    if action not in _ACTIONS:
        return _fail("error", "invalid_request")

    try:
        from scope_recall.contracts import ContractError  # noqa: F401
        from scope_recall.core import CoreConfig, MemoryCore  # noqa: F401
    except Exception:  # noqa: BLE001 - unavailable dependency is a blocked state
        return _fail("blocked", "adapter_unavailable")

    if action == "search":
        return _search(request)
    if action == "recall":
        return _recall(request)
    if action == "time_window":
        return _time_window(request)
    return _capture(request)


if __name__ == "__main__":
    raise SystemExit(main())
