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
    Redacted,
)
from ...foundation.jsonutil import canonical_json_text, sha256_hex
from ...store.domain import DomainStore, require_authorization_ref
from ...store.records import RecordVersion
from ..errors import SourceUntrusted
from ..refs import EventRef, RecordRef
from .policy import (
    CANDIDATE_KIND,
    MAX_POLICY_TEXT_CHARS,
    POLICY_SCHEMA_VERSION,
    PUBLISHED_KIND,
    policy_record_id,
    policy_text_sha256,
    validate_policy_body,
)

#: Authorization operation recorded when a published policy is written together
#: with its profile.  Kept distinct from ``persona.profile.publish`` so the audit
#: can tell "a persona version was published" from "so was its expression text".
POLICY_PUBLISH_OPERATION = "persona.policy.publish"

#: The Lab-only seed operation; it never creates a published asset.
POLICY_SEED_OPERATION = "persona.policy.seed"

#: The promotion operation; it creates the published asset and never edits the
#: candidate it was copied from.
POLICY_PROMOTE_OPERATION = "persona.policy.promote"

PERSONA_PROFILE_KIND = "persona_profile"

#: Body schema version this card writes and validates.  RG06 added the optional
#: ``examples[].pre_tool_commentary`` (the public short line spoken before a
#: tool call).  The clean v8 profile adds the optional
#: ``examples[].assistant_messages`` (the ordered bubbles of one final reply);
#: it is schema "3".  Older bodies stay valid and keep their exact shape.
PROFILE_SCHEMA_VERSION = "3"
PROFILE_SCHEMA_VERSIONS = ("1", "2", "3")

#: Frozen audit constants for the trusted user review entry (this card).
REVIEW_APPROVE_EVENT_TYPE = "persona.review.approve"
REVIEW_SOURCE_KIND = "user_report"

