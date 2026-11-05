"""J08 WP3: bounded Cognition review driven by verified Memory material.

The module owns the *bounded iteration and the outcome vocabulary*; the caller
owns the semantic mapping from material to a candidate bundle, because that
mapping is a policy decision and must not be hidden in plumbing.  A builder
that returns ``None`` means "this material does not justify a change", which is
a first-class outcome (``no_change``), not a failure.

Nothing here commits.  A candidate is reported as ``proposed`` and its review is
a later, separate step through the existing governance path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from ...foundation.errors import InvalidInput
from ..memory.maintenance_source import SourceMaterial

#: The most items one review slice may propose for.
MAX_REVIEW_ITEMS = 20

#: Outcomes a single item can end in.  ``proposed`` is not a commit.
REVIEW_OUTCOMES = frozenset(
    {"proposed", "no_change", "insufficient_evidence", "budget_exhausted",
     "dependency_unavailable", "authorization_invalid", "failed"}
)


@dataclass(frozen=True)
class ReviewOutcome:
    event_id: str
    outcome: str
    candidate_id: str | None = None
    detail: str | None = None


#: Stable machine labels for the exception classes a propose can raise.  The
#: item record must keep the failure class, not the exception text: a message
#: can quote a field name, a record id or model output, and ``detail`` is a
#: bounded diagnostic column.
_ERROR_LABELS = {
    "InvalidInput": "invalid_input",
    "EvidenceMissing": "evidence_missing",
    "UnsupportedOperation": "unsupported_operation",
    "ScopeViolation": "scope_violation",
    "SourceRedacted": "source_redacted",
    "SourceUntrusted": "source_untrusted",
    "StateRevisionConflict": "revision_conflict",
}


def _stable_code(exc: BaseException) -> str:
    """One bounded label for a refused propose; never the exception text."""

    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        return _label(reason)
    return _ERROR_LABELS.get(type(exc).__name__) or _label(type(exc).__name__)


def _label(value: str) -> str:
    text = "".join(ch.lower() if ch.isalnum() else "_" for ch in value)
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")[:48] or "failed"


def propose_from_material(
    changes: Any,
    access: Any,
    *,
    context_id: str,
    material: Sequence[SourceMaterial],
    build_bundle: Callable[[SourceMaterial], Mapping[str, Any] | None],
    request_ids: Sequence[str],
    limit: int = MAX_REVIEW_ITEMS,
) -> tuple[ReviewOutcome, ...]:
    """Propose at most ``limit`` candidates from live material.

    ``request_ids`` is supplied by the caller so a retried slice reuses the same
    identities instead of minting new ones: a duplicate proposal under a fresh
    request id is exactly the failure this signature is meant to prevent.
    """

    if type(limit) is not int or limit < 1 or limit > MAX_REVIEW_ITEMS:
        raise InvalidInput(f"limit must be between 1 and {MAX_REVIEW_ITEMS}")
    items = [item for item in material if item.usable]
    if len(items) > limit:
        raise InvalidInput(
            f"{len(items)} usable items but the limit is {limit}; page the scan"
        )
    if len(request_ids) != len(items):
        raise InvalidInput("one request id is required per usable item")

    outcomes: list[ReviewOutcome] = []
    for item, request_id in zip(items, request_ids):
        try:
            bundle = build_bundle(item)
        except Exception as exc:  # a bad mapping must not abort the slice
            outcomes.append(
                ReviewOutcome(item.event_id, "failed", detail=f"builder: {exc}")
            )
            continue
        if bundle is None:
            outcomes.append(
                ReviewOutcome(
                    item.event_id,
                    "no_change",
                    detail="material does not justify a change",
                )
            )
            continue
        try:
            result = changes.propose(
                access,
                context_id=context_id,
                request_id=request_id,
                bundle=bundle,
                activation_kind="manual_review",
            )
        except Exception as exc:
            outcomes.append(
                ReviewOutcome(
                    item.event_id,
                    "failed",
                    detail=f"propose:{_stable_code(exc)}",
                )
            )
            continue
        outcomes.append(
            ReviewOutcome(item.event_id, "proposed", candidate_id=result.candidate_id)
        )
    return tuple(outcomes)


__all__ = [
    "MAX_REVIEW_ITEMS",
    "REVIEW_OUTCOMES",
    "ReviewOutcome",
    "propose_from_material",
]
