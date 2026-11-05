"""Pure aggregation for the read-only usage and turn-trace views.

This module never touches the database: the store hands in the already
authorized attempt rows, the persisted ``repair_attempt`` markers and the
Context byte budget, and everything here is arithmetic on those facts.

Two rules decide every value:

* a token number exists only when the supplier reported all three usage keys
  (``usage_known``).  There is no tokenizer, no character estimate and no
  fallback from another request;
* a request is a *repair* only when a persisted marker names its attempt.  A
  same-body resend is a transport retry; an unmarked request is ``unknown``,
  never silently classified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

#: The provider usage keys a known attempt must carry (http_model contract).
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")

#: The frozen reason vocabulary of a persisted ``repair_attempt`` marker.
REPAIR_REASONS = frozenset({"format", "feedback_retry"})

#: Attempt kinds the trace may state.  ``unmarked`` means "cannot prove".
KIND_PRIMARY = "primary"
KIND_REPAIR = "repair"
KIND_TRANSPORT_RETRY = "transport_retry"
KIND_UNMARKED = "unmarked"


@dataclass(frozen=True)
class AttemptRecord:
    """One stored HTTP attempt plus the Context/stage it belongs to."""

    attempt_id: str
    attempt_no: int
    turn_id: str
    context_id: str
    stage_kind: str
    purpose: str
    ordinal: int
    status: str
    outcome: str | None
    usage: Mapping[str, Any] | None
    usage_known: bool
    started_at: str
    finished_at: str | None
    request_hash: str
    finish_reason: str | None
    error_label: str | None
    repair_reason: str | None = None


@dataclass(frozen=True)
class AttemptSummary:
    """The public, body-free description of one attempt."""

    attempt_id: str
    attempt_no: int
    stage: str
    purpose: str
    ordinal: int
    status: str
    outcome: str | None
    finish_reason: str | None
    error_label: str | None
    usage_known: bool
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    tokens_per_second: float | None
    repair: bool | None
    repair_reason: str | None
    resend_same_body: bool
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "attempt_no": self.attempt_no,
            "stage": self.stage,
            "purpose": self.purpose,
            "ordinal": self.ordinal,
            "status": self.status,
            "outcome": self.outcome,
            "finish_reason": self.finish_reason,
            "error_label": self.error_label,
            "usage_known": self.usage_known,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "tokens_per_second": self.tokens_per_second,
            "repair": self.repair,
            "repair_reason": self.repair_reason,
            "resend_same_body": self.resend_same_body,
            "kind": self.kind,
        }


def _instant(value: object) -> datetime | None:
    """Parse one stored RFC 3339 UTC instant, or return None."""

    if not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def duration_ms(record: AttemptRecord) -> int | None:
    """Real elapsed attempt time, from reservation to settlement."""

    started = _instant(record.started_at)
    finished = _instant(record.finished_at)
    if started is None or finished is None:
        return None
    elapsed = (finished - started).total_seconds()
    if elapsed < 0:
        return None
    return int(round(elapsed * 1000))


def token_values(record: AttemptRecord) -> tuple[int, int, int] | None:
    """The three supplier numbers, or None when any of them is unavailable."""

    if record.status != "finished" or not record.usage_known:
        return None
    usage = record.usage
    if not isinstance(usage, Mapping):
        return None
    values: list[int] = []
    for key in USAGE_KEYS:
        value = usage.get(key)
        if type(value) is not int or value < 0:
            return None
        values.append(value)
    return values[0], values[1], values[2]


def _speed(completion_tokens: int | None, elapsed_ms: int | None) -> float | None:
    if completion_tokens is None or elapsed_ms is None or elapsed_ms <= 0:
        return None
    return completion_tokens / (elapsed_ms / 1000.0)


def is_complete(*, attempts: int, unknown: int, open_attempts: int, unreadable: int) -> bool:
    """True only when every attempt in scope carries a readable token number."""

    return attempts > 0 and unknown == 0 and open_attempts == 0 and unreadable == 0


def aggregate(records: Sequence[AttemptRecord]) -> dict[str, Any]:
    """Attempt counts and token sums for one scope (turn or conversation)."""

    attempts = len(records)
    open_attempts = sum(1 for item in records if item.status == "reserved")
    unreadable = 0
    known = 0
    prompt = 0
    completion = 0
    total = 0
    for record in records:
        values = token_values(record)
        if values is None:
            if record.status == "finished" and record.usage_known:
                unreadable += 1
            continue
        known += 1
        prompt += values[0]
        completion += values[1]
        total += values[2]
    unknown = attempts - open_attempts - known - unreadable
    complete = is_complete(
        attempts=attempts,
        unknown=unknown,
        open_attempts=open_attempts,
        unreadable=unreadable,
    )
    return {
        "attempts_total": attempts,
        "attempts_known": known,
        "attempts_unknown": unknown,
        "attempts_open": open_attempts,
        "attempts_unreadable": unreadable,
        "tokens": {
            "prompt": prompt if known else None,
            "completion": completion if known else None,
            "total": total if known else None,
            "complete": complete,
        },
    }


def _chronological(records: Sequence[AttemptRecord]) -> list[AttemptRecord]:
    def key(item: AttemptRecord) -> tuple[str, int, str]:
        return (item.started_at or "", item.attempt_no, item.attempt_id)

    return sorted(records, key=key)


def latest_record(records: Sequence[AttemptRecord]) -> AttemptRecord | None:
    """The most recent attempt row in the scope, or None when there is none."""

    ordered = _chronological(records)
    return ordered[-1] if ordered else None


def latest_attempt(
    records: Sequence[AttemptRecord],
    repair_map: Mapping[str, str],
) -> AttemptSummary | None:
    """The most recent attempt in the scope, or None when there is none."""

    record = latest_record(records)
    if record is None:
        return None
    previous, following = _neighbours_by_context(records)
    return summarize(
        record,
        repair_map,
        previous=previous.get(record.attempt_id),
        following=following.get(record.attempt_id),
    )


def _neighbours_by_context(
    records: Sequence[AttemptRecord],
) -> tuple[dict[str, AttemptRecord], dict[str, AttemptRecord]]:
    """For every attempt id, the attempt before and after it in its Context."""

    grouped: dict[str, list[AttemptRecord]] = {}
    for record in records:
        grouped.setdefault(record.context_id, []).append(record)
    previous: dict[str, AttemptRecord] = {}
    following: dict[str, AttemptRecord] = {}
    for items in grouped.values():
        ordered = sorted(items, key=lambda item: (item.attempt_no, item.attempt_id))
        for index, record in enumerate(ordered):
            if index:
                previous[record.attempt_id] = ordered[index - 1]
            if index + 1 < len(ordered):
                following[record.attempt_id] = ordered[index + 1]
    return previous, following


def summarize(
    record: AttemptRecord,
    repair_map: Mapping[str, str],
    *,
    previous: AttemptRecord | None,
    following: AttemptRecord | None = None,
) -> AttemptSummary:
    values = token_values(record)
    elapsed = duration_ms(record)
    reason = repair_map.get(record.attempt_id)
    if reason is not None and reason not in REPAIR_REASONS:
        reason = "unknown"
    same_before = (
        previous is not None
        and previous.request_hash == record.request_hash
        and previous.context_id == record.context_id
    )
    same_after = (
        following is not None
        and following.request_hash == record.request_hash
        and following.context_id == record.context_id
    )
    # The exact body was sent more than once in this Context: a transport
    # retry on either side of the pair.  It is never a repair.
    resend = same_before or same_after
    if reason is not None:
        kind = KIND_REPAIR
        repair: bool | None = True
    elif same_before:
        kind = KIND_TRANSPORT_RETRY
        repair = False
    elif record.attempt_no == 1:
        kind = KIND_PRIMARY
        repair = False
    elif same_after:
        kind = KIND_TRANSPORT_RETRY
        repair = False
    else:
        kind = KIND_UNMARKED
        repair = None
    completion = values[1] if values is not None else None
    return AttemptSummary(
        attempt_id=record.attempt_id,
        attempt_no=record.attempt_no,
        stage=record.stage_kind,
        purpose=record.purpose,
        ordinal=record.ordinal,
        status=record.status,
        outcome=record.outcome,
        finish_reason=record.finish_reason,
        error_label=record.error_label,
        usage_known=values is not None,
        prompt_tokens=values[0] if values is not None else None,
        completion_tokens=completion,
        total_tokens=values[2] if values is not None else None,
        started_at=record.started_at,
        finished_at=record.finished_at,
        duration_ms=elapsed,
        tokens_per_second=_speed(completion, elapsed),
        repair=repair,
        repair_reason=reason if repair else None,
        resend_same_body=resend,
        kind=kind,
    )


def summarize_scope(
    records: Sequence[AttemptRecord],
    repair_map: Mapping[str, str],
) -> dict[str, Any]:
    """Aggregate plus per-attempt summaries for one scope, in stable order."""

    previous, following = _neighbours_by_context(records)
    ordered = sorted(
        records,
        key=lambda item: (
            0 if item.stage_kind == "content" else 1,
            item.ordinal,
            item.context_id,
            item.attempt_no,
            item.attempt_id,
        ),
    )
    attempts = [
        summarize(
            item,
            repair_map,
            previous=previous.get(item.attempt_id),
            following=following.get(item.attempt_id),
        )
        for item in ordered
    ]
    scope = aggregate(records)
    repairs = [
        {
            "stage": item.stage,
            "attempt_id": item.attempt_id,
            "attempt_no": item.attempt_no,
            "reason": item.repair_reason,
        }
        for item in attempts
        if item.repair is True
    ]
    scope["attempts"] = [item.to_dict() for item in attempts]
    scope["repairs"] = repairs
    scope["counts"] = {
        "repairs": len(repairs),
        "repair_unknown": sum(1 for item in attempts if item.repair is None),
        "transport_retries": sum(
            1 for item in attempts if item.kind == KIND_TRANSPORT_RETRY
        ),
        "primary": sum(1 for item in attempts if item.kind == KIND_PRIMARY),
    }
    scope["latest_request"] = latest_attempt(records, repair_map)
    if scope["latest_request"] is not None:
        scope["latest_request"] = scope["latest_request"].to_dict()
    return scope


def complete_from_aggregate(aggregate_row: Mapping[str, Any]) -> dict[str, Any]:
    """Turn the SQL aggregate row into the public token block.

    The SQL uses ``json_type`` guards so a row that claims ``usage_known`` but
    stores a non-integer number is counted as unreadable, never summed as 0.
    """

    attempts = int(aggregate_row["attempts_total"])
    known = int(aggregate_row["attempts_known"])
    open_attempts = int(aggregate_row["attempts_open"])
    unreadable = int(aggregate_row["attempts_unreadable"])
    unknown = attempts - open_attempts - known - unreadable
    return {
        "prompt": int(aggregate_row["prompt_tokens"]) if known else None,
        "completion": int(aggregate_row["completion_tokens"]) if known else None,
        "total": int(aggregate_row["total_tokens"]) if known else None,
        "complete": is_complete(
            attempts=attempts,
            unknown=unknown,
            open_attempts=open_attempts,
            unreadable=unreadable,
        ),
    }


def context_bytes(budget: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The provable Context budget: PCS-rendered bytes and the PCS cap.

    This is deliberately *not* a model context-window percentage: no token or
    window measurement exists in this host, so none is manufactured here.
    """

    if not isinstance(budget, Mapping):
        return None
    used = budget.get("used_bytes")
    limit = budget.get("limit_bytes")
    if type(used) is not int or type(limit) is not int or limit <= 0:
        return None
    return {"used_bytes": used, "limit_bytes": limit}


__all__ = [
    "AttemptRecord",
    "AttemptSummary",
    "KIND_PRIMARY",
    "KIND_REPAIR",
    "KIND_TRANSPORT_RETRY",
    "KIND_UNMARKED",
    "REPAIR_REASONS",
    "USAGE_KEYS",
    "aggregate",
    "complete_from_aggregate",
    "context_bytes",
    "duration_ms",
    "is_complete",
    "latest_attempt",
    "latest_record",
    "summarize",
    "summarize_scope",
    "token_values",
]
