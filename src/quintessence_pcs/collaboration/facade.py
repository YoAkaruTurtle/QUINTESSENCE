"""C07: the review-only trusted host Facade and the Lab data boundary.

This module is the one public surface a trusted host may call for the
collaboration review loop:

    work scan -> Context/generation -> candidate -> approve/reject ->
    commit/WAIT -> result recovery -> experience read

It deliberately implements **only** that loop.  It adds no consult flow, no
model protocol, no query surface, no Persona/Memory integration, no ACL or
identity platform, and no new contract, migration or error code.  The Facade
is an adapter over the C06 trusted internal API
(:class:`~quintessence_pcs.collaboration.service.CollaborationService`), not a
second implementation of it.

Three boundaries shape the module.

* :class:`HostAccess` is a capability object a trusted host constructs.  It is
  not authentication and there is no permission table: an instance only says
  which review actions one host call may attempt.  Model output, candidate
  bodies and request JSON are data and never construct or widen one.
* :class:`ReviewView` never carries body text.  A commit receipt is shown only
  after :meth:`ReviewFacade._disclose_receipt` re-applies the caller's current
  permissions, the Context/trigger deletion barriers and the candidate's
  current privacy classes and purpose.  A hidden receipt never downgrades the
  lifecycle state: a committed fact stays ``committed``.
* :func:`open_store` refuses to create a collaboration database inside the
  source checkout.  Lab data belongs outside the repository; the Facade never
  writes a runtime artefact into it.

Non-guarantees, stated once here and in ``docs/collaboration/FACADE.md``:

* ``review()`` is not exactly-once.  Every call creates a new interaction and a
  fresh request id, so retrying after a lost response can make a second model
  call.  Only an already known ``interaction_id`` can be recovered through
  ``result()``.
* The Facade never runs a public method inside an open store transaction.  The
  C06c ``get_result`` repair path must execute in autocommit mode, and a caller
  that wraps this Facade in its own transaction is refused rather than
  silently joined.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domains.cognition.beliefs import parse_belief_body, validate_belief_body
from ..domains.cognition.context_guard import require_live_context
from ..domains.cognition.errors import EvidenceMissing
from ..domains.refs import EventRef, RecordRef
from ..domains.scope import SCOPE_FIELDS, normalize_scope
from ..foundation import identifiers
from ..foundation.errors import (
    AuthorizationRequired,
    ContextStale,
    CorruptStore,
    InvalidInput,
    RecordNotFound,
    Redacted,
    ScopeViolation,
    SnapshotNotFound,
)
from ..foundation.jsonutil import canonical_json_text
from ..runtime.candidate import generate_candidate
from ..runtime.proposal_generation import parse_proposal, validate_proposal
from ..store import CognitionStore, PendingWork
from ..store.collaboration import (
    ACTIVATION_KINDS,
    CANDIDATE_STATUSES,
    COLLAB_ARTIFACT,
    COLLAB_INPUT,
    Approval,
    ArtifactRow,
    CandidateSummary,
    InteractionRow,
)
from ..store.errors import CandidateTransitionDenied
from ..store.experience import (
    READABILITY_AVAILABLE,
    READABILITY_REDACTED,
    ExperienceAccess,
    ExperienceUnavailable,
    ExperienceView,
)
from ..store.tx_b import CommitBasis, CommitReceipt
from .policy import PROPOSAL_OPERATIONS, CommitAuthority
from .service import DECISION_APPROVE, DECISION_REJECT, CollaborationService

#: The closed capability vocabulary of this card.  A HostAccess may hold any
#: subset; no other permission is meaningful to the review-only Facade.
FACADE_PERMISSIONS = frozenset(
    {
        "review.generate",
        "review.approve",
        "review.reject",
        "result.read",
        "experience.read",
        "work.scan",
    }
)

#: The public review vocabulary.  ``structural_only`` means "parsed and
#: domain-validated, nothing persisted"; ``pending`` means "a candidate row
#: exists and no receipt does"; ``committed`` means "a receipt exists".  The
#: remaining states are honest lifecycle conclusions, never stand-ins for a
#: commit.
REVIEW_STATES = (
    "structural_only",
    "pending",
    "committed",
    "waiting",
    "rejected",
    "stale",
    "denied",
    "failed",
    "incomplete",
)

_POLICY_REF = "policy:collab.v1"
_EXPERIENCE_PURPOSE = "review"
_MAX_TEXT = 512
_MAX_PURPOSE_LENGTH = 64

_RESULT_STATES = {
    "candidate_pending": "pending",
    "committed": "committed",
    "waiting": "waiting",
    "rejected": "rejected",
    "stale": "stale",
    "denied": "denied",
    "failed": "failed",
    "incomplete": "incomplete",
}
_RESOLUTION_STATES = {
    "committed": "committed",
    "already_committed": "committed",
    "rejected": "rejected",
    "stale": "stale",
    "failed": "failed",
}


# --------------------------------------------------------------------------
# public DTOs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HostAccess:
    """Trusted-host capability object for one review call.

    The host constructs it; the Facade only checks it.  ``namespace`` and
    ``host_instance`` bind the call to one store namespace and one host
    installation, ``permissions`` is a subset of :data:`FACADE_PERMISSIONS`,
    ``privacy_classes`` is the set of content classes this call may read, and
    ``purposes`` is the closed set of audit purposes it may state.
    ``access_ref`` is the opaque authorization reference recorded in the
    interaction and the experience read audit.

    This is **not** an ACL or a token.  There is no permission table, no
    revocation store and no identity service; the object is rebuilt by the
    trusted host for every call.
    """

    namespace: str
    host_instance: str
    operator: str
    permissions: frozenset[str]
    privacy_classes: frozenset[str]
    purposes: frozenset[str]
    access_ref: str

    def __post_init__(self) -> None:
        identifiers.require_namespace(self.namespace)
        _require_text(self.host_instance, "host_instance", 256)
        _require_text(self.operator, "operator", 256)
        if not isinstance(self.permissions, frozenset):
            raise InvalidInput("permissions must be a frozenset")
        for permission in self.permissions:
            if permission not in FACADE_PERMISSIONS:
                raise InvalidInput(
                    f"permission must be one of {', '.join(sorted(FACADE_PERMISSIONS))}"
                )
        if not isinstance(self.privacy_classes, frozenset):
            raise InvalidInput("privacy_classes must be a frozenset")
        for privacy in self.privacy_classes:
            if privacy not in identifiers.PRIVACY_CLASSES:
                raise InvalidInput(
                    "privacy_classes must use "
                    + ", ".join(identifiers.PRIVACY_CLASSES)
                )
        if not isinstance(self.purposes, frozenset) or not self.purposes:
            raise InvalidInput("purposes must be a non-empty frozenset")
        for purpose in self.purposes:
            _require_text(purpose, "purpose", _MAX_PURPOSE_LENGTH)
        _require_text(self.access_ref, "access_ref", _MAX_TEXT)


@dataclass(frozen=True)
class ReviewView:
    """One review conclusion; never contains body text.

    ``receipt`` appears only when the current call may see it.  ``redacted``
    records that a receipt (or the body behind it) was hidden, while ``state``
    still reports the real lifecycle conclusion.  ``detail`` carries only a
    reason label or error code, never proposal or model text.
    """

    state: str
    interaction_id: str | None = None
    candidate_id: str | None = None
    receipt: CommitReceipt | None = None
    redacted: bool = False
    detail: str | None = None
    model_adapter: str | None = None

    def __post_init__(self) -> None:
        if self.state not in REVIEW_STATES:
            raise InvalidInput(f"unknown review state {self.state!r}")


@dataclass(frozen=True)
class ReviewWorkItem:
    """One work item that already has a collaboration WorkControl row."""

    work_id: str
    event_id: str
    generation: int
    blocked: bool
    rerun_authorization_ref: str | None


@dataclass(frozen=True)
class ReviewCandidateRef:
    """The exact stored identity of one candidate version (C12c-PRE)."""

    candidate_id: str
    artifact_version: int
    work_id: str
    work_generation: int


@dataclass(frozen=True)
class ReviewCandidateSummary:
    """Read-only candidate metadata; never a body or proposal payload.

    A candidate that is not visible under the caller's access does not appear
    in :meth:`ReviewFacade.candidates`; there is no ``readable=false`` marker
    that would leak its existence.
    """

    candidate: ReviewCandidateRef
    interaction_id: str
    context_id: str
    status: str
    created_at: str
    decided_at: str | None
    decision_ref: str | None
    attempt_ref: dict | None


@dataclass(frozen=True)
class ReviewProposalView:
    """The explicit, field-level projection of one stored candidate proposal."""

    operation: str | None
    target_ref: RecordRef | None
    claim: str | None
    claim_type: str | None
    scope: dict[str, Any] | None
    evidence_links: tuple[dict[str, Any], ...]
    strength: str | None
    strength_reason: str | None
    valid_until: str | None


@dataclass(frozen=True)
class ReviewCurrentView:
    """The exact target Belief version the candidate would change."""

    ref: RecordRef
    claim: str
    claim_type: str
    status: str
    strength: str
    scope: dict[str, Any]
    valid_until: str | None
    applicability: str


@dataclass(frozen=True)
class ReviewDiffField:
    """One field-level difference between the current Belief and a candidate."""

    field: str
    before: Any
    after: Any
    changed: bool


@dataclass(frozen=True)
class ReviewSourceView:
    """One exact source reference and its read availability.

    ``body`` is populated only for an input/artifact source whose body the
    C05c read surface may disclose.  Event and record sources never carry body
    text here.
    """

    ref: dict[str, Any]
    kind: str
    availability: str
    reason: str | None
    body: str | None
    ref_version: int | None


@dataclass(frozen=True)
class ReviewCandidateDetail:
    """One candidate's explicit review projection (C12c-PRE)."""

    candidate: ReviewCandidateSummary
    proposal: ReviewProposalView | None
    structural_validation: str | None
    current: ReviewCurrentView | None
    current_state: str
    diff: tuple[ReviewDiffField, ...]
    sources: tuple[ReviewSourceView, ...]
    can_approve: bool
    can_reject: bool
    eligibility_reason: str | None
    redacted: bool
    detail: str | None


