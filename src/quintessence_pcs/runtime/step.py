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

from ..domains.cognition.beliefs import (
    BeliefService,
    validate_belief_body,
)
from ..domains.cognition.context_guard import require_live_context
from ..domains.cognition.errors import EvidenceMissing, UnsupportedOperation
from ..domains.cognition.views import SelfViewService
from ..domains.errors import SourceUntrusted
from ..domains.refs import EventRef, RecordRef
from ..domains.scope import SCOPE_FIELDS, normalize_scope
from ..foundation import identifiers
from ..foundation.errors import (
    CorruptStore,
    DbBusy,
    EventNotFound,
    InvalidInput,
    ScopeViolation,
    SourceRedacted,
    StorageError,
)
from ..store.cognition import (
    CognitionStore,
    WORK_OUTCOME_CHECKED_NO_BELIEF,
    WORK_OUTCOME_COMMITTED,
)
from ..store.tx_b import CommitReceipt
from .context import ContextEnvelope, ContextService, proposal_format_hint
from .fake_model import FakeModel, parse_proposal

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
MAX_MODEL_CALLS = 2  # one attempt plus one format repair


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

        # A real adapter may refuse before any Context material is assembled
        # (missing/expired authorization, missing key, exhausted budget).  This
        # is a local refusal, not a fallback to FakeModel.
        preflight = getattr(self._model, "preflight", None)
        if preflight is not None:
            try:
                preflight(namespace=ns)
            except (CorruptStore, DbBusy):
                raise
            except Exception as exc:  # noqa: BLE001 - adapter refusal is a result
                return StepResult(
                    outcome=OUTCOME_FAILED,
                    namespace=ns,
                    trigger_ref=trigger,
                    work_id=work.work_id,
                    state_revision=self._store.current_state_revision(ns),
                    error_code=MODEL_FAILURE_CODE,
                    error_message=f"{type(exc).__name__}: {exc}",
                )

        try:
            context = self._contexts.build_context(
                namespace=ns,
                trigger_ref=trigger.to_dict(),
                adapter_kind=self._model.adapter_kind,
            )
        except StorageError as exc:
            if isinstance(exc, (CorruptStore, DbBusy)):
                raise
            return self._failure(ns, trigger, work.work_id, None, exc)

        calls = 0
        repairs = 0
        proposal: dict[str, Any] | None = None
        last_error: StorageError | None = None
        while calls < MAX_MODEL_CALLS:
            calls += 1
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
                        feedback=feedback,
                    )
            except (CorruptStore, DbBusy):
                raise
            except Exception as exc:  # noqa: BLE001 - model failures are a result
                if last_error is not None and calls > 1:
                    # The model produced syntactically valid JSON that failed
                    # validation, and the one repair could not be sent (for
                    # example the scenario quota refused the next attempt).
                    # Keep the original validation error; record the repair
                    # failure separately so the budget refusal cannot hide it.
                    return StepResult(
                        outcome=OUTCOME_FAILED,
                        namespace=ns,
                        trigger_ref=trigger,
                        work_id=work.work_id,
                        context_id=context.context_id,
                        state_revision=self._store.current_state_revision(ns),
                        model_calls=calls,
                        repairs=repairs,
                        error_code=getattr(last_error, "code", "INVALID_SCHEMA"),
                        error_message=str(last_error),
                        repair_error=f"{type(exc).__name__}: {exc}",
                    )
                return StepResult(
                    outcome=OUTCOME_FAILED,
                    namespace=ns,
                    trigger_ref=trigger,
                    work_id=work.work_id,
                    context_id=context.context_id,
                    state_revision=self._store.current_state_revision(ns),
                    model_calls=calls,
                    repairs=repairs,
                    error_code=MODEL_FAILURE_CODE,
                    error_message=f"{type(exc).__name__}: {exc}",
                )
            try:
                proposal = parse_proposal(raw)
                if proposal["operation"] != "wait":
                    self._validate_proposal(ns, proposal)
                break
            except (StorageError, ValueError) as exc:
                last_error = exc if isinstance(exc, StorageError) else None
                proposal = None
                if calls >= MAX_MODEL_CALLS:
                    return StepResult(
                        outcome=OUTCOME_FAILED,
                        namespace=ns,
                        trigger_ref=trigger,
                        work_id=work.work_id,
                        context_id=context.context_id,
                        state_revision=self._store.current_state_revision(ns),
                        model_calls=calls,
                        repairs=repairs,
                        error_code=getattr(exc, "code", "INVALID_SCHEMA"),
                        error_message=str(exc),
                    )
                repairs += 1
                feedback = repair_feedback(exc)

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
                        model_calls=calls,
                        repairs=repairs,
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
                    model_calls=calls,
                    repairs=repairs,
                    validation_retries=tuple(validation_retries),
                )
            except StorageError as exc:
                if isinstance(exc, (CorruptStore, DbBusy)):
                    raise
                validation_retries.append(str(exc))
                if calls >= MAX_MODEL_CALLS:
                    return StepResult(
                        outcome=OUTCOME_FAILED,
                        namespace=ns,
                        trigger_ref=trigger,
                        work_id=work.work_id,
                        context_id=context.context_id,
                        state_revision=self._store.current_state_revision(ns),
                        model_calls=calls,
                        repairs=repairs,
                        validation_retries=tuple(validation_retries),
                        error_code=getattr(exc, "code", "CORRUPT_STORE"),
                        error_message=str(exc),
                    )
                # One bounded semantic repair.  The rejected transaction did
                # not change state, so the same live Context is still valid.
                repairs += 1
                calls += 1
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
                        model_calls=calls,
                        repairs=repairs,
                        validation_retries=tuple(validation_retries),
                        error_code=getattr(exc, "code", "CORRUPT_STORE"),
                        error_message=str(exc),
                        repair_error=f"{type(model_exc).__name__}: {model_exc}",
                    )
                try:
                    proposal = parse_proposal(raw)
                    if proposal["operation"] != "wait":
                        self._validate_proposal(ns, proposal)
                except (StorageError, ValueError) as retry_exc:
                    return StepResult(
                        outcome=OUTCOME_FAILED,
                        namespace=ns,
                        trigger_ref=trigger,
                        work_id=work.work_id,
                        context_id=context.context_id,
                        state_revision=self._store.current_state_revision(ns),
                        model_calls=calls,
                        repairs=repairs,
                        validation_retries=tuple(validation_retries),
                        error_code=getattr(retry_exc, "code", "INVALID_SCHEMA"),
                        error_message=str(retry_exc),
                    )
                continue

    # ------------------------------------------------------------- internals

    def _validate_proposal(self, namespace: str, proposal: Mapping[str, Any]) -> None:
        operation = proposal["operation"]
        if operation == "propose_belief":
            scope = normalize_scope(proposal["scope"])
            validate_belief_body(proposal["body"], scope=scope)
            return
        if operation == "revise_belief":
            target = RecordRef.from_value(proposal["target"], field="target")
            if target.namespace != namespace:
                raise ScopeViolation(
                    f"target is in namespace {target.namespace!r}, not {namespace!r}"
                )
            scope = normalize_scope(proposal["scope"])
            validate_belief_body(proposal["body"], scope=scope)
            return
        if operation == "retract_belief":
            target = RecordRef.from_value(proposal["target"], field="target")
            if target.namespace != namespace:
                raise ScopeViolation(
                    f"target is in namespace {target.namespace!r}, not {namespace!r}"
                )
            if not proposal.get("evidence_links"):
                raise InvalidInput(
                    "retract_belief must cite the evidence that justifies the "
                    "withdrawal"
                )
            if not str(proposal.get("reason") or "").strip():
                raise InvalidInput("retract_belief requires a non-empty reason")
            return
        raise UnsupportedOperation(
            f"PCS2-008 cannot submit {operation!r}; allowed: propose_belief, "
            "revise_belief, retract_belief, wait"
        )

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




