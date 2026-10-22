"""C4: the bounded one-step cognition loop and its crash-window proof.

``run_step`` does exactly one thing, once:

1. take the unfinished ``pending_work`` row created by the trigger event;
2. build and persist a Context (no ``state_revision``);
3. call the model at most once plus at most one format repair;
4. validate the structurally parsed proposal;
5. commit the Belief version **and** ``work_progress`` in the same Tx B
   transaction through ``TxScope.complete_work``;
6. a legal WAIT (or a revision that changes nothing) writes
   ``checked_no_belief`` in that same transaction and advances no revision.

Failure before commit writes no progress row, so the work stays pending and a
retry is safe: the previous attempt never committed, and the new request_id
cannot "bypass" a completion that does not exist.  Failure after commit is
invisible to a retry because the work query excludes completed rows.  Two
concurrent processes may both pass every watermark check, but only one can
insert the ``work_progress`` primary key; the loser's whole transaction rolls
back.

``StepResult`` is a runtime result object, not a ``pcs.v2`` response envelope.
Controlled storage failures are reported with their contract ``code``;
``MODEL_FAILURE`` is the only runtime-local label (the adapter refused or the
network exchange failed instead of returning usable content).  A proposal that
is not valid JSON after the bounded repair remains ``INVALID_SCHEMA``, as in
PCS2-008; no public error-code enum is extended.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from ..domains.cognition.beliefs import BeliefService
from ..domains.cognition.context_guard import require_live_context
from ..domains.cognition.views import SelfViewService
from ..domains.refs import EventRef, RecordRef
from ..foundation import identifiers
from ..foundation.errors import (
    CorruptStore,
    DbBusy,
    EventNotFound,
    StorageError,
)
from ..store.cognition import (
    CognitionStore,
    WORK_OUTCOME_CHECKED_NO_BELIEF,
    WORK_OUTCOME_COMMITTED,
)
from ..store.tx_b import CommitReceipt
from .candidate import MAX_MODEL_CALLS, GenerationBudget, generate_candidate
from .context import ContextEnvelope, ContextService
from .fake_model import FakeModel
from .proposal_generation import (
    parse_proposal,
    repair_feedback,
    validate_proposal,
)

OUTCOME_COMMITTED = "committed"
OUTCOME_CHECKED_NO_BELIEF = "checked_no_belief"
OUTCOME_NO_PENDING_WORK = "no_pending_work"
OUTCOME_FAILED = "failed"
STEP_OUTCOMES = (
    OUTCOME_COMMITTED,
    OUTCOME_CHECKED_NO_BELIEF,
    OUTCOME_NO_PENDING_WORK,
    OUTCOME_FAILED,
)

MODEL_FAILURE_CODE = "MODEL_FAILURE"


@dataclass(frozen=True)
class StepResult:
    """Outcome of one ``run_step`` call."""

    outcome: str
    namespace: str
    trigger_ref: EventRef
    work_id: str | None = None
    context_id: str | None = None
    state_revision: int = 0
    receipt: CommitReceipt | None = None
    belief_ref: RecordRef | None = None
    belief_status: str | None = None
    model_calls: int = 0
    repairs: int = 0
    validation_retries: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    #: When a format repair could not even be sent (quota, transport or another
    #: adapter refusal), the *original* validation error stays in
    #: ``error_code``/``error_message`` and this field holds the repair
    #: attempt's own failure.  A budget error must not mask the real error.
    repair_error: str | None = None
    #: Non-error explanation for a no-work result (for example a deleted
    #: trigger whose work is terminal).  Never contains private body text.
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in (
            OUTCOME_COMMITTED,
            OUTCOME_CHECKED_NO_BELIEF,
            OUTCOME_NO_PENDING_WORK,
        )


class CognitionRuntime:
    """One-step runtime: Context + ModelPort + Tx B."""

    def __init__(
        self,
        store: CognitionStore,
        *,
        model: Any | None = None,
        belief_service: BeliefService | None = None,
        context_service: ContextService | None = None,
        self_view_service: SelfViewService | None = None,
        clock: Any | None = None,
    ) -> None:
        self._store = store
        self._self_view = self_view_service or SelfViewService(store)
        self._beliefs = belief_service or BeliefService(store, clock=clock)
        self._contexts = context_service or ContextService(
            store, self_view_service=self._self_view
        )
        self._model = model if model is not None else FakeModel()

    # ------------------------------------------------------------- components

    @property
    def belief_service(self) -> BeliefService:
        return self._beliefs

    @property
    def context_service(self) -> ContextService:
        return self._contexts

    @property
    def self_view_service(self) -> SelfViewService:
        return self._self_view

    # ---------------------------------------------------------------- context

    def build_context(
        self,
        *,
        namespace: str,
        trigger_ref: Mapping[str, Any],
        adapter_kind: str | None = None,
    ) -> ContextEnvelope:
        return self._contexts.build_context(
            namespace=namespace,
            trigger_ref=trigger_ref,
            adapter_kind=adapter_kind,
        )

    # ------------------------------------------------------------------ step

    def run_step(
        self,
        *,
        namespace: str,
        trigger_event_id: str,
        feedback: str | None = None,
    ) -> StepResult:
        ns = identifiers.require_namespace(namespace)
        event_id = identifiers.require_event_id(trigger_event_id)
        trigger = EventRef(namespace=ns, event_id=event_id)
        revision = self._store.current_state_revision(ns)

        works = self._store.unfinished_work_for_event(ns, event_id)
        if not works:
            if self._store.event_is_deleted(ns, event_id):
                # The trigger source is gone.  Its work is not a valid basis for
                # a Context; make the terminal state durable (one bounded,
                # idempotent reconciliation) and return an explicit no-work
                # result.  Do not fabricate a trigger or a WAIT receipt.
                self._store.reconcile_redacted_work(namespace=ns)
                return StepResult(
                    outcome=OUTCOME_NO_PENDING_WORK,
                    namespace=ns,
                    trigger_ref=trigger,
                    state_revision=self._store.current_state_revision(ns),
                    note=(
                        "trigger source was deleted; no processable work "
                        "remains (deleted pending work is terminal)"
                    ),
                )
            if self._store.event_header(ns, event_id) is None:
                raise EventNotFound(
                    f"trigger event {event_id} does not exist in {ns!r}"
                )
            return StepResult(
                outcome=OUTCOME_NO_PENDING_WORK,
                namespace=ns,
                trigger_ref=trigger,
                state_revision=revision,
            )
        work = works[0]

        generation_budget = GenerationBudget()
        candidate = generate_candidate(
            namespace=ns,
            trigger_ref=trigger,
            context_service=self._contexts,
            model=self._model,
            generation_budget=generation_budget,
            feedback=feedback,
        )
        if candidate.failure is not None:
            return self._candidate_failure(ns, trigger, work.work_id, candidate)
        context = candidate.context
        proposal = candidate.proposal

        if proposal is None:  # pragma: no cover - loop always sets or returns
            raise CorruptStore("the model loop ended without a proposal")

        validation_retries: list[str] = []
        while True:
            try:
                if proposal["operation"] == "wait":
                    receipt = self._commit_wait(
                        ns=ns, context=context, work=work, reason=proposal["reason"]
                    )
                    progress = self._store.work_progress(work.work_id)
                    if progress is None:
                        raise CorruptStore(
                            "a WAIT transaction committed without a work_progress row"
                        )
                    return StepResult(
                        outcome=OUTCOME_CHECKED_NO_BELIEF,
                        namespace=ns,
                        trigger_ref=trigger,
                        work_id=work.work_id,
                        context_id=context.context_id,
                        state_revision=progress.completed_state_revision,
                        receipt=receipt,
                        model_calls=generation_budget.calls,
                        repairs=generation_budget.repairs,
                        validation_retries=tuple(validation_retries),
                    )
                request_id = str(uuid.uuid4())
                submission = self._beliefs.submit_proposal(
                    namespace=ns,
                    context_id=context.context_id,
                    request_id=request_id,
                    proposal=proposal,
                    work_id=work.work_id,
                )
                progress = self._store.work_progress(work.work_id)
                if progress is None:
                    raise CorruptStore(
                        "a belief transaction committed without a work_progress row"
                    )
                if submission.appended:
                    if progress.outcome != WORK_OUTCOME_COMMITTED:
                        raise CorruptStore(
                            "an appended belief recorded a non-committed work outcome"
                        )
                    outcome = OUTCOME_COMMITTED
                else:
                    # A revise that changed nothing is a legal "checked, no new
                    # judgment" step; it still completes the work atomically.
                    if progress.outcome != WORK_OUTCOME_CHECKED_NO_BELIEF:
                        raise CorruptStore(
                            "a no-op revision recorded a committed work outcome"
                        )
                    outcome = OUTCOME_CHECKED_NO_BELIEF
                return StepResult(
                    outcome=outcome,
                    namespace=ns,
                    trigger_ref=trigger,
                    work_id=work.work_id,
                    context_id=context.context_id,
                    state_revision=progress.completed_state_revision,
                    receipt=submission.receipt,
                    belief_ref=RecordRef(
                        namespace=ns,
                        record_id=submission.version.record_id,
                        record_version=submission.version.record_version,
                    ),
                    belief_status=submission.version.status,
                    model_calls=generation_budget.calls,
                    repairs=generation_budget.repairs,
                    validation_retries=tuple(validation_retries),
                )
            except StorageError as exc:
                if isinstance(exc, (CorruptStore, DbBusy)):
                    raise
                validation_retries.append(str(exc))
                if generation_budget.calls >= MAX_MODEL_CALLS:
                    return StepResult(
                        outcome=OUTCOME_FAILED,
                        namespace=ns,
                        trigger_ref=trigger,
                        work_id=work.work_id,
                        context_id=context.context_id,
                        state_revision=self._store.current_state_revision(ns),
                        model_calls=generation_budget.calls,
                        repairs=generation_budget.repairs,
                        validation_retries=tuple(validation_retries),
                        error_code=getattr(exc, "code", "CORRUPT_STORE"),
                        error_message=str(exc),
                    )
                # One bounded semantic repair.  The rejected transaction did
                # not change state, so the same live Context is still valid.
                generation_budget.note_repair()
                if not generation_budget.claim_call():  # pragma: no cover
                    raise CorruptStore(
                        "semantic retry exceeded the shared model-call budget"
                    )
                binder = getattr(self._model, "bind_run", None)
                scope = (
                    binder(namespace=ns, context=context)
                    if binder is not None
                    else nullcontext()
                )
                try:
                    with scope:
                        raw = self._model.propose(
                            rendered_input=context.rendered_input,
                            references=context.available_references,
                            feedback=semantic_retry_feedback(exc),
                        )
                except (CorruptStore, DbBusy):
                    raise
                except Exception as model_exc:  # noqa: BLE001
                    return StepResult(
                        outcome=OUTCOME_FAILED,
                        namespace=ns,
                        trigger_ref=trigger,
                        work_id=work.work_id,
                        context_id=context.context_id,
                        state_revision=self._store.current_state_revision(ns),
                        model_calls=generation_budget.calls,
                        repairs=generation_budget.repairs,
                        validation_retries=tuple(validation_retries),
                        error_code=getattr(exc, "code", "CORRUPT_STORE"),
                        error_message=str(exc),
                        repair_error=f"{type(model_exc).__name__}: {model_exc}",
                    )
                try:
                    proposal = parse_proposal(raw)
                    if proposal["operation"] != "wait":
                        validate_proposal(ns, proposal)
                except (StorageError, ValueError) as retry_exc:
                    return StepResult(
                        outcome=OUTCOME_FAILED,
                        namespace=ns,
                        trigger_ref=trigger,
                        work_id=work.work_id,
                        context_id=context.context_id,
                        state_revision=self._store.current_state_revision(ns),
                        model_calls=generation_budget.calls,
                        repairs=generation_budget.repairs,
                        validation_retries=tuple(validation_retries),
                        error_code=getattr(retry_exc, "code", "INVALID_SCHEMA"),
                        error_message=str(retry_exc),
                    )
                continue

    # ------------------------------------------------------------- internals

    def _commit_wait(
        self,
        *,
        ns: str,
        context: ContextEnvelope,
        work: Any,
        reason: str,
    ) -> CommitReceipt:
        def apply(tx: Any) -> None:
            # Same re-validation as a belief commit: a WAIT may not be recorded
            # on top of a Context whose sources moved or were deleted.
            require_live_context(
                self._store, context_id=context.context_id, namespace=ns
            )
            tx.complete_work(
                work_id=work.work_id,
                context_id=context.context_id,
                outcome=WORK_OUTCOME_CHECKED_NO_BELIEF,
            )

        return self._store.commit_command(
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            operation="cognition.wait",
            request_payload={"reason": reason, "work_id": work.work_id},
            apply=apply,
            namespace=ns,
        )

    def _failure(
        self,
        ns: str,
        trigger: EventRef,
        work_id: str | None,
        context_id: str | None,
        exc: StorageError,
    ) -> StepResult:
        return StepResult(
            outcome=OUTCOME_FAILED,
            namespace=ns,
            trigger_ref=trigger,
            work_id=work_id,
            context_id=context_id,
            state_revision=self._store.current_state_revision(ns),
            error_code=getattr(exc, "code", "CORRUPT_STORE"),
            error_message=str(exc),
        )

    def _candidate_failure(
        self,
        ns: str,
        trigger: EventRef,
        work_id: str,
        candidate: Any,
    ) -> StepResult:
        """Map a generation failure to the same StepResult as before C03."""

        failure = candidate.failure
        if failure.phase == "preflight":
            return StepResult(
                outcome=OUTCOME_FAILED,
                namespace=ns,
                trigger_ref=trigger,
                work_id=work_id,
                state_revision=self._store.current_state_revision(ns),
                error_code=MODEL_FAILURE_CODE,
                error_message=f"{type(failure.cause).__name__}: {failure.cause}",
            )
        if failure.phase == "context":
            return self._failure(ns, trigger, work_id, None, failure.cause)
        if failure.phase == "model":
            if (
                failure.last_validation_error is not None
                and candidate.model_calls > 1
            ):
                # The model produced syntactically valid JSON that failed
                # validation, and the one repair could not be sent (for
                # example the scenario quota refused the next attempt).
                # Keep the original validation error; record the repair
                # failure separately so the budget refusal cannot hide it.
                return StepResult(
                    outcome=OUTCOME_FAILED,
                    namespace=ns,
                    trigger_ref=trigger,
                    work_id=work_id,
                    context_id=candidate.context.context_id,
                    state_revision=self._store.current_state_revision(ns),
                    model_calls=candidate.model_calls,
                    repairs=candidate.repairs,
                    error_code=getattr(
                        failure.last_validation_error, "code", "INVALID_SCHEMA"
                    ),
                    error_message=str(failure.last_validation_error),
                    repair_error=f"{type(failure.cause).__name__}: {failure.cause}",
                )
            return StepResult(
                outcome=OUTCOME_FAILED,
                namespace=ns,
                trigger_ref=trigger,
                work_id=work_id,
                context_id=candidate.context.context_id,
                state_revision=self._store.current_state_revision(ns),
                model_calls=candidate.model_calls,
                repairs=candidate.repairs,
                error_code=MODEL_FAILURE_CODE,
                error_message=f"{type(failure.cause).__name__}: {failure.cause}",
            )
        if failure.phase == "proposal":
            return StepResult(
                outcome=OUTCOME_FAILED,
                namespace=ns,
                trigger_ref=trigger,
                work_id=work_id,
                context_id=candidate.context.context_id,
                state_revision=self._store.current_state_revision(ns),
                model_calls=candidate.model_calls,
                repairs=candidate.repairs,
                error_code=getattr(failure.cause, "code", "INVALID_SCHEMA"),
                error_message=str(failure.cause),
            )
        raise CorruptStore(
            f"unknown candidate failure phase {failure.phase!r}"
        )


def semantic_retry_feedback(exc: BaseException) -> str:
    """Explain a Tx-B rejection and the legal correction operation.

    This is feedback, not acceptance: the model may still repeat an illegal
    proposal, and the bounded retry then fails.  In particular, the runtime
    never turns the rejected ``revise_belief`` into a retraction itself.
    """

    message = str(exc)
    parts = [f"PCS2-008 rejected the previous proposal: {message}."]
    if (
        "claim text is frozen" in message
        or "substantive change" in message
        or "claim_type cannot change" in message
    ):
        parts.append(
            "The claim text and claim_type of an existing Belief are frozen. "
            "revise_belief cannot change them. If the new evidence requires a "
            "different claim, use retract_belief with the existing target, "
            "evidence_links and reason; after the retraction commits, a new "
            "Context will be built and you may be asked again. WAIT is also "
            "legal. Do not repeat the same revise_belief."
        )
    else:
        parts.append(repair_feedback(exc))
    return " ".join(parts)


__all__ = [
    "CognitionRuntime",
    "MAX_MODEL_CALLS",
    "MODEL_FAILURE_CODE",
    "OUTCOME_CHECKED_NO_BELIEF",
    "OUTCOME_COMMITTED",
    "OUTCOME_FAILED",
    "OUTCOME_NO_PENDING_WORK",
    "STEP_OUTCOMES",
    "StepResult",
    "repair_feedback",
]
