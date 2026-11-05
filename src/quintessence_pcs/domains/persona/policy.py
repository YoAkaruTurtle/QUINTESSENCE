"""RG08: persona expression policy assets (two-state, kind-separated).

A persona expression policy is a Record, never a source constant.  The two
states use **different record kinds** so a candidate can never impersonate a
published asset by editing its body: the resolver dispatches on ``kind``.

The uniqueness key is ``(namespace, kind, profile_id, profile_version)`` and the
record id is derived from it with ``uuid5`` -- exactly the pattern the profile
record id already uses -- so no lookup table, no migration and no scan is
needed.

This module is pure with respect to the store: callers pass an object exposing
``get_version``/``get_current``/``record_is_deleted``.  It never writes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from ...foundation import identifiers
from ...foundation.errors import InvalidInput, RecordNotFound
from ...foundation.jsonutil import canonical_json_text, sha256_hex
from ..refs import RecordRef

#: Published assets are the only ones a production host may load.
PUBLISHED_KIND = "persona_expression_policy"

#: Candidate assets exist for isolated Lab experiments and are never published.
CANDIDATE_KIND = "persona_expression_policy_candidate"

POLICY_KINDS = (PUBLISHED_KIND, CANDIDATE_KIND)

#: Policy document schema this card writes and validates.
POLICY_SCHEMA_VERSION = "1"

#: Fixed namespace for the deterministic UUIDv5 record identity of a policy.
POLICY_RECORD_NAMESPACE = uuid.UUID("6f2f1c2a-8f3b-5a4d-9e71-2d0c7b6a5e13")

#: Closed set of body keys.  ``status`` mirrors the kind for readers; the
#: resolver never trusts it -- ``kind`` is authoritative.
POLICY_BODY_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "profile_id",
        "profile_version",
        "text",
        "text_sha256",
        "review_ref",
    }
)

#: One policy text is bounded; the largest authored policy today is < 4 KB.
MAX_POLICY_TEXT_CHARS = 16384


@dataclass(frozen=True)
class PersonaPolicyAsset:
    """The resolved persona policy for one turn (never a source constant)."""

    state: str  # "present" | "absent" | "invalid"
    profile_id: str
    profile_version: str
    text: str = ""
    sha256: str | None = None
    ref: dict[str, Any] | None = None
    reason: str | None = None
    review_state: str | None = None

    @property
    def is_present(self) -> bool:
        return self.state == "present"


def policy_record_id(
    namespace: str, kind: str, profile_id: str, profile_version: str
) -> str:
    """Deterministic record identity for one (namespace, kind, profile, version).

    ``kind`` is part of the key so the published and candidate assets of the
    same persona version can never collide.
    """

    ns = identifiers.require_namespace(namespace)
    if kind not in POLICY_KINDS:
        raise InvalidInput("kind must be a persona policy kind")
    pid = _require_text(profile_id, "profile_id")
    pv = _require_text(profile_version, "profile_version")
    return str(uuid.uuid5(POLICY_RECORD_NAMESPACE, f"{ns}\x1f{kind}\x1f{pid}\x1f{pv}"))


def policy_text_sha256(text: str) -> str:
    return sha256_hex(text)


def validate_policy_body(
    body: Any,
    *,
    profile_id: str,
    profile_version: str,
    kind: str,
) -> dict[str, Any]:
    """Validate one policy body (closed keys, self-digest, kind/status agreement)."""

    if kind not in POLICY_KINDS:
        raise InvalidInput("kind must be a persona policy kind")
    if not isinstance(body, Mapping):
        raise InvalidInput("policy body must be a JSON object")
    keys = set(body)
    missing = sorted(POLICY_BODY_KEYS - keys)
    if missing:
        raise InvalidInput(
            "policy body is missing required field(s): " + ", ".join(missing)
        )
    unknown = sorted(str(key) for key in keys - POLICY_BODY_KEYS)
    if unknown:
        raise InvalidInput(
            "policy body has unknown field(s): " + ", ".join(unknown)
        )
    if body["schema_version"] != POLICY_SCHEMA_VERSION:
        raise InvalidInput(
            "policy schema_version must be " + POLICY_SCHEMA_VERSION
        )
    if body["profile_id"] != profile_id:
        raise InvalidInput("policy body profile_id does not match its record key")
    if body["profile_version"] != profile_version:
        raise InvalidInput(
            "policy body profile_version does not match its record key"
        )
    expected_status = "published" if kind == PUBLISHED_KIND else "lab_candidate"
    if body["status"] != expected_status:
        raise InvalidInput("policy body status disagrees with its record kind")
    text = body["text"]
    if not isinstance(text, str) or not text.strip():
        raise InvalidInput("policy text must be non-empty")
    if len(text) > MAX_POLICY_TEXT_CHARS:
        raise InvalidInput("policy text exceeds its bound")
    if body["text_sha256"] != policy_text_sha256(text):
        raise InvalidInput("policy text_sha256 does not match the text")
    review = body["review_ref"]
    if kind == PUBLISHED_KIND:
        if not isinstance(review, Mapping):
            raise InvalidInput("a published policy needs a review_ref")
    elif review is not None:
        raise InvalidInput("a candidate policy must not carry a review_ref")
    document = dict(body)
    canonical_json_text(document)
    return document


def resolve_persona_policy(
    records: Any,
    namespace: str,
    profile_ref: RecordRef,
    *,
    host_is_lab: bool,
) -> PersonaPolicyAsset:
    """Resolve this turn's persona policy asset.

    Precedence is fixed: a published asset always wins; a candidate is only
    consulted on a trusted Lab host; an invalid published asset never falls
    back to the candidate.  Store errors propagate -- they must fail the turn,
    never look like "absent".
    """

    ns = identifiers.require_namespace(namespace)
    if not isinstance(profile_ref, RecordRef):
        raise InvalidInput("profile_ref must be a RecordRef")
    if profile_ref.namespace != ns:
        raise InvalidInput("profile_ref does not belong to this namespace")

    profile = records.get_version(
        ns, profile_ref.record_id, profile_ref.record_version
    )
    body = profile.body if isinstance(profile.body, Mapping) else {}
    pid = body.get("profile_id")
    pv = body.get("profile_version")
    if not isinstance(pid, str) or not isinstance(pv, str):
        return PersonaPolicyAsset(
            state="invalid",
            profile_id=str(pid or ""),
            profile_version=str(pv or ""),
            reason="profile_identity_missing",
        )

    published = _read_optional(records, ns, policy_record_id(ns, PUBLISHED_KIND, pid, pv))
    if published is not None:
        return _asset_from_record(
            records,
            ns,
            published,
            kind=PUBLISHED_KIND,
            profile_id=pid,
            profile_version=pv,
            review_state="approved",
        )

    candidate = _read_optional(records, ns, policy_record_id(ns, CANDIDATE_KIND, pid, pv))
    if candidate is not None:
        if not host_is_lab:
            return PersonaPolicyAsset(
                state="absent",
                profile_id=pid,
                profile_version=pv,
                reason="candidate_ignored",
            )
        return _asset_from_record(
            records,
            ns,
            candidate,
            kind=CANDIDATE_KIND,
            profile_id=pid,
            profile_version=pv,
            review_state="unreviewed",
        )

    return PersonaPolicyAsset(
        state="absent", profile_id=pid, profile_version=pv, reason="no_asset"
    )


def _asset_from_record(
    records: Any,
    namespace: str,
    record: Any,
    *,
    kind: str,
    profile_id: str,
    profile_version: str,
    review_state: str,
) -> PersonaPolicyAsset:
    if records.record_is_deleted(namespace, record.record_id):
        return PersonaPolicyAsset(
            state="invalid",
            profile_id=profile_id,
            profile_version=profile_version,
            reason="deleted",
        )
    if record.kind != kind:
        return PersonaPolicyAsset(
            state="invalid",
            profile_id=profile_id,
            profile_version=profile_version,
            reason="kind_mismatch",
        )
    if record.record_version != 1:
        # One (profile_id, profile_version) owns exactly one version.  A second
        # version would silently replace the text the id promises, so the
        # resolver refuses it instead of following the pointer.
        return PersonaPolicyAsset(
            state="invalid",
            profile_id=profile_id,
            profile_version=profile_version,
            reason="unexpected_version",
        )
    try:
        document = validate_policy_body(
            record.body,
            profile_id=profile_id,
            profile_version=profile_version,
            kind=kind,
        )
    except InvalidInput as exc:
        return PersonaPolicyAsset(
            state="invalid",
            profile_id=profile_id,
            profile_version=profile_version,
            reason="invalid_body:" + _short(str(exc)),
        )
    return PersonaPolicyAsset(
        state="present",
        profile_id=profile_id,
        profile_version=profile_version,
        text=document["text"],
        sha256=document["text_sha256"],
        ref=RecordRef(
            namespace=namespace,
            record_id=record.record_id,
            record_version=record.record_version,
        ).to_dict(),
        review_state=review_state,
    )


def _read_optional(records: Any, namespace: str, record_id: str) -> Any | None:
    try:
        return records.get_current(namespace, record_id)
    except RecordNotFound:
        return None


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"{field} must be a non-empty string")
    return value


def _short(text: str, limit: int = 64) -> str:
    return text[:limit]


__all__ = [
    "CANDIDATE_KIND",
    "MAX_POLICY_TEXT_CHARS",
    "POLICY_BODY_KEYS",
    "POLICY_KINDS",
    "POLICY_RECORD_NAMESPACE",
    "POLICY_SCHEMA_VERSION",
    "PUBLISHED_KIND",
    "PersonaPolicyAsset",
    "policy_record_id",
    "policy_text_sha256",
    "resolve_persona_policy",
    "validate_policy_body",
]
