"""J06 S7b: the cognition-change service and its commit dispatch.

This module is the trusted-host entry point for a ``cognition.change`` bundle.
It never invents a second commit path: :meth:`CognitionChangeService.commit`
resolves authority and then calls
:meth:`~quintessence_pcs.collaboration.service.CollaborationService.commit_candidate`,
which dispatches back into :func:`commit_change_candidate` for the single
``commit_command`` that appends every record version, the review-decision row and
the provenance edges in one transaction.

Two rules shape the code:

* **BLOCKED is a hard block.** ``derived_rule`` and rule publication never reach
  an authority check: the decision row records ``impact='BLOCKED'`` and the call
  returns ``DEFERRED``.  No approval, grant, directive or auto-authorization can
  move it.
* **Impact and policy are advisory.** Both run outside the transaction on a
  consistent read; the commit guard re-checks the candidate, its version
  binding, the authority window and the Context inside the write transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from ..domains.cognition.beliefs import BeliefService
from ..domains.cognition.errors import EvidenceMissing
from ..domains.cognition.changes import (
    ChangeApplier,
    ValidatedChange,
    prepare_change_set,
    validate_change_bundle,
)
from ..domains.cognition.questions import (
    DuplicateQuestion,
    QuestionAccess,
    QuestionService,
)
from ..domains.refs import EventRef, RecordRef
from ..foundation import identifiers
from ..foundation.errors import (
    AuthorizationRequired,
    ContextStale,
    CorruptStore,
    InvalidInput,
    RecordNotFound,
    Redacted,
    ScopeViolation,
)
from ..foundation.jsonutil import canonical_json_text, sha256_hex
from ..store.errors import CandidateTransitionDenied
from ..store.collaboration import Approval
from ..store.governance import (
    CHANGE_OPERATION,
    IMPACT_LEVELS,
    GovernanceStore,
)
from ..store.tx_b import CommitBasis, CommitReceipt
from .policy import CommitAuthority
from .review_policy import (
    BASE_POLICY,
    PolicyDecision,
    PolicyStateView,
    RiskPolicyConfig,
    TargetFact,
    change_fingerprint,
    classify_impact,
    evidence_linkage_diagnosis,
    describe_changes,
    evaluate_policy,
    evidence_linkage,
    predict_status,
    scope_contained,
)

#: The proposal schema label stored on the candidate artifact.
CHANGE_PROPOSAL_VERSION = "cognition.change.v1"

#: The policy reference recorded on the interaction (audit only).
CHANGE_POLICY_REF = "policy:cognition.change.v1"

#: The closed permission vocabulary of the change surface.
CHANGE_PERMISSIONS = frozenset(
    {"change.propose", "change.review", "change.commit", "change.result"}
)

#: The audit purpose every call must carry.
CHANGE_PURPOSE = "cognition.change"

#: The result vocabulary (v0.5 §8.2).  It is deliberately separate from the
#: frozen ``REVIEW_STATES`` of the C07 review loop.
CHANGE_RESULT_STATES = (
    "PROPOSED",
    "AWAITING_REVIEW",
    "AWAITING_HUMAN",
    "DEFERRED",
    "REJECTED",
    "ESCALATED",
    "COMMITTED",
    "CONFLICT",
    "NOT_AUTHORIZED",
    "UNKNOWN_RESULT",
)

#: Directive kinds a binding may state.  Anything else fails at construction,
#: which is how a quotation or a hypothetical is refused before it can act.
DIRECTIVE_KINDS = frozenset(
    {"state_preference", "state_decision", "revoke_previous", "cancel_question"}
)

#: Which claim types each directive kind may authorize.
DIRECTIVE_CLAIM_TYPES = {
    "state_preference": frozenset({"user_preference"}),
    "state_decision": frozenset({"project_decision"}),
    "revoke_previous": frozenset({"user_preference", "project_decision"}),
    "cancel_question": frozenset(),
}

def _impact_rank(level: str) -> int:
    """Rank of an impact level; a BLOCKED candidate outranks every ceiling."""

    order = {level_name: index for index, level_name in enumerate(IMPACT_LEVELS)}
    return order.get(level, len(order))


_NAMESPACE_OF_DECISION = uuid.UUID("6f6f2b34-6a1d-5b8e-9a2f-1f4f2a6c9d11")

#: The eight single-operation Question commands the F1 contract froze.  They are
#: not bundles: each one is exactly one Tx B command, and ``question.create``
#: keeps its "return the existing object" de-dupe semantics.
QUESTION_OPERATIONS = (
    "question.create",
    "question.accept",
    "question.wait",
    "question.pause",
    "question.resume",
    "question.wake",
    "question.update_triggers",
    "question.reopen",
)

#: Impact of each single-operation Question command (v0.5 §7.6).
_QUESTION_IMPACT = {
    "question.create": "E0",
    "question.accept": "E1",
    "question.wait": "E1",
    "question.pause": "E1",
    "question.resume": "E1",
    "question.wake": "E1",
    "question.update_triggers": "E1",
    "question.reopen": "E2",
}


# ---------------------------------------------------------------------------
# public values
# ---------------------------------------------------------------------------


def _require_text(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{field_name} must be a non-empty string")
    if len(value) > maximum:
        raise InvalidInput(f"{field_name} must be at most {maximum} characters")
    return value


@dataclass(frozen=True)
class ChangeAccess:
    """The trusted host's capability object for one change call."""

    namespace: str
    host_instance: str
    operator: str
    access_ref: str
    permissions: frozenset[str]
    privacy_classes: frozenset[str]
    purposes: frozenset[str]

    def __post_init__(self) -> None:
        identifiers.require_namespace(self.namespace)
        _require_text(self.host_instance, "host_instance", 256)
        _require_text(self.operator, "operator", 256)
        _require_text(self.access_ref, "access_ref", 512)
        if not isinstance(self.permissions, frozenset):
            raise InvalidInput("permissions must be a frozenset")
        for permission in sorted(self.permissions):
            if permission not in CHANGE_PERMISSIONS:
                raise InvalidInput(
                    "permission must be one of " + ", ".join(sorted(CHANGE_PERMISSIONS))
                )
        if not isinstance(self.privacy_classes, frozenset) or not self.privacy_classes:
            raise InvalidInput("privacy_classes must be a non-empty frozenset")
        for privacy in sorted(self.privacy_classes):
            identifiers.require_privacy_class(privacy)
        if not isinstance(self.purposes, frozenset) or CHANGE_PURPOSE not in self.purposes:
            raise InvalidInput(f"purposes must contain {CHANGE_PURPOSE!r}")


@dataclass(frozen=True)
class DirectiveBinding:
    """A bound user statement that may authorize a change without re-approval.

    ``classifier_policy_id``/``classifier_policy_version`` are the optional
    audit-only identity of the classifier that produced the directive plan for
    the turn.  They never take part in the authorization predicates; a binding
    without them (the legacy event branch and most synthetic tests) behaves
    exactly as before, and its digest keeps the historical shape.
    """

    namespace: str
    host_instance: str
    statement_ref: Mapping[str, Any]
    statement_digest: str
    directive_kind: str
    target: Mapping[str, Any]
    allowed_claim_types: frozenset[str]
    decision_scope: Mapping[str, Any]
    bound_at: str
    binding_policy_id: str
    binding_policy_version: str
    access_ref: str
    project_authority_ref: str | None = None
    classifier_policy_id: str | None = None
    classifier_policy_version: str | None = None

    def __post_init__(self) -> None:
        identifiers.require_namespace(self.namespace)
        if self.directive_kind not in DIRECTIVE_KINDS:
            raise InvalidInput(
                "directive_kind must be one of " + ", ".join(sorted(DIRECTIVE_KINDS))
            )
        if not isinstance(self.allowed_claim_types, frozenset):
            raise InvalidInput("allowed_claim_types must be a frozenset")
        allowed = DIRECTIVE_CLAIM_TYPES[self.directive_kind]
        if not self.allowed_claim_types <= allowed:
            raise InvalidInput(
                f"directive_kind {self.directive_kind!r} cannot authorize "
                + ", ".join(sorted(set(self.allowed_claim_types) - set(allowed)))
            )
        if not isinstance(self.decision_scope, Mapping) or not self.decision_scope:
            raise InvalidInput("decision_scope must be a non-empty mapping")
        if not isinstance(self.statement_ref, Mapping):
            raise InvalidInput("statement_ref must be a reference object")
        digest = _require_text(self.statement_digest, "statement_digest", 71)
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise InvalidInput("statement_digest must be sha256:<64 lower-case hex>")
        hex_part = digest[7:]
        if any(character not in "0123456789abcdef" for character in hex_part):
            raise InvalidInput("statement_digest must be sha256:<64 lower-case hex>")
        _require_text(self.access_ref, "access_ref", 512)
        _require_text(self.binding_policy_id, "binding_policy_id", 128)
        _require_text(self.binding_policy_version, "binding_policy_version", 64)
        if (self.classifier_policy_id is None) != (
            self.classifier_policy_version is None
        ):
            raise InvalidInput(
                "classifier_policy_id and classifier_policy_version must be "
                "provided together"
            )
        if self.classifier_policy_id is not None:
            _require_text(self.classifier_policy_id, "classifier_policy_id", 128)
            _require_text(
                self.classifier_policy_version, "classifier_policy_version", 64
            )
        if self.directive_kind == "state_decision" and not self.project_authority_ref:
            raise InvalidInput("a state_decision binding requires project_authority_ref")

    @property
    def digest(self) -> str:
        payload: dict[str, Any] = {
            "namespace": self.namespace,
            "host_instance": self.host_instance,
            "statement_ref": dict(self.statement_ref),
            "statement_digest": self.statement_digest,
            "directive_kind": self.directive_kind,
            "target": dict(self.target),
            "allowed_claim_types": sorted(self.allowed_claim_types),
            "decision_scope": dict(self.decision_scope),
            "binding_policy_id": self.binding_policy_id,
            "binding_policy_version": self.binding_policy_version,
            "project_authority_ref": self.project_authority_ref,
        }
        # Only a binding that actually names a classifier extends the digest.
        # This keeps the historical digest of the unaffected callers, while a
        # turn that records its classifier cannot have that fact altered after
        # the commit without changing ``directive_digest``.
        if self.classifier_policy_id is not None:
            payload["classifier_policy_id"] = self.classifier_policy_id
            payload["classifier_policy_version"] = self.classifier_policy_version
        return "sha256:" + sha256_hex(canonical_json_text(payload))