@dataclass(frozen=True)
class _CurrentRead:
    """Internal join of the current view, its state and the full body."""

    view: ReviewCurrentView | None
    state: str
    moved: bool
    body: dict[str, Any] | None


# --------------------------------------------------------------------------
# Facade
# --------------------------------------------------------------------------


class ReviewFacade:
    """The trusted host's review-only entry point.

    All public methods take a :class:`HostAccess` first and refuse to run while
    the store connection is inside a caller-owned transaction.  The Facade
    owns its ``CollaborationService`` so the three C06 write paths
    (``review_start``, ``register_review_candidate``, ``resolve_candidate``)
    and the one recovery path (``get_result``) keep their C06a/b/c semantics.
    """

    def __init__(
        self,
        store: Any,
        *,
        context_service: Any,
        model: Any,
        clock: Any = None,
    ) -> None:
        if not hasattr(store, "connection"):
            raise InvalidInput("store must be a CognitionStore")
        if not callable(getattr(context_service, "build_context", None)):
            raise InvalidInput("context_service must expose build_context")
        if not callable(getattr(model, "propose", None)):
            raise InvalidInput("model must expose propose")
        self._store = store
        self._contexts = context_service
        self._model = model
        self._service = CollaborationService(store, clock=clock)

    # ------------------------------------------------------------- preview

    def preview(self, access: HostAccess, *, raw_output: str) -> ReviewView:
        """Parse and domain-validate one raw model output; write nothing.

        The check is exactly the generation validator: a WAIT is structurally
        legal without a domain body, so it is not passed through
        ``validate_proposal``.  No interaction, candidate, Context, Belief or
        command is created, and no model call is made.
        """

        self._require_entry(access)
        self._require_permission(access, "review.generate")
        try:
            proposal = parse_proposal(raw_output)
            if proposal["operation"] != "wait":
                validate_proposal(access.namespace, proposal)
        except Exception as exc:  # noqa: BLE001 - a parse failure is a view
            return ReviewView(state="failed", detail=_error_label(exc))
        return ReviewView(state="structural_only")

    # ---------------------------------------------------------------- scans

    def legacy_work(self, access: HostAccess) -> tuple[PendingWork, ...]:
        """Pending work with no collaboration control: the old scan set.

        A work item that has any ``collab_work_control`` row is never returned
        here, so the legacy ``run_step`` path and the managed review path stay
        disjoint.  ``run_step`` itself is unchanged.
        """

        self._require_entry(access)
        self._require_permission(access, "work.scan")
        ns = access.namespace
        return tuple(
            work
            for work in self._store.unfinished_work(ns)
            if self._store.work_control(namespace=ns, work_id=work.work_id)
            is None
        )

    def managed_work(self, access: HostAccess) -> tuple[ReviewWorkItem, ...]:
        """Pending work that already has a collaboration control row."""

        self._require_entry(access)
        self._require_permission(access, "work.scan")
        ns = access.namespace
        items: list[ReviewWorkItem] = []
        for work in self._store.unfinished_work(ns):
            control = self._store.work_control(
                namespace=ns, work_id=work.work_id
            )
            if control is None:
                continue
            items.append(
                ReviewWorkItem(
                    work_id=work.work_id,
                    event_id=work.event_id,
                    generation=control.generation,
                    blocked=control.blocked_after_rejection,
                    rerun_authorization_ref=control.rerun_authorization_ref,
                )
            )
        return tuple(items)

    # ----------------------------------------------------------- candidates

    def candidates(
        self,
        access: HostAccess,
        *,
        work_id: str,
        generation: int | None = None,
    ) -> tuple[ReviewCandidateSummary, ...]:
        """List the visible candidate metadata for one Work.

        Only ``collab_artifacts`` metadata is read.  The body and proposal
        payloads are never loaded.  A candidate outside the caller's privacy
        classes or inside a deletion barrier is omitted entirely; the result
        does not distinguish "no candidates" from "no visible candidates".

        The Work must belong to ``access.namespace``; a Work in another
        namespace simply has no rows here.  ``generation`` is an exact Work
        generation filter when supplied.
        """

        self._require_entry(access)
        self._require_permission(access, "work.scan")
        self._require_permission(access, "experience.read")
        ns = access.namespace
        work = _require_uuid(work_id, "work_id")
        if generation is not None and (
            type(generation) is not int or generation < 0
        ):
            raise InvalidInput("generation must be a non-negative integer")
        rows = self._store.candidate_summaries_for_work(
            ns, work, generation
        )
        return tuple(
            self._candidate_summary(row)
            for row in rows
            if self._candidate_metadata_visible(row, access)
        )

    def candidate_detail(
        self,
        access: HostAccess,
        *,
        candidate_id: str,
        artifact_version: int,
    ) -> ReviewCandidateDetail:
        """Read one candidate at its exact artifact version.

        Visibility is decided before any body or proposal payload is loaded:
        the candidate is read through the C05c ``read_experience`` surface, so
        deletion barriers, privacy classes and the ``review`` purpose apply.
        A hidden candidate returns a redacted detail rather than a body, with
        no proposal, diff, sources or actionable flags.

        ``artifact_version`` is the exact stored version.  This method never
        assumes version 1; the existing C07 ``resolve`` path, which the PRE
        card does not modify, still operates on the supported single-version
        candidate shape.
        """

        self._require_entry(access)
        self._require_permission(access, "work.scan")
        self._require_permission(access, "experience.read")
        ns = access.namespace
        identifier = _require_uuid(candidate_id, "candidate_id")
        version = _require_artifact_version(artifact_version)
        row = self._store.candidate_summary_for_ref(
            namespace=ns,
            artifact_id=identifier,
            artifact_version=version,
        )
        if row is None:
            raise RecordNotFound(
                f"no candidate {identifier} version {version} in {ns!r}"
            )

        experience = self._store.read_experience(
            self._experience_access(access),
            ref=_artifact_ref(ns, identifier, version),
            purpose=_EXPERIENCE_PURPOSE,
        )
        if (
            isinstance(experience, ExperienceUnavailable)
            or experience.readability != READABILITY_AVAILABLE
        ):
            return self._redacted_detail(self._candidate_summary(row))

        try:
            artifact = self._store.load_artifact(
                namespace=ns,
                artifact_id=identifier,
                artifact_version=version,
            )
        except (Redacted, RecordNotFound):
            return self._redacted_detail(self._candidate_summary(row))
        if artifact.kind != "candidate":
            raise CorruptStore(f"artifact {identifier} is not a candidate")
        if artifact.artifact_version != version:
            raise CorruptStore(
                f"candidate {identifier} was read at the wrong artifact version"
            )
        if artifact.proposal is None:
            return self._redacted_detail(self._summary_from_artifact(artifact))

        summary = self._summary_from_artifact(artifact)
        proposal = self._proposal_view(ns, artifact.proposal)
        current = self._read_current(ns, proposal.target_ref, access)
        diff = _diff_fields(proposal, current)
        sources = self._source_views(access, artifact, proposal)

        can_approve = True
        can_reject = True
        reason: str | None = None

        if summary.candidate.artifact_version != 1:
            # The supported single-version candidate shape is version 1.
            # resolve() intentionally still binds version 1, so PRE must not
            # advertise an approval/rejection it cannot deliver.
            can_approve = False
            can_reject = False
            reason = "unsupported_version"
        elif summary.status not in CANDIDATE_STATUSES:
            raise CorruptStore(
                f"candidate {identifier} has status {summary.status!r}"
            )
        elif summary.status != "pending":
            can_approve = False
            can_reject = False
            reason = "terminal"
        else:
            control = self._store.work_control(
                namespace=ns, work_id=summary.candidate.work_id
            )
            if (
                control is None
                or control.generation
                != summary.candidate.work_generation
            ):
                can_approve = False
                can_reject = False
                reason = "stale_generation"
            else:
                if proposal.operation not in PROPOSAL_OPERATIONS:
                    can_approve = False
                    reason = "unsupported_operation"
                if current.state == "redacted":
                    can_approve = False
                    reason = "current_redacted"
                elif current.state == "missing":
                    can_approve = False
                    reason = "current_missing"
                elif current.moved:
                    can_approve = False
                    reason = "target_moved"
                elif (
                    current.view is not None
                    and current.view.status in ("retracted", "contradicted")
                ):
                    can_approve = False
                    reason = "current_terminal"
                source_reason = _source_block_reason(sources)
                if source_reason is not None:
                    can_approve = False
                    if reason is None:
                        reason = source_reason

        return ReviewCandidateDetail(
            candidate=summary,
            proposal=proposal,
            structural_validation=row.structural_validation,
            current=current.view,
            current_state=current.state,
            diff=diff,
            sources=sources,
            can_approve=can_approve,
            can_reject=can_reject,
            eligibility_reason=reason,
            redacted=False,
            detail=None,
        )

    # --------------------------------------------------------------- review

    def review(
        self,
        access: HostAccess,
        *,
        work_id: str,
        trigger_event_id: str,
        activation_kind: str = "manual_review",
        feedback: str | None = None,
    ) -> ReviewView:
        """Gate -> Context/generation -> candidate (or a committed WAIT).

        The Work/Event binding is proven *before* any Context or model call:
        a mismatched pair is ``INVALID_SCHEMA`` with zero model calls and no
        new row.  The gate decision is then read from the persisted control.
        A generated proposal never commits here.
        """

        self._require_entry(access)
        self._require_permission(access, "review.generate")
        if activation_kind not in ACTIVATION_KINDS:
            raise InvalidInput(
                "activation_kind must be one of " + ", ".join(ACTIVATION_KINDS)
            )
        if feedback is not None and not isinstance(feedback, str):
            raise InvalidInput("feedback must be None or a string")
        ns = access.namespace
        work = _require_uuid(work_id, "work_id")
        event_id = identifiers.require_event_id(trigger_event_id)

        # P1: the Work must belong to this Event and still be pending.  This
        # one query is the store's existing read; it constrains namespace,
        # event id, pending status, completion and source deletion.
        works = self._store.unfinished_work_for_event(ns, event_id)
        if not any(item.work_id == work for item in works):
            raise InvalidInput(
                "work does not belong to this event or is not pending"
            )

        decision = self._service.review_start(namespace=ns, work_id=work)
        if not decision.allowed:
            return ReviewView(state="denied", detail=decision.reason)

        trigger = EventRef(namespace=ns, event_id=event_id)
        generated = generate_candidate(
            namespace=ns,
            trigger_ref=trigger,
            context_service=self._contexts,
            model=self._model,
            feedback=feedback,
            focus_work_id=work,
            trigger_as_fact=True,
        )
        if generated.failure is not None:
            return ReviewView(
                state="failed",
                detail=_failure_detail(generated.failure),
                model_adapter=self._adapter_kind(),
            )
        context = generated.context
        proposal = generated.proposal
        if context is None or proposal is None:  # pragma: no cover - guarded
            raise CorruptStore("candidate generation returned no Context or proposal")
        if (
            context.namespace != ns
            or context.trigger_ref.namespace != ns
            or context.trigger_ref.event_id != event_id
        ):
            raise CorruptStore(
                "the generated context does not match the review trigger"
            )

        interaction_id = self._store.register_interaction(
            namespace=ns,
            mode="review",
            activation_kind=activation_kind,
            host_instance=access.host_instance,
            access_ref=access.access_ref,
            policy_ref=_POLICY_REF,
            context_id=context.context_id,
            trigger_ref=trigger.to_dict(),
            work_id=work,
            work_generation=decision.generation,
        )

        if proposal["operation"] == "wait":
            return self._review_wait(
                access,
                ns=ns,
                work_id=work,
                event_id=event_id,
                context_id=context.context_id,
                interaction_id=interaction_id,
                generation=decision.generation,
                proposal=proposal,
            )

        return self._review_candidate(
            access,
            ns=ns,
            work_id=work,
            event_id=event_id,
            context=context,
            proposal=proposal,
            interaction_id=interaction_id,
            generation=decision.generation,
        )

    # -------------------------------------------------------------- resolve

    def resolve(
        self,
        access: HostAccess,
        *,
        candidate_id: str,
        decision: str,
        decision_id: str,
        grant_id: str | None = None,
    ) -> ReviewView:
        """Approve or reject one candidate; the host supplies no Approval.

        ``approve`` builds the exact :class:`Approval` binding from the stored
        candidate, or presents one commit grant id.  ``reject`` presents no
        authority and requires its own permission.  The interaction must
        belong to the same ``host_instance`` as the access object.
        """

        self._require_entry(access)
        if decision not in (DECISION_APPROVE, DECISION_REJECT):
            raise InvalidInput("decision must be approve or reject")
        self._require_permission(
            access,
            "review.approve" if decision == DECISION_APPROVE else "review.reject",
        )
        ns = access.namespace
        candidate = self._load_candidate(ns, candidate_id)
        interaction = self._require_own_interaction(
            ns, candidate.interaction_id, access
        )

        if decision == DECISION_APPROVE:
            authority = (
                CommitAuthority(grant_id=grant_id)
                if grant_id is not None
                else CommitAuthority(approval=self._approval_for(candidate))
            )
        else:
            if grant_id is not None:
                raise InvalidInput("reject does not accept a commit grant")
            authority = None

        resolution = self._service.resolve_candidate(
            namespace=ns,
            candidate_id=candidate.artifact_id,
            decision=decision,
            decision_ref=decision_id,
            authority=authority,
        )
        state = _RESOLUTION_STATES.get(resolution.status)
        if state is None:  # pragma: no cover - Resolution is a closed set
            raise CorruptStore(
                f"unexpected resolution status {resolution.status!r}"
            )
        resolved_id = resolution.candidate.artifact_id
        receipt, redacted = self._disclose_receipt(
            access,
            interaction,
            candidate_id=resolved_id,
            receipt=resolution.receipt,
            candidate_ref=_artifact_ref(ns, resolved_id),
        )
        return ReviewView(
            state=state,
            interaction_id=interaction.interaction_id,
            candidate_id=resolved_id,
            receipt=receipt,
            redacted=redacted,
            detail=resolution.status if state in ("stale", "failed") else None,
        )

    # --------------------------------------------------------------- result

    def result(
        self, access: HostAccess, *, interaction_id: str
    ) -> ReviewView:
        """Recover one interaction result, then disclose its receipt.

        The interaction must belong to this host instance.  Recovery runs in
        autocommit mode; the label repair C06c may perform is not this card's
        business.  The returned state is the real lifecycle conclusion even
        when the current access hides the receipt.
        """

        self._require_entry(access)
        self._require_permission(access, "result.read")
        ns = access.namespace
        interaction = self._require_own_interaction(
            ns, interaction_id, access
        )
        recovered = self._service.get_result(
            namespace=ns, interaction_id=interaction.interaction_id
        )
        state = _RESULT_STATES.get(recovered.result)
        if state is None:  # pragma: no cover - recovery is a closed set
            raise CorruptStore(
                f"unexpected interaction result {recovered.result!r}"
            )
        candidate_ref = (
            None
            if recovered.candidate_id is None
            else _artifact_ref(ns, recovered.candidate_id)
        )
        receipt, redacted = self._disclose_receipt(
            access,
            interaction,
            candidate_id=recovered.candidate_id,
            receipt=recovered.receipt,
            candidate_ref=candidate_ref,
        )
        return ReviewView(
            state=state,
            interaction_id=interaction.interaction_id,
            candidate_id=recovered.candidate_id,
            receipt=receipt,
            redacted=redacted,
            detail=(
                recovered.result
                if state in ("stale", "denied", "failed", "incomplete")
                else None
            ),
        )

    # ------------------------------------------------------- read experience

    def read_experience(
        self, access: HostAccess, *, ref: Any, purpose: str
    ) -> ExperienceView | ExperienceUnavailable:
        """Read one exact experience reference through the host's access.

        The body is returned by the C05c read surface unchanged; this method
        never copies it into a :class:`ReviewView` and never interprets it.
        A missing, barred, foreign, wrong-privacy-class or wrong-purpose
        reference is :class:`ExperienceUnavailable`, exactly as in C05c.
        """

        self._require_entry(access)
        self._require_permission(access, "experience.read")
        if not isinstance(purpose, str) or not purpose:
            raise InvalidInput("purpose must be a non-empty string")
        return self._store.read_experience(
            self._experience_access(access), ref=ref, purpose=purpose
        )

    # ------------------------------------------------- candidate read internals

    def _candidate_summary(
        self, row: CandidateSummary
    ) -> ReviewCandidateSummary:
        """Map one metadata-only store row to the public summary."""

        return ReviewCandidateSummary(
            candidate=ReviewCandidateRef(
                candidate_id=row.artifact_id,
                artifact_version=row.artifact_version,
                work_id=row.work_id,
                work_generation=row.work_generation,
            ),
            interaction_id=row.interaction_id,
            context_id=row.context_id,
            status=row.status,
            created_at=row.created_at,
            decided_at=row.decided_at,
            decision_ref=row.decision_ref,
            attempt_ref=row.attempt_ref,
        )

    def _summary_from_artifact(
        self, artifact: ArtifactRow
    ) -> ReviewCandidateSummary:
        """Build the same summary from a loaded artifact row."""

        if artifact.status not in CANDIDATE_STATUSES:
            raise CorruptStore(
                f"candidate {artifact.artifact_id} has status "
                f"{artifact.status!r}"
            )
        if artifact.work_id is None or artifact.work_generation is None:
            raise CorruptStore(
                f"candidate {artifact.artifact_id} is missing its Work binding"
            )
        attempt_ref = artifact.attempt_ref
        if attempt_ref is not None and not isinstance(attempt_ref, dict):
            raise CorruptStore(
                f"candidate {artifact.artifact_id} has a non-object attempt_ref"
            )
        return ReviewCandidateSummary(
            candidate=ReviewCandidateRef(
                candidate_id=artifact.artifact_id,
                artifact_version=artifact.artifact_version,
                work_id=artifact.work_id,
                work_generation=artifact.work_generation,
            ),
            interaction_id=artifact.interaction_id,
            context_id=artifact.context_id,
            status=artifact.status,
            created_at=artifact.created_at,
            decided_at=artifact.decided_at,
            decision_ref=artifact.decision_ref,
            attempt_ref=attempt_ref,
        )

    def _candidate_metadata_visible(
        self, row: CandidateSummary, access: HostAccess
    ) -> bool:
        """Apply the C05c visibility rule using metadata only.

        This calls the same read-only privacy-closure helper
        ``read_experience`` uses, so the list cannot become more permissive
        than the detail read.  A deletion barrier always hides the candidate.
        No payload is loaded and no experience read audit row is written.
        """

        if _EXPERIENCE_PURPOSE not in access.purposes:
            return False
        if self._store.artifact_barred(
            namespace=row.namespace,
            artifact_id=row.artifact_id,
            artifact_version=row.artifact_version,
        ):
            return False
        privacy = self._store._artifact_privacy_classes(
            namespace=row.namespace,
            artifact_id=row.artifact_id,
            artifact_version=row.artifact_version,
            depth=0,
            seen=set(),
        )
        return privacy is not None and privacy.issubset(access.privacy_classes)

    def _redacted_detail(
        self, summary: ReviewCandidateSummary
    ) -> ReviewCandidateDetail:
        """The one fail-closed detail for a candidate that cannot be read."""

        return ReviewCandidateDetail(
            candidate=summary,
            proposal=None,
            structural_validation=None,
            current=None,
            current_state="not_applicable",
            diff=(),
            sources=(),
            can_approve=False,
            can_reject=False,
            eligibility_reason="candidate_redacted",
            redacted=True,
            detail="candidate_redacted",
        )

    def _proposal_view(
        self, namespace: str, proposal: dict[str, Any]
    ) -> ReviewProposalView:
        """Project a stored proposal into explicit C12c fields.

        The raw proposal object is never returned.  A candidate proposal that
        no longer satisfies the frozen shape is storage corruption, not a view.
        """

        operation = proposal.get("operation")
        if not isinstance(operation, str) or not operation:
            raise CorruptStore("a stored candidate proposal has no operation")

        target_ref: RecordRef | None = None
        raw_target = proposal.get("target")
        if raw_target is not None:
            try:
                target_ref = RecordRef.from_value(raw_target, field="target")
            except (InvalidInput, ScopeViolation) as exc:
                raise CorruptStore(
                    "a stored candidate proposal has an invalid target"
                ) from exc
            if target_ref.namespace != namespace:
                raise CorruptStore(
                    "a stored candidate proposal targets another namespace"
                )

        claim = claim_type = strength = strength_reason = valid_until = None
        scope: dict[str, Any] | None = None
        evidence: tuple[dict[str, Any], ...] = ()

        if operation in ("propose_belief", "revise_belief"):
            raw_body = proposal.get("body")
            raw_scope = proposal.get("scope")
            try:
                scope = normalize_scope(raw_scope)
                body = validate_belief_body(
                    raw_body, scope=scope, enforce_support_rules=False
                )
            except (InvalidInput, ScopeViolation, EvidenceMissing) as exc:
                raise CorruptStore(
                    "a stored candidate proposal has an invalid body"
                ) from exc
            claim = body["claim"]
            claim_type = body["claim_type"]
            strength = body["strength"]
            strength_reason = body["strength_reason"]
            valid_until = body["valid_until"]
            evidence = tuple(dict(link) for link in body["evidence_links"])
        elif operation == "retract_belief":
            raw_links = proposal.get("evidence_links")
            if (
                not isinstance(raw_links, (list, tuple))
                or not raw_links
                or any(not isinstance(item, dict) for item in raw_links)
            ):
                raise CorruptStore(
                    "a stored retraction proposal has invalid evidence_links"
                )
            evidence = tuple(dict(item) for item in raw_links)
            reason = proposal.get("reason")
            # The DTO has no separate retraction-reason field.  The model's
            # bounded reason is surfaced in strength_reason, which is the one
            # explicit free-text reason field the UI already renders.
            strength_reason = reason if isinstance(reason, str) else None

        return ReviewProposalView(
            operation=operation,
            target_ref=target_ref,
            claim=claim,
            claim_type=claim_type,
            scope=scope,
            evidence_links=evidence,
            strength=strength,
            strength_reason=strength_reason,
            valid_until=valid_until,
        )

    def _read_current(
        self,
        namespace: str,
        target_ref: RecordRef | None,
        access: HostAccess,
    ) -> _CurrentRead:
        """Read the candidate's exact target version and the current pointer."""

        if target_ref is None:
            return _CurrentRead(None, "not_applicable", False, None)
        if target_ref.namespace != namespace:
            raise CorruptStore("a candidate target belongs to another namespace")
        try:
            version = self._store.get_version(
                namespace, target_ref.record_id, target_ref.record_version
            )
        except Redacted:
            return _CurrentRead(None, "redacted", False, None)
        except RecordNotFound:
            return _CurrentRead(None, "missing", False, None)
        if version.kind != "belief":
            return _CurrentRead(None, "missing", False, None)
        if version.privacy_class not in access.privacy_classes:
            return _CurrentRead(None, "redacted", False, None)
        body = parse_belief_body(version)
        scope = dict(version.scope or {})
        view = ReviewCurrentView(
            ref=target_ref,
            claim=body["claim"],
            claim_type=body["claim_type"],
            status=version.status,
            strength=body["strength"],
            scope=scope,
            valid_until=body["valid_until"],
            applicability=_applicability(scope),
        )
        try:
            current = self._store.get_current(namespace, target_ref.record_id)
        except Redacted:
            return _CurrentRead(None, "redacted", False, None)
        except RecordNotFound:
            return _CurrentRead(None, "missing", False, None)
        if current.kind != "belief":
            return _CurrentRead(None, "missing", False, None)
        return _CurrentRead(
            view,
            "available",
            current.record_version != version.record_version,
            body,
        )

    def _source_views(
        self,
        access: HostAccess,
        artifact: ArtifactRow,
        proposal: ReviewProposalView,
    ) -> tuple[ReviewSourceView, ...]:
        """Build exact source refs, with no event body read.

        The candidate's own Context/source links come first, then every
        evidence link the readable proposal cites.  A source appears once.
        """

        seen: set[str] = set()
        views: list[ReviewSourceView] = []
        for ref in artifact.source_refs:
            normalized = _normalize_detail_ref(ref, access.namespace)
            if normalized is None:
                raise CorruptStore(
                    "a stored candidate source is not an exact reference"
                )
            key = canonical_json_text(normalized)
            if key in seen:
                continue
            seen.add(key)
            views.append(self._source_view(access, normalized))
        for link in proposal.evidence_links:
            normalized = _normalize_detail_ref(
                link.get("ref") if isinstance(link, dict) else None,
                access.namespace,
            )
            if normalized is None:
                # A retract proposal's evidence link is only structurally
                # checked at commit time.  An unrepresentable ref cannot be
                # given an availability, so it contributes no source view.
                continue
            key = canonical_json_text(normalized)
            if key in seen:
                continue
            seen.add(key)
            views.append(self._source_view(access, normalized))
        return tuple(views)

    def _source_view(
        self, access: HostAccess, ref: dict[str, Any]
    ) -> ReviewSourceView:
        """Return one source availability, reading bodies only when allowed."""

        kind = ref["ref_type"]
        namespace = access.namespace
        if kind == "event":
            event_id = ref["event_id"]
            if self._store.event_is_deleted(namespace, event_id):
                return ReviewSourceView(
                    ref=ref,
                    kind=kind,
                    availability="redacted",
                    reason="deleted",
                    body=None,
                    ref_version=None,
                )
            header = self._store.event_header(namespace, event_id)
            if header is None:
                return ReviewSourceView(
                    ref=ref,
                    kind=kind,
                    availability="unavailable",
                    reason="not_found",
                    body=None,
                    ref_version=None,
                )
            if header.privacy_class not in access.privacy_classes:
                return ReviewSourceView(
                    ref=ref,
                    kind=kind,
                    availability="redacted",
                    reason="privacy",
                    body=None,
                    ref_version=None,
                )
            return ReviewSourceView(
                ref=ref,
                kind=kind,
                availability="available",
                reason=None,
                body=None,
                ref_version=None,
            )
        if kind == "record":
            record_id = ref["record_id"]
            record_version = ref["record_version"]
            if self._store.record_is_deleted(namespace, record_id):
                return ReviewSourceView(
                    ref=ref,
                    kind=kind,
                    availability="redacted",
                    reason="deleted",
                    body=None,
                    ref_version=record_version,
                )
            try:
                version = self._store.get_version(
                    namespace, record_id, record_version
                )
            except Redacted:
                return ReviewSourceView(
                    ref=ref,
                    kind=kind,
                    availability="redacted",
                    reason="deleted",
                    body=None,
                    ref_version=record_version,
                )
            except RecordNotFound:
                return ReviewSourceView(
                    ref=ref,
                    kind=kind,
                    availability="unavailable",
                    reason="not_found",
                    body=None,
                    ref_version=record_version,
                )
            if version.privacy_class not in access.privacy_classes:
                return ReviewSourceView(
                    ref=ref,
                    kind=kind,
                    availability="redacted",
                    reason="privacy",
                    body=None,
                    ref_version=record_version,
                )
            return ReviewSourceView(
                ref=ref,
                kind=kind,
                availability="available",
                reason=None,
                body=None,
                ref_version=record_version,
            )

        experience = self._store.read_experience(
            self._experience_access(access),
            ref=ref,
            purpose=_EXPERIENCE_PURPOSE,
        )
        ref_version = (
            ref["content_version"]
            if kind == COLLAB_INPUT
            else ref["artifact_version"]
        )
        if isinstance(experience, ExperienceUnavailable):
            return ReviewSourceView(
                ref=ref,
                kind=kind,
                availability="unavailable",
                reason="unavailable",
                body=None,
                ref_version=ref_version,
            )
        if experience.readability == READABILITY_AVAILABLE:
            return ReviewSourceView(
                ref=ref,
                kind=kind,
                availability="available",
                reason=None,
                body=experience.body,
                ref_version=ref_version,
            )
        if experience.readability == READABILITY_REDACTED:
            return ReviewSourceView(
                ref=ref,
                kind=kind,
                availability="redacted",
                reason="redacted",
                body=None,
                ref_version=ref_version,
            )
        return ReviewSourceView(
            ref=ref,
            kind=kind,
            availability="unavailable",
            reason="unavailable",
            body=None,
            ref_version=ref_version,
        )

    # ------------------------------------------------------ review internals

    def _review_wait(
        self,
        access: HostAccess,
        *,
        ns: str,
        work_id: str,
        event_id: str,
        context_id: str,
        interaction_id: str,
        generation: int,
        proposal: Mapping[str, Any],
    ) -> ReviewView:
        """Commit one WAIT through Tx B, completing the work atomically."""

        guard = _ReviewWaitGuard(
            store=self._store,
            namespace=ns,
            work_id=work_id,
            event_id=event_id,
            generation=generation,
            interaction_id=interaction_id,
            context_id=context_id,
        )

        def apply(tx: Any) -> None:
            require_live_context(
                self._store, context_id=context_id, namespace=ns
            )
            tx.complete_work(
                work_id=work_id,
                context_id=context_id,
                outcome="checked_no_belief",
            )

        try:
            receipt = self._store.commit_command(
                context_id=context_id,
                request_id=str(uuid.uuid4()),
                operation="cognition.wait",
                request_payload={
                    "reason": proposal["reason"],
                    "work_id": work_id,
                },
                apply=apply,
                namespace=ns,
                guard=guard,
            )
        except CandidateTransitionDenied:
            # The guard rolled the whole WAIT transaction back.  The
            # interaction is now a stale attempt, not a receipt holder.
            updated = self._store.complete_interaction(
                namespace=ns,
                interaction_id=interaction_id,
                result="stale",
            )
            return ReviewView(
                state="stale",
                interaction_id=updated.interaction_id,
                detail="work_moved",
                model_adapter=self._adapter_kind(),
            )

        updated = self._store.complete_interaction(
            namespace=ns,
            interaction_id=interaction_id,
            result="waiting",
            receipt_request_id=receipt.request_id,
        )
        disclosed, redacted = self._disclose_receipt(
            access,
            updated,
            candidate_id=None,
            receipt=receipt,
            candidate_ref=None,
        )
        return ReviewView(
            state="waiting",
            interaction_id=updated.interaction_id,
            receipt=disclosed,
            redacted=redacted,
            model_adapter=self._adapter_kind(),
        )

    def _review_candidate(
        self,
        access: HostAccess,
        *,
        ns: str,
        work_id: str,
        event_id: str,
        context: Any,
        proposal: Mapping[str, Any],
        interaction_id: str,
        generation: int,
    ) -> ReviewView:
        """Land one candidate only while the Work is still pending.

        The final liveness read and ``register_review_candidate`` run in the
        *same* ``BEGIN IMMEDIATE`` transaction, so another writer cannot
        finish the Work between them and leave a candidate for a completed
        Work behind.  Model generation stays outside any database write
        transaction.
        """

        body = canonical_json_text(proposal)
        sources = _context_source_refs(context)
        moved = False
        candidate: ArtifactRow | None = None
        try:
            with self._store.transaction():
                current = self._store.unfinished_work_for_event(ns, event_id)
                rerun_ref: str | None = None
                if not any(item.work_id == work_id for item in current):
                    self._store.complete_interaction(
                        namespace=ns,
                        interaction_id=interaction_id,
                        result="stale",
                    )
                    moved = True
                else:
                    # A blocked Work may only cite the exact rerun reference
                    # already registered on its control.  C05d re-validates
                    # that reference (equality, grant state and the start
                    # gate) inside ``register_candidate``; nothing here is
                    # relaxed or reconstructed.
                    control = self._store.work_control(
                        namespace=ns, work_id=work_id
                    )
                    if control is not None and control.generation != generation:
                        self._store.complete_interaction(
                            namespace=ns,
                            interaction_id=interaction_id,
                            result="stale",
                        )
                        moved = True
                    elif (
                        control is not None
                        and control.blocked_after_rejection
                    ):
                        rerun_ref = control.rerun_authorization_ref
                        if rerun_ref is None:
                            self._store.complete_interaction(
                                namespace=ns,
                                interaction_id=interaction_id,
                                result="stale",
                            )
                            moved = True

                if not moved:
                    candidate = self._service.register_review_candidate(
                        namespace=ns,
                        work_id=work_id,
                        work_generation=generation,
                        interaction_id=interaction_id,
                        context_id=context.context_id,
                        body=body,
                        retention="standard",
                        proposal=proposal,
                        proposal_version="v1",
                        request_id=str(uuid.uuid4()),
                        sources=sources,
                        rerun_authorization_ref=rerun_ref,
                    )
        except CandidateTransitionDenied:
            # The store gate refused the landing (a rejection or a generation
            # move won the race).  The transaction already rolled back; only
            # the interaction label proves the attempt went stale.
            updated = self._store.complete_interaction(
                namespace=ns,
                interaction_id=interaction_id,
                result="stale",
            )
            return ReviewView(
                state="stale",
                interaction_id=updated.interaction_id,
                detail="work_moved",
                model_adapter=self._adapter_kind(),
            )

        if moved or candidate is None:
            return ReviewView(
                state="stale",
                interaction_id=interaction_id,
                detail="work_moved",
                model_adapter=self._adapter_kind(),
            )
        return ReviewView(
            state="pending",
            interaction_id=interaction_id,
            candidate_id=candidate.artifact_id,
            model_adapter=self._adapter_kind(),
        )

    # ------------------------------------------------------ disclosure rule

    def _disclose_receipt(
        self,
        access: HostAccess,
        interaction: InteractionRow,
        *,
        candidate_id: str | None,
        receipt: CommitReceipt | None,
        candidate_ref: dict[str, Any] | None,
    ) -> tuple[CommitReceipt | None, bool]:
        """Return ``(receipt, redacted)`` under the caller's *current* access.

        This is the one receipt-disclosure rule; ``review()`` (immediate WAIT),
        ``resolve()`` and ``result()`` all call it.  It never changes the
        lifecycle state.  Every failed check fails closed with ``None, True``.
        """

        if receipt is None:
            return None, False
        if "result.read" not in access.permissions:
            return None, True
        if interaction.context_id is None:
            return None, True
        if receipt.context_id != interaction.context_id:
            return None, True
        try:
            if self._store.context_barred(
                namespace=access.namespace,
                context_id=interaction.context_id,
            ):
                return None, True
        except SnapshotNotFound:
            return None, True
        trigger = interaction.trigger_ref or {}
        event_id = trigger.get("event_id")
        if event_id is not None and self._store.event_is_deleted(
            access.namespace, event_id
        ):
            return None, True

        if candidate_id is None:
            # A WAIT receipt has no body, but it is still bound to the exact
            # interaction, Context and trigger.  It is disclosed only while
            # those sources are visible under the current privacy classes and
            # purposes; otherwise it stays hidden with the state unchanged.
            if receipt.operation != "cognition.wait":
                return None, True
            if receipt.record_refs:
                return None, True
            experience_access = self._experience_access(access)
            if _EXPERIENCE_PURPOSE not in experience_access.purposes:
                return None, True
            if not self._context_sources_visible(
                experience_access, context_id=interaction.context_id
            ):
                return None, True
            return receipt, False

        if self._store.artifact_barred(
            namespace=access.namespace, artifact_id=candidate_id
        ):
            return None, True
        experience_access = self._experience_access(access)
        if candidate_ref is None:  # pragma: no cover - caller passes one
            return None, True
        view = self._store.read_experience(
            experience_access, ref=candidate_ref, purpose=_EXPERIENCE_PURPOSE
        )
        if isinstance(view, ExperienceUnavailable):
            return None, True
        return receipt, False

    def _context_sources_visible(
        self, access: ExperienceAccess, *, context_id: str
    ) -> bool:
        """Whether every exact Context source is readable under *access*.

        Only public reads are used.  An unknown source kind, a missing row, a
        deletion barrier or a privacy class outside the access set makes the
        whole Context invisible (fail closed).  Collaboration sources are
        checked by the C05c read surface itself, which also applies the
        purpose and the full recursive privacy closure.
        """

        for source in self._store.context_sources(context_id):
            if source.ref_kind == "event":
                if self._store.event_is_deleted(
                    access.namespace, source.ref_id
                ):
                    return False
                header = self._store.event_header(
                    access.namespace, source.ref_id
                )
                if (
                    header is None
                    or header.privacy_class not in access.privacy_classes
                ):
                    return False
            elif source.ref_kind == "record":
                if self._store.record_is_deleted(
                    access.namespace, source.ref_id
                ):
                    return False
                try:
                    version = self._store.get_version(
                        access.namespace,
                        source.ref_id,
                        source.ref_version,
                    )
                except (RecordNotFound, Redacted):
                    return False
                if version.privacy_class not in access.privacy_classes:
                    return False
            elif source.ref_kind == COLLAB_INPUT:
                try:
                    loaded = self._store.load_input(
                        namespace=access.namespace,
                        input_id=source.ref_id,
                        content_version=source.ref_version,
                    )
                except (RecordNotFound, Redacted):
                    return False
                if loaded.privacy_class not in access.privacy_classes:
                    return False
            elif source.ref_kind == COLLAB_ARTIFACT:
                view = self._store.read_experience(
                    access,
                    ref=_artifact_ref(
                        access.namespace, source.ref_id, source.ref_version
                    ),
                    purpose=_EXPERIENCE_PURPOSE,
                )
                if isinstance(view, ExperienceUnavailable):
                    return False
            else:  # pragma: no cover - stored kinds are a closed set
                return False
        return True

    # --------------------------------------------------------------- shared

    def _require_entry(self, access: Any) -> None:
        """Every public method starts here (C07 §3.1)."""

        if not isinstance(access, HostAccess):
            raise InvalidInput("access must be a HostAccess")
        if self._store.connection.in_transaction:
            raise InvalidInput(
                "facade methods must not run inside an open store transaction"
            )

    def _require_permission(self, access: HostAccess, permission: str) -> None:
        if permission not in access.permissions:
            raise AuthorizationRequired(
                f"the host access does not hold {permission!r}"
            )

    def _experience_access(self, access: HostAccess) -> ExperienceAccess:
        return ExperienceAccess(
            namespace=access.namespace,
            host_instance=access.host_instance,
            access_ref=access.access_ref,
            privacy_classes=access.privacy_classes,
            purposes=access.purposes,
        )

    def _adapter_kind(self) -> str | None:
        kind = getattr(self._model, "adapter_kind", None)
        return kind if isinstance(kind, str) and kind else None

    def _load_candidate(self, ns: str, candidate_id: str) -> ArtifactRow:
        identifier = _require_uuid(candidate_id, "candidate_id")
        try:
            candidate = self._store.load_artifact(
                namespace=ns,
                artifact_id=identifier,
                artifact_version=1,
            )
        except Redacted as exc:
            raise ContextStale(
                f"candidate {identifier} is inside a deletion barrier"
            ) from exc
        if candidate.kind != "candidate":
            raise InvalidInput(
                f"artifact {candidate.artifact_id} is not a candidate"
            )
        if (
            candidate.interaction_id is None
            or candidate.context_id is None
            or candidate.proposal_digest is None
            or candidate.request_id is None
        ):
            raise CorruptStore(
                f"candidate {candidate.artifact_id} is missing its identity"
            )
        return candidate

    def _approval_for(self, candidate: ArtifactRow) -> Approval:
        return Approval(
            candidate_id=candidate.artifact_id,
            proposal_digest=candidate.proposal_digest,
            context_id=candidate.context_id,
            request_id=candidate.request_id,
        )

    def _require_own_interaction(
        self, ns: str, interaction_id: str, access: HostAccess
    ) -> InteractionRow:
        identifier = _require_uuid(interaction_id, "interaction_id")
        interaction = self._store.interaction(
            namespace=ns, interaction_id=identifier
        )
        if interaction is None:
            raise RecordNotFound(f"no interaction {identifier} in {ns!r}")
        host = self._interaction_host(ns, identifier)
        if host is None:
            raise RecordNotFound(f"no interaction {identifier} in {ns!r}")
        if host != access.host_instance:
            raise AuthorizationRequired(
                "the interaction belongs to another host instance"
            )
        return interaction

    def _interaction_host(
        self, ns: str, interaction_id: str
    ) -> str | None:
        """Read the persisted host instance for one interaction.

        ``InteractionRow`` intentionally carries no body, but it also omits
        this one identity column; the Facade reads it with a single read-only
        SELECT against the frozen 0008 schema instead of widening the store
        API in this card.
        """

        row = self._store.connection.execute(
            "SELECT host_instance FROM collab_interactions "
            "WHERE namespace = ? AND interaction_id = ?",
            (ns, interaction_id),
        ).fetchone()
        return None if row is None else str(row["host_instance"])


