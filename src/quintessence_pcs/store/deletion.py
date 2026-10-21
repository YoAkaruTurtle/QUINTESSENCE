"""PCS2-006 B group: the two-phase deletion barrier (J.2/J.3/J.6/J.7).

State machine (written here and in ``docs/PCS2-006-TXB.md``)::

    ACTIVE --Tx1 commits the barrier--> REDACTED_PENDING_CLEANUP
           --cleanup succeeds--------> REDACTED_CLEANED

Tx1 is one independent write transaction: it inserts the ``deletion_targets``
row, advances ``privacy_epoch`` and ``state_revision`` exactly once, and
commits.  From that commit on every controlled read of the target returns
``REDACTED`` even though the payload body may still be on disk.

Cleanup is a separate, retryable, cross-process-resumable phase.  It clears a
payload body only when no record version and no event outside a deletion
barrier still points at it.  Clearing is committed per payload, so an
interruption keeps the rows already cleared and leaves the target
``pending_cleanup`` with a public ``last_error``; a later run continues.

``private_payloads`` has no triggers and both referencing columns are NOT NULL
foreign keys, so this is plan A from the handoff card: immutable rows are never
rewritten and no guard trigger is added.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from ..foundation import identifiers
from ..foundation.errors import (
    AuthorizationRequired,
    CorruptStore,
    EventNotFound,
    InvalidDeletionTarget,
    InvalidInput,
    Redacted,
    RecordNotFound,
    SourceRedacted,
)
from ..foundation.timeutil import utc_now_rfc3339

DELETION_PENDING_CLEANUP = "pending_cleanup"
DELETION_CLEANED = "cleaned"
DELETION_STATUSES = (DELETION_PENDING_CLEANUP, DELETION_CLEANED)
DELETION_RECORD = "record"
DELETION_EVENT = "event"
DELETION_TARGET_KINDS = (DELETION_RECORD, DELETION_EVENT)

AUTHORIZATION_REF_MAX_LENGTH = 512
LAST_ERROR_MAX_LENGTH = 1024

_TARGET_COLUMNS = (
    "target_kind, namespace, target_id, status, authorization_ref, "
    "created_at, updated_at, last_error"
)


@dataclass(frozen=True)
class DeletionTarget:
    """One durable deletion barrier row."""

    target_kind: str
    namespace: str
    target_id: str
    status: str
    authorization_ref: str
    created_at: str
    updated_at: str
    last_error: str | None


@dataclass(frozen=True)
class CleanupResult:
    """Outcome of trying to clean one target."""

    target: DeletionTarget
    cleared_payloads: int
    error: str | None


@dataclass(frozen=True)
class CleanupReport:
    """Outcome of one cleanup pass, one result per pending target."""

    results: tuple[CleanupResult, ...]

    @property
    def cleaned_count(self) -> int:
        return sum(
            1
            for result in self.results
            if result.error is None and result.target.status == DELETION_CLEANED
        )

    @property
    def failed_count(self) -> int:
        return sum(1 for result in self.results if result.error is not None)

    @property
    def cleared_payloads(self) -> int:
        return sum(result.cleared_payloads for result in self.results)


class DeletionMixin:
    """B-group behaviour composed into
    :class:`~quintessence_pcs.store.control.ControlStore`.

    The read overrides form the logical barrier: after Tx1 commits they answer
    ``REDACTED`` for callers inside the target's namespace.  A read in another
    namespace never finds the row at all, so it keeps answering ``NOT_FOUND``;
    that namespace check is the minimal stand-in for an authorization layer
    (see the design document).
    """

    # ------------------------------------------------------------ read barrier

    def get_version(self, namespace: str, record_id: str, record_version: int) -> Any:
        identifiers.require_namespace(namespace)
        identifiers.require_record_id(record_id)
        self._raise_if_record_redacted(namespace, record_id)
        return super().get_version(namespace, record_id, record_version)

    def get_current(self, namespace: str, record_id: str) -> Any:
        identifiers.require_namespace(namespace)
        identifiers.require_record_id(record_id)
        self._raise_if_record_redacted(namespace, record_id)
        return super().get_current(namespace, record_id)

    def get_event(self, namespace: str, event_id: str) -> Any:
        identifiers.require_namespace(namespace)
        identifiers.require_event_id(event_id)
        self._raise_if_event_redacted(namespace, event_id)
        return super().get_event(namespace, event_id)

    def causes_of(self, namespace: str, event_id: str) -> Any:
        identifiers.require_namespace(namespace)
        identifiers.require_event_id(event_id)
        self._raise_if_event_redacted(namespace, event_id)
        return super().causes_of(namespace, event_id)

    def pending_work_for(self, namespace: str, event_id: str) -> Any:
        identifiers.require_namespace(namespace)
        identifiers.require_event_id(event_id)
        self._raise_if_event_redacted(namespace, event_id)
        return super().pending_work_for(namespace, event_id)

    def _check_record_writable(self, namespace: str, record_id: str) -> None:
        """Write barrier: a deleted record may not receive a new version."""

        if self._record_is_deleted(namespace, record_id):
            raise Redacted(
                f"record {record_id} has been deleted; new versions are not accepted"
            )

    def _check_source_redacted(self, namespace: str, event_id: str) -> None:
        """Source resurrection barrier (J.2): deleted keys are tombstones."""

        if self._event_is_deleted(namespace, event_id):
            raise SourceRedacted(
                f"source event {event_id} has been deleted; its content cannot be restored"
            )

    def _raise_if_record_redacted(self, namespace: str, record_id: str) -> None:
        if self._record_is_deleted(namespace, record_id):
            raise Redacted(f"record {record_id} has been deleted")

    def _raise_if_event_redacted(self, namespace: str, event_id: str) -> None:
        if self._event_is_deleted(namespace, event_id):
            raise Redacted(f"event {event_id} has been deleted")

    # ------------------------------------------------------------------- Tx1

    def request_deletion(
        self,
        *,
        target_kind: str,
        namespace: str,
        target_id: str,
        authorization_ref: str,
    ) -> DeletionTarget:
        """Commit the deletion barrier for one record or event (Tx1).

        The caller must name an authorization explicitly; the string is stored
        verbatim in ``deletion_targets`` and never interpreted by this card.
        A repeated request for an already-barriered target returns the existing
        row and moves no watermark.
        """

        self._require_standalone("request_deletion")
        kind = _require_target_kind(target_kind)
        ns = identifiers.require_namespace(namespace)
        if kind == DELETION_RECORD:
            target = identifiers.require_record_id(target_id)
        else:
            target = identifiers.require_event_id(target_id)
        authorization = _require_authorization_ref(authorization_ref)

        with self.transaction():
            existing = self._load_target_locked(ns, kind, target)
            if existing is not None:
                return _target_from_row(existing)

            connection = self._require_connection()
            if kind == DELETION_RECORD:
                exists = connection.execute(
                    "SELECT 1 FROM records WHERE namespace = ? AND record_id = ?",
                    (ns, target),
                ).fetchone()
                if exists is None:
                    raise RecordNotFound(
                        f"record {target} does not exist in namespace {ns!r}"
                    )
            else:
                exists = connection.execute(
                    "SELECT 1 FROM event_journal WHERE namespace = ? AND event_id = ?",
                    (ns, target),
                ).fetchone()
                if exists is None:
                    raise EventNotFound(
                        f"event {target} does not exist in namespace {ns!r}"
                    )

            now = utc_now_rfc3339()
            connection.execute(
                "INSERT INTO deletion_targets ("
                "target_kind, namespace, target_id, status, authorization_ref, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    ns,
                    target,
                    DELETION_PENDING_CLEANUP,
                    authorization,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO namespace_state (namespace, state_revision, privacy_epoch) "
                "VALUES (?, 1, 1) "
                "ON CONFLICT(namespace) DO UPDATE SET "
                "state_revision = namespace_state.state_revision + 1, "
                "privacy_epoch = namespace_state.privacy_epoch + 1",
                (ns,),
            )
            # PCS2-008-R1 hook: a deleted event's still-unfinished work must
            # reach a terminal state in this same transaction.  The default is
            # a no-op, so PCS2-006/007 semantics are unchanged.  If the hook
            # cannot write the terminal row, the barrier rolls back as a whole.
            self._after_deletion_barrier_locked(
                DeletionTarget(
                    target_kind=kind,
                    namespace=ns,
                    target_id=target,
                    status=DELETION_PENDING_CLEANUP,
                    authorization_ref=authorization,
                    created_at=now,
                    updated_at=now,
                    last_error=None,
                )
            )
            return DeletionTarget(
                target_kind=kind,
                namespace=ns,
                target_id=target,
                status=DELETION_PENDING_CLEANUP,
                authorization_ref=authorization,
                created_at=now,
                updated_at=now,
                last_error=None,
            )

    # ---------------------------------------------------------------- cleanup

    def cleanup_deletions(
        self,
        *,
        namespace: str | None = None,
        limit: int | None = None,
    ) -> CleanupReport:
        """Continue every pending cleanup, one durable step at a time.

        Retrying is always safe: already-cleared payloads are skipped, the
        barrier watermarks are untouched, and finishing an already-cleaned
        target changes nothing.
        """

        self._require_standalone("cleanup_deletions")
        sql = (
            f"SELECT {_TARGET_COLUMNS} FROM deletion_targets "
            "WHERE status = ?"
        )
        parameters: list[Any] = [DELETION_PENDING_CLEANUP]
        if namespace is not None:
            sql += " AND namespace = ?"
            parameters.append(identifiers.require_namespace(namespace))
        sql += " ORDER BY created_at, target_kind, target_id"
        if limit is not None:
            if type(limit) is not int or limit < 1:
                raise InvalidInput("limit must be a positive integer or null")
            sql += " LIMIT ?"
            parameters.append(limit)
        rows = self._require_connection().execute(sql, tuple(parameters)).fetchall()
        return CleanupReport(
            results=tuple(self._cleanup_one(_target_from_row(row)) for row in rows)
        )

    def _cleanup_one(self, target: DeletionTarget) -> CleanupResult:
        cleared = 0
        try:
            for payload_ref in self._payload_refs_for_target(target):
                with self.transaction():
                    cleared += self._clear_payload_if_unreferenced_locked(payload_ref)
            with self.transaction():
                # PCS2-008 hook: derived copies that used this target (rendered
                # Context payloads) are cleared in the same transaction that
                # marks the target cleaned.  The default is a no-op, so the
                # PCS2-006 semantics are unchanged for stores without it.
                self._after_target_cleared_locked(target)
                self._mark_cleaned_locked(target)
            final = self._load_public_target(target)
            return CleanupResult(target=final, cleared_payloads=cleared, error=None)
        except Exception as exc:  # noqa: BLE001 - the report is the error channel
            message = _public_error(exc)
            try:
                with self.transaction():
                    self._record_cleanup_error_locked(target, message)
            except Exception:  # pragma: no cover - keep the original failure
                pass
            return CleanupResult(
                target=self._load_public_target(target),
                cleared_payloads=cleared,
                error=message,
            )

    def _payload_refs_for_target(self, target: DeletionTarget) -> tuple[str, ...]:
        connection = self._require_connection()
        if target.target_kind == DELETION_RECORD:
            rows = connection.execute(
                "SELECT body_payload_ref FROM record_versions "
                "WHERE namespace = ? AND record_id = ? ORDER BY record_version",
                (target.namespace, target.target_id),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT payload_ref FROM event_journal "
                "WHERE namespace = ? AND event_id = ?",
                (target.namespace, target.target_id),
            ).fetchall()
        if not rows:
            raise CorruptStore(
                f"deletion target {target.target_kind} {target.target_id} has no stored rows"
            )
        seen: set[str] = set()
        refs: list[str] = []
        for row in rows:
            ref = str(row[0])
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
        return tuple(refs)

    def _clear_payload_if_unreferenced_locked(self, payload_ref: str) -> int:
        if self._payload_is_live_referenced_locked(payload_ref):
            return 0
        cursor = self._require_connection().execute(
            "UPDATE private_payloads SET body_json = '', sha256 = ? "
            "WHERE payload_ref = ? AND body_json <> ''",
            (_tombstone_digest(), payload_ref),
        )
        return 1 if cursor.rowcount else 0

    def _payload_is_live_referenced_locked(self, payload_ref: str) -> bool:
        """True when a record version or event outside any barrier points here.

        "Revoked" has no separate state in this card: a reference is live
        exactly when its owning record/event has no ``deletion_targets`` row.
        A later revocation card must extend this query together with its own
        read semantics; see ``docs/PCS2-006-TXB.md`` section 4.5.
        """

        connection = self._require_connection()
        record_hit = connection.execute(
            "SELECT 1 FROM record_versions rv "
            "WHERE rv.body_payload_ref = ? AND NOT EXISTS ("
            "  SELECT 1 FROM deletion_targets dt "
            "  WHERE dt.target_kind = 'record' AND dt.namespace = rv.namespace "
            "    AND dt.target_id = rv.record_id"
            ") LIMIT 1",
            (payload_ref,),
        ).fetchone()
        if record_hit is not None:
            return True
        event_hit = connection.execute(
            "SELECT 1 FROM event_journal ej "
            "WHERE ej.payload_ref = ? AND NOT EXISTS ("
            "  SELECT 1 FROM deletion_targets dt "
            "  WHERE dt.target_kind = 'event' AND dt.namespace = ej.namespace "
            "    AND dt.target_id = ej.event_id"
            ") LIMIT 1",
            (payload_ref,),
        ).fetchone()
        return event_hit is not None

    def _mark_cleaned_locked(self, target: DeletionTarget) -> None:
        self._require_connection().execute(
            "UPDATE deletion_targets SET status = ?, updated_at = ?, last_error = NULL "
            "WHERE namespace = ? AND target_kind = ? AND target_id = ? "
            "AND status = ?",
            (
                DELETION_CLEANED,
                utc_now_rfc3339(),
                target.namespace,
                target.target_kind,
                target.target_id,
                DELETION_PENDING_CLEANUP,
            ),
        )

    def _after_deletion_barrier_locked(self, target: DeletionTarget) -> int:
        """Hook for work whose source is the target; PCS2-006 has none.

        Runs inside the Tx1 transaction, after the barrier row and the
        ``privacy_epoch``/``state_revision`` bump.  PCS2-008 uses it to mark the
        deleted event's unfinished ``pending_work`` as ``skipped`` (no Context,
        no watermark movement).  The default keeps the frozen 006 behaviour.
        """

        return 0

    def _after_target_cleared_locked(self, target: DeletionTarget) -> int:
        """Hook for derived copies; PCS2-006 has none, PCS2-008 clears Contexts.

        Runs inside the cleanup transaction *after* the target's own payloads
        have been cleared and *before* the target is marked cleaned.  If the
        hook fails, the whole transaction rolls back and the target stays
        ``pending_cleanup``, so a later cleanup retries both steps.
        """

        return 0

    def _record_cleanup_error_locked(self, target: DeletionTarget, message: str) -> None:
        self._require_connection().execute(
            "UPDATE deletion_targets SET last_error = ?, updated_at = ? "
            "WHERE namespace = ? AND target_kind = ? AND target_id = ? "
            "AND status = ?",
            (
                message,
                utc_now_rfc3339(),
                target.namespace,
                target.target_kind,
                target.target_id,
                DELETION_PENDING_CLEANUP,
            ),
        )

    # ------------------------------------------------------------- inspection

    def get_deletion_target(
        self, *, target_kind: str, namespace: str, target_id: str
    ) -> DeletionTarget | None:
        """Control-plane read of one barrier row (never contains a body)."""

        kind = _require_target_kind(target_kind)
        ns = identifiers.require_namespace(namespace)
        if kind == DELETION_RECORD:
            target = identifiers.require_record_id(target_id)
        else:
            target = identifiers.require_event_id(target_id)
        row = self._load_target_locked(ns, kind, target)
        return None if row is None else _target_from_row(row)

    def deletion_targets(
        self, *, namespace: str | None = None, status: str | None = None
    ) -> tuple[DeletionTarget, ...]:
        """List barrier rows, newest last; for recovery and tests."""

        sql = f"SELECT {_TARGET_COLUMNS} FROM deletion_targets"
        clauses: list[str] = []
        parameters: list[Any] = []
        if namespace is not None:
            clauses.append("namespace = ?")
            parameters.append(identifiers.require_namespace(namespace))
        if status is not None:
            if status not in DELETION_STATUSES:
                raise InvalidInput(
                    f"status must be one of {', '.join(DELETION_STATUSES)}"
                )
            clauses.append("status = ?")
            parameters.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, target_kind, target_id"
        rows = self._require_connection().execute(sql, tuple(parameters)).fetchall()
        return tuple(_target_from_row(row) for row in rows)

    # -------------------------------------------------------------- internals

    def _record_is_deleted(self, namespace: str, record_id: str) -> bool:
        row = self._require_connection().execute(
            "SELECT 1 FROM deletion_targets "
            "WHERE namespace = ? AND target_kind = ? AND target_id = ?",
            (namespace, DELETION_RECORD, record_id),
        ).fetchone()
        return row is not None

    def _event_is_deleted(self, namespace: str, event_id: str) -> bool:
        row = self._require_connection().execute(
            "SELECT 1 FROM deletion_targets "
            "WHERE namespace = ? AND target_kind = ? AND target_id = ?",
            (namespace, DELETION_EVENT, event_id),
        ).fetchone()
        return row is not None

    def _load_target_locked(
        self, namespace: str, target_kind: str, target_id: str
    ) -> Any:
        return self._require_connection().execute(
            f"SELECT {_TARGET_COLUMNS} FROM deletion_targets "
            "WHERE namespace = ? AND target_kind = ? AND target_id = ?",
            (namespace, target_kind, target_id),
        ).fetchone()

    def _load_public_target(self, target: DeletionTarget) -> DeletionTarget:
        row = self._load_target_locked(
            target.namespace, target.target_kind, target.target_id
        )
        if row is None:  # pragma: no cover - rows are never deleted in this card
            raise CorruptStore("deletion target disappeared during cleanup")
        return _target_from_row(row)


def _require_target_kind(value: Any) -> str:
    if value not in DELETION_TARGET_KINDS:
        raise InvalidDeletionTarget(
            f"target_kind must be one of {', '.join(DELETION_TARGET_KINDS)}"
        )
    return value


def _require_authorization_ref(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthorizationRequired(
            "deletion requires an explicit, non-empty authorization_ref"
        )
    if len(value) > AUTHORIZATION_REF_MAX_LENGTH:
        raise AuthorizationRequired(
            f"authorization_ref must be at most {AUTHORIZATION_REF_MAX_LENGTH} characters"
        )
    return value


def _tombstone_digest() -> str:
    """Overwrite a cleared payload's digest with an unrelated tombstone value."""

    return secrets.token_hex(32)


def _public_error(exc: Exception) -> str:
    text = str(exc)
    message = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return message[:LAST_ERROR_MAX_LENGTH]


def _target_from_row(row: Any) -> DeletionTarget:
    return DeletionTarget(
        target_kind=row["target_kind"],
        namespace=row["namespace"],
        target_id=row["target_id"],
        status=row["status"],
        authorization_ref=row["authorization_ref"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_error=row["last_error"],
    )


__all__ = [
    "AUTHORIZATION_REF_MAX_LENGTH",
    "CleanupReport",
    "CleanupResult",
    "DELETION_CLEANED",
    "DELETION_EVENT",
    "DELETION_PENDING_CLEANUP",
    "DELETION_RECORD",
    "DELETION_STATUSES",
    "DELETION_TARGET_KINDS",
    "DeletionMixin",
    "DeletionTarget",
    "LAST_ERROR_MAX_LENGTH",
]
