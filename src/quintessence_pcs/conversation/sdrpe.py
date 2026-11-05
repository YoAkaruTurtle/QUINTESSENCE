"""RG07: the host-owned off/on/auto policy for the expression stage.

The module is pure: it never reads the store, never calls a model and never
writes a row.  It only answers two questions:

* which mode a configuration and a protocol actually resolve to at turn
  acceptance (:class:`SdrpePlan`);
* whether one finished content answer may be handed to the expression stage
  (:func:`decide`).

``auto`` is **reserved**: it is parsed (so the setting can be shown as
"reserved / unavailable") but it never runs a trigger policy.  A turn under
``auto`` records a real ``skipped`` stage with ``auto_not_implemented``, so no
configuration can look like it implements automatic triggering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

SDRPE_POLICY_VERSION = "sdrpe.v3"
SDRPE_MODES = ("off", "on", "auto")
SDRPE_EFFECTIVE_MODES = ("off", "on")

#: ``auto`` is a config position, not an implemented trigger policy.
AUTO_IMPLEMENTED = False

#: One request of every turn's budget is kept for the expression stage.
EXPRESSION_REQUEST_RESERVE = 1
MIN_MODEL_REQUESTS_FOR_EXPRESSION = EXPRESSION_REQUEST_RESERVE + 1

#: Below this much remaining wall clock the stage is skipped, not started.
MIN_EXPRESSION_WALL_SECONDS = 15.0

EXPRESSION_STAGE_KIND = "expression"

SKIP_MODE_OFF = "mode_off"
SKIP_AUTO_NOT_IMPLEMENTED = "auto_not_implemented"
SKIP_PROTOCOL_V1 = "protocol_v1"
SKIP_WORK_MODE = "work_mode"
SKIP_WAIT_QUESTION = "wait_question"
SKIP_NO_RESERVE = "budget_no_reserve"
SKIP_BUDGET = "budget_model_requests"
SKIP_WALL = "wall_clock"
SKIP_DRAFT_TOO_LARGE = "draft_too_large"
SKIP_CONTEXT_TOO_LARGE = "context_too_large"

SKIP_REASONS = (
    SKIP_MODE_OFF,
    SKIP_AUTO_NOT_IMPLEMENTED,
    SKIP_PROTOCOL_V1,
    SKIP_WORK_MODE,
    SKIP_WAIT_QUESTION,
    SKIP_NO_RESERVE,
    SKIP_BUDGET,
    SKIP_WALL,
    SKIP_DRAFT_TOO_LARGE,
    SKIP_CONTEXT_TOO_LARGE,
)


@dataclass(frozen=True)
class SdrpePlan:
    """The per-turn resolution of the configured mode.

    ``requested_mode`` is what the configuration said; ``effective_mode`` is
    what this build can actually do.  A missing ``resolved_reason`` means the
    requested mode was used as-is.
    """

    requested_mode: str
    effective_mode: str
    policy_version: str = SDRPE_POLICY_VERSION
    resolved_reason: str | None = None

    def __post_init__(self) -> None:
        if self.requested_mode not in SDRPE_MODES:
            raise ValueError("requested_mode must be off, on or auto")
        if self.effective_mode not in SDRPE_EFFECTIVE_MODES:
            raise ValueError("effective_mode must be off or on")
        if self.effective_mode == "on" and self.requested_mode != "on":
            raise ValueError("only an explicitly requested 'on' can be effective")
        if self.resolved_reason is not None and self.resolved_reason not in SKIP_REASONS:
            raise ValueError("resolved_reason is not a frozen skip reason")

    def to_strategy(self) -> dict[str, Any]:
        """The host-owned per-turn snapshot (never supplied by the UI)."""

        return {
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "policy_version": self.policy_version,
            "resolved_reason": self.resolved_reason,
        }

    @classmethod
    def resolve(
        cls, requested_mode: str, *, protocol_version: str | None
    ) -> "SdrpePlan":
        """Resolve one configured mode against the protocol this build speaks."""

        if requested_mode not in SDRPE_MODES:
            raise ValueError("mode must be off, on or auto")
        if protocol_version != "collab.v2":
            # The expression stage reuses the v2 final shape; v1 turns keep
            # their single-generation path byte for byte.
            return cls(
                requested_mode=requested_mode,
                effective_mode="off",
                resolved_reason=SKIP_PROTOCOL_V1,
            )
        if requested_mode == "auto":
            return cls(
                requested_mode="auto",
                effective_mode="off",
                resolved_reason=SKIP_AUTO_NOT_IMPLEMENTED,
            )
        return cls(requested_mode=requested_mode, effective_mode=requested_mode)

    @classmethod
    def from_strategy(
        cls, value: Any, *, protocol_version: str | None = None
    ) -> "SdrpePlan":
        """Read the snapshot, tolerating the legacy ``"off"`` string.

        A turn registered by an older build carries ``strategy["sdrpe"] = "off"``;
        that is exactly the single-stage behaviour, so it reads as off.
        """

        if isinstance(value, Mapping):
            requested = value.get("requested_mode", "off")
            if requested not in SDRPE_MODES:
                requested = "off"
            resolved = cls.resolve(requested, protocol_version=protocol_version)
            # The snapshot is the authority for this turn once it exists, but a
            # protocol downgrade must still win: never run a stage the current
            # protocol cannot parse back.
            if resolved.effective_mode == "off":
                return resolved
            stored = value.get("effective_mode")
            if stored in SDRPE_EFFECTIVE_MODES:
                # RG08: the recorded policy version identifies the mechanism
                # this turn was registered under.  It is the *origin* label and
                # is never upgraded to the current constant -- an old turn must
                # not be relabelled as if it had run under a newer policy.
                stored_policy = value.get("policy_version")
                origin = (
                    stored_policy
                    if isinstance(stored_policy, str) and stored_policy
                    else cls.policy_version
                )
                return cls(
                    requested_mode=requested,
                    effective_mode=stored,
                    policy_version=origin,
                    resolved_reason=value.get("resolved_reason"),
                )
            return resolved
        if isinstance(value, str) and value in SDRPE_MODES:
            return cls.resolve(value, protocol_version=protocol_version)
        return cls.resolve("off", protocol_version=protocol_version)


@dataclass(frozen=True)
class ExpressionDecision:
    """Whether the expression stage may run, and why not when it may not."""

    run: bool
    reason: str | None
    policy_version: str = SDRPE_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.run and self.reason is not None:
            raise ValueError("a running decision never carries a skip reason")
        if not self.run and self.reason is None:
            raise ValueError("a skipped decision must carry a reason")


def skip(reason: str) -> ExpressionDecision:
    if reason not in SKIP_REASONS:
        raise ValueError("reason is not a frozen skip reason")
    return ExpressionDecision(run=False, reason=reason)


def content_request_cap(plan: SdrpePlan, max_model_requests: int) -> int:
    """How many requests the content stage may spend in this turn.

    ``on`` reserves one request for the expression stage; every other
    resolution keeps the configured cap untouched.
    """

    if plan.effective_mode != "on":
        return max_model_requests
    return max(1, max_model_requests - EXPRESSION_REQUEST_RESERVE)


def decide(
    *,
    plan: SdrpePlan,
    turn_mode: str,
    content_action: str,
    protocol_version: str | None,
    max_model_requests: int,
    requests_used: int,
    wall_left: float,
) -> ExpressionDecision:
    """Decide the expression stage for one finished content answer.

    The order is deliberate: mode, protocol, turn mode, action, budget, wall.
    The first failing gate is the recorded reason, so a skip is explainable.
    """

    if plan.effective_mode != "on":
        return skip(plan.resolved_reason or SKIP_MODE_OFF)
    if plan.requested_mode == "auto":
        return skip(SKIP_AUTO_NOT_IMPLEMENTED)
    if protocol_version != "collab.v2":
        return skip(SKIP_PROTOCOL_V1)
    if turn_mode != "chat":
        return skip(SKIP_WORK_MODE)
    if content_action != "final":
        return skip(SKIP_WAIT_QUESTION)
    if max_model_requests < MIN_MODEL_REQUESTS_FOR_EXPRESSION:
        return skip(SKIP_NO_RESERVE)
    if requests_used >= max_model_requests:
        return skip(SKIP_BUDGET)
    if wall_left < MIN_EXPRESSION_WALL_SECONDS:
        return skip(SKIP_WALL)
    return ExpressionDecision(run=True, reason=None)


__all__ = [
    "AUTO_IMPLEMENTED",
    "EXPRESSION_REQUEST_RESERVE",
    "EXPRESSION_STAGE_KIND",
    "ExpressionDecision",
    "MIN_EXPRESSION_WALL_SECONDS",
    "MIN_MODEL_REQUESTS_FOR_EXPRESSION",
    "SDRPE_EFFECTIVE_MODES",
    "SDRPE_MODES",
    "SDRPE_POLICY_VERSION",
    "SKIP_AUTO_NOT_IMPLEMENTED",
    "SKIP_BUDGET",
    "SKIP_CONTEXT_TOO_LARGE",
    "SKIP_DRAFT_TOO_LARGE",
    "SKIP_MODE_OFF",
    "SKIP_NO_RESERVE",
    "SKIP_PROTOCOL_V1",
    "SKIP_REASONS",
    "SKIP_WAIT_QUESTION",
    "SKIP_WALL",
    "SKIP_WORK_MODE",
    "SdrpePlan",
    "content_request_cap",
    "decide",
    "skip",
]