# --------------------------------------------------------------------------
# WAIT guard
# --------------------------------------------------------------------------


class _ReviewWaitGuard:
    """C07's in-transaction re-check for one WAIT commit.

    It runs after Tx B's three watermark checks and before ``apply``.  Every
    binding is re-read from the same serialized transaction: the interaction
    row, its trigger, the Work's pending state and the review gate generation.
    A failure raises :class:`CandidateTransitionDenied`, which rolls the whole
    WAIT transaction back, so no ``work_progress`` row and no receipt survive.

    This guard is independent of C06b's ``_NoManagedWorkGuard``; neither path
    is reused or modified.
    """

    def __init__(
        self,
        *,
        store: Any,
        namespace: str,
        work_id: str,
        event_id: str,
        generation: int,
        interaction_id: str,
        context_id: str,
    ) -> None:
        self._store = store
        self._ns = namespace
        self._work_id = work_id
        self._event_id = event_id
        self._generation = generation
        self._interaction_id = interaction_id
        self._context_id = context_id

    def before_apply(self, basis: CommitBasis) -> None:
        if basis.context_id != self._context_id:
            raise CandidateTransitionDenied("wait context mismatch")
        interaction = self._store.interaction(
            namespace=self._ns, interaction_id=self._interaction_id
        )
        if interaction is None:
            raise CandidateTransitionDenied("wait interaction missing")
        if (
            interaction.work_id != self._work_id
            or interaction.work_generation != self._generation
            or interaction.context_id != basis.context_id
        ):
            raise CandidateTransitionDenied("wait binding changed")
        trigger = interaction.trigger_ref or {}
        if trigger.get("event_id") != self._event_id:
            raise CandidateTransitionDenied("wait trigger changed")
        works = self._store.unfinished_work_for_event(
            self._ns, self._event_id
        )
        if not any(item.work_id == self._work_id for item in works):
            raise CandidateTransitionDenied("the work is no longer pending")
        decision = self._store.review_start_decision(
            namespace=self._ns, work_id=self._work_id
        )
        if not decision.allowed or decision.generation != self._generation:
            raise CandidateTransitionDenied(
                "the review start gate closed during generation"
            )

    def after_receipt(
        self, basis: CommitBasis, receipt: CommitReceipt
    ) -> None:
        del basis, receipt
        return None


