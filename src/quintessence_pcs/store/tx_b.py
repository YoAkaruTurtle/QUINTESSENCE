"""PCS2-006 A group: Tx B, snapshots and idempotent commands (J.4/J.6/J.7).

A snapshot is captured and persisted by the store, never reported by the
caller.  ``commit_command`` reads its three watermarks from that persisted row,
computes the request digest itself, and runs the caller's ``apply`` inside one
write transaction together with the receipt.

The transaction protocol is intentionally narrow:

* the snapshot must exist (``NOT_FOUND``) and its namespace must match the
  command's expected namespace when the caller supplies one
  (``SCOPE_VIOLATION``);
* ``(namespace, request_id)`` that already committed with the same digest
  returns the stored receipt and writes nothing; a different digest is
  ``ID_CONFLICT``;
* watermarks are checked in the fixed order ``privacy_epoch`` ->
  ``last_event_seq`` -> ``state_revision``, so a deletion always surfaces as
  ``CONTEXT_STALE`` even when a domain commit also moved the revision;
* any mismatch writes nothing and occupies no ``request_id``;
* a real domain write advances ``state_revision`` exactly once per transaction;
  a legal no-op stores a receipt and does not advance it;
* a failed ``apply`` rolls back the whole transaction, so the same
  ``request_id`` can be retried;
* lock contention becomes ``DB_BUSY`` instead of a bare ``OperationalError``.

``apply`` receives a :class:`TxScope`, not the connection.  That is an API
boundary, not a security boundary: see ``docs/PCS2-006-TXB.md`` section 6.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..foundation import identifiers
from ..foundation.errors import (
    CommandConflict,
    ContextStale,
    CorruptStore,
    InvalidInput,
    ScopeViolation,
    SnapshotNotFound,
    StateRevisionConflict,
)
from ..foundation.jsonutil import canonical_json_text, sha256_hex, strict_json_loads
from ..foundation.timeutil import utc_now_rfc3339
from .errors import InvalidWorkCompletion

OUTCOME_COMMITTED = "committed"
OUTCOME_NOOP = "noop"
COMMIT_OUTCOMES = (OUTCOME_COMMITTED, OUTCOME_NOOP)

_SNAPSHOT_COLUMNS = (
    "context_id, namespace, state_revision, context_event_seq, privacy_epoch, created_at"
)
_RECEIPT_KEYS = frozenset(
    {
        "request_id",
        "namespace",
        "operation",
        "context_id",
        "outcome",
        "state_revision",
        "record_refs",
        "committed_at",
    }
)


@dataclass(frozen=True)
class Snapshot:
    """One persisted context snapshot (J.5 watermarks, without payloads)."""

    context_id: str
    namespace: str
    state_revision: int
    context_event_seq: int
    privacy_epoch: int
    created_at: str


@dataclass(frozen=True)
class CommittedRecordRef:
    """Non-sensitive reference stored in a receipt: IDs and versions only."""

    record_id: str
    record_version: int


@dataclass(frozen=True)
class CommitReceipt:
    """Result of one ``commit_command`` call, also stored verbatim."""

    request_id: str
    namespace: str
    operation: str
    context_id: str
    outcome: str
    state_revision: int
    record_refs: tuple[CommittedRecordRef, ...]
    committed_at: str


class TxScope:
    """Restricted transaction view handed to ``apply``.

    It exposes exactly two write paths, both bound to the snapshot:

    * ``append_record_version`` in the snapshot's namespace;
    * ``complete_work`` for one pending-work row of that namespace (PCS2-008).

    It deliberately has no ``connection`` / ``execute`` / ``delete_*`` member,
    so the ordinary proposal path cannot reach SQL or the deletion API.  Python
    does not enforce this against code that reaches into ``_store``; that is
    documented as an API boundary, not a security boundary.
    """

    __slots__ = (
        "_store",
        "_namespace",
        "_appended",
        "_write_failed",
        "_context_id",
        "_completed_work",
    )

    _FORBIDDEN = frozenset(
        {
            "connection",
            "execute",
            "executescript",
            "cursor",
            "commit",
            "rollback",
            "close",
            "transaction",
            "next_state_revision",
            "delete_record",
            "delete_event",
            "delete_target",
            "request_deletion",
            "cleanup_deletions",
        }
    )

    def __init__(self, store: Any, namespace: str, context_id: str | None = None) -> None:
        self._store = store
        self._namespace = namespace
        self._context_id = context_id
        self._appended: list[CommittedRecordRef] = []
        self._write_failed = False
        self._completed_work: str | None = None

    @property
    def namespace(self) -> str:
        """Namespace bound by the snapshot; ``apply`` cannot choose another."""

        return self._namespace

    @property
    def appended(self) -> tuple[CommittedRecordRef, ...]:
        """Record versions this scope appended so far, in call order."""

        return tuple(self._appended)

    def append_record_version(
        self,
        *,
        record_id: str,
        record_version: int,
        kind: str,
        body: Mapping[str, Any],
        status: str = "active",
        schema_version: str = "1",
        privacy_class: str = "private",
        recorded_at: str | None = None,
        scope: Mapping[str, Any] | None = None,
    ) -> Any:
        """Append one immutable version inside the surrounding transaction."""

        if self._completed_work is not None:
            raise InvalidWorkCompletion(
                "complete_work() must be the last write of a step; no record "
                "versions may be appended after it"
            )
        try:
            version = self._store._append_record_version_locked(
                namespace=self._namespace,
                record_id=record_id,
                record_version=record_version,
                kind=kind,
                body=dict(body) if isinstance(body, Mapping) else body,
                status=status,
                schema_version=schema_version,
                privacy_class=privacy_class,
                recorded_at=recorded_at,
                scope=scope,
            )
        except BaseException:
            # Even if the caller catches this, the command must not commit a
            # half-finished transaction with a reserved revision.
            self._write_failed = True
            raise
        self._appended.append(
            CommittedRecordRef(record_id=record_id, record_version=record_version)
        )
        return version

    def complete_work(
        self,
        *,
        work_id: str,
        context_id: str,
        outcome: str,
    ) -> Any:
        """Mark one pending work item finished **inside this transaction**.

        This is the only domain-level write PCS2-008 adds to ``TxScope``.  It
        deliberately cannot reach a connection, the deletion API or another
        namespace: the work id must belong to the snapshot's namespace and the
        ``context_id`` must be the snapshot this scope was opened for.  The
        completion row commits or rolls back with every record version the
        same ``apply`` appended, which closes the "commit first, then record
        progress" crash window.

        ``outcome`` must agree with what the transaction actually did:

        * ``committed`` requires at least one appended record version;
        * ``checked_no_belief`` requires no appended record version (a legal
          WAIT / "no new judgment" step).

        Calling it twice is an ``INVALID_TRANSITION``; appending after it is
        likewise rejected, so ``completed_state_revision`` can never be stale.
        """

        if self._context_id is None:  # pragma: no cover - only commit_command builds scopes
            raise InvalidInput(
                "this transaction scope is not bound to a context and cannot complete work"
            )
        if context_id != self._context_id:
            raise ScopeViolation(
                "complete_work() must name the context this transaction was opened for"
            )
        if self._completed_work is not None:
            raise InvalidWorkCompletion(
                "complete_work() may only be called once per transaction"
            )
        if outcome not in ("committed", "checked_no_belief"):
            raise InvalidInput(
                "outcome must be 'committed' or 'checked_no_belief'"
            )
        if outcome == "committed" and not self._appended:
            raise InvalidWorkCompletion(
                "outcome 'committed' requires at least one appended record version"
            )
        if outcome == "checked_no_belief" and self._appended:
            raise InvalidWorkCompletion(
                "outcome 'checked_no_belief' cannot carry appended record versions"
            )
        revision = self._store._tx_revisions.get(self._namespace)
        if revision is None:
            revision = self._store._watermarks_locked(self._namespace)["state_revision"]
        progress = self._store._insert_work_progress_locked(
            namespace=self._namespace,
            work_id=work_id,
            context_id=context_id,
            outcome=outcome,
            completed_state_revision=revision,
        )
        self._completed_work = work_id
        return progress

    def __getattr__(self, name: str) -> Any:
        if name in self._FORBIDDEN or name.startswith("delete_"):
            raise AttributeError(
                f"TxScope does not expose {name!r}; a proposal cannot delete "
                "or reach the database connection"
            )
        raise AttributeError(f"TxScope has no attribute {name!r}")

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"TxScope(namespace={self._namespace!r}, appended={len(self._appended)})"


class TxBMixin:
    """A-group methods composed into :class:`~quintessence_pcs.store.control.ControlStore`."""

    # -------------------------------------------------------------- snapshots

    def capture_snapshot(self, namespace: str) -> Snapshot:
        """Persist one consistent set of watermarks and return its snapshot.

        The row is written in the same short transaction that reads the
        watermarks, so the stored values cannot be torn by a concurrent commit.
        Writing the snapshot row itself does not touch ``state_revision``
        (J.6/J.7: Context archival is not a domain commit).
        """

        self._require_standalone("capture_snapshot")
        ns = identifiers.require_namespace(namespace)
        context_id = str(uuid.uuid4())
        created_at = utc_now_rfc3339()
        with self.transaction():
            current = self._watermarks_locked(ns)
            self._require_connection().execute(
                "INSERT INTO context_snapshots ("
                "context_id, namespace, state_revision, context_event_seq, "
                "privacy_epoch, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    context_id,
                    ns,
                    current["state_revision"],
                    current["last_event_seq"],
                    current["privacy_epoch"],
                    created_at,
                ),
            )
        return Snapshot(
            context_id=context_id,
            namespace=ns,
            state_revision=current["state_revision"],
            context_event_seq=current["last_event_seq"],
            privacy_epoch=current["privacy_epoch"],
            created_at=created_at,
        )

    # --------------------------------------------------------------- commands

    def commit_command(
        self,
        *,
        context_id: str,
        request_id: str,
        operation: str,
        request_payload: Mapping[str, Any],
        apply: Callable[[TxScope], None],
        namespace: str | None = None,
    ) -> CommitReceipt:
        """Commit one command on top of a persisted snapshot.

        ``namespace`` is an optional assertion only: the authoritative
        namespace is the one stored on the snapshot, and a mismatch is
        ``SCOPE_VIOLATION``.  The digest is always computed here from
        ``(namespace, operation, context_id, canonical payload)``; callers
        cannot pass one in.
        """

        self._require_standalone("commit_command")
        context_id = identifiers.require_context_id(context_id)
        request_id = identifiers.require_request_id(request_id)
        operation = identifiers.require_operation(operation)
        expected_namespace = (
            None if namespace is None else identifiers.require_namespace(namespace)
        )
        if not isinstance(request_payload, Mapping):
            raise InvalidInput("request_payload must be a JSON object")
        payload = dict(request_payload)
        # Validate the payload before touching the database: a non-canonical
        # value must fail without starting a transaction.
        canonical_json_text(payload)
        if not callable(apply):
            raise InvalidInput("apply must be callable")

        with self.transaction():
            snapshot = self._load_snapshot_locked(context_id)
            if snapshot is None:
                raise SnapshotNotFound(f"no persisted snapshot {context_id!r}")
            if expected_namespace is not None and snapshot.namespace != expected_namespace:
                raise ScopeViolation(
                    f"snapshot {context_id} belongs to namespace "
                    f"{snapshot.namespace!r}, not {expected_namespace!r}"
                )
            ns = snapshot.namespace
            digest = _request_digest(ns, operation, context_id, payload)

            stored = self._load_command_locked(ns, request_id)
            if stored is not None:
                if stored["request_digest"] != digest:
                    raise CommandConflict(
                        f"request {request_id} already committed in namespace {ns!r} "
                        "with a different request digest"
                    )
                return _receipt_from_json(stored["receipt_json"])

            current = self._watermarks_locked(ns)
            # Fixed priority (handoff card A.6): privacy -> event seq ->
            # revision.  A deletion must not be masked by a domain commit.
            if snapshot.privacy_epoch != current["privacy_epoch"]:
                raise ContextStale(
                    f"privacy_epoch moved from {snapshot.privacy_epoch} to "
                    f"{current['privacy_epoch']}; the proposal basis is stale"
                )
            if snapshot.context_event_seq != current["last_event_seq"]:
                raise ContextStale(
                    f"last_event_seq moved from {snapshot.context_event_seq} to "
                    f"{current['last_event_seq']}; the proposal basis is stale"
                )
            if snapshot.state_revision != current["state_revision"]:
                raise StateRevisionConflict(
                    f"state_revision moved from {snapshot.state_revision} to "
                    f"{current['state_revision']}; the proposal basis is stale"
                )

            scope = TxScope(self, ns, context_id=context_id)
            apply(scope)
            if scope._write_failed:  # applied swallowed a failed store write
                raise CorruptStore(
                    "apply caught a failed store write; refusing to commit a "
                    "transaction with a reserved revision"
                )

            if scope.appended:
                revision = self._tx_revisions.get(ns)
                if revision is None:  # pragma: no cover - internal invariant
                    raise CorruptStore("an append succeeded without reserving a revision")
                outcome = OUTCOME_COMMITTED
            else:
                revision = current["state_revision"]
                outcome = OUTCOME_NOOP

            committed_at = utc_now_rfc3339()
            receipt = CommitReceipt(
                request_id=request_id,
                namespace=ns,
                operation=operation,
                context_id=context_id,
                outcome=outcome,
                state_revision=revision,
                record_refs=tuple(scope.appended),
                committed_at=committed_at,
            )
            try:
                self._require_connection().execute(
                    "INSERT INTO processed_commands ("
                    "namespace, request_id, request_digest, operation, "
                    "receipt_json, committed_at"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        ns,
                        request_id,
                        digest,
                        operation,
                        canonical_json_text(_receipt_to_json(receipt)),
                        committed_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed" in str(exc):
                    raise CommandConflict(
                        f"request {request_id} was committed concurrently"
                    ) from exc
                raise
            return receipt

    # ----------------------------------------------------------- internals

    def _load_snapshot_locked(self, context_id: str) -> Snapshot | None:
        row = self._require_connection().execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM context_snapshots WHERE context_id = ?",
            (context_id,),
        ).fetchone()
        if row is None:
            return None
        return Snapshot(
            context_id=row["context_id"],
            namespace=row["namespace"],
            state_revision=int(row["state_revision"]),
            context_event_seq=int(row["context_event_seq"]),
            privacy_epoch=int(row["privacy_epoch"]),
            created_at=row["created_at"],
        )

    def _load_command_locked(self, namespace: str, request_id: str) -> sqlite3.Row | None:
        return self._require_connection().execute(
            "SELECT request_digest, operation, receipt_json, committed_at "
            "FROM processed_commands WHERE namespace = ? AND request_id = ?",
            (namespace, request_id),
        ).fetchone()

    # ---------------------------------------------------------- receipt lookup

    def lookup_command_receipt(
        self, *, namespace: str, request_id: str
    ) -> CommitReceipt | None:
        """Return the stored receipt for one committed ``(namespace, request_id)``.

        This is a **trusted internal Store API**, not a public query.  It reads
        the receipt exactly as it was committed and returns ``None`` only when
        that key never committed.  It is a single ``SELECT``: it opens no
        transaction, takes no write lock, writes no row, and never consults the
        current watermarks, the deletion barrier or ``private_payloads``.

        What the result proves and what it does not:

        * it proves the command **once committed successfully** — that is the
          durable historical fact;
        * it does **not** prove the Context that produced it is still fresh, and
          it does **not** prove the records the receipt names are still readable
          (they may be superseded or under a deletion barrier);
        * a future public Facade must perform its own access and privacy
          filtering before showing a receipt to anyone; this method deliberately
          performs none.

        The receipt carries no body: only IDs, versions, watermarks and
        timestamps (see :class:`CommitReceipt`).

        A stored receipt that fails its defensive parse raises
        :class:`CorruptStore` and is never reported as "no such command"; a
        receipt whose namespace/request key or operation disagrees with its row
        is likewise corrupt.
        """

        ns = identifiers.require_namespace(namespace)
        rid = identifiers.require_request_id(request_id)
        row = self._load_command_locked(ns, rid)
        if row is None:
            return None
        receipt = _receipt_from_json(row["receipt_json"])
        if receipt.namespace != ns or receipt.request_id != rid:
            raise CorruptStore("stored command receipt does not match its key")
        if receipt.operation != row["operation"]:
            raise CorruptStore(
                "stored command receipt does not match its stored operation"
            )
        return receipt

    def _watermarks_locked(self, namespace: str) -> dict[str, int]:
        row = self._require_connection().execute(
            "SELECT state_revision, last_event_seq, privacy_epoch "
            "FROM namespace_state WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        if row is None:
            return {"state_revision": 0, "last_event_seq": 0, "privacy_epoch": 0}
        return {
            "state_revision": int(row["state_revision"]),
            "last_event_seq": int(row["last_event_seq"]),
            "privacy_epoch": int(row["privacy_epoch"]),
        }


def _request_digest(
    namespace: str, operation: str, context_id: str, payload: Mapping[str, Any]
) -> str:
    """Digest of the exact commit basis, computed at the store boundary.

    ``sha256(canonical({"namespace", "operation", "context_id",
    "request_payload"}))``.  Canonical JSON sorts object keys, so the result is
    stable across processes and Python versions.
    """

    material = {
        "context_id": context_id,
        "namespace": namespace,
        "operation": operation,
        "request_payload": payload,
    }
    return sha256_hex(canonical_json_text(material))


def _receipt_to_json(receipt: CommitReceipt) -> dict[str, Any]:
    return {
        "request_id": receipt.request_id,
        "namespace": receipt.namespace,
        "operation": receipt.operation,
        "context_id": receipt.context_id,
        "outcome": receipt.outcome,
        "state_revision": receipt.state_revision,
        "record_refs": [
            {"record_id": ref.record_id, "record_version": ref.record_version}
            for ref in receipt.record_refs
        ],
        "committed_at": receipt.committed_at,
    }


def _receipt_from_json(text: str) -> CommitReceipt:
    """Parse a stored receipt defensively; a corrupt one is never returned."""

    try:
        data = strict_json_loads(text)
    except ValueError as exc:
        raise CorruptStore("stored command receipt is not strict JSON") from exc
    if not isinstance(data, dict) or set(data) != _RECEIPT_KEYS:
        raise CorruptStore("stored command receipt has an unexpected shape")
    if not all(
        isinstance(data[key], str)
        for key in ("request_id", "namespace", "operation", "context_id", "committed_at")
    ):
        raise CorruptStore("stored command receipt has non-string fields")
    if data["outcome"] not in COMMIT_OUTCOMES:
        raise CorruptStore("stored command receipt has an unknown outcome")
    if type(data["state_revision"]) is not int or data["state_revision"] < 0:
        raise CorruptStore("stored command receipt has an invalid state_revision")
    refs_raw = data["record_refs"]
    if not isinstance(refs_raw, list):
        raise CorruptStore("stored command receipt has invalid record_refs")
    refs: list[CommittedRecordRef] = []
    for item in refs_raw:
        if (
            not isinstance(item, dict)
            or set(item) != {"record_id", "record_version"}
            or not isinstance(item["record_id"], str)
            or type(item["record_version"]) is not int
            or item["record_version"] < 1
        ):
            raise CorruptStore("stored command receipt has an invalid record reference")
        refs.append(
            CommittedRecordRef(
                record_id=item["record_id"], record_version=item["record_version"]
            )
        )
    return CommitReceipt(
        request_id=data["request_id"],
        namespace=data["namespace"],
        operation=data["operation"],
        context_id=data["context_id"],
        outcome=data["outcome"],
        state_revision=data["state_revision"],
        record_refs=tuple(refs),
        committed_at=data["committed_at"],
    )


__all__ = [
    "COMMIT_OUTCOMES",
    "CommitReceipt",
    "CommittedRecordRef",
    "OUTCOME_COMMITTED",
    "OUTCOME_NOOP",
    "Snapshot",
    "TxBMixin",
    "TxScope",
]
