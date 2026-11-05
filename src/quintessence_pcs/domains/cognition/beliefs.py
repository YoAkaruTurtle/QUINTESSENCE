"""A group: Belief proposal, evidence rules and revision (cognition guide §2/§3).

A Belief is a unified Record with ``kind='belief'``.  Two entry points exist,
both through Tx B (``store.commit_command``):

``propose_belief``
    validate the body, resolve every evidence reference against the store,
    derive ``status`` and the effective ``strength``, append version 1.

``revise_belief``
    append the next version of one **current** belief.  ``claim`` text and
    ``claim_type`` are frozen in PCS2-008: changing either is
    ``INVALID_TRANSITION``.  ``retracted`` is terminal and ``contradicted``
    needs an explicit reopen (not implemented here), so revising either is
    rejected.  A revision with no observable change is a legal no-op.

Evidence and support rules are deliberately conservative:

* every ``evidence_links[].ref`` must be an existing EventRef or the exact
  RecordRef of an Episode; missing -> ``EVIDENCE_MISSING``, deleted ->
  ``SOURCE_REDACTED``, another record kind or a model/synthetic source ->
  rejected (``EVIDENCE_MISSING`` / ``SOURCE_UNTRUSTED``);
* ``origin_group_id`` is **validated against the store's own derivation**, not
  trusted from the model.  PCS2-008's minimal resolution is "one Event = one
  origin group"; a single-source Episode belongs to its source Event's group,
  and a multi-source Episode is conservatively one unverifiable group;
* the number of independent supporting sources caps strength.  Sources only
  count as N when ``(source_instance, source_event_id)`` differ, no
  ``causation_refs`` edge connects them and none is ``imported_memory``.  Any
  uncertainty collapses the count to 1.  Semantic same-origin detection is
  deliberately left to PCS2-011/012, but it may not inflate the count here;
* ``strength`` is an ordinal, never a probability.  It is capped by the
  independent count, not by what the model asked for.  The stored body carries
  the effective value;
* scope is written to the Record envelope (``record_versions.scope_json``),
  never only into ``strength_reason``.  Narrowing is a new version with a new
  scope.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from ...foundation import identifiers
from ...foundation.errors import (
    CorruptStore,
    InvalidInput,
    RecordKindMismatch,
    RecordNotFound,
    Redacted,
    ScopeViolation,
    SourceRedacted,
    StateRevisionConflict,
)
from ...foundation.jsonutil import canonical_json_text, sha256_hex
from ...store.cognition import CognitionStore
from ...store.records import RecordVersion
from ..aliases import canonicalize_entity_ids
from ..errors import SourceUntrusted
from ..memory.episode_shape import parse_episode_body
from ..refs import EventRef, RecordRef
from ..scope import normalize_scope
from .context_guard import require_live_context
from .errors import EvidenceMissing, InvalidTransition, UnsupportedOperation

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from ...store.tx_b import CommitGuard

BELIEF_KIND = "belief"
BELIEF_SCHEMA_VERSION = "1"

#: PCS2-010-P0: a question is not a factual statement.  The classification is
#: made from the stored envelope (``event_journal.event_type``), never from a
#: payload field the event itself claims, so a payload cannot promote itself to
#: evidence or demote itself out of the question class.
QUESTION_EVENT_SUFFIX = ".question"

CLAIM_TYPES = (
    "user_preference",
    "environment_state",
    "capability",
    "general_hypothesis",
    "derived_rule",
    # J06 policy correction (J1): a statement *about a source*, not about the
    # world.  "Page P says X" can be mechanically checked; "X is true" cannot be
    # derived from it.  Keeping the two separate is what lets a web-informed
    # tentative claim be persisted honestly instead of being pushed to E2.
    "external_report",
)

STRENGTHS = ("insufficient", "limited", "moderate", "strong")
STRENGTH_RANK = {name: index for index, name in enumerate(STRENGTHS)}

RELATIONS = ("supports", "contradicts", "qualifies")
SUPPORTING_RELATIONS = ("supports", "qualifies")

INFERENCE_BASES = ("direct_report", "observation", "inductive", "deductive")
INFERENCE_BASIS_BY_CLAIM_TYPE = {
    "user_preference": "direct_report",
    "environment_state": "observation",
    "capability": "observation",
    "general_hypothesis": "inductive",
    "derived_rule": "deductive",
    "external_report": "direct_report",
}

#: A source kind that may appear as evidence at all.  A model inference and a
#: synthetic fixture are not observations of the world; they can be triggers
#: and history, but they cannot support or contradict a belief in this card.
TRUSTED_EVIDENCE_SOURCE_KINDS = (
    "host_observation",
    "user_report",
    "system_internal",
    "imported_memory",
)

#: Support-rule source kinds per claim type (the card's table).
SUPPORT_SOURCE_KINDS = {
    "user_preference": frozenset({"user_report"}),
    "environment_state": frozenset({"host_observation"}),
    "capability": frozenset({"host_observation"}),
    "general_hypothesis": frozenset(TRUSTED_EVIDENCE_SOURCE_KINDS),
    "derived_rule": frozenset(TRUSTED_EVIDENCE_SOURCE_KINDS),
    # A fetch is performed and recorded by the host, so host_observation is the
    # honest source kind: it records "the host received this response", never
    # "the page's claim is true".
    "external_report": frozenset({"host_observation"}),
}

#: Claims that must state their applicability range in the Record envelope
#: whenever they are supported.  A global ``user_preference`` would generalize
#: beyond the range the person declared; a global capability/environment claim
#: would generalize beyond the tested/observed range.
SCOPE_REQUIRED_CLAIM_TYPES = frozenset(
    {"user_preference", "environment_state", "capability", "external_report"}
)

#: The JSON shape of one belief body, expressed for tool schemas.  Kept next to
#: the validator on purpose: a field added here without a schema update, or the
#: other way round, shows up in the catalog's consistency test instead of in a
#: model that cannot form a legal body.
#: One line per claim type, published in the tool schema.  Without it the model
#: has to guess whether "the deployment window is Tuesday" is an observation of
#: the world or a user preference -- and that guess decides whether the change is
#: graded E0 or E2.
CLAIM_TYPE_DESCRIPTIONS: Mapping[str, str] = MappingProxyType(
    {
        "capability": "a judgement about what the system can do; needs observation",
        "environment_state": "a judgement about the outside world or a project; needs observation",
        "external_report": "a source says X; needs a quote from a fetched page",
        "user_preference": (
            "how the user wants things to be; an explicit lasting directive needs "
            "a host binding, while a low-impact ordinary expression is recorded as "
            "a tentative conversation preference, not a confirmed belief"
        ),
        # ``project_decision`` is deliberately absent: it is not a Belief claim
        # type in this card.  The model-visible descriptions must match the
        # enum, not suggest a type the schema will reject.
        "general_hypothesis": "a guess or hypothesis; can stay tentative",
        "derived_rule": "a rule derived from premises; publication stays blocked",
    }
)

BELIEF_BODY_FIELDS: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "claim": {"type": "string", "minLength": 1},
        "claim_type": {"type": "string", "enum": list(CLAIM_TYPES)},
        "evidence_links": {"type": "array"},
        "strength": {
            "type": "string",
            "enum": list(STRENGTHS),
            "description": (
                "how strongly the evidence backs the claim; the number of "
                "independent support groups caps it: 0 groups -> insufficient, "
                "1 -> limited, 2 -> moderate, 3+ -> strong. Claiming more than "
                "the evidence supports is graded E2 (strength_overstated)."
            ),
        },
        "strength_reason": {"type": "string", "minLength": 1},
        "inference_basis": {"type": "string", "enum": list(INFERENCE_BASES)},
        "premise_refs": {"type": "array"},
        "alternative_group_id": {"type": ["string", "null"]},
        "valid_until": {"type": ["string", "null"]},
        "review_condition": {"type": "string", "minLength": 1},
    }
)

#: Evidence link shape: the four frozen keys plus the optional quote the J06
#: correction added for external reports.
EVIDENCE_LINK_FIELDS: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "ref": {"type": "object"},
        "relation": {"type": "string", "enum": list(RELATIONS)},
        "origin_group_id": {"type": "string", "minLength": 1},
        "applicability_reason": {"type": "string", "minLength": 1},
        "quote": {
            "type": "string",
            "maxLength": 512,
            "description": (
                "an exact fragment of the *fetched page text* (not the search "
                "result summary); it is checked as a substring of the stored "
                "body and a mismatch refuses the change"
            ),
        },
    }
)

BELIEF_BODY_KEYS = frozenset(
    {
        "claim",
        "claim_type",
        "evidence_links",
        "strength",
        "strength_reason",
        "inference_basis",
        "premise_refs",
        "alternative_group_id",
        "valid_until",
        "review_condition",
    }
)

MAX_CLAIM_LENGTH = 8192
MAX_STRENGTH_REASON_LENGTH = 4096
MAX_APPLICABILITY_REASON_LENGTH = 1024
MAX_ORIGIN_GROUP_LENGTH = 256
MAX_ALTERNATIVE_GROUP_LENGTH = 256
MAX_REVIEW_CONDITION_LENGTH = 2048
MAX_EVIDENCE_LINKS = 64
MAX_PREMISE_REFS = 32

_EVIDENCE_KEYS = frozenset(
    {"ref", "relation", "origin_group_id", "applicability_reason"}
)
#: Optional keys an evidence link may carry.  ``quote`` is the verbatim fragment
#: an ``external_report`` claims to have read; it is optional at the schema level
#: so existing claim types keep their exact link shape, and required by the
#: support rules of ``external_report``.
_OPTIONAL_EVIDENCE_KEYS = frozenset({"quote"})
MAX_QUOTE_LENGTH = 512
_EVENT_REF_KEYS = frozenset({"ref_type", "namespace", "event_id"})
_RECORD_REF_KEYS = frozenset(
    {"ref_type", "namespace", "record_id", "record_version"}
)


@dataclass(frozen=True)
class _ResolvedEvidence:
    """One evidence link resolved to store facts (no payload text copied)."""

    link: dict[str, Any]
    ref_type: str
    origin_group_id: str
    source_kinds: tuple[str, ...]
    source_instances: tuple[str, ...]
    source_event_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    multi_source: bool
    imported: bool

    @property
    def relation(self) -> str:
        return self.link["relation"]


@dataclass(frozen=True)
class BeliefSubmission:
    """Result of one Tx B belief submission (version, receipt, append flag)."""

    version: RecordVersion
    receipt: Any
    appended: bool


def derive_status(
    *,
    claim_type: str,
    has_support: bool,
    has_contradiction: bool,
    expired: bool,
    retract: bool,
) -> str:
    """The frozen status branch of :meth:`BeliefService._derive`.

    Pure by design: it takes only facts that have already been resolved, so the
    review policy can predict the status a proposal would derive without opening
    a transaction or reaching the store.  This must stay the single definition --
    a second copy inside the policy layer would drift from the commit path.
    """

    if retract:
        return "retracted"
    if has_support and has_contradiction:
        return "contested"
    if has_contradiction and not has_support:
        return "contradicted"
    if not has_support:
        return "proposed"
    if claim_type in ("general_hypothesis", "derived_rule"):
        # The card forbids automatic support for induction and requires a
        # designated validator for deductive rules; PCS2-008 has neither,
        # so the honest status is proposed.
        return "proposed"
    if expired:
        return "stale"
    return "supported"


class BeliefService:
    """Belief proposals and revisions for one PCS cognition store."""

    def __init__(
        self,
        store: CognitionStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock if clock is not None else _utc_now

    # --------------------------------------------------------------- public

    def propose_belief(
        self,
        *,
        namespace: str,
        context_id: str,
        request_id: str,
        body: Mapping[str, Any],
        scope: Mapping[str, Any],
    ) -> RecordVersion:
        """Append one new Belief through Tx B; never changes an existing one."""

        return self._propose(
            namespace=namespace,
            context_id=context_id,
            request_id=request_id,
            body=body,
            scope=scope,
            work_id=None,
        ).version

    def revise_belief(
        self,
        *,
        namespace: str,
        context_id: str,
        request_id: str,
        target: Mapping[str, Any],
        body: Mapping[str, Any],
        scope: Mapping[str, Any],
        retract: bool = False,
    ) -> RecordVersion:
        """Append the next version of one current Belief through Tx B.

        ``retract=True`` is the explicit PCS2-008 retraction switch.  It is a
        method argument rather than a body field because the Belief body's field
        names are frozen; the body still has to carry the evidence that
        justifies the retraction.
        """

        return self._revise(
            namespace=namespace,
            context_id=context_id,
            request_id=request_id,
            target=target,
            body=body,
            scope=scope,
            retract=retract,
            work_id=None,
        ).version

    def submit_proposal(
        self,
        *,
        namespace: str,
        context_id: str,
        request_id: str,
        proposal: Mapping[str, Any],
        work_id: str | None,
        commit_basis: tuple[str, Mapping[str, Any]] | None = None,
        guard: "CommitGuard | None" = None,
    ) -> BeliefSubmission:
        """Runtime entry: commit a parsed proposal and optional work progress.

        The structural proposal has already been parsed by the runtime; this
        method re-validates the body and runs the store-dependent checks inside
        the same Tx B transaction that appends the version and, when
        ``work_id`` is not ``None``, writes ``work_progress``.

        ``commit_basis`` and ``guard`` are the C06a candidate path.  When both
        are supplied, the Tx B command identity (operation and request payload)
        is the candidate's original proposal, not the internal domain payload;
        ``guard`` runs inside that same transaction.  With ``commit_basis=None``
        the legacy runtime identity is unchanged byte for byte.
        """

        operation = proposal.get("operation")
        if operation == "propose_belief":
            return self._propose(
                namespace=namespace,
                context_id=context_id,
                request_id=request_id,
                body=proposal["body"],
                scope=proposal["scope"],
                work_id=work_id,
                commit_basis=commit_basis,
                guard=guard,
            )
        if operation == "revise_belief":
            return self._revise(
                namespace=namespace,
                context_id=context_id,
                request_id=request_id,
                target=proposal["target"],
                body=proposal["body"],
                scope=proposal["scope"],
                retract=bool(proposal.get("retract", False)),
                work_id=work_id,
                commit_basis=commit_basis,
                guard=guard,
            )
        if operation == "retract_belief":
            target_ref = RecordRef.from_value(
                proposal["target"], field="target"
            )
            if target_ref.namespace != namespace:
                raise ScopeViolation(
                    f"target is in namespace {target_ref.namespace!r}, "
                    f"not {namespace!r}"
                )
            current = self._store.get_current(
                namespace, target_ref.record_id
            )
            if current.record_version != target_ref.record_version:
                raise StateRevisionConflict(
                    f"belief {target_ref.record_id} moved from version "
                    f"{target_ref.record_version} to {current.record_version}; "
                    "retract the current version"
                )
            old_body = parse_belief_body(current)
            retraction_body = dict(old_body)
            retraction_body["evidence_links"] = list(
                proposal["evidence_links"]
            )
            retraction_body["strength_reason"] = proposal["reason"]
            return self._revise(
                namespace=namespace,
                context_id=context_id,
                request_id=request_id,
                target=proposal["target"],
                body=retraction_body,
                scope=current.scope or {},
                retract=True,
                work_id=work_id,
                commit_basis=commit_basis,
                guard=guard,
            )
        raise UnsupportedOperation(
            f"PCS2-008 cannot submit {operation!r}; allowed: propose_belief, "
            "revise_belief, retract_belief, wait"
        )

    # ------------------------------------------------------------- internals

    def _propose(
        self,
        *,
        namespace: str,
        context_id: str,
        request_id: str,
        body: Mapping[str, Any],
        scope: Mapping[str, Any],
        work_id: str | None,
        commit_basis: tuple[str, Mapping[str, Any]] | None = None,
        guard: "CommitGuard | None" = None,
    ) -> BeliefSubmission:
        ns = identifiers.require_namespace(namespace)
        cid = identifiers.require_context_id(context_id)
        rid = identifiers.require_request_id(request_id)
        normalized_scope = canonicalize_entity_ids(
            self._store, ns, normalize_scope(scope)
        )
        validated = validate_belief_body(body, scope=normalized_scope)
        record_id = str(uuid.uuid4())
        result: list[RecordVersion] = []

        def apply(tx: Any) -> None:
            self._apply_propose_locked(
                tx,
                namespace=ns,
                context_id=cid,
                record_id=record_id,
                validated=validated,
                normalized_scope=normalized_scope,
                work_id=work_id,
                result=result,
            )

        if commit_basis is None:
            operation = "belief.propose"
            payload: Mapping[str, Any] = {
                "belief": validated,
                "scope": normalized_scope,
                "work_id": work_id,
            }
        else:
            # C06a: the candidate proposal *is* the Tx B command identity.
            operation, payload = commit_basis
        receipt = self._store.commit_command(
            context_id=cid,
            request_id=rid,
            operation=operation,
            request_payload=payload,
            apply=apply,
            namespace=ns,
            guard=guard,
        )
        if result:
            return BeliefSubmission(
                version=result[0], receipt=receipt, appended=True
            )
        return BeliefSubmission(
            version=_version_from_receipt(self._store, receipt),
            receipt=receipt,
            appended=bool(receipt.record_refs),
        )

    def _apply_propose_locked(
        self,
        tx: Any,
        *,
        namespace: str,
        context_id: str,
        record_id: str,
        validated: Mapping[str, Any],
        normalized_scope: Mapping[str, Any],
        work_id: str | None,
        result: list[RecordVersion],
    ) -> None:
        """Append one new Belief inside an already open TxScope.

        This is the transaction body of :meth:`_propose`, moved here verbatim so
        one combination commit can validate and append several objects inside a
        single ``commit_command``.  It performs exactly one append and calls
        ``complete_work`` last when a work item is bound.
        """

        require_live_context(
            self._store,
            context_id=context_id,
            namespace=namespace,
            moved_in_transaction=tuple(ref.record_id for ref in tx.appended),
        )
        resolved = self._resolve_evidence_locked(namespace, validated)
        self._check_support_rules_locked(namespace, validated, resolved)
        status, strength = self._derive(
            namespace, validated, resolved, retract=False
        )
        effective = dict(validated)
        effective["strength"] = strength
        version = tx.append_record_version(
            record_id=record_id,
            record_version=1,
            kind=BELIEF_KIND,
            body=effective,
            status=status,
            schema_version=BELIEF_SCHEMA_VERSION,
            privacy_class="private",
            scope=normalized_scope,
        )
        result.append(version)
        if work_id is not None:
            tx.complete_work(
                work_id=work_id,
                context_id=context_id,
                outcome="committed",
            )

    def _revise(
        self,
        *,
        namespace: str,
        context_id: str,
        request_id: str,
        target: Mapping[str, Any],
        body: Mapping[str, Any],
        scope: Mapping[str, Any],
        retract: bool,
        work_id: str | None,
        commit_basis: tuple[str, Mapping[str, Any]] | None = None,
        guard: "CommitGuard | None" = None,
    ) -> BeliefSubmission:
        ns = identifiers.require_namespace(namespace)
        cid = identifiers.require_context_id(context_id)
        rid = identifiers.require_request_id(request_id)
        target_ref = RecordRef.from_value(target, field="target")
        if target_ref.namespace != ns:
            raise ScopeViolation(
                f"target is in namespace {target_ref.namespace!r}, not {ns!r}"
            )
        normalized_scope = canonicalize_entity_ids(
            self._store, ns, normalize_scope(scope)
        )
        validated = validate_belief_body(
            body, scope=normalized_scope, retract=retract
        )
        if retract and not validated["evidence_links"]:
            raise EvidenceMissing(
                "an explicit retraction must carry the evidence that justifies it"
            )
        result: list[RecordVersion] = []
        noop_version: list[RecordVersion] = []

        def apply(tx: Any) -> None:
            self._apply_revise_locked(
                tx,
                namespace=ns,
                context_id=cid,
                target_ref=target_ref,
                validated=validated,
                normalized_scope=normalized_scope,
                retract=retract,
                work_id=work_id,
                result=result,
                noop_version=noop_version,
            )

        if commit_basis is None:
            operation = "belief.revise"
            payload: Mapping[str, Any] = {
                "target": target_ref.to_dict(),
                "belief": validated,
                "scope": normalized_scope,
                "retract": bool(retract),
                "work_id": work_id,
            }
        else:
            # C06a: retraction still reuses this method, but the command
            # identity stays the candidate's original proposal.  The internal
            # `retract=True` transformation must not change what was received.
            operation, payload = commit_basis
        receipt = self._store.commit_command(
            context_id=cid,
            request_id=rid,
            operation=operation,
            request_payload=payload,
            apply=apply,
            namespace=ns,
            guard=guard,
        )
        if result:
            return BeliefSubmission(
                version=result[0], receipt=receipt, appended=True
            )
        if noop_version:
            return BeliefSubmission(
                version=noop_version[0], receipt=receipt, appended=False
            )
        if receipt.record_refs:
            return BeliefSubmission(
                version=_version_from_receipt(self._store, receipt),
                receipt=receipt,
                appended=True,
            )
        # An idempotent retry whose first attempt was a legal no-op revision.
        return BeliefSubmission(
            version=self._store.get_current(ns, target_ref.record_id),
            receipt=receipt,
            appended=False,
        )

    def _apply_revise_locked(
        self,
        tx: Any,
        *,
        namespace: str,
        context_id: str,
        target_ref: RecordRef,
        validated: Mapping[str, Any],
        normalized_scope: Mapping[str, Any],
        retract: bool,
        work_id: str | None,
        result: list[RecordVersion],
        noop_version: list[RecordVersion],
    ) -> None:
        """Append the next version of one Belief inside an open TxScope.

        This is the transaction body of :meth:`_revise`, moved here verbatim so
        one combination commit can validate and append several objects inside a
        single ``commit_command``.  A legal no-op revision records the current
        version in ``noop_version`` instead of appending; ``complete_work``
        stays the last write.
        """

        require_live_context(
            self._store,
            context_id=context_id,
            namespace=namespace,
            moved_in_transaction=tuple(ref.record_id for ref in tx.appended),
        )
        current = self._store.get_current(namespace, target_ref.record_id)
        if current.kind != BELIEF_KIND:
            raise RecordKindMismatch(
                f"record {target_ref.record_id} is kind {current.kind!r}, "
                "not a belief"
            )
        if current.record_version != target_ref.record_version:
            raise StateRevisionConflict(
                f"belief {target_ref.record_id} moved from version "
                f"{target_ref.record_version} to {current.record_version}; "
                "revise the current version"
            )
        if current.status in ("retracted", "contradicted"):
            raise InvalidTransition(
                f"belief {target_ref.record_id} is {current.status}; PCS2-008 "
                "does not implement an explicit reopen"
            )
        old_body = parse_belief_body(current)
        conflict = revision_claim_conflict(old_body, validated)
        if conflict == CLAIM_TEXT_FROZEN:
            raise InvalidTransition(
                "the claim text is frozen in PCS2-008; a substantive change "
                "requires retracting this belief and proposing a new record"
            )
        if conflict == CLAIM_TYPE_FROZEN:
            raise InvalidTransition(
                "the claim_type cannot change within one belief record"
            )
        resolved = self._resolve_evidence_locked(namespace, validated)
        self._check_support_rules_locked(
            namespace,
            validated,
            resolved,
            self_record_id=target_ref.record_id if not retract else None,
        )
        status, strength = self._derive(
            namespace, validated, resolved, retract=retract
        )
        effective = dict(validated)
        effective["strength"] = strength
        unchanged = (
            not retract
            and canonical_json_text(effective) == canonical_json_text(old_body)
            and normalized_scope == (current.scope or {})
        )
        if unchanged:
            noop_version.append(current)
            if work_id is not None:
                tx.complete_work(
                    work_id=work_id,
                    context_id=context_id,
                    outcome="checked_no_belief",
                )
            return
        version = tx.append_record_version(
            record_id=target_ref.record_id,
            record_version=current.record_version + 1,
            kind=BELIEF_KIND,
            body=effective,
            status=status,
            schema_version=BELIEF_SCHEMA_VERSION,
            privacy_class="private",
            scope=normalized_scope,
        )
        result.append(version)
        if work_id is not None:
            tx.complete_work(
                work_id=work_id,
                context_id=context_id,
                outcome="committed",
            )

    # -------------------------------------------------------------- evidence

    def _resolve_evidence_locked(
        self, namespace: str, body: Mapping[str, Any]
    ) -> tuple[_ResolvedEvidence, ...]:
        resolved: list[_ResolvedEvidence] = []
        for link in body["evidence_links"]:
            ref = link["ref"]
            if ref.get("ref_type") == "event":
                resolved.append(self._resolve_event_ref(namespace, link))
            elif ref.get("ref_type") == "collab_input":
                resolved.append(self._resolve_input_ref(namespace, link))
            else:
                resolved.append(self._resolve_record_ref(namespace, link))
        return tuple(resolved)

    def _resolve_input_ref(
        self, namespace: str, link: Mapping[str, Any]
    ) -> _ResolvedEvidence:
        """RG05-F02: a current user statement is the evidence for a preference.

        The input is a collaboration row, not an event.  It is resolved through
        the same deletion barrier beliefs use for events: a deleted or cleared
        input raises ``SourceRedacted`` rather than being silently treated as a
        usable statement.
        """

        ref = link["ref"]
        if set(ref) != {"ref_type", "namespace", "input_id", "content_version"}:
            raise InvalidInput(
                "evidence_links[].ref for a collab_input must have exactly "
                "ref_type, namespace, input_id and content_version"
            )
        if ref.get("namespace") != namespace:
            raise ScopeViolation(
                f"evidence input {ref.get('input_id')!r} is in namespace "
                f"{ref.get('namespace')!r}, not {namespace!r}"
            )
        try:
            row = self._store.load_input(
                namespace=namespace,
                input_id=str(ref["input_id"]),
                content_version=int(ref["content_version"]),
            )
        except Redacted as exc:
            raise SourceRedacted(
                f"evidence input {ref.get('input_id')} has been deleted"
            ) from exc
        except RecordNotFound as exc:
            raise EvidenceMissing(
                f"evidence input {ref.get('input_id')} does not exist in this namespace"
            ) from exc
        if row.origin_kind != "user_statement":
            raise SourceUntrusted(
                f"evidence input {row.input_id} has origin_kind "
                f"{row.origin_kind!r}; a user statement is required"
            )
        group = f"collab_input:{row.input_id}"
        _require_group(link, group)
        return _ResolvedEvidence(
            link=dict(link),
            ref_type="collab_input",
            origin_group_id=group,
            source_kinds=("user_report",),
            source_instances=("collab_input",),
            source_event_ids=(row.input_id,),
            event_ids=(),
            multi_source=False,
            imported=False,
        )

    def _resolve_event_ref(
        self, namespace: str, link: Mapping[str, Any]
    ) -> _ResolvedEvidence:
        ref = EventRef.from_value(link["ref"], field="evidence_links[].ref")
        if ref.namespace != namespace:
            raise ScopeViolation(
                f"evidence {ref.event_id} is in namespace {ref.namespace!r}, "
                f"not {namespace!r}"
            )
        if self._store.event_is_deleted(namespace, ref.event_id):
            raise SourceRedacted(
                f"evidence event {ref.event_id} has been deleted"
            )
        projection_gate = getattr(self._store, "projection_is_usable", None)
        if projection_gate is not None and not projection_gate(
            namespace, ref.event_id
        ):
            raise SourceRedacted(
                f"evidence event {ref.event_id} has no usable experience projection"
            )
        header = self._store.event_header(namespace, ref.event_id)
        if header is None:
            raise EvidenceMissing(
                f"evidence event {ref.event_id} does not exist in this namespace"
            )
        if is_question_event_type(header.event_type):
            # B: a question expresses an information request, not a fact about
            # the world.  Keeping the existing SOURCE_UNTRUSTED code (no new
            # public error code) is deliberate: the source is not usable as
            # observation evidence.
            raise SourceUntrusted(
                f"evidence event {ref.event_id} is a question "
                f"({header.event_type!r}); a question is not a factual statement"
            )
        group = f"event:{ref.event_id}"
        _require_group(link, group)
        return _ResolvedEvidence(
            link=dict(link),
            ref_type="event",
            origin_group_id=group,
            source_kinds=(header.source_kind,),
            source_instances=(header.source_instance,),
            source_event_ids=(header.source_event_id,),
            event_ids=(ref.event_id,),
            multi_source=False,
            imported=header.source_kind == "imported_memory",
        )

    def _resolve_record_ref(
        self, namespace: str, link: Mapping[str, Any]
    ) -> _ResolvedEvidence:
        ref = RecordRef.from_value(link["ref"], field="evidence_links[].ref")
        if ref.namespace != namespace:
            raise ScopeViolation(
                f"evidence {ref.record_id} is in namespace {ref.namespace!r}, "
                f"not {namespace!r}"
            )
        if self._store.record_is_deleted(namespace, ref.record_id):
            raise SourceRedacted(
                f"evidence record {ref.record_id} has been deleted"
            )
        pointer = self._store.record_pointer(namespace, ref.record_id)
        if pointer is None:
            raise EvidenceMissing(
                f"evidence record {ref.record_id} does not exist in this namespace"
            )
        if pointer.kind != "episode":
            raise EvidenceMissing(
                "belief evidence must be an EventRef or an Episode RecordRef; "
                f"record {ref.record_id} is kind {pointer.kind!r}"
            )
        try:
            episode_version = self._store.get_version(
                namespace, ref.record_id, ref.record_version
            )
        except RecordNotFound as exc:
            raise EvidenceMissing(
                f"episode {ref.record_id} has no version {ref.record_version}"
            ) from exc
        sources = self._store.episode_sources_for_record(namespace, ref.record_id)
        if not sources:
            raise CorruptStore(
                f"episode {ref.record_id} has no episode_sources mapping"
            )
        # The mapping is per record, so verify that the exact version cited as
        # evidence still claims the same source events.  A fabricated or
        # rewritten Episode body must not contribute a foreign origin group.
        shape = parse_episode_body(episode_version)
        if {item.event_id for item in shape.event_refs} != {
            source.event_id for source in sources
        }:
            raise CorruptStore(
                f"episode {ref.record_id}@{ref.record_version} disagrees with "
                "its episode_sources mapping"
            )
        headers = []
        for source in sources:
            if self._store.event_is_deleted(namespace, source.event_id):
                raise SourceRedacted(
                    f"a source event of episode {ref.record_id} has been deleted"
                )
            header = self._store.event_header(namespace, source.event_id)
            if header is None:
                raise CorruptStore(
                    f"episode {ref.record_id} references missing event "
                    f"{source.event_id}"
                )
            if is_question_event_type(header.event_type):
                # A question must not be laundered into evidence by wrapping it
                # in an Episode record first.
                raise SourceUntrusted(
                    f"episode {ref.record_id} is built from question event "
                    f"{source.event_id} ({header.event_type!r}); a question is "
                    "not a factual statement"
                )
            headers.append(header)
        multi_source = len(headers) > 1
        group = (
            f"episode:{ref.record_id}"
            if multi_source
            else f"event:{sources[0].event_id}"
        )
        _require_group(link, group)
        return _ResolvedEvidence(
            link=dict(link),
            ref_type="record",
            origin_group_id=group,
            source_kinds=tuple(header.source_kind for header in headers),
            source_instances=tuple(header.source_instance for header in headers),
            source_event_ids=tuple(header.source_event_id for header in headers),
            event_ids=tuple(source.event_id for source in sources),
            multi_source=multi_source,
            imported=any(
                header.source_kind == "imported_memory" for header in headers
            ),
        )

    # ---------------------------------------------------------- support rules

    def _check_support_rules_locked(
        self,
        namespace: str,
        body: Mapping[str, Any],
        resolved: Sequence[_ResolvedEvidence],
        *,
        self_record_id: str | None = None,
    ) -> None:
        claim_type = body["claim_type"]
        supports = [
            item for item in resolved if item.relation in SUPPORTING_RELATIONS
        ]
        # A derived rule's premises are part of its support rule, so they are
        # resolved even when the rule currently has no supporting links.
        if claim_type == "derived_rule":
            self._check_premises_locked(
                namespace, body, self_record_id=self_record_id
            )
        # No model inference / synthetic fixture may act as evidence at all.
        for item in resolved:
            for source_kind in item.source_kinds:
                if source_kind not in TRUSTED_EVIDENCE_SOURCE_KINDS:
                    raise SourceUntrusted(
                        f"source_kind {source_kind!r} is not trusted evidence"
                    )
        if not supports:
            return
        allowed = SUPPORT_SOURCE_KINDS[claim_type]
        for item in supports:
            for source_kind in item.source_kinds:
                if source_kind not in allowed:
                    raise EvidenceMissing(
                        f"claim_type {claim_type!r} cannot be supported by "
                        f"source_kind {source_kind!r}"
                    )
        if claim_type == "user_preference":
            instances = {
                instance for item in supports for instance in item.source_instances
            }
            if len(instances) > 1:
                raise EvidenceMissing(
                    "a user_preference must come from one reporting person; "
                    "supports from different source_instance values were supplied"
                )
        if claim_type == "external_report":
            self._check_external_report_quotes_locked(namespace, supports)

    def _check_external_report_quotes_locked(
        self, namespace: str, supports: Sequence["_ResolvedEvidence"]
    ) -> None:
        """Verify that each quoted fragment really came from its fetch event.

        ``external_report`` claims "source S says X", so the only honest support
        is a verbatim fragment of what the host actually received.  The check is
        mechanical and deliberately narrow:

        * the event must carry the captured body (``capture="evidence"``);
        * ``sha256(text)`` must equal the recorded ``content_sha256``;
        * ``quote`` must be a substring of ``text``.

        It proves the fragment appears in that response -- nothing about whether
        the page is right, whether the fragment supports the conclusion, or
        whether the page was later changed.
        """

        for item in supports:
            if item.ref_type != "event":
                raise _evidence_unusable(
                    "an external_report must cite a fetch event, not a record",
                    "evidence_type_mismatch",
                )
            quote = item.link.get("quote")
            if not isinstance(quote, str) or not quote:
                raise _evidence_unusable(
                    "an external_report evidence link must carry a quote",
                    "evidence_quote_missing",
                )
            event_id = item.event_ids[0]
            entry = self._store.get_event(namespace, event_id)
            payload = entry.payload if isinstance(entry.payload, Mapping) else {}
            text = payload.get("text")
            recorded = payload.get("content_sha256")
            if not isinstance(text, str) or not isinstance(recorded, str):
                raise _evidence_unusable(
                    f"fetch event {event_id} carries no captured body; "
                    "only capture=\"evidence\" fetches can support a claim",
                    "evidence_text_unavailable",
                )
            if sha256_hex(text) != recorded:
                raise _evidence_unusable(
                    f"fetch event {event_id} content does not match its digest",
                    "evidence_content_hash_mismatch",
                )
            if quote not in text:
                raise _evidence_unusable(
                    f"quote is not a verbatim fragment of fetch event {event_id}",
                    "evidence_quote_mismatch",
                )

    def _check_premises_locked(
        self,
        namespace: str,
        body: Mapping[str, Any],
        *,
        self_record_id: str | None,
    ) -> None:
        for raw in body["premise_refs"]:
            ref = RecordRef.from_value(raw, field="premise_refs[]")
            if ref.namespace != namespace:
                raise ScopeViolation("a premise belongs to another namespace")
            if self_record_id is not None and ref.record_id == self_record_id:
                raise EvidenceMissing(
                    "a belief cannot use itself as its own premise"
                )
            if self._store.record_is_deleted(namespace, ref.record_id):
                raise SourceRedacted(
                    f"premise {ref.record_id} has been deleted"
                )
            pointer = self._store.record_pointer(namespace, ref.record_id)
            if pointer is None:
                raise EvidenceMissing(
                    f"premise {ref.record_id} does not exist"
                )
            if pointer.kind != BELIEF_KIND:
                raise EvidenceMissing(
                    f"premise {ref.record_id} is kind {pointer.kind!r}, not a belief"
                )
            if pointer.current_version != ref.record_version:
                raise EvidenceMissing(
                    f"premise {ref.record_id} must reference its current version"
                )
            premise = self._store.get_current(namespace, ref.record_id)
            if premise.status != "supported":
                raise EvidenceMissing(
                    f"premise {ref.record_id} is {premise.status!r}, not supported"
                )

    # ------------------------------------------------------------- derivation

    def _derive(
        self,
        namespace: str,
        body: Mapping[str, Any],
        resolved: Sequence[_ResolvedEvidence],
        *,
        retract: bool,
    ) -> tuple[str, str]:
        claim_type = body["claim_type"]
        supports = [
            item for item in resolved if item.relation in SUPPORTING_RELATIONS
        ]
        contradicts = [
            item for item in resolved if item.relation == "contradicts"
        ]
        count = self._independent_support_count(namespace, supports)
        strength = _cap_strength(body["strength"], count, claim_type)
        expired = _is_expired(body["valid_until"], self._clock())
        status = derive_status(
            claim_type=claim_type,
            has_support=bool(supports),
            has_contradiction=bool(contradicts),
            expired=expired,
            retract=retract,
        )
        # A claim with no support, and a claim the evidence contradicts, both
        # carry "insufficient" strength regardless of what was requested.
        if status == "contradicted" or (status == "proposed" and not supports):
            return status, "insufficient"
        return status, strength

    def _independent_support_count(
        self, namespace: str, supports: Sequence[_ResolvedEvidence]
    ) -> int:
        if not supports:
            return 0
        groups: dict[str, _ResolvedEvidence] = {}
        for item in supports:
            groups.setdefault(item.origin_group_id, item)
        if any(item.multi_source or item.imported for item in groups.values()):
            return 1
        if len(groups) <= 1:
            return len(groups)
        seen_sources: set[tuple[str, str]] = set()
        for item in groups.values():
            for source in zip(item.source_instances, item.source_event_ids):
                if source in seen_sources:
                    return 1
                seen_sources.add(source)
        items = list(groups.values())
        for index, left in enumerate(items):
            for right in items[index + 1 :]:
                if self._causation_exists(
                    namespace, left.event_ids[0], right.event_ids[0]
                ):
                    return 1
        return min(len(items), 3)

    def _causation_exists(
        self, namespace: str, left_event_id: str, right_event_id: str
    ) -> bool:
        row = self._store.connection.execute(
            "SELECT 1 FROM event_causes "
            "WHERE namespace = ? AND ("
            "  (from_event_id = ? AND to_event_id = ?) OR "
            "  (from_event_id = ? AND to_event_id = ?)"
            ") LIMIT 1",
            (namespace, left_event_id, right_event_id, right_event_id, left_event_id),
        ).fetchone()
        return row is not None


# ------------------------------------------------------------ validation


def validate_belief_body(
    body: Any,
    *,
    scope: Mapping[str, Any],
    retract: bool = False,
    enforce_support_rules: bool = True,
) -> dict[str, Any]:
    """Validate the ten frozen Belief body fields and enforce §3 support rules.

    ``enforce_support_rules=False`` is used when reading a stored body back:
    the shape is still checked strictly, but the "supported claim needs a
    declared scope / deadline" rules are not re-litigated (they were enforced
    when the version was written, and old status/evidence may since have
    changed through newer versions).
    """

    if not isinstance(body, Mapping):
        raise InvalidInput("belief body must be a JSON object")
    keys = set(body)
    missing = sorted(BELIEF_BODY_KEYS - keys)
    if missing:
        raise InvalidInput(
            f"belief body is missing required field(s): {', '.join(missing)}"
        )
    unknown = sorted(str(key) for key in keys - BELIEF_BODY_KEYS)
    if unknown:
        raise InvalidInput(
            f"belief body has unknown field(s): {', '.join(unknown)}"
        )

    claim = _require_text(body["claim"], "claim", MAX_CLAIM_LENGTH)
    claim_type = body["claim_type"]
    if claim_type not in CLAIM_TYPES:
        raise InvalidInput(f"claim_type must be one of {', '.join(CLAIM_TYPES)}")
    strength = body["strength"]
    if strength not in STRENGTHS:
        raise InvalidInput(f"strength must be one of {', '.join(STRENGTHS)}")
    strength_reason = _require_text(
        body["strength_reason"], "strength_reason", MAX_STRENGTH_REASON_LENGTH
    )
    inference_basis = body["inference_basis"]
    if inference_basis not in INFERENCE_BASES:
        raise InvalidInput(
            f"inference_basis must be one of {', '.join(INFERENCE_BASES)}"
        )
    expected_basis = INFERENCE_BASIS_BY_CLAIM_TYPE[claim_type]
    if inference_basis != expected_basis:
        raise InvalidInput(
            f"claim_type {claim_type!r} requires inference_basis "
            f"{expected_basis!r}, not {inference_basis!r}"
        )
    review_condition = _require_text(
        body["review_condition"], "review_condition", MAX_REVIEW_CONDITION_LENGTH
    )
    alternative_group_id = body["alternative_group_id"]
    if alternative_group_id is not None:
        alternative_group_id = _require_text(
            alternative_group_id,
            "alternative_group_id",
            MAX_ALTERNATIVE_GROUP_LENGTH,
        )
    valid_until = _parse_valid_until(body["valid_until"])

    links = _validate_evidence_links(body["evidence_links"])
    premises = _validate_premise_refs(body["premise_refs"])

    has_support = any(
        link["relation"] in SUPPORTING_RELATIONS for link in links
    )
    if retract and not links:
        raise EvidenceMissing(
            "an explicit retraction must carry the evidence that justifies it"
        )
    if enforce_support_rules:
        if claim_type == "derived_rule" and not premises:
            raise EvidenceMissing(
                "a derived_rule needs at least one premise belief"
            )
        if has_support and claim_type in SCOPE_REQUIRED_CLAIM_TYPES:
            if not scope:
                raise EvidenceMissing(
                    f"a supported {claim_type} must state its applicability in "
                    "the record scope, not only in strength_reason"
                )
        if has_support and claim_type == "environment_state":
            if valid_until is None:
                raise EvidenceMissing(
                    "an environment_state observation needs an explicit "
                    "valid_until deadline"
                )

    return {
        "claim": claim,
        "claim_type": claim_type,
        "evidence_links": links,
        "strength": strength,
        "strength_reason": strength_reason,
        "inference_basis": inference_basis,
        "premise_refs": premises,
        "alternative_group_id": alternative_group_id,
        "valid_until": valid_until,
        "review_condition": review_condition,
    }


def is_question_event_type(event_type: Any) -> bool:
    """Whether a stored ``event_type`` names a question (ends with ``.question``).

    This is the one trusted classifier used by both the Context renderer and
    the evidence validator.  It reads the journal envelope field only; an
    event payload that calls itself a fact (or a question) is irrelevant.
    """

    return isinstance(event_type, str) and event_type.endswith(
        QUESTION_EVENT_SUFFIX
    )


def parse_belief_body(version: RecordVersion) -> dict[str, Any]:
    """Parse one stored Belief body; corruption is never half-returned."""

    if version.kind != BELIEF_KIND:
        raise CorruptStore(
            f"record {version.record_id} is kind {version.kind!r}, not a belief"
        )
    if version.schema_version != BELIEF_SCHEMA_VERSION:
        raise CorruptStore(
            f"belief {version.record_id} has unsupported schema_version "
            f"{version.schema_version!r}"
        )
    try:
        return validate_belief_body(
            version.body,
            scope={},
            enforce_support_rules=False,
        )
    except (InvalidInput, EvidenceMissing) as exc:
        raise CorruptStore(
            f"belief {version.record_id} has an invalid body: {exc}"
        ) from exc


#: Stable reasons for a revise that violates the PCS2-008 claim freeze.
CLAIM_TEXT_FROZEN = "claim_text_frozen"
CLAIM_TYPE_FROZEN = "claim_type_frozen"


def revision_claim_conflict(
    current_body: Mapping[str, Any],
    proposed_body: Mapping[str, Any],
) -> str | None:
    """Return the C06 claim-freeze reason for a proposed revision, if any.

    This is deliberately a pure predicate over already-validated belief bodies.
    The C06 transaction remains the authority: this helper only decides what a
    review projection may say before a commit is attempted.  Keeping the rule
    here avoids C07 growing a second, divergent interpretation of
    ``revise_belief``.
    """

    if proposed_body.get("claim") != current_body.get("claim"):
        return CLAIM_TEXT_FROZEN
    if proposed_body.get("claim_type") != current_body.get("claim_type"):
        return CLAIM_TYPE_FROZEN
    return None


def _evidence_link_is_live(
    store: CognitionStore,
    namespace: str,
    link: Mapping[str, Any],
) -> bool:
    """Whether one evidence link still points at readable, undeleted sources."""

    ref = link["ref"]
    if ref.get("ref_type") == "event":
        event_id = ref["event_id"]
        if store.event_is_deleted(namespace, event_id):
            return False
        projection_gate = getattr(store, "projection_is_usable", None)
        if projection_gate is not None and not projection_gate(
            namespace, event_id
        ):
            return False
        if store.event_header(namespace, event_id) is None:
            raise CorruptStore(
                f"belief evidence event {event_id} is missing"
            )
        return True
    if ref.get("ref_type") == "collab_input":
        try:
            row = store.load_input(
                namespace=namespace,
                input_id=str(ref["input_id"]),
                content_version=int(ref["content_version"]),
            )
        except Redacted:
            return False
        except RecordNotFound as exc:
            raise CorruptStore(
                f"belief evidence input {ref.get('input_id')} is missing"
            ) from exc
        return row.origin_kind == "user_statement"
    record = RecordRef.from_value(ref, field="evidence_links[].ref")
    if store.record_is_deleted(namespace, record.record_id):
        return False
    pointer = store.record_pointer(namespace, record.record_id)
    if pointer is None:
        raise CorruptStore(
            f"belief evidence record {record.record_id} is missing"
        )
    if pointer.kind != "episode":
        raise CorruptStore(
            f"belief evidence record {record.record_id} is not an episode"
        )
    try:
        episode_version = store.get_version(
            namespace, record.record_id, record.record_version
        )
    except RecordNotFound as exc:
        raise CorruptStore(
            f"belief evidence episode {record.record_id} has no version "
            f"{record.record_version}"
        ) from exc
    shape = parse_episode_body(episode_version)
    sources = store.episode_sources_for_record(namespace, record.record_id)
    if not sources:
        raise CorruptStore(
            f"episode {record.record_id} has no episode_sources mapping"
        )
    if {item.event_id for item in shape.event_refs} != {
        source.event_id for source in sources
    }:
        raise CorruptStore(
            f"episode {record.record_id}@{record.record_version} disagrees "
            "with its episode_sources mapping"
        )
    for source in sources:
        if store.event_is_deleted(namespace, source.event_id):
            return False
        if store.event_header(namespace, source.event_id) is None:
            raise CorruptStore(
                f"episode {record.record_id} references missing event "
                f"{source.event_id}"
            )
    return True


def _premise_is_live(
    store: CognitionStore,
    namespace: str,
    raw: Mapping[str, Any],
) -> bool:
    """Whether one premise reference still points at a readable record version."""

    premise = RecordRef.from_value(raw, field="premise_refs[]")
    if store.record_is_deleted(namespace, premise.record_id):
        return False
    if store.record_pointer(namespace, premise.record_id) is None:
        raise CorruptStore(
            f"belief premise {premise.record_id} is missing"
        )
    try:
        store.get_version(
            namespace, premise.record_id, premise.record_version
        )
    except RecordNotFound as exc:
        raise CorruptStore(
            f"belief premise {premise.record_id} has no version "
            f"{premise.record_version}"
        ) from exc
    return True


def belief_evidence_is_live(
    store: CognitionStore,
    *,
    namespace: str,
    body: Mapping[str, Any],
) -> bool:
    """Whether every stored source this Belief cites is still outside a barrier.

    PCS2-008 does not rewrite an existing Belief's status when a later deletion
    removes its evidence (automatic propagation is a later card).  It does,
    however, refuse to present that Belief as current material: a Context or
    SelfView builder calls this function and omits the Belief when any event or
    Episode source it used has been deleted.  That is the deletion-side answer
    to "a source was removed, so the judgment must not keep entering Context".

    A *missing* source (not merely deleted) is corruption, because event and
    record-version rows are immutable in this schema.
    """

    for link in body["evidence_links"]:
        if not _evidence_link_is_live(store, namespace, link):
            return False
    for raw in body["premise_refs"]:
        if not _premise_is_live(store, namespace, raw):
            return False
    return True


@dataclass(frozen=True)
class BeliefReEvaluation:
    """Read-only verdict computed from only the still-readable evidence links.

    Produced by :func:`reevaluate_belief_against_live_sources`; it is material
    for a later Review card, never a commit.  Nothing here is persisted.
    """

    live_link_count: int
    retired_refs: tuple[dict[str, Any], ...]
    status: str
    strength: str
    degraded_to_proposed: bool


def reevaluate_belief_against_live_sources(
    store: CognitionStore,
    *,
    namespace: str,
    body: Mapping[str, Any],
    clock: Callable[[], datetime] | None = None,
) -> BeliefReEvaluation:
    """Recompute one stored Belief's verdict without its deleted sources.

    This is the read-only half of the J01 re-evaluation split: it answers
    "what would this judgement be if only the surviving evidence counted?" and
    writes nothing.  The verdict is derived through the same rules as the
    commit path (:meth:`BeliefService._derive`), so the computation cannot
    drift from what a new proposal would store.

    Writing the verdict back (a new Belief version through Tx B, or a Review
    candidate) belongs to the later review card and is deliberately absent.
    A *missing* source is corruption and raises ``CorruptStore``, exactly like
    :func:`belief_evidence_is_live`.
    """

    ns = identifiers.require_namespace(namespace)
    live_links: list[Mapping[str, Any]] = []
    retired: list[dict[str, Any]] = []
    for link in body["evidence_links"]:
        if _evidence_link_is_live(store, ns, link):
            live_links.append(link)
        else:
            retired.append(_retired_ref(link["ref"], role="evidence"))
    for raw in body["premise_refs"]:
        if not _premise_is_live(store, ns, raw):
            retired.append(_retired_ref(raw, role="premise"))

    service = BeliefService(store, clock=clock)
    filtered = dict(body)
    filtered["evidence_links"] = live_links
    resolved = service._resolve_evidence_locked(ns, filtered)
    status, strength = service._derive(ns, body, resolved, retract=False)
    live_support = any(
        item.relation in SUPPORTING_RELATIONS for item in resolved
    )
    return BeliefReEvaluation(
        live_link_count=len(live_links),
        retired_refs=tuple(retired),
        status=status,
        strength=strength,
        degraded_to_proposed=status == "proposed" and not live_support,
    )


def _retired_ref(ref: Mapping[str, Any], *, role: str) -> dict[str, Any]:
    """Describe one unreachable source without copying its content."""

    out: dict[str, Any] = {"role": role, "ref_type": ref.get("ref_type")}
    if ref.get("ref_type") == "event":
        out["namespace"] = ref.get("namespace")
        out["event_id"] = ref.get("event_id")
    else:
        out["namespace"] = ref.get("namespace")
        out["record_id"] = ref.get("record_id")
        out["record_version"] = ref.get("record_version")
    return out


def _validate_evidence_links(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InvalidInput("evidence_links must be an array of objects")
    if len(value) > MAX_EVIDENCE_LINKS:
        raise InvalidInput(
            f"evidence_links must have at most {MAX_EVIDENCE_LINKS} items"
        )
    links: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise InvalidInput(f"evidence_links[{index}] must be an object")
        keys = set(item)
        if not _EVIDENCE_KEYS <= keys or keys - _EVIDENCE_KEYS - _OPTIONAL_EVIDENCE_KEYS:
            raise InvalidInput(
                f"evidence_links[{index}] must have exactly ref, relation, "
                "origin_group_id and applicability_reason, plus an optional quote"
            )
        relation = item["relation"]
        if relation not in RELATIONS:
            raise InvalidInput(
                f"evidence_links[{index}].relation must be one of "
                f"{', '.join(RELATIONS)}"
            )
        origin_group = _require_text(
            item["origin_group_id"],
            f"evidence_links[{index}].origin_group_id",
            MAX_ORIGIN_GROUP_LENGTH,
        )
        applicability = _require_text(
            item["applicability_reason"],
            f"evidence_links[{index}].applicability_reason",
            MAX_APPLICABILITY_REASON_LENGTH,
        )
        ref = _validate_any_ref(item["ref"], f"evidence_links[{index}].ref")
        ref_key = canonical_json_text(ref)
        if ref_key in seen:
            raise InvalidInput(
                "evidence_links must not repeat the same reference"
            )
        seen.add(ref_key)
        entry = {
            "ref": ref,
            "relation": relation,
            "origin_group_id": origin_group,
            "applicability_reason": applicability,
        }
        if "quote" in keys:
            entry["quote"] = _require_text(
                item["quote"],
                f"evidence_links[{index}].quote",
                MAX_QUOTE_LENGTH,
            )
        links.append(entry)
    return links


def _validate_premise_refs(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InvalidInput("premise_refs must be an array of RecordRef objects")
    if len(value) > MAX_PREMISE_REFS:
        raise InvalidInput(
            f"premise_refs must have at most {MAX_PREMISE_REFS} items"
        )
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        ref = RecordRef.from_value(item, field=f"premise_refs[{index}]").to_dict()
        key = canonical_json_text(ref)
        if key in seen:
            raise InvalidInput("premise_refs must not repeat the same reference")
        seen.add(key)
        refs.append(ref)
    return refs


def _validate_any_ref(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidInput(f"{field} must be a typed reference object")
    ref_type = value.get("ref_type")
    if ref_type == "event":
        return EventRef.from_value(value, field=field).to_dict()
    if ref_type == "record":
        return RecordRef.from_value(value, field=field).to_dict()
    if ref_type == "collab_input":
        if set(value) != {"ref_type", "namespace", "input_id", "content_version"}:
            raise InvalidInput(
                f"{field} must have exactly ref_type, namespace, input_id and "
                "content_version for a collab_input"
            )
        return {
            "ref_type": "collab_input",
            "namespace": identifiers.require_namespace(value["namespace"]),
            "input_id": identifiers.require_record_id(value["input_id"]),
            "content_version": identifiers.require_record_version(
                value["content_version"]
            ),
        }
    raise InvalidInput(
        f'{field}.ref_type must be "event", "record" or "collab_input"'
    )


def _require_group(link: Mapping[str, Any], expected: str) -> None:
    if link["origin_group_id"] != expected:
        raise InvalidInput(
            f"origin_group_id {link['origin_group_id']!r} is not the group the "
            f"store derives for this source ({expected!r}); a model cannot "
            "declare source independence"
        )


def _require_text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise InvalidInput(f"{field} must be at most {max_length} characters")
    return value


def _parse_valid_until(value: Any) -> str | None:
    text = identifiers.require_observed_at(value)
    if text is None:
        return None
    _parse_timestamp(text, "valid_until")
    return text


def _parse_timestamp(text: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidInput(f"{field} is not an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise InvalidInput(f"{field} must carry a UTC offset")
    return parsed.astimezone(timezone.utc)


def _is_expired(valid_until: str | None, now: datetime) -> bool:
    if valid_until is None:
        return False
    moment = _parse_timestamp(valid_until, "valid_until")
    return moment <= now.astimezone(timezone.utc)


def _evidence_unusable(message: str, reason_code: str) -> EvidenceMissing:
    """An evidence error that names the failing check on the exception itself.

    No new public error code is introduced (the contract's ``EVIDENCE_MISSING``
    stays the code); ``reason_code`` is what the change service records in the
    decision row so "why was this rejected" stays answerable.
    """

    error = EvidenceMissing(message)
    error.reason_code = reason_code  # type: ignore[attr-defined]
    return error


def _cap_strength(requested: str, count: int, claim_type: str) -> str:
    if count <= 0:
        return "insufficient"
    cap = {1: "limited", 2: "moderate"}.get(count, "strong")
    if claim_type == "derived_rule" and STRENGTH_RANK[cap] > STRENGTH_RANK["limited"]:
        cap = "limited"
    if STRENGTH_RANK[requested] < STRENGTH_RANK[cap]:
        return requested
    return cap


def _version_from_receipt(store: CognitionStore, receipt: Any) -> RecordVersion:
    if len(receipt.record_refs) != 1:
        raise CorruptStore(
            "a committed belief proposal receipt must name exactly one record version"
        )
    ref = receipt.record_refs[0]
    return store.get_version(receipt.namespace, ref.record_id, ref.record_version)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "BELIEF_BODY_KEYS",
    "BELIEF_KIND",
    "BELIEF_SCHEMA_VERSION",
    "BeliefReEvaluation",
    "BeliefService",
    "BeliefSubmission",
    "CLAIM_TYPES",
    "INFERENCE_BASES",
    "INFERENCE_BASIS_BY_CLAIM_TYPE",
    "QUESTION_EVENT_SUFFIX",
    "RELATIONS",
    "SCOPE_REQUIRED_CLAIM_TYPES",
    "STRENGTHS",
    "STRENGTH_RANK",
    "SUPPORTING_RELATIONS",
    "SUPPORT_SOURCE_KINDS",
    "derive_status",
    "TRUSTED_EVIDENCE_SOURCE_KINDS",
    "belief_evidence_is_live",
    "is_question_event_type",
    "parse_belief_body",
    "reevaluate_belief_against_live_sources",
    "validate_belief_body",
]
