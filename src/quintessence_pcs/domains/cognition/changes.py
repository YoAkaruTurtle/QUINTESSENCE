"""J06 S3b: the ``cognition.change`` composition bundle.

A composition bundle asks for one to four cognitive changes to become
authoritative **together**: either every record version is appended in one
``TxScope`` or nothing is.  This module owns the envelope and the replay; it
never opens a transaction of its own.

Three phases, deliberately separated:

``validate_change_bundle``
    Runs before any transaction.  Checks only the envelope: closed operation
    set, exact key sets, one to four changes, unique targets, namespace
    agreement, and the ``close``/``resolve`` discrimination.  It touches no
    store state.

``prepare_change_set``
    Runs before the transaction and performs the domain validation that needs
    the store: ``normalize_scope`` + ``validate_belief_body`` for Belief
    operations and ``QuestionService.prepare_transition`` for Question
    operations.  The result is an immutable value set.

``ChangeApplier.apply``
    Runs **inside** the open transaction and appends exactly those prepared
    values through the existing ``BeliefService`` / ``QuestionService`` seams.

Prepared values are not a commit authorisation
----------------------------------------------
Every store-dependent decision — record versions, evidence liveness, Scope,
privacy, deletion barriers, the three watermarks, the frozen claim text and
the Question state machine — is re-run inside the transaction by those seams.
A prepared set may be stale by the time the transaction opens; the
transaction is what decides, and a stale set fails closed with the same error
a direct call would raise.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from dataclasses import dataclass, field
from typing import Any

from ...foundation import identifiers
from ...foundation.errors import InvalidInput, StateRevisionConflict
from ..refs import RecordRef
from ..scope import normalize_scope
from .beliefs import (
    BeliefService,
    parse_belief_body,
    validate_belief_body,
)
from .questions import QuestionAccess, QuestionService

#: The outer operation string of one composition commit.
BUNDLE_OPERATION = "cognition.change"

#: The closed set of operations one bundle may carry.
CHANGE_OPS = (
    "propose_belief",
    "revise_belief",
    "retract_belief",
    "resolve_question",
    "close_question",
    "reopen_question",
)

#: A bundle carries at least one and at most four changes.
MIN_CHANGES = 1
MAX_CHANGES = 4

#: ``summary`` is a bounded audit label, never free-form reasoning.
MAX_SUMMARY_CHARS = 512

_BUNDLE_KEYS = frozenset({"operation", "summary", "changes"})
_CHANGE_KEYS = {
    "propose_belief": frozenset({"op", "body", "scope"}),
    "revise_belief": frozenset({"op", "target", "body", "scope"}),
    "retract_belief": frozenset({"op", "target", "evidence_links", "reason"}),
    "resolve_question": frozenset({"op", "target", "resolution_refs", "resolution_reason"}),
    "close_question": frozenset({"op", "target", "closed_reason"}),
    "reopen_question": frozenset({"op", "target", "new_evidence_refs", "reason"}),
}

#: Public view of the closed field set for each operation.  The tool catalog
#: builds its JSON Schema from these constants instead of keeping a second copy
#: of the bundle shape that could drift from the validator.
CHANGE_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {op: frozenset(keys) for op, keys in _CHANGE_KEYS.items()}
)

#: JSON type of each bundle field, again from the validator's point of view:
#: these are the shapes ``_validate_one_change`` actually accepts.
CHANGE_FIELD_TYPES: Mapping[str, str] = MappingProxyType(
    {
        "body": "object",
        "scope": "object",
        "target": "object",
        "evidence_links": "array",
        "resolution_refs": "array",
        "new_evidence_refs": "array",
        "reason": "string",
        "resolution_reason": "string",
        "closed_reason": "string",
    }
)

_BELIEF_OPS = ("propose_belief", "revise_belief", "retract_belief")
_QUESTION_OPS = ("resolve_question", "close_question", "reopen_question")


class ChangeBundleInvalid(InvalidInput):
    """A composition bundle violates the frozen envelope rules.

    ``reason`` is a stable machine label so callers and tests do not have to
    parse prose.  It is not a new ``pcs.v2`` error code: the exception stays an
    ``INVALID_SCHEMA`` ``InvalidInput`` like every other structural refusal.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True)
