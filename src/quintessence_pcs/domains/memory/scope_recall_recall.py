"""RG08 R-B: bounded, read-only automatic memory recall mapping.

The adapter returns one upstream ``RecallPacket``; this module validates that
envelope, recovers the PCS experience source behind each upstream event through
the delivery outbox, and produces the bounded material a consult Context may
render.  It never imports the upstream package, never reads SQLite, never calls
a model and never writes.

Only exact upstream ``event`` items are deliverable in R-B.  A claim/episode/
artifact has no reliable PCS speaker or source version, so it is recorded as a
``derived_item`` omission instead of being guessed into the Context.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from ...store.experience import (
    READABILITY_AVAILABLE,
    ExperienceAccess,
    ExperienceUnavailable,
)
from .experience_bridge import role_for_origin
from .time_window import in_windows, parse_timestamp, resolve_zone

#: The approved final-delivery bounds.  The adapter also receives them; these
#: are the host's last line of defence, not a second retrieval policy.
MAX_ITEMS = 4
MAX_BUDGET_UNITS = 2048
MAX_CONTENT_CHARS = 640
MAX_SECTION_BYTES = 8 * 1024
MAX_GAPS = 8
MAX_GAP_CHARS = 80
MAX_TOP_KINDS = 16

#: Status vocabulary shared by the adapter view, the Context audit and the
#: per-turn event.  ``invalid`` is keep distinct for audit even though the
#: Context/event surface folds it into ``unavailable``.
STATUSES = (
    "disabled",
    "ok",
    "partial",
    "no_match",
    "incomplete",
    "unavailable",
    "timeout",
    "invalid",
)

#: The upstream item fields the host needs.  Everything else is bounded out
#: before it can reach a Context or a log line.
_ITEM_FIELDS = (
    "ref",
    "revision",
    "kind",
    "content",
    "content_truncated",
    "temporal_status",
    "origin",
    "applicability",
    "evidence_refs",
    "expandable",
    "basis",
    "occurred_at",
)
_GAP_RE = re.compile(r"[a-z0-9_:.-]{1,%d}" % MAX_GAP_CHARS)
_INCOMPLETE_PREFIXES = (
    "deadline_exceeded",
    "sqlite_unavailable",
    "sqlite_candidate_error",
    "scan_",
    "packet_",
)


@dataclass(frozen=True)
class MemoryRecallPacket:
    """One validated upstream packet, before PCS source binding."""

    status: str
    items: tuple[dict[str, Any], ...]
    gaps: tuple[str, ...]
    coverage: str
    answerability: str
    memory_epoch: int | None
    elapsed_ms: int
    reason: str = ""
    query_variant: str | None = None
    stop_reason: str = ""
    scan_complete: bool = True
    scan_pages: int = 0
    examined: int = 0


@dataclass(frozen=True)
class RecallTimeFilter:
    """The host-computed window one recall delivery must respect.

    ``intent`` decides which PCS time is read: ``message`` filters on the
    message's own ``observed_at``; ``event`` filters on ``pcs_time.event_time``
    and only accepts ``event_type=occurred``.  A plan, quote, hypothesis or
    unknown is never read as an occurrence.  Windows are half-open UTC pairs.
    """

    windows: tuple[tuple[str, str], ...]
    intent: str
    anchor: str | None = None
    anchor_source: str = "unknown"
    unsupported: bool = False
    timezone: str = "Asia/Shanghai"


@dataclass(frozen=True)
class MemoryRecallItem:
    """One upstream event that is allowed to enter a consult Context."""

    upstream_ref: str
    upstream_revision: int
    upstream_kind: str
    content: str
    content_truncated: bool
    subject: str
    subject_ref: str
    temporal_status: str
    occurred_at: str | None
    recorded_at: str | None
    scope_id: str
    source_event_id: str
    source_ref: dict[str, Any]
    delivery_state: str
    #: RG08 R-E: the exact PCS ``EventRef`` a maintenance Context may show as a
    #: citable material reference.  R-B leaves this ``None`` (the chat recall
    #: section deliberately hides internal refs); only the maintenance renderer
    #: reads it.
    visible_ref: dict[str, Any] | None = None
    units: int = 0
    message_time: str | None = None
    message_time_basis: str = "unknown"
    event_type: str = "unknown"
    event_time: str | None = None
    time_precision: str = "unknown"
    time_basis: str = "unknown"
    #: J11 expression-preference consolidation: two *structural* marks, never a
    #: replacement of the event's attribution.  A message can be a direct
    #: experience and still contain a question or a quotation; downstream keeps
    #: the whole body, and who said what is the model's reading plus Review.
    contains_question: bool = False
    contains_quotation: bool = False


@dataclass(frozen=True)
class MemoryRecallMaterial:
    """What a consult Context may render and account for one recall attempt."""

    status: str
    items: tuple[MemoryRecallItem, ...]
    gaps: tuple[str, ...] = ()
    coverage: str = "unknown"
    upstream_answerability: str = "unknown"
    memory_epoch: int | None = None
    elapsed_ms: int = 0
    units: int = 0
    bytes: int = 0
    dropped: tuple[dict[str, Any], ...] = ()
    query_variant: str | None = None
    time_intent: str = "none"
    windows: int = 0
    scan_pages: int = 0
    scan_complete: bool = True
    time_unknown: int = 0
    time_not_occurred: int = 0
    time_dropped: int = 0
    time_unsupported: bool = False
    window_start: str | None = None
    window_end: str | None = None
    anchor_source: str = "unknown"
    #: A bounded, content-free code for a failed attempt (J10 W5).  Empty when
    #: the attempt succeeded: a successful recall has nothing to explain.
    reason: str = ""

    def with_dropped(self, reason: str, **detail: Any) -> "MemoryRecallMaterial":
        entry: dict[str, Any] = {"reason": reason}
        for key, value in detail.items():
            if value is not None:
                entry[key] = value
        return replace(self, dropped=self.dropped + (entry,))


class MemoryRecallContextError(Exception):
    """A malformed host-side recall request (never an upstream failure)."""


def recall_scope_recall(
    adapter: Any,
    *,
    query: str,
    scope_id: str,
    max_items: int = MAX_ITEMS,
    budget_units: int = MAX_BUDGET_UNITS,
    timeout_seconds: float = 1.8,
    timezone: str = "Asia/Shanghai",
    request_id: str | None = None,
) -> MemoryRecallPacket:
    """Run one bounded upstream recall and validate its public packet.

    Engine failures are returned as statuses; only an invalid host request
    raises.  The caller catches everything else and never lets a recall break a
    chat turn.
    """

    if not isinstance(query, str) or not 1 <= len(query) <= 8192:
        raise MemoryRecallContextError("query")
    if not isinstance(scope_id, str) or not 1 <= len(scope_id) <= 240:
        raise MemoryRecallContextError("scope")
    if type(max_items) is not int or not 1 <= max_items <= MAX_ITEMS:
        raise MemoryRecallContextError("max_items")
    if type(budget_units) is not int or not 64 <= budget_units <= MAX_BUDGET_UNITS:
        raise MemoryRecallContextError("budget_units")
    if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
        raise MemoryRecallContextError("timeout")
    if not isinstance(timezone, str) or not timezone:
        raise MemoryRecallContextError("timezone")
    if request_id is not None and (
        not isinstance(request_id, str) or not 1 <= len(request_id) <= 100
    ):
        raise MemoryRecallContextError("request_id")

    started = time.monotonic()
    try:
        outcome = adapter.recall(
            query=query,
            max_items=max_items,
            budget_tokens=budget_units,
            timeout_seconds=float(timeout_seconds),
            timezone=timezone,
            request_id=request_id,
        )
    except Exception as exc:  # noqa: BLE001 - adapter failure is a state
        return MemoryRecallPacket(
            status="unavailable",
            items=(),
            gaps=(),
            coverage="unknown",
            answerability="unknown",
            memory_epoch=None,
            elapsed_ms=_elapsed_ms(started),
            reason=type(exc).__name__,
        )
    elapsed = _elapsed_ms(started)

    if not isinstance(outcome, Mapping):
        return _failure("invalid", elapsed, "response")
    kind = outcome.get("kind")
    if kind == "ok":
        return _packet(outcome.get("result"), elapsed, max_items)
    if kind == "blocked":
        status = "unavailable"
        reason = str(outcome.get("error") or "blocked")[:64]
    elif kind == "unavailable":
        status, reason = "unavailable", str(outcome.get("error") or "")[:64]
    elif kind == "conflict":
        status, reason = "invalid", str(outcome.get("error") or "conflict")[:64]
    elif kind == "unknown":
        status = "timeout" if outcome.get("error") == "timeout" else "unavailable"
        reason = str(outcome.get("error") or "")[:64]
    else:
        status, reason = "invalid", str(outcome.get("error") or "kind")[:64]
    return _failure(status, elapsed, reason)


def recall_time_window(
    adapter: Any,
    *,
    since: str | None,
    until: str | None,
    limit: int = 20,
    pages: int = 3,
    timeout_seconds: float = 1.8,
    request_id: str | None = None,
) -> MemoryRecallPacket:
    """One bounded inventory read over the public time filter.

    This is the only channel Event-time retrieval can use: the upstream recall
    request has no range, while ``MemoryFilter.since/until`` filters the source
    occurrence it stores.  The host computes the window and post-filters; this
    function only validates the bounded page set.  An event-time caller passes
    ``until=None``: a message said today may report an event from yesterday.
    """

    if since is not None and parse_timestamp(since) is None:
        raise MemoryRecallContextError("since")
    if until is not None and parse_timestamp(until) is None:
        raise MemoryRecallContextError("until")
    if since is None and until is None:
        raise MemoryRecallContextError("window")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise MemoryRecallContextError("limit")
    if type(pages) is not int or not 1 <= pages <= 3:
        raise MemoryRecallContextError("pages")
    if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
        raise MemoryRecallContextError("timeout")

    started = time.monotonic()
    try:
        outcome = adapter.time_window(
            since=since,
            until=until,
            limit=limit,
            pages=pages,
            timeout_seconds=float(timeout_seconds),
            request_id=request_id,
        )
    except Exception as exc:  # noqa: BLE001 - adapter failure is a state
        return MemoryRecallPacket(
            status="unavailable",
            items=(),
            gaps=(),
            coverage="unknown",
            answerability="unknown",
            memory_epoch=None,
            elapsed_ms=_elapsed_ms(started),
            reason=type(exc).__name__,
        )
    elapsed = _elapsed_ms(started)
    if not isinstance(outcome, Mapping):
        return _failure("invalid", elapsed, "response")
    kind = outcome.get("kind")
    if kind == "ok":
        return _window_packet(outcome.get("result"), elapsed)
    if kind == "blocked":
        status, reason = "unavailable", str(outcome.get("error") or "blocked")[:64]
    elif kind == "unavailable":
        status, reason = "unavailable", str(outcome.get("error") or "")[:64]
    elif kind == "conflict":
        status, reason = "invalid", str(outcome.get("error") or "conflict")[:64]
    elif kind == "unknown":
        status = "timeout" if outcome.get("error") == "timeout" else "unavailable"
        reason = str(outcome.get("error") or "")[:64]
    else:
        status, reason = "invalid", str(outcome.get("error") or "kind")[:64]
    return _failure(status, elapsed, reason)


def _window_packet(value: Any, elapsed_ms: int) -> MemoryRecallPacket:
    """Validate one bounded ``list`` page set before source binding."""

    if not isinstance(value, Mapping):
        return _failure("invalid", elapsed_ms, "result")
    raw_items = value.get("items")
    if not isinstance(raw_items, (list, tuple)) or len(raw_items) > 60:
        return _failure("invalid", elapsed_ms, "items")
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            return _failure("invalid", elapsed_ms, "item")
        ref = raw.get("ref")
        revision = raw.get("revision")
        if not isinstance(ref, str) or not 1 <= len(ref) <= 240:
            return _failure("invalid", elapsed_ms, "item_ref")
        if type(revision) is not int or revision < 1:
            return _failure("invalid", elapsed_ms, "item_revision")
        if raw.get("kind") != "event":
            return _failure("invalid", elapsed_ms, "item_kind")
        key = (ref, revision)
        if key in seen:
            continue
        seen.add(key)
        content = raw.get("content")
        content = content if isinstance(content, str) else ""
        if len(content) > 12000:
            return _failure("invalid", elapsed_ms, "item_content")
        occurred_at = raw.get("occurred_at")
        items.append(
            {
                "ref": ref,
                "revision": revision,
                "kind": "event",
                "content": content[:MAX_CONTENT_CHARS],
                "content_truncated": bool(raw.get("content_truncated"))
                or len(content) > MAX_CONTENT_CHARS,
                "temporal_status": "unknown",
                "origin": "",
                "applicability": "",
                "evidence_refs": (),
                "expandable": False,
                "basis": "",
                "occurred_at": str(occurred_at)[:48]
                if isinstance(occurred_at, str)
                else None,
            }
        )
    scan_complete = value.get("scan_complete") is True
    pages = value.get("pages")
    examined = value.get("examined")
    stop_reason = value.get("stop_reason")
    return MemoryRecallPacket(
        status="ok",
        items=tuple(items),
        gaps=(),
        coverage="complete_for_query" if scan_complete else "partial",
        answerability="unknown",
        memory_epoch=None,
        elapsed_ms=elapsed_ms,
        stop_reason=str(stop_reason or "")[:48],
        scan_complete=scan_complete,
        scan_pages=pages if type(pages) is int and pages >= 0 else 0,
        examined=examined if type(examined) is int and examined >= 0 else 0,
    )


def bind_recall_sources(
    store: Any,
    *,
    namespace: str,
    access: ExperienceAccess,
    page: MemoryRecallPacket,
    scope_id: str,
    max_items: int = MAX_ITEMS,
    budget_units: int = MAX_BUDGET_UNITS,
    current_input_ref: Mapping[str, Any] | None = None,
    history_refs: Sequence[Any] = (),
    tool_results: Sequence[Any] = (),
    time_filter: RecallTimeFilter | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> MemoryRecallMaterial:
    """Bind each upstream event to its exact PCS delivery and bound it.

    A source the PCS cannot prove (no delivery, ambiguous mapping, stale
    revision, unauthorized/privacy-unreadable, or a claim/episode) is dropped
    with a bounded reason.  The host never guesses a speaker from upstream
    text and never treats a dropped item as a successful match.

    ``time_filter`` is one additional gate only: a source must still pass every
    identity, revision, permission and deletion check above.  Event-time intent
    reads the separately stored ``request_json.pcs_time`` and accepts only
    ``event_type=occurred``; a plan or an unknown never becomes an occurrence.
    """

    # Imported here rather than at module load: ``runtime.budget`` lives in the
    # runtime package that imports ``runtime.context``, which imports this
    # module.  A top-level import would create a cycle.
    from ...runtime.budget import estimate_units

    if page.status in ("unavailable", "timeout", "invalid", "disabled"):
        return _material_from_page(page)
    if page.status == "no_match" and not page.items:
        return _material_from_page(page)

    refs = [str(item.get("ref") or "") for item in page.items]
    mapping: dict[str, tuple[dict[str, Any], ...]] = {}
    try:
        mapping = store.experience_recall_sources(namespace, refs)
    except Exception:  # noqa: BLE001 - a read failure is an omission, not a crash
        mapping = {}

    current_key = _ref_key(current_input_ref)
    history = {_history_key(value) for value in history_refs}
    history.discard(None)
    tools = _tool_keys(tool_results)

    delivered: list[MemoryRecallItem] = []
    dropped: list[dict[str, Any]] = []
    seen_upstream: set[tuple[str, int]] = set()
    seen_pcs: set[tuple[str, str, int]] = set()
    units = 0
    total_bytes = 0
    time_unknown = 0
    time_not_occurred = 0
    time_dropped = 0

    for raw in page.items:
        if not isinstance(raw, Mapping):
            dropped.append({"reason": "invalid_item"})
            continue
        upstream_ref = str(raw.get("ref") or "")
        revision = raw.get("revision")
        if not upstream_ref or type(revision) is not int or revision < 1:
            dropped.append({"reason": "invalid_item"})
            continue
        if raw.get("kind") != "event":
            dropped.append(
                {"reason": "derived_item", "ref": upstream_ref[:80], "revision": revision}
            )
            continue
        if (upstream_ref, revision) in seen_upstream:
            dropped.append({"reason": "duplicate", "ref": upstream_ref[:80], "revision": revision})
            continue
        seen_upstream.add((upstream_ref, revision))

        rows = mapping.get(upstream_ref) or ()
        if not rows:
            dropped.append({"reason": "unmapped_source", "ref": upstream_ref[:80], "revision": revision})
            continue
        sources = {
            (str(row.get("ref_type") or ""), str(row.get("ref_id") or ""), int(row.get("ref_version") or 0))
            for row in rows
        }
        if len(sources) != 1:
            dropped.append({"reason": "ambiguous_source", "ref": upstream_ref[:80], "revision": revision})
            continue
        row = rows[0]
        ref_type, ref_id, ref_version = next(iter(sources))
        if (
            ref_type not in ("collab_input", "collab_artifact")
            or not ref_id
            or ref_version < 1
        ):
            dropped.append({"reason": "invalid_source", "ref": upstream_ref[:80], "revision": revision})
            continue
        if str(row.get("state") or "") not in ("applied", "verified"):
            dropped.append({"reason": "delivery_not_current", "ref": upstream_ref[:80], "revision": revision})
            continue
        external_revision = row.get("external_revision")
        if type(external_revision) is not int or external_revision != revision:
            dropped.append({"reason": "stale_revision", "ref": upstream_ref[:80], "revision": revision})
            continue
        key = (ref_type, ref_id, ref_version)
        if current_key == key:
            dropped.append({"reason": "current_input"})
            continue
        if key in history:
            dropped.append({"reason": "history_duplicate", "ref": upstream_ref[:80], "revision": revision})
            continue
        if (upstream_ref, revision) in tools["upstream"] or key in tools["pcs"]:
            dropped.append({"reason": "tool_duplicate", "ref": upstream_ref[:80], "revision": revision})
            continue
        if key in seen_pcs:
            dropped.append({"reason": "source_collapsed", "ref": upstream_ref[:80], "revision": revision})
            continue

        source_ref = _source_ref(ref_type, ref_id, ref_version, namespace)
        try:
            view = store.read_experience(access, ref=source_ref, purpose="consult")
        except Exception:  # noqa: BLE001 - permission/read failures are omissions
            view = None
        if (
            not isinstance(view, object)
            or isinstance(view, ExperienceUnavailable)
            or getattr(view, "readability", None) != READABILITY_AVAILABLE
            or not getattr(view, "body", None)
        ):
            dropped.append({"reason": "source_unavailable", "ref": upstream_ref[:80], "revision": revision})
            continue

        message_time, message_time_basis, event_type, event_time, time_precision, time_basis = _row_times(
            row,
            timezone_name=(
                time_filter.timezone if time_filter is not None else timezone_name
            ),
        )
        if time_filter is not None and time_filter.windows:
            if time_filter.intent == "event":
                if event_type in ("planned", "hypothetical", "quoted"):
                    dropped.append(
                        {"reason": "time_not_occurred", "ref": upstream_ref[:80], "revision": revision}
                    )
                    time_not_occurred += 1
                    continue
                if (
                    event_type != "occurred"
                    or event_time is None
                    or time_precision == "unknown"
                ):
                    dropped.append(
                        {"reason": "time_unknown", "ref": upstream_ref[:80], "revision": revision}
                    )
                    time_unknown += 1
                    continue
                if not in_windows(event_time, time_filter.windows):
                    dropped.append(
                        {"reason": "time_outside_window", "ref": upstream_ref[:80], "revision": revision}
                    )
                    time_dropped += 1
                    continue
            else:
                if message_time is None or not in_windows(message_time, time_filter.windows):
                    dropped.append(
                        {"reason": "time_outside_window", "ref": upstream_ref[:80], "revision": revision}
                    )
                    time_dropped += 1
                    continue

        content = raw.get("content")
        content = content if isinstance(content, str) else ""
        content = content[:MAX_CONTENT_CHARS]
        truncated = bool(raw.get("content_truncated")) or len(str(raw.get("content") or "")) > MAX_CONTENT_CHARS
        occurred_at = raw.get("occurred_at")
        occurred_at = occurred_at if isinstance(occurred_at, str) and len(occurred_at) <= 48 else None
        temporal = raw.get("temporal_status")
        temporal = temporal if temporal in ("current", "historical", "disputed", "unknown") else "unknown"
        subject = role_for_origin(ref_type, str(row.get("origin_kind") or ""))
        subject_ref = f"{ref_type}:{ref_id}@{ref_version}"
        item_units = estimate_units(content)
        item_bytes = len(content.encode("utf-8"))
        if (
            len(delivered) >= max_items
            or units + item_units > budget_units
            or total_bytes + item_bytes > MAX_SECTION_BYTES
        ):
            dropped.append({"reason": "budget", "ref": upstream_ref[:80], "revision": revision})
            continue
        delivered.append(
            MemoryRecallItem(
                upstream_ref=upstream_ref[:240],
                upstream_revision=revision,
                upstream_kind="event",
                content=content,
                content_truncated=truncated,
                subject=subject,
                subject_ref=subject_ref,
                temporal_status=temporal,
                occurred_at=occurred_at,
                recorded_at=None,
                scope_id=scope_id,
                source_event_id=str(row.get("event_id") or "")[:36],
                source_ref=source_ref,
                delivery_state=str(row.get("state") or ""),
                units=item_units,
                message_time=message_time,
                message_time_basis=message_time_basis,
                event_type=event_type,
                event_time=event_time,
                time_precision=time_precision,
                time_basis=time_basis,
            )
        )
        seen_pcs.add(key)
        units += item_units
        total_bytes += item_bytes

    status = _delivery_status(page, delivered, dropped, time_filter)
    material = MemoryRecallMaterial(
        status=status,
        items=tuple(delivered),
        gaps=page.gaps,
        coverage=page.coverage,
        upstream_answerability=page.answerability,
        memory_epoch=page.memory_epoch,
        elapsed_ms=page.elapsed_ms,
        units=units,
        bytes=total_bytes,
        dropped=tuple(dropped[:32]),
        query_variant=page.query_variant,
        time_intent=time_filter.intent if time_filter is not None else "none",
        windows=len(time_filter.windows) if time_filter is not None else 0,
        scan_pages=page.scan_pages,
        scan_complete=page.scan_complete,
        time_unknown=time_unknown,
        time_not_occurred=time_not_occurred,
        time_dropped=time_dropped,
        time_unsupported=bool(time_filter.unsupported) if time_filter is not None else False,
        window_start=(
            min(start for start, _end in time_filter.windows)
            if time_filter is not None and time_filter.windows
            else None
        ),
        window_end=(
            max(end for _start, end in time_filter.windows)
            if time_filter is not None and time_filter.windows
            else None
        ),
        anchor_source=time_filter.anchor_source if time_filter is not None else "unknown",
    )
    return material


def _delivery_status(
    page: MemoryRecallPacket,
    delivered: list[Any],
    dropped: list[dict[str, Any]],
    time_filter: RecallTimeFilter | None = None,
) -> str:
    """The honest status of one delivery set.

    The delivered-item path follows R-B: a clean upstream packet with only
    expected de-duplication drops is ``ok``.  A time filter that withheld
    material yields ``partial``.  The empty path separates "scanned completely
    and found nothing verifiable" from "did not finish scanning", "engine
    unavailable", and "recognised but unsupported time": only the first is
    ``no_match``.
    """

    page_status = page.status
    reasons = [str(entry.get("reason") or "") for entry in dropped]
    time_unknown = reasons.count("time_unknown")
    time_not_occurred = reasons.count("time_not_occurred")
    time_filtered = "time_outside_window" in reasons
    if delivered:
        if (
            page_status in ("ok", "partial")
            and page.scan_complete
            and _only_expected_drops(dropped)
            and not time_filtered
        ):
            return "ok" if page_status == "ok" else "partial"
        return "partial"
    if page_status in ("unavailable", "timeout", "invalid"):
        return page_status
    if page_status == "incomplete":
        return "incomplete"
    if page_status == "partial" and not delivered:
        return "incomplete"
    if page.scan_complete is False:
        return "incomplete"
    if time_filter is not None and time_filter.unsupported:
        return "incomplete"
    if page_status == "no_match":
        return "incomplete" if dropped else "no_match"
    if time_filter is not None and time_filter.intent == "event":
        if time_unknown:
            # A candidate without a proven occurrence could be in the window.
            return "incomplete"
        if time_not_occurred:
            # The candidates were plans/quotes/hypotheses: no occurrence is
            # proven, so the window was not shown to be empty.
            return "incomplete"
    if dropped and not _only_expected_drops(dropped):
        return "incomplete"
    if dropped and any(
        str(entry.get("reason") or "") != "time_outside_window"
        for entry in dropped
    ):
        # R-B: the packet had material, but every item was already delivered
        # elsewhere (history/tool/current input); that is not a clean miss.
        return "incomplete"
    # A complete scan with only time-outside-window drops: no verified material
    # in the requested window.
    return "no_match"


def _only_expected_drops(dropped: Sequence[dict[str, Any]]) -> bool:
    expected = {
        "current_input",
        "history_duplicate",
        "tool_duplicate",
        "duplicate",
        "source_collapsed",
        "time_outside_window",
    }
    return all(str(entry.get("reason") or "") in expected for entry in dropped)


def _material_from_page(page: MemoryRecallPacket) -> MemoryRecallMaterial:
    return MemoryRecallMaterial(
        status=page.status,
        items=(),
        gaps=page.gaps,
        coverage=page.coverage,
        upstream_answerability=page.answerability,
        memory_epoch=page.memory_epoch,
        elapsed_ms=page.elapsed_ms,
        query_variant=page.query_variant,
        scan_pages=page.scan_pages,
        scan_complete=page.scan_complete,
        reason=_bounded_reason(page.reason),
    )


_REASON_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,47}$")

#: The only words a free-text failure becomes.  A provider message, a query or
#: a memory body never reaches the event: it lands on one of these instead.
_REASON_KEYWORDS = (
    "adapter_unavailable",
    "timeout",
    "blocked",
    "unavailable",
    "conflict",
    "invalid",
)


def _bounded_reason(value: Any) -> str:
    """One content-free failure code, bounded and never free text (J10 W5).

    A machine token (``adapter_unavailable``, ``SOURCE_MISSING``) is kept as-is.
    Anything else -- a provider sentence, a stack message, a query fragment --
    collapses to a known keyword when it mentions one, and to ``other``
    otherwise.  The event is an audit record, not a log sink.
    """

    if not isinstance(value, str):
        return ""
    token = value.strip()[:64]
    if not token:
        return ""
    if _REASON_TOKEN.match(token):
        return token
    lowered = token.lower()
    for keyword in _REASON_KEYWORDS:
        if keyword in lowered:
            return keyword
    return "other"


def _row_times(
    row: Mapping[str, Any], *, timezone_name: str = "Asia/Shanghai"
) -> tuple[str | None, str, str, str | None, str, str]:
    """The message time and stored classification behind one delivery row.

    ``message_time_basis`` is ``recorded_at_fallback`` for an old row without a
    real ``observed_at``.  ``event_time`` comes only from
    ``request_json.pcs_time`` written at projection time and only for an
    occurred event; plans/quotes/hypotheses/unknown never read as occurrences.
    """

    try:
        zone = resolve_zone(timezone_name)
    except ValueError:
        zone = None
    message_time: str | None = None
    message_time_basis = "unknown"
    observed = parse_timestamp(row.get("observed_at"))
    created = parse_timestamp(row.get("created_at"))
    chosen = observed if observed is not None else created
    if chosen is not None:
        message_time = (
            chosen.astimezone(zone).isoformat() if zone is not None else chosen.isoformat()
        )
        message_time_basis = (
            "observed_at" if observed is not None else "recorded_at_fallback"
        )
    event_type = "unknown"
    event_time: str | None = None
    precision = "unknown"
    time_basis = "unknown"
    raw = row.get("request_json")
    if isinstance(raw, str) and 2 <= len(raw) <= 16384:
        try:
            document = json.loads(raw)
        except (TypeError, ValueError):
            document = None
        if isinstance(document, Mapping):
            pcs = document.get("pcs_time")
            if isinstance(pcs, Mapping):
                candidate_type = pcs.get("event_type")
                if candidate_type in (
                    "occurred",
                    "planned",
                    "hypothetical",
                    "quoted",
                    "unknown",
                ):
                    event_type = candidate_type
                candidate_precision = pcs.get("time_precision")
                if candidate_precision in ("instant", "day", "approximate", "unknown"):
                    precision = candidate_precision
                candidate_basis = pcs.get("time_basis")
                if candidate_basis in ("relative_day", "explicit_date", "unknown"):
                    time_basis = candidate_basis
                candidate_event = pcs.get("event_time")
                if (
                    event_type == "occurred"
                    and parse_timestamp(candidate_event) is not None
                ):
                    event_time = str(candidate_event)
    return (
        message_time,
        message_time_basis,
        event_type,
        event_time,
        precision,
        time_basis,
    )


def _packet(value: Any, elapsed_ms: int, max_items: int) -> MemoryRecallPacket:
    if not isinstance(value, Mapping):
        return _failure("invalid", elapsed_ms, "result")
    status = value.get("status")
    if status not in ("ok", "no_match", "partial", "unavailable"):
        return _failure("invalid", elapsed_ms, "status")
    raw_items = value.get("items")
    if not isinstance(raw_items, (list, tuple)):
        return _failure("invalid", elapsed_ms, "items")
    if len(raw_items) > max_items:
        return _failure("invalid", elapsed_ms, "items_over_limit")
    items: list[dict[str, Any]] = []
    for raw in raw_items[:MAX_TOP_KINDS]:
        if not isinstance(raw, Mapping):
            continue
        ref = raw.get("ref")
        revision = raw.get("revision")
        if not isinstance(ref, str) or not 1 <= len(ref) <= 240:
            return _failure("invalid", elapsed_ms, "item_ref")
        if type(revision) is not int or revision < 1:
            return _failure("invalid", elapsed_ms, "item_revision")
        kind = raw.get("kind")
        if kind not in ("event", "claim", "episode", "procedure", "artifact"):
            return _failure("invalid", elapsed_ms, "item_kind")
        content = raw.get("content")
        content = content if isinstance(content, str) else ""
        if len(content) > 12000:
            return _failure("invalid", elapsed_ms, "item_content")
        items.append(
            {
                "ref": ref,
                "revision": revision,
                "kind": kind,
                "content": content[:MAX_CONTENT_CHARS],
                "content_truncated": bool(raw.get("content_truncated"))
                or len(content) > MAX_CONTENT_CHARS,
                "temporal_status": raw.get("temporal_status")
                if raw.get("temporal_status") in ("current", "historical", "disputed", "unknown")
                else "unknown",
                "origin": str(raw.get("origin") or "")[:80],
                "applicability": str(raw.get("applicability") or "")[:256],
                "evidence_refs": tuple(str(ref)[:240] for ref in (raw.get("evidence_refs") or ())[:8]),
                "expandable": raw.get("expandable") is True,
                "basis": str(raw.get("basis") or "")[:32],
                "occurred_at": str(raw.get("occurred_at"))[:48]
                if isinstance(raw.get("occurred_at"), str)
                else None,
            }
        )
    gaps = _bounded_codes(value.get("gaps"))
    coverage = value.get("coverage")
    coverage = coverage if coverage in ("complete_for_query", "partial", "unknown") else "unknown"
    answerability = value.get("answerability")
    answerability = answerability if answerability in (
        "supported",
        "partial",
        "ambiguous",
        "unknown",
    ) else "unknown"
    epoch = value.get("memory_epoch")
    epoch = epoch if type(epoch) is int and epoch >= 0 else None
    variant = value.get("query_variant")
    variant = variant if variant == "focus_prefix" else None
    status = status
    if status == "no_match" and _incomplete(gaps, coverage):
        status = "incomplete"
    if status == "partial" and not items:
        status = "incomplete"
    return MemoryRecallPacket(
        status=status,
        items=tuple(items),
        gaps=gaps,
        coverage=coverage,
        answerability=answerability,
        memory_epoch=epoch,
        elapsed_ms=elapsed_ms,
        query_variant=variant,
    )


def _failure(status: str, elapsed_ms: int, reason: str) -> MemoryRecallPacket:
    return MemoryRecallPacket(
        status=status,
        items=(),
        gaps=(),
        coverage="unknown",
        answerability="unknown",
        memory_epoch=None,
        elapsed_ms=elapsed_ms,
        reason=reason[:64],
    )


def _incomplete(gaps: Sequence[str], coverage: str) -> bool:
    """Whether a no-match packet may not be trusted as a complete scan.

    ``vector_unavailable`` is an optional channel this batch does not use; on
    an installation without embeddings every lexical recall reports it.  It
    must not turn a real lexical no-match into ``incomplete``.  A deadline,
    SQLite failure, scan bound or any other gap still does.
    """

    blocking = [gap for gap in gaps if not gap.startswith("vector_")]
    if not blocking:
        return False
    if coverage == "partial":
        return True
    return any(
        gap.startswith(prefix) for gap in blocking for prefix in _INCOMPLETE_PREFIXES
    )


def _bounded_codes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for raw in value[:MAX_GAPS]:
        if not isinstance(raw, str):
            continue
        code = raw[:MAX_GAP_CHARS]
        if _GAP_RE.fullmatch(code):
            result.append(code)
    return tuple(result)


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _ref_key(value: Mapping[str, Any] | None) -> tuple[str, str, int] | None:
    if not isinstance(value, Mapping):
        return None
    ref_type = value.get("ref_type")
    version = (
        value.get("content_version")
        if ref_type == "collab_input"
        else value.get("artifact_version")
    )
    ref_id = value.get("input_id") if ref_type == "collab_input" else value.get("artifact_id")
    if ref_type not in ("collab_input", "collab_artifact"):
        return None
    if not isinstance(ref_id, str) or type(version) is not int or version < 1:
        return None
    return (ref_type, ref_id, version)


def _history_key(value: Any) -> tuple[str, str, int] | None:
    if isinstance(value, Mapping):
        ref_type = value.get("ref_kind") or value.get("ref_type")
        ref_id = value.get("ref_id")
        version = value.get("ref_version")
        if ref_type == "collab_input":
            version = value.get("content_version", version)
        elif ref_type == "collab_artifact":
            version = value.get("artifact_version", version)
    else:
        ref_type = getattr(value, "ref_kind", None)
        ref_id = getattr(value, "ref_id", None)
        version = getattr(value, "ref_version", None)
    if ref_type not in ("collab_input", "collab_artifact"):
        return None
    if not isinstance(ref_id, str) or type(version) is not int or version < 1:
        return None
    return (ref_type, ref_id, version)


def _source_ref(ref_type: str, ref_id: str, ref_version: int, namespace: str) -> dict[str, Any]:
    if ref_type == "collab_input":
        return {
            "ref_type": "collab_input",
            "namespace": namespace,
            "input_id": ref_id,
            "content_version": ref_version,
        }
    return {
        "ref_type": "collab_artifact",
        "namespace": namespace,
        "artifact_id": ref_id,
        "artifact_version": ref_version,
    }


def _tool_keys(tool_results: Sequence[Any]) -> dict[str, set[Any]]:
    """Extract exact refs from persisted memory.search result bodies.

    The excerpt the model sees is clipped; the persisted body is the exact
    artifact body.  A malformed body skips dedupe instead of deleting an
    otherwise valid recall item.
    """

    upstream: set[tuple[str, int]] = set()
    pcs: set[tuple[str, str, int]] = set()
    for material in tool_results:
        if getattr(material, "tool", "") != "memory.search":
            continue
        body = getattr(material, "body", None)
        if not isinstance(body, str):
            continue
        try:
            import json

            document = json.loads(body)
        except (TypeError, ValueError):
            continue
        # ToolResultMaterial.body is the handler payload itself; older/other
        # wrappers may nest it under "result".  Accept both, never a guess.
        result = document.get("result") if isinstance(document, dict) else None
        if not isinstance(result, dict):
            result = document
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            ref = item.get("ref")
            revision = item.get("revision")
            if isinstance(ref, str) and type(revision) is int:
                upstream.add((ref, revision))
            subject_ref = item.get("subject_ref")
            if isinstance(subject_ref, str) and ":" in subject_ref and "@" in subject_ref:
                kind, rest = subject_ref.split(":", 1)
                identity, _, version = rest.rpartition("@")
                if kind in ("collab_input", "collab_artifact") and identity and version.isdigit():
                    pcs.add((kind, identity, int(version)))
    return {"upstream": upstream, "pcs": pcs}


__all__ = [
    "MAX_BUDGET_UNITS",
    "MAX_CONTENT_CHARS",
    "MAX_ITEMS",
    "MAX_SECTION_BYTES",
    "MemoryRecallContextError",
    "MemoryRecallItem",
    "MemoryRecallMaterial",
    "MemoryRecallPacket",
    "RecallTimeFilter",
    "STATUSES",
    "bind_recall_sources",
    "recall_scope_recall",
    "recall_time_window",
]