# --------------------------------------------------------------------------
# Lab data isolation
# --------------------------------------------------------------------------


def open_store(path: str | Path) -> CognitionStore:
    """Open a collaboration database that lives outside the source checkout.

    Lab runs keep source code, private-instance data and run artefacts in
    separate directory trees.  A path inside this checkout is refused before
    any parent directory or SQLite file is created, so a mistaken environment
    variable cannot leak a database, a ``-journal``/``-wal`` sibling or a log
    into the repository.
    """

    resolved = Path(path).expanduser().resolve()
    repo = Path(__file__).resolve().parents[3]
    if resolved == repo or repo in resolved.parents:
        raise InvalidInput(
            "the collaboration store must live outside the source checkout"
        )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return CognitionStore.open(resolved)


# --------------------------------------------------------------------------
# candidate projection helpers
# --------------------------------------------------------------------------


def _diff_fields(
    proposal: ReviewProposalView, current: _CurrentRead
) -> tuple[ReviewDiffField, ...]:
    """Compare only fields both sides actually describe.

    A propose proposal has no target, so "before" is genuinely absent.  A
    redacted or missing current Belief is *not* an empty Belief: its diff is
    empty rather than a fabricated all-changed comparison.
    """

    if current.state == "not_applicable":
        if proposal.operation != "propose_belief":
            return ()
        return tuple(
            ReviewDiffField(field=name, before=None, after=value, changed=True)
            for name, value in _proposal_fields(proposal)
        )
    if current.state != "available" or current.body is None:
        return ()
    if proposal.operation == "revise_belief":
        before = {
            "claim": current.body["claim"],
            "claim_type": current.body["claim_type"],
            "scope": dict(current.view.scope) if current.view is not None else {},
            "evidence_links": list(current.body["evidence_links"]),
            "strength": current.body["strength"],
            "valid_until": current.body["valid_until"],
        }
        return tuple(
            ReviewDiffField(
                field=name,
                before=before[name],
                after=value,
                changed=canonical_json_text(before[name])
                != canonical_json_text(value),
            )
            for name, value in _proposal_fields(proposal)
        )
    if proposal.operation == "retract_belief":
        before_links = list(current.body["evidence_links"])
        after_links = list(proposal.evidence_links)
        return (
            ReviewDiffField(
                field="evidence_links",
                before=before_links,
                after=after_links,
                changed=canonical_json_text(before_links)
                != canonical_json_text(after_links),
            ),
        )
    return ()


