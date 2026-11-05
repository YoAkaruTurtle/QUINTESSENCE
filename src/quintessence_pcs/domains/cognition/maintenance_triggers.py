"""J08 WP3: bounded, machine-judgeable review triggers.

The point of this module is to keep a *free-text* field from pretending to be a
trigger.  ``review_condition`` is prose a human wrote; it is not a predicate, and
guessing at it would silently wake questions nobody asked to revisit.  Anything
this module cannot judge deterministically is reported as
``not_machine_judgeable`` and is **excluded** from automatic review.

Only three reasons are machine-judgeable here:

* ``review_due``     — ``review_after`` has passed and has not been evaluated since;
* ``source_revision``— a caller-observed source revision differs from the one the
  view recorded (the caller has the authoritative versions; this module only
  compares them);
* ``manual_review``  — an operator explicitly asked for this subject.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ...foundation.errors import InvalidInput

#: The largest page one evaluation may consider.
MAX_TRIGGER_PAGE = 50

#: The only reasons this module can decide without guessing.
JUDGEABLE_REASONS = frozenset({"review_due", "source_revision", "manual_review"})

#: Reason used for prose that no deterministic rule can evaluate.
UNJUDGEABLE_REASON = "not_machine_judgeable"


@dataclass(frozen=True)
class TriggerCandidate:
    """One subject that a trigger, or the absence of one, applies to."""

    subject_ref: Mapping[str, Any]
    reason: str
    judgeable: bool
    detail: str | None = None

    def as_item(self) -> dict[str, Any]:
        return {
            "target_kind": "record",
            "target_id": str(self.subject_ref.get("record_id")),
            "target_version": int(self.subject_ref.get("record_version", 0)),
            "operation_key": f"trigger:{self.subject_ref.get('record_id')}",
            "outcome": "no_change" if self.judgeable else "deferred",
            "detail": self.reason if not self.detail else f"{self.reason}:{self.detail}",
        }


def _ref_of(view: Any) -> Mapping[str, Any]:
    ref = getattr(view, "ref", None)
    if ref is None:
        raise InvalidInput("a trigger view must carry a ref")
    if hasattr(ref, "to_dict"):
        return ref.to_dict()
    if isinstance(ref, Mapping):
        return dict(ref)
    raise InvalidInput("a trigger view ref must be a mapping or RecordRef")


def _require_page(views: Sequence[Any], limit: int) -> list[Any]:
    if type(limit) is not int or limit < 1 or limit > MAX_TRIGGER_PAGE:
        raise InvalidInput(f"limit must be between 1 and {MAX_TRIGGER_PAGE}")
    items = list(views)
    if len(items) > limit:
        raise InvalidInput(
            f"page holds {len(items)} views but the limit is {limit}; scan in pages"
        )
    return items


def evaluate_triggers(
    *,
    views: Sequence[Any],
    now: str,
    manual_refs: Iterable[Mapping[str, Any]] = (),
    source_revisions: Mapping[str, int] | None = None,
    include_unjudgeable: bool = False,
    limit: int = MAX_TRIGGER_PAGE,
) -> tuple[TriggerCandidate, ...]:
    """Return the bounded, deterministic trigger set for this page.

    Paused subjects are skipped outright: an operator's pause is a decision, not
    a hint, and ordinary maintenance must not wake it.
    """

    page = _require_page(views, limit)
    manual = {
        (str(ref.get("record_id")), int(ref.get("record_version", 0)))
        for ref in manual_refs
    }
    revisions = dict(source_revisions or {})
    found: list[TriggerCandidate] = []

    for view in page:
        if getattr(view, "paused", False):
            continue
        ref = _ref_of(view)
        key = (str(ref.get("record_id")), int(ref.get("record_version", 0)))

        if key in manual:
            found.append(TriggerCandidate(ref, "manual_review", True))
            continue

        recorded = revisions.get(str(ref.get("record_id")))
        if recorded is not None:
            current = getattr(view, "source_revision", None)
            if current is not None and int(current) != int(recorded):
                found.append(
                    TriggerCandidate(ref, "source_revision", True, str(current))
                )
                continue

        due_at = getattr(view, "review_after", None)
        if due_at is not None:
            evaluated = getattr(view, "last_evaluated_at", None)
            if due_at <= now and (evaluated is None or evaluated < due_at):
                found.append(TriggerCandidate(ref, "review_due", True))
                continue

        prose = getattr(view, "review_condition", None)
        if prose:
            # A human sentence is not a predicate.  Say so instead of guessing.
            if include_unjudgeable:
                found.append(TriggerCandidate(ref, UNJUDGEABLE_REASON, False, "prose"))
            continue

    return tuple(found)


@dataclass
class ReviewOpportunity:
    """The one automatic review a single logical trigger may consume.

    The budget is per trigger key and never resets on retry: a crash, a lease
    takeover or a re-delivery of the same trigger all see the same exhausted
    budget, so a failing loop cannot turn into an unbounded review engine.
    """

    version: str = "j08.review-opportunity.v1"
    per_trigger: int = 1
    _used: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if self._used is None:
            self._used = {}
        if type(self.per_trigger) is not int or self.per_trigger < 0:
            raise InvalidInput("per_trigger must be a non-negative integer")

    def remaining(self, trigger_key: str) -> int:
        return max(self.per_trigger - self._used.get(trigger_key, 0), 0)

    def consume(self, trigger_key: str) -> bool:
        """Take one opportunity; ``False`` means this trigger is spent."""

        if not isinstance(trigger_key, str) or not trigger_key:
            raise InvalidInput("trigger_key must be a non-empty string")
        used = self._used.get(trigger_key, 0)
        if used >= self.per_trigger:
            return False
        self._used[trigger_key] = used + 1
        return True


def register_trigger_tasks(
    maintenance: Any,
    *,
    namespace: str,
    candidates: Iterable[TriggerCandidate],
    config_version: str,
    input_watermark: str | None = None,
    now: str | None = None,
) -> tuple[Any, ...]:
    """Turn judgeable triggers into maintenance tasks -- the host entry point.

    This is the missing consumer between ``evaluate_triggers`` and
    ``MaintenanceStore.register``: without it a trigger can be judged and then
    dropped.  It deliberately does **not** schedule anything -- the host calls
    it, and a scheduler remains a future component.

    A trigger that already registered returns its existing job: re-evaluating the
    same page must not open a second opportunity.
    """

    del now  # the store stamps its own clock; the argument documents callers.
    jobs: list[Any] = []
    for candidate in candidates:
        if not candidate.judgeable:
            # "not_machine_judgeable" is a report, not an opportunity.
            continue
        ref = candidate.subject_ref
        record_id = str(ref.get("record_id"))
        jobs.append(
            maintenance.register(
                namespace=namespace,
                task_type="cognition.review",
                trigger_key=f"trigger:{record_id}:{candidate.reason}",
                params={
                    "record_id": record_id,
                    "record_version": int(ref.get("record_version", 0)),
                    "reason": candidate.reason,
                },
                config_version=config_version,
                input_watermark=input_watermark,
            )
        )
    return tuple(jobs)


__all__ = [
    "JUDGEABLE_REASONS",
    "MAX_TRIGGER_PAGE",
    "ReviewOpportunity",
    "TriggerCandidate",
    "UNJUDGEABLE_REASON",
    "evaluate_triggers",
    "register_trigger_tasks",
]
