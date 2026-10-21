"""PCS2-009 model authorization and per-attempt accounting.

The runtime plane has two different questions and this module keeps them apart:

* **May this namespace call this provider/model?**  ``model_authorizations``.
  An authorization is an explicit, human/API-written grant bound to
  ``provider + model + manifest_hash``.  A manifest only describes how to
  connect; its existence never implies permission.
* **What did each HTTP try actually do?**  ``model_attempts``.  Every retry and
  every format-repair call is its own row, with its own request bytes, response
  bytes, hashes, usage and outcome.

Budget accounting is deliberately pessimistic.  The reservation row is
committed *before* the socket is opened, and the row is counted whether it is
still ``reserved`` (a crash), ``failed``, ``timeout`` or ``ok``.  A reservation
is never returned automatically.

One controlled UPDATE is allowed, and only one: a ``reserved`` row may become
``finished`` exactly once.  The migration trigger enforces that transition and
freezes every request/binding column.  There is no DELETE.

Like every other store API in this project, this is an API boundary, not a
security boundary; see ``docs/PCS2-006-TXB.md`` section 4.11.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..foundation import identifiers
from ..foundation.errors import (
    CorruptStore,
    InvalidInput,
    RecordNotFound,
    Redacted,
    ScopeViolation,
    StorageError,
)
from ..foundation.jsonutil import (
    canonical_json_text,
    new_payload_ref,
    sha256_hex,
    strict_json_loads,
)
from ..foundation.timeutil import utc_now_rfc3339
from .deletion import DELETION_EVENT, DELETION_RECORD, DeletionTarget

MODEL_AUTHORIZATION_ACTIVE = "active"
MODEL_AUTHORIZATION_REVOKED = "revoked"
MODEL_AUTHORIZATION_EXHAUSTED = "exhausted"
MODEL_AUTHORIZATION_STATUSES = (
    MODEL_AUTHORIZATION_ACTIVE,
    MODEL_AUTHORIZATION_REVOKED,
    MODEL_AUTHORIZATION_EXHAUSTED,
)

MODEL_ATTEMPT_RESERVED = "reserved"
MODEL_ATTEMPT_FINISHED = "finished"
MODEL_ATTEMPT_STATUSES = (MODEL_ATTEMPT_RESERVED, MODEL_ATTEMPT_FINISHED)

MODEL_ATTEMPT_OK = "ok"
MODEL_ATTEMPT_MALFORMED = "malformed"
MODEL_ATTEMPT_FAILED = "failed"
MODEL_ATTEMPT_TIMEOUT = "timeout"
MODEL_ATTEMPT_BUDGET_EXCEEDED = "budget_exceeded"
MODEL_ATTEMPT_OUTCOMES = (
    MODEL_ATTEMPT_OK,
    MODEL_ATTEMPT_MALFORMED,
    MODEL_ATTEMPT_FAILED,
    MODEL_ATTEMPT_TIMEOUT,
    MODEL_ATTEMPT_BUDGET_EXCEEDED,
)

#: An HTTP body/response larger than this is refused before it is persisted.
#: The Context itself is capped at 24 KiB; 2 MiB leaves ample room for a normal
#: chat completion and still bounds a hostile/looping provider.
MAX_ATTEMPT_BODY_BYTES = 2 * 1024 * 1024

_AUTHORIZATION_COLUMNS = (
    "authorization_id, namespace, authorization_ref, provider, model, "
    "manifest_hash, max_calls, max_output_tokens_per_call, max_wall_seconds, "
    "granted_at, expires_at, status, updated_at"
)

_ATTEMPT_COLUMNS = (
    "run_id, attempt_no, attempt_id, namespace, context_id, authorization_id, "
    "authorization_ref, manifest_hash, provider, model_requested, model_returned, "
    "system_fingerprint, adapter_kind, policy_version, request_payload_ref, "
    "request_hash, response_payload_ref, response_hash, usage_json, usage_known, "
    "cost_estimate, cost_known, finish_reason, status, outcome, error_label, "
    "started_at, finished_at"
)


# --------------------------------------------------------------------- errors


class ModelLedgerError(StorageError):
    """Base class for model-plane store refusals."""

    code = "CORRUPT_STORE"


class ModelNotAuthorized(ModelLedgerError):
    """No usable, active authorization covers this call."""

    code = "SCOPE_VIOLATION"


class ModelAuthorizationExpired(ModelNotAuthorized):
    """The matching authorization exists but its ``expires_at`` has passed."""


class ModelAuthorizationMismatch(ModelNotAuthorized):
    """The authorization is bound to another provider/model/manifest."""


class ModelBudgetExceeded(ModelLedgerError):
    """The attempt would exceed the authorization's call or wall-time budget."""

    code = "BUDGET_EXCEEDED"