#: Upper bound for one full profile body the desktop host will show or publish
#: through the reviewed management path.  A body above this is refused, never
#: truncated: the manager either sees the exact stored text or an explicit error.
PROFILE_BODY_MAX_BYTES = 65536

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
        schema_version: str = PROFILE_SCHEMA_VERSION,
        expression_policy: Mapping[str, Any] | None = None,
    ) -> RecordVersion:
        """Append one immutable Profile version; never change the active pointer.

        ``state_revision`` advances exactly once (the new version is a domain
        record).  ``active_profile_*`` is untouched, so the currently active
        version does not drift.

        ``schema_version`` defaults to the current body schema ("3"); passing
        "1" or "2" validates the older example shapes exactly.  No migration is
        involved: the version row simply carries the schema it was published
        with.

        ``expression_policy`` is optional.  When omitted the behaviour, the
        written rows and the audit trail are exactly those of the profile-only
        path.  When supplied as ``{"text": "<policy>"}`` the matching published
        policy record is appended **in the same transaction**: the persona
        version and the expression text it was approved with can never drift
        apart.  The record id is derived from ``(namespace, kind, profile_id,
        profile_version)``, so a second policy for the same version is refused
        instead of silently appended.
        """

        ns = identifiers.require_namespace(namespace)
        pid = require_profile_id(profile_id)
        authorization = require_authorization_ref(authorization_ref)
        if schema_version not in PROFILE_SCHEMA_VERSIONS:
            raise InvalidInput(
                "profile schema_version must be one of: "
                + ", ".join(PROFILE_SCHEMA_VERSIONS)
            )
        ref = EventRef.from_value(review_ref, field="review_ref")
        if ref.namespace != ns:
            raise SourceUntrusted(
                f"review_ref belongs to namespace {ref.namespace!r}, not {ns!r}"
            )
        profile_body = validate_profile_body(
            body, profile_id=pid, review_ref=ref, schema_version=schema_version
        )
        record_id = profile_record_id(ns, pid)
        body_digest = profile_body_digest(profile_body)
        profile_version = profile_body["profile_version"]

        policy_document: dict[str, Any] | None = None
        policy_id: str | None = None
        if expression_policy is not None:
            policy_id = policy_record_id(ns, PUBLISHED_KIND, pid, profile_version)
            policy_document = _policy_document(
                expression_policy,
                profile_id=pid,
                profile_version=profile_version,
                kind=PUBLISHED_KIND,
                review_ref=ref.to_dict(),
            )

        self._store.require_standalone("publish_profile")
        with self._store.transaction():
            review_payload = self._verify_review_event(
                ns, ref, body_digest=body_digest
            )
            if policy_document is not None:
                # A review event that names the policy digest binds this exact
                # text; the profile body digest is checked by the call above.
                recorded_policy = review_payload.get("expression_policy_sha256")
                if (
                    recorded_policy is not None
                    and recorded_policy != policy_document["text_sha256"]
                ):
                    raise SourceUntrusted(
                        f"review event {ref.event_id} approves a different "
                        "persona policy text"
                    )
                if self._store.record_pointer(ns, policy_id) is not None:
                    raise InvalidInput(
                        "this profile_version already has a published policy"
                    )
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
                schema_version=schema_version,
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
            if policy_document is not None:
                self._store.append_record_version(
                    namespace=ns,
                    record_id=policy_id,
                    record_version=1,
                    kind=PUBLISHED_KIND,
                    body=policy_document,
                    status="active",
                    schema_version=POLICY_SCHEMA_VERSION,
                    scope=None,
                )
                self._store.record_authorization(
                    namespace=ns,
                    operation=POLICY_PUBLISH_OPERATION,
                    target_kind="record",
                    target_id=policy_id,
                    target_version=1,
                    authorization_ref=authorization,
                )
            return appended

    def _verify_review_event(
        self, namespace: str, ref: EventRef, *, body_digest: str | None = None
    ) -> dict[str, Any]:
        """Verify the review event's provenance, not just the reference shape.

        ``get_event`` inherits the PCS2-006 read barrier, so a deleted review
        event raises ``REDACTED`` before any write.  A missing event raises
        ``NOT_FOUND``.  Both are left as-is: the caller must not be told the
        review happened when it cannot be shown.

        A review event that names ``body_sha256`` is bound to that exact body:
        the digest must match the body about to be published.  Events written
        before the binding existed simply carry no digest and keep the earlier
        provenance-only check (the legacy ``profile.publish`` path), so the
        stricter rule applies exactly to the reviewed desktop path that writes
        the digest.

        The verified payload is returned so a caller can apply its own binding
        (``expression_policy_sha256`` for a policy write); this method never
        interprets fields it was not asked about.
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
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        recorded_digest = payload.get("body_sha256")
        if (
            body_digest is not None
            and recorded_digest is not None
            and recorded_digest != body_digest
        ):
            raise SourceUntrusted(
                f"review event {ref.event_id} approves a different profile body"
            )
        return dict(payload)

    # -------------------------------------------------------- policy assets

    def seed_lab_policy(
        self,
        *,
        namespace: str,
        profile_id: str,
        profile_version: str,
        text: str,
        host_is_lab: bool,
        authorization_ref: str = "auth:persona-policy-seed",
    ) -> RecordVersion:
        """Write one Lab-only candidate policy; never a published asset.

        ``host_is_lab`` must be the trusted host's own configuration flag
        (``HostConfig.is_lab``), never something a caller, a model or user text
        supplies.  A candidate carries no ``review_ref`` and a production host
        refuses to load it, so this path cannot become an approval bypass.
        """

        if host_is_lab is not True:
            raise InvalidInput(
                "a candidate persona policy may only be seeded by a trusted Lab host"
            )
        ns = identifiers.require_namespace(namespace)
        pid = require_profile_id(profile_id)
        pv = _require_text(profile_version, "profile_version", 128)
        authorization = require_authorization_ref(authorization_ref)
        record_id = policy_record_id(ns, CANDIDATE_KIND, pid, pv)
        document = _policy_document(
            {"text": text},
            profile_id=pid,
            profile_version=pv,
            kind=CANDIDATE_KIND,
            review_ref=None,
        )

        self._store.require_standalone("seed_lab_policy")
        with self._store.transaction():
            if self._store.record_pointer(ns, record_id) is not None:
                raise InvalidInput(
                    "this profile_version already has a candidate policy"
                )
            appended = self._store.append_record_version(
                namespace=ns,
                record_id=record_id,
                record_version=1,
                kind=CANDIDATE_KIND,
                body=document,
                status="active",
                schema_version=POLICY_SCHEMA_VERSION,
                scope=None,
            )
            self._store.record_authorization(
                namespace=ns,
                operation=POLICY_SEED_OPERATION,
                target_kind="record",
                target_id=record_id,
                target_version=1,
                authorization_ref=authorization,
            )
            return appended

    def promote_policy(
        self,
        *,
        namespace: str,
        candidate_ref: Mapping[str, Any] | RecordRef,
        review_ref: Mapping[str, Any],
        authorization_ref: str = "auth:persona-policy-promote",
    ) -> RecordVersion:
        """Publish a candidate policy under a new record; never edit the candidate.

        The approval must name ``expression_policy_sha256`` equal to the
        candidate's own ``text_sha256``; without that binding a promotion could
        silently publish different text.  The candidate record is left byte for
        byte as it was, so "assets are immutable" and "a candidate can become
        published" hold at the same time: promotion only appends a new record of
        the other kind.
        """

        ns = identifiers.require_namespace(namespace)
        ref = (
            candidate_ref
            if isinstance(candidate_ref, RecordRef)
            else RecordRef.from_value(candidate_ref, field="candidate_ref")
        )
        if ref.namespace != ns:
            raise InvalidInput("candidate_ref does not belong to this namespace")
        review = EventRef.from_value(review_ref, field="review_ref")
        if review.namespace != ns:
            raise SourceUntrusted(
                f"review_ref belongs to namespace {review.namespace!r}, not {ns!r}"
            )
        authorization = require_authorization_ref(authorization_ref)
        candidate = self._store.get_version(ns, ref.record_id, ref.record_version)
        if candidate.kind != CANDIDATE_KIND:
            raise RecordKindMismatch(
                f"record {ref.record_id} is kind {candidate.kind!r}, "
                "not a candidate persona policy"
            )
        raw = candidate.body if isinstance(candidate.body, Mapping) else {}
        pid = raw.get("profile_id")
        pv = raw.get("profile_version")
        if not isinstance(pid, str) or not isinstance(pv, str):
            raise InvalidInput("candidate policy body has no profile identity")
        document = validate_policy_body(
            raw, profile_id=pid, profile_version=pv, kind=CANDIDATE_KIND
        )
        published_id = policy_record_id(ns, PUBLISHED_KIND, pid, pv)

        self._store.require_standalone("promote_policy")
        with self._store.transaction():
            if self._store.record_is_deleted(ns, ref.record_id):
                raise Redacted("the candidate persona policy has been deleted")
            payload = self._verify_review_event(ns, review)
            approved = payload.get("expression_policy_sha256")
            if not isinstance(approved, str) or approved != document["text_sha256"]:
                raise SourceUntrusted(
                    f"review event {review.event_id} does not bind this "
                    "candidate policy text"
                )
            if self._store.record_pointer(ns, published_id) is not None:
                raise InvalidInput(
                    "this profile_version already has a published policy"
                )
            published = _policy_document(
                {"text": document["text"]},
                profile_id=pid,
                profile_version=pv,
                kind=PUBLISHED_KIND,
                review_ref=review.to_dict(),
            )
            appended = self._store.append_record_version(
                namespace=ns,
                record_id=published_id,
                record_version=1,
                kind=PUBLISHED_KIND,
                body=published,
                status="active",
                schema_version=POLICY_SCHEMA_VERSION,
                scope=None,
            )
            self._store.record_authorization(
                namespace=ns,
                operation=POLICY_PROMOTE_OPERATION,
                target_kind="record",
                target_id=published_id,
                target_version=1,
                authorization_ref=authorization,
            )
            return appended


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


def profile_body_digest(body: Any) -> str:
    """SHA-256 of the canonical profile body **without** its ``review_ref``.

    The review reference is the approval that names this exact content, so it
    cannot be inside the content it protects: two bodies that differ only in
    which approval accompanies them are the same persona asset.  The host
    writes this digest into the ``persona.review.approve`` event payload and
    verifies it again at publish time, which is what makes an old approval
    non-replayable for a different body.
    """

    if not isinstance(body, Mapping):
        raise InvalidInput("profile body must be a JSON object")
    content = {key: value for key, value in body.items() if key != "review_ref"}
    return sha256_hex(canonical_json_text(content))


def validate_profile_body(
    body: Any,
    *,
    profile_id: str,
    review_ref: EventRef,
    schema_version: str = PROFILE_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Validate the Profile body shape for the requested schema version.

    This is a shape check, not a calibrated persona review.  It never reads or
    rewrites the real persona assets: the tests build synthetic bodies.

    Schema "1" keeps the original example shape exactly (five keys).  Schema
    "2" additionally accepts the optional ``pre_tool_commentary`` on one
    example: the short public line spoken before a tool call, kept separate
    from ``assistant`` (the final reply) so the two are never concatenated.
    Schema "3" additionally accepts ``assistant_messages``: the ordered bubbles
    of one final reply, mutually exclusive with ``assistant``.  All top-level
    keys are identical in every version.
    """

    if schema_version not in PROFILE_SCHEMA_VERSIONS:
        raise InvalidInput(
            "profile schema_version must be one of: "
            + ", ".join(PROFILE_SCHEMA_VERSIONS)
        )
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
    _validate_examples(body["examples"], schema_version=schema_version)
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
    # J11: raised once more for an approved author text (V17 needs 726 / 412 /
    # 204).  These stay upper bounds, not targets: the non-empty check and every
    # other field's limit are untouched.
    _require_text(style.get("tone"), "style.tone", 1024)
    verbosity = style.get("verbosity_by_context")
    if not isinstance(verbosity, Mapping):
        raise InvalidInput("profile body style.verbosity_by_context must be an object")
    limits = {"casual": 512, "technical": 256}
    for context, limit in limits.items():
        _require_text(
            verbosity.get(context), f"style.verbosity_by_context.{context}", limit
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


_EXAMPLE_KEYS = frozenset({"example_id", "scene", "user", "assistant", "synthetic"})
_EXAMPLE_EXTRA_KEYS = {
    "2": frozenset({"pre_tool_commentary"}),
    "3": frozenset({"pre_tool_commentary", "assistant_messages"}),
}
_EXAMPLE_REQUIRED_KEYS = frozenset({"example_id", "scene", "user", "synthetic"})


def _validate_examples(value: Any, *, schema_version: str = PROFILE_SCHEMA_VERSION) -> None:
    if not isinstance(value, list):
        raise InvalidInput("profile body examples must be an array")
    if len(value) > 64:
        raise InvalidInput("profile body examples must have at most 64 items")
    extra = _EXAMPLE_EXTRA_KEYS.get(schema_version, frozenset())
    allowed = _EXAMPLE_KEYS | extra
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise InvalidInput(f"profile body examples[{index}] must be an object")
        keys = set(item)
        if keys - allowed or _EXAMPLE_REQUIRED_KEYS - keys:
            raise InvalidInput(
                f"profile body examples[{index}] must have "
                "example_id, scene, user, synthetic and exactly one of "
                "assistant or assistant_messages"
                + (
                    " plus optional pre_tool_commentary"
                    if "pre_tool_commentary" in extra
                    else ""
                )
            )
        has_assistant = "assistant" in keys
        has_messages = "assistant_messages" in keys
        if has_assistant == has_messages:
            raise InvalidInput(
                f"profile body examples[{index}] must have exactly one of "
                "assistant or assistant_messages"
            )
        if "pre_tool_commentary" in keys and not has_assistant:
            raise InvalidInput(
                f"profile body examples[{index}].pre_tool_commentary requires assistant"
            )
        _require_text(item["example_id"], f"examples[{index}].example_id", 128)
        _require_text(item["scene"], f"examples[{index}].scene", 1024)
        _require_text(item["user"], f"examples[{index}].user", 8192)
        if has_assistant:
            _require_text(item["assistant"], f"examples[{index}].assistant", 8192)
        if has_messages:
            messages = item["assistant_messages"]
            if not isinstance(messages, list) or not 2 <= len(messages) <= 4:
                raise InvalidInput(
                    f"profile body examples[{index}].assistant_messages must be "
                    "an array of 2..4 strings"
                )
            for position, message in enumerate(messages):
                _require_text(
                    message,
                    f"examples[{index}].assistant_messages[{position}]",
                    8192,
                )
        if "pre_tool_commentary" in item:
            _require_text(
                item["pre_tool_commentary"],
                f"examples[{index}].pre_tool_commentary",
                4096,
            )
        if type(item["synthetic"]) is not bool:
            raise InvalidInput(
                f"profile body examples[{index}].synthetic must be a boolean"
            )


def _policy_document(
    value: Mapping[str, Any],
    *,
    profile_id: str,
    profile_version: str,
    kind: str,
    review_ref: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build and validate one policy body from the caller's ``{"text": ...}``.

    The caller supplies text only: the status, the digest and the profile
    identity are derived here so a caller cannot publish a body that disagrees
    with the record it is about to be written to.
    """

    if not isinstance(value, Mapping):
        raise InvalidInput(
            "expression_policy must be an object with exactly one text field"
        )
    unknown = sorted(str(key) for key in set(value) - {"text"})
    if unknown:
        raise InvalidInput(
            "expression_policy has unknown field(s): " + ", ".join(unknown)
        )
    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        raise InvalidInput("expression_policy.text must be a non-empty string")
    if len(text) > MAX_POLICY_TEXT_CHARS:
        raise InvalidInput("expression_policy.text exceeds its bound")
    status = "published" if kind == PUBLISHED_KIND else "lab_candidate"
    return validate_policy_body(
        {
            "schema_version": POLICY_SCHEMA_VERSION,
            "status": status,
            "profile_id": profile_id,
            "profile_version": profile_version,
            "text": text,
            "text_sha256": policy_text_sha256(text),
            "review_ref": None if review_ref is None else dict(review_ref),
        },
        profile_id=profile_id,
        profile_version=profile_version,
        kind=kind,
    )


__all__ = [
    "ActiveProfileState",
    "PERSONA_PROFILE_KIND",
    "POLICY_PROMOTE_OPERATION",
    "POLICY_PUBLISH_OPERATION",
    "POLICY_SEED_OPERATION",
    "PROFILE_BODY_KEYS",
    "PROFILE_BODY_MAX_BYTES",
    "PROFILE_RECORD_NAMESPACE",
    "PROFILE_SCHEMA_VERSION",
    "PROFILE_SCHEMA_VERSIONS",
    "PersonaProfileService",
    "REVIEW_APPROVE_EVENT_TYPE",
    "REVIEW_SOURCE_KIND",
    "profile_body_digest",
    "profile_record_id",
    "require_profile_id",
    "validate_profile_body",
]