def _erroneous_field(message: str, exc: BaseException) -> str | None:
    """Name the field a structural refusal is about, when the message says so."""

    if isinstance(exc, EvidenceMissing):
        if "applicability in the record scope" in message:
            return "scope"
        if "valid_until" in message:
            return "valid_until"
        return None
    if isinstance(exc, (SourceUntrusted, SourceRedacted)):
        return None
    if isinstance(exc, ScopeViolation) or message.startswith("scope"):
        return "scope"
    if message.startswith("claim_type"):
        return "claim_type"
    if message.startswith("inference_basis"):
        return "inference_basis"
    if message.startswith("strength_reason"):
        return "strength_reason"
    if message.startswith("strength"):
        return "strength"
    if message.startswith("review_condition"):
        return "review_condition"
    if message.startswith("claim"):
        return "claim"
    if message.startswith("valid_until"):
        return "valid_until"
    if message.startswith("alternative_group_id"):
        return "alternative_group_id"
    if "belief body is missing required field(s)" in message:
        return "body"
    if "belief body has unknown field(s)" in message:
        return "body"
    if isinstance(exc, UnsupportedOperation) or message.startswith(
        "model output operation"
    ):
        return "operation"
    if "must have exactly operation" in message or "must be exactly operation" in message:
        return "operation/body/scope"
    if "body and scope must be JSON objects" in message:
        return "body/scope"
    return None


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


def repair_feedback(exc: BaseException) -> str:
    """Specific, contract-derived feedback for the single format repair.

    The old feedback was generic ("return one JSON object"), which did not tell
    the model that ``capability_assessment`` is not a claim type or that
    ``namespace`` is not a scope field.  This function names the erroneous
    field (when the validator message identifies it), states the allowed form
    from the same authoritative constants as the rendered contract, and keeps
    the frozen rejection rules unchanged.
    """

    message = str(exc)
    parts = [f"{type(exc).__name__}: {message}"]
    field = _erroneous_field(message, exc)
    if field is not None:
        parts.append(f"Erroneous field: {field}.")
    if "scope has unknown field(s):" in message:
        bad = message.split("scope has unknown field(s):", 1)[1].strip().rstrip(".")
        parts.append(
            f"scope must not contain {bad!r}; allowed scope fields are: "
            f"{', '.join(SCOPE_FIELDS)}."
        )
    if isinstance(exc, ScopeViolation) and "must be a scope object" in message:
        parts.append("scope must be a JSON object, not a string.")
    evidence_note = (
        "The proposal shape may be valid, but the evidence reference is not "
        "usable; use an EventRef from available_references with "
        "evidence_usable=true, or the exact Episode RecordRef."
    )
    if isinstance(exc, (SourceUntrusted, SourceRedacted)):
        parts.append(evidence_note)
    elif isinstance(exc, EvidenceMissing) and not (
        "scope" in message or "valid_until" in message
    ):
        parts.append(evidence_note)
    else:
        parts.append(proposal_format_hint())
    parts.append(
        "Return exactly one JSON object; do not supply state_revision, "
        "request_id, read_set or context_id."
    )
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
