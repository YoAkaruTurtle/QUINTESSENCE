"""PCS2-005: the event journal and Tx A.

Appendix J.2 fixes the Event envelope and the source idempotency key
``(namespace, source_instance, source_event_id)``.  Appendix J.6 fixes the
short transaction implemented here::

    validate the entry -> deduplicate by source key (a different payload is
    ID_CONFLICT) -> write the Event and its controlled payload -> write
    pending_work -> advance last_event_seq -> commit

J.7 fixes what must *not* happen here: accepting an event never reserves a
``state_revision``.  Domain commits and accepted events are two separate
watermarks (``state_revision`` and ``last_event_seq``) and stay separate.

Three properties are structural rather than cached:

* idempotency is the UNIQUE ``(namespace, source_instance, source_event_id)``
  constraint on ``event_journal``, so it survives closing the connection, a
  restart and a different process;
* the receive sequence is allocated from ``namespace_state.last_event_seq``
  inside the same transaction, so a rolled-back ingest leaves no hole and a
  duplicate consumes no number;
* the event body lives in ``private_payloads`` (shared with PCS2-004) and the
  journal only carries ``payload_ref``; a read re-verifies the address, the
  digest and the canonical form.

Causation edges are recorded in ``event_causes``.  A reference to an event that
has not arrived yet is legal and is stored with ``status='pending'``; this card
implements no resolution, so an edge never changes status here.  ``event_seq``
is receive order only and is never used to order causes.

``docs/PCS2-005-EVENTS.md`` holds the design notes and the acceptance mapping.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..foundation import identifiers
from ..foundation.errors import (
    CorruptStore,
    EventNotFound,
    InvalidInput,
    ScopeViolation,
    SourceEventConflict,
)
from ..foundation.jsonutil import (
    canonical_json_text,
    sha256_hex,
    strict_json_loads,
)
from ..foundation.timeutil import utc_now_rfc3339
from .records import RecordStore

#: Closed set of ingest outcomes (ADR-0001 D4: an operation's outcome enum is
#: closed and unknown values are rejected).
OUTCOME_CREATED = "created"
OUTCOME_DUPLICATE = "duplicate"
OUTCOMES = (OUTCOME_CREATED, OUTCOME_DUPLICATE)

#: The only kind of work this card writes.  PCS2-007/016 introduce real task
#: kinds together with the consumer that understands them.
PENDING_WORK_KIND = "event.derive"

CAUSE_PENDING = "pending"
CAUSE_RESOLVED = "resolved"

_CAUSE_REF_KEYS = frozenset(("ref_type", "namespace", "event_id"))

_EVENT_COLUMNS = (
    "namespace, event_id, event_seq, source_kind, source_instance, source_event_id, "
    "source_position, event_type, observed_at, recorded_at, payload_ref, privacy_class"
)


@dataclass(frozen=True)
class IngestReceipt:
    """Result of one ingest attempt; the same shape for created and duplicate."""

    outcome: str
    event_id: str
    event_seq: int
    namespace: str


@dataclass(frozen=True)
class EventJournalEntry:
    """One stored event as read back from the journal and its payload area."""

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
    payload_ref: str
    payload_sha256: str
    privacy_class: str
    payload: dict[str, Any]
    payload_text: str


@dataclass(frozen=True)
class EventCause:
    """One causation edge from a stored event to a (possibly absent) event."""

    namespace: str
    from_event_id: str
    to_event_id: str
    status: str


@dataclass(frozen=True)
class PendingWork:
    """One pending-work row written inside Tx A; this card never consumes it."""

    work_id: str
    namespace: str
    kind: str
    event_id: str
    source_event_id: str
    status: str
    created_at: str


class EventStore(RecordStore):
    """Event ingestion on the PCS2-004 database, one short transaction per event.

    The class deliberately builds on ``RecordStore``: events, records and
    payloads share one database, one migration ledger and one
    ``transaction()`` implementation.  ``EventStore.open(...)`` is inherited and
    returns an ``EventStore``.
    """

    # ---------------------------------------------------------------- ingest

    def ingest_event(
        self,
        *,
        namespace: str,
        source_kind: str,
        source_instance: str,
        source_event_id: str,
        event_type: str,
        observed_at: str | None = None,
        payload: Any,
        privacy_class: str = "private",
        causation_refs: Sequence[Mapping[str, Any]] = (),
        source_position: int | None = None,
        recorded_at: str | None = None,
    ) -> IngestReceipt:
        """Accept one source event through Tx A.

        ``payload`` is a JSON object (a mapping) or its JSON text.  Either way it
        is normalized to canonical JSON first: sorted keys, compact separators,
        UTF-8 with no ASCII escaping.  The idempotency decision is made on that
        normalized text, so two deliveries that differ only in key order or
        insignificant whitespace are the same payload and return ``duplicate``.

        A repeat of an accepted key with different payload bytes raises
        :class:`SourceEventConflict` (``ID_CONFLICT``) and changes nothing.
        """

        identifiers.require_namespace(namespace)
        identifiers.require_source_kind(source_kind)
        identifiers.require_instance_id(source_instance)
        identifiers.require_source_event_id(source_event_id)
        identifiers.require_event_type(event_type)
        identifiers.require_privacy_class(privacy_class)
        identifiers.require_source_position(source_position)
        observed = identifiers.require_observed_at(observed_at)
        if recorded_at is None:
            recorded = utc_now_rfc3339()
        else:
            recorded = identifiers.require_recorded_at(recorded_at)
        targets = _normalize_causation_refs(causation_refs, namespace)
        payload_text, payload_digest = _normalize_payload(payload)

        with self.transaction():
            connection = self._require_connection()
            existing = connection.execute(
                "SELECT ej.event_id, ej.event_seq, ej.payload_ref, "
                "pp.sha256 AS payload_sha256, pp.body_json AS payload_body "
                "FROM event_journal ej "
                "LEFT JOIN private_payloads pp ON pp.payload_ref = ej.payload_ref "
                "WHERE ej.namespace = ? AND ej.source_instance = ? AND ej.source_event_id = ?",
                (namespace, source_instance, source_event_id),
            ).fetchone()
            if existing is not None:
                # PCS2-006: a deleted source key is a tombstone, not a
                # duplicate.  This check runs before any payload comparison so
                # a cleared payload can never be mistaken for a conflict.
                self._check_source_redacted(namespace, existing["event_id"])
                if existing["payload_ref"] is None or existing["payload_sha256"] is None:
                    raise CorruptStore(
                        f"event {existing['event_id']} references a missing payload"
                    )
                if not existing["payload_body"]:
                    raise CorruptStore(
                        f"event {existing['event_id']} references a cleared payload"
                    )
                if (
                    existing["payload_sha256"] != payload_digest
                    or existing["payload_body"] != payload_text
                ):
                    raise SourceEventConflict(
                        f"source event {source_event_id!r} of instance "
                        f"{source_instance!r} is already stored in namespace "
                        f"{namespace!r} with different payload bytes"
                    )
                return IngestReceipt(
                    outcome=OUTCOME_DUPLICATE,
                    event_id=existing["event_id"],
                    event_seq=int(existing["event_seq"]),
                    namespace=namespace,
                )

            payload_ref = self._store_payload(payload_text, payload_digest)
            event_seq = self._allocate_event_seq_locked(namespace)
            event_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO event_journal ("
                "namespace, event_id, event_seq, source_kind, source_instance, "
                "source_event_id, source_position, event_type, observed_at, "
                "recorded_at, payload_ref, privacy_class"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    namespace,
                    event_id,
                    event_seq,
                    source_kind,
                    source_instance,
                    source_event_id,
                    source_position,
                    event_type,
                    observed,
                    recorded,
                    payload_ref,
                    privacy_class,
                ),
            )
            for target in targets:
                self._insert_cause_locked(namespace, event_id, target, recorded)
            self._insert_pending_work_locked(namespace, event_id, source_event_id, recorded)
            return IngestReceipt(
                outcome=OUTCOME_CREATED,
                event_id=event_id,
                event_seq=event_seq,
                namespace=namespace,
            )

    def _allocate_event_seq_locked(self, namespace: str) -> int:
        """Advance the namespace event watermark and return the new sequence.

        This never touches ``state_revision`` (J.6/J.7).  A namespace that has
        no domain commit yet gets a row with ``state_revision = 0``.
        """

        if self._tx_depth == 0:  # pragma: no cover - internal invariant
            raise RuntimeError("event sequences may only be allocated inside a transaction")
        connection = self._require_connection()
        row = connection.execute(
            "UPDATE namespace_state SET last_event_seq = last_event_seq + 1 "
            "WHERE namespace = ? RETURNING last_event_seq",
            (namespace,),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO namespace_state (namespace, state_revision, last_event_seq) "
                "VALUES (?, 0, 1)",
                (namespace,),
            )
            return 1
        return int(row[0])

    def _insert_cause_locked(
        self, namespace: str, event_id: str, target: str, created_at: str
    ) -> None:
        exists = self._require_connection().execute(
            "SELECT 1 FROM event_journal WHERE namespace = ? AND event_id = ?",
            (namespace, target),
        ).fetchone()
        status = CAUSE_RESOLVED if exists is not None else CAUSE_PENDING
        self._require_connection().execute(
            "INSERT INTO event_causes (namespace, from_event_id, to_event_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (namespace, event_id, target, status, created_at),
        )

    def _insert_pending_work_locked(
        self, namespace: str, event_id: str, source_event_id: str, created_at: str
    ) -> None:
        self._require_connection().execute(
            "INSERT INTO pending_work ("
            "work_id, namespace, kind, event_id, source_event_id, status, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                namespace,
                PENDING_WORK_KIND,
                event_id,
                source_event_id,
                "pending",
                created_at,
            ),
        )

    # ----------------------------------------------------------------- reads

    def last_event_seq(self, namespace: str) -> int:
        """Current committed event watermark (0 for an unused namespace)."""

        identifiers.require_namespace(namespace)
        row = self._require_connection().execute(
            "SELECT last_event_seq FROM namespace_state WHERE namespace = ?", (namespace,)
        ).fetchone()
        return 0 if row is None else int(row["last_event_seq"])

    def get_event(self, namespace: str, event_id: str) -> EventJournalEntry:
        """Read one event; the payload is re-verified against its own address."""

        identifiers.require_namespace(namespace)
        identifiers.require_event_id(event_id)
        row = self._require_connection().execute(
            f"SELECT {_EVENT_COLUMNS} FROM event_journal "
            "WHERE namespace = ? AND event_id = ?",
            (namespace, event_id),
        ).fetchone()
        if row is None:
            raise EventNotFound(f"no event {event_id} in namespace {namespace}")
        digest, payload, payload_text = self._load_payload(
            row["payload_ref"], owner=f"event {event_id}"
        )
        return EventJournalEntry(
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
            payload_ref=row["payload_ref"],
            payload_sha256=digest,
            privacy_class=row["privacy_class"],
            payload=payload,
            payload_text=payload_text,
        )

    def causes_of(self, namespace: str, event_id: str) -> tuple[EventCause, ...]:
        """Causation edges recorded for *event_id*, ordered by target ID."""

        identifiers.require_namespace(namespace)
        identifiers.require_event_id(event_id)
        if not self._event_exists(namespace, event_id):
            raise EventNotFound(f"no event {event_id} in namespace {namespace}")
        rows = self._require_connection().execute(
            "SELECT namespace, from_event_id, to_event_id, status FROM event_causes "
            "WHERE namespace = ? AND from_event_id = ? ORDER BY to_event_id",
            (namespace, event_id),
        ).fetchall()
        return tuple(
            EventCause(
                namespace=row["namespace"],
                from_event_id=row["from_event_id"],
                to_event_id=row["to_event_id"],
                status=row["status"],
            )
            for row in rows
        )

    def pending_work_for(self, namespace: str, event_id: str) -> tuple[PendingWork, ...]:
        """Work rows created by *event_id*, ordered by ``work_id``."""

        identifiers.require_namespace(namespace)
        identifiers.require_event_id(event_id)
        if not self._event_exists(namespace, event_id):
            raise EventNotFound(f"no event {event_id} in namespace {namespace}")
        rows = self._require_connection().execute(
            "SELECT work_id, namespace, kind, event_id, source_event_id, status, created_at "
            "FROM pending_work WHERE namespace = ? AND event_id = ? ORDER BY work_id",
            (namespace, event_id),
        ).fetchall()
        return tuple(
            PendingWork(
                work_id=row["work_id"],
                namespace=row["namespace"],
                kind=row["kind"],
                event_id=row["event_id"],
                source_event_id=row["source_event_id"],
                status=row["status"],
                created_at=row["created_at"],
            )
            for row in rows
        )

    def _event_exists(self, namespace: str, event_id: str) -> bool:
        row = self._require_connection().execute(
            "SELECT 1 FROM event_journal WHERE namespace = ? AND event_id = ?",
            (namespace, event_id),
        ).fetchone()
        return row is not None

    def _check_source_redacted(self, namespace: str, event_id: str) -> None:
        """PCS2-006 hook; the deletion layer raises ``SOURCE_REDACTED`` here."""

        return None


def _normalize_payload(payload: Any) -> tuple[str, str]:
    """Return ``(canonical_text, sha256)`` for an event payload."""

    if isinstance(payload, str):
        try:
            payload = strict_json_loads(payload)
        except ValueError as exc:
            raise InvalidInput(f"payload text is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidInput("payload must be a JSON object")
    text = canonical_json_text(payload)
    digest = sha256_hex(text)
    return text, digest


def _normalize_causation_refs(
    causation_refs: Sequence[Mapping[str, Any]], namespace: str
) -> tuple[str, ...]:
    """Validate ``causation_refs`` and return the target event IDs in order."""

    if causation_refs is None or isinstance(causation_refs, (str, bytes)):
        raise InvalidInput("causation_refs must be a sequence of EventRef objects")
    targets: list[str] = []
    seen: set[str] = set()
    for ref in causation_refs:
        if not isinstance(ref, Mapping):
            raise InvalidInput(
                "causation_refs entries must be EventRef objects; bare IDs are not accepted"
            )
        if set(ref) != _CAUSE_REF_KEYS:
            raise InvalidInput(
                "causation_refs entries must have exactly ref_type, namespace and event_id"
            )
        if ref["ref_type"] != "event":
            raise InvalidInput(
                "PCS2-005 stores EventRef causation edges only; a RecordRef edge belongs "
                "to record_relations (PCS2-007)"
            )
        ref_namespace = identifiers.require_namespace(ref["namespace"])
        target = identifiers.require_event_id(ref["event_id"])
        if ref_namespace != namespace:
            raise ScopeViolation(
                f"causation reference {target} is in namespace {ref_namespace!r}, "
                f"not {namespace!r}"
            )
        if target in seen:
            raise InvalidInput(f"causation reference {target} is duplicated")
        seen.add(target)
        targets.append(target)
    return tuple(targets)


__all__ = [
    "CAUSE_PENDING",
    "CAUSE_RESOLVED",
    "EventCause",
    "EventJournalEntry",
    "EventStore",
    "IngestReceipt",
    "OUTCOMES",
    "OUTCOME_CREATED",
    "OUTCOME_DUPLICATE",
    "PENDING_WORK_KIND",
    "PendingWork",
]
