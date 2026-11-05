"""J05: persistent OpenQuestion and its trusted-host service.

The Question is a normal immutable PCS Record (``kind="cognition.open_question"``);
there is no second state store and no migration.  Writes go through the existing
Tx B ``commit_command`` path so request-id idempotency, snapshot watermarks and
receipts are the same as every other cognition command.

Only trusted host code constructs :class:`QuestionAccess`.  Model DTOs, bodies
and candidate JSON never grant a permission, scope or privacy class.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from ...foundation import identifiers
from ...foundation.errors import (
    CommandConflict,
    CorruptStore,
    InvalidInput,
    RecordNotFound,
    RecordVersionConflict,
    RecordVersionExists,
    Redacted,
    ScopeViolation,
    SourceRedacted,
    StateRevisionConflict,
    StorageError,
)
from ...foundation.jsonutil import canonical_json_text, sha256_hex
from ...foundation.timeutil import utc_now_rfc3339
from ..refs import EventRef, RecordRef
from ..scope import normalize_scope, scope_matches
from .errors import InvalidTransition

QUESTION_KIND = "cognition.open_question"
QUESTION_SCHEMA_VERSION = "1"
QUESTION_CONTRACT = "cognition.open_question.v1"

QUESTION_STATUSES = ("proposed", "open", "waiting", "resolved", "closed")
ACTIVE_STATUSES = frozenset(("proposed", "open", "waiting"))
ORIGINS = ("user_requested", "system_proposed")
PRIORITIES = ("low", "normal", "high")
EVALUATION_RESULTS = ("no_change", "evidence_reviewed", "needs_followup")
ACTIONS = (
    "accept",
    "wait",
    "pause",
    "resume",
    "wake",
    "resolve",
    "close",
    "reopen",
    "update_triggers",
    "link_related_question",
)

QUESTION_PERMISSIONS = frozenset(
    {
        "question.read",
        "question.create",
        "question.transition",
        "question.evaluate",
        "question.attention",
    }
)
QUESTION_PURPOSE = "cognition.question"

MAX_QUESTION_CHARS = 2000
MAX_WAITING_CHARS = 512
MAX_REASON_CHARS = 512
MAX_ORIGIN_REFS = 16
MAX_EVIDENCE_REFS = 32
MAX_RESOLUTION_REFS = 16
MAX_RELATED_BELIEFS = 16
MAX_RELATED_QUESTIONS = 8
MAX_TRIGGER_REFS = 16
MAX_TOPIC_KEYS = 16
MAX_TOPIC_KEY_CHARS = 64
MAX_CONSUMED_TRIGGERS = 16
MAX_LIST_LIMIT = 50
MAX_ATTENTION_SOURCES = 32
MAX_DEDUPE_PROBES = 10000

#: Fixed UUIDv5 namespace for deterministic Question identities.  It is a
#: constant, never supplied by a caller.
QUESTION_DEDUPE_NAMESPACE = uuid.UUID("5b0f44f1-5d2f-4f54-9f31-0f6f5f9d2a10")

_BODY_KEYS = frozenset(
    {
        "contract",
        "question",
        "origin",
        "origin_refs",
        "evidence_refs",
        "related_belief_refs",
        "resolution_refs",
        "resolution_reason",
        "reopen_reason",
        "priority",
        "waiting_for",
        "paused",
        "pause_reason",
        "paused_at",
        "paused_by",
        "last_evaluated_at",
        "last_evaluated_revision",
        "review_after",
        "trigger_spec",
        "consumed_trigger_refs",
        "closed_reason",
        "related_question_refs",
    }
)

_TIMESTAMP = re.compile(
    r"^(?:19|20)[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?Z$"
)


class QuestionError(StorageError):
    """Base for J05 domain errors."""


class QuestionInvalid(QuestionError, InvalidInput):
    """A structurally invalid Question body/argument."""


class QuestionUnavailable(QuestionError):
    """A current Question cannot be delivered as live material."""


class DuplicateQuestion(QuestionError):
    """An active Question already exists for the same dedupe identity."""

    def __init__(self, ref: RecordRef):
        super().__init__(f"active open question already exists: {ref.record_id}")
        self.ref = ref


class TriggerLimitReached(QuestionError):
    """The bounded consumed-trigger list is full."""


@dataclass(frozen=True)
class QuestionAccess:
    """Trusted-host authority for one Question call.

    The host constructs it; it is not an ACL and never comes from model JSON.
    ``permissions`` must stay inside :data:`QUESTION_PERMISSIONS`.
    """

    namespace: str
    host_instance: str
    access_ref: str
    privacy_classes: frozenset[str]
    permissions: frozenset[str]
    purposes: frozenset[str]

    def __post_init__(self) -> None:
        identifiers.require_namespace(self.namespace)
        _require_text(self.host_instance, "host_instance", 256)
        _require_text(self.access_ref, "access_ref", 512)
        if not isinstance(self.privacy_classes, frozenset):
            raise QuestionInvalid("privacy_classes must be a frozenset")
        for privacy in sorted(self.privacy_classes):
            identifiers.require_privacy_class(privacy)
        if not isinstance(self.permissions, frozenset) or not self.permissions <= QUESTION_PERMISSIONS:
            raise QuestionInvalid("permissions must be a subset of the question permissions")
        if not isinstance(self.purposes, frozenset) or QUESTION_PURPOSE not in self.purposes:
            raise QuestionInvalid("purposes must contain cognition.question")
        for purpose in sorted(self.purposes):
            _require_text(purpose, "purposes[]", 64)


@dataclass(frozen=True)
class QuestionView:
    """Read-only projection of one usable Question version."""

    ref: RecordRef
    status: str
    question: str
    origin: str
    priority: str
    paused: bool
    pause_reason: str | None
    waiting_for: str | None
    review_after: str | None
    last_evaluated_at: str | None
    last_evaluated_revision: int | None
    scope: dict[str, Any]
    recorded_at: str
    evidence_refs: tuple[dict[str, Any], ...]
    trigger_spec: dict[str, Any]
    consumed_trigger_refs: tuple[dict[str, Any], ...]
    resolution_refs: tuple[dict[str, Any], ...]
    related_question_refs: tuple[dict[str, Any], ...]
    origin_refs: tuple[dict[str, Any], ...] = ()
    resolution_reason: str | None = None
    reopen_reason: str | None = None


@dataclass(frozen=True)
class CreateResult:
    view: QuestionView
    deduped: bool
    receipt: Any | None


@dataclass(frozen=True)
class EvaluationReceipt:
    ref: RecordRef
    request_id: str
    evaluated_at: str
    evaluation_result: str
    previous_version: int
    new_version: int
    commit_receipt: Any


@dataclass(frozen=True)
class PreparedTransition:
    """The validated outcome of one Question transition, before it is written.

    ``new_body`` is the parsed/normalized body that would be appended, not the
    raw caller body.  Produced by :meth:`QuestionService.prepare_transition`
    and consumed by the single-transaction apply path.
    """

    new_body: dict[str, Any]
    new_status: str
    privacy_class: str
    scope: dict[str, Any] | None


@dataclass(frozen=True)
class PreparedCreate:
    """The validated outcome of one Question creation, before it is written.

    Produced by :meth:`QuestionService.prepare_create` and consumed by
    :meth:`QuestionService._apply_create_locked`, so a creation can join the
    same transaction as other cognition changes.  Every store-independent check
    (permission, text, scope, references, trigger spec) has already run.
    """

    namespace: str
    question: str
    origin: str
    scope: dict[str, Any]
    privacy_class: str
    priority: str
    origin_refs: list[dict[str, Any]]
    trigger_spec: dict[str, Any]
    related_question_refs: list[dict[str, Any]]
    review_after: str | None
    body: dict[str, Any]
    status: str
    dedupe_key: str


# ---------------------------------------------------------------------------
# small validators


def _require_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise QuestionInvalid(f"{field} must be a string")
    text = value.strip()
    if not text or len(text) > maximum:
        raise QuestionInvalid(f"{field} must be 1..{maximum} characters")
    return text


def _optional_text(value: Any, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _require_text(value, field, maximum)


def _require_timestamp(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise QuestionInvalid(f"{field} must be a UTC RFC 3339 timestamp ending in Z")
    return value


def _normalize_text(value: Any) -> str:
    text = _require_text(value, "question", MAX_QUESTION_CHARS)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise QuestionInvalid("question is empty after normalization")
    return text


def _dedupe_text(value: str) -> str:
    return value.casefold()


def _normalize_scope(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    return normalize_scope(value)


def _dedupe_key(namespace: str, scope: Mapping[str, Any], normalized_text: str) -> str:
    return sha256_hex(
        canonical_json_text(
            {
                "v": 1,
                "namespace": namespace,
                "scope": dict(scope),
                "text": _dedupe_text(normalized_text),
            }
        )
    )


def _ref_key(ref: Mapping[str, Any]) -> str:
    return canonical_json_text(dict(ref))


def _commit_digest(
    namespace: str,
    operation: str,
    context_id: str,
    payload: Mapping[str, Any],
) -> str:
    """Exactly the Tx B request digest, computed outside the store for replay."""

    return sha256_hex(
        canonical_json_text(
            {
                "context_id": context_id,
                "namespace": namespace,
                "operation": operation,
                "request_payload": dict(payload),
            }
        )
    )


def _create_payload(
    *,
    question: str,
    origin: str,
    scope: Mapping[str, Any],
    privacy_class: str,
    priority: str,
    origin_refs: Sequence[Mapping[str, Any]],
    trigger_spec: Mapping[str, Any],
    related_question_refs: Sequence[Mapping[str, Any]],
    review_after: str | None,
) -> dict[str, Any]:
    return {
        "question": question,
        "origin": origin,
        "scope": dict(scope),
        "privacy_class": privacy_class,
        "priority": priority,
        "origin_refs": list(origin_refs),
        "trigger_spec": dict(trigger_spec),
        "related_question_refs": list(related_question_refs),
        "review_after": review_after,
    }


def _transition_payload(
    *,
    action: str,
    record_id: str,
    expected_version: int,
    reason: str | None,
    waiting_for: str | None,
    resolution_refs: Sequence[Mapping[str, Any]],
    new_evidence_refs: Sequence[Mapping[str, Any]],
    review_after: str | None,
    pause_reason: str | None,
    paused_by: str | None,
    trigger_spec: Mapping[str, Any] | None,
    related_question_ref: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "action": action,
        "record_id": record_id,
        "expected_version": expected_version,
        "reason": reason,
        "waiting_for": waiting_for,
        "resolution_refs": list(resolution_refs),
        "new_evidence_refs": list(new_evidence_refs),
        "review_after": review_after,
        "pause_reason": pause_reason,
        "paused_by": paused_by,
        "trigger_spec": None if trigger_spec is None else dict(trigger_spec),
        "related_question_ref": (
            None if related_question_ref is None else dict(related_question_ref)
        ),
    }


def _evaluate_payload(
    *,
    record_id: str,
    expected_version: int,
    evaluation_result: str,
    reason: str,
    evaluated_source_refs: Sequence[Mapping[str, Any]],
    review_after: str | None,
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "expected_version": expected_version,
        "evaluation_result": evaluation_result,
        "reason": reason,
        "evaluated_source_refs": list(evaluated_source_refs),
        "review_after": review_after,
    }


def _consume_payload(
    *,
    record_id: str,
    expected_version: int,
    trigger_ref: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "expected_version": expected_version,
        "trigger_ref": dict(trigger_ref),
    }



def _validate_exact_refs(
    value: Any, field: str, maximum: int, namespace: str
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise QuestionInvalid(f"{field} must be an array of typed refs")
    if len(value) > maximum:
        raise QuestionInvalid(f"{field} must have at most {maximum} items")
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise QuestionInvalid(f"{field}[{index}] must be an object")
        kind = item.get("ref_type")
        if kind == "event":
            ref = EventRef.from_value(item, field=f"{field}[{index}]")
        elif kind == "record":
            ref = RecordRef.from_value(item, field=f"{field}[{index}]")
        else:
            raise QuestionInvalid(f'{field}[{index}].ref_type must be "event" or "record"')
        if ref.namespace != namespace:
            raise ScopeViolation(f"{field}[{index}] is outside the access namespace")
        wire = ref.to_dict()
        key = _ref_key(wire)
        if key in seen:
            raise QuestionInvalid(f"{field} contains a duplicate ref")
        seen.add(key)
        refs.append(wire)
    return refs


def _validate_record_refs(
    value: Any, field: str, maximum: int, namespace: str
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise QuestionInvalid(f"{field} must be an array of RecordRef objects")
    if len(value) > maximum:
        raise QuestionInvalid(f"{field} must have at most {maximum} items")
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        ref = RecordRef.from_value(item, field=f"{field}[{index}]")
        if ref.namespace != namespace:
            raise ScopeViolation(f"{field}[{index}] is outside the access namespace")
        wire = ref.to_dict()
        key = _ref_key(wire)
        if key in seen:
            raise QuestionInvalid(f"{field} contains a duplicate ref")
        seen.add(key)
        refs.append(wire)
    return refs


def _validate_trigger_spec(value: Any, namespace: str) -> dict[str, Any]:
    if value is None:
        return {"source_refs": [], "topic_keys": []}
    if not isinstance(value, Mapping) or set(value) != {"source_refs", "topic_keys"}:
        raise QuestionInvalid("trigger_spec must contain exactly source_refs and topic_keys")
    source_refs = _validate_exact_refs(
        value["source_refs"], "trigger_spec.source_refs", MAX_TRIGGER_REFS, namespace
    )
    raw_keys = value["topic_keys"]
    if isinstance(raw_keys, (str, bytes)) or not isinstance(raw_keys, Sequence):
        raise QuestionInvalid("trigger_spec.topic_keys must be an array of strings")
    if len(raw_keys) > MAX_TOPIC_KEYS:
        raise QuestionInvalid(f"trigger_spec.topic_keys must have at most {MAX_TOPIC_KEYS} items")
    keys: list[str] = []
    seen: set[str] = set()
    for index, key in enumerate(raw_keys):
        text = _require_text(key, f"trigger_spec.topic_keys[{index}]", MAX_TOPIC_KEY_CHARS)
        if text in seen:
            raise QuestionInvalid("trigger_spec.topic_keys contains a duplicate")
        seen.add(text)
        keys.append(text)
    return {"source_refs": source_refs, "topic_keys": keys}


def _parse_body(
    body: Mapping[str, Any],
    *,
    status: str,
    namespace: str,
    record_version: int,
) -> dict[str, Any]:
    if not isinstance(body, Mapping) or set(body) != _BODY_KEYS:
        raise QuestionInvalid("question body has unexpected or missing fields")
    if body["contract"] != QUESTION_CONTRACT:
        raise QuestionInvalid("unsupported question contract")
    if status not in QUESTION_STATUSES:
        raise QuestionInvalid(f"unknown question status {status!r}")
    question = _require_text(body["question"], "question", MAX_QUESTION_CHARS)
    if _normalize_text(question) != question:
        raise QuestionInvalid("question must be stored in normalized form")
    origin = body["origin"]
    if origin not in ORIGINS:
        raise QuestionInvalid("origin is not a question origin")
    origin_refs = _validate_exact_refs(body["origin_refs"], "origin_refs", MAX_ORIGIN_REFS, namespace)
    evidence_refs = _validate_exact_refs(
        body["evidence_refs"], "evidence_refs", MAX_EVIDENCE_REFS, namespace
    )
    related_beliefs = _validate_record_refs(
        body["related_belief_refs"], "related_belief_refs", MAX_RELATED_BELIEFS, namespace
    )
    resolution_refs = _validate_exact_refs(
        body["resolution_refs"], "resolution_refs", MAX_RESOLUTION_REFS, namespace
    )
    resolution_reason = _optional_text(
        body["resolution_reason"], "resolution_reason", MAX_REASON_CHARS
    )
    reopen_reason = _optional_text(
        body["reopen_reason"], "reopen_reason", MAX_REASON_CHARS
    )
    priority = body["priority"]
    if priority not in PRIORITIES:
        raise QuestionInvalid("priority must be low, normal or high")
    waiting_for = _optional_text(body["waiting_for"], "waiting_for", MAX_WAITING_CHARS)
    paused = body["paused"]
    if type(paused) is not bool:
        raise QuestionInvalid("paused must be a boolean")
    pause_reason = _optional_text(body["pause_reason"], "pause_reason", MAX_REASON_CHARS)
    paused_at = _require_timestamp(body["paused_at"], "paused_at", optional=True)
    paused_by = body["paused_by"]
    if paused_by is not None and paused_by not in ("user", "host"):
        raise QuestionInvalid("paused_by must be user, host or null")
    last_evaluated_at = _require_timestamp(
        body["last_evaluated_at"], "last_evaluated_at", optional=True
    )
    last_evaluated_revision = body["last_evaluated_revision"]
    if last_evaluated_revision is not None:
        if type(last_evaluated_revision) is not int or last_evaluated_revision < 1:
            raise QuestionInvalid("last_evaluated_revision must be a positive integer or null")
        if last_evaluated_revision > record_version:
            raise QuestionInvalid("last_evaluated_revision cannot be newer than the record version")
    review_after = _require_timestamp(body["review_after"], "review_after", optional=True)
    trigger_spec = _validate_trigger_spec(body["trigger_spec"], namespace)
    consumed = _validate_exact_refs(
        body["consumed_trigger_refs"],
        "consumed_trigger_refs",
        MAX_CONSUMED_TRIGGERS,
        namespace,
    )
    closed_reason = _optional_text(body["closed_reason"], "closed_reason", MAX_REASON_CHARS)
    related_questions = _validate_record_refs(
        body["related_question_refs"],
        "related_question_refs",
        MAX_RELATED_QUESTIONS,
        namespace,
    )

    if status == "waiting":
        if waiting_for is None:
            raise QuestionInvalid("waiting questions require waiting_for")
    else:
        if waiting_for is not None:
            raise QuestionInvalid("waiting_for is only valid while waiting")
    if status == "resolved":
        if not resolution_refs:
            raise QuestionInvalid("resolved questions require resolution_refs")
        if resolution_reason is None:
            raise QuestionInvalid("resolved questions require resolution_reason")
        if paused or pause_reason is not None or paused_at is not None or paused_by is not None:
            raise QuestionInvalid("resolved questions cannot be paused")
    else:
        if resolution_refs:
            raise QuestionInvalid("resolution_refs are only valid while resolved")
        if resolution_reason is not None:
            raise QuestionInvalid("resolution_reason is only valid while resolved")
    if status == "proposed" and reopen_reason is not None:
        raise QuestionInvalid("reopen_reason is not valid on a proposed question")
    if status == "closed":
        if closed_reason is None:
            raise QuestionInvalid("closed questions require closed_reason")
        if paused:
            raise QuestionInvalid("closed questions cannot be paused")
    elif closed_reason is not None:
        raise QuestionInvalid("closed_reason is only valid while closed")
    if paused:
        if pause_reason is None or paused_at is None or paused_by is None:
            raise QuestionInvalid("paused questions require reason, time and actor")
    elif any(item is not None for item in (pause_reason, paused_at, paused_by)):
        raise QuestionInvalid("pause fields require paused=true")
    if status in ("proposed", "open") and origin_refs == [] and origin == "user_requested":
        raise QuestionInvalid("user-requested questions require origin_refs")
    if status == "open" and not origin_refs:
        raise QuestionInvalid("open questions require origin_refs")

    return {
        "contract": QUESTION_CONTRACT,
        "question": question,
        "origin": origin,
        "origin_refs": origin_refs,
        "evidence_refs": evidence_refs,
        "related_belief_refs": related_beliefs,
        "resolution_refs": resolution_refs,
        "resolution_reason": resolution_reason,
        "reopen_reason": reopen_reason,
        "priority": priority,
        "waiting_for": waiting_for,
        "paused": paused,
        "pause_reason": pause_reason,
        "paused_at": paused_at,
        "paused_by": paused_by,
        "last_evaluated_at": last_evaluated_at,
        "last_evaluated_revision": last_evaluated_revision,
        "review_after": review_after,
        "trigger_spec": trigger_spec,
        "consumed_trigger_refs": consumed,
        "closed_reason": closed_reason,
        "related_question_refs": related_questions,
    }


def _base_body(
    *,
    normalized_question: str,
    origin: str,
    origin_refs: list[dict[str, Any]],
    priority: str,
    trigger_spec: dict[str, Any],
    related_questions: list[dict[str, Any]],
    review_after: str | None,
) -> dict[str, Any]:
    return {
        "contract": QUESTION_CONTRACT,
        "question": normalized_question,
        "origin": origin,
        "origin_refs": origin_refs,
        "evidence_refs": [],
        "related_belief_refs": [],
        "resolution_refs": [],
        "resolution_reason": None,
        "reopen_reason": None,
        "priority": priority,
        "waiting_for": None,
        "paused": False,
        "pause_reason": None,
        "paused_at": None,
        "paused_by": None,
        "last_evaluated_at": None,
        "last_evaluated_revision": None,
        "review_after": review_after,
        "trigger_spec": trigger_spec,
        "consumed_trigger_refs": [],
        "closed_reason": None,
        "related_question_refs": related_questions,
    }


# ---------------------------------------------------------------------------
# source gate


@dataclass(frozen=True)
class _RefDetail:
    ref_type: str
    privacy_class: str
    scope: dict[str, Any]
    kind: str
    record: Any | None = None


def _resolve_ref(store: Any, namespace: str, ref: Mapping[str, Any]) -> _RefDetail | None:
    if ref["ref_type"] == "event":
        event_id = ref["event_id"]
        if store.event_is_deleted(namespace, event_id):
            return None
        header = store.event_header(namespace, event_id)
        if header is None:
            return None
        projection_gate = getattr(store, "projection_is_usable", None)
        if projection_gate is not None and not projection_gate(namespace, event_id):
            return None
        return _RefDetail("event", header.privacy_class, {}, "event")
    record_id = ref["record_id"]
    record_version = int(ref["record_version"])
    if store.record_is_deleted(namespace, record_id):
        return None
    try:
        version = store.get_version(namespace, record_id, record_version)
    except RecordNotFound:
        return None
    return _RefDetail(
        "record",
        version.privacy_class,
        dict(version.scope or {}),
        version.kind,
        version,
    )


def _check_refs(
    store: Any,
    access: QuestionAccess,
    body: Mapping[str, Any],
    *,
    privacy_class: str,
    scope: Mapping[str, Any],
    write: bool,
) -> list[str]:
    reasons: list[str] = []
    groups: list[tuple[str, Iterable[Mapping[str, Any]], str | None]] = [
        ("origin_refs", body["origin_refs"], None),
        ("evidence_refs", body["evidence_refs"], None),
        ("resolution_refs", body["resolution_refs"], None),
        ("trigger_spec.source_refs", body["trigger_spec"]["source_refs"], None),
        ("consumed_trigger_refs", body["consumed_trigger_refs"], None),
        ("related_belief_refs", body["related_belief_refs"], "belief"),
        ("related_question_refs", body["related_question_refs"], QUESTION_KIND),
    ]
    for field, refs, required_kind in groups:
        for ref in refs:
            detail = _resolve_ref(store, access.namespace, ref)
            if detail is None:
                reasons.append(f"{field}:missing_or_deleted")
                continue
            if detail.privacy_class != privacy_class:
                reasons.append(f"{field}:privacy_mismatch")
                continue
            if detail.privacy_class not in access.privacy_classes:
                reasons.append(f"{field}:privacy_not_authorized")
                continue
            if required_kind is not None and detail.kind != required_kind:
                reasons.append(f"{field}:kind_mismatch")
                continue
            if detail.scope and not scope_matches(detail.scope, scope):
                reasons.append(f"{field}:scope_mismatch")
    if write and reasons:
        if any(reason.endswith(":missing_or_deleted") for reason in reasons):
            raise SourceRedacted(f"question source unavailable: {reasons[0]}")
        raise QuestionInvalid(f"question source rejected: {reasons[0]}")
    return reasons


# ---------------------------------------------------------------------------
# service


class QuestionService:
    """Trusted-host service over one :class:`CognitionStore`."""

    def __init__(self, store: Any):
        self._store = store

    # ------------------------------------------------------------------ reads

    def get(
        self,
        access: QuestionAccess,
        *,
        ref: RecordRef,
        scope: Mapping[str, Any] | None = None,
    ) -> QuestionView:
        _require_permission(access, "question.read")
        return self._read_view(access, ref=ref, scope=scope)

    def _read_view(
        self,
        access: QuestionAccess,
        *,
        ref: RecordRef,
        scope: Mapping[str, Any] | None = None,
    ) -> QuestionView:
        query_scope = _normalize_scope(scope)
        if not isinstance(ref, RecordRef):
            raise QuestionInvalid("ref must be a RecordRef")
        if ref.namespace != access.namespace:
            raise ScopeViolation("question ref is outside the access namespace")
        pointer = self._store.record_pointer(ref.namespace, ref.record_id)
        if pointer is None:
            raise QuestionUnavailable("question does not exist")
        current = pointer.current_version == ref.record_version
        unavailable = QuestionUnavailable if current else Redacted
        if pointer.kind != QUESTION_KIND:
            raise unavailable("record is not an open question")
        if self._store.record_is_deleted(ref.namespace, ref.record_id):
            raise unavailable("question has been deleted")
        version = self._store.get_version(ref.namespace, ref.record_id, ref.record_version)
        if version.kind != QUESTION_KIND:
            raise unavailable("record is not an open question")
        if version.privacy_class not in access.privacy_classes:
            raise unavailable("question is not authorized")
        if not scope_matches(version.scope or {}, query_scope):
            raise unavailable("question is outside the query scope")
        body = _parse_body(
            version.body,
            status=version.status,
            namespace=ref.namespace,
            record_version=ref.record_version,
        )
        reasons = _check_refs(
            self._store,
            access,
            body,
            privacy_class=version.privacy_class,
            scope=version.scope or {},
            write=False,
        )
        if reasons:
            raise unavailable("question source unavailable")
        return _view_from_version(version, body)

    def list_current(
        self,
        access: QuestionAccess,
        *,
        scope: Mapping[str, Any] | None = None,
        statuses: Sequence[str] = ("open", "waiting"),
        limit: int = MAX_LIST_LIMIT,
    ) -> tuple[QuestionView, ...]:
        _require_permission(access, "question.read")
        return self._collect_current(access, scope=scope, statuses=statuses, limit=limit)

    def _collect_current(
        self,
        access: QuestionAccess,
        *,
        scope: Mapping[str, Any] | None,
        statuses: Sequence[str],
        limit: int | None,
    ) -> tuple[QuestionView, ...]:
        query_scope = _normalize_scope(scope)
        if not isinstance(statuses, Sequence) or isinstance(statuses, (str, bytes)):
            raise QuestionInvalid("statuses must be a sequence")
        chosen = tuple(statuses)
        if not chosen or any(item not in QUESTION_STATUSES for item in chosen):
            raise QuestionInvalid("statuses contains an unknown question status")
        if limit is not None and (
            type(limit) is not int or limit < 1 or limit > MAX_LIST_LIMIT
        ):
            raise QuestionInvalid(f"limit must be 1..{MAX_LIST_LIMIT}")
        result: list[QuestionView] = []
        for version in self._store.current_versions_of_kind(
            access.namespace, QUESTION_KIND
        ):
            if version.status not in chosen:
                continue
            if version.privacy_class not in access.privacy_classes:
                continue
            if not scope_matches(version.scope or {}, query_scope):
                continue
            try:
                body = _parse_body(
                    version.body,
                    status=version.status,
                    namespace=access.namespace,
                    record_version=version.record_version,
                )
            except QuestionInvalid:
                continue
            if _check_refs(
                self._store,
                access,
                body,
                privacy_class=version.privacy_class,
                scope=version.scope or {},
                write=False,
            ):
                continue
            result.append(_view_from_version(version, body))
            if limit is not None and len(result) >= limit:
                break
        return tuple(result)

    def attention(
        self, access: QuestionAccess, *, request: Any
    ) -> Any:
        _require_permission(access, "question.attention")
        from .state_view import AttentionRequest, build_attention

        if not isinstance(request, AttentionRequest):
            raise QuestionInvalid("request must be an AttentionRequest")
        scope = _normalize_scope(request.scope)
        new_sources = _validate_exact_refs(
            tuple(item.to_dict() if isinstance(item, (EventRef, RecordRef)) else item for item in request.new_source_refs),
            "new_source_refs",
            MAX_ATTENTION_SOURCES,
            access.namespace,
        )
        live_sources: list[dict[str, Any]] = []
        for ref in new_sources:
            detail = _resolve_ref(self._store, access.namespace, ref)
            if detail is None:
                continue
            if detail.privacy_class not in access.privacy_classes:
                continue
            if detail.scope and not scope_matches(detail.scope, scope):
                continue
            live_sources.append(ref)
        views = self._collect_current(
            access,
            scope=scope,
            statuses=("open", "waiting"),
            limit=None,
        )
        return build_attention(
            request,
            views,
            now=utc_now_rfc3339(),
            new_source_refs=tuple(live_sources),
            state_revision=self._store.current_state_revision(access.namespace),
        )

    # ----------------------------------------------------------------- create

    def create(
        self,
        access: QuestionAccess,
        *,
        context_id: str,
        request_id: str,
        question: str,
        origin: str,
        scope: Mapping[str, Any] | None = None,
        origin_refs: Sequence[Mapping[str, Any]] = (),
        privacy_class: str,
        priority: str = "normal",
        trigger_spec: Mapping[str, Any] | None = None,
        related_question_refs: Sequence[Mapping[str, Any]] = (),
        review_after: str | None = None,
    ) -> CreateResult:
        prepared = self.prepare_create(
            access,
            question=question,
            origin=origin,
            scope=scope,
            origin_refs=origin_refs,
            privacy_class=privacy_class,
            priority=priority,
            trigger_spec=trigger_spec,
            related_question_refs=related_question_refs,
            review_after=review_after,
        )
        context_id = identifiers.require_context_id(context_id)
        request_id = identifiers.require_request_id(request_id)
        normalized = prepared.question
        scope_norm = prepared.scope
        refs = prepared.origin_refs
        related = prepared.related_question_refs
        triggers = prepared.trigger_spec
        review = prepared.review_after
        key = prepared.dedupe_key

        def apply(tx_scope: Any) -> None:
            self._apply_create_locked(tx_scope, prepared=prepared)

        try:
            receipt = self._store.commit_command(
                context_id=context_id,
                request_id=request_id,
                operation="question.create",
                request_payload=_create_payload(
                    question=normalized,
                    origin=origin,
                    scope=scope_norm,
                    privacy_class=privacy_class,
                    priority=priority,
                    origin_refs=refs,
                    trigger_spec=triggers,
                    related_question_refs=related,
                    review_after=review,
                ),
                apply=apply,
                namespace=access.namespace,
            )
        except DuplicateQuestion as exc:
            return CreateResult(self._read_existing(access, exc.ref, scope_norm), True, None)
        except StateRevisionConflict:
            existing = self._find_active(access, scope_norm, key, privacy_class)
            if existing is None:
                raise
            return CreateResult(existing, True, None)
        except (RecordVersionExists, RecordVersionConflict):
            existing = self._find_active(access, scope_norm, key, privacy_class)
            if existing is None:
                raise
            return CreateResult(existing, True, None)

        record_ref = receipt.record_refs[0]
        ref = RecordRef(access.namespace, record_ref.record_id, record_ref.record_version)
        return CreateResult(self._read_view(access, ref=ref, scope=scope_norm), False, receipt)

    def prepare_create(
        self,
        access: QuestionAccess,
        *,
        question: str,
        origin: str,
        scope: Mapping[str, Any] | None = None,
        origin_refs: Sequence[Mapping[str, Any]] = (),
        privacy_class: str,
        priority: str = "normal",
        trigger_spec: Mapping[str, Any] | None = None,
        related_question_refs: Sequence[Mapping[str, Any]] = (),
        review_after: str | None = None,
    ) -> PreparedCreate:
        """Validate one creation and compute exactly what would be written.

        The pre-transaction half of :meth:`create`, moved here verbatim so one
        combination commit can create a Question in the same transaction as
        other cognition changes.  It never writes and never opens a transaction.
        """

        _require_permission(access, "question.create")
        normalized = _normalize_text(question)
        scope_norm = _normalize_scope(scope)
        if origin not in ORIGINS:
            raise QuestionInvalid("origin must be user_requested or system_proposed")
        identifiers.require_privacy_class(privacy_class)
        if privacy_class not in access.privacy_classes:
            raise QuestionInvalid("question privacy_class is not authorized")
        if priority not in PRIORITIES:
            raise QuestionInvalid("priority must be low, normal or high")
        refs = _validate_exact_refs(origin_refs, "origin_refs", MAX_ORIGIN_REFS, access.namespace)
        related = _validate_record_refs(
            related_question_refs,
            "related_question_refs",
            MAX_RELATED_QUESTIONS,
            access.namespace,
        )
        triggers = _validate_trigger_spec(trigger_spec, access.namespace)
        review = _require_timestamp(review_after, "review_after", optional=True)
        body = _base_body(
            normalized_question=normalized,
            origin=origin,
            origin_refs=refs,
            priority=priority,
            trigger_spec=triggers,
            related_questions=related,
            review_after=review,
        )
        status = "open" if origin == "user_requested" else "proposed"
        _check_refs(
            self._store,
            access,
            _parse_body(body, status=status, namespace=access.namespace, record_version=1),
            privacy_class=privacy_class,
            scope=scope_norm,
            write=True,
        )
        key = _dedupe_key(access.namespace, scope_norm, normalized)
        return PreparedCreate(
            namespace=access.namespace,
            question=normalized,
            origin=origin,
            scope=scope_norm,
            privacy_class=privacy_class,
            priority=priority,
            origin_refs=refs,
            trigger_spec=triggers,
            related_question_refs=related,
            review_after=review,
            body=body,
            status=status,
            dedupe_key=key,
        )

    def _apply_create_locked(
        self,
        tx_scope: Any,
        *,
        prepared: PreparedCreate,
    ) -> RecordRef:
        """Append one new Question inside an already open TxScope.

        This is the transaction body of :meth:`create`, moved here verbatim.
        It raises :class:`DuplicateQuestion` when an active question already
        owns the dedupe identity; the caller decides whether that is a de-dupe
        or an aborted transaction.
        """

        namespace = prepared.namespace
        key = prepared.dedupe_key
        body = prepared.body
        matches = self._matching_current(namespace, prepared.scope, key)
        active_matches = [item for item in matches if item[1] in ACTIVE_STATUSES]
        if active_matches:
            if any(item[2] != prepared.privacy_class for item in active_matches):
                raise QuestionInvalid("dedupe privacy class mismatch")
            raise DuplicateQuestion(active_matches[0][0])
        old = next(
            (
                item[0]
                for item in matches
                if item[1] in ("resolved", "closed")
                and item[2] == prepared.privacy_class
            ),
            None,
        )
        related_now = list(body["related_question_refs"])
        if old is not None and _ref_key(old.to_dict()) not in {
            _ref_key(item) for item in related_now
        }:
            if len(related_now) >= MAX_RELATED_QUESTIONS:
                raise QuestionInvalid("related_question_refs is full")
            related_now.append(old.to_dict())
        body_now = dict(body)
        body_now["related_question_refs"] = related_now
        record_id = self._next_record_id(namespace, key)
        tx_scope.append_record_version(
            record_id=record_id,
            record_version=1,
            kind=QUESTION_KIND,
            body=body_now,
            status=prepared.status,
            schema_version=QUESTION_SCHEMA_VERSION,
            privacy_class=prepared.privacy_class,
            scope=prepared.scope,
        )
        return RecordRef(namespace, record_id, 1)

    # -------------------------------------------------------------- transition

    def transition(
        self,
        access: QuestionAccess,
        *,
        context_id: str,
        request_id: str,
        ref: RecordRef,
        expected_version: int,
        action: str,
        reason: str | None = None,
        waiting_for: str | None = None,
        resolution_refs: Sequence[Mapping[str, Any]] = (),
        new_evidence_refs: Sequence[Mapping[str, Any]] = (),
        review_after: str | None = None,
        pause_reason: str | None = None,
        paused_by: str | None = None,
        trigger_spec: Mapping[str, Any] | None = None,
        related_question_ref: Mapping[str, Any] | None = None,
    ) -> QuestionView:
        _require_permission(access, "question.transition")
        context_id = identifiers.require_context_id(context_id)
        request_id = identifiers.require_request_id(request_id)
        if action not in ACTIONS:
            raise QuestionInvalid(f"unknown question action {action!r}")
        if not isinstance(ref, RecordRef) or ref.namespace != access.namespace:
            raise ScopeViolation("question ref is outside the access namespace")
        if type(expected_version) is not int or expected_version < 1:
            raise QuestionInvalid("expected_version must be a positive integer")
        payload = _transition_payload(
            action=action,
            record_id=ref.record_id,
            expected_version=expected_version,
            reason=reason,
            waiting_for=waiting_for,
            resolution_refs=resolution_refs,
            new_evidence_refs=new_evidence_refs,
            review_after=review_after,
            pause_reason=pause_reason,
            paused_by=paused_by,
            trigger_spec=trigger_spec,
            related_question_ref=related_question_ref,
        )
        receipt = self._recover_committed(
            namespace=access.namespace,
            context_id=context_id,
            request_id=request_id,
            operation=f"question.{action}",
            payload=payload,
        )
        if receipt is not None:
            return self._view_from_receipt(access, receipt)
        prepared = self.prepare_transition(
            access,
            ref=ref,
            expected_version=expected_version,
            action=action,
            reason=reason,
            waiting_for=waiting_for,
            resolution_refs=resolution_refs,
            new_evidence_refs=new_evidence_refs,
            review_after=review_after,
            pause_reason=pause_reason,
            paused_by=paused_by,
            trigger_spec=trigger_spec,
            related_question_ref=related_question_ref,
        )

        def apply(tx_scope: Any) -> None:
            self._apply_transition_locked(
                tx_scope,
                namespace=access.namespace,
                record_id=ref.record_id,
                expected_version=expected_version,
                new_body=prepared.new_body,
                new_status=prepared.new_status,
                privacy_class=prepared.privacy_class,
                scope=prepared.scope,
            )

        self._store.commit_command(
            context_id=context_id,
            request_id=request_id,
            operation=f"question.{action}",
            request_payload=payload,
            apply=apply,
            namespace=access.namespace,
        )
        return self._read_view(
            access,
            ref=RecordRef(access.namespace, ref.record_id, expected_version + 1),
            scope=prepared.scope,
        )

    def prepare_transition(
        self,
        access: QuestionAccess,
        *,
        ref: RecordRef,
        expected_version: int,
        action: str,
        reason: str | None = None,
        waiting_for: str | None = None,
        resolution_refs: Sequence[Mapping[str, Any]] = (),
        new_evidence_refs: Sequence[Mapping[str, Any]] = (),
        review_after: str | None = None,
        pause_reason: str | None = None,
        paused_by: str | None = None,
        trigger_spec: Mapping[str, Any] | None = None,
        related_question_ref: Mapping[str, Any] | None = None,
    ) -> PreparedTransition:
        """Validate one transition and compute what it would write.

        This is the pre-transaction half of :meth:`transition`, moved here
        verbatim so one combination commit can validate a Question transition
        in the same transaction that appends the Belief versions.  It never
        writes, never calls ``commit_command`` and never opens a transaction.
        """

        _require_permission(access, "question.transition")
        if action not in ACTIONS:
            raise QuestionInvalid(f"unknown question action {action!r}")
        if not isinstance(ref, RecordRef) or ref.namespace != access.namespace:
            raise ScopeViolation("question ref is outside the access namespace")
        if type(expected_version) is not int or expected_version < 1:
            raise QuestionInvalid("expected_version must be a positive integer")
        self._require_expected(access, ref, expected_version)
        current_version = self._store.get_version(access.namespace, ref.record_id, ref.record_version)
        if current_version.privacy_class not in access.privacy_classes:
            raise QuestionUnavailable("question is not authorized")
        body = _parse_body(
            current_version.body,
            status=current_version.status,
            namespace=access.namespace,
            record_version=current_version.record_version,
        )
        status = current_version.status

        supplied = {
            "reason": reason,
            "waiting_for": waiting_for,
            "resolution_refs": tuple(resolution_refs),
            "new_evidence_refs": tuple(new_evidence_refs),
            "review_after": review_after,
            "pause_reason": pause_reason,
            "paused_by": paused_by,
            "trigger_spec": trigger_spec,
            "related_question_ref": related_question_ref,
        }
        _validate_action_fields(action, supplied)

        new_status = status
        new_body = dict(body)
        if action == "accept":
            if status != "proposed":
                raise InvalidTransition("accept requires proposed")
            if not body["origin_refs"]:
                raise QuestionInvalid("accept requires live origin_refs")
            new_status = "open"
        elif action == "wait":
            if status != "open":
                raise InvalidTransition("wait requires open")
            new_body["waiting_for"] = _require_text(waiting_for, "waiting_for", MAX_WAITING_CHARS)
            new_body["review_after"] = _require_timestamp(review_after, "review_after", optional=True)
            new_status = "waiting"
        elif action == "pause":
            if status != "waiting" or body["paused"]:
                raise InvalidTransition("pause requires unpaused waiting")
            if paused_by not in ("user", "host"):
                raise QuestionInvalid("paused_by must be user or host")
            new_body["paused"] = True
            new_body["pause_reason"] = _require_text(pause_reason, "pause_reason", MAX_REASON_CHARS)
            new_body["paused_at"] = utc_now_rfc3339()
            new_body["paused_by"] = paused_by
        elif action == "resume":
            if status != "waiting" or not body["paused"]:
                raise InvalidTransition("resume requires paused waiting")
            new_body["paused"] = False
            new_body["pause_reason"] = None
            new_body["paused_at"] = None
            new_body["paused_by"] = None
            new_body["waiting_for"] = None
            new_status = "open"
        elif action == "wake":
            if status != "waiting" or body["paused"]:
                raise InvalidTransition("wake requires unpaused waiting")
            if not tuple(new_evidence_refs):
                raise QuestionInvalid("wake requires new_evidence_refs")
            _require_text(reason, "reason", MAX_REASON_CHARS)
            evidence = self._merge_refs(
                body["evidence_refs"],
                _validate_exact_refs(new_evidence_refs, "new_evidence_refs", MAX_EVIDENCE_REFS, access.namespace),
                MAX_EVIDENCE_REFS,
            )
            new_body["evidence_refs"] = evidence
            new_body["waiting_for"] = None
            new_status = "open"
        elif action == "resolve":
            if status not in ("open", "waiting"):
                raise InvalidTransition("resolve requires open or waiting")
            if body["paused"]:
                raise InvalidTransition("resolve requires an unpaused question")
            resolution = _validate_exact_refs(
                resolution_refs, "resolution_refs", MAX_RESOLUTION_REFS, access.namespace
            )
            if not resolution:
                raise QuestionInvalid("resolve requires resolution_refs")
            _require_text(reason, "reason", MAX_REASON_CHARS)
            self._check_write_refs(access, resolution, current_version.privacy_class, current_version.scope or {})
            new_body["resolution_refs"] = resolution
            new_body["resolution_reason"] = _require_text(
                reason, "reason", MAX_REASON_CHARS
            )
            new_body["review_after"] = _require_timestamp(
                review_after, "review_after", optional=True
            )
            new_body["waiting_for"] = None
            new_body["paused"] = False
            new_body["pause_reason"] = None
            new_body["paused_at"] = None
            new_body["paused_by"] = None
            new_status = "resolved"
        elif action == "close":
            if status not in ("proposed", "open", "waiting"):
                raise InvalidTransition("close requires proposed, open or waiting")
            if body["paused"]:
                raise InvalidTransition("close requires an unpaused question")
            new_body["closed_reason"] = _require_text(reason, "reason", MAX_REASON_CHARS)
            new_body["waiting_for"] = None
            new_body["paused"] = False
            new_body["pause_reason"] = None
            new_body["paused_at"] = None
            new_body["paused_by"] = None
            new_status = "closed"
        elif action == "reopen":
            if status != "resolved":
                raise InvalidTransition("reopen requires resolved")
            if not tuple(new_evidence_refs):
                raise QuestionInvalid("reopen requires new_evidence_refs")
            _require_text(reason, "reason", MAX_REASON_CHARS)
            evidence = self._merge_refs(
                body["evidence_refs"],
                _validate_exact_refs(new_evidence_refs, "new_evidence_refs", MAX_EVIDENCE_REFS, access.namespace),
                MAX_EVIDENCE_REFS,
            )
            new_body["evidence_refs"] = evidence
            new_body["resolution_refs"] = []
            new_body["resolution_reason"] = None
            new_body["reopen_reason"] = _require_text(
                reason, "reason", MAX_REASON_CHARS
            )
            new_status = "open"
        elif action == "update_triggers":
            if status not in ("proposed", "open", "waiting"):
                raise InvalidTransition("update_triggers requires active status")
            if trigger_spec is None:
                raise QuestionInvalid("update_triggers requires trigger_spec")
            new_body["trigger_spec"] = _validate_trigger_spec(trigger_spec, access.namespace)
        elif action == "link_related_question":
            if status not in ("proposed", "open", "waiting"):
                raise InvalidTransition("link_related_question requires active status")
            if related_question_ref is None:
                raise QuestionInvalid("link_related_question requires related_question_ref")
            related = RecordRef.from_value(related_question_ref, field="related_question_ref")
            if related.namespace != access.namespace:
                raise ScopeViolation("related question is outside the namespace")
            if related.record_id == ref.record_id:
                raise QuestionInvalid("a question cannot relate to itself")
            detail = _resolve_ref(self._store, access.namespace, related.to_dict())
            if detail is None or detail.kind != QUESTION_KIND:
                raise SourceRedacted("related question is unavailable or not a question")
            if detail.privacy_class != current_version.privacy_class or detail.privacy_class not in access.privacy_classes:
                raise QuestionInvalid("related question privacy mismatch")
            if detail.scope and not scope_matches(detail.scope, current_version.scope or {}):
                raise QuestionInvalid("related question scope mismatch")
            if len(body["related_question_refs"]) >= MAX_RELATED_QUESTIONS:
                raise QuestionInvalid("related_question_refs is full")
            new_body["related_question_refs"] = self._merge_refs(
                body["related_question_refs"], [related.to_dict()], MAX_RELATED_QUESTIONS
            )

        # Validate the candidate body before opening the write transaction.
        candidate = _parse_body(
            new_body,
            status=new_status,
            namespace=access.namespace,
            record_version=expected_version + 1,
        )
        _check_refs(
            self._store,
            access,
            candidate,
            privacy_class=current_version.privacy_class,
            scope=current_version.scope or {},
            write=True,
        )

        return PreparedTransition(
            new_body=candidate,
            new_status=new_status,
            privacy_class=current_version.privacy_class,
            scope=current_version.scope,
        )

    def _apply_transition_locked(
        self,
        tx_scope: Any,
        *,
        namespace: str,
        record_id: str,
        expected_version: int,
        new_body: Mapping[str, Any],
        new_status: str,
        privacy_class: str,
        scope: Mapping[str, Any] | None,
    ) -> None:
        """Append the next Question version inside an already open TxScope.

        This is the transaction body of :meth:`transition`, moved here verbatim
        so one combination commit can validate and append several objects inside
        a single ``commit_command``.  The current-version check stays inside the
        caller's transaction and before the append.
        """

        pointer = self._store.record_pointer(namespace, record_id)
        if pointer is None or pointer.current_version != expected_version:
            raise RecordVersionConflict("question changed since expected_version")
        tx_scope.append_record_version(
            record_id=record_id,
            record_version=expected_version + 1,
            kind=QUESTION_KIND,
            body=new_body,
            status=new_status,
            schema_version=QUESTION_SCHEMA_VERSION,
            privacy_class=privacy_class,
            scope=scope,
        )

    def evaluate(
        self,
        access: QuestionAccess,
        *,
        context_id: str,
        request_id: str,
        ref: RecordRef,
        expected_version: int,
        evaluation_result: str,
        reason: str,
        evaluated_source_refs: Sequence[Mapping[str, Any]] = (),
        review_after: str | None = None,
    ) -> EvaluationReceipt:
        _require_permission(access, "question.evaluate")
        context_id = identifiers.require_context_id(context_id)
        request_id = identifiers.require_request_id(request_id)
        if evaluation_result not in EVALUATION_RESULTS:
            raise QuestionInvalid("unknown evaluation_result")
        _require_text(reason, "reason", MAX_REASON_CHARS)
        evaluated = _validate_exact_refs(
            evaluated_source_refs,
            "evaluated_source_refs",
            MAX_EVIDENCE_REFS,
            access.namespace,
        )
        payload = _evaluate_payload(
            record_id=ref.record_id,
            expected_version=expected_version,
            evaluation_result=evaluation_result,
            reason=reason,
            evaluated_source_refs=evaluated,
            review_after=review_after,
        )
        receipt = self._recover_committed(
            namespace=access.namespace,
            context_id=context_id,
            request_id=request_id,
            operation="question.evaluate",
            payload=payload,
        )
        if receipt is not None:
            if not receipt.record_refs:
                raise CorruptStore("question evaluate receipt has no record refs")
            record = receipt.record_refs[0]
            return EvaluationReceipt(
                ref=RecordRef(access.namespace, record.record_id, record.record_version),
                request_id=request_id,
                evaluated_at=receipt.committed_at,
                evaluation_result=evaluation_result,
                previous_version=record.record_version - 1,
                new_version=record.record_version,
                commit_receipt=receipt,
            )
        self._require_expected(access, ref, expected_version)
        current = self._store.get_version(access.namespace, ref.record_id, ref.record_version)
        if current.privacy_class not in access.privacy_classes:
            raise QuestionUnavailable("question is not authorized")
        if current.status not in ("open", "waiting"):
            raise InvalidTransition("evaluate requires open or waiting")
        body = _parse_body(
            current.body,
            status=current.status,
            namespace=access.namespace,
            record_version=current.record_version,
        )
        if not evaluated:
            raise QuestionInvalid("evaluate requires evaluated_source_refs")
        self._check_write_refs(access, evaluated, current.privacy_class, current.scope or {})
        _check_refs(
            self._store,
            access,
            body,
            privacy_class=current.privacy_class,
            scope=current.scope or {},
            write=True,
        )
        now = utc_now_rfc3339()
        body["last_evaluated_at"] = now
        body["last_evaluated_revision"] = expected_version + 1
        if review_after is not None:
            body["review_after"] = _require_timestamp(
                review_after, "review_after", optional=True
            )
        _parse_body(
            body,
            status=current.status,
            namespace=access.namespace,
            record_version=expected_version + 1,
        )

        def apply(tx_scope: Any) -> None:
            pointer = self._store.record_pointer(access.namespace, ref.record_id)
            if pointer is None or pointer.current_version != expected_version:
                raise RecordVersionConflict("question changed since expected_version")
            tx_scope.append_record_version(
                record_id=ref.record_id,
                record_version=expected_version + 1,
                kind=QUESTION_KIND,
                body=body,
                status=current.status,
                schema_version=QUESTION_SCHEMA_VERSION,
                privacy_class=current.privacy_class,
                scope=current.scope,
            )

        receipt = self._store.commit_command(
            context_id=context_id,
            request_id=request_id,
            operation="question.evaluate",
            request_payload=payload,
            apply=apply,
            namespace=access.namespace,
        )
        return EvaluationReceipt(
            ref=RecordRef(access.namespace, ref.record_id, expected_version + 1),
            request_id=request_id,
            evaluated_at=receipt.committed_at,
            evaluation_result=evaluation_result,
            previous_version=expected_version,
            new_version=expected_version + 1,
            commit_receipt=receipt,
        )

    def consume_trigger(
        self,
        access: QuestionAccess,
        *,
        context_id: str,
        request_id: str,
        ref: RecordRef,
        expected_version: int,
        trigger_ref: Mapping[str, Any],
    ) -> QuestionView:
        _require_permission(access, "question.transition")
        context_id = identifiers.require_context_id(context_id)
        request_id = identifiers.require_request_id(request_id)
        normalized = _validate_exact_refs([trigger_ref], "trigger_ref", 1, access.namespace)
        trigger = normalized[0]
        payload = _consume_payload(
            record_id=ref.record_id,
            expected_version=expected_version,
            trigger_ref=trigger,
        )
        receipt = self._recover_committed(
            namespace=access.namespace,
            context_id=context_id,
            request_id=request_id,
            operation="question.consume_trigger",
            payload=payload,
        )
        if receipt is not None:
            return self._view_from_receipt(access, receipt)
        self._require_expected(access, ref, expected_version)
        current = self._store.get_version(access.namespace, ref.record_id, ref.record_version)
        if current.privacy_class not in access.privacy_classes:
            raise QuestionUnavailable("question is not authorized")
        if current.status not in ("open", "waiting"):
            raise InvalidTransition("consume_trigger requires open or waiting")
        body = _parse_body(
            current.body,
            status=current.status,
            namespace=access.namespace,
            record_version=current.record_version,
        )
        _check_refs(
            self._store,
            access,
            body,
            privacy_class=current.privacy_class,
            scope=current.scope or {},
            write=True,
        )
        key = _ref_key(trigger)
        if key in {_ref_key(item) for item in body["consumed_trigger_refs"]}:
            return self._read_view(access, ref=ref, scope=current.scope)
        allowed = {
            _ref_key(item) for item in body["trigger_spec"]["source_refs"]
        }
        if key not in allowed:
            raise QuestionInvalid("trigger is not in trigger_spec.source_refs")
        if len(body["consumed_trigger_refs"]) >= MAX_CONSUMED_TRIGGERS:
            raise TriggerLimitReached("consumed trigger list is full")
        detail = _resolve_ref(self._store, access.namespace, trigger)
        if detail is None:
            raise SourceRedacted("trigger source is unavailable")
        if detail.privacy_class != current.privacy_class or detail.privacy_class not in access.privacy_classes:
            raise QuestionInvalid("trigger privacy mismatch")
        if detail.scope and not scope_matches(detail.scope, current.scope or {}):
            raise QuestionInvalid("trigger scope mismatch")
        body["consumed_trigger_refs"] = [*body["consumed_trigger_refs"], trigger]
        _parse_body(
            body,
            status=current.status,
            namespace=access.namespace,
            record_version=expected_version + 1,
        )

        def apply(tx_scope: Any) -> None:
            pointer = self._store.record_pointer(access.namespace, ref.record_id)
            if pointer is None or pointer.current_version != expected_version:
                raise RecordVersionConflict("question changed since expected_version")
            tx_scope.append_record_version(
                record_id=ref.record_id,
                record_version=expected_version + 1,
                kind=QUESTION_KIND,
                body=body,
                status=current.status,
                schema_version=QUESTION_SCHEMA_VERSION,
                privacy_class=current.privacy_class,
                scope=current.scope,
            )

        self._store.commit_command(
            context_id=context_id,
            request_id=request_id,
            operation="question.consume_trigger",
            request_payload=payload,
            apply=apply,
            namespace=access.namespace,
        )
        return self._read_view(
            access,
            ref=RecordRef(access.namespace, ref.record_id, expected_version + 1),
            scope=current.scope,
        )

    # ---------------------------------------------------------------- helpers

    def _recover_committed(
        self,
        *,
        namespace: str,
        context_id: str,
        request_id: str,
        operation: str,
        payload: Mapping[str, Any],
    ) -> Any | None:
        """Return a stored receipt only when the full request basis matches.

        This is the safe fast path for retries whose first response was lost.
        It runs before expected-version/domain checks: a replayed successful
        transition must not fail merely because the current pointer advanced.
        A different payload under the same request id is a conflict, never a
        dedupe path.
        """

        receipt = self._store.lookup_command_receipt(
            namespace=namespace, request_id=request_id
        )
        if receipt is None:
            return None
        stored_digest = self._store.lookup_command_request_digest(
            namespace=namespace, request_id=request_id
        )
        expected_digest = _commit_digest(namespace, operation, context_id, payload)
        if stored_digest != expected_digest or receipt.operation != operation:
            raise CommandConflict(
                "request id was already committed with a different request basis"
            )
        return receipt

    def _view_from_receipt(self, access: QuestionAccess, receipt: Any) -> QuestionView:
        if not receipt.record_refs:
            raise CorruptStore("question command receipt has no record refs")
        record = receipt.record_refs[0]
        version = self._store.get_version(
            access.namespace, record.record_id, record.record_version
        )
        return self._read_view(
            access,
            ref=RecordRef(
                access.namespace, record.record_id, record.record_version
            ),
            scope=version.scope,
        )

    def _require_expected(self, access: QuestionAccess, ref: RecordRef, expected: int) -> None:
        if ref.record_version != expected:
            raise RecordVersionConflict("ref version does not match expected_version")
        pointer = self._store.record_pointer(access.namespace, ref.record_id)
        if pointer is None:
            raise QuestionUnavailable("question does not exist")
        if pointer.kind != QUESTION_KIND:
            raise QuestionUnavailable("record is not an open question")
        if pointer.current_version != expected:
            raise RecordVersionConflict("question changed since expected_version")
        if self._store.record_is_deleted(access.namespace, ref.record_id):
            raise QuestionUnavailable("question has been deleted")

    def _check_write_refs(
        self,
        access: QuestionAccess,
        refs: Sequence[Mapping[str, Any]],
        privacy_class: str,
        scope: Mapping[str, Any],
    ) -> None:
        for ref in refs:
            detail = _resolve_ref(self._store, access.namespace, ref)
            if detail is None:
                raise SourceRedacted("question source unavailable")
            if detail.privacy_class != privacy_class or detail.privacy_class not in access.privacy_classes:
                raise QuestionInvalid("question source privacy mismatch")
            if detail.scope and not scope_matches(detail.scope, scope):
                raise QuestionInvalid("question source scope mismatch")

    def _matching_current(
        self, namespace: str, scope: Mapping[str, Any], key: str
    ) -> list[tuple[RecordRef, str, str]]:
        matches: list[tuple[RecordRef, str, str]] = []
        for version in self._store.current_versions_of_kind(namespace, QUESTION_KIND):
            try:
                body = _parse_body(
                    version.body,
                    status=version.status,
                    namespace=namespace,
                    record_version=version.record_version,
                )
            except QuestionInvalid:
                continue
            if not (
                scope_matches(version.scope or {}, scope)
                and scope_matches(scope, version.scope or {})
            ):
                continue
            if _dedupe_key(namespace, scope, body["question"]) != key:
                continue
            matches.append(
                (
                    RecordRef(namespace, version.record_id, version.record_version),
                    version.status,
                    version.privacy_class,
                )
            )
        return matches

    def _find_active(
        self,
        access: QuestionAccess,
        scope: Mapping[str, Any],
        key: str,
        privacy_class: str,
    ) -> QuestionView | None:
        matches = [
            item
            for item in self._matching_current(access.namespace, scope, key)
            if item[1] in ACTIVE_STATUSES
        ]
        if not matches:
            return None
        if any(item[2] != privacy_class for item in matches):
            raise QuestionInvalid("dedupe privacy class mismatch")
        try:
            return self._read_existing(access, matches[0][0], scope)
        except (QuestionUnavailable, Redacted, QuestionInvalid, SourceRedacted, ScopeViolation):
            return None

    def _read_existing(self, access: QuestionAccess, ref: RecordRef, scope: Mapping[str, Any]) -> QuestionView:
        return self._read_view(access, ref=ref, scope=scope)

    def _next_record_id(self, namespace: str, key: str) -> str:
        for generation in range(1, MAX_DEDUPE_PROBES + 1):
            candidate = str(
                uuid.uuid5(
                    QUESTION_DEDUPE_NAMESPACE,
                    f"{namespace}:{key}:{generation}",
                )
            )
            if self._store.record_pointer(namespace, candidate) is None:
                return candidate
        raise QuestionInvalid("question dedupe generation space exhausted")

    def _merge_refs(
        self,
        existing: Sequence[Mapping[str, Any]],
        extra: Sequence[Mapping[str, Any]],
        maximum: int,
    ) -> list[dict[str, Any]]:
        result = [dict(item) for item in existing]
        seen = {_ref_key(item) for item in result}
        for item in extra:
            wire = dict(item)
            key = _ref_key(wire)
            if key not in seen:
                result.append(wire)
                seen.add(key)
        if len(result) > maximum:
            raise QuestionInvalid(f"ref list exceeds {maximum} items")
        return result


# ---------------------------------------------------------------------------
# module functions


def _require_permission(access: QuestionAccess, permission: str) -> None:
    if not isinstance(access, QuestionAccess):
        raise QuestionInvalid("access must be a QuestionAccess")
    if permission not in access.permissions:
        raise ScopeViolation(f"question permission {permission!r} is required")


def _view_from_version(version: Any, body: Mapping[str, Any]) -> QuestionView:
    return QuestionView(
        ref=RecordRef(version.namespace, version.record_id, version.record_version),
        status=version.status,
        question=body["question"],
        origin=body["origin"],
        priority=body["priority"],
        paused=body["paused"],
        pause_reason=body["pause_reason"],
        waiting_for=body["waiting_for"],
        review_after=body["review_after"],
        last_evaluated_at=body["last_evaluated_at"],
        last_evaluated_revision=body["last_evaluated_revision"],
        scope=dict(version.scope or {}),
        recorded_at=version.recorded_at,
        evidence_refs=tuple(body["evidence_refs"]),
        trigger_spec=dict(body["trigger_spec"]),
        consumed_trigger_refs=tuple(body["consumed_trigger_refs"]),
        resolution_refs=tuple(body["resolution_refs"]),
        related_question_refs=tuple(body["related_question_refs"]),
        origin_refs=tuple(body["origin_refs"]),
        resolution_reason=body["resolution_reason"],
        reopen_reason=body["reopen_reason"],
    )


def _validate_action_fields(action: str, supplied: Mapping[str, Any]) -> None:
    allowed = {
        "accept": set(),
        "wait": {"waiting_for", "review_after"},
        "pause": {"pause_reason", "paused_by"},
        "resume": set(),
        "wake": {"new_evidence_refs", "reason"},
        "resolve": {"resolution_refs", "reason"},
        "close": {"reason"},
        "reopen": {"new_evidence_refs", "reason"},
        "update_triggers": {"trigger_spec"},
        "link_related_question": {"related_question_ref"},
    }[action]
    for field, value in supplied.items():
        if value is None or value == () or value == []:
            continue
        if field not in allowed:
            raise QuestionInvalid(f"{action} does not accept {field}")


__all__ = [
    "ACTIONS",
    "ACTIVE_STATUSES",
    "CreateResult",
    "DuplicateQuestion",
    "EVALUATION_RESULTS",
    "EvaluationReceipt",
    "InvalidTransition",
    "MAX_ATTENTION_SOURCES",
    "MAX_LIST_LIMIT",
    "QUESTION_KIND",
    "QUESTION_PERMISSIONS",
    "QUESTION_PURPOSE",
    "QUESTION_STATUSES",
    "QuestionAccess",
    "QuestionError",
    "QuestionInvalid",
    "QuestionService",
    "QuestionUnavailable",
    "QuestionView",
    "TriggerLimitReached",
]
