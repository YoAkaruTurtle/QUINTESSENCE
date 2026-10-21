"""PCS2-007 domain-plane reads and management entries.

This module adds the store surface the Persona and Memory domain services use;
it does not change any PCS2-004/005/006 behaviour.  ``DomainStore`` composes it
on top of :class:`~quintessence_pcs.store.control.ControlStore`, so every read here
inherits the PCS2-006 deletion barrier (``get_version`` / ``get_event``).

What lives here, and why it belongs to the store rather than the domain layer:

* ``read_snapshot`` — one consistent read transaction for a search, so a
  deletion cannot commit between two ``SELECT`` statements of the same query;
* ``record_pointer`` / ``current_versions_of_kind`` / ``event_header`` — plain
  reads that never pass through a request DTO;
* ``active_profile_pointer`` / ``set_active_profile`` — the two columns added by
  ``0004``; setting the pointer advances ``state_revision`` exactly once;
* ``episode_source`` / ``episode_sources_for_record`` /
  ``insert_episode_source`` — the structural "one source event -> one episode"
  mapping.  ``insert_episode_source`` is the concurrency backstop: the
  ``(namespace, event_id)`` primary key decides the race, and the loser gets
  :class:`EpisodeSourceExists` instead of a second episode;
* ``record_authorization`` — the append-only audit row the PCS2-007 card
  requires for admin publishes (it never stores a body);
* ``record_is_deleted`` / ``event_is_deleted`` — public bool wrappers over the
  PCS2-006 barrier for retrieval, which must skip a deleted hit instead of
  raising ``REDACTED`` for the whole search.

None of these methods is a security boundary.  The deletion barrier and the
API boundaries are the PCS2-006 ones; see ``docs/PCS2-006-TXB.md`` section 4.11.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ..foundation import identifiers
from ..foundation.errors import (
    AuthorizationRequired,
    CorruptStore,
    InvalidInput,
    RecordKindMismatch,
    RecordNotFound,
    Redacted,
    StorageError,
)
from ..foundation.timeutil import utc_now_rfc3339
from .control import ControlStore
from .deletion import DELETION_EVENT, DELETION_RECORD
from .records import RecordVersion

#: Same bound as ``deletion_targets.authorization_ref`` (0003).
AUTHORIZATION_REF_MAX_LENGTH = 512

_DOMAIN_AUDIT_KINDS = ("record", "event")

_VERSION_COLUMNS = (
    "namespace, record_id, record_version, kind, schema_version, status, "
    "privacy_class, recorded_at, committed_state_revision, body_payload_ref, "
    "scope_json"
)
_RV_COLUMNS = ", ".join(f"rv.{name.strip()}" for name in _VERSION_COLUMNS.split(","))

_EVENT_HEADER_COLUMNS = (
    "namespace, event_id, event_seq, source_kind, source_instance, "
    "source_event_id, source_position, event_type, observed_at, recorded_at, "
    "privacy_class"
)

_EPISODE_SOURCE_COLUMNS = "namespace, event_id, record_id, record_version, created_at"


@dataclass(frozen=True)
class RecordPointer:
    """The ``records`` current pointer for one record (kind + version)."""

    kind: str
    current_version: int


@dataclass(frozen=True)
class EventHeader:
    """Event envelope metadata without touching the controlled payload.

    Retrieval needs ``source_kind`` and the timestamps to derive
    ``epistemic_status``; loading the payload would be unnecessary contact with
    private bytes.
    """

    namespace: str
    event_id: str
    event_seq: int
    source_kind: str
    source_instance: str
    source_event_id: str
    source_position: int | None
    event_type: str
    observed_at: str | None
    recorded_at: str
    privacy_class: str


@dataclass(frozen=True)
class ActiveProfilePointer:
    """Exact active PersonaProfile pointer; both fields are null = neutral."""

    record_id: str | None
    record_version: int | None


@dataclass(frozen=True)
class EpisodeSource:
    """One row of ``episode_sources``: an event that an episode was built from."""

    namespace: str
    event_id: str
    record_id: str
    record_version: int
    created_at: str


class EpisodeSourceExists(StorageError):
    """The source event already maps to an episode (primary-key backstop)."""

    code = "ID_CONFLICT"


class DomainStateMixin:
    """PCS2-007 reads and management entries (composed into ``DomainStore``)."""

    # -------------------------------------------------------------- boundaries

    def require_standalone(self, operation: str) -> None:
        """Refuse to join a caller's transaction (same rule as PCS2-006)."""

        self._require_standalone(operation)

    # ------------------------------------------------------------------ reads

    @contextmanager
    def read_snapshot(self) -> Iterator["DomainStateMixin"]:
        """Hold one read transaction so a search sees a single committed state.

        A deferred ``BEGIN`` only takes a shared lock when the first row is
        read, so this is cheap.  In rollback-journal mode a writer cannot commit
        while this snapshot is open, which is what makes "deleted between two
        SELECTs" impossible inside one search.  Nested use joins the open
        transaction instead of opening a second one.
        """

        connection = self._require_connection()
        if self._tx_depth > 0 or connection.in_transaction:
            yield self
            return
        connection.execute("BEGIN")
        try:
            yield self
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - defensive
                pass
            raise
        else:
            connection.execute("COMMIT")

    def record_pointer(self, namespace: str, record_id: str) -> RecordPointer | None:
        """Read the current pointer only; ``None`` when the record is absent."""

        identifiers.require_namespace(namespace)
        identifiers.require_record_id(record_id)
        row = self._require_connection().execute(
            "SELECT kind, current_version FROM records "
            "WHERE namespace = ? AND record_id = ?",
            (namespace, record_id),
        ).fetchone()
        if row is None:
            return None
        return RecordPointer(
            kind=row["kind"], current_version=int(row["current_version"])
        )

    def current_versions_of_kind(
        self, namespace: str, kind: str
    ) -> tuple[RecordVersion, ...]:
        """Every readable record of *kind* at its current version.

        Uses the (namespace, kind, record_id) index added by ``0004``.  Old
        versions are deliberately unreachable through this read.  Records under
        a deletion barrier are excluded in SQL: their bodies may already be
        cleared, and the PCS2-006 barrier must govern this read exactly like
        ``get_current`` does.  Ordered by record ID.
        """

        identifiers.require_namespace(namespace)
        identifiers.require_kind(kind)
        rows = self._require_connection().execute(
            f"SELECT {_RV_COLUMNS} "
            "FROM records r "
            "JOIN record_versions rv "
            "  ON rv.namespace = r.namespace "
            " AND rv.record_id = r.record_id "
            " AND rv.record_version = r.current_version "
            "WHERE r.namespace = ? AND r.kind = ? "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM deletion_targets dt "
            "    WHERE dt.target_kind = ? AND dt.namespace = r.namespace "
            "      AND dt.target_id = r.record_id"
            "  ) "
            "ORDER BY rv.record_id",
            (namespace, kind, DELETION_RECORD),
        ).fetchall()
        return tuple(self._version_from_row(row) for row in rows)

    def event_header(self, namespace: str, event_id: str) -> EventHeader | None:
        """Read event envelope metadata without loading its private payload."""

        identifiers.require_namespace(namespace)
        identifiers.require_event_id(event_id)
        row = self._require_connection().execute(
            f"SELECT {_EVENT_HEADER_COLUMNS} FROM event_journal "
            "WHERE namespace = ? AND event_id = ?",
            (namespace, event_id),
        ).fetchone()
        if row is None:
            return None
        return EventHeader(
            namespace=row["namespace"],
            event_id=row["event_id"],
            event_seq=int(row["event_seq"]),
            source_kind=row["source_kind"],
            source_instance=row["source_instance"],
            source_event_id=row["source_event_id"],
            source_position=(
                None if row["source_position"] is None else int(row["source_position"])
            ),
            event_type=row["event_type"],
            observed_at=row["observed_at"],
            recorded_at=row["recorded_at"],
            privacy_class=row["privacy_class"],
        )

    def record_is_deleted(self, namespace: str, record_id: str) -> bool:
        """Public bool view of the PCS2-006 record barrier."""

        identifiers.require_namespace(namespace)
        identifiers.require_record_id(record_id)
        row = self._require_connection().execute(
            "SELECT 1 FROM deletion_targets "
            "WHERE namespace = ? AND target_kind = ? AND target_id = ?",
            (namespace, DELETION_RECORD, record_id),
        ).fetchone()
        return row is not None

    def event_is_deleted(self, namespace: str, event_id: str) -> bool:
        """Public bool view of the PCS2-006 event barrier."""

        identifiers.require_namespace(namespace)
        identifiers.require_event_id(event_id)
        row = self._require_connection().execute(
            "SELECT 1 FROM deletion_targets "
            "WHERE namespace = ? AND target_kind = ? AND target_id = ?",
            (namespace, DELETION_EVENT, event_id),
        ).fetchone()
        return row is not None

    # --------------------------------------------------------- active profile

    def active_profile_pointer(self, namespace: str) -> ActiveProfilePointer:
        """Exact active-profile pointer; null columns are neutral degradation."""

        identifiers.require_namespace(namespace)
        row = self._require_connection().execute(
            "SELECT active_profile_record_id, active_profile_version "
            "FROM namespace_state WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        if row is None:
            return ActiveProfilePointer(record_id=None, record_version=None)
        record_id = row["active_profile_record_id"]
        record_version = row["active_profile_version"]
        if (record_id is None) != (record_version is None):
            raise CorruptStore(
                f"namespace {namespace!r} has a half active-profile pointer"
            )
        return ActiveProfilePointer(
            record_id=record_id,
            record_version=None if record_version is None else int(record_version),
        )

    def set_active_profile(
        self,
        *,
        namespace: str,
        record_id: str,
        record_version: int,
        required_kind: str | None = None,
    ) -> int:
        """Point ``active_profile_*`` at one exact version (caller owns the tx).

        The version row must exist and must not be inside a deletion barrier.
        ``state_revision`` advances exactly once here, which is the whole
        domain commit of an activation.  Returns the new ``state_revision``.
        """

        if self._tx_depth == 0:
            raise RuntimeError(
                "set_active_profile must run inside the caller's transaction"
            )
        ns = identifiers.require_namespace(namespace)
        rid = identifiers.require_record_id(record_id)
        ver = identifiers.require_record_version(record_version)
        if required_kind is not None:
            identifiers.require_kind(required_kind)

        if self.record_is_deleted(ns, rid):
            raise Redacted(f"record {rid} has been deleted and cannot be activated")
        row = self._require_connection().execute(
            "SELECT kind FROM record_versions "
            "WHERE namespace = ? AND record_id = ? AND record_version = ?",
            (ns, rid, ver),
        ).fetchone()
        if row is None:
            raise RecordNotFound(
                f"record {rid} has no version {ver} in namespace {ns!r}"
            )
        if required_kind is not None and row["kind"] != required_kind:
            raise RecordKindMismatch(
                f"record {rid} is kind {row['kind']!r}; {required_kind!r} is required"
            )

        revision = self._allocate_state_revision_locked(ns)
        cursor = self._require_connection().execute(
            "UPDATE namespace_state "
            "SET active_profile_record_id = ?, active_profile_version = ? "
            "WHERE namespace = ?",
            (rid, ver, ns),
        )
        if cursor.rowcount != 1:  # pragma: no cover - allocated above
            raise CorruptStore("namespace_state row disappeared during activation")
        return revision

    # -------------------------------------------------------- authorization

    def record_authorization(
        self,
        *,
        namespace: str,
        operation: str,
        target_kind: str,
        target_id: str,
        target_version: int | None,
        authorization_ref: str,
    ) -> None:
        """Append one non-sensitive audit row inside the caller's transaction.

        The audit row records who was credited for an admin operation and which
        exact version it produced.  It is not an authentication model: the
        label is supplied by the caller that owns the admin entry point, exactly
        like ``deletion_targets.authorization_ref``.
        """

        if self._tx_depth == 0:
            raise RuntimeError(
                "record_authorization must run inside the caller's transaction"
            )
        ns = identifiers.require_namespace(namespace)
        op = identifiers.require_operation(operation)
        if target_kind not in _DOMAIN_AUDIT_KINDS:
            raise InvalidInput(
                f"target_kind must be one of {', '.join(_DOMAIN_AUDIT_KINDS)}"
            )
        target = (
            identifiers.require_record_id(target_id)
            if target_kind == DELETION_RECORD
            else identifiers.require_event_id(target_id)
        )
        version = (
            None
            if target_version is None
            else identifiers.require_record_version(target_version)
        )
        authorization = require_authorization_ref(authorization_ref)
        self._require_connection().execute(
            "INSERT INTO authorization_audit ("
            "audit_id, namespace, operation, target_kind, target_id, "
            "target_version, authorization_ref, recorded_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                ns,
                op,
                target_kind,
                target,
                version,
                authorization,
                utc_now_rfc3339(),
            ),
        )

    # ------------------------------------------------------- episode sources

    def episode_source(self, namespace: str, event_id: str) -> EpisodeSource | None:
        """The episode mapping for one source event, if any."""

        identifiers.require_namespace(namespace)
        identifiers.require_event_id(event_id)
        row = self._require_connection().execute(
            f"SELECT {_EPISODE_SOURCE_COLUMNS} FROM episode_sources "
            "WHERE namespace = ? AND event_id = ?",
            (namespace, event_id),
        ).fetchone()
        return None if row is None else _episode_source_from_row(row)

    def episode_sources_for_record(
        self, namespace: str, record_id: str
    ) -> tuple[EpisodeSource, ...]:
        """Every source event mapped to one episode record, ordered by event ID."""

        identifiers.require_namespace(namespace)
        identifiers.require_record_id(record_id)
        rows = self._require_connection().execute(
            f"SELECT {_EPISODE_SOURCE_COLUMNS} FROM episode_sources "
            "WHERE namespace = ? AND record_id = ? ORDER BY event_id",
            (namespace, record_id),
        ).fetchall()
        return tuple(_episode_source_from_row(row) for row in rows)

    def insert_episode_source(
        self,
        *,
        namespace: str,
        event_id: str,
        record_id: str,
        record_version: int,
    ) -> EpisodeSource:
        """Claim the (namespace, event_id) source key inside the caller's tx.

        The primary key is the concurrency backstop: two writers that both pass
        their own pre-checks still produce one mapping, and the loser gets
        :class:`EpisodeSourceExists` (the domain service then re-reads the
        existing mapping).  This is deliberately not a "look first" API.
        """

        if self._tx_depth == 0:
            raise RuntimeError(
                "insert_episode_source must run inside the caller's transaction"
            )
        ns = identifiers.require_namespace(namespace)
        eid = identifiers.require_event_id(event_id)
        rid = identifiers.require_record_id(record_id)
        ver = identifiers.require_record_version(record_version)
        now = utc_now_rfc3339()
        try:
            self._require_connection().execute(
                "INSERT INTO episode_sources ("
                "namespace, event_id, record_id, record_version, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (ns, eid, rid, ver, now),
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if (
                "UNIQUE constraint failed" in message
                and "episode_sources" in message
            ):
                raise EpisodeSourceExists(
                    f"source event {eid} already maps to an episode"
                ) from exc
            raise
        return EpisodeSource(
            namespace=ns,
            event_id=eid,
            record_id=rid,
            record_version=ver,
            created_at=now,
        )


class DomainStore(DomainStateMixin, ControlStore):
    """PCS2-007 domain reads on top of the PCS2-006 control plane."""


def require_authorization_ref(value: Any) -> str:
    """An admin entry point must name its authorization explicitly."""

    if not isinstance(value, str) or not value.strip():
        raise AuthorizationRequired(
            "this operation requires an explicit, non-empty authorization_ref"
        )
    if len(value) > AUTHORIZATION_REF_MAX_LENGTH:
        raise AuthorizationRequired(
            f"authorization_ref must be at most {AUTHORIZATION_REF_MAX_LENGTH} characters"
        )
    return value


def _episode_source_from_row(row: Any) -> EpisodeSource:
    return EpisodeSource(
        namespace=row["namespace"],
        event_id=row["event_id"],
        record_id=row["record_id"],
        record_version=int(row["record_version"]),
        created_at=row["created_at"],
    )


__all__ = [
    "AUTHORIZATION_REF_MAX_LENGTH",
    "ActiveProfilePointer",
    "DomainStateMixin",
    "DomainStore",
    "EpisodeSource",
    "EpisodeSourceExists",
    "EventHeader",
    "RecordPointer",
    "require_authorization_ref",
]
