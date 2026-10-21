"""A group: the minimal PersonaProfile lifecycle.

A PersonaProfile is a unified Record with ``kind='persona_profile'``.  It is not
a separate table, and publishing never touches the active pointer:

* ``publish_profile`` appends one immutable version and advances
  ``state_revision`` exactly once (J.7: a new domain record is a domain commit),
  but leaves ``active_profile_*`` alone.  The deliberate consequence is that a
  Context captured before publishing is conservatively stale afterwards.
* ``activate_profile`` accepts one exact ``(record_id, record_version)`` that
  already exists; it never picks "the newest".  It advances ``state_revision``
  exactly once and is the only way the active pointer moves.  Rolling back is
  just activating an older version.
* ``get_active_profile`` always returns the same shape.  No active version is
  reported as ``active=False, degraded=True`` (neutral), never by silently
  choosing one.

The review reference is verified semantically, not just syntactically: the
event must exist in the same namespace, must not be inside a deletion barrier,
must have been ingested through the trusted user entry (``source_kind =
user_report``) and must carry the frozen review event type.  A synthetic or
model-produced event cannot stand in for a user approval.

File layout follows the persona guide: this module lives in
``domains/persona/`` and only this card's slice (profiles) is implemented;
interaction state, dispositions, rendering and feedback are later cards.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ...foundation import identifiers
from ...foundation.errors import (
    CorruptStore,
    InvalidInput,
    RecordKindMismatch,
)
from ...foundation.jsonutil import canonical_json_text
from ...store.domain import DomainStore, require_authorization_ref
from ...store.records import RecordVersion
from ..errors import SourceUntrusted
from ..refs import EventRef, RecordRef

PERSONA_PROFILE_KIND = "persona_profile"

#: Body schema version this card writes and validates.
PROFILE_SCHEMA_VERSION = "1"

#: Frozen audit constants for the trusted user review entry (this card).
REVIEW_APPROVE_EVENT_TYPE = "persona.review.approve"
REVIEW_SOURCE_KIND = "user_report"

PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

#: Fixed namespace for the deterministic UUIDv5 record identity of a profile.
#: A stable record ID per (namespace, profile_id) is what makes "publish a new
#: version" possible without a profile-id lookup table (the card forbids a
#: separate persona table).  The derivation is documented, not a guess.
PROFILE_RECORD_NAMESPACE = uuid.UUID("0d7c0b6e-6a4e-5f2a-9c31-7b1f0a5e9d42")

#: J.2/§3.1 top-level profile body keys.  Unknown keys are rejected while
#: ``schema_version`` is "1": the body is a versioned asset, not an open bag.
PROFILE_BODY_KEYS = frozenset(
    {
        "profile_id",
        "profile_version",
        "identity_summary",
        "principles",
        "style",
        "attention_preferences",
        "examples",
        "asset_hash",
        "review_ref",
        "renderer_compatibility",
    }
)


@dataclass(frozen=True)
class ActiveProfileState:
    """Uniform answer of ``get_active_profile``; neutral is explicit."""

    namespace: str
    active: bool
    degraded: bool
    ref: RecordRef | None


def require_profile_id(value: Any) -> str:
    if not isinstance(value, str) or PROFILE_ID_PATTERN.fullmatch(value) is None:
        raise InvalidInput(
            "profile_id must match ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
        )
    return value


def profile_record_id(namespace: str, profile_id: str) -> str:
    """Deterministic record ID for one logical profile within a namespace."""

    ns = identifiers.require_namespace(namespace)
    pid = require_profile_id(profile_id)
    return str(uuid.uuid5(PROFILE_RECORD_NAMESPACE, f"{ns}\x1f{pid}"))


class PersonaProfileService:
    """Profile publish / activate / read for one PCS store."""

    def __init__(self, store: DomainStore):
        self._store = store

    # ---------------------------------------------------------------- publish

    def publish_profile(
        self,
        *,
        namespace: str,
        profile_id: str,
        body: Mapping[str, Any],
        review_ref: Mapping[str, Any],
        authorization_ref: str,
    ) -> RecordVersion:
        """Append one immutable Profile version; never change the active pointer.

        ``state_revision`` advances exactly once (the new version is a domain
        record).  ``active_profile_*`` is untouched, so the currently active
        version does not drift.
        """

        ns = identifiers.require_namespace(namespace)
        pid = require_profile_id(profile_id)
        authorization = require_authorization_ref(authorization_ref)
        ref = EventRef.from_value(review_ref, field="review_ref")
        if ref.namespace != ns:
            raise SourceUntrusted(
                f"review_ref belongs to namespace {ref.namespace!r}, not {ns!r}"
            )
        profile_body = validate_profile_body(
            body, profile_id=pid, review_ref=ref
        )
        record_id = profile_record_id(ns, pid)

        self._store.require_standalone("publish_profile")
        with self._store.transaction():
            self._verify_review_event(ns, ref)
            pointer = self._store.record_pointer(ns, record_id)
            if pointer is None:
                version = 1
            else:
                if pointer.kind != PERSONA_PROFILE_KIND:
                    raise RecordKindMismatch(
                        f"record {record_id} is kind {pointer.kind!r}, not a profile"
                    )
                version = pointer.current_version + 1
            appended = self._store.append_record_version(
                namespace=ns,
                record_id=record_id,
                record_version=version,
                kind=PERSONA_PROFILE_KIND,
                body=profile_body,
                status="active",
                schema_version=PROFILE_SCHEMA_VERSION,
                scope=None,
            )
            self._store.record_authorization(
                namespace=ns,
                operation="persona.profile.publish",
                target_kind="record",
                target_id=record_id,
                target_version=version,
                authorization_ref=authorization,
            )
            return appended

    def _verify_review_event(self, namespace: str, ref: EventRef) -> None:
        """Verify the review event's provenance, not just the reference shape.

        ``get_event`` inherits the PCS2-006 read barrier, so a deleted review
        event raises ``REDACTED`` before any write.  A missing event raises
        ``NOT_FOUND``.  Both are left as-is: the caller must not be told the
        review happened when it cannot be shown.
        """

        event = self._store.get_event(namespace, ref.event_id)
        if event.source_kind != REVIEW_SOURCE_KIND:
            raise SourceUntrusted(
                f"review event {ref.event_id} has source_kind "
                f"{event.source_kind!r}; a user report is required"
            )
        if event.event_type != REVIEW_APPROVE_EVENT_TYPE:
            raise SourceUntrusted(
                f"review event {ref.event_id} has event_type "
                f"{event.event_type!r}; {REVIEW_APPROVE_EVENT_TYPE!r} is required"
            )

    # --------------------------------------------------------------- activate

    def activate_profile(
        self,
        *,
        namespace: str,
        record_id: str,
        record_version: int,
        authorization_ref: str,
    ) -> None:
        """Make one exact existing Profile version active (rollback included).

        The version is never inferred.  A missing version is ``NOT_FOUND``; a
        record of another kind is ``INVALID_TRANSITION``; a deleted version is
        ``REDACTED``.  Each explicit activation commits one ``state_revision``.
        """

        ns = identifiers.require_namespace(namespace)
        rid = identifiers.require_record_id(record_id)
        ver = identifiers.require_record_version(record_version)
        authorization = require_authorization_ref(authorization_ref)

        self._store.require_standalone("activate_profile")
        with self._store.transaction():
            self._store.set_active_profile(
                namespace=ns,
                record_id=rid,
                record_version=ver,
                required_kind=PERSONA_PROFILE_KIND,
            )
            self._store.record_authorization(
                namespace=ns,
                operation="persona.profile.activate",
                target_kind="record",
                target_id=rid,
                target_version=ver,
                authorization_ref=authorization,
            )

    # ------------------------------------------------------------------ read

    def get_active_profile(self, namespace: str) -> ActiveProfileState:
        """Return the exact active Profile version, or an explicit neutral.

        A deleted active version is not reported as neutral: the deletion
        barrier wins and ``REDACTED`` propagates, because claiming "no profile"
        would hide a deletion decision.
        """

        ns = identifiers.require_namespace(namespace)
        with self._store.read_snapshot():
            pointer = self._store.active_profile_pointer(ns)
            if pointer.record_id is None or pointer.record_version is None:
                return ActiveProfileState(
                    namespace=ns, active=False, degraded=True, ref=None
                )
            version = self._store.get_version(
                ns, pointer.record_id, pointer.record_version
            )
        if version.kind != PERSONA_PROFILE_KIND:
            raise CorruptStore(
                f"active pointer of namespace {ns!r} names a {version.kind!r} record"
            )
        return ActiveProfileState(
            namespace=ns,
            active=True,
            degraded=False,
            ref=RecordRef(
                namespace=ns,
                record_id=pointer.record_id,
                record_version=pointer.record_version,
            ),
        )


# --------------------------------------------------------------- body checks


def validate_profile_body(
    body: Any, *, profile_id: str, review_ref: EventRef
) -> dict[str, Any]:
    """Validate the minimal §3.1 Profile body this card publishes.

    This is a shape check for ``schema_version == "1"``, not a calibrated
    persona review.  It never reads or rewrites the real persona assets: the
    tests build synthetic bodies.
    """

    if not isinstance(body, Mapping):
        raise InvalidInput("profile body must be a JSON object")
    keys = set(body)
    missing = sorted(PROFILE_BODY_KEYS - keys)
    if missing:
        raise InvalidInput(f"profile body is missing required field(s): {', '.join(missing)}")
    unknown = sorted(str(key) for key in keys - PROFILE_BODY_KEYS)
    if unknown:
        raise InvalidInput(f"profile body has unknown field(s): {', '.join(unknown)}")

    if not isinstance(body["profile_id"], str) or body["profile_id"] != profile_id:
        raise InvalidInput("profile body profile_id must equal the published profile_id")
    _require_text(body["profile_version"], "profile_version", 128)
    _require_text(body["identity_summary"], "identity_summary", 8192)
    _require_text_list(body["principles"], "principles", 256, 2048)
    _validate_style(body["style"])
    _validate_attention_preferences(body["attention_preferences"])
    _validate_examples(body["examples"])
    _require_text(body["asset_hash"], "asset_hash", 256)
    _require_text(body["renderer_compatibility"], "renderer_compatibility", 128)

    body_review_ref = EventRef.from_value(body["review_ref"], field="body.review_ref")
    if body_review_ref != review_ref:
        raise InvalidInput(
            "profile body review_ref must be the review event that was verified"
        )

    document = dict(body)
    # Reject non-JSON values (bytes, NaN, ...) before a transaction starts.
    canonical_json_text(document)
    return document


def _require_text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"profile body {field} must be a non-empty string")
    if len(value) > max_length:
        raise InvalidInput(f"profile body {field} must be at most {max_length} characters")
    return value


def _require_text_list(
    value: Any, field: str, max_items: int, max_length: int
) -> list[str]:
    if not isinstance(value, list):
        raise InvalidInput(f"profile body {field} must be an array of strings")
    if len(value) > max_items:
        raise InvalidInput(f"profile body {field} must have at most {max_items} items")
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise InvalidInput(
                f"profile body {field}[{index}] must be a non-empty string"
            )
        if len(item) > max_length:
            raise InvalidInput(
                f"profile body {field}[{index}] must be at most {max_length} characters"
            )
    return value


def _validate_style(style: Any) -> None:
    if not isinstance(style, Mapping):
        raise InvalidInput("profile body style must be a JSON object")
    _require_text(style.get("language"), "style.language", 128)
    _require_text(style.get("tone"), "style.tone", 128)
    verbosity = style.get("verbosity_by_context")
    if not isinstance(verbosity, Mapping):
        raise InvalidInput("profile body style.verbosity_by_context must be an object")
    for context in ("casual", "technical"):
        _require_text(
            verbosity.get(context), f"style.verbosity_by_context.{context}", 64
        )


def _validate_attention_preferences(value: Any) -> None:
    if not isinstance(value, list):
        raise InvalidInput(
            "profile body attention_preferences must be an array of objects"
        )
    if len(value) > 256:
        raise InvalidInput(
            "profile body attention_preferences must have at most 256 items"
        )
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or not item:
            raise InvalidInput(
                f"profile body attention_preferences[{index}] must be a non-empty object"
            )


def _validate_examples(value: Any) -> None:
    if not isinstance(value, list):
        raise InvalidInput("profile body examples must be an array")
    if len(value) > 64:
        raise InvalidInput("profile body examples must have at most 64 items")
    keys = {"example_id", "scene", "user", "assistant", "synthetic"}
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise InvalidInput(f"profile body examples[{index}] must be an object")
        if set(item) != keys:
            raise InvalidInput(
                f"profile body examples[{index}] must have exactly "
                "example_id, scene, user, assistant and synthetic"
            )
        _require_text(item["example_id"], f"examples[{index}].example_id", 128)
        _require_text(item["scene"], f"examples[{index}].scene", 1024)
        _require_text(item["user"], f"examples[{index}].user", 8192)
        _require_text(item["assistant"], f"examples[{index}].assistant", 8192)
        if type(item["synthetic"]) is not bool:
            raise InvalidInput(
                f"profile body examples[{index}].synthetic must be a boolean"
            )


__all__ = [
    "ActiveProfileState",
    "PERSONA_PROFILE_KIND",
    "PROFILE_BODY_KEYS",
    "PROFILE_RECORD_NAMESPACE",
    "PROFILE_SCHEMA_VERSION",
    "PersonaProfileService",
    "REVIEW_APPROVE_EVENT_TYPE",
    "REVIEW_SOURCE_KIND",
    "profile_record_id",
    "require_profile_id",
    "validate_profile_body",
]
