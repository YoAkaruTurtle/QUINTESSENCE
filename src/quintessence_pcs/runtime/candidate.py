"""C03: bounded candidate generation, separated from commit.

``generate_candidate`` is the first half of one cognition step, extracted from
``step.run_step`` without changing its behaviour: the adapter preflight, the
Context assembly, the bounded model loop, and the structural parse, domain
validation and format repair of one proposal.  It never commits anything.

The generator does not know that ``work`` exists.  It reads no
``work_progress`` row, writes nothing to the store, never calls
``submit_proposal`` or ``complete_work``.  Context archival is part of building
a Context, not a submission.

The two model calls are owned by one :class:`GenerationBudget` shared with the
semantic retry in ``step.py``.  Format repair and semantic retry stay two
separate paths with their own counters, and both spend the same bounded
budget.

A candidate is not a commit.  :class:`CandidateResult` has no "committed"
field; only ``step.run_step`` submits the proposal and completes the pending
work.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from ..domains.refs import EventRef
from ..foundation import identifiers
from ..foundation.errors import (
    CorruptStore,
    DbBusy,
    InvalidInput,
    ScopeViolation,
    StorageError,
)
from .context import ContextEnvelope, ContextService
from .proposal_generation import (
    parse_proposal,
    repair_feedback,
    validate_proposal,
)

MAX_MODEL_CALLS = 2  # one attempt plus one format repair


@dataclass
class GenerationBudget:
    """The single model-call budget of one step.

    Generation and semantic retry share this object; there is no second
    counter or second limit.  ``calls`` counts every attempt sent to the
    model, ``repairs`` counts only format repairs.
    """

    max_calls: int = MAX_MODEL_CALLS
    calls: int = 0
    repairs: int = 0

    def __post_init__(self) -> None:
        if self.max_calls < 1:
            raise InvalidInput("max_calls must be at least 1")

    def claim_call(self) -> bool:
        """Take one call; return False (without consuming) when exhausted."""

        if self.calls >= self.max_calls:
            return False
        self.calls += 1
        return True

    def note_repair(self) -> None:
        self.repairs += 1


@dataclass(frozen=True)
class CandidateFailure:
    """Why generation stopped before producing a candidate."""

    phase: str  # "preflight" | "context" | "model" | "proposal"
    cause: BaseException
    last_validation_error: StorageError | None


@dataclass(frozen=True)
class CandidateResult:
    """A generated (not committed) proposal plus its Context and counters."""

    namespace: str
    trigger_ref: EventRef
    context: ContextEnvelope | None
    proposal: dict[str, Any] | None
    failure: CandidateFailure | None
    model_calls: int
    repairs: int

    @property
    def generated(self) -> bool:
        """Whether a candidate was produced; never means "already submitted"."""

        return self.failure is None and self.proposal is not None


def generate_candidate(
    *,
    namespace: str,
    trigger_ref: Mapping[str, Any] | EventRef,
    context_service: ContextService,
    model: Any,
    generation_budget: GenerationBudget | None = None,
    feedback: str | None = None,
    refresh_self_view: bool = True,
    focus_work_id: str | None = None,
    trigger_as_fact: bool = False,
) -> CandidateResult:
    """Run preflight, Context assembly and the bounded model loop, no commit.

    ``focus_work_id``/``trigger_as_fact`` are used by C07: restrict the
    projection to the Work under review and project its own trigger Event body
    as a fact.  Legacy C02/C05 callers keep the defaults and unchanged behavior.
    """

    ns = identifiers.require_namespace(namespace)
    trigger = (
        trigger_ref
        if isinstance(trigger_ref, EventRef)
        else EventRef.from_value(trigger_ref, field="trigger_ref")
    )
    if trigger.namespace != ns:
        raise ScopeViolation(
            f"trigger is in namespace {trigger.namespace!r}, not {ns!r}"
        )
    budget = (
        generation_budget if generation_budget is not None else GenerationBudget()
    )

    # A real adapter may refuse before any Context material is assembled
    # (missing/expired authorization, missing key, exhausted budget).  This is
    # a local refusal, not a fallback to FakeModel.
    preflight = getattr(model, "preflight", None)
    if preflight is not None:
        try:
            preflight(namespace=ns)
        except (CorruptStore, DbBusy):
            raise
        except Exception as exc:  # noqa: BLE001 - adapter refusal is a result
            return CandidateResult(
                namespace=ns,
                trigger_ref=trigger,
                context=None,
                proposal=None,
                failure=CandidateFailure(
                    phase="preflight", cause=exc, last_validation_error=None
                ),
                model_calls=budget.calls,
                repairs=budget.repairs,
            )

    try:
        context = context_service.build_context(
            namespace=ns,
            trigger_ref=trigger.to_dict(),
            adapter_kind=model.adapter_kind,
            refresh_self_view=refresh_self_view,
            focus_work_id=focus_work_id,
            trigger_as_fact=trigger_as_fact,
        )
    except StorageError as exc:
        if isinstance(exc, (CorruptStore, DbBusy)):
            raise
        return CandidateResult(
            namespace=ns,
            trigger_ref=trigger,
            context=None,
            proposal=None,
            failure=CandidateFailure(
                phase="context", cause=exc, last_validation_error=None
            ),
            model_calls=budget.calls,
            repairs=budget.repairs,
        )

    last_validation_error: StorageError | None = None
    while budget.claim_call():
        binder = getattr(model, "bind_run", None)
        scope = (
            binder(namespace=ns, context=context)
            if binder is not None
            else nullcontext()
        )
        try:
            with scope:
                raw = model.propose(
                    rendered_input=context.rendered_input,
                    references=context.available_references,
                    feedback=feedback,
                )
        except (CorruptStore, DbBusy):
            raise
        except Exception as exc:  # noqa: BLE001 - model failures are a result
            return CandidateResult(
                namespace=ns,
                trigger_ref=trigger,
                context=context,
                proposal=None,
                failure=CandidateFailure(
                    phase="model",
                    cause=exc,
                    last_validation_error=last_validation_error,
                ),
                model_calls=budget.calls,
                repairs=budget.repairs,
            )
        try:
            proposal = parse_proposal(raw)
            # A WAIT is legal without domain validation, so the skip rule stays
            # after the parse and before validation.
            if proposal["operation"] != "wait":
                validate_proposal(ns, proposal)
        except (StorageError, ValueError) as exc:
            last_validation_error = exc if isinstance(exc, StorageError) else None
            if budget.calls >= budget.max_calls:
                return CandidateResult(
                    namespace=ns,
                    trigger_ref=trigger,
                    context=context,
                    proposal=None,
                    failure=CandidateFailure(
                        phase="proposal",
                        cause=exc,
                        last_validation_error=last_validation_error,
                    ),
                    model_calls=budget.calls,
                    repairs=budget.repairs,
                )
            budget.note_repair()
            feedback = repair_feedback(exc)
            continue
        return CandidateResult(
            namespace=ns,
            trigger_ref=trigger,
            context=context,
            proposal=proposal,
            failure=None,
            model_calls=budget.calls,
            repairs=budget.repairs,
        )

    raise CorruptStore("the model loop ended without a proposal")


__all__ = [
    "CandidateFailure",
    "CandidateResult",
    "GenerationBudget",
    "MAX_MODEL_CALLS",
    "generate_candidate",
]
