"""Public DTOs for the RG02 conversation bridge methods.

Only explicit fields leave the host: no payload references, no rendered input,
no provider body, no path and no authority material.
"""

from __future__ import annotations

from typing import Any

from ..store.conversation import (
    ConversationPage,
    ConversationRow,
    ConversationSummary,
    MessagePage,
    MessageView,
    StepPage,
    StepView,
    TurnCreation,
    TurnDetail,
    TurnEventPage,
    TurnEventRow,
    TurnResultView,
    TurnRow,
)


def conversation_dict(row: ConversationRow) -> dict[str, Any]:
    return {
        "conversation_id": row.conversation_id,
        "namespace": row.namespace,
        "mode": row.mode,
        "title": row.title,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def conversation_summary_dict(item: ConversationSummary) -> dict[str, Any]:
    row = item.conversation
    return {
        "conversation_id": row.conversation_id,
        "mode": row.mode,
        "title": row.title,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "turn_count": item.turn_count,
        "last_turn_state": item.last_turn_state,
    }


def conversation_page_dict(page: ConversationPage) -> dict[str, Any]:
    return {
        "conversations": [
            conversation_summary_dict(item) for item in page.conversations
        ],
        "next_cursor": page.next_cursor,
    }


def turn_dict(detail: TurnDetail) -> dict[str, Any]:
    turn = detail.turn
    return {
        "turn_id": turn.turn_id,
        "conversation_id": turn.conversation_id,
        "turn_no": turn.turn_no,
        "state": turn.state,
        "mode": turn.mode,
        "created_at": turn.created_at,
        "updated_at": turn.updated_at,
        "started_at": turn.started_at,
        "completed_at": turn.completed_at,
        "failure_reason": detail.failure_reason,
    }


def message_dict(view: MessageView) -> dict[str, Any]:
    document = {
        "message_id": view.message_id,
        "turn_id": view.turn_id,
        "seq": view.seq,
        "kind": view.kind,
        "role": "user" if view.kind == "user" else "assistant",
        "text": view.text,
        "redacted": view.redacted,
        "created_at": view.created_at,
    }
    if len(view.messages) > 1:
        # RG08: only a genuinely split reply gains the ordered bubble list;
        # a legacy single reply keeps the exact old DTO shape.
        document["messages"] = list(view.messages)
    return document


def message_page_dict(page: MessagePage) -> dict[str, Any]:
    return {
        "messages": [message_dict(item) for item in page.messages],
        "next_before": (
            None
            if page.next_before is None
            else {"turn_no": page.next_before[0], "seq": page.next_before[1]}
        ),
    }


def turn_result_dict(view: TurnResultView) -> dict[str, Any]:
    turn = view.turn
    return {
        "turn_id": turn.turn_id,
        "conversation_id": turn.conversation_id,
        "turn_no": turn.turn_no,
        "state": turn.state,
        "terminal": turn.state in (
            "completed",
            "failed",
            "cancelled",
            "interrupted",
        ),
        "final": None if view.final is None else message_dict(view.final),
        "wait_question": (
            None
            if view.wait_question is None
            else message_dict(view.wait_question)
        ),
        "failure_reason": view.failure_reason,
        "created_at": turn.created_at,
        "completed_at": turn.completed_at,
    }


def accepted_dict(creation: TurnCreation) -> dict[str, Any]:
    turn = creation.turn
    return {
        "conversation_id": turn.conversation_id,
        "turn_id": turn.turn_id,
        "turn_no": turn.turn_no,
        "client_request_id": turn.client_request_id,
        "state": turn.state,
        "reused": creation.reused,
    }




#: The only payload fields an event DTO may expose.  Anything else (a raw
#: payload, a rendered input, a provider body) never leaves the host.
_EVENT_PAYLOAD_KEYS = frozenset(
    {
        "step_id",
        "step_no",
        "tool",
        "arguments_digest",
        "status",
        "duration_ms",
        "result_artifact_id",
        "result_artifact_version",
        "sizes",
        "stage",
        "message_id",
        "kind",
        "message_seq",
        "reason",
        "commentary_omitted",
        "commentary_source",
        "state",
        "attempt_ref",
        "finish_reason",
    }
)

#: The only transient fields the in-progress ring may add on top of the
#: persisted payload (already-published public text, never a draft).
_LIVE_EVENT_KEYS = frozenset({"text", "arguments", "result_excerpt"})


def turn_event_dict(
    row: TurnEventRow, live_payload: Any | None = None
) -> dict[str, Any]:
    document = {
        "seq": row.seq,
        "event_id": row.event_id,
        "type": row.kind,
        "at": row.created_at,
    }
    for key in _EVENT_PAYLOAD_KEYS:
        if key in row.payload:
            document[key] = row.payload[key]
    if live_payload:
        for key in _LIVE_EVENT_KEYS:
            if key in live_payload:
                document[key] = live_payload[key]
    return document


def turn_event_page_dict(
    page: TurnEventPage,
    live: dict[int, Any] | None = None,
) -> dict[str, Any]:
    document = {
        "events": [
            turn_event_dict(row, None if live is None else live.get(row.seq))
            for row in page.events
        ],
        "next_seq": page.next_seq,
        "terminal": page.terminal,
    }
    return document


def step_dict(view: StepView) -> dict[str, Any]:
    row = view.step
    return {
        "step_id": row.step_id,
        "step_no": row.step_no,
        "tool_name": row.tool_name,
        "status": row.status,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "error_label": row.error_label,
        "result_artifact_ref": (
            None
            if row.result_artifact_id is None
            else {
                "ref_type": "collab_artifact",
                "namespace": row.namespace,
                "artifact_id": row.result_artifact_id,
                "artifact_version": row.result_artifact_version,
            }
        ),
        "arguments": view.arguments,
        "result": {
            "available": view.result_available,
            "excerpt": view.result_excerpt,
            "truncated": view.result_truncated,
        },
    }


def step_page_dict(page: StepPage) -> dict[str, Any]:
    return {
        "steps": [step_dict(view) for view in page.steps],
        "next_after_step_no": page.next_after_step_no,
    }


#: The attempt fields the process panel may render.  Never a body, a request
#: payload, an authorization reference or a provider response.
_ATTEMPT_KEYS = (
    "attempt_id",
    "attempt_no",
    "stage",
    "purpose",
    "ordinal",
    "status",
    "outcome",
    "finish_reason",
    "error_label",
    "usage_known",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "started_at",
    "finished_at",
    "duration_ms",
    "tokens_per_second",
    "repair",
    "repair_reason",
    "resend_same_body",
    "kind",
)

_TOKENS_KEYS = ("prompt", "completion", "total", "complete")


def _tokens_dict(value: Any) -> dict[str, Any]:
    return {key: value.get(key) for key in _TOKENS_KEYS}


def _attempt_dict(view: Any) -> dict[str, Any]:
    return {key: view.get(key) for key in _ATTEMPT_KEYS}


def _bytes_dict(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {"used_bytes": value.get("used_bytes"), "limit_bytes": value.get("limit_bytes")}


def _context_dict(view: Any) -> dict[str, Any]:
    return {
        "context_id": view["context_id"],
        "ordinal": view["ordinal"],
        "purpose": view["purpose"],
        "created_at": view["created_at"],
        "bytes": _bytes_dict(view.get("bytes")),
        "attempts": [_attempt_dict(item) for item in view.get("attempts") or ()],
    }


def _latest_context_dict(view: Any) -> dict[str, Any] | None:
    if not isinstance(view, dict):
        return None
    return {
        "context_id": view["context_id"],
        "purpose": view["purpose"],
        "stage": view["stage"],
        "bytes": _bytes_dict(view.get("bytes")),
    }


def _usage_turn_dict(view: Any) -> dict[str, Any]:
    return {
        "turn_id": view["turn_id"],
        "turn_no": view["turn_no"],
        "state": view["state"],
        "attempts_total": view["attempts_total"],
        "attempts_known": view["attempts_known"],
        "attempts_unknown": view["attempts_unknown"],
        "attempts_open": view["attempts_open"],
        "attempts_unreadable": view["attempts_unreadable"],
        "tokens": _tokens_dict(view["tokens"]),
        "counts": dict(view["counts"]),
        "latest_request": (
            None
            if view.get("latest_request") is None
            else _attempt_dict(view["latest_request"])
        ),
        "latest_context": _latest_context_dict(view.get("latest_context")),
    }


def usage_dict(view: Any) -> dict[str, Any]:
    """The public ``conversation.usage`` projection."""

    return {
        "conversation_id": view["conversation_id"],
        "turns_total": view["turns_total"],
        "turns_with_attempts": view["turns_with_attempts"],
        "attempts_total": view["attempts_total"],
        "attempts_known": view["attempts_known"],
        "attempts_unknown": view["attempts_unknown"],
        "attempts_open": view["attempts_open"],
        "attempts_unreadable": view["attempts_unreadable"],
        "counts": dict(view["counts"]),
        "tokens": _tokens_dict(view["tokens"]),
        "latest_request": (
            None
            if view.get("latest_request") is None
            else _attempt_dict(view["latest_request"])
        ),
        "latest_context": _latest_context_dict(view.get("latest_context")),
        "turn": None if view.get("turn") is None else _usage_turn_dict(view["turn"]),
    }


def trace_dict(view: Any) -> dict[str, Any]:
    """The public ``turn.trace`` projection: stages, attempts, final origin."""

    turn = view["turn"]
    final = view.get("final")
    return {
        "turn": {
            "turn_id": turn["turn_id"],
            "conversation_id": turn["conversation_id"],
            "turn_no": turn["turn_no"],
            "state": turn["state"],
            "created_at": turn["created_at"],
            "started_at": turn["started_at"],
            "completed_at": turn["completed_at"],
            "failure_reason": turn["failure_reason"],
        },
        "stages": [
            {
                "stage_id": stage["stage_id"],
                "kind": stage["kind"],
                "status": stage["status"],
                "skip_reason": stage["skip_reason"],
                "policy_version": stage["policy_version"],
                "created_at": stage["created_at"],
                "completed_at": stage["completed_at"],
                "tool_steps": stage["tool_steps"],
                "tool_steps_by_status": dict(stage["tool_steps_by_status"]),
                "contexts": [_context_dict(item) for item in stage["contexts"]],
            }
            for stage in view["stages"]
        ],
        "final": (
            None
            if final is None
            else {
                "message_id": final["message_id"],
                "seq": final["seq"],
                "published_at": final["published_at"],
                "attempt_id": final["attempt_id"],
                "stage_kind": final["stage_kind"],
                "source": final["source"],
                "fallback": final["fallback"],
                "fallback_reason": final["fallback_reason"],
            }
        ),
        "attempts_total": view["attempts_total"],
        "attempts_known": view["attempts_known"],
        "attempts_unknown": view["attempts_unknown"],
        "attempts_open": view["attempts_open"],
        "attempts_unreadable": view["attempts_unreadable"],
        "counts": dict(view["counts"]),
        "tokens": _tokens_dict(view["tokens"]),
        "repairs": [
            {
                "stage": item["stage"],
                "attempt_id": item["attempt_id"],
                "attempt_no": item["attempt_no"],
                "reason": item["reason"],
            }
            for item in view["repairs"]
        ],
        "latest_request": (
            None
            if view.get("latest_request") is None
            else _attempt_dict(view["latest_request"])
        ),
        "latest_context": _latest_context_dict(view.get("latest_context")),
        "truncated": view["truncated"],
    }


__all__ = [
    "trace_dict",
    "usage_dict",
    "accepted_dict",
    "conversation_dict",
    "conversation_page_dict",
    "conversation_summary_dict",
    "message_dict",
    "message_page_dict",
    "step_dict",
    "step_page_dict",
    "turn_dict",
    "turn_event_dict",
    "turn_event_page_dict",
    "turn_result_dict",
]
