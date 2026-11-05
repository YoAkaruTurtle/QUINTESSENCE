"""J08: bounded maintenance jobs, their checkpoint and their receipt recovery.

Three tables created by ``0016_maintenance_jobs.sql`` record *operational*
control for maintenance work: which bounded task exists, where its scan got to,
which items are confirmed, and which candidate/decision/receipt each item
refers to.  They are deliberately not a second authority: no Belief body, no
Question body and no source text is copied here, and nothing in this module
writes ``record_versions`` or the command receipts.

Two rules carry most of the weight and are enforced in code rather than by
convention:

* **scan position is not a checkpoint.**  ``scan_position`` is an opaque
  host cursor; ``success_checkpoint`` is an integer recomputed from the items —
  the highest ``item_no`` whose whole prefix is confirmed
  (``committed``/``no_change``).  It can therefore never leap over an unfinished
  item.
* **a job is only ``succeeded`` when nothing is outstanding.**  Every item must
  be in a terminal-success outcome and the checkpoint must cover the last item.
  Producing a candidate, a review decision or a Memory row is not a commit and
  cannot make a job succeed.

The module wraps an already-open store (the same shape ``GovernanceStore``
uses) instead of editing ``store/cognition.py``.
"""

from __future__ import annotations

import secrets
import time
import uuid
from datetime import datetime, timezone
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..foundation import identifiers
from ..foundation.errors import CorruptStore, InvalidInput, RecordNotFound
from ..foundation.jsonutil import canonical_json_text, sha256_hex, strict_json_loads

#: Closed maintenance-job state vocabulary.  ``waiting_*`` states are *not*
#: failures and never collapse into ``succeeded``.
MAINTENANCE_STATES = (
    "pending",
    "leased",
    "waiting_authorization",
    "waiting_human",
    "waiting_dependency",
    "waiting_budget",
    "paused",
    "cancelled",
    "failed",
    "obsolete",
    "succeeded",
)

#: States a worker may take a lease from (``paused`` needs an explicit resume).
LEASABLE_STATES = (
    "pending",
    "waiting_authorization",
    "waiting_human",
    "waiting_dependency",
    "waiting_budget",
)

#: Terminal states — nothing may move a job out of these.
TERMINAL_STATES = frozenset({"succeeded", "cancelled", "failed", "obsolete"})

#: Item outcomes that count as *confirmed* for checkpoint purposes.  A created
#: candidate, an awaiting-human review or a rejection is not a commit.
CONFIRMED_ITEM_OUTCOMES = frozenset({"committed", "no_change"})

#: Item outcomes that still block the checkpoint.
OUTSTANDING_ITEM_OUTCOMES = frozenset(
    {"pending", "awaiting_human", "rejected", "deferred", "failed",
     "waiting_dependency"}
)

_TARGET_KINDS = frozenset({"record", "event", "memory_delivery", "question"})

#: How a recorded review decision maps onto an item outcome.
_DECISION_TO_ITEM_OUTCOME = {
    "committed": "committed",
    "rejected": "rejected",
    "deferred": "deferred",
    "awaiting_human": "awaiting_human",
    "escalated": "awaiting_human",
}


_JOB_COLUMNS = (
    "task_id, namespace, task_type, trigger_key, params_digest, config_version, "
    "input_watermark, target_watermark, scan_position, success_checkpoint, state, "
    "attempt, lease_token, lease_until, next_retry_at, budget_units, units_spent, "
    "last_error, last_receipt_ref, created_at, updated_at"
)

_ITEM_COLUMNS = (
    "task_id, item_no, target_kind, target_id, target_version, operation_key, "
    "request_id, candidate_id, decision_id, receipt_ref, outcome, detail, "
    "created_at, updated_at"
)


@dataclass(frozen=True)
class MaintenanceJob:
    task_id: str
    namespace: str
    task_type: str
    trigger_key: str
    params_digest: str
    config_version: str
    input_watermark: str | None
    target_watermark: str | None
    scan_position: str | None
    success_checkpoint: int
    state: str
    attempt: int
    lease_token: str | None
    lease_until: str | None
    next_retry_at: str | None
    budget_units: int | None
    units_spent: int
    last_error: str | None
    last_receipt_ref: str | None
    created_at: str
    updated_at: str

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def leased(self) -> bool:
        return self.state == "leased"


@dataclass(frozen=True)
class MaintenanceItem:
    task_id: str
    item_no: int
    target_kind: str
    target_id: str
    target_version: int
    operation_key: str
    request_id: str | None
    candidate_id: str | None
    decision_id: str | None
    receipt_ref: str | None
    outcome: str
    detail: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MaintenanceAttempt:
    task_id: str
    attempt_no: int
    phase: str
    started_at: str
    finished_at: str | None
    outcome: str | None
    stop_reason: str | None
    units_spent: int | None
    receipt_ref: str | None
    detail: str | None


