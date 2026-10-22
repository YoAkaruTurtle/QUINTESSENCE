"""C05c: the experience read surface (list + exact read).

This mixin is deliberately only a read surface.  It offers paginated reference
listing and exact reads; it does not search semantically, summarise, score,
build Episodes/Beliefs, run background jobs, or expose a delete entry point.
Deletion stays the trusted C05b ``delete_experience`` capability.

Two rules shape every method:

* ``ExperienceAccess`` is built by the trusted host, never by a model or a
  request JSON document.  Authorization failures are returned as
  :class:`ExperienceUnavailable`, indistinguishable from ``not found``;
  only malformed *arguments* raise :class:`InvalidInput`.
* An object body is only ever read through the C05b ``load_input`` /
  ``load_artifact`` path, so the deletion barrier and payload digest checks
  still run.  This module never selects ``private_payloads`` directly.

Input ownership is a representative association: among every interaction that
linked one exact input version, the view names the interaction with the
smallest ``(created_at, interaction_id)`` pair.  Both are durable columns, so
the association survives reopening the database.  It is not the input's
permanent source identity and not its only source; a normally appended later
link does not change it.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from ..foundation import identifiers
from ..foundation.errors import (
    CorruptStore,
    InvalidInput,
    Redacted,
    RecordNotFound,
    ScopeViolation,
)
from ..foundation.jsonutil import canonical_json_text, strict_json_loads
from ..foundation.timeutil import utc_now_rfc3339
from .collaboration import COLLAB_ARTIFACT, COLLAB_INPUT, MAX_CLOSURE_DEPTH

DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100
MAX_PURPOSE_LENGTH = 64
MAX_HOST_INSTANCE_LENGTH = 256
MAX_ACCESS_REF_LENGTH = 512

READABILITY_AVAILABLE = "available"
READABILITY_REDACTED = "redacted"
READABILITY_UNAVAILABLE = "unavailable"
READABILITY_VALUES = (
    READABILITY_AVAILABLE,
    READABILITY_REDACTED,
    READABILITY_UNAVAILABLE,
)

PROCESSING_IN_PROGRESS = "in_progress"
PROCESSING_RESULTS = (
    "answered",
    "waiting",
    "candidate_pending",
    "committed",
    "rejected",
    "stale",
    "denied",
    "failed",
    "incomplete",
)
#: The frozen ``experience_view.origin_kind`` enum.  An input may also be
#: ``host_supplied``, which the view contract cannot represent; such a
#: reference is returned as unavailable rather than fabricating an origin.
VIEW_ORIGIN_KINDS = ("user_statement", "model_output", "deterministic_query")

REF_TYPES = (COLLAB_INPUT, COLLAB_ARTIFACT)
READ_AUDIT_TABLE = "collab_experience_reads"

#: A ref dictionary is the only shape returned by the listing; it never
#: carries body text.
CollaborationRef = dict[str, Any]


@dataclass(frozen=True)
class ExperienceAccess:
    """Trusted-host read authority for one namespace.

    ``privacy_classes`` is the set of content classes this caller may read;
    every collaboration source of a derived artifact must be inside it.
    ``purposes`` is the closed set of audit purposes this caller may state.
    """

    namespace: str
    host_instance: str
    access_ref: str
    privacy_classes: frozenset[str]
    purposes: frozenset[str]


@dataclass(frozen=True)
class ExperiencePage:
    refs: tuple[CollaborationRef, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class ExperienceView:
    ref: CollaborationRef
    namespace: str
    interaction_id: str
    mode: str
    origin_kind: str
    processing: str
    readability: str
    body: str | None
    retention: str
    context_ref: str
    attempt_ref: dict[str, Any] | None
    source_refs: tuple[dict[str, Any], ...]
    created_at: str


@dataclass(frozen=True)
class ExperienceUnavailable:
    """The only failure shape: caller ref plus the fixed status."""

    ref: CollaborationRef
    readability: str = READABILITY_UNAVAILABLE


@dataclass(frozen=True)
class _Cursor:
    created_at: str
    ref_type: str
    ref_id: str
    ref_version: int


# --------------------------------------------------------------------------
# small validators
# --------------------------------------------------------------------------


def _require_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or identifiers.UUID_PATTERN.fullmatch(value) is None:
        raise InvalidInput(f"{field_name} must be a canonical lower-case UUID")
    return value


def _require_version(value: Any, field_name: str) -> int:
    if type(value) is not int or value < 1 or value > identifiers.MAX_SAFE_INTEGER:
        raise InvalidInput(f"{field_name} must be a positive JSON-safe integer")
    return value


def _require_text(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{field_name} must be a non-empty string")
    if len(value) > max_length:
        raise InvalidInput(f"{field_name} must be at most {max_length} characters")
    return value


def _require_purpose(value: Any) -> str:
    return _require_text(value, "purpose", MAX_PURPOSE_LENGTH)


def _require_access(access: Any) -> None:
    if not isinstance(access, ExperienceAccess):
        raise InvalidInput("access must be an ExperienceAccess")
    identifiers.require_namespace(access.namespace)
    _require_text(
        access.host_instance, "host_instance", MAX_HOST_INSTANCE_LENGTH
    )
    _require_text(access.access_ref, "access_ref", MAX_ACCESS_REF_LENGTH)
    if not isinstance(access.privacy_classes, frozenset):
        raise InvalidInput("access.privacy_classes must be a frozenset")
    if not isinstance(access.purposes, frozenset):
        raise InvalidInput("access.purposes must be a frozenset")
    for privacy in access.privacy_classes:
        if privacy not in identifiers.PRIVACY_CLASSES:
            raise InvalidInput(
                f"access.privacy_classes must use {', '.join(identifiers.PRIVACY_CLASSES)}"
            )
    for purpose in access.purposes:
        _require_purpose(purpose)


def _normalize_ref(ref: Any) -> tuple[CollaborationRef, str, str, int]:
    """Return ``(contract_ref, ref_type, ref_id, ref_version)`` or raise."""

    if not isinstance(ref, Mapping):
        raise InvalidInput("ref must be a typed collaboration reference object")
    value = dict(ref)
    ref_type = value.get("ref_type")
    if ref_type == COLLAB_INPUT:
        expected = {"ref_type", "namespace", "input_id", "content_version"}
        if set(value) != expected:
            raise InvalidInput(
                "a collab_input ref must have exactly ref_type, namespace, "
                "input_id, content_version"
            )
        namespace = identifiers.require_namespace(value["namespace"])
        ref_id = _require_uuid(value["input_id"], "input_id")
        ref_version = _require_version(value["content_version"], "content_version")
        normalized: CollaborationRef = {
            "ref_type": COLLAB_INPUT,
            "namespace": namespace,
            "input_id": ref_id,
            "content_version": ref_version,
        }
        return normalized, COLLAB_INPUT, ref_id, ref_version
    if ref_type == COLLAB_ARTIFACT:
        expected = {
            "ref_type",
            "namespace",
            "artifact_id",
            "artifact_version",
        }
        if set(value) != expected:
            raise InvalidInput(
                "a collab_artifact ref must have exactly ref_type, namespace, "
                "artifact_id, artifact_version"
            )
        namespace = identifiers.require_namespace(value["namespace"])
        ref_id = _require_uuid(value["artifact_id"], "artifact_id")
        ref_version = _require_version(value["artifact_version"], "artifact_version")
        normalized = {
            "ref_type": COLLAB_ARTIFACT,
            "namespace": namespace,
            "artifact_id": ref_id,
            "artifact_version": ref_version,
        }
        return normalized, COLLAB_ARTIFACT, ref_id, ref_version
    raise InvalidInput(
        f"ref_type must be one of {COLLAB_INPUT}, {COLLAB_ARTIFACT}"
    )


def _ref_from_row(namespace: str, row: Any) -> CollaborationRef:
    ref_type = row["ref_type"]
    ref_id = str(row["ref_id"])
    ref_version = int(row["ref_version"])
    if ref_type == COLLAB_INPUT:
        return {
            "ref_type": COLLAB_INPUT,
            "namespace": namespace,
            "input_id": ref_id,
            "content_version": ref_version,
        }
    if ref_type == COLLAB_ARTIFACT:
        return {
            "ref_type": COLLAB_ARTIFACT,
            "namespace": namespace,
            "artifact_id": ref_id,
            "artifact_version": ref_version,
        }
    raise CorruptStore(f"stored experience candidate has unknown kind {ref_type!r}")


def _encode_cursor(ref: CollaborationRef, created_at: str) -> str:
    text = canonical_json_text(
        {
            "created_at": created_at,
            "ref_type": ref["ref_type"],
            "ref_id": (
                ref["input_id"]
                if ref["ref_type"] == COLLAB_INPUT
                else ref["artifact_id"]
            ),
            "ref_version": (
                ref["content_version"]
                if ref["ref_type"] == COLLAB_INPUT
                else ref["artifact_version"]
            ),
        }
    )
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _decode_cursor(value: Any) -> _Cursor:
    if not isinstance(value, str) or not value:
        raise InvalidInput("after_cursor must be a non-empty string")
    try:
        raw = base64.b64decode(
            value.encode("ascii"), altchars=b"-_", validate=True
        )
        document = strict_json_loads(raw.decode("utf-8"))
    except (UnicodeError, binascii.Error, ValueError, TypeError) as exc:
        raise InvalidInput("after_cursor is not a valid experience cursor") from exc
    if not isinstance(document, dict) or set(document) != {
        "created_at",
        "ref_type",
        "ref_id",
        "ref_version",
    }:
        raise InvalidInput("after_cursor is not a valid experience cursor")
    created_at = document["created_at"]
    if (
        not isinstance(created_at, str)
        or identifiers.TIMESTAMP_PATTERN.fullmatch(created_at) is None
    ):
        raise InvalidInput("after_cursor is not a valid experience cursor")
    ref_type = document["ref_type"]
    if ref_type not in REF_TYPES:
        raise InvalidInput("after_cursor is not a valid experience cursor")
    ref_id = _require_uuid(document["ref_id"], "after_cursor.ref_id")
    ref_version = _require_version(
        document["ref_version"], "after_cursor.ref_version"
    )
    return _Cursor(
        created_at=created_at,
        ref_type=ref_type,
        ref_id=ref_id,
        ref_version=ref_version,
    )


def _decode_stored_json(text: str | None, field_name: str) -> Any:
    if text is None:
        return None
    try:
        return strict_json_loads(text)
    except ValueError as exc:
        raise CorruptStore(f"stored {field_name} is not strict JSON") from exc


# --------------------------------------------------------------------------
# mixin
# --------------------------------------------------------------------------


class ExperienceMixin:
    """Paginated listing plus exact reads; no delete and no ``_after_*`` hooks."""

    # ------------------------------------------------------------- listing

    def list_experience_refs(
        self,
        access: ExperienceAccess,
        *,
        namespace: str,
        after_cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> ExperiencePage:
        """List visible experience refs in stable ``(created_at, ref)`` order.

        Visibility is filtered **before** ``limit`` is applied: a hidden row
        never consumes a page slot, never influences ``next_cursor`` and does
        not reveal itself through the page size.  The cursor names the last
        record actually returned to the caller.

        An input is a candidate only when at least one interaction linked it;
        an unlinked input is not an experience.  An input has no separate
        ``created_at`` column, so its immutable ``recorded_at`` is the
        ordering/creation value.  A hidden artifact must have every source
        privacy class inside ``access.privacy_classes`` (see
        :meth:`_artifact_privacy_classes`).
        """

        _require_access(access)
        ns = identifiers.require_namespace(namespace)
        if ns != access.namespace:
            raise ScopeViolation(
                f"namespace {ns!r} is outside the access namespace {access.namespace!r}"
            )
        if type(limit) is not int or limit < 1 or limit > MAX_LIST_LIMIT:
            raise InvalidInput(
                f"limit must be an integer between 1 and {MAX_LIST_LIMIT}"
            )
        cursor = None if after_cursor is None else _decode_cursor(after_cursor)

        connection = self._require_connection()
        query = (
            "SELECT ref_type, ref_id, ref_version, created_at FROM ("
            "SELECT 'collab_input' AS ref_type, input_id AS ref_id, "
            "content_version AS ref_version, recorded_at AS created_at "
            "FROM collab_inputs AS i WHERE namespace = ? "
            "AND EXISTS (SELECT 1 FROM collab_interaction_inputs AS l "
            "WHERE l.namespace = i.namespace AND l.input_id = i.input_id "
            "AND l.content_version = i.content_version) "
            "UNION ALL "
            "SELECT 'collab_artifact' AS ref_type, artifact_id AS ref_id, "
            "artifact_version AS ref_version, created_at AS created_at "
            "FROM collab_artifacts WHERE namespace = ?"
            ") "
        )
        parameters: list[Any] = [ns, ns]
        if cursor is not None:
            query += (
                "WHERE (created_at, ref_type, ref_id, ref_version) "
                "> (?, ?, ?, ?) "
            )
            parameters.extend(
                [
                    cursor.created_at,
                    cursor.ref_type,
                    cursor.ref_id,
                    cursor.ref_version,
                ]
            )
        query += "ORDER BY created_at, ref_type, ref_id, ref_version"

        refs: list[CollaborationRef] = []
        last_returned: tuple[CollaborationRef, str] | None = None
        next_cursor: str | None = None
        for row in connection.execute(query, tuple(parameters)).fetchall():
            if not self._is_visible(
                namespace=ns,
                ref_type=row["ref_type"],
                ref_id=str(row["ref_id"]),
                ref_version=int(row["ref_version"]),
                access=access,
            ):
                continue
            if len(refs) == limit:
                # One more visible record exists after the last returned one.
                assert last_returned is not None
                next_cursor = _encode_cursor(*last_returned)
                break
            ref = _ref_from_row(ns, row)
            refs.append(ref)
            last_returned = (ref, str(row["created_at"]))
        return ExperiencePage(refs=tuple(refs), next_cursor=next_cursor)

    # ---------------------------------------------------------------- read

    def read_experience(
        self,
        access: ExperienceAccess,
        *,
        ref: Any,
        purpose: str,
    ) -> ExperienceView | ExperienceUnavailable:
        """Read one exact collaboration reference.

        Malformed arguments raise :class:`InvalidInput`.  A missing object, a
        foreign namespace, an unauthorized privacy class, an unauthorized
        purpose, or an unresolvable source privacy all return
        :class:`ExperienceUnavailable` with exactly the caller's ref, so none
        of them is distinguishable from ``not found``.
        """

        _require_access(access)
        normalized, ref_type, ref_id, ref_version = _normalize_ref(ref)
        statement_purpose = _require_purpose(purpose)

        if normalized["namespace"] != access.namespace:
            return ExperienceUnavailable(normalized)
        if statement_purpose not in access.purposes:
            return ExperienceUnavailable(normalized)

        if ref_type == COLLAB_INPUT:
            return self._read_input(
                access=access,
                ref=normalized,
                ref_id=ref_id,
                ref_version=ref_version,
                purpose=statement_purpose,
            )
        return self._read_artifact(
            access=access,
            ref=normalized,
            ref_id=ref_id,
            ref_version=ref_version,
            purpose=statement_purpose,
        )

    # --------------------------------------------------------- input read

    def _read_input(
        self,
        *,
        access: ExperienceAccess,
        ref: CollaborationRef,
        ref_id: str,
        ref_version: int,
        purpose: str,
    ) -> ExperienceView | ExperienceUnavailable:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT origin_kind, privacy_class, retention_policy, recorded_at "
            "FROM collab_inputs WHERE namespace = ? AND input_id = ? "
            "AND content_version = ?",
            (access.namespace, ref_id, ref_version),
        ).fetchone()
        if row is None:
            return ExperienceUnavailable(ref)
        if row["privacy_class"] not in identifiers.PRIVACY_CLASSES:
            return ExperienceUnavailable(ref)
        if row["privacy_class"] not in access.privacy_classes:
            return ExperienceUnavailable(ref)

        interaction = self._representative_interaction(
            namespace=access.namespace, input_id=ref_id, content_version=ref_version
        )
        if interaction is None:
            return ExperienceUnavailable(ref)
        if interaction["context_id"] is None:
            # The frozen view requires a Context UUID; do not invent one.
            return ExperienceUnavailable(ref)
        if row["origin_kind"] not in VIEW_ORIGIN_KINDS:
            # ``host_supplied`` is representable as an input but not as a view.
            return ExperienceUnavailable(ref)

        processing = self._processing(interaction["result"])
        body: str | None
        readability: str
        try:
            loaded = self.load_input(
                namespace=access.namespace,
                input_id=ref_id,
                content_version=ref_version,
            )
        except Redacted:
            readability = READABILITY_UNAVAILABLE
            body = None
        else:
            readability = READABILITY_AVAILABLE
            body = loaded.body
            if loaded.privacy_class != row["privacy_class"]:
                raise CorruptStore("collab input privacy_class changed between reads")

        view = ExperienceView(
            ref=ref,
            namespace=access.namespace,
            interaction_id=interaction["interaction_id"],
            mode=interaction["mode"],
            origin_kind=row["origin_kind"],
            processing=processing,
            readability=readability,
            body=body,
            retention=row["retention_policy"],
            context_ref=interaction["context_id"],
            attempt_ref=None,
            source_refs=(),
            created_at=row["recorded_at"],
        )
        self._record_read(
            access=access,
            purpose=purpose,
            ref_type=COLLAB_INPUT,
            ref_id=ref_id,
            ref_version=ref_version,
            outcome=readability,
        )
        return view

    def _representative_interaction(
        self, *, namespace: str, input_id: str, content_version: int
    ) -> Any:
        """First linked interaction by ``(created_at, interaction_id)``."""

        connection = self._require_connection()
        link = connection.execute(
            "SELECT 1 FROM collab_interaction_inputs WHERE namespace = ? "
            "AND input_id = ? AND content_version = ? LIMIT 1",
            (namespace, input_id, content_version),
        ).fetchone()
        if link is None:
            return None
        row = connection.execute(
            "SELECT i.interaction_id, i.mode, i.result, i.context_id, i.created_at "
            "FROM collab_interaction_inputs AS l "
            "JOIN collab_interactions AS i "
            "ON i.namespace = l.namespace AND i.interaction_id = l.interaction_id "
            "WHERE l.namespace = ? AND l.input_id = ? AND l.content_version = ? "
            "ORDER BY i.created_at, i.interaction_id LIMIT 1",
            (namespace, input_id, content_version),
        ).fetchone()
        if row is None:  # pragma: no cover - the foreign key prevents this
            raise CorruptStore(
                f"input {input_id} is linked to a missing interaction"
            )
        return row

    # ------------------------------------------------------- artifact read

    def _read_artifact(
        self,
        *,
        access: ExperienceAccess,
        ref: CollaborationRef,
        ref_id: str,
        ref_version: int,
        purpose: str,
    ) -> ExperienceView | ExperienceUnavailable:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT kind, interaction_id, context_id, attempt_ref_json, "
            "origin_kind, retention_policy, created_at "
            "FROM collab_artifacts WHERE namespace = ? AND artifact_id = ? "
            "AND artifact_version = ?",
            (access.namespace, ref_id, ref_version),
        ).fetchone()
        if row is None:
            return ExperienceUnavailable(ref)
        privacy = self._artifact_privacy_classes(
            namespace=access.namespace,
            artifact_id=ref_id,
            artifact_version=ref_version,
            depth=0,
            seen=set(),
        )
        if privacy is None:
            return ExperienceUnavailable(ref)
        if not privacy.issubset(access.privacy_classes):
            return ExperienceUnavailable(ref)

        interaction = connection.execute(
            "SELECT mode, result, context_id FROM collab_interactions "
            "WHERE namespace = ? AND interaction_id = ?",
            (access.namespace, row["interaction_id"]),
        ).fetchone()
        if interaction is None:  # pragma: no cover - registration binds it
            raise CorruptStore(
                f"artifact {ref_id} names missing interaction {row['interaction_id']}"
            )
        if row["origin_kind"] not in VIEW_ORIGIN_KINDS:
            return ExperienceUnavailable(ref)

        processing = self._processing(interaction["result"])
        readability: str
        body: str | None
        source_refs: tuple[dict[str, Any], ...]
        attempt_ref: dict[str, Any] | None
        try:
            loaded = self.load_artifact(
                namespace=access.namespace,
                artifact_id=ref_id,
                artifact_version=ref_version,
            )
        except Redacted:
            if self._is_deletion_target(
                namespace=access.namespace,
                ref_type=COLLAB_ARTIFACT,
                ref_id=ref_id,
                ref_version=ref_version,
            ):
                readability = READABILITY_UNAVAILABLE
            else:
                readability = READABILITY_REDACTED
            body = None
            source_refs = self._artifact_source_refs(
                namespace=access.namespace,
                artifact_id=ref_id,
                artifact_version=ref_version,
            )
            attempt_ref = _decode_stored_json(
                row["attempt_ref_json"], "attempt_ref_json"
            )
        except RecordNotFound:
            return ExperienceUnavailable(ref)
        else:
            readability = READABILITY_AVAILABLE
            body = loaded.body
            source_refs = loaded.source_refs
            attempt_ref = loaded.attempt_ref

        view = ExperienceView(
            ref=ref,
            namespace=access.namespace,
            interaction_id=row["interaction_id"],
            mode=interaction["mode"],
            origin_kind=row["origin_kind"],
            processing=processing,
            readability=readability,
            body=body,
            retention=row["retention_policy"],
            context_ref=row["context_id"],
            attempt_ref=attempt_ref,
            source_refs=source_refs,
            created_at=row["created_at"],
        )
        self._record_read(
            access=access,
            purpose=purpose,
            ref_type=COLLAB_ARTIFACT,
            ref_id=ref_id,
            ref_version=ref_version,
            outcome=readability,
        )
        return view

    # ---------------------------------------------------------- visibility

    def _is_visible(
        self,
        *,
        namespace: str,
        ref_type: str,
        ref_id: str,
        ref_version: int,
        access: ExperienceAccess,
    ) -> bool:
        if ref_type == COLLAB_INPUT:
            row = self._require_connection().execute(
                "SELECT privacy_class FROM collab_inputs WHERE namespace = ? "
                "AND input_id = ? AND content_version = ?",
                (namespace, ref_id, ref_version),
            ).fetchone()
            if row is None or row["privacy_class"] not in identifiers.PRIVACY_CLASSES:
                return False
            return row["privacy_class"] in access.privacy_classes
        privacy = self._artifact_privacy_classes(
            namespace=namespace,
            artifact_id=ref_id,
            artifact_version=ref_version,
            depth=0,
            seen=set(),
        )
        if privacy is None:
            return False
        return privacy.issubset(access.privacy_classes)

    def _artifact_privacy_classes(
        self,
        *,
        namespace: str,
        artifact_id: str,
        artifact_version: int,
        depth: int,
        seen: set[tuple[str, int]],
    ) -> frozenset[str] | None:
        """Collect every source privacy class, or ``None`` when unresolvable.

        There is no privacy rank and no ``private > restricted`` assumption:
        the caller must hold **every** class that any source carries.  A
        missing or malformed source row is invisible (fail closed), never
        treated as unrestricted.  An artifact without collaboration sources is
        conservatively ``private``.  Depth/cycle violations are corruption, not
        invisibility, and raise :class:`CorruptStore`.
        """

        if depth > MAX_CLOSURE_DEPTH:
            raise CorruptStore("experience source closure depth exceeded")
        key = (artifact_id, artifact_version)
        if key in seen:
            raise CorruptStore("experience source closure cycle")
        seen.add(key)
        try:
            connection = self._require_connection()
            row = connection.execute(
                "SELECT 1 FROM collab_artifacts WHERE namespace = ? "
                "AND artifact_id = ? AND artifact_version = ?",
                (namespace, artifact_id, artifact_version),
            ).fetchone()
            if row is None:
                return None
            links = connection.execute(
                "SELECT source_kind, source_id, source_version "
                "FROM collab_source_links WHERE namespace = ? "
                "AND owner_kind = 'artifact' AND owner_id = ? AND owner_version = ?",
                (namespace, artifact_id, artifact_version),
            ).fetchall()
            if not links:
                return frozenset({"private"})
            classes: set[str] = set()
            for link in links:
                kind = link["source_kind"]
                source_id = str(link["source_id"])
                source_version = int(link["source_version"])
                if kind == "event":
                    source_row = connection.execute(
                        "SELECT privacy_class FROM event_journal "
                        "WHERE namespace = ? AND event_id = ?",
                        (namespace, source_id),
                    ).fetchone()
                elif kind == "record":
                    source_row = connection.execute(
                        "SELECT privacy_class FROM record_versions "
                        "WHERE namespace = ? AND record_id = ? AND record_version = ?",
                        (namespace, source_id, source_version),
                    ).fetchone()
                elif kind == COLLAB_INPUT:
                    source_row = connection.execute(
                        "SELECT privacy_class FROM collab_inputs "
                        "WHERE namespace = ? AND input_id = ? AND content_version = ?",
                        (namespace, source_id, source_version),
                    ).fetchone()
                elif kind == COLLAB_ARTIFACT:
                    nested = self._artifact_privacy_classes(
                        namespace=namespace,
                        artifact_id=source_id,
                        artifact_version=source_version,
                        depth=depth + 1,
                        seen=seen,
                    )
                    if nested is None:
                        return None
                    classes.update(nested)
                    continue
                else:
                    return None
                if (
                    source_row is None
                    or source_row["privacy_class"] not in identifiers.PRIVACY_CLASSES
                ):
                    return None
                classes.add(source_row["privacy_class"])
            return frozenset(classes)
        finally:
            seen.discard(key)

    # ---------------------------------------------------------- audit/meta

    def _processing(self, result: Any) -> str:
        if result is None:
            return PROCESSING_IN_PROGRESS
        if result not in PROCESSING_RESULTS:
            raise CorruptStore(f"stored interaction result {result!r} is not frozen")
        return result

    def _is_deletion_target(
        self,
        *,
        namespace: str,
        ref_type: str,
        ref_id: str,
        ref_version: int,
    ) -> bool:
        row = self._require_connection().execute(
            "SELECT 1 FROM deletion_targets WHERE namespace = ? "
            "AND target_kind = ? AND target_id = ? AND target_version = ?",
            (namespace, ref_type, ref_id, ref_version),
        ).fetchone()
        return row is not None

    def _artifact_source_refs(
        self, *, namespace: str, artifact_id: str, artifact_version: int
    ) -> tuple[dict[str, Any], ...]:
        rows = self._require_connection().execute(
            "SELECT source_kind, source_id, source_version "
            "FROM collab_source_links WHERE namespace = ? "
            "AND owner_kind = 'artifact' AND owner_id = ? AND owner_version = ? "
            "ORDER BY source_kind, source_id, source_version",
            (namespace, artifact_id, artifact_version),
        ).fetchall()
        refs: list[dict[str, Any]] = []
        for row in rows:
            kind = row["source_kind"]
            source_id = str(row["source_id"])
            source_version = int(row["source_version"])
            if kind == "event":
                refs.append(
                    {
                        "ref_type": "event",
                        "namespace": namespace,
                        "event_id": source_id,
                    }
                )
            elif kind == "record":
                refs.append(
                    {
                        "ref_type": "record",
                        "namespace": namespace,
                        "record_id": source_id,
                        "record_version": source_version,
                    }
                )
            elif kind == COLLAB_INPUT:
                refs.append(
                    {
                        "ref_type": COLLAB_INPUT,
                        "namespace": namespace,
                        "input_id": source_id,
                        "content_version": source_version,
                    }
                )
            elif kind == COLLAB_ARTIFACT:
                refs.append(
                    {
                        "ref_type": COLLAB_ARTIFACT,
                        "namespace": namespace,
                        "artifact_id": source_id,
                        "artifact_version": source_version,
                    }
                )
            else:  # pragma: no cover - visibility fail-closes first
                raise CorruptStore(f"stored source link has unknown kind {kind!r}")
        return tuple(refs)

    def _record_read(
        self,
        *,
        access: ExperienceAccess,
        purpose: str,
        ref_type: str,
        ref_id: str,
        ref_version: int,
        outcome: str,
    ) -> None:
        if outcome not in READABILITY_VALUES:  # pragma: no cover - internal
            raise CorruptStore(f"unknown read outcome {outcome!r}")
        with self.transaction():
            self._require_connection().execute(
                "INSERT INTO " + READ_AUDIT_TABLE + " ("
                "read_id, namespace, host_instance, access_ref, purpose, "
                "ref_type, ref_id, ref_version, outcome, read_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    access.namespace,
                    access.host_instance,
                    access.access_ref,
                    purpose,
                    ref_type,
                    ref_id,
                    ref_version,
                    outcome,
                    utc_now_rfc3339(),
                ),
            )


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "ExperienceAccess",
    "ExperienceMixin",
    "ExperiencePage",
    "ExperienceUnavailable",
    "ExperienceView",
    "MAX_LIST_LIMIT",
    "READABILITY_AVAILABLE",
    "READABILITY_REDACTED",
    "READABILITY_UNAVAILABLE",
]
