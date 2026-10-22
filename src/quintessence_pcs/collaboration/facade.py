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

from ..domains.cognition.context_guard import require_live_context
from ..domains.refs import EventRef
from ..foundation import identifiers
from ..foundation.errors import (
    AuthorizationRequired,
    ContextStale,
    CorruptStore,
    InvalidInput,
    RecordNotFound,
    Redacted,
    SnapshotNotFound,
)
from ..foundation.jsonutil import canonical_json_text
from ..runtime.candidate import generate_candidate
from ..runtime.proposal_generation import parse_proposal, validate_proposal
from ..store import CognitionStore, PendingWork
from ..store.collaboration import (
    ACTIVATION_KINDS,
    COLLAB_ARTIFACT,
    COLLAB_INPUT,
    Approval,
    ArtifactRow,
    InteractionRow,
)
from ..store.errors import CandidateTransitionDenied
from ..store.experience import (
    ExperienceAccess,
    ExperienceUnavailable,
    ExperienceView,
)
from ..store.tx_b import CommitBasis, CommitReceipt
from .policy import CommitAuthority
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
                if not any(item.work_id == work_id for item in current):
                    self._store.complete_interaction(
                        namespace=ns,
                        interaction_id=interaction_id,
                        result="stale",
                    )
                    moved = True
                else:
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
    "ReviewFacade",
    "ReviewView",
    "ReviewWorkItem",
    "open_store",
]