class ChangeBundle:
    """One raw composition bundle before any validation."""

    namespace: str
    summary: str
    changes: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class ValidatedChange:
    """A bundle whose envelope is proven and whose payload is canonical."""

    namespace: str
    summary: str
    changes: tuple[dict[str, Any], ...]
    request_payload: dict[str, Any]


@dataclass(frozen=True)
class PreparedBelief:
    """One Belief operation prepared for the transaction.

    ``record_id`` and ``validated`` are computed outside the transaction; the
    seam re-checks versions, evidence and deletion inside it.
    """

    op: str
    record_id: str
    target_ref: RecordRef | None
    validated: Mapping[str, Any]
    scope: Mapping[str, Any]
    retract: bool


@dataclass(frozen=True)
class PreparedQuestion:
    """One Question transition prepared by ``QuestionService.prepare_transition``."""

    op: str
    ref: RecordRef
    expected_version: int
    new_body: Mapping[str, Any]
    new_status: str
    privacy_class: str
    scope: Mapping[str, Any] | None


@dataclass(frozen=True)
class PreparedChangeSet:
    """The immutable result of the pre-transaction preparation phase."""

    beliefs: tuple[PreparedBelief, ...] = field(default_factory=tuple)
    questions: tuple[PreparedQuestion, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# phase 1: envelope
# ---------------------------------------------------------------------------


def validate_change_bundle(namespace: str, bundle: Mapping[str, Any]) -> ValidatedChange:
    """Prove the envelope and return the canonical request payload.

    The payload is what ``commit_command`` hashes: the same logical bundle must
    always produce the same payload, so a retry with the same ``request_id``
    replays its original receipt instead of colliding on a different digest.
    """

    ns = identifiers.require_namespace(namespace)
    if isinstance(bundle, ChangeBundle):
        bundle = {
            "operation": BUNDLE_OPERATION,
            "summary": bundle.summary,
            "changes": list(bundle.changes),
        }
    if not isinstance(bundle, Mapping):
        raise ChangeBundleInvalid("invalid_shape", "bundle must be a JSON object")

    unknown = sorted(str(key) for key in bundle if key not in _BUNDLE_KEYS)
    if unknown:
        raise ChangeBundleInvalid(
            "unknown_field", f"bundle has unknown field(s): {', '.join(unknown)}"
        )
    operation = bundle.get("operation", BUNDLE_OPERATION)
    if operation != BUNDLE_OPERATION:
        raise ChangeBundleInvalid(
            "invalid_shape", f"bundle operation must be {BUNDLE_OPERATION!r}"
        )

    summary = bundle.get("summary")
    if not isinstance(summary, str):
        raise ChangeBundleInvalid("invalid_shape", "summary must be a string")
    summary = summary.strip()
    if not summary or len(summary) > MAX_SUMMARY_CHARS:
        raise ChangeBundleInvalid(
            "invalid_shape",
            f"summary must be 1..{MAX_SUMMARY_CHARS} characters",
        )

    raw_changes = bundle.get("changes")
    if isinstance(raw_changes, (str, bytes)) or not isinstance(raw_changes, Sequence):
        raise ChangeBundleInvalid("invalid_shape", "changes must be an array")
    if not MIN_CHANGES <= len(raw_changes) <= MAX_CHANGES:
        raise ChangeBundleInvalid(
            "invalid_shape", f"changes must have {MIN_CHANGES}..{MAX_CHANGES} items"
        )

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_changes):
        normalized.append(_validate_one_change(ns, index, raw))

    # Beliefs first, then Questions, each in caller order.  The order is part
    # of the payload so the digest a retry recomputes is the order that will
    # actually be applied.
    ordered = [item for item in normalized if item["op"] in _BELIEF_OPS]
    ordered += [item for item in normalized if item["op"] in _QUESTION_OPS]

    seen: set[str] = set()
    for item in ordered:
        target = item.get("target")
        if target is None:
            continue
        record_id = target["record_id"]
        if record_id in seen:
            raise ChangeBundleInvalid(
                "duplicate_target",
                f"record {record_id} appears more than once in one bundle",
            )
        seen.add(record_id)

    payload = {"summary": summary, "changes": ordered}
    return ValidatedChange(
        namespace=ns,
        summary=summary,
        changes=tuple(ordered),
        request_payload=payload,
    )