@dataclass(frozen=True)
class ApprovalAction:
    """One human approve/reject produced by J07's trusted host."""

    approval_id: str
    candidate_id: str
    artifact_version: int
    proposal_digest: str
    decision: str
    approval_scope: Mapping[str, Any]
    statement_ref: Mapping[str, Any]
    approved_at: str
    expires_at: str
    access_ref: str

    def __post_init__(self) -> None:
        identifiers.require_record_id(self.approval_id)
        identifiers.require_record_id(self.candidate_id)
        if type(self.artifact_version) is not int or self.artifact_version < 1:
            raise InvalidInput("artifact_version must be a positive integer")
        if self.decision not in ("approve", "reject"):
            raise InvalidInput("decision must be approve or reject")
        if not isinstance(self.approval_scope, Mapping):
            raise InvalidInput("approval_scope must be a mapping")
        if not isinstance(self.statement_ref, Mapping):
            raise InvalidInput("statement_ref must be a reference object")


@dataclass(frozen=True)
class ChangeResult:
    """One change conclusion; carries no body text and no hidden authority."""

    state: str
    candidate_id: str | None = None
    artifact_version: int | None = None
    receipt: CommitReceipt | None = None
    redacted: bool = False
    reason_code: str | None = None
    impact: str | None = None
    derived_status: str | None = None
    support_basis: str | None = None
    requires_human: bool = False
    evidence_linkage: str | None = None
    policy_id: str | None = None
    policy_version: str | None = None
    risk_policy_id: str | None = None
    risk_policy_version: str | None = None
    authority_kind: str = "none"
    approval_request: Mapping[str, Any] | None = None
    record_refs: tuple[dict[str, Any], ...] = ()
    retry_after: str | None = None
    deduped: bool = False
    question_ref: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.state not in CHANGE_RESULT_STATES:
            raise InvalidInput(f"unknown change state {self.state!r}")


@dataclass(frozen=True)
class ChangeCommitOutcome:
    """What the change branch of ``commit_candidate`` returns."""

    candidate: Any
    result: ChangeResult


# ---------------------------------------------------------------------------
# helpers shared with the commit branch
# ---------------------------------------------------------------------------


def _now(clock: Callable[[], str] | None) -> str:
    return (clock or _utc_now_rfc3339)()


def _utc_now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:  # pragma: no cover - callers validate shape first
        raise InvalidInput("timestamp must be RFC 3339 UTC") from exc


def proposal_of(candidate: Any) -> dict[str, Any]:
    """The stored proposal of one change candidate, shape-checked."""

    proposal = getattr(candidate, "proposal", None)
    if not isinstance(proposal, Mapping):
        raise CorruptStore(f"candidate {candidate.artifact_id} has no proposal")
    if proposal.get("operation") != CHANGE_OPERATION:
        raise CandidateTransitionDenied(
            f"candidate {candidate.artifact_id} is not a {CHANGE_OPERATION} proposal"
        )
    return dict(proposal)


def settle_candidate(
    changes: "CognitionChangeService",
    access: ChangeAccess,
    *,
    candidate_id: str,
    review_decision_id: str,
    auto_authorization_id: str | None,
    commit_decision_id: str | None = None,
) -> ChangeResult:
    """Review one already-registered candidate and conditionally auto-commit.

    RG08 R-E uses this after ``propose_from_material``: the proposal helper
    registers the candidate, and this host step runs the same existing Review
    policy and Tx B commit as a chat turn.  It is deliberately stricter than the
    tool handler's convenience branch: a decision that already requires a human
    is never followed by an automatic commit attempt.

    ``NOT_AUTHORIZED`` is returned as the authoritative commit result (the
    candidate stays pending with a ``not_authorized`` decision); it is not
    rewritten into a success or into "no decision".
    """

    reviewed = changes.review(
        access, candidate_id=candidate_id, decision_id=review_decision_id
    )
    if reviewed.state == "COMMITTED" or reviewed.requires_human:
        return reviewed
    if reviewed.state not in ("PROPOSED", "DEFERRED"):
        return reviewed
    if auto_authorization_id is None:
        return reviewed
    return changes.commit(
        access,
        candidate_id=candidate_id,
        authority=CommitAuthority(auto_authorization_id=auto_authorization_id),
        decision_id=commit_decision_id or str(uuid.uuid4()),
    )


def plan_from_proposal(proposal: Mapping[str, Any]) -> ValidatedChange:
    """Rebuild the validated bundle from a stored proposal."""

    return validate_change_bundle(
        proposal["namespace_hint"]
        if "namespace_hint" in proposal
        else proposal.get("_namespace", ""),
        {"summary": proposal.get("summary"), "changes": proposal.get("changes")},
    )


def _context_sources(store: Any, snapshot: Any) -> list[dict[str, Any]]:
    """Every exact source the candidate's Context used.

    RG05 decision 01: a chat candidate is anchored to a consult Context, whose
    rendered collaboration inputs/artifacts live in ``collab_source_links``
    rather than in ``snapshot.sources``.  The store's source-closure rule
    requires the candidate to cover *all* of them, so both sets are merged here.
    No new source kind is introduced.
    """

    sources: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(entry: dict[str, Any]) -> None:
        key = canonical_json_text(entry)
        if key in seen:
            return
        seen.add(key)
        sources.append(entry)

    for source in snapshot.sources:
        kind = source.ref_kind
        if kind == "event":
            entry = {
                "ref_type": "event",
                "namespace": snapshot.namespace,
                "event_id": source.ref_id,
            }
        elif kind == "record":
            entry = {
                "ref_type": "record",
                "namespace": snapshot.namespace,
                "record_id": source.ref_id,
                "record_version": source.ref_version,
            }
        else:  # pragma: no cover - review Contexts only carry events and records
            continue
        add(entry)
    for ref in store.context_collab_sources(
        namespace=snapshot.namespace, context_id=snapshot.context_id
    ):
        add(dict(ref))
    return sources


def _supporting_event_types(
    store: Any, namespace: str, plan: ValidatedChange
) -> list[str]:
    """The ``event_type`` of every supporting evidence link, in bundle order."""

    types: list[str] = []
    for change in plan.changes:
        links = (
            change.get("evidence_links")
            if change["op"] == "retract_belief"
            else (change.get("body") or {}).get("evidence_links")
        )
        for link in links or ():
            if link.get("relation") not in ("supports", "qualifies"):
                continue
            ref = link.get("ref") or {}
            if ref.get("ref_type") == "collab_input":
                # RG05-F02: a current user statement is the right kind of
                # observation for a user_preference.  This is a type label for
                # linkage only; it does not create or impersonate an event.
                types.append("UserStatement:")
                continue
            if ref.get("ref_type") != "event":
                continue
            header = store.event_header(namespace, ref["event_id"])
            if header is not None:
                types.append(header.event_type)
    return types


def _target_facts(
    store: Any, namespace: str, plan: ValidatedChange
) -> dict[str, TargetFact]:
    facts: dict[str, TargetFact] = {}
    for change in plan.changes:
        target = change.get("target")
        if not isinstance(target, Mapping):
            continue
        record_id = target.get("record_id")
        if not isinstance(record_id, str):
            continue
        try:
            current = store.get_current(namespace, record_id)
        except RecordNotFound:
            continue
        body = current.body if isinstance(current.body, Mapping) else {}
        facts[record_id] = TargetFact(
            status=current.status, claim_type=str(body.get("claim_type") or "unknown")
        )
    return facts


def _predicted_status(
    store: Any, namespace: str, plan: ValidatedChange, *, now: str
) -> str:
    from ..domains.cognition.beliefs import derive_status

    facts = describe_changes(plan, now=now)
    statuses: list[str] = []
    for fact in facts:
        if fact.op in ("resolve_question", "close_question"):
            statuses.append("none")
            continue
        if fact.op == "retract_belief":
            statuses.append("retracted")
            continue
        statuses.append(
            derive_status(
                claim_type=fact.claim_type or "",
                has_support=fact.has_support,
                has_contradiction=fact.has_contradiction,
                expired=fact.expired,
                retract=False,
            )
        )
    if "contradicted" in statuses:
        return "contradicted"
    if "contested" in statuses:
        return "contested"
    if "supported" in statuses:
        return "supported"
    if "stale" in statuses:
        return "stale"
    if "retracted" in statuses:
        return "retracted"
    if statuses and all(item == "none" for item in statuses):
        return "none"
    return "proposed"


def _bundle_claim_type(plan: ValidatedChange) -> str | None:
    for change in plan.changes:
        body = change.get("body")
        if isinstance(body, Mapping) and isinstance(body.get("claim_type"), str):
            return str(body["claim_type"])
    return None


def _authorized_scope_of(binding: DirectiveBinding) -> Mapping[str, Any]:
    return binding.decision_scope


#: The frozen directive-refusal vocabulary (v0.5 §9.6).  The binding path and the
#: approval-envelope table both speak these strings, so a decision row says *why*
#: a directive was refused instead of collapsing every case into
#: ``not_authorized``.  J07 has to answer "why wasn't this remembered?" and
#: ``not_authorized`` cannot.
_DIRECTIVE_FAILURE_CODES = (
    "directive_source_not_user_report",
    "directive_digest_mismatch",
    "directive_scope_mismatch",
    "directive_claim_type_forbidden",
    "directive_kind_not_directive",
    "project_authority_missing",
    "impact_requires_human",
)