class ModelAttemptConflict(ModelLedgerError):
    """An attempt is already finished, or a concurrent writer won the slot."""

    code = "ID_CONFLICT"


class ModelAttemptNotFound(RecordNotFound):
    """No model attempt exists for the supplied ``attempt_id``."""


# ------------------------------------------------------------------ dataclasses


@dataclass(frozen=True)
class ModelAuthorization:
    """One explicit permission record (never contains an API key)."""

    authorization_id: str
    namespace: str
    authorization_ref: str
    provider: str
    model: str
    manifest_hash: str
    max_calls: int
    max_output_tokens_per_call: int
    max_wall_seconds: int
    granted_at: str
    expires_at: str | None
    status: str
    updated_at: str


@dataclass(frozen=True)
class ModelAttempt:
    """One stored HTTP attempt, decoded for reading."""

    run_id: str
    attempt_no: int
    attempt_id: str
    namespace: str
    context_id: str
    authorization_id: str
    authorization_ref: str
    manifest_hash: str
    provider: str
    model_requested: str
    model_returned: str | None
    system_fingerprint: str | None
    adapter_kind: str
    policy_version: str
    request_payload_ref: str
    request_hash: str
    response_payload_ref: str | None
    response_hash: str | None
    usage: dict[str, Any] | None
    usage_known: bool
    cost_estimate: float | None
    cost_known: bool
    finish_reason: str | None
    status: str
    outcome: str | None
    error_label: str | None
    started_at: str
    finished_at: str | None

    @property
    def finished(self) -> bool:
        return self.status == MODEL_ATTEMPT_FINISHED


@dataclass(frozen=True)
class ModelAttemptReservation:
    """The committed reservation plus the limits that applied to it."""

    attempt: ModelAttempt
    authorization: ModelAuthorization
    effective_max_calls: int
    effective_max_output_tokens: int
    effective_max_wall_seconds: int
    remaining_wall_seconds: float


# ----------------------------------------------------------------------- mixin