def _proposal_fields(
    proposal: ReviewProposalView,
) -> tuple[tuple[str, Any], ...]:
    """The explicit fields a Belief proposal can change."""

    return (
        ("claim", proposal.claim),
        ("claim_type", proposal.claim_type),
        ("scope", proposal.scope or {}),
        ("evidence_links", list(proposal.evidence_links)),
        ("strength", proposal.strength),
        ("valid_until", proposal.valid_until),
    )


def _source_block_reason(
    sources: tuple[ReviewSourceView, ...]
) -> str | None:
    """One public reason label for the strongest source gap, if any."""

    if any(source.availability == "redacted" for source in sources):
        return "source_redacted"
    if any(source.availability == "unavailable" for source in sources):
        return "source_unavailable"
    return None


def _applicability(scope: dict[str, Any]) -> str:
    """Render the stored record scope as a UI-ready applicability label."""

    if not scope:
        return "general"
    parts: list[str] = []
    for field in SCOPE_FIELDS:
        if field not in scope:
            continue
        value = scope[field]
        if isinstance(value, list):
            rendered = ", ".join(str(item) for item in value)
        else:
            rendered = str(value)
        parts.append(f"{field}={rendered}")
    return "; ".join(parts)


def _normalize_detail_ref(
    value: Any, namespace: str
) -> dict[str, Any] | None:
    """Normalize one exact source ref, or return ``None`` when not exact."""

    if not isinstance(value, Mapping):
        return None
    ref_type = value.get("ref_type")
    try:
        if ref_type == "event":
            if set(value) != {"ref_type", "namespace", "event_id"}:
                return None
            if value["namespace"] != namespace:
                return None
            return {
                "ref_type": "event",
                "namespace": identifiers.require_namespace(value["namespace"]),
                "event_id": identifiers.require_event_id(value["event_id"]),
            }
        if ref_type == "record":
            if set(value) != {
                "ref_type",
                "namespace",
                "record_id",
                "record_version",
            }:
                return None
            if value["namespace"] != namespace:
                return None
            return {
                "ref_type": "record",
                "namespace": identifiers.require_namespace(value["namespace"]),
                "record_id": identifiers.require_record_id(value["record_id"]),
                "record_version": identifiers.require_record_version(
                    value["record_version"]
                ),
            }
        if ref_type == COLLAB_INPUT:
            if set(value) != {
                "ref_type",
                "namespace",
                "input_id",
                "content_version",
            }:
                return None
            if value["namespace"] != namespace:
                return None
            return {
                "ref_type": COLLAB_INPUT,
                "namespace": identifiers.require_namespace(value["namespace"]),
                "input_id": _require_uuid(value["input_id"], "input_id"),
                "content_version": _require_artifact_version(
                    value["content_version"]
                ),
            }
        if ref_type == COLLAB_ARTIFACT:
            if set(value) != {
                "ref_type",
                "namespace",
                "artifact_id",
                "artifact_version",
            }:
                return None
            if value["namespace"] != namespace:
                return None
            return {
                "ref_type": COLLAB_ARTIFACT,
                "namespace": identifiers.require_namespace(value["namespace"]),
                "artifact_id": _require_uuid(value["artifact_id"], "artifact_id"),
                "artifact_version": _require_artifact_version(
                    value["artifact_version"]
                ),
            }
    except InvalidInput:
        return None
    return None