#: How one failed ``ApprovalAction`` check is recorded and reported.
#: reason -> (decision row value, ChangeResult state, requires_human)
_APPROVAL_FAILURES: Mapping[str, tuple[str, str, bool]] = {
    "approval_expired": ("approval_expired", "CONFLICT", False),
    "approval_superseded": ("conflict", "CONFLICT", False),
    "approval_scope_insufficient": ("not_authorized", "NOT_AUTHORIZED", True),
    "directive_source_not_user_report": ("not_authorized", "NOT_AUTHORIZED", True),
    "directive_digest_mismatch": ("not_authorized", "NOT_AUTHORIZED", True),
    "directive_scope_mismatch": ("not_authorized", "NOT_AUTHORIZED", True),
    "directive_claim_type_forbidden": ("not_authorized", "NOT_AUTHORIZED", True),
    "directive_kind_not_directive": ("not_authorized", "NOT_AUTHORIZED", True),
    "project_authority_missing": ("not_authorized", "NOT_AUTHORIZED", True),
    "impact_requires_human": ("awaiting_human", "AWAITING_HUMAN", True),
    "version_conflict": ("conflict", "CONFLICT", False),
    "conflict": ("conflict", "CONFLICT", False),
}


@dataclass(frozen=True)
class _AuthorityResolution:
    """Either a usable commit authority, or the frozen code that refused one.

    ``failure_code`` is set only when an authority *was presented* and refused on
    a specific ground (today: a ``DirectiveBinding`` that does not hold).  A
    plain "no authority applies" is still reported as ``None`` so the caller
    keeps its existing ``impact_requires_human`` / ``not_authorized`` fallback.
    """

    kind: str | None = None
    validator: Callable[[], str | None] | None = None
    commit_ref: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)
    failure_code: str | None = None


def _plan_targets(plan: ValidatedChange) -> tuple[Mapping[str, Any], ...]:
    """The exact targets one bundle names, for the approval audit row."""

    targets: list[Mapping[str, Any]] = []
    for change in plan.changes:
        target = change.get("target")
        if isinstance(target, Mapping):
            targets.append(dict(target))
    return tuple(targets)


def _directive_audit_fields(binding: DirectiveBinding) -> dict[str, str]:
    """The audit columns of a directive-authorized commit, without a migration.

    The decision table already has three existing JSON columns.  A
    ``user_directive`` row fills them with the binding facts that answer the
    post-hoc audit questions:

    * ``evidence_refs_json`` — the exact statement reference followed by one
      ``directive_binding_audit`` object (statement digest, binding/classifier
      policy ids and versions, directive kind, bound time, allowed claims);
    * ``target_refs_json`` — the host-built ``DirectiveBinding.target``
      (dimension/value/conversation, and the old record for a correction);
    * ``scope_json`` — the host-built ``DirectiveBinding.decision_scope``.

    The first evidence element stays a plain statement reference.  The second
    is marked ``kind='directive_binding_audit'`` and is not a loadable ref; a
    reader that only wants refs selects elements carrying ``ref_type``.  The
    binding itself is still not stored as a first-class row: a first-class
    ``directive_binding_json`` column would need a migration and is deliberately
    out of scope here.
    """

    audit = {
        "kind": "directive_binding_audit",
        "directive_kind": binding.directive_kind,
        "statement_digest": binding.statement_digest,
        "bound_at": binding.bound_at,
        "binding_policy_id": binding.binding_policy_id,
        "binding_policy_version": binding.binding_policy_version,
        "classifier_policy_id": binding.classifier_policy_id,
        "classifier_policy_version": binding.classifier_policy_version,
        "allowed_claim_types": sorted(binding.allowed_claim_types),
    }
    return {
        "evidence_refs_json": canonical_json_text(
            [dict(binding.statement_ref), audit]
        ),
        "target_refs_json": canonical_json_text([dict(binding.target)]),
        "scope_json": canonical_json_text(dict(binding.decision_scope)),
    }


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------


