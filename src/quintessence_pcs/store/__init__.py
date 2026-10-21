"""PCS persistent state.

``RecordStore`` implements the PCS2-004 slice of J.3: a current-version
pointer over an immutable version table, with exact ``(record_id,
record_version)`` reads that never pass through the pointer.

``EventStore`` implements the PCS2-005 slice of J.2/J.6: source-key
idempotency, the receive sequence and Tx A (event + payload + pending work in
one short transaction, without touching ``state_revision``).

``ControlStore`` implements the PCS2-006 slice of J.6/J.7: persisted context
snapshots, Tx B with three watermarks and idempotent receipts, and the
two-phase deletion barrier with resumable payload cleanup.

``DomainStore`` adds the PCS2-007 domain-plane reads and management entries
(active profile pointer, episode sources, authorization audit, snapshot reads)
without changing any earlier behaviour.

``CognitionStore`` adds the PCS2-008 slice: full J.5 Context snapshots with
exact source accounting, the rendered-input payload, ``work_progress`` and the
reverse lookup that clears a Context when one of its sources is deleted.
"""

from __future__ import annotations

from .cognition import (
    CONTEXT_ADAPTER_DIRECT_HTTP,
    CONTEXT_ADAPTER_FAKE_MODEL,
    TX_WORK_OUTCOMES,
    CognitionStore,
    ContextInsert,
    ContextSnapshot,
    ContextSource,
    ContextSourceInput,
    ContextStateMixin,
    MAX_CONTEXT_BYTES,
    ReconcileReport,
    WORK_OUTCOMES,
    WORK_OUTCOME_CHECKED_NO_BELIEF,
    WORK_OUTCOME_COMMITTED,
    WORK_OUTCOME_SKIPPED,
    WORK_SKIP_REASON_SOURCE_REDACTED,
    WorkProgress,
    context_content_hash,
)
from .control import ControlStore
from .deletion import (
    CleanupReport,
    CleanupResult,
    DELETION_CLEANED,
    DELETION_EVENT,
    DELETION_PENDING_CLEANUP,
    DELETION_RECORD,
    DELETION_STATUSES,
    DeletionTarget,
)
from .domain import (
    ActiveProfilePointer,
    DomainStateMixin,
    DomainStore,
    EpisodeSource,
    EpisodeSourceExists,
    EventHeader,
    RecordPointer,
)
from .errors import (
    ContextTooLarge,
    InvalidWorkCompletion,
    NeedsContext,
    WorkAlreadyCompleted,
)
from .events import (
    EventCause,
    EventJournalEntry,
    EventStore,
    IngestReceipt,
    PendingWork,
)
from .model import (
    MAX_ATTEMPT_BODY_BYTES,
    MODEL_ATTEMPT_FAILED,
    MODEL_ATTEMPT_FINISHED,
    MODEL_ATTEMPT_MALFORMED,
    MODEL_ATTEMPT_OK,
    MODEL_ATTEMPT_OUTCOMES,
    MODEL_ATTEMPT_RESERVED,
    MODEL_ATTEMPT_TIMEOUT,
    MODEL_AUTHORIZATION_ACTIVE,
    MODEL_AUTHORIZATION_EXHAUSTED,
    MODEL_AUTHORIZATION_REVOKED,
    ModelAttempt,
    ModelAttemptConflict,
    ModelAttemptNotFound,
    ModelAttemptReservation,
    ModelAuthorization,
    ModelAuthorizationExpired,
    ModelAuthorizationMismatch,
    ModelBudgetExceeded,
    ModelLedgerError,
    ModelLedgerMixin,
    ModelNotAuthorized,
)
from .records import MAX_SCOPE_JSON_LENGTH, RecordStore, RecordVersion
from .tx_b import (
    COMMIT_OUTCOMES,
    CommitReceipt,
    CommittedRecordRef,
    OUTCOME_COMMITTED,
    OUTCOME_NOOP,
    Snapshot,
    TxScope,
)

__all__ = [
    "COMMIT_OUTCOMES",
    "CONTEXT_ADAPTER_DIRECT_HTTP",
    "CONTEXT_ADAPTER_FAKE_MODEL",
    "CleanupReport",
    "CleanupResult",
    "CognitionStore",
    "CommitReceipt",
    "CommittedRecordRef",
    "ContextInsert",
    "ContextSnapshot",
    "ContextSource",
    "ContextSourceInput",
    "ContextStateMixin",
    "ContextTooLarge",
    "ControlStore",
    "DELETION_CLEANED",
    "DELETION_EVENT",
    "DELETION_PENDING_CLEANUP",
    "DELETION_RECORD",
    "DELETION_STATUSES",
    "DeletionTarget",
    "DomainStateMixin",
    "DomainStore",
    "EpisodeSource",
    "EpisodeSourceExists",
    "EventCause",
    "EventHeader",
    "EventJournalEntry",
    "EventStore",
    "IngestReceipt",
    "InvalidWorkCompletion",
    "MAX_CONTEXT_BYTES",
    "MAX_SCOPE_JSON_LENGTH",
    "MAX_ATTEMPT_BODY_BYTES",
    "MODEL_ATTEMPT_FAILED",
    "MODEL_ATTEMPT_FINISHED",
    "MODEL_ATTEMPT_MALFORMED",
    "MODEL_ATTEMPT_OK",
    "MODEL_ATTEMPT_OUTCOMES",
    "MODEL_ATTEMPT_RESERVED",
    "MODEL_ATTEMPT_TIMEOUT",
    "MODEL_AUTHORIZATION_ACTIVE",
    "MODEL_AUTHORIZATION_EXHAUSTED",
    "MODEL_AUTHORIZATION_REVOKED",
    "ModelAttempt",
    "ModelAttemptConflict",
    "ModelAttemptNotFound",
    "ModelAttemptReservation",
    "ModelAuthorization",
    "ModelAuthorizationExpired",
    "ModelAuthorizationMismatch",
    "ModelBudgetExceeded",
    "ModelLedgerError",
    "ModelLedgerMixin",
    "ModelNotAuthorized",
    "NeedsContext",
    "OUTCOME_COMMITTED",
    "OUTCOME_NOOP",
    "PendingWork",
    "RecordPointer",
    "RecordStore",
    "RecordVersion",
    "Snapshot",
    "TxScope",
    "TX_WORK_OUTCOMES",
    "WORK_OUTCOMES",
    "WORK_OUTCOME_CHECKED_NO_BELIEF",
    "WORK_OUTCOME_COMMITTED",
    "WORK_OUTCOME_SKIPPED",
    "WORK_SKIP_REASON_SOURCE_REDACTED",
    "WorkAlreadyCompleted",
    "WorkProgress",
    "ReconcileReport",
    "context_content_hash",
]
