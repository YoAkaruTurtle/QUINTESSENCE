"""J05: bounded, derived Attention view.

Attention is a projection, not a second authority.  It persists no score, no
candidate and no "current attention" row; it reads current Questions handed to
:func:`build_attention` and returns exact refs plus reason codes.  The caller
must pass only Questions that already passed the Question domain gate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ...foundation.errors import InvalidInput
from ...foundation.jsonutil import canonical_json_text
from .questions import MAX_ATTENTION_SOURCES, MAX_LIST_LIMIT, QuestionInvalid

_PRIORITY_RANK = {"high": 3, "normal": 2, "low": 1}
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"


@dataclass(frozen=True)
class AttentionRequest:
    """One bounded, on-demand attention request."""

    scope: Mapping[str, Any] | None = None
    new_source_refs: Sequence[Mapping[str, Any]] = ()
    now: str | None = None
    limit: int = 20
    include_paused: bool = False

    def __post_init__(self) -> None:
        if self.scope is not None and not isinstance(self.scope, Mapping):
            raise QuestionInvalid("attention scope must be a mapping or null")
        if isinstance(self.new_source_refs, (str, bytes)) or not isinstance(
            self.new_source_refs, Sequence
        ):
            raise QuestionInvalid("new_source_refs must be an array")
        if len(self.new_source_refs) > MAX_ATTENTION_SOURCES:
            raise QuestionInvalid(
                f"new_source_refs must have at most {MAX_ATTENTION_SOURCES} items"
            )
        if type(self.limit) is not int or not 1 <= self.limit <= MAX_LIST_LIMIT:
            raise QuestionInvalid(f"attention limit must be 1..{MAX_LIST_LIMIT}")
        if type(self.include_paused) is not bool:
            raise QuestionInvalid("include_paused must be a boolean")
        if self.now is not None:
            _parse_timestamp(self.now, "attention.now")


@dataclass(frozen=True)
class AttentionItem:
    """One exact Question ref and why it is in the bounded view."""

    ref: Any
    status: str
    reason_codes: tuple[str, ...]
    priority: str
    review_after: str | None
    paused: bool
    actionable: bool


@dataclass(frozen=True)
class AttentionView:
    items: tuple[AttentionItem, ...]
    truncated: bool
    state_revision: int
    generated_at: str


def _parse_timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise QuestionInvalid(f"{field} must be an RFC 3339 timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError as exc:
        raise QuestionInvalid(f"{field} must be a valid RFC 3339 timestamp") from exc


def _ref_key(ref: Mapping[str, Any]) -> str:
    return canonical_json_text(dict(ref))


def build_attention(
    request: AttentionRequest,
    views: Sequence[Any],
    *,
    now: str,
    new_source_refs: Sequence[Mapping[str, Any]],
    state_revision: int,
) -> AttentionView:
    """Build one bounded Attention view from already-gated Question views."""

    if not isinstance(request, AttentionRequest):
        raise QuestionInvalid("request must be an AttentionRequest")
    generated = now if request.now is None else request.now
    now_dt = _parse_timestamp(generated, "attention.now")
    source_keys = {_ref_key(ref) for ref in new_source_refs}

    items: list[AttentionItem] = []
    for view in views:
        if view.paused and not request.include_paused:
            continue
        reasons: list[str] = []
        if view.review_after is not None:
            due_at = _parse_timestamp(view.review_after, "review_after")
            evaluated_at = (
                None
                if view.last_evaluated_at is None
                else _parse_timestamp(view.last_evaluated_at, "last_evaluated_at")
            )
            if due_at <= now_dt and (evaluated_at is None or evaluated_at < due_at):
                reasons.append("review_due")
        consumed = {_ref_key(ref) for ref in view.consumed_trigger_refs}
        for trigger in view.trigger_spec.get("source_refs", ()):
            key = _ref_key(trigger)
            if key in source_keys and key not in consumed:
                reasons.append("source_match")
                break
        if not reasons and not view.paused:
            continue
        items.append(
            AttentionItem(
                ref=view.ref,
                status=view.status,
                reason_codes=tuple(reasons),
                priority=view.priority,
                review_after=view.review_after,
                paused=view.paused,
                actionable=not view.paused,
            )
        )

    items.sort(
        key=lambda item: (
            -_PRIORITY_RANK.get(item.priority, 0),
            item.review_after or "9999-12-31T23:59:59Z",
            item.ref.record_id,
            item.ref.record_version,
        )
    )
    return AttentionView(
        items=tuple(items[: request.limit]),
        truncated=len(items) > request.limit,
        state_revision=state_revision,
        generated_at=generated,
    )


__all__ = [
    "AttentionItem",
    "AttentionRequest",
    "AttentionView",
    "build_attention",
]