class CognitionChangeService:
    """Trusted-host entry point for one ``cognition.change`` bundle."""

    def __init__(
        self,
        store: Any,
        *,
        beliefs: BeliefService | None = None,
        questions: QuestionService | None = None,
        clock: Callable[[], str] | None = None,
        risk_config: RiskPolicyConfig | None = None,
    ) -> None:
        self._store = store
        self._beliefs = beliefs or BeliefService(store)
        self._questions = questions or QuestionService(store)
        self._governance = GovernanceStore(store)
        self._clock = clock or _utc_now_rfc3339
        self._risk_config = risk_config or RiskPolicyConfig()
        self._applier = ChangeApplier(store, self._beliefs, self._questions)

    # ------------------------------------------------------------- propose

    def propose(
        self,
        access: ChangeAccess,
        *,
        context_id: str,
        request_id: str,
        bundle: Mapping[str, Any],
        activation_kind: str = "user_input",
    ) -> ChangeResult:
        """Persist one pending change candidate; never commits anything."""

        _require_permission(access, "change.propose")
        artifact = self._register_candidate(
            access,
            context_id=context_id,
            request_id=request_id,
            bundle=bundle,
            activation_kind=activation_kind,
        )
        return ChangeResult(
            state="PROPOSED",
            candidate_id=artifact.artifact_id,
            artifact_version=artifact.artifact_version,
            policy_id=BASE_POLICY.policy_id,
            policy_version=BASE_POLICY.policy_version,
            risk_policy_id=self._risk_config.policy_id,
            risk_policy_version=self._risk_config.policy_version,
        )

    def _register_candidate(
        self,
        access: ChangeAccess,
        *,
        context_id: str,
        request_id: str,
        bundle: Mapping[str, Any],
        activation_kind: str = "user_input",
    ) -> Any:
        """Validate a bundle and persist it as one pending candidate.

        No permission check happens here: the public entry points check the
        permission that fits their own promise, and both of them need exactly
        this write.  Nothing is committed.
        """

        ns = access.namespace
        context_id = identifiers.require_context_id(context_id)
        request_id = identifiers.require_request_id(request_id)
        plan = validate_change_bundle(ns, bundle)
        self._prepare(access, plan)

        snapshot = self._store.load_context_snapshot(context_id)
        if snapshot.namespace != ns:
            raise ScopeViolation("the context belongs to another namespace")
        # RG05 decision 01: a chat candidate is anchored to its consult Context.
        # The frozen 0008/0014 CHECK requires every ``review`` interaction to
        # carry a trigger event, and a consult Context has none, so a chat-formed
        # candidate travels in a consult interaction envelope.  Nothing about the
        # governance path changes: the candidate, its decisions and its receipt
        # are the same rows, and the commit guard re-proves the Context sources.
        consult_basis = (
            getattr(snapshot, "context_kind", None) == "consult"
            and not snapshot.trigger_ref
        )
        interaction_id = self._store.register_interaction(
            namespace=ns,
            mode="consult" if consult_basis else "review",
            activation_kind=activation_kind,
            host_instance=access.host_instance,
            access_ref=access.access_ref,
            policy_ref=CHANGE_POLICY_REF,
            context_id=context_id,
            trigger_ref=(snapshot.trigger_ref or None),
            work_less=(not consult_basis),
        )
        proposal = {"operation": CHANGE_OPERATION, **plan.request_payload}
        return self._store.register_artifact(
            namespace=ns,
            kind="candidate",
            interaction_id=interaction_id,
            context_id=context_id,
            origin_kind="model_output",
            body=canonical_json_text(plan.request_payload),
            retention="standard",
            sources=_context_sources(self._store, snapshot),
            proposal=proposal,
            proposal_version=CHANGE_PROPOSAL_VERSION,
            work_id=None,
            work_generation=None,
            request_id=request_id,
        )

    def _propose_reopen(
        self,
        access: ChangeAccess,
        *,
        context: str,
        request: str,
        fields: Mapping[str, Any],
    ) -> ChangeResult:
        """Register the candidate behind one ``question.reopen`` request."""

        ref = dict(fields.get("ref") or {})
        if "expected_version" in fields:
            ref["record_version"] = fields["expected_version"]
        evidence = fields.get("new_evidence_refs")
        bundle = {
            "summary": "reopen a resolved question",
            "changes": [
                {
                    "op": "reopen_question",
                    "target": ref,
                    "new_evidence_refs": list(evidence or ()),
                    "reason": fields.get("reason"),
                }
            ],
        }
        artifact = self._register_candidate(
            access, context_id=context, request_id=request, bundle=bundle
        )
        candidate = self._governance.require_current_change_candidate(
            access.namespace, artifact.artifact_id
        )
        decision_id = str(
            uuid.uuid5(
                _NAMESPACE_OF_DECISION,
                f"{access.namespace}:{request}:{artifact.artifact_id}",
            )
        )
        return self._review_candidate(access, candidate, decision_id)

    # -------------------------------------------------------------- review

    def review(
        self,
        access: ChangeAccess,
        *,
        candidate_id: str,
        decision_id: str,
    ) -> ChangeResult:
        """Evaluate one pending candidate and record a non-committing decision."""

        _require_permission(access, "change.review")
        identifier = identifiers.require_record_id(candidate_id)
        self._require_uuid(decision_id, "decision_id")
        candidate = self._governance.require_current_change_candidate(
            access.namespace, identifier
        )
        return self._review_candidate(access, candidate, decision_id)

    def _review_candidate(
        self,
        access: ChangeAccess,
        candidate: Any,
        decision_id: str,
    ) -> ChangeResult:
        """The review body, without the permission check.

        ``question.reopen`` is E2 and has no candidate of its own: it registers
        one and then runs exactly this evaluation, so the awaiting-human decision
        and the approval request come from the same code as any other candidate.
        """

        ns = access.namespace
        plan = validate_change_bundle(
            ns,
            {"summary": candidate.proposal["summary"], "changes": candidate.proposal["changes"]},
        )
        now = self._clock()
        assessment = self._assess(ns, plan, now=now)

        if assessment.linkage_problem == "evidence_type_mismatch":
            # J3, review side: the same wrong association the commit path
            # refuses must not be reported as merely "worth reviewing".
            self._write_decision(
                access,
                candidate,
                decision_id=decision_id,
                decision="rejected",
                reason_code="evidence_type_mismatch",
                assessment=assessment,
                requires_human=False,
                authority_kind="none",
            )
            return assessment.to_result(
                state="REJECTED",
                candidate=candidate,
                reason_code="evidence_type_mismatch",
            )

        if assessment.impact.blocked:
            self._write_decision(
                access,
                candidate,
                decision_id=decision_id,
                decision="deferred",
                reason_code="rule_publication_not_supported",
                assessment=assessment,
                requires_human=True,
                authority_kind="none",
            )
            return assessment.to_result(
                state="DEFERRED",
                candidate=candidate,
                reason_code="rule_publication_not_supported",
            )

        policy = assessment.policy
        if policy.recommendation == "accept" and not policy.requires_human:
            return assessment.to_result(state="PROPOSED", candidate=candidate)

        decision, state = _decision_for(policy)
        self._write_decision(
            access,
            candidate,
            decision_id=decision_id,
            decision=decision,
            reason_code=policy.reason_code,
            assessment=assessment,
            requires_human=policy.requires_human,
            authority_kind="none",
            review_after=policy.review_after,
        )
        return assessment.to_result(
            state=state, candidate=candidate, reason_code=policy.reason_code
        )

    def withdraw_candidate(
        self,
        access: ChangeAccess,
        *,
        candidate_id: str,
        decision_id: str,
        reason_code: str = "superseded_by_user_correction",
    ) -> ChangeResult:
        """Host-only: terminally withdraw a pending candidate.

        RG05-F02 uses this when an explicit correction supersedes a preference
        candidate that is still waiting for a human.  The withdrawal is an
        append-only ``rejected`` decision row, so the candidate can never be
        approved later and the old value cannot keep entering Context.  It is a
        review decision, not a commit: no record version is written.
        """

        _require_permission(access, "change.review")
        identifier = identifiers.require_record_id(candidate_id)
        self._require_uuid(decision_id, "decision_id")
        lifecycle = self._governance.lifecycle(access.namespace, identifier)
        if lifecycle == "rejected":
            return ChangeResult(
                state="REJECTED",
                candidate_id=identifier,
                artifact_version=1,
                reason_code=reason_code,
            )
        if lifecycle != "pending":
            raise CandidateTransitionDenied(
                f"candidate {identifier} is {lifecycle!r}, not pending"
            )
        candidate = self._governance.require_current_change_candidate(
            access.namespace, identifier
        )
        proposal = proposal_of(candidate)
        plan = validate_change_bundle(
            access.namespace,
            {"summary": proposal.get("summary"), "changes": proposal.get("changes")},
        )
        assessment = self._assess(access.namespace, plan, now=self._clock())
        self._write_decision(
            access,
            candidate,
            decision_id=decision_id,
            decision="rejected",
            reason_code=reason_code,
            assessment=assessment,
            requires_human=False,
            authority_kind="none",
            extra_apply=lambda _scope: self._store.mark_candidate_rejected(
                namespace=access.namespace,
                candidate_id=identifier,
                decision_ref=decision_id,
                decided_at=self._clock(),
            ),
        )
        return assessment.to_result(
            state="REJECTED",
            candidate=candidate,
            reason_code=reason_code,
        )

    # -------------------------------------------------------------- commit

    def commit(
        self,
        access: ChangeAccess,
        *,
        candidate_id: str,
        authority: "CommitAuthority | ApprovalAction",
        decision_id: str,
        appended_events: Collection[str] = (),
    ) -> ChangeResult:
        """Resolve authority and submit through the one commit entry point.

        ``authority`` is either a :class:`CommitAuthority` (the four J06
        authority branches) or an :class:`ApprovalAction` — the envelope J07's
        trusted host produces from a natural-language approval.  An action is
        *verified* here and then turned into the ordinary
        ``Approval`` four-field binding, so both shapes end in the same commit
        path; the envelope itself never reaches the store.

        An ``ApprovalAction`` is checked for candidate/version/digest binding,
        ``approval_scope`` coverage, ``expires_at``, a live ``user_report``
        ``statement_ref``, the current decision state, and single use of its
        ``approval_id``.  Every failed check still records a decision row, so a
        rejected or expired approval is auditable rather than silently dropped.
        """

        _require_permission(access, "change.commit")
        identifier = identifiers.require_record_id(candidate_id)
        if isinstance(authority, ApprovalAction):
            return self._commit_with_approval_action(
                access,
                identifier,
                authority,
                decision_id,
                appended_events=appended_events,
            )

        from .service import CollaborationService

        service = CollaborationService(
            self._store, belief_service=self._beliefs, clock=self._clock
        )
        outcome = service.commit_candidate(
            namespace=access.namespace,
            candidate_id=candidate_id,
            authority=authority,
            decision_ref=decision_id,
            change_access=access,
            appended_events=appended_events,
        )
        if isinstance(outcome, ChangeCommitOutcome):
            return outcome.result
        # The candidate was already committed: ``commit_candidate`` replays the
        # stored receipt instead of running a second command.
        return ChangeResult(
            state="COMMITTED",
            candidate_id=identifier,
            artifact_version=1,
            receipt=getattr(outcome, "receipt", None),
            reason_code="committed",
        )

    def question(
        self,
        access: ChangeAccess,
        *,
        operation: str,
        context_id: str,
        request_id: str,
        auto_authorization_id: str | None = None,
        **fields: Any,
    ) -> ChangeResult:
        """Run one of the eight frozen single-operation Question commands.

        These are not bundles: each call is exactly one Tx B command whose apply
        body appends the Question version and its review-decision row together.
        ``question.create`` keeps its de-dupe semantics — a second identical
        proposal returns the existing question with ``deduped=True``, writes
        nothing and records no decision, because no change happened.

        ``question.reopen`` is E2, so it never commits here: it registers a
        ``cognition.change`` candidate carrying a ``reopen_question`` change and
        reports ``AWAITING_HUMAN`` with that candidate's id.  Nothing is written
        to the Question until an ``ApprovalAction`` approves it.
        """

        _require_permission(access, "change.commit")
        if operation not in QUESTION_OPERATIONS:
            raise InvalidInput(
                "operation must be one of " + ", ".join(QUESTION_OPERATIONS)
            )
        ns = access.namespace
        context = identifiers.require_context_id(context_id)
        request = identifiers.require_request_id(request_id)
        impact = _QUESTION_IMPACT[operation]
        now = self._clock()

        if operation == "question.reopen":
            # E2 and reversible only through a recorded human decision: register
            # a real ``cognition.change`` candidate so the ordinary approval
            # envelope can carry it, then record the awaiting-human decision.
            return self._propose_reopen(
                access, context=context, request=request, fields=fields
            )

        if impact == "E2":
            return ChangeResult(
                state="AWAITING_HUMAN",
                impact=impact,
                requires_human=True,
                reason_code="impact_requires_human",
                support_basis="none",
                evidence_linkage="not_applicable",
            )

        problem = self._question_authorization_problem(
            ns, operation, impact, auto_authorization_id, now
        )
        if problem is not None:
            return ChangeResult(
                state="NOT_AUTHORIZED",
                impact=impact,
                reason_code="not_authorized",
                support_basis="none",
                evidence_linkage="not_applicable",
            )

        snapshot = self._store.capture_snapshot(ns)
        decision_id = str(
            uuid.uuid5(_NAMESPACE_OF_DECISION, f"{ns}:{request}:{operation}")
        )

        if operation == "question.create":
            qaccess = self._question_access(
                access, permissions=frozenset({"question.read", "question.create"})
            )
            prepared = self._questions.prepare_create(qaccess, **fields)
            existing = self._questions._find_active(
                qaccess,
                prepared.scope,
                prepared.dedupe_key,
                prepared.privacy_class,
            )
            if existing is not None:
                return ChangeResult(
                    state="PROPOSED",
                    impact=impact,
                    deduped=True,
                    question_ref=existing.ref.to_dict(),
                    reason_code="deduplicated",
                    support_basis="none",
                    evidence_linkage="not_applicable",
                )
            payload = {
                "operation": operation,
                "question": prepared.question,
                "origin": prepared.origin,
                "scope": prepared.scope,
                "privacy_class": prepared.privacy_class,
                "priority": prepared.priority,
                "origin_refs": prepared.origin_refs,
                "trigger_spec": prepared.trigger_spec,
                "related_question_refs": prepared.related_question_refs,
                "review_after": prepared.review_after,
            }

            def apply_create(scope: Any) -> None:
                ref = self._questions._apply_create_locked(scope, prepared=prepared)
                self._write_question_decision(
                    scope,
                    ns,
                    operation=operation,
                    record=ref,
                    impact=impact,
                    authorization_id=auto_authorization_id,
                    request=request,
                    decision_id=decision_id,
                    payload=payload,
                    now=now,
                )

            try:
                receipt = self._store.commit_command(
                    context_id=context,
                    request_id=request,
                    operation=operation,
                    request_payload=payload,
                    apply=apply_create,
                    namespace=ns,
                )
            except DuplicateQuestion as exc:
                return ChangeResult(
                    state="PROPOSED",
                    impact=impact,
                    deduped=True,
                    question_ref=exc.ref.to_dict(),
                    reason_code="deduplicated",
                    support_basis="none",
                    evidence_linkage="not_applicable",
                )
            ref = receipt.record_refs[0]
            return ChangeResult(
                state="COMMITTED",
                impact=impact,
                receipt=receipt,
                reason_code="committed",
                support_basis="none",
                evidence_linkage="not_applicable",
                question_ref={"ref_type": "record", "namespace": ns,
                              "record_id": ref.record_id,
                              "record_version": ref.record_version},
                record_refs=(
                    {"record_id": ref.record_id, "record_version": ref.record_version},
                ),
            )

        # Every other command is a transition on an existing question version.
        action = operation.split(".", 1)[1]
        ref = RecordRef.from_value(fields.pop("ref"), field="ref")
        expected = fields.pop("expected_version")
        qaccess = self._question_access(
            access, permissions=frozenset({"question.read", "question.transition"})
        )
        prepared_transition = self._questions.prepare_transition(
            qaccess,
            ref=ref,
            expected_version=expected,
            action=action,
            **fields,
        )
        payload = {
            "operation": operation,
            "record_id": ref.record_id,
            "expected_version": expected,
            "action": action,
            **{key: value for key, value in fields.items()},
        }

        def apply_transition(scope: Any) -> None:
            self._questions._apply_transition_locked(
                scope,
                namespace=ns,
                record_id=ref.record_id,
                expected_version=expected,
                new_body=prepared_transition.new_body,
                new_status=prepared_transition.new_status,
                privacy_class=prepared_transition.privacy_class,
                scope=prepared_transition.scope,
            )
            self._write_question_decision(
                scope,
                ns,
                operation=operation,
                record=RecordRef(ns, ref.record_id, expected + 1),
                impact=impact,
                authorization_id=auto_authorization_id,
                request=request,
                decision_id=decision_id,
                payload=payload,
                now=now,
            )

        receipt = self._store.commit_command(
            context_id=context,
            request_id=request,
            operation=operation,
            request_payload=payload,
            apply=apply_transition,
            namespace=ns,
        )
        applied = receipt.record_refs[0]
        return ChangeResult(
            state="COMMITTED",
            impact=impact,
            receipt=receipt,
            reason_code="committed",
            support_basis="none",
            evidence_linkage="not_applicable",
            question_ref={
                "ref_type": "record",
                "namespace": ns,
                "record_id": applied.record_id,
                "record_version": applied.record_version,
            },
            record_refs=(
                {"record_id": applied.record_id, "record_version": applied.record_version},
            ),
        )

    def _question_authorization_problem(
        self,
        namespace: str,
        operation: str,
        impact: str,
        authorization_id: str | None,
        now: str,
    ) -> str | None:
        if authorization_id is None:
            return "no authorization presented"
        row = self._governance.load_commit_authorization(
            namespace=namespace, authorization_id=authorization_id
        )
        if row is None:
            return "unknown auto authorization"
        if not self._governance.authorization_is_active(row, now):
            return "auto authorization is not active"
        if operation not in row.operations:
            return "auto authorization does not cover this operation"
        if _impact_rank(impact) > _impact_rank(row.max_impact):
            return "impact exceeds the authorization ceiling"
        return None

    def _write_question_decision(
        self,
        tx_scope: Any,
        namespace: str,
        *,
        operation: str,
        record: RecordRef,
        impact: str,
        authorization_id: str | None,
        request: str,
        decision_id: str,
        payload: Mapping[str, Any],
        now: str,
    ) -> None:
        """Append the review decision of one single-operation command.

        The decision and the record version land in the same Tx B command, so a
        failure in either one rolls back both.  ``candidate_id`` carries the
        command's request id: these commands have no candidate artifact, and the
        column is the decision's correlation key.
        """

        digest = "sha256:" + sha256_hex(canonical_json_text(payload))
        fields = {
            "decision_id": decision_id,
            "decision_seq": self._governance.next_decision_seq(namespace),
            "candidate_id": request,
            "artifact_version": 1,
            "decision": "committed",
            "reason_code": "committed",
            "impact": impact,
            "derived_status": "none",
            "support_basis": "none",
            "requires_human": 0,
            "evidence_linkage": "not_applicable",
            "policy_id": BASE_POLICY.policy_id,
            "policy_version": BASE_POLICY.policy_version,
            "impact_policy_id": "policy:cognition.impact.v1",
            "impact_policy_version": "1",
            "authority_kind": "auto_authorization",
            "authorization_id": authorization_id,
            "change_digest": digest,
            "decision_request_id": str(
                uuid.uuid5(_NAMESPACE_OF_DECISION, f"{namespace}:{decision_id}")
            ),
            "commit_request_id": request,
            "decided_at": now,
        }
        tx_scope.record_review_decision(namespace=namespace, fields=fields)
        tx_scope.link_record_decision(
            namespace=namespace,
            record_id=record.record_id,
            record_version=record.record_version,
            decision_id=decision_id,
            decided_at=now,
        )

    def _commit_with_approval_action(
        self,
        access: ChangeAccess,
        candidate_id: str,
        action: ApprovalAction,
        decision_id: str,
        *,
        appended_events: Collection[str] = (),
    ) -> ChangeResult:
        """Verify one ``ApprovalAction`` and route it into the ordinary commit."""

        ns = access.namespace
        candidate = self._store.load_artifact(
            namespace=ns, artifact_id=candidate_id, artifact_version=1
        )
        now = self._clock()

        # 1. A repeated delivery of the same approval is a replay, not a second
        #    decision: return the stored receipt and append nothing.
        for row in self._governance.decisions_for_candidate(
            ns, candidate_id, candidate.artifact_version
        ):
            if row.approval_id == action.approval_id and row.decision == "committed":
                return ChangeResult(
                    state="COMMITTED",
                    candidate_id=candidate_id,
                    artifact_version=candidate.artifact_version,
                    receipt=self._store.lookup_command_receipt(
                        namespace=ns, request_id=candidate.request_id
                    ),
                    reason_code="committed",
                    authority_kind="manual_approval",
                )

        # 2. A terminal candidate cannot be approved again.
        lifecycle = self._governance.lifecycle(ns, candidate_id)
        if lifecycle != "pending":
            return ChangeResult(
                state="CONFLICT",
                candidate_id=candidate_id,
                artifact_version=candidate.artifact_version,
                reason_code=(
                    "candidate_already_committed"
                    if lifecycle == "committed"
                    else "candidate_rejected"
                ),
            )

        plan = validate_change_bundle(
            ns,
            {
                "summary": candidate.proposal.get("summary"),
                "changes": candidate.proposal.get("changes"),
            },
        )
        assessment = self._assess(ns, plan, now=now)

        problem = self._approval_action_problem(ns, candidate, plan, action, now)
        if problem is not None:
            decision, state, requires_human = _APPROVAL_FAILURES[problem]
            self._write_decision(
                access,
                candidate,
                decision_id=identifiers.require_record_id(decision_id),
                decision=decision,
                reason_code=problem,
                assessment=assessment,
                requires_human=requires_human,
                authority_kind="none",
                approval_id=action.approval_id,
                approval_scope=action.approval_scope,
                targets_expected=_plan_targets(plan),
                approval_expires_at=action.expires_at,
            )
            return assessment.to_result(
                state=state, candidate=candidate, reason_code=problem
            )

        if action.decision == "reject":
            self._write_decision(
                access,
                candidate,
                decision_id=identifiers.require_record_id(decision_id),
                decision="rejected",
                reason_code="human_rejected",
                assessment=assessment,
                requires_human=False,
                authority_kind="manual_approval",
                approval_id=action.approval_id,
                approval_scope=action.approval_scope,
                targets_expected=_plan_targets(plan),
                approval_expires_at=action.expires_at,
            )
            return assessment.to_result(
                state="REJECTED", candidate=candidate, reason_code="human_rejected"
            )

        approval = Approval(
            candidate_id=candidate.artifact_id,
            proposal_digest=candidate.proposal_digest,
            context_id=candidate.context_id,
            request_id=candidate.request_id,
        )
        from .service import CollaborationService

        service = CollaborationService(
            self._store, belief_service=self._beliefs, clock=self._clock
        )
        outcome = service.commit_candidate(
            namespace=ns,
            candidate_id=candidate_id,
            authority=CommitAuthority(approval=approval),
            decision_ref=decision_id,
            change_access=access,
            approval_action=action,
            appended_events=appended_events,
        )
        if isinstance(outcome, ChangeCommitOutcome):
            return outcome.result
        return ChangeResult(
            state="COMMITTED",
            candidate_id=candidate_id,
            artifact_version=candidate.artifact_version,
            receipt=getattr(outcome, "receipt", None),
            reason_code="committed",
            authority_kind="manual_approval",
        )

    def _approval_action_problem(
        self,
        namespace: str,
        candidate: Any,
        plan: ValidatedChange,
        action: ApprovalAction,
        now: str,
    ) -> str | None:
        """The first binding problem of one approval envelope, or ``None``."""

        if action.candidate_id != candidate.artifact_id:
            return "approval_superseded"
        if action.artifact_version != candidate.artifact_version:
            return "version_conflict"
        if action.proposal_digest != candidate.proposal_digest:
            return "approval_superseded"
        if _parse_timestamp(action.expires_at) <= _parse_timestamp(now):
            return "approval_expired"

        latest = self._governance.latest_decision(
            namespace, candidate.artifact_id, candidate.artifact_version
        )
        if latest is None or latest.decision not in ("awaiting_human", "escalated"):
            return "conflict"

        scope_spec = action.approval_scope
        operations = scope_spec.get("operations")
        claim_types = scope_spec.get("claim_types")
        scopes = scope_spec.get("scope")
        if not isinstance(operations, (list, tuple, frozenset, set)):
            return "approval_scope_insufficient"
        if not isinstance(claim_types, (list, tuple, frozenset, set)):
            return "approval_scope_insufficient"
        if not isinstance(scopes, Mapping):
            return "approval_scope_insufficient"
        if not {change["op"] for change in plan.changes} <= set(operations):
            return "approval_scope_insufficient"
        if not _bundle_claim_types(plan) <= set(claim_types):
            return "approval_scope_insufficient"
        for scope in _bundle_scopes(plan):
            # An approval that names no scope bound explicitly covers the
            # namespace-general case; ``scope_contained`` refuses two empty
            # mappings because "unbounded" is not provable from an empty bound.
            if not scope and not scopes:
                continue
            if not scope_contained(scope, scopes):
                return "approval_scope_insufficient"

        ref = EventRef.from_value(action.statement_ref, field="statement_ref")
        if ref.namespace != namespace:
            return "directive_scope_mismatch"
        if self._store.event_is_deleted(namespace, ref.event_id):
            return "directive_source_not_user_report"
        header = self._store.event_header(namespace, ref.event_id)
        if header is None or header.source_kind != "user_report":
            return "directive_source_not_user_report"
        return None


    def result(self, access: ChangeAccess, *, candidate_id: str) -> ChangeResult:
        """Read the authoritative result of one change candidate."""

        _require_permission(access, "change.result")
        ns = access.namespace
        identifier = identifiers.require_record_id(candidate_id)
        candidate = self._store.load_artifact(
            namespace=ns, artifact_id=identifier, artifact_version=1
        )
        lifecycle = self._governance.lifecycle(ns, identifier)
        receipt = self._store.lookup_command_receipt(
            namespace=ns, request_id=candidate.request_id
        )
        if lifecycle == "committed":
            return ChangeResult(
                state="COMMITTED",
                candidate_id=identifier,
                artifact_version=1,
                receipt=receipt,
                authority_kind="none",
            )
        if lifecycle == "rejected":
            return ChangeResult(
                state="REJECTED",
                candidate_id=identifier,
                artifact_version=1,
                reason_code="rejected",
            )
        return ChangeResult(state="PROPOSED", candidate_id=identifier, artifact_version=1)

    # ------------------------------------------------------------ internals

    def _require_uuid(self, value: Any, field_name: str) -> str:
        return identifiers.require_record_id(value)

    def _prepare(self, access: ChangeAccess, plan: ValidatedChange) -> Any:
        needs_question = any(
            change["op"] in ("resolve_question", "close_question", "reopen_question")
            for change in plan.changes
        )
        question_access = self._question_access(access) if needs_question else None
        return prepare_change_set(
            plan,
            beliefs=self._beliefs,
            questions=self._questions,
            question_access=question_access,
            store=self._store,
        )

    def _question_access(
        self,
        access: ChangeAccess,
        *,
        permissions: frozenset[str] | None = None,
    ) -> QuestionAccess:
        return QuestionAccess(
            namespace=access.namespace,
            host_instance=access.host_instance,
            access_ref=access.access_ref,
            privacy_classes=access.privacy_classes,
            permissions=permissions
            or frozenset({"question.read", "question.transition"}),
            purposes=frozenset({"cognition.question"}),
        )

    def _assess(
        self,
        namespace: str,
        plan: ValidatedChange,
        *,
        now: str,
        has_directive_binding: bool = False,
    ) -> "Assessment":
        predicted = _predicted_status(self._store, namespace, plan, now=now)
        claim_type = _bundle_claim_type(plan)
        linkage, linkage_problem = evidence_linkage_diagnosis(
            predicted_status=predicted,
            claim_type=claim_type,
            supporting_event_types=_supporting_event_types(self._store, namespace, plan),
        )
        impact = classify_impact(
            plan,
            target_facts=_target_facts(self._store, namespace, plan),
            evidence_linkage_value=linkage,
            has_directive_binding=has_directive_binding,
            now=now,
            config=self._risk_config,
        )
        fingerprint = change_fingerprint(plan)
        # Rejection guard (v0.5 §6.3): a bundle whose fingerprint was rejected
        # inside the guard window is not silently re-submitted.  It keeps an
        # honest record (the fingerprint stays in the decision row) and is pushed
        # to human review.  A BLOCKED bundle stays BLOCKED -- escalation never
        # downgrades the hard prohibition.
        prior_rejections = 0
        if self._governance.rejected_fingerprints(
            namespace,
            since=_guard_since(now, self._risk_config.rejection_guard_days),
            fingerprints=(fingerprint,),
        ):
            prior_rejections = 1
            if not impact.blocked and impact.level != "E2":
                impact = replace(
                    impact,
                    level="E2",
                    reasons=tuple(impact.reasons) + ("previously_rejected",),
                )
        state = PolicyStateView(
            predicted_status=predicted if predicted != "retracted" else "none",
            evidence_linkage=linkage,
            impact=impact,
            prior_rejections=prior_rejections,
            now=now,
        )
        policy = evaluate_policy(BASE_POLICY, plan, state)
        return Assessment(
            plan=plan,
            impact=impact,
            policy=policy,
            predicted_status=predicted if predicted != "retracted" else "none",
            linkage=linkage,
            fingerprint=fingerprint,
            linkage_problem=linkage_problem,
        )

    def _write_decision(
        self,
        access: ChangeAccess,
        candidate: Any,
        *,
        decision_id: str,
        decision: str,
        reason_code: str,
        assessment: "Assessment",
        requires_human: bool,
        authority_kind: str,
        review_after: str | None = None,
        approval_id: str | None = None,
        authorization_id: str | None = None,
        directive_digest: str | None = None,
        directive_binding: DirectiveBinding | None = None,
        approval_scope: Mapping[str, Any] | None = None,
        targets_expected: Sequence[Mapping[str, Any]] = (),
        approval_expires_at: str | None = None,
        commit_request_id: str | None = None,
        extra_apply: Callable[[Any], None] | None = None,
    ) -> Any:
        ns = access.namespace
        request_id = str(
            uuid.uuid5(_NAMESPACE_OF_DECISION, f"{ns}:{decision_id}")
        )
        snapshot = self._store.capture_snapshot(ns)
        fields = {
            "decision_id": decision_id,
            "candidate_id": candidate.artifact_id,
            "artifact_version": candidate.artifact_version,
            "interaction_id": candidate.interaction_id,
            "decision": decision,
            "reason_code": reason_code,
            "impact": assessment.impact.level,
            "derived_status": assessment.predicted_status,
            "support_basis": assessment.policy.support_basis,
            "requires_human": 1 if requires_human else 0,
            "evidence_linkage": assessment.linkage,
            "policy_id": assessment.policy.policy_id,
            "policy_version": assessment.policy.policy_version,
            "impact_policy_id": assessment.impact.policy_id,
            "impact_policy_version": assessment.impact.policy_version,
            "authority_kind": authority_kind,
            "change_digest": "sha256:" + assessment.plan_digest,
            "change_fingerprint": assessment.fingerprint,
            "decision_request_id": request_id,
            "decided_at": self._clock(),
        }
        if review_after is not None:
            fields["review_after"] = review_after
        if approval_id is not None:
            fields["approval_id"] = approval_id
        if authorization_id is not None:
            fields["authorization_id"] = authorization_id
        if directive_digest is not None:
            fields["directive_digest"] = directive_digest
        if directive_binding is not None:
            fields.update(_directive_audit_fields(directive_binding))
        if approval_scope is not None:
            fields["approval_scope_json"] = canonical_json_text(dict(approval_scope))
        if targets_expected:
            fields["targets_expected_json"] = canonical_json_text(
                [dict(item) for item in targets_expected]
            )
        if approval_expires_at is not None:
            fields["approval_expires_at"] = approval_expires_at
        if commit_request_id is not None:
            fields["commit_request_id"] = commit_request_id

        def apply(scope: Any) -> None:
            if extra_apply is not None:
                extra_apply(scope)
            scope_fields = dict(fields)
            scope_fields["decision_seq"] = self._governance.next_decision_seq(ns)
            scope.record_review_decision(namespace=ns, fields=scope_fields)

        return self._store.commit_command(
            context_id=snapshot.context_id,
            request_id=request_id,
            operation="cognition.decision",
            request_payload={"decision_id": decision_id},
            apply=apply,
            namespace=ns,
        )


    # ------------------------------------------------------- commit internals

    def _prepare_for_commit(self, access: Any, plan: ValidatedChange) -> Any:
        needs_question = any(
            change["op"] in ("resolve_question", "close_question", "reopen_question")
            for change in plan.changes
        )
        if needs_question and access is None:
            raise InvalidInput(
                "a bundle with Question changes must be committed with an access"
            )
        question_access = self._question_access(access) if needs_question else None
        return prepare_change_set(
            plan,
            beliefs=self._beliefs,
            questions=self._questions,
            question_access=question_access,
            store=self._store,
        )

    def _commit_candidate(
        self,
        candidate: Any,
        authority: CommitAuthority,
        decision_ref: str,
        *,
        access: Any = None,
        approval_action: ApprovalAction | None = None,
        appended_events: Collection[str] = (),
    ) -> ChangeCommitOutcome:
        """The change branch of ``CollaborationService.commit_candidate``."""

        if not isinstance(authority, CommitAuthority):
            raise InvalidInput("authority must be a CommitAuthority")
        ns = candidate.namespace
        identifier = candidate.artifact_id
        if self._governance.lifecycle(ns, identifier) != "pending":
            raise CandidateTransitionDenied(
                f"candidate {identifier} is not pending"
            )
        proposal = proposal_of(candidate)
        plan = validate_change_bundle(
            ns,
            {"summary": proposal.get("summary"), "changes": proposal.get("changes")},
        )
        now = self._clock()
        assessment = self._assess(
            ns,
            plan,
            now=now,
            has_directive_binding=authority.directive is not None,
        )
        decision_id = identifiers.require_record_id(decision_ref)

        if assessment.linkage_problem == "evidence_type_mismatch":
            # J3: a supporting link of the wrong kind is a wrong association, not
            # a weak one.  It is refused here, before any authority is consulted,
            # so no approval can wave it through and no silent strength downgrade
            # can make it look acceptable.
            self._write_decision(
                access,
                candidate,
                decision_id=decision_id,
                decision="rejected",
                reason_code="evidence_type_mismatch",
                assessment=assessment,
                requires_human=False,
                authority_kind="none",
            )
            return ChangeCommitOutcome(
                candidate=candidate,
                result=assessment.to_result(
                    state="REJECTED",
                    candidate=candidate,
                    reason_code="evidence_type_mismatch",
                ),
            )

        try:
            prepared = self._prepare_for_commit(access, plan)
        except EvidenceMissing as error:
            reason = getattr(error, "reason_code", None)
            if reason is None:
                raise
            self._write_decision(
                access,
                candidate,
                decision_id=decision_id,
                decision="rejected",
                reason_code=reason,
                assessment=assessment,
                requires_human=False,
                authority_kind="none",
            )
            return ChangeCommitOutcome(
                candidate=candidate,
                result=assessment.to_result(
                    state="REJECTED", candidate=candidate, reason_code=reason
                ),
            )

        if assessment.impact.blocked:
            self._write_decision(
                access,
                candidate,
                decision_id=decision_id,
                decision="deferred",
                reason_code="rule_publication_not_supported",
                assessment=assessment,
                requires_human=True,
                authority_kind="none",
            )
            return ChangeCommitOutcome(
                candidate=candidate,
                result=assessment.to_result(
                    state="DEFERRED",
                    candidate=candidate,
                    reason_code="rule_publication_not_supported",
                ),
            )

        resolution = self._resolve_authority(
            ns, candidate, plan, assessment, authority, now,
            approval_action=approval_action,
        )
        if resolution is None or resolution.failure_code is not None:
            requires_human = assessment.impact.requires_human
            # A presented-but-refused binding names the check that failed; only a
            # bare "no authority applies" keeps the impact-based label, which is
            # exactly the fallback ``not_authorized`` / ``impact_requires_human``.
            reason = (
                resolution.failure_code
                if resolution is not None and resolution.failure_code is not None
                else ("impact_requires_human" if requires_human else "not_authorized")
            )
            self._write_decision(
                access,
                candidate,
                decision_id=decision_id,
                decision="awaiting_human" if requires_human else "not_authorized",
                reason_code=reason,
                assessment=assessment,
                requires_human=requires_human,
                authority_kind="none",
            )
            return ChangeCommitOutcome(
                candidate=candidate,
                result=assessment.to_result(
                    state="AWAITING_HUMAN" if requires_human else "NOT_AUTHORIZED",
                    candidate=candidate,
                    reason_code=reason,
                ),
            )

        kind = resolution.kind
        validator = resolution.validator
        commit_ref = resolution.commit_ref
        extra = resolution.extra
        guard = _ChangeCommitGuard(
            store=self._store,
            service=self,
            candidate=candidate,
            namespace=ns,
            authority_kind=kind,
            authority_validator=validator,
            decision_ref=decision_id,
            commit_authorization_ref=commit_ref,
        )

        def apply(scope: Any) -> None:
            records = self._applier.apply(
                scope,
                plan,
                context_id=candidate.context_id,
                prepared=prepared,
            )
            fields = self._decision_fields(
                access,
                candidate,
                decision_id=decision_id,
                decision="committed",
                reason_code="committed",
                assessment=assessment,
                requires_human=False,
                authority_kind=kind,
                commit_request_id=candidate.request_id,
                **extra,
            )
            scope.record_review_decision(namespace=ns, fields=fields)
            for record in records:
                scope.link_record_decision(
                    namespace=ns,
                    record_id=record.record_id,
                    record_version=record.record_version,
                    decision_id=decision_id,
                    decided_at=fields["decided_at"],
                )

        receipt = self._store.commit_command(
            context_id=candidate.context_id,
            request_id=candidate.request_id,
            operation=CHANGE_OPERATION,
            request_payload=proposal,
            apply=apply,
            namespace=ns,
            guard=guard,
            appended_events=appended_events,
            allow_consult_context=True,
        )
        updated = self._store.load_artifact(
            namespace=ns, artifact_id=identifier, artifact_version=1
        )
        if updated.status != "committed":
            raise CorruptStore(
                f"candidate {identifier} is {updated.status!r} after its command "
                "committed; the receipt and the candidate disagree"
            )
        record_refs = tuple(
            {"record_id": ref.record_id, "record_version": ref.record_version}
            for ref in receipt.record_refs
        )
        return ChangeCommitOutcome(
            candidate=updated,
            result=assessment.to_result(
                state="COMMITTED",
                candidate=updated,
                reason_code="committed",
                authority_kind=kind,
                receipt=receipt,
                record_refs=record_refs,
            ),
        )

    # -------------------------------------------------- authority resolution

    def _resolve_authority(
        self,
        namespace: str,
        candidate: Any,
        plan: ValidatedChange,
        assessment: "Assessment",
        authority: CommitAuthority,
        now: str,
        *,
        approval_action: ApprovalAction | None = None,
    ) -> _AuthorityResolution | None:
        """Resolve exactly one authority, or ``None`` when none applies."""

        if authority.grant_id is not None:
            raise InvalidInput(
                "grant_id does not authorize a cognition.change candidate"
            )

        if authority.approval is not None:
            approval = authority.approval
            if (
                approval.candidate_id != candidate.artifact_id
                or approval.proposal_digest != candidate.proposal_digest
                or approval.context_id != candidate.context_id
                or approval.request_id != candidate.request_id
            ):
                return None
            if approval_action is None:
                return _AuthorityResolution(
                    kind="manual_approval", validator=lambda: None
                )
            # The envelope was verified before the commit started; this second
            # pass runs inside the write transaction so a concurrent change to
            # the statement, the clock or the decision history still blocks it.
            def approval_validator() -> str | None:
                return self._approval_action_problem(
                    namespace, candidate, plan, approval_action, self._clock()
                )

            problem = approval_validator()
            if problem is not None:
                return None
            return _AuthorityResolution(
                kind="manual_approval",
                validator=approval_validator,
                extra={
                    "approval_id": approval_action.approval_id,
                    "approval_scope": approval_action.approval_scope,
                    "targets_expected": _plan_targets(plan),
                    "approval_expires_at": approval_action.expires_at,
                },
            )

        if authority.auto_authorization_id is not None:
            row = self._governance.load_commit_authorization(
                namespace=namespace,
                authorization_id=authority.auto_authorization_id,
            )
            problem = self._authorization_problem(
                namespace, plan, assessment, row, now
            )
            if problem is not None:
                return None

            def validator() -> str | None:
                fresh = self._governance.load_commit_authorization(
                    namespace=namespace,
                    authorization_id=authority.auto_authorization_id,
                )
                return self._authorization_problem(
                    namespace, plan, assessment, fresh, self._clock()
                )

            return _AuthorityResolution(
                kind="auto_authorization",
                validator=validator,
                commit_ref=authority.auto_authorization_id,
                extra={"authorization_id": authority.auto_authorization_id},
            )

        if authority.directive is not None:
            binding = authority.directive
            if not isinstance(binding, DirectiveBinding):
                raise InvalidInput("directive must be a DirectiveBinding")
            problem = self._binding_problem(
                namespace,
                plan,
                assessment,
                binding,
                context_id=candidate.context_id,
            )
            if problem is not None:
                # A presented-but-refused binding keeps its own frozen code so the
                # decision row can name the check; a bare "no authority" is still
                # reported as ``None`` by the branches above.
                return _AuthorityResolution(failure_code=problem)
            # Re-run the whole binding check inside the write transaction: the
            # statement's payload digest, its liveness, its Context membership
            # and the delegated scope are all read again, so a source deleted or
            # rewritten between this resolution and the commit cannot slip
            # through.
            def directive_validator() -> str | None:
                return self._binding_problem(
                    namespace,
                    plan,
                    assessment,
                    binding,
                    context_id=candidate.context_id,
                )

            return _AuthorityResolution(
                kind="user_directive",
                validator=directive_validator,
                extra={
                    "directive_digest": binding.digest,
                    "directive_binding": binding,
                },
            )

        return None

    def _authorization_problem(
        self,
        namespace: str,
        plan: ValidatedChange,
        assessment: "Assessment",
        row: Any,
        now: str,
    ) -> str | None:
        if row is None:
            return "unknown auto authorization"
        if not self._governance.authorization_is_active(row, now):
            return "auto authorization is not active"
        if CHANGE_OPERATION not in row.operations:
            return "auto authorization does not cover cognition.change"
        claim_types = _bundle_claim_types(plan)
        if not claim_types <= set(row.claim_types):
            return "auto authorization does not cover this claim type"
        for scope in _bundle_scopes(plan):
            if not scope_contained(scope, row.scope):
                return "candidate scope is outside the authorization"
        if _impact_rank(assessment.impact.level) > _impact_rank(row.max_impact):
            return "impact exceeds the authorization ceiling"
        return None

    def _context_has_input(
        self,
        namespace: str,
        context_id: str,
        input_id: str,
        content_version: int,
    ) -> bool:
        """Whether the candidate's Context actually rendered that input version."""

        try:
            sources = self._store.context_collab_sources(
                namespace=namespace, context_id=context_id
            )
        except Exception:  # noqa: BLE001 - a missing Context grants nothing
            return False
        return any(
            source.get("ref_type") == "collab_input"
            and source.get("input_id") == input_id
            and int(source.get("content_version", 0)) == int(content_version)
            for source in sources
        )

    def _binding_problem(
        self,
        namespace: str,
        plan: ValidatedChange,
        assessment: "Assessment",
        binding: DirectiveBinding,
        *,
        context_id: str | None = None,
    ) -> str | None:
        """The first frozen refusal code for one ``DirectiveBinding``, or ``None``.

        Every branch returns a code from :data:`_DIRECTIVE_FAILURE_CODES` (or the
        ``not_authorized`` fallback), never a free-text sentence: the decision row
        is the audit record and has to name the check that failed.

        RG05-F02 adds one statement source: the current user's ``collab_input``.
        It is accepted only when the candidate's own Context names that exact
        input version as a source, so a binding cannot point at another turn's
        message, a fabricated id or an input the candidate never saw.  The
        legacy ``event`` branch is unchanged.
        """

        if binding.namespace != namespace:
            return "not_authorized"
        ref = binding.statement_ref
        if ref.get("namespace") != namespace:
            return "directive_source_not_user_report"
        if ref.get("ref_type") == "collab_input":
            if set(ref) != {"ref_type", "namespace", "input_id", "content_version"}:
                return "directive_source_not_user_report"
            try:
                row = self._store.load_input(
                    namespace=namespace,
                    input_id=str(ref["input_id"]),
                    content_version=int(ref["content_version"]),
                )
            except (Redacted, RecordNotFound):
                return "directive_source_not_user_report"
            if row.origin_kind != "user_statement":
                return "directive_source_not_user_report"
            if row.content_digest != binding.statement_digest:
                return "directive_digest_mismatch"
            if context_id is None or not self._context_has_input(
                namespace, context_id, row.input_id, row.content_version
            ):
                return "directive_source_not_user_report"
        else:
            if ref.get("ref_type") != "event":
                return "directive_source_not_user_report"
            event_id = ref.get("event_id")
            if not isinstance(event_id, str):
                return "directive_source_not_user_report"
            if self._store.event_is_deleted(namespace, event_id):
                return "directive_source_not_user_report"
            header = self._store.event_header(namespace, event_id)
            if header is None:
                return "directive_source_not_user_report"
            if header.source_kind != "user_report":
                return "directive_source_not_user_report"
            # The binding claims *this* statement, not merely "a user report in
            # this namespace".  Compare the digest it carries against the one
            # stored for the payload; a missing digest fails closed rather than
            # passing on the strength of an unchecked reference.
            stored = self._store.event_payload_digest(namespace, event_id)
            if stored is None or "sha256:" + stored != binding.statement_digest:
                return "directive_digest_mismatch"
        claim_types = _bundle_claim_types(plan)
        if not claim_types <= set(binding.allowed_claim_types):
            return "directive_claim_type_forbidden"
        for scope in _bundle_scopes(plan):
            if not scope_contained(scope, binding.decision_scope):
                return "directive_scope_mismatch"
        if _impact_rank(assessment.impact.level) > _impact_rank("E1"):
            return "impact_requires_human"
        if binding.directive_kind == "cancel_question":
            if any(change["op"] != "close_question" for change in plan.changes):
                return "directive_kind_not_directive"
        if binding.directive_kind == "state_decision":
            authority_row = self._governance.load_subject_authority(
                namespace=namespace,
                project_id=(binding.decision_scope or {}).get("project_id"),
                subject_ref=binding.project_authority_ref,
            )
            if authority_row is None or not self._governance.subject_authority_is_active(
                authority_row, self._clock()
            ):
                return "project_authority_missing"
        return None

    def _decision_fields(
        self,
        access: Any,
        candidate: Any,
        *,
        decision_id: str,
        decision: str,
        reason_code: str,
        assessment: "Assessment",
        requires_human: bool,
        authority_kind: str,
        review_after: str | None = None,
        approval_id: str | None = None,
        authorization_id: str | None = None,
        directive_digest: str | None = None,
        directive_binding: DirectiveBinding | None = None,
        approval_scope: Mapping[str, Any] | None = None,
        targets_expected: Sequence[Mapping[str, Any]] = (),
        approval_expires_at: str | None = None,
        commit_request_id: str | None = None,
    ) -> dict[str, Any]:
        ns = candidate.namespace
        fields: dict[str, Any] = {
            "decision_id": decision_id,
            "candidate_id": candidate.artifact_id,
            "artifact_version": candidate.artifact_version,
            "interaction_id": candidate.interaction_id,
            "decision": decision,
            "reason_code": reason_code,
            "impact": assessment.impact.level,
            "derived_status": assessment.predicted_status,
            "support_basis": assessment.policy.support_basis,
            "requires_human": 1 if requires_human else 0,
            "evidence_linkage": assessment.linkage,
            "policy_id": assessment.policy.policy_id,
            "policy_version": assessment.policy.policy_version,
            "impact_policy_id": assessment.impact.policy_id,
            "impact_policy_version": assessment.impact.policy_version,
            "authority_kind": authority_kind,
            "change_digest": "sha256:" + assessment.plan_digest,
            "decision_request_id": str(
                uuid.uuid5(_NAMESPACE_OF_DECISION, f"{ns}:{decision_id}")
            ),
            "decided_at": self._clock(),
        }
        fingerprint = assessment.fingerprint
        if fingerprint:
            fields["change_fingerprint"] = fingerprint
        if review_after is not None:
            fields["review_after"] = review_after
        if approval_id is not None:
            fields["approval_id"] = approval_id
        if authorization_id is not None:
            fields["authorization_id"] = authorization_id
        if directive_digest is not None:
            fields["directive_digest"] = directive_digest
        if directive_binding is not None:
            fields.update(_directive_audit_fields(directive_binding))
        if approval_scope is not None:
            fields["approval_scope_json"] = canonical_json_text(dict(approval_scope))
        if targets_expected:
            fields["targets_expected_json"] = canonical_json_text(
                [dict(item) for item in targets_expected]
            )
        if approval_expires_at is not None:
            fields["approval_expires_at"] = approval_expires_at
        if commit_request_id is not None:
            fields["commit_request_id"] = commit_request_id
        fields["decision_seq"] = self._governance.next_decision_seq(ns)
        return fields