def _validate_one_change(namespace: str, index: int, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ChangeBundleInvalid(
            "invalid_shape", f"changes[{index}] must be a JSON object"
        )
    op = raw.get("op")
    if op not in CHANGE_OPS:
        raise ChangeBundleInvalid(
            "unknown_op", f"changes[{index}].op must be one of {', '.join(CHANGE_OPS)}"
        )
    allowed = _CHANGE_KEYS[op]
    # ``close`` and ``resolve`` must not be able to impersonate each other's
    # evidence field: a close that carries resolution refs is a resolve claim
    # in disguise, and a resolve without them is not a resolution.
    if op == "close_question" and "resolution_refs" in raw:
        raise ChangeBundleInvalid(
            "resolution_not_supported",
            f"changes[{index}] is a close_question; it cannot carry resolution_refs",
        )
    if op == "resolve_question" and "closed_reason" in raw:
        raise ChangeBundleInvalid(
            "resolution_not_supported",
            f"changes[{index}] is a resolve_question; it cannot carry closed_reason",
        )
    if op == "reopen_question" and (
        "resolution_refs" in raw or "closed_reason" in raw
    ):
        raise ChangeBundleInvalid(
            "resolution_not_supported",
            f"changes[{index}] is a reopen_question; it carries new_evidence_refs only",
        )
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ChangeBundleInvalid(
            "unknown_field",
            f"changes[{index}] has unknown field(s): {', '.join(unknown)}",
        )
    missing = sorted(allowed - set(raw))
    if missing:
        raise ChangeBundleInvalid(
            "invalid_shape",
            f"changes[{index}] is missing field(s): {', '.join(missing)}",
        )

    if op == "propose_belief":
        return {
            "op": op,
            "body": _require_object(raw["body"], f"changes[{index}].body"),
            "scope": _require_scope(raw["scope"], f"changes[{index}].scope"),
        }
    if op == "revise_belief":
        return {
            "op": op,
            "target": _require_target(raw["target"], namespace, index),
            "body": _require_object(raw["body"], f"changes[{index}].body"),
            "scope": _require_scope(raw["scope"], f"changes[{index}].scope"),
        }
    if op == "retract_belief":
        evidence = raw["evidence_links"]
        if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
            raise ChangeBundleInvalid(
                "invalid_shape", f"changes[{index}].evidence_links must be an array"
            )
        reason = raw["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise ChangeBundleInvalid(
                "invalid_shape", f"changes[{index}].reason must be a non-empty string"
            )
        return {
            "op": op,
            "target": _require_target(raw["target"], namespace, index),
            "evidence_links": [dict(item) for item in evidence],
            "reason": reason,
        }
    if op == "resolve_question":
        refs = raw["resolution_refs"]
        if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence) or not refs:
            raise ChangeBundleInvalid(
                "invalid_shape",
                f"changes[{index}].resolution_refs must be a non-empty array",
            )
        reason = raw["resolution_reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise ChangeBundleInvalid(
                "invalid_shape",
                f"changes[{index}].resolution_reason must be a non-empty string",
            )
        return {
            "op": op,
            "target": _require_target(raw["target"], namespace, index),
            "resolution_refs": [dict(item) for item in refs],
            "resolution_reason": reason,
        }
    if op == "reopen_question":
        refs = raw["new_evidence_refs"]
        if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence) or not refs:
            raise ChangeBundleInvalid(
                "invalid_shape",
                f"changes[{index}].new_evidence_refs must be a non-empty array",
            )
        reason = raw["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise ChangeBundleInvalid(
                "invalid_shape", f"changes[{index}].reason must be a non-empty string"
            )
        return {
            "op": op,
            "target": _require_target(raw["target"], namespace, index),
            "new_evidence_refs": [dict(item) for item in refs],
            "reason": reason,
        }
    # close_question
    reason = raw["closed_reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ChangeBundleInvalid(
            "invalid_shape", f"changes[{index}].closed_reason must be a non-empty string"
        )
    return {
        "op": op,
        "target": _require_target(raw["target"], namespace, index),
        "closed_reason": reason,
    }


def _require_object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ChangeBundleInvalid("invalid_shape", f"{field_name} must be a JSON object")
    return dict(value)


