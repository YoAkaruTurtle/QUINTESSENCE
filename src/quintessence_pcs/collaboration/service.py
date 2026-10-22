"""C06a/C06b: the trusted candidate commit and review-resolution service.

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
    Approval,
    ArtifactRow,
    Decision,
    GrantRow,
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
        return self._store.register_candidate(
            namespace=ns,
            work_id=work_id,
            work_generation=work_generation,
            **candidate_fields,
        )

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
                return self._committed_resolution(ns, candidate)
            if candidate.status != "pending":
                # A rejected candidate can never be revived, and stale/failed
                # candidates are terminal too.  Report the state, write nothing.
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
                    return self._committed_resolution(ns, current)
                if current.status != "pending":
                    return Resolution(
                        candidate=current,
                        status=current.status,
                        receipt=None,
                        control=None,
                    )
                raise
            return Resolution(
                candidate=outcome.candidate,
                status=(
                    "already_committed"
                    if outcome.replayed
                    else "committed"
                ),
                receipt=outcome.receipt,
                control=None,
            )

        # decision == DECISION_REJECT
        if authority is not None:
            raise InvalidInput("reject does not accept a commit authority")
        candidate = self._load_candidate(ns, candidate_id)
        if candidate.status == "committed":
            return self._committed_resolution(ns, candidate)
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
                return self._committed_resolution(ns, current)
            raise
        return Resolution(
            candidate=rejected,
            status="rejected",
            receipt=None,
            control=control,
        )

    # ------------------------------------------------------------- internals

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