def _require_artifact_version(value: Any) -> int:
    if (
        type(value) is not int
        or value < 1
        or value > identifiers.MAX_SAFE_INTEGER
    ):
        raise InvalidInput(
            "artifact_version must be a positive JSON-safe integer"
        )
    return value


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _require_text(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{field_name} must be a non-empty string")
    if len(value) > max_length:
        raise InvalidInput(
            f"{field_name} must be at most {max_length} characters"
        )
    return value


def _require_uuid(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or identifiers.UUID_PATTERN.fullmatch(value) is None
    ):
        raise InvalidInput(f"{field_name} must be a canonical lower-case UUID")
    return value


def _error_label(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    return type(exc).__name__


def _failure_detail(failure: Any) -> str:
    return f"{failure.phase}:{_error_label(failure.cause)}"


def _artifact_ref(
    namespace: str, artifact_id: str, artifact_version: int = 1
) -> dict[str, Any]:
    return {
        "ref_type": COLLAB_ARTIFACT,
        "namespace": namespace,
        "artifact_id": artifact_id,
        "artifact_version": artifact_version,
    }


def _context_source_refs(context: Any) -> list[dict[str, Any]]:
    """Map one Context's exact sources to contract reference objects."""

    refs: list[dict[str, Any]] = []
    for source in context.sources:
        if source.ref_kind == "event":
            refs.append(
                {
                    "ref_type": "event",
                    "namespace": context.namespace,
                    "event_id": source.ref_id,
                }
            )
        elif source.ref_kind == "record":
            refs.append(
                {
                    "ref_type": "record",
                    "namespace": context.namespace,
                    "record_id": source.ref_id,
                    "record_version": source.ref_version,
                }
            )
        else:  # pragma: no cover - review Contexts are event/record only
            raise CorruptStore(
                f"a review context source has unknown kind {source.ref_kind!r}"
            )
    return refs


__all__ = [
    "FACADE_PERMISSIONS",
    "HostAccess",
    "REVIEW_STATES",
    "ReviewCandidateDetail",
    "ReviewCandidateRef",
    "ReviewCandidateSummary",
    "ReviewCurrentView",
    "ReviewDiffField",
    "ReviewFacade",
    "ReviewProposalView",
    "ReviewSourceView",
    "ReviewView",
    "ReviewWorkItem",
    "open_store",
]