def _require_scope(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ChangeBundleInvalid("invalid_shape", f"{field_name} must be a JSON object")
    return normalize_scope(dict(value), field=field_name)


def _require_target(value: Any, namespace: str, index: int) -> dict[str, Any]:
    try:
        ref = RecordRef.from_value(value, field=f"changes[{index}].target")
    except Exception as exc:  # noqa: BLE001 - a malformed ref is a structural refusal
        raise ChangeBundleInvalid(
            "invalid_shape", f"changes[{index}].target is not a record ref: {exc}"
        ) from exc
    if ref.namespace != namespace:
        raise ChangeBundleInvalid(
            "namespace_mismatch",
            f"changes[{index}].target is in {ref.namespace!r}, not {namespace!r}",
        )
    return ref.to_dict()


# ---------------------------------------------------------------------------
# phase 2: preparation
# ---------------------------------------------------------------------------


def prepare_change_set(
    plan: ValidatedChange,
    *,
    beliefs: BeliefService,
    questions: QuestionService,
    question_access: QuestionAccess | None = None,
    store: Any = None,
) -> PreparedChangeSet:
    """Validate every change against the store and freeze the write inputs.

    This never writes and never opens a transaction.  ``store`` defaults to the
    service's own store so callers only have to pass the two services; the
    explicit parameter exists so a caller that already holds one store cannot
    accidentally prepare against a different one.
    """

    if not isinstance(plan, ValidatedChange):
        raise ChangeBundleInvalid("invalid_shape", "plan must be a ValidatedChange")
    if store is None:
        store = getattr(beliefs, "_store", None)
    if store is None:  # pragma: no cover - defensive
        raise ChangeBundleInvalid(
            "invalid_shape", "a store is required to prepare a change set"
        )

    prepared_beliefs: list[PreparedBelief] = []
    prepared_questions: list[PreparedQuestion] = []
    needs_question_access = any(
        item["op"] in _QUESTION_OPS for item in plan.changes
    )
    if needs_question_access and question_access is None:
        raise ChangeBundleInvalid(
            "invalid_shape",
            "question_access is required for a bundle that carries a Question change",
        )

    for change in plan.changes:
        op = change["op"]
        if op == "propose_belief":
            scope = change["scope"]
            normalized_scope = normalize_scope(scope)
            validated = validate_belief_body(change["body"], scope=normalized_scope)
            prepared_beliefs.append(
                PreparedBelief(
                    op=op,
                    record_id=str(uuid.uuid4()),
                    target_ref=None,
                    validated=validated,
                    scope=normalized_scope,
                    retract=False,
                )
            )
        elif op == "revise_belief":
            target = RecordRef.from_value(change["target"], field="target")
            normalized_scope = normalize_scope(change["scope"])
            validated = validate_belief_body(change["body"], scope=normalized_scope)
            prepared_beliefs.append(
                PreparedBelief(
                    op=op,
                    record_id=target.record_id,
                    target_ref=target,
                    validated=validated,
                    scope=normalized_scope,
                    retract=False,
                )
            )
        elif op == "retract_belief":
            target = RecordRef.from_value(change["target"], field="target")
            current = store.get_current(plan.namespace, target.record_id)
            if current.record_version != target.record_version:
                # Same refusal a direct retract would raise, decided before the
                # transaction; the seam re-checks it inside.
                raise StateRevisionConflict(
                    f"belief {target.record_id} moved from version "
                    f"{target.record_version} to {current.record_version}; "
                    "retract the current version"
                )
            retraction_body = dict(parse_belief_body(current))
            retraction_body["evidence_links"] = [dict(item) for item in change["evidence_links"]]
            retraction_body["strength_reason"] = change["reason"]
            normalized_scope = normalize_scope(current.scope or {})
            validated = validate_belief_body(
                retraction_body, scope=normalized_scope, retract=True
            )
            prepared_beliefs.append(
                PreparedBelief(
                    op=op,
                    record_id=target.record_id,
                    target_ref=target,
                    validated=validated,
                    scope=normalized_scope,
                    retract=True,
                )
            )
        else:
            target = RecordRef.from_value(change["target"], field="target")
            if question_access is None:  # pragma: no cover - guarded above
                raise ChangeBundleInvalid(
                    "invalid_shape",
                    "question_access is required for a Question change",
                )
            if op == "resolve_question":
                prepared_transition = questions.prepare_transition(
                    question_access,
                    ref=target,
                    expected_version=target.record_version,
                    action="resolve",
                    reason=change["resolution_reason"],
                    resolution_refs=change["resolution_refs"],
                )
            elif op == "reopen_question":
                prepared_transition = questions.prepare_transition(
                    question_access,
                    ref=target,
                    expected_version=target.record_version,
                    action="reopen",
                    reason=change["reason"],
                    new_evidence_refs=change["new_evidence_refs"],
                )
            else:
                prepared_transition = questions.prepare_transition(
                    question_access,
                    ref=target,
                    expected_version=target.record_version,
                    action="close",
                    reason=change["closed_reason"],
                )
            prepared_questions.append(
                PreparedQuestion(
                    op=op,
                    ref=target,
                    expected_version=target.record_version,
                    new_body=prepared_transition.new_body,
                    new_status=prepared_transition.new_status,
                    privacy_class=prepared_transition.privacy_class,
                    scope=prepared_transition.scope,
                )
            )

    return PreparedChangeSet(
        beliefs=tuple(prepared_beliefs), questions=tuple(prepared_questions)
    )


# ---------------------------------------------------------------------------
# phase 3: in-transaction replay
# ---------------------------------------------------------------------------


class ChangeApplier:
    """Replays one prepared change set inside an already open ``TxScope``.

    The applier performs no validation of its own beyond matching the prepared
    set to the plan; each append goes through the domain seam, which re-checks
    versions, evidence, Scope, privacy and deletion inside the transaction.
    It never calls ``complete_work``: a composition bundle is not bound to a
    pending work item.
    """

    def __init__(
        self,
        store: Any,
        beliefs: BeliefService,
        questions: QuestionService,
    ) -> None:
        self._store = store
        self._beliefs = beliefs
        self._questions = questions

    def apply(
        self,
        scope: Any,
        plan: ValidatedChange,
        *,
        context_id: str,
        prepared: PreparedChangeSet,
    ) -> tuple[Any, ...]:
        """Append every prepared version, or raise and let the caller roll back."""

        if not isinstance(plan, ValidatedChange):
            raise ChangeBundleInvalid("invalid_shape", "plan must be a ValidatedChange")
        if not isinstance(prepared, PreparedChangeSet):
            raise ChangeBundleInvalid(
                "invalid_shape", "prepared must be a PreparedChangeSet"
            )
        if not isinstance(context_id, str) or not context_id:
            raise ChangeBundleInvalid("invalid_shape", "context_id is required")

        for item in prepared.beliefs:
            sink: list[Any] = []
            noop_sink: list[Any] = []
            if item.op == "propose_belief":
                self._beliefs._apply_propose_locked(
                    scope,
                    namespace=plan.namespace,
                    context_id=context_id,
                    record_id=item.record_id,
                    validated=item.validated,
                    normalized_scope=item.scope,
                    work_id=None,
                    result=sink,
                )
            else:
                if item.target_ref is None:
                    raise ChangeBundleInvalid(
                        "invalid_shape",
                        f"prepared {item.op} has no target ref",
                    )
                self._beliefs._apply_revise_locked(
                    scope,
                    namespace=plan.namespace,
                    context_id=context_id,
                    target_ref=item.target_ref,
                    validated=item.validated,
                    normalized_scope=item.scope,
                    retract=item.retract,
                    work_id=None,
                    result=sink,
                    noop_version=noop_sink,
                )

        for item in prepared.questions:
            self._questions._apply_transition_locked(
                scope,
                namespace=plan.namespace,
                record_id=item.ref.record_id,
                expected_version=item.expected_version,
                new_body=item.new_body,
                new_status=item.new_status,
                privacy_class=item.privacy_class,
                scope=item.scope,
            )

        return tuple(scope.appended)


__all__ = [
    "BUNDLE_OPERATION",
    "CHANGE_OPS",
    "ChangeApplier",
    "ChangeBundle",
    "ChangeBundleInvalid",
    "MAX_CHANGES",
    "MAX_SUMMARY_CHARS",
    "MIN_CHANGES",
    "PreparedBelief",
    "PreparedChangeSet",
    "PreparedQuestion",
    "ValidatedChange",
    "prepare_change_set",
    "validate_change_bundle",
]
