"""Which cognition claim (if any) a piece of user input may become.

J09 WP2 needed an explicit answer to one question: when the user says something,
what may the system honestly record?  The answer cannot be "whatever the model
feels like", and it cannot be "always downgrade to a hypothesis" either -- D1
(2026-10-28) drew that line:

* what the user **directly** states keeps its own source eligibility; a missing
  web citation is not a reason to downgrade it to a guess, and
* only a **relayed** report ("the ops person said ...") may take the
  low-strength ``general_hypothesis`` route, and only when it is worth keeping,
  with the relay and its unverified status recorded.

This module is deliberately pure: it maps input class + the evidence event types
the caller actually holds onto a route, using the *existing* registry
(``review_policy.EVIDENCE_EVENT_TYPES``).  It never invents a claim type, never
widens what an evidence type may support, and never fabricates an event id.  When
the registry cannot support the statement the honest answer is a Question (or no
candidate at all), not a stronger claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from ...collaboration.review_policy import EVIDENCE_EVENT_TYPES
from .beliefs import CLAIM_TYPES

#: The five input classes D1 distinguishes.  The last two must never be merged:
#: treating a direct statement as a relay downgrades it, and treating a relay as
#: a fact upgrades it -- both are wrong in the same way.
INPUT_CLASSES = (
    "preference",
    "project_decision",
    "correction",
    "direct_fact",
    "relayed_report",
)

#: Routes an input may take.
ROUTE_CLAIM = "claim"
ROUTE_DECISION = "decision"
ROUTE_QUESTION = "question"
ROUTE_NONE = "none"


@dataclass(frozen=True)
class ExpressionDecision:
    """What the caller may do with one piece of input, and why."""

    route: str
    claim_type: str | None = None
    strength: str | None = None
    requires_provenance: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "route": self.route,
            "claim_type": self.claim_type,
            "strength": self.strength,
            "requires_provenance": self.requires_provenance,
            "reason": self.reason,
        }


def evidence_types_for(
    claim_type: str, registry: Mapping[str, tuple[str, ...]] | None = None
) -> tuple[str, ...]:
    """The event types that may support ``claim_type``; empty means none needed."""

    table = EVIDENCE_EVENT_TYPES if registry is None else registry
    return tuple(table.get(claim_type, ()))


def eligible_claim_types(
    evidence_event_types: Sequence[str],
    registry: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """Claim types the held evidence can support.

    The result is the *exact* inverse of the registry: a claim type whose
    allowed list is empty (``general_hypothesis``, ``derived_rule``) is not
    evidence-driven and is therefore never returned here.
    """

    held = {item for item in evidence_event_types if isinstance(item, str) and item}
    if not held:
        return ()
    table = EVIDENCE_EVENT_TYPES if registry is None else registry
    eligible: list[str] = []
    for claim_type in CLAIM_TYPES:
        allowed = table.get(claim_type, ())
        if not allowed:
            continue
        if any(held_type.startswith(prefix) for held_type in held for prefix in allowed):
            eligible.append(claim_type)
    return tuple(eligible)


def decide(
    input_class: str,
    *,
    evidence_event_types: Sequence[str] = (),
    keep_relay: bool = False,
    registry: Mapping[str, tuple[str, ...]] | None = None,
) -> ExpressionDecision:
    """Route one input class to the strongest honest action.

    ``keep_relay`` is the D1 "worth keeping" decision for a relayed report; it
    has no effect on the other classes, which must not be downgraded by it.
    """

    if input_class not in INPUT_CLASSES:
        raise ValueError(f"unknown input class {input_class!r}")

    if input_class == "preference":
        if _supports("user_preference", evidence_event_types, registry):
            return ExpressionDecision(
                ROUTE_CLAIM,
                "user_preference",
                "limited",
                reason="the user stated the preference directly",
            )
        return ExpressionDecision(
            ROUTE_QUESTION,
            reason="no UserStatement: evidence is available for this preference",
        )

    if input_class == "correction":
        # J06 keeps three correction paths; the claim type follows the target,
        # so a correction is routed by whatever evidence the correction carries.
        eligible = eligible_claim_types(evidence_event_types, registry)
        if "user_preference" in eligible:
            return ExpressionDecision(
                ROUTE_CLAIM,
                "user_preference",
                "limited",
                reason="the user corrected a preference directly",
            )
        if eligible:
            return ExpressionDecision(
                ROUTE_CLAIM,
                eligible[0],
                "limited",
                reason="the correction is backed by an eligible observation",
            )
        return ExpressionDecision(
            ROUTE_QUESTION,
            reason="a correction needs eligible evidence; none was supplied",
        )

    if input_class == "project_decision":
        # ``project_decision`` is an evidence *source* in the registry, not a
        # belief claim type; a decision belongs to the existing decision record,
        # never to a belief claim.
        if "project_decision" in CLAIM_TYPES:
            raise AssertionError("project_decision must not become a belief claim")
        return ExpressionDecision(
            ROUTE_DECISION,
            reason="a project decision is recorded as a decision, not as a belief claim",
        )

    if input_class == "direct_fact":
        # A statement about the world is not a preference: even when the only
        # event is a ``UserStatement:``, filing it as ``user_preference`` would
        # mislabel what it is about.  Only world-describing families qualify.
        eligible = tuple(
            claim_type
            for claim_type in eligible_claim_types(evidence_event_types, registry)
            if claim_type != "user_preference"
        )
        if eligible:
            return ExpressionDecision(
                ROUTE_CLAIM,
                eligible[0],
                "limited",
                reason="the user stated it directly and the evidence supports it",
            )
        # D1: a direct statement is not downgraded to a hypothesis just because
        # there is no citation.  If nothing may support it, keep the question.
        return ExpressionDecision(
            ROUTE_QUESTION,
            reason="no eligible evidence type supports this fact yet; keep it as a question",
        )

    # relayed_report
    if not keep_relay:
        return ExpressionDecision(
            ROUTE_NONE,
            reason="a relayed report is only kept when it is worth long-term recall",
        )
    if not _supports("general_hypothesis", evidence_event_types, registry):
        # ``general_hypothesis`` allows an empty evidence list, so this branch is
        # the registry saying it needs nothing; it is kept for symmetry.
        return ExpressionDecision(
            ROUTE_QUESTION,
            reason="the registry does not allow a hypothesis here",
        )
    return ExpressionDecision(
        ROUTE_CLAIM,
        "general_hypothesis",
        "limited",
        requires_provenance=True,
        reason="a relayed report worth keeping, recorded as an unverified hypothesis",
    )


def _supports(
    claim_type: str,
    evidence_event_types: Sequence[str],
    registry: Mapping[str, tuple[str, ...]] | None,
) -> bool:
    allowed = evidence_types_for(claim_type, registry)
    if not allowed:
        return True
    held = [item for item in evidence_event_types if isinstance(item, str) and item]
    return any(
        held_type.startswith(prefix) for held_type in held for prefix in allowed
    )


__all__ = [
    "ExpressionDecision",
    "INPUT_CLASSES",
    "ROUTE_CLAIM",
    "ROUTE_DECISION",
    "ROUTE_NONE",
    "ROUTE_QUESTION",
    "decide",
    "eligible_claim_types",
    "evidence_types_for",
]