# ---------------------------------------------------------------------------
# assessment bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Assessment:
    """Everything the review and commit paths need about one bundle."""

    plan: ValidatedChange
    impact: Any
    policy: PolicyDecision
    predicted_status: str
    linkage: str
    fingerprint: str
    #: ``evidence_type_mismatch`` when supporting links exist but are the wrong
    #: kind of observation for the claim; ``None`` for "no evidence" and for
    #: "nothing to check".  The frozen three-value column cannot carry this, so
    #: it travels here and is recorded as a ``reason_code`` on rejection.
    linkage_problem: str | None = None

    @property
    def plan_digest(self) -> str:
        return sha256_hex(canonical_json_text(self.plan.request_payload))

    def to_result(
        self,
        *,
        state: str,
        candidate: Any,
        reason_code: str | None = None,
        authority_kind: str = "none",
        receipt: CommitReceipt | None = None,
        record_refs: Sequence[Mapping[str, Any]] = (),
    ) -> ChangeResult:
        approval_request = None
        if state == "AWAITING_HUMAN":
            approval_request = {
                "candidate_id": candidate.artifact_id,
                "artifact_version": candidate.artifact_version,
                "proposal_digest": candidate.proposal_digest,
                "context_id": candidate.context_id,
                "impact": self.impact.level,
                "targets": [dict(item) for item in _plan_targets(self.plan)],
                "changes": [
                    change["op"] for change in self.plan.changes
                ],
            }
        return ChangeResult(
            state=state,
            candidate_id=candidate.artifact_id,
            artifact_version=candidate.artifact_version,
            receipt=receipt,
            reason_code=reason_code or self.policy.reason_code,
            impact=self.impact.level,
            derived_status=self.predicted_status,
            support_basis=self.policy.support_basis,
            requires_human=self.policy.requires_human,
            evidence_linkage=self.linkage,
            policy_id=self.policy.policy_id,
            policy_version=self.policy.policy_version,
            risk_policy_id=self.impact.policy_id,
            risk_policy_version=self.impact.policy_version,
            approval_request=approval_request,
            authority_kind=authority_kind,
            record_refs=tuple(dict(item) for item in record_refs),
        )