class ModelLedgerMixin:
    """Authorization grants, budget reservations and the attempt ledger."""

    # ------------------------------------------------------------ authorization

    def grant_model_authorization(
        self,
        *,
        namespace: str,
        authorization_ref: str,
        provider: str,
        model: str,
        manifest_hash: str,
        max_calls: int,
        max_output_tokens_per_call: int,
        max_wall_seconds: int,
        granted_at: str | None = None,
        expires_at: str | None = None,
    ) -> ModelAuthorization:
        """Record one explicit permission.  This is the only way to allow calls.

        ``authorization_ref`` must carry the user's own words (or an explicit
        API caller's reference): it is the audit answer to "who allowed this,
        when, for which model and scope".  There is no ``enabled = true``
        configuration shortcut.
        """

        self._require_standalone("grant_model_authorization")
        ns = identifiers.require_namespace(namespace)
        reference = _require_authorization_ref(authorization_ref)
        provider_value = _require_token(provider, "provider", 64)
        model_value = _require_token(model, "model", 128)
        manifest = _require_manifest_hash(manifest_hash)
        calls = _require_int(max_calls, "max_calls", 1, 1000)
        tokens = _require_int(
            max_output_tokens_per_call,
            "max_output_tokens_per_call",
            1,
            393216,
        )
        wall = _require_int(max_wall_seconds, "max_wall_seconds", 1, 86400)
        now = utc_now_rfc3339()
        granted = now if granted_at is None else _require_timestamp(granted_at, "granted_at")
        expires = (
            None if expires_at is None else _require_timestamp(expires_at, "expires_at")
        )
        if expires is not None and _parse_timestamp(expires, "expires_at") <= _parse_timestamp(
            granted, "granted_at"
        ):
            raise InvalidInput("expires_at must be later than granted_at")

        authorization_id = str(uuid.uuid4())
        with self.transaction():
            self._require_connection().execute(
                "INSERT INTO model_authorizations ("
                "authorization_id, namespace, authorization_ref, provider, model, "
                "manifest_hash, max_calls, max_output_tokens_per_call, "
                "max_wall_seconds, granted_at, expires_at, status, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    authorization_id,
                    ns,
                    reference,
                    provider_value,
                    model_value,
                    manifest,
                    calls,
                    tokens,
                    wall,
                    granted,
                    expires,
                    MODEL_AUTHORIZATION_ACTIVE,
                    now,
                ),
            )
            return self._load_authorization_locked(authorization_id)

    def revoke_model_authorization(
        self, authorization_id: str, *, updated_at: str | None = None
    ) -> ModelAuthorization:
        """Move an active grant to ``revoked``; already-terminal rows are unchanged."""

        self._require_standalone("revoke_model_authorization")
        aid = _require_uuid(authorization_id, "authorization_id")
        now = utc_now_rfc3339() if updated_at is None else _require_timestamp(
            updated_at, "updated_at"
        )
        with self.transaction():
            current = self._load_authorization_locked(aid)
            if current.status == MODEL_AUTHORIZATION_ACTIVE:
                self._require_connection().execute(
                    "UPDATE model_authorizations SET status = ?, updated_at = ? "
                    "WHERE authorization_id = ? AND status = ?",
                    (MODEL_AUTHORIZATION_REVOKED, now, aid, MODEL_AUTHORIZATION_ACTIVE),
                )
            return self._load_authorization_locked(aid)

    def mark_model_authorization_exhausted(
        self, authorization_id: str, *, updated_at: str | None = None
    ) -> ModelAuthorization:
        """Record that the grant's own call budget has been spent.

        The attempt count remains the authority (it survives crashes); this
        status is the human-readable projection of that fact.  A revoked or
        already-exhausted grant is left unchanged.
        """

        self._require_standalone("mark_model_authorization_exhausted")
        aid = _require_uuid(authorization_id, "authorization_id")
        now = utc_now_rfc3339() if updated_at is None else _require_timestamp(
            updated_at, "updated_at"
        )
        with self.transaction():
            current = self._load_authorization_locked(aid)
            if current.status == MODEL_AUTHORIZATION_ACTIVE:
                self._require_connection().execute(
                    "UPDATE model_authorizations SET status = ?, updated_at = ? "
                    "WHERE authorization_id = ? AND status = ?",
                    (
                        MODEL_AUTHORIZATION_EXHAUSTED,
                        now,
                        aid,
                        MODEL_AUTHORIZATION_ACTIVE,
                    ),
                )
            return self._load_authorization_locked(aid)

    def model_authorization(self, authorization_id: str) -> ModelAuthorization:
        """Read one grant by ID."""

        aid = _require_uuid(authorization_id, "authorization_id")
        row = self._require_connection().execute(
            f"SELECT {_AUTHORIZATION_COLUMNS} FROM model_authorizations "
            "WHERE authorization_id = ?",
            (aid,),
        ).fetchone()
        if row is None:
            raise RecordNotFound(f"no model authorization {aid!r}")
        return _authorization_from_row(row)

    def model_authorizations(
        self,
        *,
        namespace: str | None = None,
        status: str | None = None,
    ) -> tuple[ModelAuthorization, ...]:
        """List grants newest first, optionally filtered."""

        sql = f"SELECT {_AUTHORIZATION_COLUMNS} FROM model_authorizations"
        clauses: list[str] = []
        parameters: list[Any] = []
        if namespace is not None:
            clauses.append("namespace = ?")
            parameters.append(identifiers.require_namespace(namespace))
        if status is not None:
            if status not in MODEL_AUTHORIZATION_STATUSES:
                raise InvalidInput(
                    f"status must be one of {', '.join(MODEL_AUTHORIZATION_STATUSES)}"
                )
            clauses.append("status = ?")
            parameters.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += (
            " ORDER BY julianday(granted_at) DESC, granted_at DESC, "
            "authorization_id DESC"
        )
        rows = self._require_connection().execute(sql, tuple(parameters)).fetchall()
        return tuple(_authorization_from_row(row) for row in rows)

    def active_model_authorization(
        self,
        *,
        namespace: str,
        provider: str,
        model: str,
        manifest_hash: str,
    ) -> ModelAuthorization | None:
        """Newest unexpired active grant matching the exact call scope.

        Expired grants are skipped rather than returned: an older still-valid
        grant, if one exists, is the one that actually authorizes the call.
        """

        ns = identifiers.require_namespace(namespace)
        provider_value = _require_token(provider, "provider", 64)
        model_value = _require_token(model, "model", 128)
        manifest = _require_manifest_hash(manifest_hash)
        rows = self._require_connection().execute(
            f"SELECT {_AUTHORIZATION_COLUMNS} FROM model_authorizations "
            "WHERE namespace = ? AND provider = ? AND model = ? "
            "AND manifest_hash = ? AND status = ? "
            "ORDER BY julianday(granted_at) DESC, granted_at DESC, "
            "authorization_id DESC",
            (
                ns,
                provider_value,
                model_value,
                manifest,
                MODEL_AUTHORIZATION_ACTIVE,
            ),
        ).fetchall()
        now = utc_now_rfc3339()
        now_dt = _parse_timestamp(now, "now")
        for row in rows:
            authorization = _authorization_from_row(row)
            if authorization.expires_at is None:
                return authorization
            if _parse_timestamp(authorization.expires_at, "expires_at") > now_dt:
                return authorization
        return None

    # ---------------------------------------------------------------- attempts

    def count_model_attempts(self, authorization_id: str) -> int:
        """Every reserved/finished attempt counts, including a crashed one."""

        aid = _require_uuid(authorization_id, "authorization_id")
        row = self._require_connection().execute(
            "SELECT COUNT(*) AS calls FROM model_attempts WHERE authorization_id = ?",
            (aid,),
        ).fetchone()
        return int(row["calls"])

    def reserve_model_attempt(
        self,
        *,
        run_id: str,
        namespace: str,
        context_id: str,
        authorization_id: str,
        provider: str,
        model_requested: str,
        manifest_hash: str,
        adapter_kind: str,
        policy_version: str,
        request_body: str,
        manifest_max_calls: int,
        manifest_max_output_tokens_per_call: int,
        manifest_max_wall_seconds: int,
    ) -> ModelAttemptReservation:
        """Commit one ``reserved`` row before any socket is opened.

        The authorization is re-checked here, in the same transaction that
        inserts the row.  Call-count and wall-time budgets are compared against
        the *effective* limits (the stricter of manifest and authorization).
        If the budget is exhausted, no row is inserted: the caller must not send.
        """

        self._require_standalone("reserve_model_attempt")
        run = _require_uuid(run_id, "run_id")
        ns = identifiers.require_namespace(namespace)
        cid = _require_uuid(context_id, "context_id")
        aid = _require_uuid(authorization_id, "authorization_id")
        provider_value = _require_token(provider, "provider", 64)
        model_value = _require_token(model_requested, "model_requested", 128)
        manifest = _require_manifest_hash(manifest_hash)
        adapter = _require_token(adapter_kind, "adapter_kind", 64)
        policy = _require_token(policy_version, "policy_version", 128)
        if not isinstance(request_body, str) or not request_body:
            raise InvalidInput("request_body must be a non-empty string")
        _require_body_size(request_body, "request_body")
        manifest_calls = _require_int(manifest_max_calls, "manifest_max_calls", 1, 1000)
        manifest_tokens = _require_int(
            manifest_max_output_tokens_per_call,
            "manifest_max_output_tokens_per_call",
            1,
            393216,
        )
        manifest_wall = _require_int(
            manifest_max_wall_seconds, "manifest_max_wall_seconds", 1, 86400
        )

        with self.transaction():
            context_row = self._require_connection().execute(
                "SELECT namespace FROM context_snapshots WHERE context_id = ?",
                (cid,),
            ).fetchone()
            if context_row is None:
                raise RecordNotFound(f"no persisted context {cid!r}")
            if context_row["namespace"] != ns:
                raise ScopeViolation(
                    f"context {cid} belongs to namespace {context_row['namespace']!r}"
                )

            authorization = self._load_authorization_locked(aid)
            now = utc_now_rfc3339()
            if authorization.status != MODEL_AUTHORIZATION_ACTIVE:
                raise ModelNotAuthorized(
                    f"model authorization {aid} is {authorization.status!r}, not active"
                )
            if authorization.namespace != ns:
                raise ModelAuthorizationMismatch(
                    "the model authorization belongs to another namespace"
                )
            if (
                authorization.provider != provider_value
                or authorization.model != model_value
                or authorization.manifest_hash != manifest
            ):
                raise ModelAuthorizationMismatch(
                    "the model authorization does not cover this provider/model/manifest"
                )
            if authorization.expires_at is not None:
                if _parse_timestamp(authorization.expires_at, "expires_at") <= _parse_timestamp(
                    now, "now"
                ):
                    raise ModelAuthorizationExpired(
                        f"model authorization {aid} expired at {authorization.expires_at}"
                    )

            effective_calls = min(manifest_calls, authorization.max_calls)
            effective_tokens = min(
                manifest_tokens, authorization.max_output_tokens_per_call
            )
            effective_wall = min(manifest_wall, authorization.max_wall_seconds)

            used = int(
                self._require_connection()
                .execute(
                    "SELECT COUNT(*) AS calls FROM model_attempts "
                    "WHERE authorization_id = ?",
                    (aid,),
                )
                .fetchone()["calls"]
            )
            if used >= effective_calls:
                raise ModelBudgetExceeded(
                    f"model authorization {aid} has used {used} of "
                    f"{effective_calls} allowed call(s)"
                )

            first_row = self._require_connection().execute(
                "SELECT MIN(started_at) AS first_started FROM model_attempts "
                "WHERE run_id = ?",
                (run,),
            ).fetchone()
            if first_row["first_started"] is None:
                remaining_wall = float(effective_wall)
            else:
                elapsed = (
                    _parse_timestamp(now, "now")
                    - _parse_timestamp(first_row["first_started"], "started_at")
                ).total_seconds()
                remaining_wall = float(effective_wall) - max(elapsed, 0.0)
            if remaining_wall <= 0:
                raise ModelBudgetExceeded(
                    f"run {run} used its {effective_wall}s wall-time budget"
                )

            existing = self._require_connection().execute(
                "SELECT namespace, context_id FROM model_attempts "
                "WHERE run_id = ? LIMIT 1",
                (run,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["namespace"] != ns
                    or existing["context_id"] != cid
                ):
                    raise ModelAttemptConflict(
                        f"run {run} is already bound to another namespace/context"
                    )

            next_row = self._require_connection().execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no "
                "FROM model_attempts WHERE run_id = ?",
                (run,),
            ).fetchone()
            attempt_no = int(next_row["next_no"])
            if not 1 <= attempt_no <= 1000:
                raise ModelAttemptConflict(
                    f"run {run} has no attempt number left (attempt_no={attempt_no})"
                )

            payload_ref, request_hash = self._store_attempt_payload_locked(
                "request_body", request_body
            )
            attempt_id = str(uuid.uuid4())
            try:
                self._require_connection().execute(
                    "INSERT INTO model_attempts ("
                    "run_id, attempt_no, attempt_id, namespace, context_id, "
                    "authorization_id, authorization_ref, manifest_hash, provider, "
                    "model_requested, adapter_kind, policy_version, "
                    "request_payload_ref, request_hash, usage_known, cost_known, "
                    "status, started_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
                    (
                        run,
                        attempt_no,
                        attempt_id,
                        ns,
                        cid,
                        aid,
                        authorization.authorization_ref,
                        manifest,
                        provider_value,
                        model_value,
                        adapter,
                        policy,
                        payload_ref,
                        request_hash,
                        MODEL_ATTEMPT_RESERVED,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed" in str(exc):
                    raise ModelAttemptConflict(
                        f"run {run} attempt {attempt_no} was reserved concurrently"
                    ) from exc
                raise

            row = self._require_connection().execute(
                f"SELECT {_ATTEMPT_COLUMNS} FROM model_attempts "
                "WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            return ModelAttemptReservation(
                attempt=_attempt_from_row(row),
                authorization=authorization,
                effective_max_calls=effective_calls,
                effective_max_output_tokens=effective_tokens,
                effective_max_wall_seconds=effective_wall,
                remaining_wall_seconds=remaining_wall,
            )

    def finish_model_attempt(
        self,
        *,
        attempt_id: str,
        outcome: str,
        response_body: str | None = None,
        model_returned: str | None = None,
        system_fingerprint: str | None = None,
        finish_reason: str | None = None,
        usage: Mapping[str, Any] | None = None,
        usage_known: bool = False,
        cost_estimate: float | None = None,
        cost_known: bool = False,
        error_label: str | None = None,
    ) -> ModelAttempt:
        """Settle a reserved attempt exactly once.

        ``response_body`` is the exact UTF-8 text received (an HTTP error body
        included).  A timeout/failed network attempt has no response body.
        """

        self._require_standalone("finish_model_attempt")
        aid = _require_uuid(attempt_id, "attempt_id")
        if outcome not in MODEL_ATTEMPT_OUTCOMES:
            raise InvalidInput(
                f"outcome must be one of {', '.join(MODEL_ATTEMPT_OUTCOMES)}"
            )
        response_text = None if response_body is None else _require_text_body(
            response_body, "response_body"
        )
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise InvalidInput("usage must be a JSON object or null")
            usage_text: str | None = canonical_json_text(dict(usage))
            if len(usage_text) > 65536:
                raise InvalidInput("usage is too large to store")
        else:
            usage_text = None
        if type(usage_known) is not bool:
            raise InvalidInput("usage_known must be a boolean")
        if usage_known and usage_text is None:
            raise InvalidInput("usage_known requires the usage object")
        if type(cost_known) is not bool:
            raise InvalidInput("cost_known must be a boolean")
        if cost_known:
            if cost_estimate is None:
                raise InvalidInput("cost_known requires cost_estimate")
            if type(cost_estimate) not in (int, float) or cost_estimate < 0:
                raise InvalidInput("cost_estimate must be a non-negative number")
            cost_value: float | None = float(cost_estimate)
        else:
            if cost_estimate is not None:
                raise InvalidInput("cost_estimate must be null while cost_known is false")
            cost_value = None
        returned = _optional_token(model_returned, "model_returned", 256)
        fingerprint = _optional_token(system_fingerprint, "system_fingerprint", 256)
        reason = _optional_token(finish_reason, "finish_reason", 64)
        label = _optional_token(error_label, "error_label", 128)

        if outcome == MODEL_ATTEMPT_OK:
            if response_text is None:
                raise InvalidInput("an ok attempt must carry the response it parsed")
            if reason != "stop":
                raise InvalidInput("an ok attempt must have finish_reason 'stop'")
            if label is not None:
                raise InvalidInput("an ok attempt cannot carry an error label")

        now = utc_now_rfc3339()
        with self.transaction():
            current = self._require_connection().execute(
                f"SELECT {_ATTEMPT_COLUMNS} FROM model_attempts "
                "WHERE attempt_id = ?",
                (aid,),
            ).fetchone()
            if current is None:
                raise ModelAttemptNotFound(f"no model attempt {aid!r}")
            if current["status"] != MODEL_ATTEMPT_RESERVED:
                raise ModelAttemptConflict(
                    f"model attempt {aid} is already {current['status']!r}"
                )

            response_ref: str | None = None
            response_hash: str | None = None
            if response_text is not None:
                response_ref, response_hash = self._store_attempt_payload_locked(
                    "response_body", response_text
                )

            try:
                cursor = self._require_connection().execute(
                    "UPDATE model_attempts SET "
                    "status = ?, outcome = ?, response_payload_ref = ?, "
                    "response_hash = ?, model_returned = ?, system_fingerprint = ?, "
                    "usage_json = ?, usage_known = ?, cost_estimate = ?, "
                    "cost_known = ?, finish_reason = ?, error_label = ?, "
                    "finished_at = ? "
                    "WHERE attempt_id = ? AND status = ?",
                    (
                        MODEL_ATTEMPT_FINISHED,
                        outcome,
                        response_ref,
                        response_hash,
                        returned,
                        fingerprint,
                        usage_text,
                        1 if usage_known else 0,
                        cost_value,
                        1 if cost_known else 0,
                        reason,
                        label,
                        now,
                        aid,
                        MODEL_ATTEMPT_RESERVED,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ModelAttemptConflict(
                    f"model attempt {aid} could not be settled: {exc}"
                ) from exc
            if cursor.rowcount != 1:
                raise ModelAttemptConflict(
                    f"model attempt {aid} was settled concurrently"
                )
            row = self._require_connection().execute(
                f"SELECT {_ATTEMPT_COLUMNS} FROM model_attempts "
                "WHERE attempt_id = ?",
                (aid,),
            ).fetchone()
            return _attempt_from_row(row)

    def model_attempts_for_run(self, run_id: str) -> tuple[ModelAttempt, ...]:
        """Every attempt of one logical run, in attempt order."""

        run = _require_uuid(run_id, "run_id")
        rows = self._require_connection().execute(
            f"SELECT {_ATTEMPT_COLUMNS} FROM model_attempts "
            "WHERE run_id = ? ORDER BY attempt_no",
            (run,),
        ).fetchall()
        return tuple(_attempt_from_row(row) for row in rows)

    def model_attempts_for_context(self, context_id: str) -> tuple[ModelAttempt, ...]:
        """Every attempt whose request used one persisted Context."""

        cid = _require_uuid(context_id, "context_id")
        rows = self._require_connection().execute(
            f"SELECT {_ATTEMPT_COLUMNS} FROM model_attempts "
            "WHERE context_id = ? ORDER BY started_at, attempt_no",
            (cid,),
        ).fetchall()
        return tuple(_attempt_from_row(row) for row in rows)

    def model_attempts_for_authorization(
        self, authorization_id: str
    ) -> tuple[ModelAttempt, ...]:
        """Every attempt charged to one grant (budget audit)."""

        aid = _require_uuid(authorization_id, "authorization_id")
        rows = self._require_connection().execute(
            f"SELECT {_ATTEMPT_COLUMNS} FROM model_attempts "
            "WHERE authorization_id = ? ORDER BY started_at, attempt_no",
            (aid,),
        ).fetchall()
        return tuple(_attempt_from_row(row) for row in rows)

    def load_model_attempt_payload(
        self, attempt_id: str, *, kind: str = "request"
    ) -> str:
        """Read one attempt's exact request/response text.

        A payload cleared by deletion is ``REDACTED``; a missing payload or a
        hash mismatch is ``CORRUPT_STORE``.  The wrapper digest and the inner
        body hash are both re-verified.
        """

        aid = _require_uuid(attempt_id, "attempt_id")
        if kind not in ("request", "response"):
            raise InvalidInput("kind must be 'request' or 'response'")
        row = self._require_connection().execute(
            "SELECT request_payload_ref, request_hash, response_payload_ref, "
            "response_hash, status FROM model_attempts WHERE attempt_id = ?",
            (aid,),
        ).fetchone()
        if row is None:
            raise ModelAttemptNotFound(f"no model attempt {aid!r}")
        if kind == "request":
            payload_ref = row["request_payload_ref"]
            expected_hash = row["request_hash"]
            wrapper_key = "request_body"
        else:
            payload_ref = row["response_payload_ref"]
            expected_hash = row["response_hash"]
            wrapper_key = "response_body"
            if payload_ref is None:
                raise RecordNotFound(
                    f"model attempt {aid} has no response payload"
                )

        payload_row = self._require_connection().execute(
            "SELECT sha256, body_json FROM private_payloads WHERE payload_ref = ?",
            (payload_ref,),
        ).fetchone()
        if payload_row is None:
            raise CorruptStore(
                f"model attempt {aid} references missing payload {payload_ref}"
            )
        if payload_row["body_json"] == "":
            raise Redacted(
                f"the {kind} body of model attempt {aid} has been cleared by a deletion"
            )
        body_text = payload_row["body_json"]
        if payload_row["sha256"] != sha256_hex(body_text):
            raise CorruptStore(
                f"payload {payload_ref} sha256 does not match its bytes"
            )
        try:
            wrapper = strict_json_loads(body_text)
        except ValueError as exc:
            raise CorruptStore(f"payload {payload_ref} is not strict JSON") from exc
        if not isinstance(wrapper, dict) or set(wrapper) != {wrapper_key}:
            raise CorruptStore(f"payload {payload_ref} has an unexpected shape")
        text = wrapper[wrapper_key]
        if not isinstance(text, str):
            raise CorruptStore(f"payload {payload_ref} does not hold text")
        if expected_hash != "sha256:" + sha256_hex(text):
            raise CorruptStore(
                f"model attempt {aid} {kind} hash does not match its payload"
            )
        return text

    # --------------------------------------------------------------- deletion

    def _after_target_cleared_locked(self, target: DeletionTarget) -> int:
        """Extend the Context payload clearing to model-attempt payloads.

        The MRO places this mixin before ``ContextStateMixin`` so ``super()``
        clears the Context's rendered text first, then this method clears every
        request/response body charged to an attempt that used that Context.  A
        response may echo private input, so both sides are cleared.
        """

        cleared = super()._after_target_cleared_locked(target)  # type: ignore[misc]
        return cleared + self._clear_model_attempt_payloads_locked(target)

    def _clear_model_attempt_payloads_locked(self, target: DeletionTarget) -> int:
        if target.target_kind == DELETION_RECORD:
            ref_kind = "record"
        elif target.target_kind == DELETION_EVENT:
            ref_kind = "event"
        else:  # pragma: no cover - deletion layer validates the kind
            return 0
        context_ids = self.context_ids_for_source(
            namespace=target.namespace,
            ref_kind=ref_kind,
            ref_id=target.target_id,
        )
        if not context_ids:
            return 0

        payload_refs: list[str] = []
        seen: set[str] = set()
        for start in range(0, len(context_ids), 500):
            chunk = context_ids[start : start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._require_connection().execute(
                "SELECT request_payload_ref, response_payload_ref "
                "FROM model_attempts "
                f"WHERE context_id IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            for row in rows:
                for ref in (row["request_payload_ref"], row["response_payload_ref"]):
                    if ref is not None and ref not in seen:
                        seen.add(ref)
                        payload_refs.append(str(ref))

        cleared = 0
        for payload_ref in payload_refs:
            cursor = self._require_connection().execute(
                "UPDATE private_payloads SET body_json = '', sha256 = ? "
                "WHERE payload_ref = ? AND body_json <> ''",
                (_tombstone_digest(), payload_ref),
            )
            cleared += 1 if cursor.rowcount else 0
        return cleared

    # -------------------------------------------------------------- internals

    def _store_attempt_payload_locked(
        self, wrapper_key: str, text: str
    ) -> tuple[str, str]:
        if self._tx_depth == 0:
            raise RuntimeError(
                "_store_attempt_payload_locked must run inside the caller's transaction"
            )
        if wrapper_key not in ("request_body", "response_body"):
            raise InvalidInput("unknown attempt payload wrapper")
        body_text = canonical_json_text({wrapper_key: text})
        payload_ref = new_payload_ref()
        self._require_connection().execute(
            "INSERT INTO private_payloads (payload_ref, sha256, body_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (payload_ref, sha256_hex(body_text), body_text, utc_now_rfc3339()),
        )
        return payload_ref, "sha256:" + sha256_hex(text)

    def _load_authorization_locked(self, authorization_id: str) -> ModelAuthorization:
        row = self._require_connection().execute(
            f"SELECT {_AUTHORIZATION_COLUMNS} FROM model_authorizations "
            "WHERE authorization_id = ?",
            (authorization_id,),
        ).fetchone()
        if row is None:
            raise RecordNotFound(
                f"no model authorization {authorization_id!r}"
            )
        return _authorization_from_row(row)


# ------------------------------------------------------------------- decoding


def _authorization_from_row(row: Any) -> ModelAuthorization:
    return ModelAuthorization(
        authorization_id=row["authorization_id"],
        namespace=row["namespace"],
        authorization_ref=row["authorization_ref"],
        provider=row["provider"],
        model=row["model"],
        manifest_hash=row["manifest_hash"],
        max_calls=int(row["max_calls"]),
        max_output_tokens_per_call=int(row["max_output_tokens_per_call"]),
        max_wall_seconds=int(row["max_wall_seconds"]),
        granted_at=row["granted_at"],
        expires_at=row["expires_at"],
        status=row["status"],
        updated_at=row["updated_at"],
    )


def _attempt_from_row(row: Any) -> ModelAttempt:
    usage_text = row["usage_json"]
    usage: dict[str, Any] | None = None
    if usage_text is not None:
        try:
            decoded = strict_json_loads(usage_text)
        except ValueError as exc:
            raise CorruptStore("stored model usage is not strict JSON") from exc
        if not isinstance(decoded, dict):
            raise CorruptStore("stored model usage is not a JSON object")
        usage = decoded
    return ModelAttempt(
        run_id=row["run_id"],
        attempt_no=int(row["attempt_no"]),
        attempt_id=row["attempt_id"],
        namespace=row["namespace"],
        context_id=row["context_id"],
        authorization_id=row["authorization_id"],
        authorization_ref=row["authorization_ref"],
        manifest_hash=row["manifest_hash"],
        provider=row["provider"],
        model_requested=row["model_requested"],
        model_returned=row["model_returned"],
        system_fingerprint=row["system_fingerprint"],
        adapter_kind=row["adapter_kind"],
        policy_version=row["policy_version"],
        request_payload_ref=row["request_payload_ref"],
        request_hash=row["request_hash"],
        response_payload_ref=row["response_payload_ref"],
        response_hash=row["response_hash"],
        usage=usage,
        usage_known=bool(int(row["usage_known"])),
        cost_estimate=(
            None if row["cost_estimate"] is None else float(row["cost_estimate"])
        ),
        cost_known=bool(int(row["cost_known"])),
        finish_reason=row["finish_reason"],
        status=row["status"],
        outcome=row["outcome"],
        error_label=row["error_label"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


# --------------------------------------------------------------------- helpers


def _require_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str) or identifiers.UUID_PATTERN.fullmatch(value) is None:
        raise InvalidInput(f"{field} must be a canonical lower-case UUID")
    return value


def _require_token(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise InvalidInput(f"{field} must be at most {max_length} characters")
    return value


def _optional_token(value: Any, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    return _require_token(value, field, max_length)


def _require_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise InvalidInput(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise InvalidInput(f"{field} must be between {minimum} and {maximum}")
    return value


def _require_authorization_ref(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput("authorization_ref must be the explicit grant text")
    if len(value) > 4096:
        raise InvalidInput("authorization_ref must be at most 4096 characters")
    return value


def _require_manifest_hash(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise InvalidInput("manifest_hash must be 'sha256:<64 lower-case hex>'")
    return value


def _require_timestamp(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or identifiers.TIMESTAMP_PATTERN.fullmatch(value) is None
    ):
        raise InvalidInput(f"{field} must be a UTC RFC 3339 timestamp ending in Z")
    return value


def _parse_timestamp(value: str, field: str) -> datetime:
    _require_timestamp(value, field)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise InvalidInput(f"{field} must carry a UTC offset")
    return parsed.astimezone(timezone.utc)


def _require_text_body(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidInput(f"{field} must be a string")
    _require_body_size(value, field)
    return value


def _require_body_size(value: str, field: str) -> None:
    if len(value.encode("utf-8")) > MAX_ATTEMPT_BODY_BYTES:
        raise InvalidInput(
            f"{field} exceeds the {MAX_ATTEMPT_BODY_BYTES}-byte attempt body limit"
        )


def _tombstone_digest() -> str:
    import secrets

    return secrets.token_hex(32)


__all__ = [
    "MAX_ATTEMPT_BODY_BYTES",
    "MODEL_ATTEMPT_BUDGET_EXCEEDED",
    "MODEL_ATTEMPT_FAILED",
    "MODEL_ATTEMPT_FINISHED",
    "MODEL_ATTEMPT_MALFORMED",
    "MODEL_ATTEMPT_OK",
    "MODEL_ATTEMPT_OUTCOMES",
    "MODEL_ATTEMPT_RESERVED",
    "MODEL_ATTEMPT_STATUSES",
    "MODEL_ATTEMPT_TIMEOUT",
    "MODEL_AUTHORIZATION_ACTIVE",
    "MODEL_AUTHORIZATION_EXHAUSTED",
    "MODEL_AUTHORIZATION_REVOKED",
    "MODEL_AUTHORIZATION_STATUSES",
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
]
