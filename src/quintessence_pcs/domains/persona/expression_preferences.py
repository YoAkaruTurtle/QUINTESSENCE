"""RG05-F02: conversation-scoped tentative expression preferences.

A tentative preference is **not** a Belief and **not** an authorization.  It is
a small, unified Record whose only job is to tell the next Context "the user
said, in their own words, that they prefer this length/format/tone in this
conversation".  It never enters the belief store, the review queue, the
experience projection or the memory outbox.

Three properties make the honest representation possible without a migration:

* a unified Record carries an arbitrary ``kind`` and ``status``; the status is
  ``tentative`` / ``revoked`` / ``superseded`` and every change is a new
  immutable version (history is preserved);
* the retrieval scope is the conversation (``session_id``), so the preference
  does not silently become a namespace-wide rule;
* the body cites the exact collab input and quote, so a deleted input simply
  makes the record unusable at read time (the same rule beliefs use for their
  evidence).

The class is a host-only management entry: no model tool calls it with an
authorization, and the record cannot authorize a cognition change.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from ...foundation import identifiers
from ...foundation.errors import InvalidInput, RecordNotFound, Redacted
from ...store.domain import DomainStore
from .expression_directives import (
    EXPRESSION_DIMENSIONS,
    EXPRESSION_VALUES,
    parse_preference_marker,
    preference_marker,
)

EXPRESSION_PREFERENCE_KIND = "expression_preference"
EXPRESSION_PREFERENCE_SCHEMA = "1"
EXPRESSION_PREFERENCE_STATES = ("tentative", "revoked", "superseded")

#: Fixed UUID namespace for the deterministic per-(conversation, dimension)
#: record identity.  A stable record id is what makes "a new version of the same
#: preference" possible without a lookup table.
EXPRESSION_PREFERENCE_NAMESPACE = uuid.UUID(
    "5f2d0c1a-8b47-5e39-9a61-2c7e4d8f0b13"
)

MAX_QUOTE_CHARS = 512

_BODY_KEYS = frozenset(
    {
        "dimension",
        "value",
        "quote",
        "source_ref",
        "applies_to",
        "state",
    }
)
_OPTIONAL_BODY_KEYS = frozenset(
    {"revoked_reason", "revoked_by_ref", "superseded_by", "note"}
)


@dataclass(frozen=True)
class ExpressionPreference:
    """One current tentative preference, already checked for liveness."""

    version: Any
    dimension: str
    value: str
    quote: str
    source_ref: dict[str, Any]

    @property
    def ref(self) -> dict[str, Any]:
        return {
            "ref_type": "record",
            "namespace": self.version.namespace,
            "record_id": self.version.record_id,
            "record_version": self.version.record_version,
        }


class ExpressionPreferenceService:
    """Append-only tentative preference records for one PCS store."""

    def __init__(self, store: DomainStore):
        self._store = store

    # ------------------------------------------------------------- identity

    @staticmethod
    def record_id(
        namespace: str, conversation_id: str, dimension: str
    ) -> str:
        ns = identifiers.require_namespace(namespace)
        cid = identifiers.require_record_id(conversation_id)
        if dimension not in EXPRESSION_DIMENSIONS:
            raise InvalidInput(f"unknown expression dimension {dimension!r}")
        return str(
            uuid.uuid5(
                EXPRESSION_PREFERENCE_NAMESPACE,
                f"{ns}\x1f{cid}\x1f{dimension}",
            )
        )

    # ---------------------------------------------------------------- writes

    def revoke(
        self,
        *,
        namespace: str,
        conversation_id: str,
        dimension: str,
        reason: str,
        revoked_by_ref: Mapping[str, Any] | None = None,
        recorded_at: str | None = None,
    ) -> Any | None:
        """Append a revoked version; a missing/revoked record is a no-op."""

        ns = identifiers.require_namespace(namespace)
        cid = identifiers.require_record_id(conversation_id)
        record_id = self.record_id(ns, cid, dimension)
        current = self._current(ns, record_id)
        if current is None or current.status == "revoked":
            return current
        if current.status != "tentative":
            # A superseded preference is no longer the operative tentative
            # value; a later correction targets the confirmed preference.
            return current
        body = dict(current.body)
        body.update(
            {
                "state": "revoked",
                "revoked_reason": _require_text(reason, "revoked_reason", 512),
            }
        )
        if revoked_by_ref is not None:
            body["revoked_by_ref"] = _require_input_ref(revoked_by_ref, namespace=ns)
        validated = self._validate_body(body, namespace=ns)
        return self._store.append_record_version(
            namespace=ns,
            record_id=record_id,
            record_version=int(current.record_version) + 1,
            kind=EXPRESSION_PREFERENCE_KIND,
            body=validated,
            status="revoked",
            schema_version=EXPRESSION_PREFERENCE_SCHEMA,
            privacy_class="private",
            recorded_at=recorded_at,
            scope={"session_id": cid},
        )

    def active(
        self,
        *,
        namespace: str,
        conversation_id: str,
        privacy_classes: frozenset[str] | None = None,
    ) -> tuple[ExpressionPreference, ...]:
        """Current tentative values whose source input is still readable."""

        ns = identifiers.require_namespace(namespace)
        cid = identifiers.require_record_id(conversation_id)
        allowed = (
            frozenset({"private"})
            if privacy_classes is None
            else frozenset(privacy_classes)
        )
        current: list[ExpressionPreference] = []
        for version in self._store.current_versions_of_kind(
            ns, EXPRESSION_PREFERENCE_KIND
        ):
            if version.privacy_class not in allowed:
                continue
            scope = version.scope or {}
            if scope.get("session_id") != cid:
                continue
            if version.status != "tentative":
                continue
            body = version.body if isinstance(version.body, Mapping) else {}
            if body.get("state") != "tentative":
                continue
            dimension = body.get("dimension")
            value = body.get("value")
            if dimension not in EXPRESSION_VALUES or value not in EXPRESSION_VALUES[dimension]:
                continue
            source_ref = body.get("source_ref")
            quote = body.get("quote")
            if not isinstance(source_ref, Mapping) or not isinstance(quote, str):
                continue
            if not self._source_alive(ns, source_ref):
                continue
            current.append(
                ExpressionPreference(
                    version=version,
                    dimension=dimension,
                    value=value,
                    quote=quote,
                    source_ref=dict(source_ref),
                )
            )
        current.sort(
            key=lambda item: (
                EXPRESSION_DIMENSIONS.index(item.dimension),
                item.version.recorded_at,
            )
        )
        return tuple(current)

    def current(
        self, *, namespace: str, conversation_id: str, dimension: str
    ) -> Any | None:
        ns = identifiers.require_namespace(namespace)
        cid = identifiers.require_record_id(conversation_id)
        record_id = self.record_id(ns, cid, dimension)
        current = self._current(ns, record_id)
        if current is None:
            return None
        if (current.scope or {}).get("session_id") != cid:
            return None
        return current

    def current_in_scope(
        self,
        *,
        namespace: str,
        conversation_id: str,
        privacy_classes: frozenset[str] | None = None,
    ) -> tuple[Any, ...]:
        """Every current version of one conversation, **any** status.

        ``active()`` deliberately returns only the values that still apply to
        the expression layer.  A management view must also show what was
        revoked or superseded, so this read returns the current version of each
        conversation-scoped preference record regardless of status (including a
        dead source), ordered by dimension and recording time.  It writes
        nothing and never follows any pointer outside ``record_versions``.
        """

        ns = identifiers.require_namespace(namespace)
        cid = identifiers.require_record_id(conversation_id)
        allowed = (
            frozenset({"private"})
            if privacy_classes is None
            else frozenset(privacy_classes)
        )
        rows: list[Any] = []
        for version in self._store.current_versions_of_kind(
            ns, EXPRESSION_PREFERENCE_KIND
        ):
            if version.privacy_class not in allowed:
                continue
            if (version.scope or {}).get("session_id") != cid:
                continue
            if version.status not in EXPRESSION_PREFERENCE_STATES:
                continue
            body = version.body if isinstance(version.body, Mapping) else {}
            dimension = body.get("dimension")
            value = body.get("value")
            if dimension not in EXPRESSION_VALUES:
                continue
            if value not in EXPRESSION_VALUES[dimension]:
                continue
            rows.append(version)
        rows.sort(
            key=lambda version: (
                EXPRESSION_DIMENSIONS.index(
                    (version.body or {}).get("dimension", EXPRESSION_DIMENSIONS[0])
                ),
                version.recorded_at,
            )
        )
        return tuple(rows)

    def source_alive(self, *, namespace: str, ref: Mapping[str, Any]) -> bool:
        """Whether a stored tentative source input is still readable."""

        return self._source_alive(identifiers.require_namespace(namespace), ref)

    # ------------------------------------------------------------- internals

    def _current(self, namespace: str, record_id: str) -> Any | None:
        try:
            return self._store.get_current(namespace, record_id)
        except RecordNotFound:
            return None
        except Redacted:
            return None

    def _source_alive(self, namespace: str, ref: Mapping[str, Any]) -> bool:
        try:
            row = self._store.load_input(
                namespace=namespace,
                input_id=str(ref["input_id"]),
                content_version=int(ref["content_version"]),
            )
        except (Redacted, RecordNotFound, KeyError, ValueError):
            return False
        return row.origin_kind == "user_statement"

    def _require_source(
        self, namespace: str, ref: Mapping[str, Any], quote: str
    ) -> None:
        try:
            row = self._store.load_input(
                namespace=namespace,
                input_id=str(ref["input_id"]),
                content_version=int(ref["content_version"]),
            )
        except (Redacted, RecordNotFound) as exc:
            raise InvalidInput("the expression-preference source input is unavailable") from exc
        if row.origin_kind != "user_statement":
            raise InvalidInput("the expression-preference source must be a user statement")
        if quote not in row.body:
            raise InvalidInput("the expression-preference quote is not in the source input")

    def _validate_body(
        self, body: Mapping[str, Any], *, namespace: str
    ) -> dict[str, Any]:
        keys = set(body)
        missing = _BODY_KEYS - keys
        unknown = keys - _BODY_KEYS - _OPTIONAL_BODY_KEYS
        if missing or unknown:
            raise InvalidInput(
                "expression_preference body must have exactly dimension, value, "
                "quote, source_ref, applies_to and state"
            )
        dimension = body["dimension"]
        value = body["value"]
        if dimension not in EXPRESSION_VALUES:
            raise InvalidInput(f"unknown expression dimension {dimension!r}")
        if value not in EXPRESSION_VALUES[dimension]:
            raise InvalidInput(f"unknown expression value {value!r}")
        quote = _require_text(body["quote"], "quote", MAX_QUOTE_CHARS)
        state = body["state"]
        if state not in EXPRESSION_PREFERENCE_STATES:
            raise InvalidInput(f"unknown expression preference state {state!r}")
        if body["applies_to"] != "conversation":
            raise InvalidInput("an expression preference applies to its conversation")
        ref = _require_input_ref(body["source_ref"], namespace=namespace)
        if state == "revoked":
            _require_text(body.get("revoked_reason"), "revoked_reason", 512)
            if "revoked_by_ref" in body:
                _require_input_ref(body["revoked_by_ref"], namespace=namespace)
        if state == "superseded":
            _require_belief_ref(body.get("superseded_by"), namespace=namespace)
        return {
            "dimension": dimension,
            "value": value,
            "quote": quote,
            "source_ref": ref,
            "applies_to": "conversation",
            "state": state,
            **{
                key: body[key]
                for key in _OPTIONAL_BODY_KEYS
                if key in body
            },
        }


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------


def _require_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise InvalidInput(f"{field} must be at most {maximum} characters")
    return value


def _require_input_ref(value: Any, *, namespace: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidInput("source_ref must be a collab_input reference")
    if set(value) != {"ref_type", "namespace", "input_id", "content_version"}:
        raise InvalidInput(
            "source_ref must have exactly ref_type, namespace, input_id and "
            "content_version"
        )
    if value.get("ref_type") != "collab_input":
        raise InvalidInput("source_ref must be a collab_input reference")
    if value.get("namespace") != namespace:
        raise InvalidInput("source_ref belongs to another namespace")
    return {
        "ref_type": "collab_input",
        "namespace": namespace,
        "input_id": identifiers.require_record_id(value.get("input_id")),
        "content_version": identifiers.require_record_version(
            value.get("content_version")
        ),
    }


def _require_belief_ref(value: Any, *, namespace: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidInput("superseded_by must be a record reference")
    if set(value) != {"record_id", "record_version"}:
        raise InvalidInput("superseded_by must have record_id and record_version")
    return {
        "record_id": identifiers.require_record_id(value.get("record_id")),
        "record_version": identifiers.require_record_version(
            value.get("record_version")
        ),
    }


def preference_identity(dimension: str, value: str) -> str:
    """The stable marker used by belief bodies (kept next to the record code)."""

    return preference_marker(dimension, value)


def parse_identity(marker: object) -> tuple[str, str] | None:
    return parse_preference_marker(marker)


__all__ = [
    "EXPRESSION_PREFERENCE_KIND",
    "EXPRESSION_PREFERENCE_NAMESPACE",
    "EXPRESSION_PREFERENCE_SCHEMA",
    "EXPRESSION_PREFERENCE_STATES",
    "ExpressionPreference",
    "ExpressionPreferenceService",
    "parse_identity",
    "preference_identity",
]
