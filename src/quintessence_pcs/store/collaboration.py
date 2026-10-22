"""C05b: collaboration storage, source registration and the barrier.

The collaboration plane is deliberately small and closed:

* one row per interaction, input version, artifact version, work control,
  grant and source link;
* every body (input text, artifact text, candidate proposal) lives in
  ``private_payloads`` behind an opaque payload ref; no business table has a
  body TEXT column;
* a derived artifact may only be written after every collaboration source has
  been proven to exist at its exact version, to share the owner's namespace,
  to be outside the deletion barrier, and to cover the Context it names;
* reads consult the barrier *before* cleanup: a deleted target or a target
  whose collaboration source is barred is ``REDACTED`` immediately, even while
  the bytes are still on disk;
* cleanup clears both the target payloads and every dependent artifact/Context
  payload (including candidate proposals), and a pending candidate becomes
  ``stale`` in the same transaction;
* the two decision gates have a pure core and a thin row-loading wrapper, so
  C06 can call the same core inside a Tx B write transaction.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..foundation import identifiers
from ..foundation.errors import (
    AuthorizationRequired,
    CorruptStore,
    InvalidInput,
    RecordNotFound,
    Redacted,
    ScopeViolation,
    SnapshotNotFound,
    SourceRedacted,
)
from ..foundation.jsonutil import (
    canonical_json_text,
    new_payload_ref,
    sha256_hex,
    strict_json_loads,
)
from ..foundation.timeutil import utc_now_rfc3339
from .deletion import (
    DELETION_COLLAB_ARTIFACT,
    DELETION_COLLAB_INPUT,
    DELETION_EVENT,
    DELETION_RECORD,
    DeletionTarget,
)

COLLAB_SCHEMA_VERSION = "collab.v1"
#: Most-to-least restrictive.  A derived object's rank may never exceed the
#: strictest collaboration source's rank (C05a R15).
RETENTION_ORDER = ("session", "standard", "durable")
RETENTION_RANK = {name: rank for rank, name in enumerate(RETENTION_ORDER)}

COLLAB_INPUT = DELETION_COLLAB_INPUT
COLLAB_ARTIFACT = DELETION_COLLAB_ARTIFACT
COLLAB_SOURCE_KINDS = ("event", "record", COLLAB_INPUT, COLLAB_ARTIFACT)
ARTIFACT_KINDS = ("reply", "query_result", "candidate")
CANDIDATE_STATUSES = ("pending", "committed", "rejected", "stale", "failed")
DECIDED_STATUSES = ("committed", "rejected", "stale", "failed")
ORIGIN_KINDS = ("model_output", "deterministic_query")
INPUT_ORIGIN_KINDS = ("user_statement", "host_supplied")
INTERACTION_MODES = ("consult", "review")
ACTIVATION_KINDS = ("user_input", "manual_review")
MAX_BODY_CHARS = 262144
MAX_CLOSURE_DEPTH = 8
MAX_GRANT_TEXT = 512

_ARTIFACT_COLUMNS = (
    "namespace, artifact_id, artifact_version, schema_version, kind, "
    "interaction_id, context_id, attempt_ref_json, origin_kind, "
    "body_payload_ref, content_digest, retention_policy, created_at, "
    "proposal_payload_ref, proposal_digest, proposal_version, work_id, "
    "work_generation, request_id, command_digest, structural_validation, "
    "rerun_authorization_ref, commit_authorization_ref, status, decided_at, "
    "decision_ref"
)
_INPUT_COLUMNS = (
    "namespace, input_id, content_version, schema_version, origin_kind, "
    "body_payload_ref, content_digest, privacy_class, retention_policy, "
    "observed_at, recorded_at, supersedes_json"
)
_GRANT_COLUMNS = (
    "grant_id, namespace, schema_version, kind, grant_version, subject, "
    "authorization_ref, issued_at, expires_at, revoked_at, scope, "
    "purposes_json, data_classes_json, provider, endpoint, model, "
    "manifest_hash, privacy_classes_json, model_authorization_ref, "
    "operations_json, trigger_kinds_json, work_id, generation"
)
_LINK_COLUMNS = (
    "namespace, owner_kind, owner_id, owner_version, source_kind, source_id, "
    "source_version, created_at"
)
_WORK_COLUMNS = (
    "namespace, work_id, schema_version, generation, blocked_after_rejection, "
    "control_version, rerun_authorization_ref, block_reason, updated_at, "
    "last_candidate_ref"
)
_INTERACTION_COLUMNS = (
    "interaction_id, namespace, schema_version, mode, activation_kind, "
    "host_instance, trigger_ref_json, work_id, work_generation, context_id, "
    "result, receipt_request_id, access_ref, policy_ref, created_at, completed_at"
)


# --------------------------------------------------------------------------
# dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """One gate result; the pure core and the wrapper return the same shape."""

    allowed: bool
    reason: str
    generation: int


@dataclass(frozen=True)
class Approval:
    """The exact manual-approval binding required by the contract."""

    candidate_id: str
    proposal_digest: str
    context_id: str
    request_id: str


@dataclass(frozen=True)
class WorkControl:
    namespace: str
    work_id: str
    generation: int
    blocked_after_rejection: bool
    control_version: int
    rerun_authorization_ref: str | None
    block_reason: str | None
    updated_at: str
    last_candidate_ref: str | None


@dataclass(frozen=True)
class InputRow:
    namespace: str
    input_id: str
    content_version: int
    origin_kind: str
    body: str
    content_digest: str
    privacy_class: str
    retention: str
    observed_at: str | None
    recorded_at: str
    supersedes: dict[str, Any] | None


@dataclass(frozen=True)
class ArtifactRow:
    namespace: str
    artifact_id: str
    artifact_version: int
    kind: str
    interaction_id: str
    context_id: str
    attempt_ref: dict[str, Any] | None
    origin_kind: str
    body: str
    content_digest: str
    retention: str
    created_at: str
    source_refs: tuple[dict[str, Any], ...] = ()
    proposal: dict[str, Any] | None = None
    proposal_digest: str | None = None
    proposal_version: str | None = None
    work_id: str | None = None
    work_generation: int | None = None
    request_id: str | None = None
    command_digest: str | None = None
    rerun_authorization_ref: str | None = None
    commit_authorization_ref: str | None = None
    status: str | None = None
    decided_at: str | None = None
    decision_ref: str | None = None


@dataclass(frozen=True)
class GrantRow:
    grant_id: str
    namespace: str
    schema_version: str
    kind: str
    grant_version: int
    subject: str
    authorization_ref: str
    issued_at: str
    expires_at: str | None
    revoked_at: str | None
    scope: str | None = None
    purposes: tuple[str, ...] = ()
    data_classes: tuple[str, ...] = ()
    provider: str | None = None
    endpoint: str | None = None
    model: str | None = None
    manifest_hash: str | None = None
    privacy_classes: tuple[str, ...] = ()
    model_authorization_ref: str | None = None
    operations: tuple[str, ...] = ()
    trigger_kinds: tuple[str, ...] = ()
    work_id: str | None = None
    generation: int | None = None


# --------------------------------------------------------------------------
# small validators
# --------------------------------------------------------------------------


def _require_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or identifiers.UUID_PATTERN.fullmatch(value) is None:
        raise InvalidInput(f"{field_name} must be a canonical lower-case UUID")
    return value


def _require_text(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{field_name} must be a non-empty string")
    if len(value) > max_length:
        raise InvalidInput(f"{field_name} must be at most {max_length} characters")
    return value


def _require_timestamp(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or identifiers.TIMESTAMP_PATTERN.fullmatch(value) is None
    ):
        raise InvalidInput(f"{field_name} must be a UTC RFC 3339 timestamp ending in Z")
    return value


def _nullable_timestamp(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_timestamp(value, field_name)


def _require_version(value: Any, field_name: str) -> int:
    if type(value) is not int or value < 1 or value > identifiers.MAX_SAFE_INTEGER:
        raise InvalidInput(f"{field_name} must be a positive JSON-safe integer")
    return value


def _require_generation(value: Any, field_name: str) -> int:
    if type(value) is not int or value < 0 or value > identifiers.MAX_SAFE_INTEGER:
        raise InvalidInput(f"{field_name} must be a non-negative JSON-safe integer")
    return value


def _require_retention(value: Any) -> str:
    if value not in RETENTION_RANK:
        raise InvalidInput(
            f"retention must be one of {', '.join(RETENTION_ORDER)}"
        )
    return value


def _require_privacy(value: Any) -> str:
    if value not in identifiers.PRIVACY_CLASSES:
        raise InvalidInput(
            f"privacy_class must be one of {', '.join(identifiers.PRIVACY_CLASSES)}"
        )
    return value


def _require_authorization_ref(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthorizationRequired(
            "deletion requires an explicit, non-empty authorization_ref"
        )
    if len(value) > MAX_GRANT_TEXT:
        raise AuthorizationRequired(
            f"authorization_ref must be at most {MAX_GRANT_TEXT} characters"
        )
    return value


def _require_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidInput(f"{field_name} must be a JSON object")
    result = dict(value)
    canonical_json_text(result)
    return result


def _optional_mapping(value: Any, field_name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return _require_mapping(value, field_name)


def _text_list(value: Any, field_name: str, max_items: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise InvalidInput(f"{field_name} must be a non-empty list")
    if len(value) > max_items:
        raise InvalidInput(f"{field_name} must have at most {max_items} items")
    result: list[str] = []
    for item in value:
        text = _require_text(item, field_name, 128)
        if text in result:
            raise InvalidInput(f"{field_name} must not contain duplicates")
        result.append(text)
    return tuple(result)


def _privacy_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise InvalidInput(f"{field_name} must be a non-empty list")
    if len(value) > len(identifiers.PRIVACY_CLASSES):
        raise InvalidInput(f"{field_name} has too many privacy classes")
    result: list[str] = []
    for item in value:
        text = _require_privacy(item)
        if text in result:
            raise InvalidInput(f"{field_name} must not contain duplicates")
        result.append(text)
    return tuple(result)


def _decode_json(text: str | None, field_name: str) -> Any:
    if text is None:
        return None
    try:
        return strict_json_loads(text)
    except ValueError as exc:
        raise CorruptStore(f"stored {field_name} is not strict JSON") from exc


def _sha256_text(value: str) -> str:
    return "sha256:" + sha256_hex(value)


def _source_value(value: Any, owner_namespace: str) -> tuple[str, str, int, dict[str, Any]]:
    """Normalise one contract-style source ref to ``(kind, id, version, ref)``."""

    if not isinstance(value, Mapping):
        raise InvalidInput("a source must be a typed reference object")
    kind = value.get("ref_type")
    namespace = value.get("namespace")
    if namespace != owner_namespace:
        raise ScopeViolation(
            f"source {kind!r} belongs to namespace {namespace!r}, "
            f"not {owner_namespace!r}"
        )
    if kind == "event":
        expected = {"ref_type", "namespace", "event_id"}
        if set(value) != expected:
            raise InvalidInput("event source must have exactly ref_type, namespace, event_id")
        identifier = _require_uuid(value["event_id"], "event_id")
        return kind, identifier, 0, {
            "ref_type": "event",
            "namespace": namespace,
            "event_id": identifier,
        }
    if kind == "record":
        expected = {"ref_type", "namespace", "record_id", "record_version"}
        if set(value) != expected:
            raise InvalidInput(
                "record source must have exactly ref_type, namespace, record_id, record_version"
            )
        identifier = _require_uuid(value["record_id"], "record_id")
        version = _require_version(value["record_version"], "record_version")
        return kind, identifier, version, {
            "ref_type": "record",
            "namespace": namespace,
            "record_id": identifier,
            "record_version": version,
        }
    if kind == COLLAB_INPUT:
        expected = {"ref_type", "namespace", "input_id", "content_version"}
        if set(value) != expected:
            raise InvalidInput(
                "collab_input source must have exactly ref_type, namespace, "
                "input_id, content_version"
            )
        identifier = _require_uuid(value["input_id"], "input_id")
        version = _require_version(value["content_version"], "content_version")
        return kind, identifier, version, {
            "ref_type": "collab_input",
            "namespace": namespace,
            "input_id": identifier,
            "content_version": version,
        }
    if kind == COLLAB_ARTIFACT:
        expected = {"ref_type", "namespace", "artifact_id", "artifact_version"}
        if set(value) != expected:
            raise InvalidInput(
                "collab_artifact source must have exactly ref_type, namespace, "
                "artifact_id, artifact_version"
            )
        identifier = _require_uuid(value["artifact_id"], "artifact_id")
        version = _require_version(value["artifact_version"], "artifact_version")
        return kind, identifier, version, {
            "ref_type": "collab_artifact",
            "namespace": namespace,
            "artifact_id": identifier,
            "artifact_version": version,
        }
    raise InvalidInput(f"unknown source ref_type {kind!r}")


def _input_ref_value(value: Any, owner_namespace: str) -> tuple[str, int]:
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            raise InvalidInput("input_refs tuples must be (input_id, content_version)")
        identifier, version = value
    elif isinstance(value, Mapping):
        identifier = value.get("input_id")
        version = value.get("content_version")
        if value.get("namespace") not in (None, owner_namespace):
            raise ScopeViolation("input reference crosses namespaces")
    else:
        raise InvalidInput("input_refs must contain (input_id, content_version) pairs")
    return _require_uuid(identifier, "input_id"), _require_version(
        version, "content_version"
    )


# --------------------------------------------------------------------------
# mixin
# --------------------------------------------------------------------------


class CollaborationMixin:
    """Collaboration storage composed into ``CognitionStore``.

    It relies on the store base for transactions, payload access and the
    record/event deletion barrier; it never re-implements those.
    """

    # ------------------------------------------------------------- payloads

    def _insert_collab_payload_locked(self, wrapper_key: str, value: Any) -> str:
        if self._tx_depth == 0:  # pragma: no cover - callers are transactional
            raise RuntimeError(
                "_insert_collab_payload_locked must run inside the caller's transaction"
            )
        body_text = canonical_json_text({wrapper_key: value})
        payload_ref = new_payload_ref()
        self._require_connection().execute(
            "INSERT INTO private_payloads (payload_ref, sha256, body_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (payload_ref, sha256_hex(body_text), body_text, utc_now_rfc3339()),
        )
        return payload_ref

    def _load_collab_payload(
        self, payload_ref: str, wrapper_key: str, owner: str
    ) -> str:
        row = self._require_connection().execute(
            "SELECT sha256, body_json FROM private_payloads WHERE payload_ref = ?",
            (payload_ref,),
        ).fetchone()
        if row is None:
            raise CorruptStore(f"{owner} references missing payload {payload_ref}")
        if row["body_json"] == "":
            raise Redacted(f"{owner} payload has been cleared by a deletion")
        if row["sha256"] != sha256_hex(row["body_json"]):
            raise CorruptStore(f"{owner} payload digest does not match its bytes")
        try:
            wrapper = strict_json_loads(row["body_json"])
        except ValueError as exc:
            raise CorruptStore(f"{owner} payload is not strict JSON") from exc
        if not isinstance(wrapper, dict) or set(wrapper) != {wrapper_key}:
            raise CorruptStore(f"{owner} payload has an unexpected shape")
        text = wrapper[wrapper_key]
        if not isinstance(text, str):
            raise CorruptStore(f"{owner} payload does not hold text")
        return text

    # ------------------------------------------------------------- inputs

    def register_input(
        self,
        *,
        namespace: str,
        origin_kind: str,
        body: str,
        privacy_class: str,
        retention: str,
        observed_at: str | None = None,
        recorded_at: str | None = None,
        input_id: str | None = None,
        content_version: int = 1,
        supersedes: Mapping[str, Any] | None = None,
    ) -> InputRow:
        """Write one immutable input version in a single transaction."""

        ns = identifiers.require_namespace(namespace)
        if origin_kind not in INPUT_ORIGIN_KINDS:
            raise InvalidInput(
                f"origin_kind must be one of {', '.join(INPUT_ORIGIN_KINDS)}"
            )
        if not isinstance(body, str) or not body or len(body) > MAX_BODY_CHARS:
            raise InvalidInput("input body must be 1..262144 characters")
        privacy = _require_privacy(privacy_class)
        policy = _require_retention(retention)
        observed = _nullable_timestamp(observed_at, "observed_at")
        recorded = utc_now_rfc3339() if recorded_at is None else _require_timestamp(
            recorded_at, "recorded_at"
        )
        identifier = str(uuid.uuid4()) if input_id is None else _require_uuid(
            input_id, "input_id"
        )
        version = _require_version(content_version, "content_version")
        supersedes_ref = _optional_mapping(supersedes, "supersedes")
        if supersedes_ref is not None:
            kind, ref_id, ref_version, _ = _source_value(supersedes_ref, ns)
            if kind != COLLAB_INPUT:
                raise InvalidInput("supersedes must be a collab_input reference")
            self._require_source_exists_locked(ns, kind, ref_id, ref_version)

        digest = _sha256_text(body)
        with self.transaction():
            payload_ref = self._insert_collab_payload_locked("body", body)
            try:
                self._require_connection().execute(
                    "INSERT INTO collab_inputs ("
                    + _INPUT_COLUMNS
                    + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ns,
                        identifier,
                        version,
                        COLLAB_SCHEMA_VERSION,
                        origin_kind,
                        payload_ref,
                        digest,
                        privacy,
                        policy,
                        observed,
                        recorded,
                        None
                        if supersedes_ref is None
                        else canonical_json_text(supersedes_ref),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidInput(
                    f"input {identifier} version {version} already exists"
                ) from exc
        return InputRow(
            namespace=ns,
            input_id=identifier,
            content_version=version,
            origin_kind=origin_kind,
            body=body,
            content_digest=digest,
            privacy_class=privacy,
            retention=policy,
            observed_at=observed,
            recorded_at=recorded,
            supersedes=supersedes_ref,
        )

    def load_input(
        self, *, namespace: str, input_id: str, content_version: int
    ) -> InputRow:
        ns = identifiers.require_namespace(namespace)
        identifier = _require_uuid(input_id, "input_id")
        version = _require_version(content_version, "content_version")
        if self._source_barred_locked(
            namespace=ns, kind=COLLAB_INPUT, ref_id=identifier, version=version
        ):
            raise Redacted(f"collab input {identifier} version {version} has been deleted")
        row = self._require_connection().execute(
            f"SELECT {_INPUT_COLUMNS} FROM collab_inputs "
            "WHERE namespace = ? AND input_id = ? AND content_version = ?",
            (ns, identifier, version),
        ).fetchone()
        if row is None:
            raise RecordNotFound(
                f"no collab input {identifier} version {version} in {ns!r}"
            )
        body = self._load_collab_payload(
            row["body_payload_ref"], "body", f"collab input {identifier}"
        )
        if row["content_digest"] != _sha256_text(body):
            raise CorruptStore("collab input content_digest does not match its body")
        return InputRow(
            namespace=ns,
            input_id=identifier,
            content_version=int(row["content_version"]),
            origin_kind=row["origin_kind"],
            body=body,
            content_digest=row["content_digest"],
            privacy_class=row["privacy_class"],
            retention=row["retention_policy"],
            observed_at=row["observed_at"],
            recorded_at=row["recorded_at"],
            supersedes=_decode_json(row["supersedes_json"], "supersedes_json"),
        )

    # --------------------------------------------------------- interactions

    def register_interaction(
        self,
        *,
        namespace: str,
        mode: str,
        activation_kind: str,
        host_instance: str,
        access_ref: str,
        policy_ref: str,
        input_refs: Iterable[Any] = (),
        context_id: str | None = None,
        trigger_ref: Mapping[str, Any] | None = None,
        work_id: str | None = None,
        work_generation: int | None = None,
        result: str | None = None,
        receipt_request_id: str | None = None,
        completed_at: str | None = None,
        interaction_id: str | None = None,
        created_at: str | None = None,
    ) -> str:
        """Write an interaction and its exact input versions atomically."""

        ns = identifiers.require_namespace(namespace)
        if mode not in INTERACTION_MODES:
            raise InvalidInput(f"mode must be one of {', '.join(INTERACTION_MODES)}")
        if activation_kind not in ACTIVATION_KINDS:
            raise InvalidInput(
                f"activation_kind must be one of {', '.join(ACTIVATION_KINDS)}"
            )
        host = _require_text(host_instance, "host_instance", 256)
        access = _require_text(access_ref, "access_ref", MAX_GRANT_TEXT)
        policy = _require_text(policy_ref, "policy_ref", MAX_GRANT_TEXT)
        identifier = str(uuid.uuid4()) if interaction_id is None else _require_uuid(
            interaction_id, "interaction_id"
        )
        created = utc_now_rfc3339() if created_at is None else _require_timestamp(
            created_at, "created_at"
        )
        trigger = _optional_mapping(trigger_ref, "trigger_ref")
        if trigger is not None:
            kind, ref_id, _, trigger = _source_value(trigger, ns)
            if kind != "event":
                raise InvalidInput("a review trigger must be an event reference")
        work: str | None = None
        generation: int | None = None
        if mode == "review":
            if trigger is None:
                raise InvalidInput("a review interaction requires a trigger event")
            if work_id is None or work_generation is None:
                raise InvalidInput(
                    "a review interaction requires work_id and work_generation"
                )
        if mode == "consult":
            if trigger is not None or work_id is not None or work_generation is not None:
                raise InvalidInput("a consult interaction has no trigger or work binding")
        if work_id is not None:
            work = _require_uuid(work_id, "work_id")
            generation = _require_generation(work_generation, "work_generation")
        context = None if context_id is None else _require_uuid(context_id, "context_id")
        receipt = (
            None
            if receipt_request_id is None
            else _require_uuid(receipt_request_id, "receipt_request_id")
        )
        completed = _nullable_timestamp(completed_at, "completed_at")
        if result is not None and result not in (
            "answered",
            "waiting",
            "candidate_pending",
            "committed",
            "rejected",
            "stale",
            "denied",
            "failed",
            "incomplete",
        ):
            raise InvalidInput("result is not a frozen interaction result")
        refs: list[tuple[str, int]] = []
        for value in input_refs:
            refs.append(_input_ref_value(value, ns))

        with self.transaction():
            for input_id, version in refs:
                self._require_source_exists_locked(ns, COLLAB_INPUT, input_id, version)
                if self._source_barred_locked(
                    namespace=ns, kind=COLLAB_INPUT, ref_id=input_id, version=version
                ):
                    raise SourceRedacted(
                        f"collab input {input_id} version {version} is inside a deletion barrier"
                    )
            try:
                self._require_connection().execute(
                    "INSERT INTO collab_interactions ("
                    + _INTERACTION_COLUMNS
                    + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        identifier,
                        ns,
                        COLLAB_SCHEMA_VERSION,
                        mode,
                        activation_kind,
                        host,
                        None if trigger is None else canonical_json_text(trigger),
                        work,
                        generation,
                        context,
                        result,
                        receipt,
                        access,
                        policy,
                        created,
                        completed,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidInput(f"interaction {identifier} already exists") from exc
            for position, (input_id, version) in enumerate(refs):
                self._require_connection().execute(
                    "INSERT INTO collab_interaction_inputs ("
                    "namespace, interaction_id, input_id, content_version, position"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (ns, identifier, input_id, version, position),
                )
        return identifier

    # ------------------------------------------------------------- artifacts

    def register_artifact(
        self,
        *,
        namespace: str,
        kind: str,
        interaction_id: str,
        context_id: str,
        origin_kind: str,
        body: str,
        retention: str,
        sources: Iterable[Any] = (),
        artifact_id: str | None = None,
        artifact_version: int = 1,
        attempt_ref: Mapping[str, Any] | None = None,
        created_at: str | None = None,
        proposal: Mapping[str, Any] | None = None,
        proposal_version: str | None = None,
        work_id: str | None = None,
        work_generation: int | None = None,
        request_id: str | None = None,
        command_digest: str | None = None,
        rerun_authorization_ref: str | None = None,
        commit_authorization_ref: str | None = None,
        status: str | None = None,
        decided_at: str | None = None,
        decision_ref: str | None = None,
    ) -> ArtifactRow:
        """Write one artifact (and, for a candidate, its proposal) atomically.

        Every source is proven first: same namespace, exact version exists,
        outside the barrier, and (when ``context_id`` is supplied) covering all
        of the Context's sources.  The artifact row, its payloads and every
        source link commit together; a failure leaves no readable artifact.
        """

        ns = identifiers.require_namespace(namespace)
        if kind not in ARTIFACT_KINDS:
            raise InvalidInput(f"kind must be one of {', '.join(ARTIFACT_KINDS)}")
        if origin_kind not in ORIGIN_KINDS:
            raise InvalidInput(
                f"origin_kind must be one of {', '.join(ORIGIN_KINDS)}"
            )
        if not isinstance(body, str) or not body or len(body) > MAX_BODY_CHARS:
            raise InvalidInput("artifact body must be 1..262144 characters")
        policy = _require_retention(retention)
        interaction = _require_uuid(interaction_id, "interaction_id")
        context = _require_uuid(context_id, "context_id")
        identifier = str(uuid.uuid4()) if artifact_id is None else _require_uuid(
            artifact_id, "artifact_id"
        )
        version = _require_version(artifact_version, "artifact_version")
        created = utc_now_rfc3339() if created_at is None else _require_timestamp(
            created_at, "created_at"
        )
        attempt = _optional_mapping(attempt_ref, "attempt_ref")

        normalized: list[tuple[str, str, int, dict[str, Any]]] = []
        seen: set[tuple[str, str, int]] = set()
        for value in sources:
            source = _source_value(value, ns)
            key = (source[0], source[1], source[2])
            if key in seen:
                raise InvalidInput("duplicate source reference")
            seen.add(key)
            normalized.append(source)

        with self.transaction():
            self._validate_sources_locked(
                namespace=ns,
                sources=normalized,
                context_id=context,
                artifact_id=identifier,
                artifact_version=version,
                retention=policy,
            )
            payload_ref = self._insert_collab_payload_locked("body", body)
            digest = _sha256_text(body)
            if kind == "candidate":
                if proposal is None or proposal_version is None:
                    raise InvalidInput("a candidate requires a proposal and proposal_version")
                proposal_value = _require_mapping(proposal, "proposal")
                proposal_text = canonical_json_text(proposal_value)
                proposal_digest = _sha256_text(proposal_text)
                work = _require_uuid(work_id, "work_id")
                generation = _require_generation(work_generation, "work_generation")
                request = _require_uuid(request_id, "request_id")
                status = "pending" if status is None else status
                if status not in CANDIDATE_STATUSES:
                    raise InvalidInput("candidate status is not a frozen value")
                operation = proposal_value.get("operation")
                if not isinstance(operation, str) or not operation:
                    raise InvalidInput("candidate proposal must name an operation")
                expected_digest = sha256_hex(
                    canonical_json_text(
                        {
                            "context_id": context,
                            "namespace": ns,
                            "operation": operation,
                            "request_payload": proposal_value,
                        }
                    )
                )
                if command_digest is None:
                    command_digest_value = expected_digest
                else:
                    if (
                        not isinstance(command_digest, str)
                        or len(command_digest) != 64
                        or any(ch not in "0123456789abcdef" for ch in command_digest)
                    ):
                        raise InvalidInput("command_digest must be 64 lower-case hex")
                    if command_digest != expected_digest:
                        raise InvalidInput(
                            "command_digest does not match the Tx B request digest"
                        )
                    command_digest_value = command_digest
                proposal_payload_ref = self._insert_collab_payload_locked(
                    "proposal", proposal_text
                )
                proposal_columns: tuple[Any, ...] = (
                    proposal_payload_ref,
                    proposal_digest,
                    _require_text(proposal_version, "proposal_version", 64),
                    work,
                    generation,
                    request,
                    command_digest_value,
                    "passed",
                )
            else:
                if any(
                    value is not None
                    for value in (
                        proposal,
                        proposal_version,
                        work_id,
                        work_generation,
                        request_id,
                        command_digest,
                        rerun_authorization_ref,
                        commit_authorization_ref,
                        status,
                        decided_at,
                        decision_ref,
                    )
                ):
                    raise InvalidInput(
                        "only a candidate may carry proposal/work/decision columns"
                    )
                proposal_payload_ref = None
                proposal_columns = (None,) * 8
            decided = _nullable_timestamp(decided_at, "decided_at")
            decision = (
                None
                if decision_ref is None
                else _require_text(decision_ref, "decision_ref", 256)
            )
            rerun = (
                None
                if rerun_authorization_ref is None
                else _require_text(rerun_authorization_ref, "rerun_authorization_ref", 256)
            )
            commit = (
                None
                if commit_authorization_ref is None
                else _require_text(
                    commit_authorization_ref, "commit_authorization_ref", 256
                )
            )
            try:
                self._require_connection().execute(
                    "INSERT INTO collab_artifacts ("
                    + _ARTIFACT_COLUMNS
                    + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ns,
                        identifier,
                        version,
                        COLLAB_SCHEMA_VERSION,
                        kind,
                        interaction,
                        context,
                        None if attempt is None else canonical_json_text(attempt),
                        origin_kind,
                        payload_ref,
                        digest,
                        policy,
                        created,
                        *proposal_columns,
                        rerun,
                        commit,
                        status if kind == "candidate" else None,
                        decided,
                        decision,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidInput(
                    f"artifact {identifier} version {version} violates its constraints"
                ) from exc
            for source_kind, source_id, source_version, _ in normalized:
                self._require_connection().execute(
                    "INSERT INTO collab_source_links ("
                    + _LINK_COLUMNS
                    + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ns,
                        "artifact",
                        identifier,
                        version,
                        source_kind,
                        source_id,
                        source_version,
                        created,
                    ),
                )
        return self.load_artifact(
            namespace=ns, artifact_id=identifier, artifact_version=version
        )

    def load_artifact(
        self, *, namespace: str, artifact_id: str, artifact_version: int
    ) -> ArtifactRow:
        ns = identifiers.require_namespace(namespace)
        identifier = _require_uuid(artifact_id, "artifact_id")
        version = _require_version(artifact_version, "artifact_version")
        if self._source_barred_locked(
            namespace=ns, kind=COLLAB_ARTIFACT, ref_id=identifier, version=version
        ):
            raise Redacted(
                f"collab artifact {identifier} version {version} is inside a deletion barrier"
            )
        row = self._require_connection().execute(
            f"SELECT {_ARTIFACT_COLUMNS} FROM collab_artifacts "
            "WHERE namespace = ? AND artifact_id = ? AND artifact_version = ?",
            (ns, identifier, version),
        ).fetchone()
        if row is None:
            raise RecordNotFound(
                f"no collab artifact {identifier} version {version} in {ns!r}"
            )
        body = self._load_collab_payload(
            row["body_payload_ref"], "body", f"collab artifact {identifier}"
        )
        if row["content_digest"] != _sha256_text(body):
            raise CorruptStore("collab artifact content_digest does not match its body")
        proposal = None
        if row["proposal_payload_ref"] is not None:
            proposal_text = self._load_collab_payload(
                row["proposal_payload_ref"],
                "proposal",
                f"collab candidate {identifier}",
            )
            proposal = strict_json_loads(proposal_text)
            if row["proposal_digest"] != _sha256_text(proposal_text):
                raise CorruptStore("candidate proposal_digest does not match its payload")
        return ArtifactRow(
            namespace=ns,
            artifact_id=identifier,
            artifact_version=int(row["artifact_version"]),
            kind=row["kind"],
            interaction_id=row["interaction_id"],
            context_id=row["context_id"],
            attempt_ref=_decode_json(row["attempt_ref_json"], "attempt_ref_json"),
            origin_kind=row["origin_kind"],
            body=body,
            content_digest=row["content_digest"],
            retention=row["retention_policy"],
            created_at=row["created_at"],
            source_refs=self._source_refs_for_artifact_locked(identifier, version),
            proposal=proposal,
            proposal_digest=row["proposal_digest"],
            proposal_version=row["proposal_version"],
            work_id=row["work_id"],
            work_generation=(
                None if row["work_generation"] is None else int(row["work_generation"])
            ),
            request_id=row["request_id"],
            command_digest=row["command_digest"],
            rerun_authorization_ref=row["rerun_authorization_ref"],
            commit_authorization_ref=row["commit_authorization_ref"],
            status=row["status"],
            decided_at=row["decided_at"],
            decision_ref=row["decision_ref"],
        )

    def _source_refs_for_artifact_locked(
        self, artifact_id: str, artifact_version: int
    ) -> tuple[dict[str, Any], ...]:
        rows = self._require_connection().execute(
            "SELECT namespace, source_kind, source_id, source_version "
            "FROM collab_source_links WHERE owner_kind = 'artifact' "
            "AND owner_id = ? AND owner_version = ? "
            "ORDER BY source_kind, source_id, source_version",
            (artifact_id, artifact_version),
        ).fetchall()
        refs: list[dict[str, Any]] = []
        for row in rows:
            kind = row["source_kind"]
            if kind == "event":
                refs.append(
                    {
                        "ref_type": "event",
                        "namespace": row["namespace"],
                        "event_id": row["source_id"],
                    }
                )
            elif kind == "record":
                refs.append(
                    {
                        "ref_type": "record",
                        "namespace": row["namespace"],
                        "record_id": row["source_id"],
                        "record_version": int(row["source_version"]),
                    }
                )
            elif kind == COLLAB_INPUT:
                refs.append(
                    {
                        "ref_type": COLLAB_INPUT,
                        "namespace": row["namespace"],
                        "input_id": row["source_id"],
                        "content_version": int(row["source_version"]),
                    }
                )
            elif kind == COLLAB_ARTIFACT:
                refs.append(
                    {
                        "ref_type": COLLAB_ARTIFACT,
                        "namespace": row["namespace"],
                        "artifact_id": row["source_id"],
                        "artifact_version": int(row["source_version"]),
                    }
                )
            else:  # pragma: no cover - CHECK constraint
                raise CorruptStore(f"stored source link has unknown kind {kind!r}")
        return tuple(refs)

    # --------------------------------------------------- context source links

    def register_context_sources(
        self,
        *,
        namespace: str,
        context_id: str,
        sources: Iterable[Any],
    ) -> int:
        """Write the Context's collaboration links inside the Context insert.

        The caller owns the transaction (C08 calls this from the same
        transaction that inserts ``context_snapshots``); this method never
        starts or commits one of its own.
        """

        if self._tx_depth == 0:
            raise RuntimeError(
                "register_context_sources must run inside the Context insert transaction"
            )
        ns = identifiers.require_namespace(namespace)
        context = _require_uuid(context_id, "context_id")
        row = self._require_connection().execute(
            "SELECT namespace FROM context_snapshots WHERE context_id = ?",
            (context,),
        ).fetchone()
        if row is None:
            raise SnapshotNotFound(f"no persisted context {context!r}")
        if row["namespace"] != ns:
            raise ScopeViolation(
                f"context {context} belongs to namespace {row['namespace']!r}, not {ns!r}"
            )
        normalized: list[tuple[str, str, int, dict[str, Any]]] = []
        seen: set[tuple[str, str, int]] = set()
        for value in sources:
            source = _source_value(value, ns)
            key = (source[0], source[1], source[2])
            if key in seen:
                raise InvalidInput("duplicate source reference")
            seen.add(key)
            normalized.append(source)
        self._validate_sources_locked(
            namespace=ns,
            sources=normalized,
            context_id=None,
            artifact_id=None,
            artifact_version=None,
            retention=None,
        )
        now = utc_now_rfc3339()
        for source_kind, source_id, source_version, _ in normalized:
            try:
                self._require_connection().execute(
                    "INSERT INTO collab_source_links ("
                    + _LINK_COLUMNS
                    + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ns, "context", context, 0, source_kind, source_id, source_version, now),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidInput(
                    "a context source link already exists or violates its constraints"
                ) from exc
        return len(normalized)

    # -------------------------------------------------------------- sources

    def _validate_sources_locked(
        self,
        *,
        namespace: str,
        sources: Sequence[tuple[str, str, int, dict[str, Any]]],
        context_id: str | None,
        artifact_id: str | None,
        artifact_version: int | None,
        retention: str | None,
    ) -> None:
        # 1-3: namespace, exact existence, barrier.
        for kind, source_id, version, _ in sources:
            self._require_source_exists_locked(namespace, kind, source_id, version)
            if self._source_barred_locked(
                namespace=namespace, kind=kind, ref_id=source_id, version=version
            ):
                raise SourceRedacted(
                    f"source {kind} {source_id} version {version} is inside a deletion barrier"
                )
        # 4: a derived artifact must cover every source of the Context it names.
        if context_id is not None:
            context_keys = self._context_source_keys_locked(namespace, context_id)
            provided = {(kind, source_id, version) for kind, source_id, version, _ in sources}
            if not context_keys.issubset(provided):
                raise InvalidInput("source closure does not cover the context")
        # 5: self-reference, cycles and unbounded depth are hard failures.
        if artifact_id is not None:
            for kind, source_id, version, _ in sources:
                if kind != COLLAB_ARTIFACT:
                    continue
                if source_id == artifact_id and version == artifact_version:
                    raise CorruptStore("an artifact cannot cite itself as a source")
                depth = self._artifact_depth_locked(
                    namespace, source_id, version, set()
                )
                if depth + 1 > MAX_CLOSURE_DEPTH:
                    raise CorruptStore("source closure depth exceeded")
        # C05a R15: derived retention may not outlive a collaboration source.
        if retention is not None:
            own = RETENTION_RANK[retention]
            strictest: int | None = None
            for kind, source_id, version, _ in sources:
                if kind == COLLAB_INPUT:
                    row = self._require_connection().execute(
                        "SELECT retention_policy FROM collab_inputs "
                        "WHERE namespace = ? AND input_id = ? AND content_version = ?",
                        (namespace, source_id, version),
                    ).fetchone()
                elif kind == COLLAB_ARTIFACT:
                    row = self._require_connection().execute(
                        "SELECT retention_policy FROM collab_artifacts "
                        "WHERE namespace = ? AND artifact_id = ? AND artifact_version = ?",
                        (namespace, source_id, version),
                    ).fetchone()
                else:
                    continue
                if row is None:  # pragma: no cover - existence checked above
                    continue
                source_rank = RETENTION_RANK[row["retention_policy"]]
                strictest = (
                    source_rank if strictest is None else min(strictest, source_rank)
                )
            if strictest is not None and own > strictest:
                raise InvalidInput(
                    "derived retention is wider than a collaboration source"
                )

    def _require_source_exists_locked(
        self, namespace: str, kind: str, source_id: str, version: int
    ) -> None:
        connection = self._require_connection()
        if kind == "event":
            row = connection.execute(
                "SELECT 1 FROM event_journal WHERE namespace = ? AND event_id = ?",
                (namespace, source_id),
            ).fetchone()
        elif kind == "record":
            row = connection.execute(
                "SELECT 1 FROM record_versions "
                "WHERE namespace = ? AND record_id = ? AND record_version = ?",
                (namespace, source_id, version),
            ).fetchone()
        elif kind == COLLAB_INPUT:
            row = connection.execute(
                "SELECT 1 FROM collab_inputs "
                "WHERE namespace = ? AND input_id = ? AND content_version = ?",
                (namespace, source_id, version),
            ).fetchone()
        elif kind == COLLAB_ARTIFACT:
            row = connection.execute(
                "SELECT 1 FROM collab_artifacts "
                "WHERE namespace = ? AND artifact_id = ? AND artifact_version = ?",
                (namespace, source_id, version),
            ).fetchone()
        else:
            raise InvalidInput(f"unknown source kind {kind!r}")
        if row is None:
            raise RecordNotFound(
                f"source {kind} {source_id} version {version} does not exist "
                f"in namespace {namespace!r}"
            )

    def _context_source_keys_locked(
        self, namespace: str, context_id: str
    ) -> set[tuple[str, str, int]]:
        row = self._require_connection().execute(
            "SELECT 1 FROM context_snapshots WHERE context_id = ? AND namespace = ?",
            (context_id, namespace),
        ).fetchone()
        if row is None:
            raise SnapshotNotFound(f"no persisted context {context_id!r}")
        keys: set[tuple[str, str, int]] = set()
        for row in self._require_connection().execute(
            "SELECT ref_kind, ref_id, ref_version FROM context_sources "
            "WHERE context_id = ?",
            (context_id,),
        ).fetchall():
            keys.add(
                (row["ref_kind"], row["ref_id"], int(row["ref_version"]))
            )
        for row in self._require_connection().execute(
            "SELECT source_kind, source_id, source_version FROM collab_source_links "
            "WHERE namespace = ? AND owner_kind = 'context' AND owner_id = ? "
            "AND owner_version = 0",
            (namespace, context_id),
        ).fetchall():
            keys.add(
                (row["source_kind"], row["source_id"], int(row["source_version"]))
            )
        return keys

    def _artifact_depth_locked(
        self,
        namespace: str,
        artifact_id: str,
        artifact_version: int,
        seen: set[tuple[str, int]],
    ) -> int:
        key = (artifact_id, artifact_version)
        if key in seen:
            raise CorruptStore("artifact source cycle")
        seen.add(key)
        if len(seen) > MAX_CLOSURE_DEPTH + 1:
            raise CorruptStore("source closure depth exceeded")
        rows = self._require_connection().execute(
            "SELECT source_kind, source_id, source_version FROM collab_source_links "
            "WHERE namespace = ? AND owner_kind = 'artifact' "
            "AND owner_id = ? AND owner_version = ?",
            (namespace, artifact_id, artifact_version),
        ).fetchall()
        best = 0
        for row in rows:
            if row["source_kind"] == COLLAB_ARTIFACT:
                best = max(
                    best,
                    1
                    + self._artifact_depth_locked(
                        namespace,
                        row["source_id"],
                        int(row["source_version"]),
                        seen,
                    ),
                )
        seen.remove(key)
        return best

    # -------------------------------------------------------------- barrier

    def _source_barred_locked(
        self,
        *,
        namespace: str,
        kind: str,
        ref_id: str,
        version: int,
        depth: int = 0,
    ) -> bool:
        """True when a source is inside a barrier, recursively for artifacts.

        record/event are whole-object targets (version ignored); collaboration
        targets match the exact version.  A chain deeper than 8 is a hard
        corruption, never a silent truncation.
        """

        if depth > MAX_CLOSURE_DEPTH:
            raise CorruptStore("source closure depth exceeded")
        target_version = 0 if kind in ("event", "record") else version
        row = self._require_connection().execute(
            "SELECT 1 FROM deletion_targets "
            "WHERE namespace = ? AND target_kind = ? AND target_id = ? "
            "AND target_version = ?",
            (namespace, kind, ref_id, target_version),
        ).fetchone()
        if row is not None:
            return True
        if kind in ("event", "record"):
            return False
        if kind == COLLAB_INPUT:
            return False
        if kind == COLLAB_ARTIFACT:
            rows = self._require_connection().execute(
                "SELECT source_kind, source_id, source_version "
                "FROM collab_source_links WHERE namespace = ? "
                "AND owner_kind = 'artifact' AND owner_id = ? AND owner_version = ?",
                (namespace, ref_id, version),
            ).fetchall()
            for link in rows:
                if self._source_barred_locked(
                    namespace=namespace,
                    kind=link["source_kind"],
                    ref_id=link["source_id"],
                    version=int(link["source_version"]),
                    depth=depth + 1,
                ):
                    return True
            return False
        raise CorruptStore(f"unknown stored source kind {kind!r}")

    def context_barred(self, *, namespace: str, context_id: str) -> bool:
        """True when any source this Context used is inside a barrier."""

        ns = identifiers.require_namespace(namespace)
        context = _require_uuid(context_id, "context_id")
        row = self._require_connection().execute(
            "SELECT 1 FROM context_snapshots WHERE context_id = ? AND namespace = ?",
            (context, ns),
        ).fetchone()
        if row is None:
            raise SnapshotNotFound(f"no persisted context {context!r}")
        for source in self._require_connection().execute(
            "SELECT ref_kind, ref_id, ref_version FROM context_sources "
            "WHERE context_id = ?",
            (context,),
        ).fetchall():
            if source["ref_kind"] not in ("event", "record"):
                continue
            if self._source_barred_locked(
                namespace=ns,
                kind=source["ref_kind"],
                ref_id=source["ref_id"],
                version=int(source["ref_version"]),
            ):
                return True
        for link in self._require_connection().execute(
            "SELECT source_kind, source_id, source_version FROM collab_source_links "
            "WHERE namespace = ? AND owner_kind = 'context' AND owner_id = ? "
            "AND owner_version = 0",
            (ns, context),
        ).fetchall():
            if self._source_barred_locked(
                namespace=ns,
                kind=link["source_kind"],
                ref_id=link["source_id"],
                version=int(link["source_version"]),
            ):
                return True
        return False

    def require_context_deliverable(self, context_id: str) -> None:
        """Raise ``REDACTED`` before a barred Context is decoded or sent."""

        context = _require_uuid(context_id, "context_id")
        row = self._require_connection().execute(
            "SELECT namespace FROM context_snapshots WHERE context_id = ?",
            (context,),
        ).fetchone()
        if row is None:
            raise SnapshotNotFound(f"no persisted context {context!r}")
        if self.context_barred(namespace=row["namespace"], context_id=context):
            raise Redacted(
                f"context {context} depends on a source inside a deletion barrier"
            )

    def collab_context_sources_barred(self, *, namespace: str, context_id: str) -> bool:
        """Barrier check used by the Tx B Context guard (raises nothing)."""

        ns = identifiers.require_namespace(namespace)
        context = _require_uuid(context_id, "context_id")
        for link in self._require_connection().execute(
            "SELECT source_kind, source_id, source_version FROM collab_source_links "
            "WHERE namespace = ? AND owner_kind = 'context' AND owner_id = ? "
            "AND owner_version = 0",
            (ns, context),
        ).fetchall():
            if self._source_barred_locked(
                namespace=ns,
                kind=link["source_kind"],
                ref_id=link["source_id"],
                version=int(link["source_version"]),
            ):
                return True
        return False

    def context_ids_for_collab_source(
        self, *, namespace: str, ref_kind: str, ref_id: str, ref_version: int
    ) -> tuple[str, ...]:
        """Contexts that linked this source (record/event ignore version)."""

        ns = identifiers.require_namespace(namespace)
        if ref_kind in ("event", "record"):
            rows = self._require_connection().execute(
                "SELECT DISTINCT owner_id FROM collab_source_links "
                "WHERE namespace = ? AND owner_kind = 'context' "
                "AND source_kind = ? AND source_id = ? ORDER BY owner_id",
                (ns, ref_kind, ref_id),
            ).fetchall()
        else:
            rows = self._require_connection().execute(
                "SELECT DISTINCT owner_id FROM collab_source_links "
                "WHERE namespace = ? AND owner_kind = 'context' "
                "AND source_kind = ? AND source_id = ? AND source_version = ? "
                "ORDER BY owner_id",
                (ns, ref_kind, ref_id, ref_version),
            ).fetchall()
        return tuple(str(row["owner_id"]) for row in rows)

    # --------------------------------------------------------------- delete

    def delete_experience(
        self,
        *,
        namespace: str,
        ref_type: str,
        ref_id: str,
        ref_version: int,
        authorization_ref: str,
    ) -> DeletionTarget:
        """Commit the barrier for one exact collaboration version."""

        self._require_standalone("delete_experience")
        ns = identifiers.require_namespace(namespace)
        if ref_type not in (COLLAB_INPUT, COLLAB_ARTIFACT):
            raise InvalidInput(
                f"ref_type must be one of {COLLAB_INPUT}, {COLLAB_ARTIFACT}"
            )
        identifier = _require_uuid(ref_id, "ref_id")
        version = _require_version(ref_version, "ref_version")
        authorization = _require_authorization_ref(authorization_ref)
        with self.transaction():
            self._require_source_exists_locked(ns, ref_type, identifier, version)
            return self._commit_barrier_locked(
                namespace=ns,
                target_kind=ref_type,
                target_id=identifier,
                target_version=version,
                authorization_ref=authorization,
            )

    # -------------------------------------------------------------- cleanup

    def _payload_refs_for_target(self, target: DeletionTarget) -> tuple[str, ...]:
        if target.target_kind == COLLAB_INPUT:
            rows = self._require_connection().execute(
                "SELECT body_payload_ref FROM collab_inputs "
                "WHERE namespace = ? AND input_id = ? AND content_version = ?",
                (target.namespace, target.target_id, target.target_version),
            ).fetchall()
        elif target.target_kind == COLLAB_ARTIFACT:
            rows = self._require_connection().execute(
                "SELECT body_payload_ref, proposal_payload_ref FROM collab_artifacts "
                "WHERE namespace = ? AND artifact_id = ? AND artifact_version = ?",
                (target.namespace, target.target_id, target.target_version),
            ).fetchall()
        else:
            return super()._payload_refs_for_target(target)  # type: ignore[misc]
        if not rows:
            raise CorruptStore(
                f"deletion target {target.target_kind} {target.target_id} "
                f"version {target.target_version} has no stored row"
            )
        seen: set[str] = set()
        refs: list[str] = []
        for row in rows:
            for ref in row:
                if ref is not None and ref not in seen:
                    seen.add(ref)
                    refs.append(str(ref))
        return tuple(refs)

    def _payload_is_live_referenced_locked(self, payload_ref: str) -> bool:
        connection = self._require_connection()
        input_hit = connection.execute(
            "SELECT 1 FROM collab_inputs ci WHERE ci.body_payload_ref = ? "
            "AND NOT EXISTS (SELECT 1 FROM deletion_targets dt "
            "  WHERE dt.target_kind = 'collab_input' AND dt.namespace = ci.namespace "
            "    AND dt.target_id = ci.input_id "
            "    AND dt.target_version = ci.content_version) LIMIT 1",
            (payload_ref,),
        ).fetchone()
        if input_hit is not None:
            return True
        artifact_hit = connection.execute(
            "SELECT 1 FROM collab_artifacts ca "
            "WHERE (ca.body_payload_ref = ? OR ca.proposal_payload_ref = ?) "
            "AND NOT EXISTS (SELECT 1 FROM deletion_targets dt "
            "  WHERE dt.target_kind = 'collab_artifact' AND dt.namespace = ca.namespace "
            "    AND dt.target_id = ca.artifact_id "
            "    AND dt.target_version = ca.artifact_version) LIMIT 1",
            (payload_ref, payload_ref),
        ).fetchone()
        if artifact_hit is not None:
            return True
        return super()._payload_is_live_referenced_locked(payload_ref)  # type: ignore[misc]

    def _after_target_cleared_locked(self, target: DeletionTarget) -> int:
        cleared = super()._after_target_cleared_locked(target)  # type: ignore[misc]
        return cleared + self._clear_collab_dependents_locked(target)

    def _clear_collab_dependents_locked(self, target: DeletionTarget) -> int:
        connection = self._require_connection()
        cleared = 0
        visited_artifacts: set[tuple[str, int]] = set()
        visited_contexts: set[str] = set()
        queue: list[tuple[str, str, int, int]] = [
            (target.target_kind, target.target_id, target.target_version, 0)
        ]
        while queue:
            kind, source_id, source_version, depth = queue.pop(0)
            if depth > MAX_CLOSURE_DEPTH:
                raise CorruptStore("source closure depth exceeded")
            if kind in ("event", "record"):
                links = connection.execute(
                    "SELECT owner_kind, owner_id, owner_version "
                    "FROM collab_source_links WHERE namespace = ? "
                    "AND source_kind = ? AND source_id = ?",
                    (target.namespace, kind, source_id),
                ).fetchall()
            else:
                links = connection.execute(
                    "SELECT owner_kind, owner_id, owner_version "
                    "FROM collab_source_links WHERE namespace = ? "
                    "AND source_kind = ? AND source_id = ? AND source_version = ?",
                    (target.namespace, kind, source_id, source_version),
                ).fetchall()
            for link in links:
                if link["owner_kind"] == "context":
                    context_id = link["owner_id"]
                    if context_id in visited_contexts:
                        continue
                    visited_contexts.add(context_id)
                    row = connection.execute(
                        "SELECT rendered_input_payload_ref FROM context_snapshots "
                        "WHERE context_id = ? AND namespace = ?",
                        (context_id, target.namespace),
                    ).fetchone()
                    if row is None:
                        raise CorruptStore(
                            f"source link names missing context {context_id}"
                        )
                    if row["rendered_input_payload_ref"] is not None:
                        cleared += self._clear_payload_locked(
                            row["rendered_input_payload_ref"]
                        )
                    continue
                owner_version = int(link["owner_version"])
                key = (link["owner_id"], owner_version)
                if key in visited_artifacts:
                    continue
                visited_artifacts.add(key)
                artifact = connection.execute(
                    "SELECT body_payload_ref, proposal_payload_ref, kind, status "
                    "FROM collab_artifacts WHERE namespace = ? AND artifact_id = ? "
                    "AND artifact_version = ?",
                    (target.namespace, link["owner_id"], owner_version),
                ).fetchone()
                if artifact is None:
                    raise CorruptStore(
                        f"source link names missing artifact {link['owner_id']}"
                    )
                cleared += self._clear_payload_locked(artifact["body_payload_ref"])
                if artifact["proposal_payload_ref"] is not None:
                    cleared += self._clear_payload_locked(
                        artifact["proposal_payload_ref"]
                    )
                if artifact["kind"] == "candidate" and artifact["status"] == "pending":
                    connection.execute(
                        "UPDATE collab_artifacts SET status = 'stale' "
                        "WHERE namespace = ? AND artifact_id = ? "
                        "AND artifact_version = ? AND status = 'pending'",
                        (target.namespace, link["owner_id"], owner_version),
                    )
                queue.append(
                    (COLLAB_ARTIFACT, link["owner_id"], owner_version, depth + 1)
                )
        return cleared

    def _clear_payload_locked(self, payload_ref: str) -> int:
        cursor = self._require_connection().execute(
            "UPDATE private_payloads SET body_json = '', sha256 = ? "
            "WHERE payload_ref = ? AND body_json <> ''",
            (uuid.uuid4().hex + uuid.uuid4().hex[:32], payload_ref),
        )
        return 1 if cursor.rowcount else 0

    # ------------------------------------------------------------- work/grant

    def register_work_control(
        self,
        *,
        namespace: str,
        work_id: str,
        generation: int = 0,
        blocked_after_rejection: bool = False,
        control_version: int = 1,
        rerun_authorization_ref: str | None = None,
        block_reason: str | None = None,
        last_candidate_ref: str | None = None,
        updated_at: str | None = None,
    ) -> WorkControl:
        ns = identifiers.require_namespace(namespace)
        work = _require_uuid(work_id, "work_id")
        gen = _require_generation(generation, "generation")
        if type(control_version) is not int or control_version < 1:
            raise InvalidInput("control_version must be a positive integer")
        if block_reason is not None and block_reason != "rejected":
            raise InvalidInput("block_reason must be null or 'rejected'")
        rerun = (
            None
            if rerun_authorization_ref is None
            else _require_text(rerun_authorization_ref, "rerun_authorization_ref", 256)
        )
        last = (
            None
            if last_candidate_ref is None
            else _require_uuid(last_candidate_ref, "last_candidate_ref")
        )
        now = utc_now_rfc3339() if updated_at is None else _require_timestamp(
            updated_at, "updated_at"
        )
        with self.transaction():
            try:
                self._require_connection().execute(
                    "INSERT INTO collab_work_control (" + _WORK_COLUMNS + ") "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ns,
                        work,
                        COLLAB_SCHEMA_VERSION,
                        gen,
                        1 if blocked_after_rejection else 0,
                        control_version,
                        rerun,
                        block_reason,
                        now,
                        last,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidInput(
                    f"work control for {work} already exists"
                ) from exc
        return WorkControl(
            namespace=ns,
            work_id=work,
            generation=gen,
            blocked_after_rejection=blocked_after_rejection,
            control_version=control_version,
            rerun_authorization_ref=rerun,
            block_reason=block_reason,
            updated_at=now,
            last_candidate_ref=last,
        )

    def work_control(self, *, namespace: str, work_id: str) -> WorkControl | None:
        ns = identifiers.require_namespace(namespace)
        work = _require_uuid(work_id, "work_id")
        row = self._require_connection().execute(
            f"SELECT {_WORK_COLUMNS} FROM collab_work_control "
            "WHERE namespace = ? AND work_id = ?",
            (ns, work),
        ).fetchone()
        return None if row is None else _work_control_from_row(row)

    def register_grant(self, grant: Mapping[str, Any]) -> GrantRow:
        """Insert one of the five mutually exclusive grant branches."""

        value = _require_mapping(grant, "grant")
        grant_id = _require_uuid(value.get("grant_id"), "grant_id")
        ns = identifiers.require_namespace(value.get("namespace"))
        schema_version = _require_text(
            value.get("schema_version", COLLAB_SCHEMA_VERSION),
            "schema_version",
            128,
        )
        kind = value.get("kind")
        if kind not in ("access", "egress", "commit", "rerun"):
            raise InvalidInput("grant kind must be access, egress, commit or rerun")
        grant_version = _require_version(value.get("grant_version"), "grant_version")
        subject = _require_text(value.get("subject"), "subject", 256)
        authorization = _require_text(
            value.get("authorization_ref"), "authorization_ref", MAX_GRANT_TEXT
        )
        issued = _require_timestamp(value.get("issued_at"), "issued_at")
        expires = _nullable_timestamp(value.get("expires_at"), "expires_at")
        revoked = _nullable_timestamp(value.get("revoked_at"), "revoked_at")

        branch = set(value)
        common = {
            "grant_id",
            "namespace",
            "schema_version",
            "kind",
            "grant_version",
            "subject",
            "authorization_ref",
            "issued_at",
            "expires_at",
            "revoked_at",
        }
        allowed = {
            "access": common | {"purposes", "data_classes"},
            "egress": common
            | {
                "provider",
                "endpoint",
                "model",
                "manifest_hash",
                "privacy_classes",
                "purposes",
                "model_authorization_ref",
            },
            "commit": common
            | {"scope", "operations", "trigger_kinds", "work_id", "generation"},
            "rerun": common | {"work_id", "generation"},
        }[kind]
        if not branch.issubset(allowed):
            raise InvalidInput("grant branch carries fields from another branch")

        scope = value.get("scope")
        purposes: tuple[str, ...] = ()
        data_classes: tuple[str, ...] = ()
        provider = endpoint = model = manifest_hash = None
        privacy_classes: tuple[str, ...] = ()
        model_authorization_ref = None
        operations: tuple[str, ...] = ()
        trigger_kinds: tuple[str, ...] = ()
        work_id = generation = None

        if kind == "access":
            purposes = _text_list(value.get("purposes"), "purposes", 16)
            data_classes = _privacy_list(value.get("data_classes"), "data_classes")
        elif kind == "egress":
            provider = _require_text(value.get("provider"), "provider", 128)
            endpoint = _require_text(value.get("endpoint"), "endpoint", 512)
            model = _require_text(value.get("model"), "model", 256)
            manifest_hash = _require_text(
                value.get("manifest_hash"), "manifest_hash", 71
            )
            if (
                len(manifest_hash) != 71
                or not manifest_hash.startswith("sha256:")
                or any(ch not in "0123456789abcdef" for ch in manifest_hash[7:])
            ):
                raise InvalidInput("manifest_hash must be sha256:<64 lower-case hex>")
            privacy_classes = _privacy_list(
                value.get("privacy_classes"), "privacy_classes"
            )
            purposes = _text_list(value.get("purposes"), "purposes", 16)
            model_authorization_ref = _require_uuid(
                value.get("model_authorization_ref"), "model_authorization_ref"
            )
        elif kind == "commit":
            if scope == "policy":
                if "work_id" in value or "generation" in value:
                    raise InvalidInput("a policy commit grant has no work binding")
                operations = _text_list(value.get("operations"), "operations", 32)
                trigger_kinds = _text_list(
                    value.get("trigger_kinds"), "trigger_kinds", 16
                )
            elif scope == "work_generation":
                if "operations" in value or "trigger_kinds" in value:
                    raise InvalidInput(
                        "a work_generation commit grant has no policy lists"
                    )
                work_id = _require_uuid(value.get("work_id"), "work_id")
                generation = _require_generation(value.get("generation"), "generation")
            else:
                raise InvalidInput("a commit grant must name its scope")
        else:
            if "scope" in value:
                raise InvalidInput("a rerun grant has no scope")
            work_id = _require_uuid(value.get("work_id"), "work_id")
            generation = _require_generation(value.get("generation"), "generation")

        with self.transaction():
            try:
                self._require_connection().execute(
                    "INSERT INTO collab_grants (" + _GRANT_COLUMNS + ") "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        grant_id,
                        ns,
                        schema_version,
                        kind,
                        grant_version,
                        subject,
                        authorization,
                        issued,
                        expires,
                        revoked,
                        scope,
                        None if not purposes else canonical_json_text(list(purposes)),
                        None
                        if not data_classes
                        else canonical_json_text(list(data_classes)),
                        provider,
                        endpoint,
                        model,
                        manifest_hash,
                        None
                        if not privacy_classes
                        else canonical_json_text(list(privacy_classes)),
                        model_authorization_ref,
                        None
                        if not operations
                        else canonical_json_text(list(operations)),
                        None
                        if not trigger_kinds
                        else canonical_json_text(list(trigger_kinds)),
                        work_id,
                        generation,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidInput(
                    f"grant {grant_id} violates its branch constraints"
                ) from exc
        loaded = self.load_grant(namespace=ns, grant_id=grant_id)
        if loaded is None:  # pragma: no cover - just inserted
            raise CorruptStore("a freshly inserted grant cannot be read back")
        return loaded

    def load_grant(self, *, namespace: str, grant_id: str) -> GrantRow | None:
        ns = identifiers.require_namespace(namespace)
        identifier = _require_uuid(grant_id, "grant_id")
        row = self._require_connection().execute(
            f"SELECT {_GRANT_COLUMNS} FROM collab_grants "
            "WHERE namespace = ? AND grant_id = ?",
            (ns, identifier),
        ).fetchone()
        return None if row is None else _grant_from_row(row)

    def revoke_grant(
        self, *, namespace: str, grant_id: str, revoked_at: str | None = None
    ) -> GrantRow:
        ns = identifiers.require_namespace(namespace)
        identifier = _require_uuid(grant_id, "grant_id")
        timestamp = utc_now_rfc3339() if revoked_at is None else _require_timestamp(
            revoked_at, "revoked_at"
        )
        with self.transaction():
            cursor = self._require_connection().execute(
                "UPDATE collab_grants SET revoked_at = ? "
                "WHERE namespace = ? AND grant_id = ?",
                (timestamp, ns, identifier),
            )
            if cursor.rowcount != 1:
                raise RecordNotFound(f"no grant {identifier} in {ns!r}")
        loaded = self.load_grant(namespace=ns, grant_id=identifier)
        if loaded is None:  # pragma: no cover
            raise CorruptStore("a revoked grant cannot be read back")
        return loaded

    # -------------------------------------------------------------- decisions

    def _review_start_decision(
        self, control: WorkControl, grant: GrantRow | None
    ) -> Decision:
        """Pure core: unblocked work, or a matching rerun grant. Nothing else."""

        if not control.blocked_after_rejection:
            return Decision(True, "not_blocked", control.generation)
        if (
            grant is not None
            and grant.kind == "rerun"
            and grant.namespace == control.namespace
            and grant.work_id == control.work_id
            and grant.generation == control.generation
        ):
            return Decision(True, "rerun_grant", control.generation)
        return Decision(False, "blocked_after_rejection", control.generation)

    def _commit_decision(
        self,
        candidate: ArtifactRow,
        control: WorkControl,
        grant: GrantRow | None,
        approval: Approval | None,
        operation: str | None,
        trigger_kind: str | None,
    ) -> Decision:
        """Pure core: current pending candidate and exactly one authority."""

        generation = control.generation
        if (
            candidate.kind != "candidate"
            or candidate.status != "pending"
            or candidate.work_generation != generation
        ):
            return Decision(False, "candidate_not_current", generation)
        if (
            approval is not None
            and approval.candidate_id == candidate.artifact_id
            and approval.proposal_digest == candidate.proposal_digest
            and approval.context_id == candidate.context_id
            and approval.request_id == candidate.request_id
        ):
            return Decision(True, "manual_approval", generation)
        if grant is not None and grant.kind == "commit":
            if grant.scope == "work_generation":
                if (
                    grant.work_id == candidate.work_id
                    and grant.generation == candidate.work_generation
                ):
                    return Decision(True, "commit_grant", generation)
            elif grant.scope == "policy":
                if operation in grant.operations and trigger_kind in grant.trigger_kinds:
                    return Decision(True, "commit_grant", generation)
        return Decision(False, "no_commit_authority", generation)

    def review_start_decision(
        self, *, namespace: str, work_id: str, now: str | None = None
    ) -> Decision:
        """Start gate.

        A work item with no control row has never been rejected, so it is not
        blocked: the gate allows it at generation 0.  Only a committed
        rejection can create a barrier.
        """

        ns = identifiers.require_namespace(namespace)
        work = _require_uuid(work_id, "work_id")
        now = utc_now_rfc3339() if now is None else _require_timestamp(now, "now")
        with self.read_snapshot():
            control = self.work_control(namespace=ns, work_id=work)
            if control is None:
                return Decision(True, "not_blocked", 0)
            grant = None
            if control.rerun_authorization_ref is not None:
                loaded = self.load_grant(
                    namespace=ns, grant_id=control.rerun_authorization_ref
                )
                if loaded is not None and _grant_active(loaded, now):
                    grant = loaded
            return self._review_start_decision(control, grant)

    def commit_decision(
        self,
        *,
        namespace: str,
        candidate_id: str,
        approval: Approval | Mapping[str, Any] | None = None,
        operation: str | None = None,
        trigger_kind: str | None = None,
        now: str | None = None,
    ) -> Decision:
        """Commit gate.

        A missing control row is an integrity guard here, not an "unblocked"
        state: without it the candidate cannot be proven current.
        """

        ns = identifiers.require_namespace(namespace)
        identifier = _require_uuid(candidate_id, "candidate_id")
        normalized_approval: Approval | None
        if approval is None:
            normalized_approval = None
        elif isinstance(approval, Approval):
            normalized_approval = approval
        else:
            normalized_approval = Approval(
                candidate_id=_require_uuid(
                    approval.get("candidate_id"), "approval.candidate_id"
                ),
                proposal_digest=_require_text(
                    approval.get("proposal_digest"), "approval.proposal_digest", 71
                ),
                context_id=_require_uuid(
                    approval.get("context_id"), "approval.context_id"
                ),
                request_id=_require_uuid(
                    approval.get("request_id"), "approval.request_id"
                ),
            )
        now = utc_now_rfc3339() if now is None else _require_timestamp(now, "now")
        with self.read_snapshot():
            candidate = self.load_artifact(
                namespace=ns, artifact_id=identifier, artifact_version=1
            )
            if candidate.kind != "candidate" or candidate.work_id is None:
                return Decision(False, "not_a_candidate", candidate.work_generation or 0)
            control = self.work_control(
                namespace=ns, work_id=candidate.work_id
            )
            if control is None:
                return Decision(
                    False,
                    "no_work_control",
                    candidate.work_generation or 0,
                )
            grant = None
            if candidate.commit_authorization_ref is not None:
                loaded = self.load_grant(
                    namespace=ns, grant_id=candidate.commit_authorization_ref
                )
                if loaded is not None and _grant_active(loaded, now):
                    grant = loaded
            return self._commit_decision(
                candidate,
                control,
                grant,
                normalized_approval,
                operation,
                trigger_kind,
            )


# --------------------------------------------------------------------------
# row decoding
# --------------------------------------------------------------------------


def _grant_active(grant: GrantRow, now: str | None) -> bool:
    if grant.revoked_at is not None:
        return False
    if now is not None and grant.expires_at is not None:
        return grant.expires_at > now
    return True


def _work_control_from_row(row: Any) -> WorkControl:
    return WorkControl(
        namespace=row["namespace"],
        work_id=row["work_id"],
        generation=int(row["generation"]),
        blocked_after_rejection=bool(int(row["blocked_after_rejection"])),
        control_version=int(row["control_version"]),
        rerun_authorization_ref=row["rerun_authorization_ref"],
        block_reason=row["block_reason"],
        updated_at=row["updated_at"],
        last_candidate_ref=row["last_candidate_ref"],
    )


def _grant_from_row(row: Any) -> GrantRow:
    def array(name: str) -> tuple[str, ...]:
        value = _decode_json(row[name], name)
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise CorruptStore(f"stored {name} is not a string array")
        return tuple(value)

    return GrantRow(
        grant_id=row["grant_id"],
        namespace=row["namespace"],
        schema_version=row["schema_version"],
        kind=row["kind"],
        grant_version=int(row["grant_version"]),
        subject=row["subject"],
        authorization_ref=row["authorization_ref"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        scope=row["scope"],
        purposes=array("purposes_json"),
        data_classes=array("data_classes_json"),
        provider=row["provider"],
        endpoint=row["endpoint"],
        model=row["model"],
        manifest_hash=row["manifest_hash"],
        privacy_classes=array("privacy_classes_json"),
        model_authorization_ref=row["model_authorization_ref"],
        operations=array("operations_json"),
        trigger_kinds=array("trigger_kinds_json"),
        work_id=row["work_id"],
        generation=(
            None if row["generation"] is None else int(row["generation"])
        ),
    )


__all__ = [
    "ACTIVATION_KINDS",
    "ARTIFACT_KINDS",
    "Approval",
    "ArtifactRow",
    "CANDIDATE_STATUSES",
    "COLLAB_ARTIFACT",
    "COLLAB_INPUT",
    "COLLAB_SCHEMA_VERSION",
    "COLLAB_SOURCE_KINDS",
    "CollaborationMixin",
    "DECIDED_STATUSES",
    "Decision",
    "GrantRow",
    "INPUT_ORIGIN_KINDS",
    "INTERACTION_MODES",
    "InputRow",
    "MAX_BODY_CHARS",
    "MAX_CLOSURE_DEPTH",
    "ORIGIN_KINDS",
    "RETENTION_ORDER",
    "RETENTION_RANK",
    "WorkControl",
]
