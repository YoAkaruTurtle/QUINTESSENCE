"""Storage-layer error types mapped to the pcs.v2 error codes (appendix J.4).

``contracts/v2/error.json`` is the authority for the code vocabulary.  Which
condition produces which code belongs to the state layer; this module is the
single mapping used by the PCS2-004 store and is documented in
``docs/PCS2-004-STORE.md``.
"""

from __future__ import annotations


class StorageError(Exception):
    """Base class for every deliberate storage-layer failure."""

    code = "CORRUPT_STORE"


class InvalidInput(StorageError):
    """A caller-supplied value violates the pcs.v2 structural rules."""

    code = "INVALID_SCHEMA"


class InvalidRecordVersion(InvalidInput):
    """``record_version`` is not a positive JSON-safe integer."""


class RecordNotFound(StorageError):
    """No record or record version exists for the requested key."""

    code = "NOT_FOUND"


class RecordVersionExists(StorageError):
    """The immutable ``(record_id, record_version)`` row already exists."""

    code = "ID_CONFLICT"


class RecordVersionConflict(StorageError):
    """The requested version is neither the current nor the next version."""

    code = "REVISION_CONFLICT"


class RecordKindMismatch(StorageError):
    """A new version tried to change the record's ``kind``."""

    code = "INVALID_TRANSITION"


class EventNotFound(StorageError):
    """No event exists in that namespace for the requested ``event_id``."""

    code = "NOT_FOUND"


class SourceEventConflict(StorageError):
    """The source idempotency key exists with different payload bytes (J.2).

    The first event is kept; a second, different payload for the same
    ``(namespace, source_instance, source_event_id)`` is never stored.
    """

    code = "ID_CONFLICT"


class ScopeViolation(StorageError):
    """A reference points outside the namespace the write belongs to."""

    code = "SCOPE_VIOLATION"


class CorruptStore(StorageError):
    """Stored bytes contradict the store's own invariants."""

    code = "CORRUPT_STORE"


class MigrationError(StorageError):
    """The migration ledger and the migration files disagree."""

    code = "CORRUPT_STORE"


class MigrationRequired(MigrationError):
    """The database has pending migrations and was opened read-only-schema."""

    code = "MIGRATION_REQUIRED"


class SnapshotNotFound(StorageError):
    """The command named a ``context_id`` that is not a persisted snapshot."""

    code = "NOT_FOUND"


class CommandConflict(StorageError):
    """``(namespace, request_id)`` was committed with a different digest."""

    code = "ID_CONFLICT"


class ContextStale(StorageError):
    """A snapshot watermark (privacy epoch or event sequence) has moved."""

    code = "CONTEXT_STALE"


class StateRevisionConflict(StorageError):
    """The domain ``state_revision`` no longer equals the snapshot's value."""

    code = "REVISION_CONFLICT"


class DbBusy(StorageError):
    """The write lock could not be acquired; nothing was written."""

    code = "DB_BUSY"


class Redacted(StorageError):
    """The target is inside the caller's namespace but deleted (J.3/J.6)."""

    code = "REDACTED"


class SourceRedacted(StorageError):
    """A deleted event's source key was offered again (J.2)."""

    code = "SOURCE_REDACTED"


class AuthorizationRequired(StorageError):
    """A deletion request arrived without an explicit, non-empty authorization."""

    code = "SCOPE_VIOLATION"


class InvalidDeletionTarget(InvalidInput):
    """``target_kind`` is not one of the two frozen deletion target kinds."""


class StandaloneTransactionRequired(StorageError):
    """A control-plane entry point was called inside an open transaction.

    Snapshots, the deletion barrier and cleanup batches each own their
    transaction on purpose: a barrier that joins a caller's transaction can be
    rolled back by an unrelated failure, and cleanup progress would not be
    durable until the caller commits.
    """

    code = "INVALID_TRANSITION"


__all__ = [
    "AuthorizationRequired",
    "CommandConflict",
    "ContextStale",
    "CorruptStore",
    "DbBusy",
    "EventNotFound",
    "InvalidDeletionTarget",
    "InvalidInput",
    "InvalidRecordVersion",
    "MigrationError",
    "MigrationRequired",
    "RecordKindMismatch",
    "RecordNotFound",
    "RecordVersionConflict",
    "RecordVersionExists",
    "Redacted",
    "ScopeViolation",
    "SnapshotNotFound",
    "SourceEventConflict",
    "SourceRedacted",
    "StandaloneTransactionRequired",
    "StateRevisionConflict",
    "StorageError",
]