def _guard_since(now: str, days: int) -> str:
    """The inclusive lower bound of the rejection guard window."""

    moment = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (moment - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decision_for(policy: PolicyDecision) -> tuple[str, str]:
    if policy.recommendation == "reject":
        return "rejected", "REJECTED"
    if policy.recommendation == "defer":
        return "deferred", "DEFERRED"
    if policy.recommendation == "escalate":
        return "escalated", "ESCALATED"
    if policy.requires_human:
        return "awaiting_human", "AWAITING_HUMAN"
    return "deferred", "DEFERRED"  # pragma: no cover - accept-without-human is returned earlier


def _require_permission(access: ChangeAccess, permission: str) -> None:
    if permission not in access.permissions:
        raise AuthorizationRequired(f"{permission} is required")


# ---------------------------------------------------------------------------
# the commit branch (called by CollaborationService.commit_candidate)
# ---------------------------------------------------------------------------


class _ChangeCommitGuard:
    """The in-transaction guard of one change commit.

    Everything it proves runs while the write transaction is open: the commit
    basis really is this candidate's command, the candidate is still pending, the
    Context is still live, and the authority presented outside is still valid at
    commit time.
    """

    def __init__(
        self,
        *,
        store: Any,
        service: "CognitionChangeService",
        candidate: Any,
        namespace: str,
        authority_kind: str,
        authority_validator: Callable[[], str | None],
        decision_ref: str,
        commit_authorization_ref: str | None,
    ) -> None:
        self._store = store
        self._service = service
        self._candidate = candidate
        self._namespace = namespace
        self._authority_kind = authority_kind
        self._authority_validator = authority_validator
        self._decision_ref = decision_ref
        self._commit_authorization_ref = commit_authorization_ref
        self._entered = False

    @property
    def entered(self) -> bool:
        return self._entered

    def before_apply(self, basis: CommitBasis) -> None:
        self._entered = True
        candidate = self._candidate
        if (
            basis.namespace != self._namespace
            or basis.context_id != candidate.context_id
            or basis.request_id != candidate.request_id
        ):
            raise CorruptStore(
                "the Tx B commit basis does not name this candidate's contract"
            )
        if basis.operation != CHANGE_OPERATION:
            raise CorruptStore(
                "the Tx B operation does not match the change proposal operation"
            )
        if canonical_json_text(basis.request_payload) != canonical_json_text(
            candidate.proposal
        ):
            raise CorruptStore(
                "the Tx B request payload does not match the stored proposal"
            )
        if basis.request_digest != candidate.command_digest:
            raise CorruptStore(
                "the Tx B request digest does not match the candidate command digest"
            )
        if self._service._governance.lifecycle(
            self._namespace, candidate.artifact_id
        ) != "pending":
            raise CandidateTransitionDenied(
                f"candidate {candidate.artifact_id} is not pending"
            )
        problem = self._authority_validator()
        if problem is not None:
            raise AuthorizationRequired(f"commit denied: {problem}")
        from ..domains.cognition.context_guard import require_live_context

        require_live_context(
            self._store,
            context_id=candidate.context_id,
            namespace=self._namespace,
        )

    def after_receipt(self, basis: CommitBasis, receipt: CommitReceipt) -> None:
        self._store._mark_workless_candidate_committed_locked(
            namespace=self._namespace,
            candidate_id=self._candidate.artifact_id,
            decision_ref=self._decision_ref,
            commit_authorization_ref=self._commit_authorization_ref,
        )


def _bundle_claim_types(plan: ValidatedChange) -> set[str]:
    types: set[str] = set()
    for change in plan.changes:
        body = change.get("body")
        if isinstance(body, Mapping) and isinstance(body.get("claim_type"), str):
            types.add(str(body["claim_type"]))
    return types


def _bundle_scopes(plan: ValidatedChange) -> list[Mapping[str, Any]]:
    scopes: list[Mapping[str, Any]] = []
    for change in plan.changes:
        scope = change.get("scope")
        if isinstance(scope, Mapping):
            scopes.append(scope)
    return scopes


def commit_change(
    store: Any,
    beliefs: BeliefService,
    clock: Callable[[], str],
    *,
    candidate: Any,
    authority: CommitAuthority,
    decision_ref: str,
    access: Any = None,
    approval_action: ApprovalAction | None = None,
    appended_events: Collection[str] = (),
) -> ChangeCommitOutcome:
    """Run the one ``commit_command`` for a change candidate."""

    service = CognitionChangeService(store, beliefs=beliefs, clock=clock)
    return service._commit_candidate(
        candidate,
        authority,
        decision_ref,
        access=access,
        approval_action=approval_action,
        appended_events=appended_events,
    )
