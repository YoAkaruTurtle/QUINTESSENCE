"""RG04: turn postprocessing status and the bounded delivery sweep.

The durable work item is the terminal turn row itself: RG02/03 already commit
the turn state, the public message and the interaction result atomically, so a
crash after the final reply leaves a recoverable source.  ``memory_delivery``
remains the durable outbox for the projection/delivery states.  This module
adds no table and no second memory authority: it only reads the existing rows
and asks the existing :class:`ExperienceBridge` to project/deliver.

The sweep is deliberately bounded and honest:

* it projects at most ``max_turns`` turns and ``max_refs`` refs per slice;
* it delivers at most ``deliver_limit`` due rows with a short capture cap;
* a skipped/blocked/unknown row keeps its state and is never reported as
  delivered;
* the caller (the desktop bridge) checks the foreground-send gate before it
  calls this module, so a sweep never queues ahead of an already pending send.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...store.experience import TERMINAL_TURN_STATES

#: Memory states exposed to the UI.  ``not_configured``/``not_eligible`` are
#: about this host/turn, not about a failed delivery.
MEMORY_NOT_CONFIGURED = "not_configured"
MEMORY_NOT_ELIGIBLE = "not_eligible"
MEMORY_PENDING = "pending"
MEMORY_OUTCOME_UNKNOWN = "outcome_unknown"
MEMORY_DELIVERED = "delivered"
MEMORY_NEEDS_REVIEW = "needs_review"
MEMORY_BLOCKED = "blocked"
MEMORY_OBSOLETE = "obsolete"

_DELIVERED_STATES = frozenset({"applied", "verified"})
_PENDING_STATES = frozenset({"pending"})
_UNKNOWN_ERRORS = frozenset({"unknown", "timeout"})


@dataclass(frozen=True)
class RefPostprocess:
    """One eligible source of a turn and its current delivery state."""

    ref: dict[str, Any]
    role: str
    kind: str
    state: str
    detail: str | None = None
    next_retry_at: str | None = None
    delivery_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": dict(self.ref),
            "role": self.role,
            "kind": self.kind,
            "state": self.state,
            "detail": self.detail,
            "next_retry_at": self.next_retry_at,
            "delivery_id": self.delivery_id,
        }


@dataclass(frozen=True)
class TurnPostprocess:
    """The two independent UI dimensions: transcript saved vs memory state."""

    turn_id: str
    turn_state: str
    completed_at: str | None
    transcript_saved: bool
    memory_state: str
    memory_detail: str | None
    pending: int
    delivered: int
    refs: tuple[RefPostprocess, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "turn_state": self.turn_state,
            "completed_at": self.completed_at,
            "transcript_saved": self.transcript_saved,
            "memory": {
                "state": self.memory_state,
                "detail": self.memory_detail,
                "pending": self.pending,
                "delivered": self.delivered,
                "refs": [ref.as_dict() for ref in self.refs],
            },
        }


@dataclass(frozen=True)
class SweepReport:
    scanned_turns: int = 0
    projected_refs: int = 0
    turns_projected: int = 0
    delivery_attempted: int = 0
    delivered: int = 0
    pending: int = 0
    needs_review: int = 0
    blocked: int = 0
    obsolete: int = 0
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned_turns": self.scanned_turns,
            "projected_refs": self.projected_refs,
            "turns_projected": self.turns_projected,
            "delivery_attempted": self.delivery_attempted,
            "delivered": self.delivered,
            "pending": self.pending,
            "needs_review": self.needs_review,
            "blocked": self.blocked,
            "obsolete": self.obsolete,
            "errors": list(self.errors),
        }


def ref_key(ref: dict[str, Any]) -> tuple[str, str, int]:
    """Normalise a store ref dictionary for the by-ref lookups."""

    if ref.get("ref_type") == "collab_input":
        return (
            "collab_input",
            str(ref["input_id"]),
            int(ref["content_version"]),
        )
    return (
        "collab_artifact",
        str(ref["artifact_id"]),
        int(ref["artifact_version"]),
    )


def _mapping_state(
    store: Any,
    namespace: str,
    ref: dict[str, Any],
) -> tuple[str, str | None, str | None, str | None]:
    """Return ``(state, detail, next_retry_at, delivery_id)`` for one ref."""

    ref_type, ref_id, ref_version = ref_key(ref)
    mapping = store.projection_for_ref(
        namespace,
        ref_type=ref_type,
        ref_id=ref_id,
        ref_version=ref_version,
    )
    if mapping is None:
        gap = store.bridge_gap_for_ref(
            namespace,
            ref_type=ref_type,
            ref_id=ref_id,
            ref_version=ref_version,
        )
        if gap is not None:
            reason = str(gap.get("reason") or "unprojectable")
            if reason == "deleted":
                return (MEMORY_OBSOLETE, reason, None, None)
            return (MEMORY_BLOCKED, reason, None, None)
        return (MEMORY_PENDING, "not_projected", None, None)
    if mapping.get("invalidated_at") is not None:
        return (MEMORY_OBSOLETE, str(mapping.get("invalidation_reason") or "invalidated"), None, None)
    delivery = store.delivery_for_ref(
        namespace,
        ref_type=ref_type,
        ref_id=ref_id,
        ref_version=ref_version,
    )
    if delivery is None:
        return (MEMORY_PENDING, "outbox_missing", None, None)
    state = str(delivery.get("state"))
    error = delivery.get("last_error")
    if state == "pending" and error in _UNKNOWN_ERRORS:
        state = MEMORY_OUTCOME_UNKNOWN
    detail = None if error is None else str(error)
    return (
        state,
        detail,
        None if delivery.get("next_retry_at") is None else str(delivery["next_retry_at"]),
        str(delivery.get("delivery_id")),
    )


def _aggregate(states: list[str]) -> tuple[str, str | None]:
    if not states:
        return (MEMORY_NOT_ELIGIBLE, "no_eligible_refs")
    unique = set(states)
    if MEMORY_NEEDS_REVIEW in unique:
        return (MEMORY_NEEDS_REVIEW, "some_refs_need_review")
    if MEMORY_OUTCOME_UNKNOWN in unique:
        return (MEMORY_OUTCOME_UNKNOWN, "capture_result_unknown")
    if MEMORY_BLOCKED in unique:
        return (MEMORY_BLOCKED, "some_refs_blocked")
    if MEMORY_OBSOLETE in unique:
        return (
            MEMORY_OBSOLETE,
            "all_refs_obsolete" if unique == {MEMORY_OBSOLETE} else "some_refs_obsolete",
        )
    if MEMORY_PENDING in unique:
        return (MEMORY_PENDING, "some_refs_pending")
    if unique <= _DELIVERED_STATES:
        return (MEMORY_DELIVERED, None)
    return (MEMORY_PENDING, "unknown_delivery_state")


def turn_postprocess_status(
    store: Any,
    *,
    namespace: str,
    host_instance: str,
    turn_id: str,
    access: Any,
    configured: bool,
) -> TurnPostprocess:
    """Derive the per-turn status from terminal turn + outbox rows.

    The function never returns body text; only refs, states and timestamps.
    """

    turn = store.turn_for_host(
        namespace=namespace, host_instance=host_instance, turn_id=turn_id
    )
    terminal = turn.state in TERMINAL_TURN_STATES
    refs = (
        store.turn_experience_refs(access, namespace=namespace, turn_id=turn.turn_id)
        if terminal
        else ()
    )
    ref_statuses: list[RefPostprocess] = []
    for ref in refs:
        state, detail, next_retry_at, delivery_id = _mapping_state(
            store, namespace, ref
        )
        ref_statuses.append(
            RefPostprocess(
                ref=dict(ref),
                role="user" if ref.get("ref_type") == "collab_input" else "assistant",
                kind="user"
                if ref.get("ref_type") == "collab_input"
                else "assistant_message",
                state=state,
                detail=detail,
                next_retry_at=next_retry_at,
                delivery_id=delivery_id,
            )
        )
    if not configured:
        memory_state, detail = (MEMORY_NOT_CONFIGURED, "memory_bridge_disabled")
    elif not terminal:
        memory_state, detail = (MEMORY_PENDING, "turn_running")
    elif not refs:
        memory_state, detail = (MEMORY_NOT_ELIGIBLE, "no_eligible_refs")
    else:
        memory_state, detail = _aggregate([item.state for item in ref_statuses])
        if memory_state == MEMORY_PENDING and all(
            item.detail == "not_projected" for item in ref_statuses
        ):
            detail = "not_projected"
    pending = sum(
        1
        for item in ref_statuses
        if item.state in (MEMORY_PENDING, MEMORY_OUTCOME_UNKNOWN)
    )
    delivered = sum(1 for item in ref_statuses if item.state in _DELIVERED_STATES)
    return TurnPostprocess(
        turn_id=turn.turn_id,
        turn_state=turn.state,
        completed_at=turn.completed_at,
        transcript_saved=terminal,
        memory_state=memory_state,
        memory_detail=detail,
        pending=pending,
        delivered=delivered,
        refs=tuple(ref_statuses),
    )


def run_postprocess_sweep(
    store: Any,
    bridge: Any,
    *,
    access: Any,
    namespace: str,
    host_instance: str,
    max_turns: int = 2,
    max_refs: int = 8,
    deliver_limit: int = 1,
    capture_timeout_seconds: float = 1.5,
) -> SweepReport:
    """Project missing eligible refs of terminal turns, then deliver one due row.

    The caller must have checked the foreground-send gate.  The sweep never
    raises for a per-row bridge refusal: the bridge already records the state
    and returns counters, so the worker stays alive and the UI can explain the
    outcome.
    """

    if type(max_turns) is not int or max_turns < 0:
        raise ValueError("max_turns must be a non-negative integer")
    if type(max_refs) is not int or max_refs < 1:
        raise ValueError("max_refs must be a positive integer")
    if type(deliver_limit) is not int or deliver_limit < 1:
        raise ValueError("deliver_limit must be a positive integer")
    scanned = 0
    projected = 0
    turns_projected = 0
    errors: list[str] = []
    for turn in store.list_terminal_turns(
        namespace=namespace, host_instance=host_instance, limit=100
    ):
        if turns_projected >= max_turns:
            break
        scanned += 1
        refs = store.turn_experience_refs(
            access, namespace=namespace, turn_id=turn.turn_id
        )
        missing = []
        for ref in refs:
            ref_type, ref_id, ref_version = ref_key(ref)
            if (
                store.projection_for_ref(
                    namespace,
                    ref_type=ref_type,
                    ref_id=ref_id,
                    ref_version=ref_version,
                )
                is None
            ):
                missing.append(ref)
        if not missing:
            continue
        try:
            result = bridge.sync_refs(
                access, namespace=namespace, refs=missing, limit=max_refs
            )
        except Exception as exc:  # noqa: BLE001 - a bounded slice state
            errors.append(type(exc).__name__)
            continue
        projected += result.projected
        turns_projected += 1
        errors.extend(gap.get("reason") for gap in result.gaps if gap.get("reason"))
    delivery = bridge.deliver(
        access,
        namespace=namespace,
        limit=deliver_limit,
        capture_timeout_seconds=capture_timeout_seconds,
    )
    return SweepReport(
        scanned_turns=scanned,
        projected_refs=projected,
        turns_projected=turns_projected,
        delivery_attempted=delivery.claimed,
        delivered=delivery.applied,
        pending=delivery.pending,
        needs_review=delivery.needs_review,
        blocked=delivery.blocked,
        obsolete=delivery.obsolete,
        errors=tuple(errors),
    )


__all__ = [
    "MEMORY_BLOCKED",
    "MEMORY_DELIVERED",
    "MEMORY_NEEDS_REVIEW",
    "MEMORY_NOT_CONFIGURED",
    "MEMORY_NOT_ELIGIBLE",
    "MEMORY_OBSOLETE",
    "MEMORY_OUTCOME_UNKNOWN",
    "MEMORY_PENDING",
    "RefPostprocess",
    "SweepReport",
    "TurnPostprocess",
    "ref_key",
    "run_postprocess_sweep",
    "turn_postprocess_status",
]