class RateLimited(InvalidInput):
    """A worker hit an external rate limit; the work is retryable, not failed.

    Raising this from a slice worker records a ``waiting_dependency`` attempt
    with a backoff timestamp instead of a terminal failure: a rate limit is a
    transient external condition, and calling it "failed" would retire work that
    is still valid.
    """

    def __init__(self, message: str = "rate limited", *, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class LocalTimeout(InvalidInput):
    """The local maintenance transport hit its own deadline.

    This is not an external rate limit: it records a retryable
    ``waiting_dependency`` with a ``local_timeout`` stop reason, so operators can
    tell a provider 429 from a client-side socket/wall cap.
    """


class TaskCancelled(InvalidInput):
    """The foreground asked this maintenance attempt to yield.

    This is a distinct local stop, not a timeout, rate limit or success.  The
    supervisor closes the attempt as ``cancelled`` and leaves the job ``paused``
    for an explicit bounded resume; no item and no checkpoint are written.
    """


class MaintenanceConflict(InvalidInput):
    """One trigger key was reused with a different parameter digest."""


def _job_from_row(row: Mapping[str, Any]) -> MaintenanceJob:
    return MaintenanceJob(
        task_id=row["task_id"],
        namespace=row["namespace"],
        task_type=row["task_type"],
        trigger_key=row["trigger_key"],
        params_digest=row["params_digest"],
        config_version=row["config_version"],
        input_watermark=row["input_watermark"],
        target_watermark=row["target_watermark"],
        scan_position=row["scan_position"],
        success_checkpoint=row["success_checkpoint"],
        state=row["state"],
        attempt=row["attempt"],
        lease_token=row["lease_token"],
        lease_until=row["lease_until"],
        next_retry_at=row["next_retry_at"],
        budget_units=row["budget_units"],
        units_spent=row["units_spent"],
        last_error=row["last_error"],
        last_receipt_ref=row["last_receipt_ref"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _item_from_row(row: Mapping[str, Any]) -> MaintenanceItem:
    return MaintenanceItem(
        task_id=row["task_id"],
        item_no=row["item_no"],
        target_kind=row["target_kind"],
        target_id=row["target_id"],
        target_version=row["target_version"],
        operation_key=row["operation_key"],
        request_id=row["request_id"],
        candidate_id=row["candidate_id"],
        decision_id=row["decision_id"],
        receipt_ref=row["receipt_ref"],
        outcome=row["outcome"],
        detail=row["detail"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _require_text(value: Any, field: str, *, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise InvalidInput(f"{field} must be at most {maximum} characters")
    return value


def _require_timestamp(value: Any, field: str) -> str:
    text = _require_text(value, field, maximum=40)
    if not text.endswith("Z"):
        raise InvalidInput(f"{field} must be an RFC 3339 UTC timestamp")
    return text


class MaintenanceStore:
    """Bounded maintenance task control around one already-open PCS store."""

    def __init__(
        self,
        store: Any,
        *,
        clock: Callable[[], str] | None = None,
        lease_seconds: int = 120,
        backoff_seconds: Sequence[int] = (30, 60, 120, 300),
    ) -> None:
        if not hasattr(store, "connection"):
            raise InvalidInput("store must be an open PCS store")
        if clock is not None and not callable(clock):
            raise InvalidInput("clock must be callable")
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise InvalidInput("lease_seconds must be a positive integer")
        backoff = tuple(backoff_seconds)
        if not backoff or any(type(item) is not int or item < 1 for item in backoff):
            raise InvalidInput("backoff_seconds must be positive integers")
        self._store = store
        self._clock = clock
        self.lease_seconds = lease_seconds
        self.backoff = backoff

    # ---------------------------------------------------------------- plumbing

    @property
    def connection(self):
        return self._store.connection

    def _now(self) -> str:
        if self._clock is not None:
            return self._clock()
        # The backoff arithmetic parses timestamps with second precision, so the
        # default clock must not emit microseconds: an uninjected store would
        # otherwise fail its own ``datetime.strptime`` on the first retry.
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _require_job_row(self, namespace: str, task_id: str):
        row = self.connection.execute(
            f"SELECT {_JOB_COLUMNS} FROM maintenance_jobs "
            "WHERE namespace = ? AND task_id = ?",
            (namespace, task_id),
        ).fetchone()
        if row is None:
            raise RecordNotFound(f"maintenance job {task_id} not found")
        return row

    # ------------------------------------------------------------ registration

    def _require_owner(
        self, namespace: str, task_id: str, lease_token: str | None
    ) -> "MaintenanceJob":
        """Refuse a write from an attempt that no longer owns the lease.

        A worker whose lease expired and was taken over must not close or move
        the *new* attempt's row: the expired one is dead, and its late write is
        indistinguishable from a concurrent worker racing the live one.
        """

        job = _job_from_row(self._require_job_row(namespace, task_id))
        if job.lease_token is not None:
            if not isinstance(lease_token, str) or lease_token != job.lease_token:
                raise MaintenanceConflict(
                    f"stale lease: an open attempt on {task_id} belongs to another worker"
                )
        return job

    def register(
        self,
        *,
        namespace: str,
        task_type: str,
        trigger_key: str,
        params: Mapping[str, Any] | None = None,
        config_version: str,
        input_watermark: str | None = None,
        target_watermark: str | None = None,
        budget_units: int | None = None,
        task_id: str | None = None,
    ) -> MaintenanceJob:
        """Register one logical trigger; the same key and params is idempotent.

        The same ``trigger_key`` with a *different* parameter digest is a
        conflict: a trigger names one opportunity, and silently rewriting its
        parameters would turn a retry into a new, unapproved opportunity.
        """

        ns = identifiers.require_namespace(namespace)
        kind = _require_text(task_type, "task_type", maximum=64)
        key = _require_text(trigger_key, "trigger_key", maximum=256)
        version = _require_text(config_version, "config_version")
        digest = sha256_hex(canonical_json_text(dict(params or {})))
        if budget_units is not None and (type(budget_units) is not int or budget_units < 0):
            raise InvalidInput("budget_units must be a non-negative integer")

        with self._store.transaction():
            existing = self.connection.execute(
                f"SELECT {_JOB_COLUMNS} FROM maintenance_jobs "
                "WHERE namespace = ? AND trigger_key = ?",
                (ns, key),
            ).fetchone()
            if existing is not None:
                if existing["params_digest"] != digest:
                    raise MaintenanceConflict(
                        f"trigger {key!r} already registered with different parameters"
                    )
                return _job_from_row(existing)
            now = self._now()
            new_id = task_id or str(uuid.uuid4())
            if len(new_id) != 36:
                raise InvalidInput("task_id must be a 36-character id")
            self.connection.execute(
                "INSERT INTO maintenance_jobs ("
                "task_id, namespace, task_type, trigger_key, params_digest, "
                "config_version, input_watermark, target_watermark, scan_position, "
                "success_checkpoint, state, attempt, lease_token, lease_until, "
                "next_retry_at, budget_units, units_spent, last_error, "
                "last_receipt_ref, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 'pending', 0, "
                "NULL, NULL, NULL, ?, 0, NULL, NULL, ?, ?)",
                (
                    new_id, ns, kind, key, digest, version,
                    input_watermark, target_watermark, budget_units, now, now,
                ),
            )
            row = self._require_job_row(ns, new_id)
            return _job_from_row(row)

    def get(self, namespace: str, task_id: str) -> MaintenanceJob:
        ns = identifiers.require_namespace(namespace)
        return _job_from_row(self._require_job_row(ns, task_id))

    def list_jobs(
        self,
        namespace: str,
        *,
        states: Sequence[str] | None = None,
        limit: int = 50,
    ) -> tuple[MaintenanceJob, ...]:
        ns = identifiers.require_namespace(namespace)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidInput("limit must be an integer between 1 and 200")
        query = f"SELECT {_JOB_COLUMNS} FROM maintenance_jobs WHERE namespace = ?"
        args: list[Any] = [ns]
        if states is not None:
            chosen = tuple(states)
            for state in chosen:
                if state not in MAINTENANCE_STATES:
                    raise InvalidInput(f"unknown maintenance state {state!r}")
            query += " AND state IN (" + ", ".join("?" for _ in chosen) + ")"
            args.extend(chosen)
        query += " ORDER BY created_at, task_id LIMIT ?"
        args.append(limit)
        rows = self.connection.execute(query, tuple(args)).fetchall()
        return tuple(_job_from_row(row) for row in rows)

    # ----------------------------------------------------------------- leasing

    def lease(
        self,
        namespace: str,
        task_id: str,
        *,
        phase: str = "run",
        now: str | None = None,
    ) -> tuple[MaintenanceJob, MaintenanceAttempt]:
        """Take the lease for one bounded execution attempt.

        An expired lease is recoverable: the row still in ``leased`` whose
        ``lease_until`` has passed may be taken over, which is exactly the
        crashed-worker case.  A live lease is not.
        """

        ns = identifiers.require_namespace(namespace)
        at = _require_timestamp(now, "now") if now is not None else self._now()
        with self._store.transaction():
            row = self._require_job_row(ns, task_id)
            job = _job_from_row(row)
            if job.terminal:
                raise InvalidInput(f"maintenance job is {job.state}")
            if job.state == "leased":
                if job.lease_until is not None and job.lease_until > at:
                    raise InvalidInput("maintenance job is leased by another attempt")
            elif job.state not in LEASABLE_STATES:
                raise InvalidInput(f"cannot lease a job in state {job.state}")
            attempt_no = job.attempt + 1
            token = str(uuid.uuid4())
            lease_until = self._offset(at, self.lease_seconds)
            self.connection.execute(
                "UPDATE maintenance_jobs SET state = 'leased', attempt = ?, "
                "lease_token = ?, lease_until = ?, updated_at = ? WHERE task_id = ?",
                (attempt_no, token, lease_until, at, task_id),
            )
            self.connection.execute(
                "INSERT INTO maintenance_attempts ("
                "task_id, attempt_no, phase, started_at, finished_at, outcome, "
                "stop_reason, units_spent, receipt_ref, detail"
                ") VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL)",
                (task_id, attempt_no, _require_text(phase, "phase", maximum=64), at),
            )
            updated = _job_from_row(self._require_job_row(ns, task_id))
            attempt_row = self.connection.execute(
                "SELECT * FROM maintenance_attempts WHERE task_id = ? AND attempt_no = ?",
                (task_id, attempt_no),
            ).fetchone()
            return updated, MaintenanceAttempt(
                task_id=attempt_row["task_id"],
                attempt_no=attempt_row["attempt_no"],
                phase=attempt_row["phase"],
                started_at=attempt_row["started_at"],
                finished_at=attempt_row["finished_at"],
                outcome=attempt_row["outcome"],
                stop_reason=attempt_row["stop_reason"],
                units_spent=attempt_row["units_spent"],
                receipt_ref=attempt_row["receipt_ref"],
                detail=attempt_row["detail"],
            )

    def finish_attempt(
        self,
        namespace: str,
        task_id: str,
        *,
        outcome: str,
        next_state: str,
        stop_reason: str | None = None,
        units_spent: int | None = None,
        receipt_ref: str | None = None,
        detail: str | None = None,
        lease_token: str | None = None,
        now: str | None = None,
    ) -> MaintenanceJob:
        """Close the open attempt and move the job.

        ``next_state='succeeded'`` is refused unless every item is confirmed
        and the checkpoint covers the last item: a review decision, a candidate
        or a Memory row is not a commit.
        """

        ns = identifiers.require_namespace(namespace)
        if outcome not in ("succeeded", "partial", "failed", "paused", "waiting", "cancelled"):
            raise InvalidInput(f"unknown attempt outcome {outcome!r}")
        if next_state not in MAINTENANCE_STATES:
            raise InvalidInput(f"unknown maintenance state {next_state!r}")
        at = _require_timestamp(now, "now") if now is not None else self._now()
        if units_spent is not None and (type(units_spent) is not int or units_spent < 0):
            raise InvalidInput("units_spent must be a non-negative integer")

        with self._store.transaction():
            job = self._require_owner(ns, task_id, lease_token)
            if job.state != "leased":
                raise InvalidInput("no attempt is open on this job")
            if next_state == "succeeded":
                self._require_nothing_outstanding(task_id)
            checkpoint = self._contiguous_checkpoint(task_id)
            retry_at = None
            if next_state in ("failed", "waiting_dependency"):
                # A retryable wait (a rate limit, a missing dependency) still
                # needs a backoff stamp; otherwise a retry loop spins as fast as
                # the caller can ask.
                retry_at = self._retry_timestamp(at, job.attempt)
            self.connection.execute(
                "UPDATE maintenance_attempts SET finished_at = ?, outcome = ?, "
                "stop_reason = ?, units_spent = ?, receipt_ref = ?, detail = ? "
                "WHERE task_id = ? AND attempt_no = ?",
                (at, outcome, stop_reason, units_spent, receipt_ref, detail,
                 task_id, job.attempt),
            )
            self.connection.execute(
                "UPDATE maintenance_jobs SET state = ?, lease_token = NULL, "
                "lease_until = NULL, next_retry_at = ?, success_checkpoint = ?, "
                "units_spent = units_spent + ?, last_error = ?, "
                "last_receipt_ref = COALESCE(?, last_receipt_ref), updated_at = ? "
                "WHERE task_id = ?",
                (
                    next_state, retry_at, checkpoint,
                    units_spent or 0,
                    stop_reason if next_state == "failed" else None,
                    receipt_ref, at, task_id,
                ),
            )
            return _job_from_row(self._require_job_row(ns, task_id))

    @staticmethod
    def _offset(at: str, seconds: int) -> str:
        from datetime import datetime, timedelta, timezone

        moment = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (moment + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _retry_timestamp(self, at: str, attempt: int) -> str:
        delay = self.backoff[min(max(attempt - 1, 0), len(self.backoff) - 1)]
        return self._offset(at, delay)

    # --------------------------------------------------------------- progress

    def set_scan_position(
        self,
        namespace: str,
        task_id: str,
        position: Mapping[str, Any] | None,
        *,
        lease_token: str | None = None,
        now: str | None = None,
    ) -> MaintenanceJob:
        """Record where the *scan* reached.  This is not a checkpoint."""

        ns = identifiers.require_namespace(namespace)
        at = _require_timestamp(now, "now") if now is not None else self._now()
        text = None if position is None else canonical_json_text(dict(position))
        if text is not None and len(text) > 65536:
            raise InvalidInput("scan position is too large")
        with self._store.transaction():
            self._require_owner(ns, task_id, lease_token)
            self.connection.execute(
                "UPDATE maintenance_jobs SET scan_position = ?, updated_at = ? "
                "WHERE task_id = ?",
                (text, at, task_id),
            )
            return _job_from_row(self._require_job_row(ns, task_id))

    def record_item(
        self,
        namespace: str,
        task_id: str,
        *,
        target_kind: str,
        target_id: str,
        operation_key: str,
        target_version: int = 0,
        request_id: str | None = None,
        candidate_id: str | None = None,
        decision_id: str | None = None,
        receipt_ref: str | None = None,
        outcome: str = "pending",
        detail: str | None = None,
        lease_token: str | None = None,
        now: str | None = None,
    ) -> MaintenanceItem:
        """Record one item; a repeated ``operation_key`` updates it in place."""

        ns = identifiers.require_namespace(namespace)
        if target_kind not in _TARGET_KINDS:
            raise InvalidInput(f"unknown target_kind {target_kind!r}")
        target = _require_text(target_id, "target_id")
        key = _require_text(operation_key, "operation_key", maximum=256)
        if outcome not in CONFIRMED_ITEM_OUTCOMES | OUTSTANDING_ITEM_OUTCOMES:
            raise InvalidInput(f"unknown item outcome {outcome!r}")
        at = _require_timestamp(now, "now") if now is not None else self._now()

        with self._store.transaction():
            self._require_owner(ns, task_id, lease_token)
            existing = self.connection.execute(
                f"SELECT {_ITEM_COLUMNS} FROM maintenance_items "
                "WHERE task_id = ? AND operation_key = ?",
                (task_id, key),
            ).fetchone()
            if existing is not None:
                self.connection.execute(
                    "UPDATE maintenance_items SET outcome = ?, request_id = ?, "
                    "candidate_id = ?, decision_id = ?, receipt_ref = ?, "
                    "detail = ?, updated_at = ? WHERE task_id = ? AND operation_key = ?",
                    (outcome, request_id, candidate_id, decision_id, receipt_ref,
                     detail, at, task_id, key),
                )
                row = self.connection.execute(
                    f"SELECT {_ITEM_COLUMNS} FROM maintenance_items "
                    "WHERE task_id = ? AND operation_key = ?",
                    (task_id, key),
                ).fetchone()
                return _item_from_row(row)
            next_no = self.connection.execute(
                "SELECT COALESCE(MAX(item_no), 0) + 1 FROM maintenance_items "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            self.connection.execute(
                "INSERT INTO maintenance_items ("
                "task_id, item_no, target_kind, target_id, target_version, "
                "operation_key, request_id, candidate_id, decision_id, receipt_ref, "
                "outcome, detail, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, next_no, target_kind, target, target_version, key,
                 request_id, candidate_id, decision_id, receipt_ref, outcome,
                 detail, at, at),
            )
            row = self.connection.execute(
                f"SELECT {_ITEM_COLUMNS} FROM maintenance_items "
                "WHERE task_id = ? AND operation_key = ?",
                (task_id, key),
            ).fetchone()
            return _item_from_row(row)

    def items(self, namespace: str, task_id: str) -> tuple[MaintenanceItem, ...]:
        ns = identifiers.require_namespace(namespace)
        self._require_job_row(ns, task_id)
        rows = self.connection.execute(
            f"SELECT {_ITEM_COLUMNS} FROM maintenance_items "
            "WHERE task_id = ? ORDER BY item_no",
            (task_id,),
        ).fetchall()
        return tuple(_item_from_row(row) for row in rows)

    def outstanding_item_count(self, namespace: str, task_id: str) -> int:
        """How many recorded items are not confirmed yet.

        ``run_slice`` asks this before claiming terminal success.  The store
        already refuses ``succeeded`` while work is outstanding; without the
        pre-check, a worker whose page legitimately carries a deferred item
        (``applied`` without an external ack is the normal case) crashes the
        slice instead of pausing it.
        """

        ns = identifiers.require_namespace(namespace)
        self._require_job_row(ns, task_id)
        rows = self.connection.execute(
            "SELECT outcome FROM maintenance_items WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        return sum(
            1 for row in rows if row["outcome"] not in CONFIRMED_ITEM_OUTCOMES
        )

    def _contiguous_checkpoint(self, task_id: str) -> int:
        rows = self.connection.execute(
            "SELECT item_no, outcome FROM maintenance_items "
            "WHERE task_id = ? ORDER BY item_no",
            (task_id,),
        ).fetchall()
        checkpoint = 0
        for row in rows:
            if row["outcome"] not in CONFIRMED_ITEM_OUTCOMES:
                break
            checkpoint = row["item_no"]
        return checkpoint

    def _require_nothing_outstanding(self, task_id: str) -> None:
        rows = self.connection.execute(
            "SELECT item_no, outcome FROM maintenance_items "
            "WHERE task_id = ? ORDER BY item_no",
            (task_id,),
        ).fetchall()
        if not rows:
            raise InvalidInput(
                "a maintenance job cannot succeed before it has recorded any item"
            )
        for row in rows:
            if row["outcome"] not in CONFIRMED_ITEM_OUTCOMES:
                raise InvalidInput(
                    f"item {row['item_no']} is {row['outcome']}; a job cannot "
                    "succeed while work is outstanding"
                )

    def confirm_progress(
        self,
        namespace: str,
        task_id: str,
        *,
        lease_token: str | None = None,
        now: str | None = None,
    ) -> MaintenanceJob:
        """Recompute the contiguous checkpoint from the items."""

        ns = identifiers.require_namespace(namespace)
        at = _require_timestamp(now, "now") if now is not None else self._now()
        with self._store.transaction():
            self._require_owner(ns, task_id, lease_token)
            checkpoint = self._contiguous_checkpoint(task_id)
            self.connection.execute(
                "UPDATE maintenance_jobs SET success_checkpoint = ?, updated_at = ? "
                "WHERE task_id = ? AND success_checkpoint < ?",
                (checkpoint, at, task_id, checkpoint),
            )
            return _job_from_row(self._require_job_row(ns, task_id))

    # -------------------------------------------------------------- lifecycle

    def set_state(
        self,
        namespace: str,
        task_id: str,
        state: str,
        *,
        reason: str | None = None,
        now: str | None = None,
    ) -> MaintenanceJob:
        """Move a job between non-lease states (pause/resume/cancel/obsolete)."""

        ns = identifiers.require_namespace(namespace)
        if state not in MAINTENANCE_STATES or state == "leased":
            raise InvalidInput(f"cannot set state {state!r} directly")
        at = _require_timestamp(now, "now") if now is not None else self._now()
        with self._store.transaction():
            job = _job_from_row(self._require_job_row(ns, task_id))
            if job.terminal:
                raise InvalidInput(f"maintenance job is already {job.state}")
            if job.state == "leased":
                raise InvalidInput("finish the open attempt before changing state")
            if state in ("succeeded",):
                self._require_nothing_outstanding(task_id)
            self.connection.execute(
                "UPDATE maintenance_jobs SET state = ?, next_retry_at = NULL, "
                "last_error = ?, updated_at = ? WHERE task_id = ?",
                (state, reason, at, task_id),
            )
            return _job_from_row(self._require_job_row(ns, task_id))

    def resume(
        self, namespace: str, task_id: str, *, now: str | None = None
    ) -> MaintenanceJob:
        """Move a paused job back to ``pending`` so a worker may lease it."""

        ns = identifiers.require_namespace(namespace)
        at = _require_timestamp(now, "now") if now is not None else self._now()
        with self._store.transaction():
            job = _job_from_row(self._require_job_row(ns, task_id))
            if job.state != "paused":
                raise InvalidInput("only a paused job can be resumed")
            self.connection.execute(
                "UPDATE maintenance_jobs SET state = 'pending', next_retry_at = NULL, "
                "updated_at = ? WHERE task_id = ?",
                (at, task_id),
            )
            return _job_from_row(self._require_job_row(ns, task_id))

    def attempts(self, namespace: str, task_id: str) -> tuple[MaintenanceAttempt, ...]:
        ns = identifiers.require_namespace(namespace)
        self._require_job_row(ns, task_id)
        rows = self.connection.execute(
            "SELECT * FROM maintenance_attempts WHERE task_id = ? ORDER BY attempt_no",
            (task_id,),
        ).fetchall()
        return tuple(
            MaintenanceAttempt(
                task_id=row["task_id"],
                attempt_no=row["attempt_no"],
                phase=row["phase"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                outcome=row["outcome"],
                stop_reason=row["stop_reason"],
                units_spent=row["units_spent"],
                receipt_ref=row["receipt_ref"],
                detail=row["detail"],
            )
            for row in rows
        )

    # ------------------------------------------------------------ observability

    def health(self, namespace: str, *, now: str | None = None) -> Mapping[str, Any]:
        """Bounded, permission-scoped counters for the host (never model DTOs)."""

        ns = identifiers.require_namespace(namespace)
        at = _require_timestamp(now, "now") if now is not None else self._now()
        rows = self.connection.execute(
            "SELECT state, COUNT(*) AS n, MIN(created_at) AS oldest "
            "FROM maintenance_jobs WHERE namespace = ? GROUP BY state",
            (ns,),
        ).fetchall()
        by_state = {row["state"]: row["n"] for row in rows}
        oldest = min((row["oldest"] for row in rows if row["oldest"]), default=None)
        retrying = self.connection.execute(
            "SELECT COUNT(*) FROM maintenance_jobs "
            "WHERE namespace = ? AND next_retry_at IS NOT NULL AND next_retry_at <= ?",
            (ns, at),
        ).fetchone()[0]
        return {
            "by_state": by_state,
            "pending": by_state.get("pending", 0),
            "leased": by_state.get("leased", 0),
            "retry_due": retrying,
            "oldest_created_at": oldest,
            "now": at,
        }


@dataclass(frozen=True)
class OperationRecovery:
    """What a crashed maintenance attempt can prove about one operation.

    ``receipt`` means the command really committed; ``candidate_id`` means only
    that a candidate was registered (its review is not a commit); ``absent``
    means no side effect is visible, so the attempt may be retried with the
    *same* request id.
    """

    state: str
    receipt: Any | None
    candidate_id: str | None
    candidate_version: int | None
    decision: Any | None = None


def reconcile_operation(
    store: Any,
    maintenance: "MaintenanceStore",
    *,
    namespace: str,
    task_id: str,
    operation_key: str,
    request_id: str,
    target_kind: str,
    target_id: str,
    target_version: int = 0,
    lease_token: str | None = None,
    decision_source: Any | None = None,
) -> OperationRecovery:
    """Recover one operation from authoritative receipts instead of re-running it.

    The order matters: the committed receipt wins over the candidate, because a
    candidate that later committed must not be reported as still pending.  A
    caller that finds ``candidate_id`` must resume that candidate's review, not
    propose a second one under a new request id.
    """

    ns = identifiers.require_namespace(namespace)
    if not isinstance(request_id, str) or len(request_id) != 36:
        raise InvalidInput("request_id must be a 36-character id")

    receipt = store.lookup_command_receipt(namespace=ns, request_id=request_id)
    if receipt is not None:
        maintenance.record_item(
            ns,
            task_id,
            target_kind=target_kind,
            target_id=target_id,
            target_version=target_version,
            operation_key=operation_key,
            request_id=request_id,
            receipt_ref=f"receipt:{request_id}",
            lease_token=lease_token,
            outcome="committed",
            detail=f"recovered committed receipt (revision {receipt.state_revision})",
        )
        return OperationRecovery(
            state="committed", receipt=receipt, candidate_id=None, candidate_version=None
        )

    candidate = store.candidate_by_request(namespace=ns, request_id=request_id)
    if candidate is not None:
        candidate_id = candidate["artifact_id"]
        candidate_version = candidate["artifact_version"]
        source = decision_source if decision_source is not None else getattr(
            store, "governance", None
        )
        lookup = getattr(source, "latest_decision", None)
        decision = (
            lookup(ns, candidate_id, candidate_version) if lookup is not None else None
        )
        if decision is not None:
            # The review already answered this round; the caller only lost the
            # reply.  Re-reading the decision is authoritative: a new proposal
            # under a fresh request id would silently re-open a settled round.
            decided = _DECISION_TO_ITEM_OUTCOME.get(decision.decision, "awaiting_human")
            maintenance.record_item(
                ns,
                task_id,
                target_kind=target_kind,
                target_id=target_id,
                target_version=target_version,
                operation_key=operation_key,
                request_id=request_id,
                candidate_id=candidate_id,
                decision_id=decision.decision_id,
                lease_token=lease_token,
                outcome=decided,
                detail=(
                    f"review decision {decision.decision!r} already recorded "
                    f"(impact {decision.impact}); do not re-propose"
                ),
            )
            return OperationRecovery(
                state="decision_recorded",
                receipt=None,
                candidate_id=candidate_id,
                candidate_version=candidate_version,
                decision=decision,
            )
        maintenance.record_item(
            ns,
            task_id,
            target_kind=target_kind,
            target_id=target_id,
            target_version=target_version,
            operation_key=operation_key,
            request_id=request_id,
            candidate_id=candidate_id,
            lease_token=lease_token,
            outcome="awaiting_human",
            detail="candidate registered but not committed; resume its review",
        )
        return OperationRecovery(
            state="candidate_pending",
            receipt=None,
            candidate_id=candidate_id,
            candidate_version=candidate_version,
        )

    maintenance.record_item(
        ns,
        task_id,
        target_kind=target_kind,
        target_id=target_id,
        target_version=target_version,
        operation_key=operation_key,
        request_id=request_id,
        lease_token=lease_token,
        outcome="pending",
        detail="no receipt and no candidate: the attempt may retry the same request id",
    )
    return OperationRecovery(
        state="absent", receipt=None, candidate_id=None, candidate_version=None
    )


@dataclass(frozen=True)
class SliceLimits:
    """Caps for one bounded execution slice, and for the whole task.

    The per-slice caps stop one run from scanning or processing without bound;
    the task caps stop ``resume`` from laundering an exhausted budget into a
    fresh one — a resumed task keeps its attempt and unit totals.
    """

    max_scan: int = 200
    max_process: int = 20
    max_pages: int = 10
    max_attempts: int = 3
    max_slices: int = 10
    max_total_units: int = 2000
    max_process_total: int = 200
    max_seconds: float = 30.0


@dataclass(frozen=True)
class SliceReport:
    ok: bool
    state: str
    stop_reason: str
    scanned: int
    processed: int
    units: int
    next_scan_position: Mapping[str, Any] | None
    job: "MaintenanceJob"
    deadline_exceeded: bool = False
    remaining: Mapping[str, Any] | None = None


def run_slice(
    maintenance: "MaintenanceStore",
    *,
    namespace: str,
    task_id: str,
    worker: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    limits: SliceLimits = SliceLimits(),
    now: str | None = None,
    clock: Callable[[], float] | None = None,
    guard: Callable[[Sequence[Mapping[str, Any]]], None] | None = None,
) -> SliceReport:
    """Run exactly one bounded slice through a caller-supplied worker.

    ``worker`` receives the slice budget -- including the wall-clock deadline it
    must respect -- and returns counters plus the items it settled; it must not
    perform an unbounded scan of its own.  The supervisor enforces the caps,
    records the items, advances the scan position, recomputes the contiguous
    checkpoint and closes the attempt with a truthful outcome.

    A slice that runs past ``limits.max_seconds`` is reported as
    ``slice_deadline_exceeded`` and stays ``partial``: the deadline is a stop
    condition, not a success.  Python cannot pre-empt a blocking call, so the
    deadline is enforced at the boundaries the supervisor controls (before the
    worker starts, after each recorded item, and when the attempt closes); the
    worker is told the deadline and must bound its own I/O.

    ``guard`` is the AC-21 boundary recheck and is **required in practice**: it
    receives every item target just before the supervisor records the batch, and
    a target that was revoked, deleted or rewritten in the meantime stops the
    slice as ``stale_target`` instead of being written down as success.  A slice
    started without a callable guard does **not** fall back to the old
    unchecked behaviour: it pauses the job with ``guard_missing`` /
    ``guard_unavailable`` and raises before taking a lease, so no item, no
    checkpoint and no commit is touched.  Callers pass
    ``domains.cognition.maintenance_guard.require_live_targets``.
    """
    maintenance._store.require_ready("maintenance_slice")

    ns = identifiers.require_namespace(namespace)
    tick = clock if clock is not None else time.monotonic
    job = maintenance.get(ns, task_id)
    if guard is None or not callable(guard):
        # AC-21 enforcement: a slice that cannot re-check its targets must not
        # run at all.  Refusing here touches no item, no checkpoint and no
        # commit path -- the job only records why it stopped.
        reason = "guard_missing" if guard is None else "guard_unavailable"
        try:
            maintenance.set_state(ns, task_id, "paused", reason=reason, now=now)
        except InvalidInput:
            # Already leased by a dead worker, or terminal: keep the record and
            # still refuse to execute.
            pass
        raise InvalidInput(
            f"{reason}: a maintenance slice must be given a live-target guard"
        )
    if job.attempt >= limits.max_slices:
        raise InvalidInput(
            f"slice limit reached ({limits.max_slices}); the task needs a new trigger"
        )
    if job.units_spent >= limits.max_total_units:
        raise InvalidInput("task unit budget is exhausted; retries cannot reset it")

    started = tick()
    dead_at = started + max(float(limits.max_seconds), 0.0)
    leased, _attempt = maintenance.lease(ns, task_id, now=now)
    token = leased.lease_token
    budget = {
        "max_scan": limits.max_scan,
        "max_process": limits.max_process,
        "max_pages": limits.max_pages,
        "max_seconds": limits.max_seconds,
        "deadline_seconds": max(float(limits.max_seconds), 0.0),
        "remaining_units": max(limits.max_total_units - leased.units_spent, 0),
        "attempt": leased.attempt,
        "scan_position": leased.scan_position,
        # RG08 R-E: a worker that recovers an operation from authoritative
        # receipts must present the live lease token to record/update its item.
        # Existing workers ignore the extra key.
        "lease_token": token,
    }
    try:
        result = dict(worker(budget))
    except TaskCancelled:
        # Foreground yielded this attempt.  Leave the job non-terminal and not
        # auto-leasable: ``paused`` needs an explicit/bounded resume, and no
        # item, scan position or success checkpoint is advanced.
        final = maintenance.finish_attempt(
            ns,
            task_id,
            outcome="cancelled",
            next_state="paused",
            stop_reason="foreground_cancel",
            detail=canonical_json_text({"reason": "foreground_cancel"}),
            lease_token=token,
            now=now,
        )
        return SliceReport(
            ok=True,
            state=final.state,
            stop_reason="foreground_cancel",
            scanned=0,
            processed=0,
            units=0,
            next_scan_position=job.scan_position,
            job=final,
            remaining={"foreground_cancel": True},
        )
    except (RateLimited, LocalTimeout) as error:
        # ``RateLimited`` is provider 429/5xx; ``LocalTimeout`` is our own
        # timeout.  Both are retryable waits, but the audit reason stays exact.
        if isinstance(error, LocalTimeout):
            reason = "local_timeout"
            retry_after = None
        else:
            reason = "rate_limited"
            retry_after = error.retry_after
        final = maintenance.finish_attempt(
            ns, task_id, outcome="waiting", next_state="waiting_dependency",
            stop_reason=reason,
            detail=canonical_json_text(
                {"reason": reason, "retry_after": retry_after}
            ),
            lease_token=token, now=now,
        )
        return SliceReport(
            ok=True,
            state=final.state,
            stop_reason=reason,
            scanned=0,
            processed=0,
            units=0,
            next_scan_position=job.scan_position,
            job=final,
            remaining={reason: True, "retry_after": retry_after},
        )
    except BaseException:
        maintenance.finish_attempt(
            ns, task_id, outcome="failed", next_state="failed",
            stop_reason="worker_error", lease_token=token, now=now,
        )
        raise

    scanned = int(result.get("scanned", 0))
    processed = int(result.get("processed", 0))
    units = int(result.get("units", 0))
    if scanned > limits.max_scan or processed > limits.max_process:
        maintenance.finish_attempt(
            ns, task_id, outcome="failed", next_state="failed",
            stop_reason="slice_cap_exceeded", units_spent=max(units, 0),
            lease_token=token, now=now,
        )
        raise InvalidInput("worker exceeded the slice caps")

    deadline_exceeded = tick() > dead_at
    settled = 0
    # AC-21: the opening authorization is not a permission for the whole run.
    # The supervisor re-checks every target before it records a settled item, so
    # a source revoked or deleted mid-slice cannot be written down as success.
    guard_failure: str | None = None
    pending_items = list(result.get("items", ()))
    if guard is not None and pending_items:
        targets = [
            {
                "kind": "event" if item.get("target_kind") == "event" else item.get(
                    "target_kind", "record"
                ),
                "id": item["target_id"],
                "version": item.get("target_version"),
            }
            for item in pending_items
        ]
        try:
            guard(targets)
        except InvalidInput as error:
            guard_failure = str(error)
        except Exception as error:  # noqa: BLE001 - a broken guard is not a pass
            # An unexpected guard failure must stop the slice, not be read as
            # "the targets are fine": the whole point is that silence is not
            # consent.
            guard_failure = f"guard_unavailable:{error}"
    for index, item in enumerate(result.get("items", ())):
        if guard_failure is not None and guard_failure.startswith("guard_unavailable:"):
            # The guard itself is broken, so it said nothing about these
            # targets.  Writing them down -- even as ``failed`` -- would put a
            # finding in the record that nobody actually made.
            break
        if tick() > dead_at:
            deadline_exceeded = True
            break
        outcome = item.get("outcome", "pending")
        detail = item.get("detail")
        if guard_failure is not None:
            if item.get("receipt_ref"):
                # The authoritative receipt proves the commit happened; the
                # target going stale afterwards must not rewrite that fact.
                detail = f"stale_after_commit:{guard_failure}"
            else:
                outcome = "failed"
                detail = f"stale:{guard_failure}"
        maintenance.record_item(
            ns,
            task_id,
            target_kind=item["target_kind"],
            target_id=item["target_id"],
            operation_key=item.get("operation_key", f"auto-{leased.attempt}-{index}"),
            target_version=item.get("target_version", 0),
            request_id=item.get("request_id"),
            candidate_id=item.get("candidate_id"),
            decision_id=item.get("decision_id"),
            receipt_ref=item.get("receipt_ref"),
            outcome=outcome,
            detail=detail,
            lease_token=token,
            now=now,
        )
        settled += 1

    position = result.get("next_scan_position")
    if position is not None and not deadline_exceeded:
        maintenance.set_scan_position(ns, task_id, position, lease_token=token, now=now)
    if not deadline_exceeded:
        maintenance.confirm_progress(ns, task_id, lease_token=token, now=now)

    remaining = dict(result.get("remaining") or {})
    unrecorded = max(len(result.get("items", ())) - settled, 0)
    if unrecorded:
        remaining.setdefault("items_not_recorded", unrecorded)

    complete = bool(result.get("complete", False)) and not deadline_exceeded
    if guard_failure is not None:
        # A stale target is never a completed run, even when the worker said so.
        complete = False
        remaining.setdefault("stale_target", guard_failure)
    if complete and settled == 0:
        # Nothing was settled, so the store refuses ``succeeded`` (correctly:
        # an empty terminal state is not evidence of work).  Stop as paused and
        # say why, rather than reporting a success the record cannot support.
        complete = False
        result.setdefault("stop_reason", "slice_empty")
    if complete:
        # ``complete`` from the worker means "I walked my whole page", not "the
        # job is finished".  A page whose items are only ``applied`` (no
        # external ack) leaves work outstanding, and the store refuses
        # ``succeeded`` for it -- so pause here instead of asking and raising.
        outstanding = maintenance.outstanding_item_count(ns, task_id)
        if outstanding:
            complete = False
            remaining.setdefault("outstanding_items", outstanding)
            # Keep what the worker said, but name why the slice cannot close.
            if result.get("stop_reason"):
                remaining.setdefault("worker_stop_reason", result["stop_reason"])
            result["stop_reason"] = "awaits_confirmation"
    if guard_failure is not None:
        # A guard that itself blew up is not the same finding as a target that
        # went stale: name both, so an operator can tell a code fault from a
        # revoked source.
        stop_reason = (
            "guard_unavailable"
            if guard_failure.startswith("guard_unavailable:")
            else "stale_target"
        )
    elif deadline_exceeded:
        stop_reason = "slice_deadline_exceeded"
    else:
        stop_reason = result.get("stop_reason") or ("complete" if complete else "slice_end")
    # A finished fixed range is terminal success; anything else stays paused so
    # a later trigger -- never a silent retry -- decides whether to continue.
    next_state = "succeeded" if complete else "paused"
    outcome = "succeeded" if complete else "partial"
    final = maintenance.finish_attempt(
        ns, task_id, outcome=outcome, next_state=next_state,
        stop_reason=stop_reason, units_spent=max(units, 0),
        detail=None if not remaining else canonical_json_text(remaining),
        lease_token=token, now=now,
    )
    return SliceReport(
        ok=True,
        state=final.state,
        stop_reason=stop_reason,
        scanned=scanned,
        processed=processed,
        units=units,
        next_scan_position=position,
        job=final,
        deadline_exceeded=deadline_exceeded,
        remaining=remaining or None,
    )


__all__ = [
    "CONFIRMED_ITEM_OUTCOMES",
    "LEASABLE_STATES",
    "MAINTENANCE_STATES",
    "MaintenanceAttempt",
    "MaintenanceConflict",
    "MaintenanceItem",
    "MaintenanceJob",
    "MaintenanceStore",
    "OUTSTANDING_ITEM_OUTCOMES",
    "OperationRecovery",
    "LocalTimeout",
    "RateLimited",
    "TERMINAL_STATES",
    "SliceLimits",
    "SliceReport",
    "TaskCancelled",
    "reconcile_operation",
    "run_slice",
]
