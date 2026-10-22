"""C06a/C06b/C06c: trusted commit, review resolution and result recovery.

One call to :meth:`CollaborationService.commit_candidate` performs the whole
decision as **one** Tx B write transaction:

1. the candidate is loaded outside the transaction for an early, read-only
   rejection and for a durable-replay check;
2. ``BeliefService.submit_proposal`` opens the one ``commit_command``
   transaction whose operation and request payload *are* the candidate's
   original proposal;
3. :class:`_CandidateCommitGuard` re-proves, inside that transaction, that the
   candidate is the current pending generation, that the command identity is
   exact, that the persisted interaction trigger kind still matches, that the
   presented authority allows this operation, and that neither the Context nor
   the candidate is behind a deletion barrier;
4. the domain apply appends the Belief version and the Tx B receipt is written;
5. ``after_receipt`` marks the candidate ``committed`` in the same transaction.

Any failure rolls all five steps back.  A candidate whose command already
committed replays the stored receipt and writes nothing; a concurrent
same-digest early return from Tx B is accepted only if the candidate is
observably ``committed`` afterwards.

C06b adds the three review checkpoints and one resolution entry point:

* :meth:`CollaborationService.review_start` is the read-only gate in front of
  any model attempt.  It delegates to ``review_start_decision`` unchanged: a
  blocked work with a registered, active rerun grant may *generate* a
  candidate, and that decision never authorizes the legacy direct-commit path;
* :meth:`CollaborationService.register_review_candidate` lands one generated
  candidate only when the review is still allowed at the expected generation;
* :meth:`CollaborationService.resolve_candidate` is the one approve/reject
  entry point.  ``approve`` reuses :meth:`commit_candidate`; ``reject`` reuses
  the C05d state transition.  A rejected candidate can never be revived.

Trust boundary (C06a + C06b): this Service is a **trusted internal API**.  It
is not exposed to model output, candidate bodies, request JSON or any untrusted
transport.  A reject ``decision_ref`` is an audit label, not an authorization
token; rejection authority comes from the architectural fact that only a
trusted host decides to call this method.  When C07 opens reject to a wider
caller, the trusted host must check the operator's permission before calling.
An ``already_committed`` historical receipt must have current access and
deletion-barrier checks re-applied before a public Facade returns it: a
successful historical commit does not prove current access authorization.
This card adds no ACL, token or identity system, and it does not change
:class:`CommitAuthority` or the C06a commit semantics.

C06c adds :meth:`CollaborationService.get_result`: it treats the
``processed_commands`` receipt as the only authority for "committed", repairs
only the interaction's ``result``/``receipt_request_id``/``completed_at``
labels, and returns no body text.  The C06b write entry points settle those
labels after their authoritative write; a crash between the two is the W2
window that ``get_result`` repairs on the next call.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..domains.cognition.beliefs import BeliefService
from ..domains.cognition.context_guard import require_live_context
from ..foundation import identifiers
from ..foundation.errors import (
    AuthorizationRequired,
    ContextStale,
    CorruptStore,
    InvalidInput,
    RecordNotFound,
    Redacted,
)
from ..foundation.jsonutil import canonical_json_text
from ..foundation.timeutil import utc_now_rfc3339
from ..runtime.proposal_generation import validate_proposal
from ..store.collaboration import (
    NON_TERMINAL_INTERACTION_RESULTS,
    Approval,
    ArtifactRow,
    CandidateState,
    Decision,
    GrantRow,
    InteractionRow,
    WorkControl,
)
from ..store.errors import CandidateTransitionDenied
from ..store.tx_b import CommitBasis, CommitReceipt
from .policy import PROPOSAL_OPERATIONS, CommitAuthority

_MAX_DECISION_REF = 256

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
RESOLVE_DECISIONS = (DECISION_APPROVE, DECISION_REJECT)


@dataclass(frozen=True)
class CandidateCommitOutcome:
    """What one candidate commit did (or replayed)."""

    candidate: ArtifactRow
    receipt: CommitReceipt
    appended: bool
    replayed: bool


@dataclass(frozen=True)
class Resolution:
    """The outcome of one approve/reject decision on a candidate.

    ``status`` is the candidate's resulting state (or ``already_committed``
    when an earlier successful commit is reported back).  ``receipt`` is set
    for ``committed``/``already_committed``; ``control`` is set only by a
    rejection, which is the transition that moves the work control.
    """

    candidate: ArtifactRow
    status: str
    receipt: CommitReceipt | None
    control: WorkControl | None


@dataclass(frozen=True)
class InteractionResult:
    """The recovered lifecycle conclusion for one interaction.

    ``result`` is the interaction label (the lifecycle conclusion), while
    ``candidate_status`` is the raw persisted candidate state.  They may
    legitimately differ -- a deleted candidate can make the interaction
    ``stale`` while the candidate row is still ``pending``.  Recovery never
    rewrites the candidate to match ``result``.
    """

    interaction_id: str
    namespace: str
    mode: str
    result: str
    receipt: CommitReceipt | None
    candidate_id: str | None
    candidate_status: str | None
    repaired: bool
    completed_at: str | None


class CollaborationService:
    """The trusted C06a commit and C06b review-resolution entry point."""

    def __init__(
        self,
        store: Any,
        *,
        belief_service: BeliefService | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if clock is not None and not callable(clock):
            raise InvalidInput("clock must be callable")
        self._store = store
        self._beliefs = belief_service or BeliefService(store)
        #: Trusted wall clock.  It is called inside the Tx B write transaction,
        #: never once during construction, so a caller cannot freeze or
        #: re-choose the authorization time.
        self._clock = clock or utc_now_rfc3339

    # ---------------------------------------------------------------- public

    def commit_candidate(
        self,
        *,
        namespace: str,
        candidate_id: str,
        authority: CommitAuthority,
        decision_ref: str,
    ) -> CandidateCommitOutcome:
        """Commit one pending candidate atomically.

        There is deliberately no ``now`` or ``trigger_kind`` parameter: the
        authorization time comes from the injected trusted clock inside the
        transaction, and the trigger kind comes from the persisted interaction.
        """

        if not isinstance(authority, CommitAuthority):
            raise InvalidInput("authority must be a CommitAuthority")
        ns = identifiers.require_namespace(namespace)
        decision = _require_decision_ref(decision_ref)

        candidate = self._load_candidate(ns, candidate_id)
        receipt = self._store.lookup_command_receipt(
            namespace=ns, request_id=candidate.request_id
        )
        if candidate.status == "committed":
            if receipt is None:
                raise CorruptStore(
                    f"candidate {candidate.artifact_id} is committed without a "
                    "stored command receipt"
                )
            return CandidateCommitOutcome(
                candidate=candidate,
                receipt=receipt,
                appended=bool(receipt.record_refs),
                replayed=True,
            )
        if receipt is not None:
            raise CorruptStore(
                f"candidate {candidate.artifact_id} has a stored command receipt "
                "but is not committed"
            )
        if candidate.status != "pending":
            raise CandidateTransitionDenied(
                f"candidate {candidate.artifact_id} is {candidate.status!r}; "
                "only a pending candidate can be committed"
            )

        proposal = candidate.proposal
        operation = proposal.get("operation")
        if operation not in PROPOSAL_OPERATIONS:
            raise CandidateTransitionDenied(
                f"candidate operation {operation!r} is not one of "
                + ", ".join(sorted(PROPOSAL_OPERATIONS))
            )
        _validate_candidate_proposal(ns, proposal)
        activation = self._activation_kind(ns, candidate)

        guard = _CandidateCommitGuard(
            store=self._store,
            namespace=ns,
            candidate_id=candidate.artifact_id,
            decision_ref=decision,
            approval=authority.approval,
            grant_id=authority.grant_id,
            operation=operation,
            proposal=proposal,
            trigger_kind=activation,
            clock=self._clock,
        )
        submission = self._beliefs.submit_proposal(
            namespace=ns,
            context_id=candidate.context_id,
            request_id=candidate.request_id,
            proposal=proposal,
            # A collaboration work is not a pending_work row: never complete
            # work and never write work_progress on this path.
            work_id=None,
            commit_basis=(operation, proposal),
            guard=guard,
        )

        updated = self._store.load_artifact(
            namespace=ns,
            artifact_id=candidate.artifact_id,
            artifact_version=1,
        )
        if updated.status != "committed":
            raise CorruptStore(
                f"candidate {candidate.artifact_id} is {updated.status!r} after "
                "its command committed; the receipt and the candidate disagree"
            )
        return CandidateCommitOutcome(
            candidate=updated,
            receipt=submission.receipt,
            appended=bool(submission.receipt.record_refs),
            # Tx B short-circuits only when the very same command already
            # committed, and that early return never calls the guard.
            replayed=not guard.entered,
        )

    def review_start(self, *, namespace: str, work_id: str) -> Decision:
        """Gate before any model attempt; read-only, writes nothing.

        Semantics are exactly ``store.review_start_decision``: a blocked work
        with a registered, active rerun grant is allowed to *generate a
        candidate*.  It does not authorize the legacy direct commit path.

        This is the first of C06b's three checkpoints; the generated result
        landing and the C06a commit guard are the other two.  All three read
        the same persistent ``WorkControl``; none can be bypassed by an
        interaction id, a session, a request id or a restart.
        """

        ns = identifiers.require_namespace(namespace)
        return self._store.review_start_decision(
            namespace=ns, work_id=work_id
        )

    def register_review_candidate(
        self,
        *,
        namespace: str,
        work_id: str,
        work_generation: int,
        **candidate_fields: Any,
    ) -> ArtifactRow:
        """Land one generated candidate only if the review is still allowed.

        The store's ``register_candidate`` already re-checks the generation and
        the registered rerun authorization.  This earlier, explicit refusal is
        the "generated result landing" checkpoint: a result produced before a
        concurrent rejection never becomes a row.
        """

        ns = identifiers.require_namespace(namespace)
        decision = self._store.review_start_decision(
            namespace=ns, work_id=work_id
        )
        if not decision.allowed:
            raise CandidateTransitionDenied(
                f"review start is blocked: {decision.reason}"
            )
        if work_generation != decision.generation:
            raise CandidateTransitionDenied(
                f"work generation moved from {work_generation} "
                f"to {decision.generation}"
            )
        candidate = self._store.register_candidate(
            namespace=ns,
            work_id=work_id,
            work_generation=work_generation,
            **candidate_fields,
        )
        # An idempotent retry may return a candidate that has already moved on;
        # only a still-pending row may be labelled candidate_pending.  Any
        # other state is repaired from the authoritative facts by get_result.
        if candidate.status == "pending":
            self._settle_interaction(
                ns, candidate.interaction_id, "candidate_pending"
            )
        return candidate

    def resolve_candidate(
        self,
        *,
        namespace: str,
        candidate_id: str,
        decision: str,
        decision_ref: str,
        authority: CommitAuthority | None = None,
    ) -> Resolution:
        """Approve or reject one candidate; the one resolution entry point.

        ``approve`` reuses :meth:`commit_candidate` and therefore the whole
        C06a transaction.  ``reject`` reuses ``store.reject_candidate`` and
        therefore the C05d transition (candidate rejected, control blocked,
        generation +1, control_version +1, rerun reference cleared).

        Both orders are safe and neither ever undoes a commit:

        * a candidate already ``committed`` is reported as
          ``already_committed`` with its stored receipt; the control is not
          touched and the generation does not advance;
        * an ``approve`` after a rejection returns ``rejected`` and writes
          nothing; a ``reject`` after a commit returns ``already_committed``.

        ``decision_ref`` is an audit label, not a token.  The authority to
        reject comes from the trusted host calling this internal method; C07
        must check the operator's permission before exposing it.  An
        ``already_committed`` receipt is a historical fact: a public Facade
        must re-apply current access and deletion-barrier checks before
        returning it.  This method adds no ACL and does not change
        :class:`CommitAuthority` or the C06a guard.
        """

        if decision not in RESOLVE_DECISIONS:
            raise InvalidInput(
                "decision must be one of " + ", ".join(RESOLVE_DECISIONS)
            )
        ns = identifiers.require_namespace(namespace)

        if decision == DECISION_APPROVE:
            if not isinstance(authority, CommitAuthority):
                raise InvalidInput("approve requires a CommitAuthority")
            candidate = self._load_candidate(ns, candidate_id)
            if candidate.status == "committed":
                resolution = self._committed_resolution(ns, candidate)
                self._settle_interaction(
                    ns,
                    candidate.interaction_id,
                    "committed",
                    resolution.receipt,
                )
                return resolution
            if candidate.status != "pending":
                # A rejected candidate can never be revived, and stale/failed
                # candidates are terminal too.  Report the state, write no
                # candidate row, and mirror it in the interaction label.
                self._settle_interaction(
                    ns, candidate.interaction_id, candidate.status
                )
                return Resolution(
                    candidate=candidate,
                    status=candidate.status,
                    receipt=None,
                    control=None,
                )
            try:
                outcome = self.commit_candidate(
                    namespace=ns,
                    candidate_id=candidate_id,
                    authority=authority,
                    decision_ref=decision_ref,
                )
            except CandidateTransitionDenied:
                # A concurrent rejection may have won between our read and the
                # commit transaction.  Re-read once and report its state.
                current = self._load_candidate(ns, candidate_id)
                if current.status == "committed":
                    resolution = self._committed_resolution(ns, current)
                    self._settle_interaction(
                        ns,
                        current.interaction_id,
                        "committed",
                        resolution.receipt,
                    )
                    return resolution
                if current.status != "pending":
                    self._settle_interaction(
                        ns, current.interaction_id, current.status
                    )
                    return Resolution(
                        candidate=current,
                        status=current.status,
                        receipt=None,
                        control=None,
                    )
                raise
            resolution = Resolution(
                candidate=outcome.candidate,
                status=(
                    "already_committed"
                    if outcome.replayed
                    else "committed"
                ),
                receipt=outcome.receipt,
                control=None,
            )
            self._settle_interaction(
                ns,
                outcome.candidate.interaction_id,
                "committed",
                outcome.receipt,
            )
            return resolution

        # decision == DECISION_REJECT
        if authority is not None:
            raise InvalidInput("reject does not accept a commit authority")
        candidate = self._load_candidate(ns, candidate_id)
        if candidate.status == "committed":
            resolution = self._committed_resolution(ns, candidate)
            self._settle_interaction(
                ns,
                candidate.interaction_id,
                "committed",
                resolution.receipt,
            )
            return resolution
        if candidate.status in ("stale", "failed"):
            raise CandidateTransitionDenied(
                f"candidate {candidate.artifact_id} is {candidate.status!r}; "
                "a terminal candidate cannot be rejected"
            )
        try:
            rejected, control = self._store.reject_candidate(
                namespace=ns,
                candidate_id=candidate_id,
                decision_ref=decision_ref,
            )
        except CandidateTransitionDenied:
            # A concurrent commit may have won.  Only "now committed" turns
            # into a resolution here; every other refusal is re-raised.
            current = self._load_candidate(ns, candidate_id)
            if current.status == "committed":
                resolution = self._committed_resolution(ns, current)
                self._settle_interaction(
                    ns,
                    current.interaction_id,
                    "committed",
                    resolution.receipt,
                )
                return resolution
            raise
        resolution = Resolution(
            candidate=rejected,
            status="rejected",
            receipt=None,
            control=control,
        )
        self._settle_interaction(
            ns, rejected.interaction_id, "rejected"
        )
        return resolution

    def get_result(
        self, *, namespace: str, interaction_id: str
    ) -> InteractionResult:
        """Recover one interaction result from the authoritative receipt.

        The stored ``processed_commands`` receipt is the only authority for
        "this command committed"; this method may repair interaction labels but
        never fabricates one, never marks a pending candidate committed, and
        never writes a Belief, command, candidate or model-attempt row.

        Recovery is idempotent: the second call returns the same result with
        ``repaired=False`` and performs no UPDATE.  Historical commits keep
        their receipts even after a grant is revoked or expired; current body
        reads remain a separate ``read_experience`` decision.  This is a
        trusted internal API and returns no body text: C07 must re-check current
        access and the deletion barrier before showing a receipt externally.

        ``result`` is the interaction lifecycle conclusion; ``candidate_status``
        is the raw storage fact.  They can differ (for example ``stale`` versus
        ``pending``) and the former never rewrites the latter.
        """

        ns = identifiers.require_namespace(namespace)
        interaction = self._store.interaction(
            namespace=ns, interaction_id=interaction_id
        )
        if interaction is None:
            raise RecordNotFound(
                f"no interaction {interaction_id} in {ns!r}"
            )
        states = self._store.candidate_states_for_interaction(
            namespace=ns, interaction_id=interaction_id
        )
        if len(states) > 1:
            raise CorruptStore(
                "ambiguous candidates for one interaction"
            )
        candidate = states[0] if states else None

        # ---- authoritative receipt resolution (label vs candidate request id)
        label = interaction.receipt_request_id
        candidate_request = (
            None if candidate is None else candidate.request_id
        )
        if (
            label is not None
            and candidate_request is not None
            and label != candidate_request
        ):
            raise CorruptStore(
                "the interaction receipt label disagrees with the candidate "
                "request id"
            )
        receipt = None
        if label is not None:
            # The interaction itself named a receipt: it must exist.
            receipt = self._store.lookup_command_receipt(
                namespace=ns, request_id=label
            )
            if receipt is None:
                raise CorruptStore(
                    f"interaction {interaction_id} names a receipt that does "
                    "not exist"
                )
        elif candidate_request is not None:
            # No label yet: a missing receipt is the normal pre-TxB state.
            receipt = self._store.lookup_command_receipt(
                namespace=ns, request_id=candidate_request
            )

        # ---- result derived from the receipt, or from the candidate state
        if receipt is not None:
            if receipt.operation == "cognition.wait":
                if candidate is not None:
                    raise CorruptStore(
                        "a wait receipt cannot belong to an interaction with a "
                        "candidate"
                    )
                self._validate_wait_receipt(ns, interaction, receipt)
                result = "waiting"
            else:
                if candidate is None:
                    raise CorruptStore(
                        "a belief receipt cannot belong to an interaction "
                        "without a candidate"
                    )
                self._validate_belief_receipt(
                    ns, interaction, candidate, receipt
                )
                if candidate.status == "pending":
                    # W3: the command committed but the candidate decision did
                    # not.  The history stays auditable; never auto-repair it.
                    raise CorruptStore(
                        "a pending candidate has a committed command receipt"
                    )
                if candidate.status != "committed":
                    raise CorruptStore(
                        f"candidate status {candidate.status!r} cannot carry a "
                        "committed belief receipt"
                    )
                result = "committed"
        else:
            if interaction.result == "committed":
                raise CorruptStore(
                    "interaction is committed but has no command receipt"
                )
            if candidate is None:
                result = interaction.result or "incomplete"
            elif candidate.status == "committed":
                raise CorruptStore(
                    "candidate is committed but has no command receipt"
                )
            elif candidate.status == "pending":
                # P4: check the barrier before the Context; a deleted candidate
                # or source means stale even while the row stays pending.
                if self._store.artifact_barred(
                    namespace=ns, artifact_id=candidate.artifact_id
                ):
                    result = "stale"
                else:
                    try:
                        require_live_context(
                            self._store,
                            context_id=candidate.context_id,
                            namespace=ns,
                        )
                    except ContextStale:
                        result = "stale"
                    else:
                        result = "candidate_pending"
            else:
                result = candidate.status

        # ---- label repair (independent transaction, after any commit)
        if (
            result in NON_TERMINAL_INTERACTION_RESULTS
            and interaction.completed_at is not None
        ):
            raise CorruptStore(
                "a non-terminal interaction result carries a completion time"
            )
        completed = (
            None
            if result in NON_TERMINAL_INTERACTION_RESULTS
            else (interaction.completed_at or utc_now_rfc3339())
        )
        need_label = (
            interaction.result != result
            or (
                receipt is not None
                and interaction.receipt_request_id != receipt.request_id
            )
            or (
                result not in NON_TERMINAL_INTERACTION_RESULTS
                and interaction.completed_at is None
            )
        )
        if need_label:
            updated = self._store.complete_interaction(
                namespace=ns,
                interaction_id=interaction_id,
                result=result,
                receipt_request_id=(
                    None if receipt is None else receipt.request_id
                ),
                completed_at=completed,
            )
            completed = updated.completed_at
            repaired = True
        else:
            repaired = False

        return InteractionResult(
            interaction_id=interaction.interaction_id,
            namespace=ns,
            mode=interaction.mode,
            result=result,
            receipt=receipt,
            candidate_id=(
                None if candidate is None else candidate.artifact_id
            ),
            candidate_status=(
                None if candidate is None else candidate.status
            ),
            repaired=repaired,
            completed_at=completed,
        )

    # ------------------------------------------------------------- internals

    def _validate_wait_receipt(
        self,
        namespace: str,
        interaction: InteractionRow,
        receipt: CommitReceipt,
    ) -> None:
        """Prove a WAIT receipt belongs to this review interaction (A.1)."""

        if interaction.mode != "review":
            raise CorruptStore(
                "a wait receipt requires a review interaction"
            )
        if interaction.work_id is None:
            raise CorruptStore(
                "a wait receipt requires a work-bound interaction"
            )
        if interaction.context_id is None:
            raise CorruptStore(
                "a wait receipt requires a context-bound interaction"
            )
        if receipt.namespace != namespace:
            raise CorruptStore(
                "the wait receipt belongs to another namespace"
            )
        if (
            interaction.receipt_request_id is None
            or receipt.request_id != interaction.receipt_request_id
        ):
            raise CorruptStore(
                "the wait receipt does not match the interaction receipt label"
            )
        if receipt.context_id != interaction.context_id:
            raise CorruptStore(
                "the wait receipt context does not match the interaction context"
            )
        if receipt.operation != "cognition.wait":
            raise CorruptStore("the receipt is not a wait command")
        if receipt.record_refs:
            raise CorruptStore(
                "a wait receipt must not append a record version"
            )
        if interaction.result not in (None, "incomplete", "waiting"):
            raise CorruptStore(
                f"interaction result {interaction.result!r} cannot accept a "
                "wait receipt"
            )

    def _validate_belief_receipt(
        self,
        namespace: str,
        interaction: InteractionRow,
        candidate: CandidateState,
        receipt: CommitReceipt,
    ) -> None:
        """Prove a Belief receipt binds this exact candidate and interaction."""

        if receipt.namespace != namespace:
            raise CorruptStore(
                "the belief receipt belongs to another namespace"
            )
        if (
            candidate.request_id is None
            or receipt.request_id != candidate.request_id
        ):
            raise CorruptStore(
                "the belief receipt request id does not match the candidate"
            )
        if receipt.context_id != candidate.context_id:
            raise CorruptStore(
                "the belief receipt context does not match the candidate"
            )
        if receipt.operation not in PROPOSAL_OPERATIONS:
            raise CorruptStore(
                "the receipt operation is not a commit-capable belief operation"
            )
        proposal_operation = self._candidate_proposal_operation(
            namespace, candidate
        )
        if proposal_operation is None:
            # The payload may have been cleared by a deletion cleanup.  Only a
            # candidate already recorded as committed may omit the equality
            # check; everything else fails closed.
            if candidate.status != "committed":
                raise CorruptStore(
                    "the candidate proposal is unavailable and the candidate "
                    "is not committed"
                )
        elif receipt.operation != proposal_operation:
            raise CorruptStore(
                "the receipt operation does not match the candidate proposal"
            )
        digest = self._store.lookup_command_request_digest(
            namespace=namespace, request_id=receipt.request_id
        )
        if (
            digest is None
            or candidate.command_digest is None
            or digest != candidate.command_digest
        ):
            raise CorruptStore(
                "the belief receipt request digest does not match the "
                "candidate command digest"
            )
        if candidate.interaction_id != interaction.interaction_id:
            raise CorruptStore(
                "the candidate belongs to another interaction"
            )
        if candidate.context_id != interaction.context_id:
            raise CorruptStore(
                "the candidate context does not match the interaction context"
            )
        if (
            interaction.work_id is not None
            and candidate.work_id != interaction.work_id
        ):
            raise CorruptStore(
                "the candidate work does not match the interaction work"
            )
        if (
            interaction.work_generation is not None
            and candidate.work_generation != interaction.work_generation
        ):
            raise CorruptStore(
                "the candidate generation does not match the interaction "
                "generation"
            )

    def _candidate_proposal_operation(
        self, namespace: str, candidate: CandidateState
    ) -> str | None:
        """Read the candidate proposal operation, or ``None`` if unavailable."""

        try:
            artifact = self._store.load_artifact(
                namespace=namespace,
                artifact_id=candidate.artifact_id,
                artifact_version=1,
            )
        except (Redacted, RecordNotFound):
            return None
        if not isinstance(artifact.proposal, dict):
            return None
        operation = artifact.proposal.get("operation")
        return operation if isinstance(operation, str) else None

    def _settle_interaction(
        self,
        namespace: str,
        interaction_id: str,
        result: str,
        receipt: CommitReceipt | None = None,
    ) -> None:
        """Write one interaction label after the authoritative write.

        This is an independent transaction, deliberately outside the C06a
        CommitGuard: a crash between the commit and this label is exactly the
        W2 window that :meth:`get_result` repairs.  Storage errors propagate.
        """

        self._store.complete_interaction(
            namespace=namespace,
            interaction_id=interaction_id,
            result=result,
            receipt_request_id=(
                None if receipt is None else receipt.request_id
            ),
            completed_at=None,
        )

    def _committed_resolution(
        self, namespace: str, candidate: ArtifactRow
    ) -> Resolution:
        receipt = self._store.lookup_command_receipt(
            namespace=namespace, request_id=candidate.request_id
        )
        if receipt is None:
            raise CorruptStore(
                "a committed candidate has no command receipt"
            )
        return Resolution(
            candidate=candidate,
            status="already_committed",
            receipt=receipt,
            control=None,
        )

    def _load_candidate(self, namespace: str, candidate_id: str) -> ArtifactRow:
        try:
            candidate = self._store.load_artifact(
                namespace=namespace,
                artifact_id=candidate_id,
                artifact_version=1,
            )
        except Redacted as exc:
            raise ContextStale(
                f"candidate {candidate_id} is inside a deletion barrier"
            ) from exc
        if candidate.kind != "candidate" or candidate.artifact_version != 1:
            raise CandidateTransitionDenied(
                f"artifact {candidate_id} is not a single-version candidate"
            )
        if (
            candidate.request_id is None
            or candidate.proposal is None
            or candidate.command_digest is None
            or candidate.interaction_id is None
        ):
            raise CorruptStore(
                f"candidate {candidate.artifact_id} is missing its request "
                "identity or proposal"
            )
        return candidate

    def _activation_kind(
        self, namespace: str, candidate: ArtifactRow
    ) -> str:
        try:
            return self._store.interaction_activation_kind(
                namespace=namespace, interaction_id=candidate.interaction_id
            )
        except RecordNotFound as exc:
            raise CorruptStore(
                f"candidate {candidate.artifact_id} names a missing interaction"
            ) from exc


class _CandidateCommitGuard:
    """The one C06a Tx B guard.  Every check runs inside the write transaction."""

    def __init__(
        self,
        *,
        store: Any,
        namespace: str,
        candidate_id: str,
        decision_ref: str,
        approval: Approval | None,
        grant_id: str | None,
        operation: str,
        proposal: Mapping[str, Any],
        trigger_kind: str,
        clock: Callable[[], str],
    ) -> None:
        self._store = store
        self._ns = namespace
        self._cid = candidate_id
        self._decision_ref = decision_ref
        self._approval = approval
        self._grant_id = grant_id
        self._operation = operation
        self._proposal = proposal
        self._trigger_kind = trigger_kind
        self._clock = clock
        self._entered = False

    @property
    def entered(self) -> bool:
        """Whether Tx B actually reached ``before_apply`` for this attempt."""

        return self._entered

    # ------------------------------------------------------- CommitGuard API

    def before_apply(self, basis: CommitBasis) -> None:
        self._entered = True
        candidate, control = self._current_candidate()
        if (
            basis.namespace != self._ns
            or basis.context_id != candidate.context_id
            or basis.request_id != candidate.request_id
        ):
            raise CorruptStore(
                "the Tx B commit basis does not name this candidate's contract"
            )
        if candidate.commit_authorization_ref is not None:
            raise CorruptStore(
                f"pending candidate {candidate.artifact_id} already carries a "
                "commit authorization reference"
            )
        if basis.operation != self._operation:
            raise CorruptStore(
                "the Tx B operation does not match the candidate proposal operation"
            )
        if canonical_json_text(basis.request_payload) != canonical_json_text(
            self._proposal
        ):
            raise CorruptStore(
                "the Tx B request payload does not match the candidate proposal"
            )
        if basis.request_digest != candidate.command_digest:
            raise CorruptStore(
                "the Tx B request digest does not match the candidate command digest"
            )

        # The trigger kind is never taken from the caller: it is re-read from
        # the persisted interaction inside this transaction, and the decision
        # below uses that value.
        persisted = self._persisted_trigger_kind(candidate)
        if persisted != self._trigger_kind:
            raise CorruptStore(
                "the interaction activation_kind changed during the commit"
            )
        now = self._trusted_now()
        grant = self._load_active_grant(now)

        decision = self._store._commit_decision(
            candidate,
            control,
            grant,
            self._approval,
            self._operation,
            persisted,
        )
        if not decision.allowed:
            raise AuthorizationRequired(f"commit denied: {decision.reason}")

        require_live_context(
            self._store,
            context_id=candidate.context_id,
            namespace=self._ns,
        )
        if self._store.artifact_barred(
            namespace=self._ns, artifact_id=candidate.artifact_id
        ):
            raise ContextStale(
                f"candidate {candidate.artifact_id} is inside a deletion barrier"
            )

    def after_receipt(
        self, basis: CommitBasis, receipt: CommitReceipt
    ) -> None:
        self._store._mark_candidate_committed_locked(
            namespace=self._ns,
            candidate_id=self._cid,
            decision_ref=self._decision_ref,
            commit_authorization_ref=self._grant_id,
        )

    # ------------------------------------------------------------- internals

    def _current_candidate(
        self,
    ) -> tuple[ArtifactRow, Any]:
        try:
            return self._store.require_current_candidate(
                namespace=self._ns, candidate_id=self._cid
            )
        except Redacted as exc:
            raise ContextStale(
                f"candidate {self._cid} is inside a deletion barrier"
            ) from exc

    def _persisted_trigger_kind(self, candidate: ArtifactRow) -> str:
        try:
            return self._store.interaction_activation_kind(
                namespace=self._ns, interaction_id=candidate.interaction_id
            )
        except RecordNotFound as exc:
            raise CorruptStore(
                f"candidate {candidate.artifact_id} names a missing interaction"
            ) from exc

    def _trusted_now(self) -> str:
        now = self._clock()
        if (
            not isinstance(now, str)
            or identifiers.TIMESTAMP_PATTERN.fullmatch(now) is None
        ):
            raise InvalidInput(
                "the trusted clock must return a UTC RFC 3339 timestamp ending in Z"
            )
        return now

    def _load_active_grant(self, now: str) -> GrantRow | None:
        if self._grant_id is None:
            return None
        grant = self._store.load_grant(
            namespace=self._ns, grant_id=self._grant_id
        )
        if (
            grant is None
            or grant.kind != "commit"
            or not self._store.grant_is_active(grant=grant, now=now)
        ):
            raise AuthorizationRequired(
                "a candidate commit requires an active commit grant"
            )
        return grant


# --------------------------------------------------------------------------
# validators
# --------------------------------------------------------------------------


def _require_decision_ref(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInput("decision_ref must be a non-empty string")
    if len(value) > _MAX_DECISION_REF:
        raise InvalidInput(
            f"decision_ref must be at most {_MAX_DECISION_REF} characters"
        )
    return value


def _validate_candidate_proposal(
    namespace: str, proposal: Mapping[str, Any]
) -> None:
    """Apply the frozen proposal validator, converting a missing key.

    ``validate_proposal`` indexes contract-required fields directly (it is
    normally reached after ``parse_proposal``).  A stored candidate could be
    structurally incomplete, and that must be an ``INVALID_INPUT`` rather than
    an uncaught ``KeyError``.
    """

    try:
        validate_proposal(namespace, proposal)
    except KeyError as exc:
        raise InvalidInput(
            f"candidate proposal is missing the required field {exc.args[0]!r}"
        ) from exc


__all__ = [
    "CandidateCommitOutcome",
    "CollaborationService",
    "DECISION_APPROVE",
    "DECISION_REJECT",
    "RESOLVE_DECISIONS",
    "Resolution",
]
