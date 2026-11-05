"""QUINTESSENCE desktop Consult sidecar speaking framed ``desktop.v1`` on stdio.

Architecture: a minimal reader loop plus one consult execution slot.  The
reader never blocks on a consult, so ``bridge.ping`` and ``app.info`` stay
responsive and a second ``consult.interact`` is answered with ``BUSY``
immediately.  A second consult is never queued and never creates a second
interaction, Context, input or Attempt.

stdout carries frames and nothing else.  Diagnostics go to stderr and never
contain a question, a reply, a prompt, a database path, a model credential or
an endpoint.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import framing
from host_config import (
    APP_NAME,
    APP_VERSION,
    MAX_QUESTION_CHARS,
    PROTOCOL_VERSION,
    ConfigError,
    HostConfig,
    load_config,
    memory_recall_status,
    web_status,
)
from locking import DataInstanceLock, DataLocked
from runtime import (
    ConversationRequestError,
    DesktopRuntime,
    InputRegistrationError,
    PersonaControlError,
    ReviewRequestError,
    RuntimeStartupError,
    valid_persona_import_name,
)
from quintessence_pcs.domains.errors import SourceUntrusted
from quintessence_pcs.foundation.errors import (
    AuthorizationRequired,
    ContextStale,
    CorruptStore,
    DbBusy,
    EventNotFound,
    InvalidInput,
    RecordNotFound,
    RecordVersionConflict,
    Redacted,
    StateRevisionConflict,
)
from quintessence_pcs.runtime import (
    ModelAdapterError,
    ModelBudgetExceededError,
    ModelConfigurationError,
    ModelNotAuthorizedError,
    ModelResponseError,
    ModelTransportError,
)
from quintessence_pcs.store.errors import (
    CandidateTransitionDenied,
    ClientRequestConflict,
    TurnStateConflict,
)
from quintessence_pcs.conversation import dto as conversation_dto
from quintessence_pcs.conversation.event_ring import TurnEventRing
from quintessence_pcs.foundation.jsonutil import sha256_hex

LOGGER = logging.getLogger("quintessence.desktop.bridge")

ERROR_INVALID_REQUEST = "INVALID_REQUEST"
ERROR_INVALID_JSON = "INVALID_JSON"
ERROR_INVALID_PARAMS = "INVALID_PARAMS"
ERROR_DUPLICATE_REQUEST_ID = "DUPLICATE_REQUEST_ID"
ERROR_AUTHORITY_FIELD = "AUTHORITY_FIELD_REJECTED"
ERROR_METHOD_NOT_FOUND = "METHOD_NOT_FOUND"
ERROR_RESERVED = "RESERVED_NOT_IMPLEMENTED"
ERROR_BUSY = "BUSY"
ERROR_INTERNAL = "INTERNAL_ERROR"
ERROR_HOST_NOT_READY = "HOST_NOT_READY"
ERROR_DATA_LOCKED = "DATA_LOCKED"
ERROR_SESSION_MISMATCH = "SESSION_MISMATCH"
ERROR_CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
ERROR_DESTINATION_MISMATCH = "DESTINATION_MISMATCH"
ERROR_INPUT_REGISTRATION = "INPUT_REGISTRATION_FAILED"
ERROR_UNAVAILABLE = "SIDECAR_UNAVAILABLE"
ERROR_AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
ERROR_REVISION_CONFLICT = "REVISION_CONFLICT"
ERROR_CANDIDATE_STALE = "CANDIDATE_STALE"
ERROR_CANDIDATE_VERSION_MISMATCH = "CANDIDATE_VERSION_MISMATCH"
ERROR_CANDIDATE_EXISTS = "CANDIDATE_EXISTS"
ERROR_WORK_ALREADY_RESOLVED = "WORK_ALREADY_RESOLVED"
ERROR_WORK_NOT_PENDING = "WORK_NOT_PENDING"
ERROR_LEGACY_WORK_READ_ONLY = "LEGACY_WORK_READ_ONLY"
ERROR_UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
ERROR_NOT_FOUND = "NOT_FOUND"
ERROR_REDACTED = "REDACTED"
ERROR_CLIENT_REQUEST_CONFLICT = "CONFLICT"
ERROR_DB_BUSY = "DB_BUSY"
ERROR_PERSONA_CONTROL_DISABLED = "PERSONA_CONTROL_DISABLED"
ERROR_PERSONA_EXPECTATION_CONFLICT = "PERSONA_EXPECTATION_CONFLICT"
ERROR_MAINTENANCE_STATE_INVALID = "MAINTENANCE_STATE_INVALID"
ERROR_IMPORT_UNAVAILABLE = "IMPORT_UNAVAILABLE"
ERROR_IMPORT_INVALID = "IMPORT_INVALID"
ERROR_PREFERENCE_NOT_ACTIVE = "PREFERENCE_NOT_ACTIVE"

REVIEW_READ_METHODS = frozenset(
    {"review.work.list", "review.candidates.list", "review.candidate.detail"}
)
REVIEW_METHODS = frozenset(
    {
        *REVIEW_READ_METHODS,
        "review.generate",
        "review.resolve",
        "review.result",
    }
)
MODEL_TEST_METHOD = "model.test_connection"
MAINTENANCE_METHODS = frozenset(
    {"maintenance.status", "maintenance.run", "maintenance.control"}
)
IMPLEMENTED_METHODS = frozenset(
    {
        "app.info",
        "app.confirm_destination",
        "consult.interact",
        "conversation.create",
        "conversation.list",
        "conversation.get",
        "conversation.send",
        "conversation.search",
        "turn.result",
        "turn.events",
        "turn.steps",
        "turn.trace",
        "conversation.usage",
        "turn.cancel",
        "bridge.ping",
        "settings.reload",
        "memory.bridge.sync",
        "memory.bridge.deliver",
        "memory.search",
        "postprocess.status",
        "postprocess.retry",
        "cognition.candidates.list",
        "cognition.candidate.decide",
        "profile.status",
        "profile.read",
        "profile.publish",
        "profile.activate",
        "profile.list",
        "profile.read_body",
        "profile.import.list",
        "profile.import.preview",
        "profile.publish_reviewed",
        "preference.list",
        "preference.revoke",
        MODEL_TEST_METHOD,
        *REVIEW_METHODS,
        *MAINTENANCE_METHODS,
    }
)

#: Methods that exist in the long-term interaction vocabulary but are not
#: implemented by C12c.  They are answered honestly instead of being faked.
RESERVED_METHODS = frozenset(
    {
        "consult.result",
        "consult.history",
        "consult.resume",
        "consult.cancel",
        "review.commit",
        "review.cancel",
        "review.history",
    }
)

#: Fields a compromised WebView might send to widen authority.  The whole
#: request is rejected before an input, Context, interaction or Attempt exists.
AUTHORITY_FIELDS = frozenset(
    {
        "privacy_classes",
        "privacy_class",
        "privacy",
        "purposes",
        "purpose",
        "disclose",
        "grant",
        "grants",
        "authorization",
        "authorization_ref",
        "access_ref",
        "provider",
        "endpoint",
        "base_url",
        "model",
        "model_protocol",
        "manifest_hash",
        "max_calls",
        "budget",
        "namespace",
        "host_instance",
        "operator",
        "policy_ref",
        "query_plan",
        "input_refs",
        "input_ref",
        "origin_kind",
        "retention",
        "interaction_id",
        "context_id",
        "confirmation",
        "confirmed",
        "destination",
        "confirmation_required",
        "external",
        "transport",
        "config",
        "config_path",
        "secret",
        "api_key",
    }
)

#: ``destination_id`` is legitimate in ``app.confirm_destination`` but never in
#: ``consult.interact``; the consult path adds it to the rejected set.
_CONSULT_AUTHORITY_FIELDS = AUTHORITY_FIELDS | {"destination_id"}

#: A review decision accepts no authority material.  ``candidate_id``,
#: ``artifact_version`` and ``decision`` are the only decision inputs; the
#: decision id, any Approval/grant, the candidate body and the model/provider
#: are host-owned.
_RESOLVE_AUTHORITY_FIELDS = AUTHORITY_FIELDS | {
    "approval",
    "approvals",
    "authority",
    "candidate_body",
    "body",
    "proposal",
    "decision_id",
    "decision_ref",
    "grant_id",
    "permissions",
    "role",
    "state_revision",
    "token",
    "reviewer",
}

#: ``review.result`` is the one method whose legitimate argument is an
#: interaction id; every other method rejects a frontend-supplied one.
_REVIEW_RESULT_AUTHORITY_FIELDS = AUTHORITY_FIELDS - {"interaction_id"}

_WORK_LIST_KEYS = frozenset({"session_id"})
_CANDIDATE_LIST_KEYS = frozenset({"session_id", "work_id", "generation"})
_CANDIDATE_DETAIL_KEYS = frozenset(
    {"session_id", "candidate_id", "artifact_version"}
)
_REVIEW_GENERATE_KEYS = frozenset(
    {"session_id", "work_id", "event_id", "confirmed_destination_id"}
)
_REVIEW_RESOLVE_KEYS = frozenset(
    {"session_id", "candidate_id", "artifact_version", "decision"}
)
_REVIEW_RESULT_KEYS = frozenset({"session_id", "interaction_id"})
_MODEL_TEST_KEYS = frozenset({"session_id"})
_PROFILE_STATUS_KEYS = frozenset({"session_id"})
_PROFILE_READ_KEYS = frozenset({"session_id"})
_PROFILE_PUBLISH_KEYS = frozenset(
    {"session_id", "profile_id", "body", "review_event_id"}
)
_PROFILE_ACTIVATE_KEYS = frozenset({"session_id", "profile_id", "record_version"})
_PROFILE_LIST_KEYS = frozenset({"session_id"})
_PROFILE_READ_BODY_KEYS = frozenset(
    {"session_id", "record_id", "record_version"}
)
_PROFILE_IMPORT_LIST_KEYS = frozenset({"session_id"})
_PROFILE_IMPORT_PREVIEW_KEYS = frozenset({"session_id", "name"})
_PROFILE_PUBLISH_REVIEWED_KEYS = frozenset(
    {"session_id", "import_id", "profile_id", "confirm"}
)
_PREFERENCE_LIST_KEYS = frozenset({"session_id", "conversation_id"})
_PREFERENCE_REVOKE_KEYS = frozenset(
    {"session_id", "conversation_id", "dimension", "reason"}
)

_CONSULT_KEYS = frozenset({"session_id", "question", "confirmed_destination_id"})

_CONVERSATION_CREATE_KEYS = frozenset({"session_id", "mode"})
_CONVERSATION_LIST_KEYS = frozenset({"session_id", "limit", "cursor"})
_CONVERSATION_GET_KEYS = frozenset(
    {"session_id", "conversation_id", "before_turn_no", "before_seq", "limit"}
)
_CONVERSATION_SEND_KEYS = frozenset(
    {
        "session_id",
        "conversation_id",
        "client_request_id",
        "text",
        "confirmed_destination_id",
    }
)
_CONVERSATION_SEARCH_KEYS = frozenset(
    {"session_id", "query", "limit", "conversation_id", "before", "include_full"}
)
_MEMORY_SEARCH_KEYS = frozenset({"session_id", "query", "limit", "cursor"})
_TURN_RESULT_KEYS = frozenset({"session_id", "turn_id"})
_TURN_TRACE_KEYS = frozenset({"session_id", "turn_id"})
_CONVERSATION_USAGE_KEYS = frozenset(
    {"session_id", "conversation_id", "turn_id"}
)
_TURN_EVENTS_KEYS = frozenset({"session_id", "turn_id", "after_seq", "limit"})
_TURN_STEPS_KEYS = frozenset(
    {"session_id", "turn_id", "after_step_no", "limit"}
)
_TURN_CANCEL_KEYS = frozenset({"session_id", "turn_id"})
_POSTPROCESS_STATUS_KEYS = frozenset(
    {"session_id", "conversation_id", "turn_ids"}
)
_POSTPROCESS_RETRY_KEYS = frozenset({"session_id", "turn_id"})
_COGNITION_LIST_KEYS = frozenset({"session_id", "limit"})
_COGNITION_DECIDE_KEYS = frozenset(
    {"session_id", "candidate_id", "artifact_version", "decision"}
)
#: RG05 decisions take only the candidate identity and the human's choice.  The
#: ApprovalAction, its statement event, the scope and the digest are host-built.
_COGNITION_DECIDE_FORBIDDEN = AUTHORITY_FIELDS | {
    "approval",
    "approvals",
    "approval_id",
    "approval_scope",
    "authority",
    "candidate_body",
    "body",
    "proposal",
    "decision_id",
    "decision_ref",
    "grant_id",
    "statement_ref",
    "expires_at",
    "approved_at",
    "requires_human",
    "impact",
    "reason_code",
    "reviewer",
}
_COGNITION_READ_METHODS = frozenset({"cognition.candidates.list"})
_COGNITION_METHODS = frozenset(
    {*_COGNITION_READ_METHODS, "cognition.candidate.decide"}
)
_CONVERSATION_AUTHORITY_FIELDS = AUTHORITY_FIELDS | {"destination_id"}
_TURN_REPLAY_LIMIT = 1024
_CONFIRM_KEYS = frozenset({"session_id", "destination_id"})
_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789._:-"
)
_MAX_SEEN_IDS = 50_000
_ACTIVE_JOIN_SECONDS = 10.0


class _Stop(Exception):
    """The reader must stop (invalid frame boundary or a closed stdout)."""


class _RuntimeWorker:
    """Create and use the SQLite-backed runtime on exactly one thread.

    ``sqlite3`` connections are thread-affine; keeping the store on a dedicated
    worker also lets the reader answer ``BUSY`` while a consult is running.
    """

    def __init__(
        self,
        config: HostConfig,
        *,
        event_sink: Any = None,
        cancel_requested: Any = None,
    ) -> None:
        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._ready = threading.Event()
        self.runtime: DesktopRuntime | None = None
        self.error: RuntimeStartupError | None = None
        self._event_sink = event_sink
        self._cancel_requested = cancel_requested
        self._thread = threading.Thread(
            target=self._run, args=(config,), name="pcs-desktop-db", daemon=True
        )
        self._thread.start()
        self._ready.wait()
        if self.runtime is None and self.error is None:
            raise RuntimeStartupError(
                "RUNTIME_UNAVAILABLE", "the trusted runtime did not start"
            )

    def _run(self, config: HostConfig) -> None:
        try:
            runtime = DesktopRuntime(
                config,
                event_sink=self._event_sink,
                cancel_requested=self._cancel_requested,
            )
        except RuntimeStartupError as exc:
            self.error = exc
            self._ready.set()
            return
        except BaseException as exc:  # noqa: BLE001 - reported to the reader
            self.error = RuntimeStartupError(
                "RUNTIME_UNAVAILABLE", f"the trusted runtime failed ({type(exc).__name__})"
            )
            self._ready.set()
            return
        self.runtime = runtime
        self._ready.set()
        try:
            while True:
                job = self._queue.get()
                if job is None:
                    return
                try:
                    job()
                except BaseException:  # noqa: BLE001 - a job must not kill the DB thread
                    LOGGER.exception("consult job failed outside its handler")
        finally:
            runtime.close()

    def submit(self, job: Callable[[], None]) -> None:
        self._queue.put(job)

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=_ACTIVE_JOIN_SECONDS)


#: RG05-F01: one external trigger may continue the bounded sweep a few times.
#: The sweep parameters stay RG04-sized; only the number of follow-up slices is
#: bounded, so an idle app can finish the normal user+final pair without a loop.
_POSTPROCESS_FOLLOWUP_LIMIT = 3
_POSTPROCESS_FOLLOWUP_DELAY_SECONDS = 1.0
_POSTPROCESS_FOLLOWUP_JITTER_SECONDS = 0.25
_POSTPROCESS_FOLLOWUP_MAX_DELAY_SECONDS = 3600.0


@dataclass(frozen=True)
class _SweepFollowUp:
    delay_seconds: float
    depth: int


def _seconds_until(value: object, *, now: datetime | None = None) -> float | None:
    """Seconds from ``now`` to an RFC3339 outbox timestamp, or ``None``."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return (parsed - current).total_seconds()


def _next_postprocess_follow_up(
    report: dict[str, Any], *, depth: int, now: datetime | None = None
) -> _SweepFollowUp | None:
    """RG05-F01: decide the next bounded follow-up from the read-only backlog.

    ``depth`` is the number of follow-ups already scheduled for this external
    trigger.  A backlog with pending-but-not-due rows waits for the outbox's own
    ``next_retry_at``; a projection-capped slice retries once more even when no
    delivery is pending.
    """

    if depth >= _POSTPROCESS_FOLLOWUP_LIMIT:
        return None
    backlog = report.get("backlog")
    if not isinstance(backlog, dict):
        return None
    pending = max(0, int(backlog.get("pending") or 0))
    due = max(0, int(backlog.get("due") or 0))
    capped = bool(backlog.get("projection_capped"))
    if pending == 0 and not capped:
        return None
    delay = _POSTPROCESS_FOLLOWUP_DELAY_SECONDS
    if pending > 0 and due == 0:
        remaining = _seconds_until(backlog.get("next_due_at"), now=now)
        if remaining is None:
            return None
        delay = min(
            max(delay, remaining + _POSTPROCESS_FOLLOWUP_JITTER_SECONDS),
            _POSTPROCESS_FOLLOWUP_MAX_DELAY_SECONDS,
        )
    return _SweepFollowUp(delay_seconds=delay, depth=depth + 1)


class Bridge:
    """One sidecar process: session state, confirmation records and routing."""

    def __init__(
        self,
        config_path: str | None,
        stdin: Any,
        stdout: Any,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.started = time.monotonic()
        self.session_id = str(uuid.uuid4())
        self.config: HostConfig | None = None
        self.config_path = config_path
        self._settings_pending = False
        self.config_error: ConfigError | None = None
        self.worker: _RuntimeWorker | None = None
        self.runtime: DesktopRuntime | None = None
        self.lock: DataInstanceLock | None = None
        self.data_locked = False
        self._write_lock = threading.Lock()
        self._seen_lock = threading.Lock()
        self._seen_order: list[str] = []
        self._seen: set[str] = set()
        self._model_lock = threading.Lock()
        self._confirm_lock = threading.Lock()
        self._confirmations: dict[tuple[str, str], str] = {}
        self._turn_replay_lock = threading.Lock()
        self._turn_replay: dict[tuple[str, str], tuple] = {}
        self._turn_replay_order: list[tuple[str, str]] = []
        self._event_ring = TurnEventRing()
        self._cancel_lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._stopping = threading.Event()
        # RG04/Q6: a bounded postprocess sweep must yield to a foreground send.
        # The counter is incremented before the send job is queued and decremented
        # when that job finishes; the timer callback only submits a worker job.
        self._send_lock = threading.Lock()
        self._send_pending = 0
        self._sweep_lock = threading.Lock()
        self._sweep_timer: threading.Timer | None = None
        self._sweep_skips = 0
        self._sweep_followups = 0
        # RG08 R-E: one bounded cognition-review slice per terminal turn.  The
        # queue is bounded and in-process; durability is the maintenance job row
        # (with its scan_position), not this list.
        self._maintenance_lock = threading.Lock()
        self._maintenance_timer: threading.Timer | None = None
        self._maintenance_turns: list[str] = []
        #: A foreground-cancelled job is paused.  The bridge remembers its id
        #: and resumes it once, after the foreground goes idle; a restart leaves
        #: the paused job visible for the explicit ``maintenance.run`` entry.
        self._maintenance_resume: tuple[str, str] | None = None
        self._maintenance_resume_timer: threading.Timer | None = None

        if not config_path:
            self.config_error = ConfigError(
                "CONFIG_MISSING",
                "no private desktop host configuration was provided",
            )
        else:
            try:
                self.config = load_config(config_path)
            except ConfigError as exc:
                self.config_error = exc
            except BaseException as exc:  # noqa: BLE001 - a config bug is not a crash
                self.config_error = ConfigError(
                    "CONFIG_INVALID",
                    f"the host configuration could not be read ({type(exc).__name__})",
                )

        if self.config is not None:
            self._start_runtime(self.config)

    def _start_runtime(self, config: HostConfig) -> None:
        lock = DataInstanceLock(config.database_path)
        try:
            lock.acquire(owner=f"pcs-bridge:{os.getpid()}")
        except DataLocked:
            self.data_locked = True
            LOGGER.warning("data instance is locked by another process")
            return
        self.lock = lock
        try:
            self.worker = _RuntimeWorker(
                config,
                event_sink=self._ring_event,
                cancel_requested=self._cancel_requested,
            )
        except RuntimeStartupError as exc:
            LOGGER.error("runtime startup refused code=%s", exc.code)
            self.config_error = ConfigError(exc.code, exc.message)
            self._release_lock()
            return
        if self.worker.error is not None:
            LOGGER.error("runtime startup refused code=%s", self.worker.error.code)
            self.config_error = ConfigError(
                self.worker.error.code, self.worker.error.message
            )
            self.worker.stop()
            self.worker = None
            self._release_lock()
            return
        self.runtime = self.worker.runtime
        if self.runtime.memory_bridge_configured():
            self._schedule_postprocess_sweep(1.5)

    # ------------------------------------------------------- RG04 postprocess

    def _foreground_send_pending(self) -> bool:
        """Q6: is a foreground conversation send queued or executing?"""

        if self._model_lock.locked():
            return True
        with self._send_lock:
            return self._send_pending > 0

    def _mark_send_pending(self, delta: int) -> None:
        with self._send_lock:
            self._send_pending = max(0, self._send_pending + delta)

    def _schedule_postprocess_sweep(
        self, delay_seconds: float, *, follow_up: bool = False
    ) -> bool:
        """Submit one bounded sweep; a follow-up is part of a bounded chain."""

        if self._stopping.is_set() or self.worker is None or self.runtime is None:
            return False
        if not self.runtime.memory_bridge_configured():
            return False
        with self._sweep_lock:
            if follow_up:
                if self._sweep_followups >= _POSTPROCESS_FOLLOWUP_LIMIT:
                    return False
                self._sweep_followups += 1
            else:
                self._sweep_followups = 0
            if self._sweep_timer is not None:
                self._sweep_timer.cancel()
            timer = threading.Timer(
                float(delay_seconds), self._timer_postprocess_sweep
            )
            timer.daemon = True
            self._sweep_timer = timer
            timer.start()
            return True

    def _timer_postprocess_sweep(self) -> None:
        with self._sweep_lock:
            self._sweep_timer = None
        if self._stopping.is_set() or self.worker is None:
            return
        try:
            self.worker.submit(self._run_postprocess_sweep_job)
        except BaseException:  # noqa: BLE001 - a timer must not kill the process
            LOGGER.exception("postprocess sweep could not be queued")

    def _run_postprocess_sweep_job(self) -> None:
        """Worker-side sweep entry with the Q6 foreground gate and F01 chain."""

        if self._stopping.is_set() or self.runtime is None:
            return
        if self._foreground_send_pending():
            with self._sweep_lock:
                self._sweep_skips += 1
                self._sweep_followups = 0
            LOGGER.info("postprocess sweep deferred: foreground send pending")
            return
        try:
            report = self.runtime.postprocess_sweep()
        except BaseException:  # noqa: BLE001 - bounded failure keeps pending state
            with self._sweep_lock:
                self._sweep_followups = 0
            LOGGER.exception("postprocess sweep failed")
            return
        if not isinstance(report, dict):
            report = {}
        backlog = report.get("backlog")
        LOGGER.info(
            "postprocess sweep projected=%s turns=%s delivered=%s pending=%s "
            "needs_review=%s blocked=%s obsolete=%s backlog_pending=%s "
            "backlog_due=%s projection_capped=%s",
            report.get("projected_refs"),
            report.get("turns_projected"),
            report.get("delivered"),
            report.get("pending"),
            report.get("needs_review"),
            report.get("blocked"),
            report.get("obsolete"),
            (backlog or {}).get("pending"),
            (backlog or {}).get("due"),
            (backlog or {}).get("projection_capped"),
        )
        if self._stopping.is_set() or self._foreground_send_pending():
            with self._sweep_lock:
                self._sweep_followups = 0
            LOGGER.info("postprocess follow-up deferred: foreground send or shutdown")
            return
        with self._sweep_lock:
            depth = self._sweep_followups
        plan = _next_postprocess_follow_up(report, depth=depth)
        if plan is None:
            with self._sweep_lock:
                self._sweep_followups = 0
            LOGGER.info("postprocess idle chain complete")
            return
        if self._schedule_postprocess_sweep(plan.delay_seconds, follow_up=True):
            LOGGER.info(
                "postprocess follow-up scheduled delay=%.2f depth=%d "
                "pending=%s due=%s projection_capped=%s",
                plan.delay_seconds,
                plan.depth,
                (backlog or {}).get("pending"),
                (backlog or {}).get("due"),
                (backlog or {}).get("projection_capped"),
            )

    # ------------------------------------------------------- RG08 maintenance

    def _queue_maintenance_turn(self, turn_id: str) -> None:
        """Remember one terminal turn for the bounded review attempt."""

        if self.runtime is None or not self.runtime.maintenance_configured():
            return
        with self._maintenance_lock:
            if turn_id in self._maintenance_turns:
                self._maintenance_turns.remove(turn_id)
            if len(self._maintenance_turns) >= 4:
                self._maintenance_turns.pop(0)
            self._maintenance_turns.append(turn_id)

    def _schedule_maintenance_review(self, delay_seconds: float = 1.5) -> bool:
        """Submit one bounded review attempt; never a standing loop."""

        if (
            self._stopping.is_set()
            or self.worker is None
            or self.runtime is None
            or not self.runtime.maintenance_configured()
        ):
            return False
        with self._maintenance_lock:
            if self._maintenance_timer is not None:
                self._maintenance_timer.cancel()
            timer = threading.Timer(
                max(0.0, float(delay_seconds)), self._timer_maintenance_review
            )
            timer.daemon = True
            self._maintenance_timer = timer
            timer.start()
            return True

    def _timer_maintenance_review(self) -> None:
        with self._maintenance_lock:
            self._maintenance_timer = None
        if self._stopping.is_set() or self.worker is None:
            return
        try:
            self.worker.submit(self._run_maintenance_review_job)
        except BaseException:  # noqa: BLE001 - a timer must not kill the process
            LOGGER.exception("maintenance review could not be queued")

    def _request_maintenance_yield(self) -> bool:
        """Reader-thread signal; never touches the maintenance worker queue."""

        if self.runtime is None:
            return False
        try:
            requested = bool(
                self.runtime.request_maintenance_yield("foreground_send")
            )
        except Exception:  # noqa: BLE001 - a yield signal must not fail a send
            LOGGER.exception("maintenance yield signal failed")
            return False
        if requested:
            LOGGER.info("maintenance yield requested: foreground_send")
        return requested

    def _schedule_maintenance_resume(self, delay_seconds: float = 3.0) -> bool:
        """One bounded resume attempt after the foreground goes idle."""

        if (
            self._stopping.is_set()
            or self.worker is None
            or self.runtime is None
            or not self.runtime.maintenance_configured()
        ):
            return False
        with self._maintenance_lock:
            if self._maintenance_resume is None:
                return False
            if self._maintenance_resume_timer is not None:
                self._maintenance_resume_timer.cancel()
            timer = threading.Timer(
                max(0.0, float(delay_seconds)), self._timer_maintenance_resume
            )
            timer.daemon = True
            self._maintenance_resume_timer = timer
            timer.start()
            return True

    def _timer_maintenance_resume(self) -> None:
        with self._maintenance_lock:
            self._maintenance_resume_timer = None
            pending = self._maintenance_resume
        if pending is None or self._stopping.is_set() or self.worker is None:
            return
        try:
            self.worker.submit(
                lambda: self._run_maintenance_review_job(resume=pending)
            )
        except BaseException:  # noqa: BLE001 - a timer must not kill the process
            LOGGER.exception("maintenance resume could not be queued")

    def _run_maintenance_review_job(
        self, resume: tuple[str, str] | None = None
    ) -> None:
        if self._stopping.is_set() or self.runtime is None:
            return
        if self._foreground_send_pending():
            # Q6: never queue behind a live foreground send.  A resume stays
            # pending; the next terminal turn offers it again.
            LOGGER.info("maintenance review deferred: foreground send pending")
            return
        with self._maintenance_lock:
            if resume is None:
                turn_id = (
                    self._maintenance_turns[0] if self._maintenance_turns else None
                )
                task_id = None
            else:
                task_id, turn_id = resume
        if not turn_id:
            return
        result: dict[str, Any] | None = None
        try:
            result = self.runtime.maintenance_after_turn(
                turn_id,
                should_yield=self._foreground_send_pending,
                resume_task_id=task_id,
                allow_repair=False,
            )
        except BaseException:  # noqa: BLE001 - a bounded failure is a result
            LOGGER.exception("maintenance review failed")
        result = result or {}
        with self._maintenance_lock:
            if (
                resume is None
                and self._maintenance_turns
                and self._maintenance_turns[0] == turn_id
            ):
                self._maintenance_turns.pop(0)
            more = bool(self._maintenance_turns)
            stop_reason = str(result.get("stop_reason") or "")
            result_task_id = result.get("task_id") or task_id
            if stop_reason in (
                "foreground_cancel",
                "foreground_yield",
                "transport_draining",
            ) and result_task_id:
                self._maintenance_resume = (str(result_task_id), turn_id)
            elif resume is not None:
                self._maintenance_resume = None
        LOGGER.info(
            "maintenance review turn=%s task=%s status=%s state=%s stop_reason=%s",
            turn_id,
            result.get("task_id") or task_id,
            result.get("status"),
            result.get("state"),
            result.get("stop_reason"),
        )
        if self._maintenance_resume is not None and not self._stopping.is_set():
            delay = 1.0 if result.get("status") == "draining" else 3.0
            self._schedule_maintenance_resume(delay)
        if more and not self._stopping.is_set() and not self._foreground_send_pending():
            self._schedule_maintenance_review(0.8)

    # ----------------------------------------------------------------- serve

    def serve(self) -> int:
        LOGGER.info(
            "bridge ready app=%s version=%s protocol=%s pid=%s configured=%s "
            "data_locked=%s ready=%s session_id=%s",
            APP_NAME,
            APP_VERSION,
            PROTOCOL_VERSION,
            os.getpid(),
            self.config is not None,
            self.data_locked,
            self._ready(),
            self.session_id,
        )
        try:
            while not self._stopping.is_set():
                try:
                    body = framing.read_frame(self.stdin)
                except framing.InvalidLength as exc:
                    LOGGER.error("invalid frame length: %s", type(exc).__name__)
                    return 2
                except framing.TruncatedFrame as exc:
                    LOGGER.error("truncated frame at EOF: %s", type(exc).__name__)
                    return 2
                if body is None:
                    break
                try:
                    document = framing.decode_json(body)
                except framing.InvalidPayload as exc:
                    LOGGER.error("invalid frame payload: %s", type(exc).__name__)
                    self._send_error(None, ERROR_INVALID_JSON, "frame is not valid JSON")
                    continue
                try:
                    self._dispatch(document)
                except _Stop:
                    raise
                except BaseException as exc:  # noqa: BLE001 - keep the reader alive
                    LOGGER.error("dispatch failed class=%s", type(exc).__name__)
                    self._send_error(None, ERROR_INTERNAL, "request dispatch failed")
        except _Stop:
            return 2
        finally:
            self._stopping.set()
            with self._sweep_lock:
                if self._sweep_timer is not None:
                    self._sweep_timer.cancel()
                    self._sweep_timer = None
            if self.worker is not None:
                self.worker.stop()
            self._release_lock()
            LOGGER.info("bridge stopping")
        return 0

    def _release_lock(self) -> None:
        if self.lock is not None:
            self.lock.release()
            self.lock = None

    # --------------------------------------------------------------- dispatch

    def _dispatch(self, document: Any) -> None:
        if not isinstance(document, dict):
            self._send_error(None, ERROR_INVALID_REQUEST, "request must be a JSON object")
            return
        request_id = document.get("id")
        if not _valid_id(request_id):
            self._send_error(None, ERROR_INVALID_REQUEST, "request id is required")
            return
        if document.get("v") != PROTOCOL_VERSION:
            self._send_error(
                request_id,
                ERROR_INVALID_REQUEST,
                f"v must be {PROTOCOL_VERSION!r}",
            )
            return
        if not self._claim_id(request_id):
            self._send_error(
                request_id,
                ERROR_DUPLICATE_REQUEST_ID,
                "request id was already used by this sidecar process",
            )
            return
        method = document.get("method")
        if not isinstance(method, str):
            self._send_error(request_id, ERROR_INVALID_REQUEST, "method is required")
            return
        params = document.get("params", {})
        if not isinstance(params, dict):
            self._send_error(request_id, ERROR_INVALID_PARAMS, "params must be an object")
            return

        if method in RESERVED_METHODS:
            self._send_error(
                request_id,
                ERROR_RESERVED,
                f"{method} is reserved and not implemented in this desktop build",
            )
            return
        if method not in IMPLEMENTED_METHODS:
            self._send_error(
                request_id,
                ERROR_METHOD_NOT_FOUND,
                "method is not part of desktop.v1",
            )
            return
        if method == "app.info":
            self._send_ok(request_id, self._app_info())
            return
        if method == "bridge.ping":
            self._send_ok(request_id, self._ping())
            return
        if method == "settings.reload":
            if params or self.worker is None or self._settings_pending:
                self._send_error(request_id, ERROR_BUSY, "settings update unavailable")
                return
            self._settings_pending = True
            self.worker.submit(lambda: self._reload_settings(request_id))
            return
        if self._settings_pending and method not in {"turn.cancel", "turn.events", "turn.result", "profile.status"}:
            self._send_error(request_id, ERROR_BUSY, "settings will apply after the running turn")
            return
        if method == "app.confirm_destination":
            self._dispatch_confirm(request_id, params)
            return
        if method in ("memory.bridge.sync", "memory.bridge.deliver"):
            self._dispatch_memory_bridge(request_id, method, params)
            return
        if method == "memory.search":
            self._dispatch_memory_search(request_id, params)
            return
        if method in MAINTENANCE_METHODS:
            self._dispatch_maintenance(request_id, method, params)
            return
        if method in REVIEW_METHODS:
            self._dispatch_review(request_id, method, params)
            return
        if method in _COGNITION_METHODS:
            self._dispatch_cognition(request_id, method, params)
            return
        if method == MODEL_TEST_METHOD:
            self._dispatch_model_test(request_id, params)
            return
        if method == "conversation.create":
            self._dispatch_conversation_create(request_id, params)
            return
        if method == "conversation.list":
            self._dispatch_conversation_list(request_id, params)
            return
        if method == "conversation.get":
            self._dispatch_conversation_get(request_id, params)
            return
        if method == "conversation.send":
            self._dispatch_conversation_send(request_id, params)
            return
        if method == "conversation.search":
            self._dispatch_conversation_search(request_id, params)
            return
        if method == "turn.result":
            self._dispatch_turn_result(request_id, params)
            return
        if method == "turn.events":
            self._dispatch_turn_events(request_id, params)
            return
        if method == "turn.steps":
            self._dispatch_turn_steps(request_id, params)
            return
        if method == "turn.trace":
            self._dispatch_turn_trace(request_id, params)
            return
        if method == "conversation.usage":
            self._dispatch_conversation_usage(request_id, params)
            return
        if method == "turn.cancel":
            self._dispatch_turn_cancel(request_id, params)
            return
        if method == "postprocess.status":
            self._dispatch_postprocess_status(request_id, params)
            return
        if method == "postprocess.retry":
            self._dispatch_postprocess_retry(request_id, params)
            return
        if method == "profile.status":
            self._dispatch_profile_status(request_id, params)
            return
        if method in (
            "profile.list",
            "profile.read_body",
            "profile.import.list",
            "profile.import.preview",
        ):
            self._dispatch_profile_admin(request_id, method, params)
            return
        if method == "profile.publish_reviewed":
            self._dispatch_profile_publish_reviewed(request_id, params)
            return
        if method in ("preference.list", "preference.revoke"):
            self._dispatch_preferences(request_id, method, params)
            return
        if method in ("profile.read", "profile.publish", "profile.activate"):
            self._dispatch_profile(request_id, method, params)
            return
        self._dispatch_consult(request_id, params)

    # ------------------------------------------------------------------- info

    def _reload_settings(self, request_id: str) -> None:
        """Runs on the DB worker after the current job; never restarts a turn."""
        try:
            config = load_config(self.config_path)
            self.runtime.apply_generation_settings(config)
            self.config = config
            self._send_ok(request_id, {"applied": True, "effect": "next_turn"})
        except (ConfigError, RuntimeStartupError):
            self._send_error(request_id, "SETTINGS_INVALID", "settings were not applied")
        except Exception:
            self._send_error(request_id, ERROR_INTERNAL, "settings could not be applied")
        finally:
            self._settings_pending = False

    def _ready(self) -> bool:
        return self.runtime is not None and self.runtime.ready

    def _reason(self) -> tuple[str | None, str | None]:
        if self.data_locked:
            return (
                ERROR_DATA_LOCKED,
                "another process owns this data instance; this sidecar did not open it",
            )
        if self.config is None:
            if self.config_error is not None:
                return self.config_error.code, self.config_error.message
            return "CONFIG_MISSING", "no private desktop host configuration"
        if self.runtime is None:
            if self.config_error is not None:
                return self.config_error.code, self.config_error.message
            return "RUNTIME_UNAVAILABLE", "the trusted runtime is not available"
        if not self.runtime.ready:
            return self.runtime.reason_code, self.runtime.reason_detail
        return None, None

    def _app_info(self) -> dict[str, Any]:
        ready = self._ready()
        reason_code, reason_detail = self._reason()
        config = self.config
        authority = config.authority if config is not None else None
        review_caps = self._review_capabilities()
        review_reason = None
        if not review_caps["read"]:
            if self.runtime is not None:
                review_reason = self.runtime.review_workspace_reason()
            else:
                review_reason = reason_code or "HOST_NOT_READY"
        return {
            "app": APP_NAME,
            "version": APP_VERSION,
            "protocol": PROTOCOL_VERSION,
            "mode": config.profile if config is not None else "unconfigured",
            "label": config.label if config is not None else None,
            "ai_display_name": (
                config.ai_display_name if config is not None else None
            ),
            "session_id": self.session_id,
            "host": {
                "namespace": authority.namespace if authority else None,
                "host_instance": authority.host_instance if authority else None,
                "operator": authority.operator if authority else None,
                "retention": authority.retention if authority else None,
                "input_privacy_class": (
                    authority.input_privacy_class if authority else None
                ),
                "policy_ref": authority.policy_ref if authority else None,
                "ready": ready,
                "reason_code": reason_code,
                "reason_detail": reason_detail,
            },
            "destination": (
                config.destination.to_public_dict() if config is not None else None
            ),
            "workspace": {
                "consult": ready,
                "conversation": ready,
                "review": review_caps["read"],
                "review_reason": review_reason,
            },
            "capabilities": {
                "consult": ready,
                "conversation": ready,
                "confirm_destination": ready,
                "input_registration": ready,
                "process": ready,
                "cancel": ready,
                "review": review_caps,
                "persona": self._persona_capabilities(),
                "memory": {
                    "configured": bool(
                        self.runtime is not None
                        and self.runtime.memory_bridge_configured()
                    ),
                    "search": bool(
                        self.runtime is not None
                        and self.runtime.memory_search_configured()
                    ),
                    "recall": self._memory_recall_capabilities(),
                },
                "cognition": self._cognition_capabilities(),
                "expression": self._expression_capabilities(),
                "preferences": self._preference_capabilities(),
                "web": self._web_capabilities(),
            },
            "persona": self._persona_info(),
            "deletion_safety": self._deletion_safety(),
            "limits": {
                "frame_bytes": framing.MAX_FRAME_BYTES,
                "request_timeout_seconds": 120,
                "max_question_chars": (
                    config.max_question_chars if config is not None else MAX_QUESTION_CHARS
                ),
            },
            "methods": sorted(IMPLEMENTED_METHODS),
            "reserved": sorted(RESERVED_METHODS),
            "pcs": {
                "package": "quintessence_pcs",
                "version": _pcs_version(),
                "schema_migrations": (
                    self.runtime.migrations_version if self.runtime is not None else 0
                ),
                "facade": "ConsultFacade + ReviewFacade",
            },
        }

    def _deletion_safety(self) -> str | None:
        """Whether the deletion ledger is actually enforcing PS06 here.

        ``enforced`` means a ledger backs the restore gate; ``not_configured``
        means no usable ledger was configured and the default gate is closed.
        Reporting it separately keeps "opened" from looking like "verified".
        """

        store = getattr(self.runtime, "store", None) if self.runtime is not None else None
        decision = getattr(store, "restore_decision", None) if store is not None else None
        return getattr(decision, "deletion_safety", None)

    def _persona_info(self) -> dict[str, Any]:
        """The persona block of ``app.info`` (summary only, never the body)."""

        runtime = self.runtime
        config = self.config
        reason_code, _detail = self._reason()
        if runtime is not None and config is not None:
            info = runtime.persona_status()
            if not runtime.ready:
                info["reason_code"] = reason_code
            return info
        expected = None if config is None else config.persona_expected
        speaker = None if config is None else config.speaker
        return {
            "state": "not_ready",
            "source": None,
            "profile_id": None,
            "profile_version": None,
            "record_id": None,
            "record_version": None,
            "asset_hash": None,
            "expected": None if expected is None else expected.to_dict(),
            "matches_expected": None if expected is None else False,
            "speaker": None if speaker is None else speaker.to_dict(),
            "reason_code": reason_code,
        }

    def _persona_capabilities(self) -> dict[str, bool]:
        runtime = self.runtime
        if runtime is None:
            return {
                "read": False,
                "publish": False,
                "activate": False,
                "read_body": False,
                "import": False,
            }
        return runtime.persona_capabilities()

    def _preference_capabilities(self) -> dict[str, bool]:
        runtime = self.runtime
        if runtime is None:
            return {"read": False, "revoke": False}
        return runtime.preference_capabilities()

    def _memory_recall_capabilities(self) -> dict[str, Any]:
        """RG08 R-B: the configured automatic-recall surface, never a secret."""

        config = self.config
        if config is None:
            return {
                "enabled": False,
                "configured": False,
                "declared": False,
                "bridge_configured": False,
                "max_items": 4,
                "budget_units": 2048,
                "timeout_seconds": 2.0,
                "timezone": "Asia/Shanghai",
                "source": None,
                "missing": ["config"],
                "entry": None,
                "apply": "next_turn",
            }
        adapter_present = bool(
            self.runtime is not None and self.runtime.memory_search_configured()
        )
        entry_compat = (
            self.runtime.memory_entry_compatibility()
            if self.runtime is not None
            else None
        )
        return memory_recall_status(
            config,
            adapter_present=adapter_present,
            entry_compat=entry_compat,
        )

    def _web_capabilities(self) -> dict[str, Any]:
        """RG08: the configured web status, readable even before the host is ready.

        It is derived from the validated host configuration and the presence of
        the named environment variable only; the credential value never enters
        ``app.info``.  An unreadable configuration reports what is missing
        instead of pretending the surface is on.
        """

        config = self.config
        if config is None:
            return {
                "enabled": False,
                "configured": False,
                "declared": False,
                "search": False,
                "fetch": False,
                "credential_present": False,
                "credential_env": None,
                "allowed_hosts": 0,
                "host_policy": None,
                "source": None,
                "missing": ["config"],
            }
        return web_status(config)

    def _dispatch_profile_status(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        """Reader-thread persona status (in-memory; never queues behind a turn)."""

        poisoned = sorted(AUTHORITY_FIELDS.intersection(params))
        if poisoned:
            self._send_error(
                request_id,
                ERROR_AUTHORITY_FIELD,
                "authority and destination fields are host-owned",
            )
            return
        unknown = sorted(set(params) - _PROFILE_STATUS_KEYS)
        if unknown:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "profile.status accepts only session_id"
            )
            return
        if not _valid_id(params.get("session_id")):
            self._send_error(request_id, ERROR_INVALID_PARAMS, "session_id is required")
            return
        self._send_ok(request_id, self._persona_info())

    def _dispatch_profile(
        self, request_id: str, method: str, params: dict[str, Any]
    ) -> None:
        """RG06 host-controlled profile read/publish/activate (worker thread)."""

        allowed = {
            "profile.read": _PROFILE_READ_KEYS,
            "profile.publish": _PROFILE_PUBLISH_KEYS,
            "profile.activate": _PROFILE_ACTIVATE_KEYS,
        }[method]
        poisoned = sorted(AUTHORITY_FIELDS.intersection(params))
        if poisoned:
            self._send_error(
                request_id,
                ERROR_AUTHORITY_FIELD,
                "authority and destination fields are host-owned",
            )
            return
        unknown = sorted(set(params) - allowed)
        if unknown:
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                f"{method} accepts only {', '.join(sorted(allowed))}",
            )
            return
        if not _valid_id(params.get("session_id")):
            self._send_error(request_id, ERROR_INVALID_PARAMS, "session_id is required")
            return
        if method == "profile.publish":
            if not _valid_id(params.get("profile_id")):
                self._send_error(
                    request_id, ERROR_INVALID_PARAMS, "profile_id is required"
                )
                return
            body = params.get("body")
            if not isinstance(body, dict):
                self._send_error(
                    request_id, ERROR_INVALID_PARAMS, "body must be a JSON object"
                )
                return
            review_event_id = params.get("review_event_id")
            if (
                not isinstance(review_event_id, str)
                or not 1 <= len(review_event_id) <= 128
                or any(char in review_event_id for char in "\x00\r\n\t")
            ):
                self._send_error(
                    request_id,
                    ERROR_INVALID_PARAMS,
                    "review_event_id must be 1..128 printable characters",
                )
                return
        elif method == "profile.activate":
            if not _valid_id(params.get("profile_id")):
                self._send_error(
                    request_id, ERROR_INVALID_PARAMS, "profile_id is required"
                )
                return
            record_version = params.get("record_version")
            if (
                type(record_version) is not int
                or not 1 <= record_version <= 1_000_000
            ):
                self._send_error(
                    request_id,
                    ERROR_INVALID_PARAMS,
                    "record_version must be a positive integer",
                )
                return
        if self.data_locked:
            self._send_error(
                request_id,
                ERROR_DATA_LOCKED,
                "another process owns this data instance; no profile action ran",
            )
            return
        if self.config is None or self.runtime is None:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        worker = self.worker
        if worker is None:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        frozen = dict(params)
        worker.submit(lambda: self._run_profile(request_id, method, frozen))

    def _run_profile(
        self, request_id: str, method: str, params: dict[str, Any]
    ) -> None:
        """The worker-thread body of ``profile.read/publish/activate``."""

        runtime = self.runtime
        if runtime is None:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        try:
            if method == "profile.read":
                result = runtime.profile_read()
            elif method == "profile.publish":
                result = runtime.profile_publish(
                    profile_id=params["profile_id"],
                    body=params["body"],
                    review_event_id=params["review_event_id"],
                )
            else:
                result = runtime.profile_activate(
                    profile_id=params["profile_id"],
                    record_version=int(params["record_version"]),
                )
        except PersonaControlError as exc:
            self._send_error(request_id, exc.code, exc.message)
        except SourceUntrusted as exc:
            self._send_error(
                request_id,
                "SOURCE_UNTRUSTED",
                "the review reference is not a trusted user approval",
            )
        except (RecordNotFound, EventNotFound):
            self._send_error(
                request_id, ERROR_NOT_FOUND, "the profile version does not exist"
            )
        except Redacted:
            self._send_error(
                request_id, ERROR_REDACTED, "the profile is behind a deletion barrier"
            )
        except InvalidInput:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "the profile request was not accepted"
            )
        except (RecordVersionConflict, StateRevisionConflict):
            self._send_error(
                request_id,
                ERROR_REVISION_CONFLICT,
                "the target version changed; read the authoritative state again",
            )
        except DbBusy:
            self._send_error(request_id, ERROR_DB_BUSY, "the host database is busy")
        except CorruptStore:
            self._send_error(
                request_id,
                ERROR_INTERNAL,
                "the host internal persona state is inconsistent",
            )
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the UI
            LOGGER.error("profile action failed class=%s", type(exc).__name__)
            self._send_error(
                request_id, ERROR_INTERNAL, "the profile action failed in the host"
            )
        else:
            self._send_ok(request_id, result)

    def _dispatch_profile_admin(
        self, request_id: str, method: str, params: dict[str, Any]
    ) -> None:
        """UI-F02 bounded profile/import reads (worker thread, never a write).

        These calls read the store, so they obey the same R2 rule as the
        conversation reads: while a turn is running they answer ``BUSY``
        instead of queueing behind it.  ``profile.read_body`` additionally
        writes one audit row; it is still a manager-invoked read.
        """

        allowed = {
            "profile.list": _PROFILE_LIST_KEYS,
            "profile.read_body": _PROFILE_READ_BODY_KEYS,
            "profile.import.list": _PROFILE_IMPORT_LIST_KEYS,
            "profile.import.preview": _PROFILE_IMPORT_PREVIEW_KEYS,
        }[method]
        poisoned = sorted(AUTHORITY_FIELDS.intersection(params))
        if poisoned:
            self._send_error(
                request_id,
                ERROR_AUTHORITY_FIELD,
                "authority and destination fields are host-owned",
            )
            return
        unknown = sorted(set(params) - allowed)
        if unknown:
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                f"{method} accepts only {', '.join(sorted(allowed))}",
            )
            return
        if not _valid_id(params.get("session_id")):
            self._send_error(request_id, ERROR_INVALID_PARAMS, "session_id is required")
            return
        if method == "profile.read_body":
            if not _valid_id(params.get("record_id")):
                self._send_error(
                    request_id, ERROR_INVALID_PARAMS, "record_id is required"
                )
                return
            if not _valid_version(params.get("record_version")):
                self._send_error(
                    request_id,
                    ERROR_INVALID_PARAMS,
                    "record_version must be a positive integer",
                )
                return
        elif method == "profile.import.preview":
            if not valid_persona_import_name(params.get("name")):
                self._send_error(
                    request_id,
                    ERROR_INVALID_PARAMS,
                    "name must be a bare *.json file name",
                )
                return
        if self.data_locked:
            self._send_error(
                request_id,
                ERROR_DATA_LOCKED,
                "another process owns this data instance; no profile read ran",
            )
            return
        if self.config is None or self.runtime is None or not self.runtime.ready:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        if params.get("session_id") != self.session_id:
            self._send_error(
                request_id,
                ERROR_SESSION_MISMATCH,
                "the request belongs to a different host session",
            )
            return
        if self.worker is None:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        if not self._require_turn_slot(request_id, method):
            return
        frozen = dict(params)
        self.worker.submit(
            lambda: self._run_profile_admin(request_id, method, frozen)
        )

    def _run_profile_admin(
        self, request_id: str, method: str, params: dict[str, Any]
    ) -> None:
        runtime = self.runtime
        if runtime is None:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        try:
            if method == "profile.list":
                result = runtime.profile_list()
            elif method == "profile.read_body":
                result = runtime.profile_read_body(
                    record_id=params["record_id"],
                    record_version=int(params["record_version"]),
                )
            elif method == "profile.import.list":
                result = runtime.profile_import_list()
            else:
                result = runtime.profile_import_preview(name=params["name"])
        except PersonaControlError as exc:
            self._send_error(request_id, exc.code, exc.message)
        except (RecordNotFound, EventNotFound):
            self._send_error(
                request_id, ERROR_NOT_FOUND, "the profile version does not exist"
            )
        except Redacted:
            self._send_error(
                request_id, ERROR_REDACTED, "the profile is behind a deletion barrier"
            )
        except InvalidInput:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "the profile request was not accepted"
            )
        except DbBusy:
            self._send_error(request_id, ERROR_DB_BUSY, "the host database is busy")
        except CorruptStore:
            self._send_error(
                request_id,
                ERROR_INTERNAL,
                "the host internal persona state is inconsistent",
            )
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            LOGGER.error("profile read failed class=%s", type(exc).__name__)
            self._send_error(
                request_id, ERROR_INTERNAL, "the profile read failed in the host"
            )
        else:
            self._send_ok(request_id, result)

    def _dispatch_profile_publish_reviewed(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        """The reviewed publish path: the host writes the approval event."""

        poisoned = sorted(AUTHORITY_FIELDS.intersection(params))
        if poisoned:
            self._send_error(
                request_id,
                ERROR_AUTHORITY_FIELD,
                "authority and destination fields are host-owned",
            )
            return
        unknown = sorted(set(params) - _PROFILE_PUBLISH_REVIEWED_KEYS)
        if unknown:
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "profile.publish_reviewed accepts only session_id, import_id, "
                "profile_id and confirm",
            )
            return
        if not _valid_id(params.get("session_id")):
            self._send_error(request_id, ERROR_INVALID_PARAMS, "session_id is required")
            return
        if not _valid_id(params.get("import_id")):
            self._send_error(request_id, ERROR_INVALID_PARAMS, "import_id is required")
            return
        profile_id = params.get("profile_id")
        if profile_id is not None and not _valid_id(profile_id):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "profile_id must be a profile id"
            )
            return
        if params.get("confirm") is not True:
            self._send_error(
                request_id,
                ERROR_CONFIRMATION_REQUIRED,
                "publishing requires explicit confirmation",
            )
            return
        if self.data_locked:
            self._send_error(
                request_id,
                ERROR_DATA_LOCKED,
                "another process owns this data instance; nothing was published",
            )
            return
        if self.config is None or self.runtime is None or not self.runtime.ready:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        if params.get("session_id") != self.session_id:
            self._send_error(
                request_id,
                ERROR_SESSION_MISMATCH,
                "the request belongs to a different host session",
            )
            return
        if self.worker is None:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        frozen = dict(params)
        self.worker.submit(
            lambda: self._run_profile_publish_reviewed(request_id, frozen)
        )

    def _run_profile_publish_reviewed(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        runtime = self.runtime
        if runtime is None:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        try:
            result = runtime.profile_publish_reviewed(
                import_id=params["import_id"],
                profile_id=params.get("profile_id"),
                confirm=True,
            )
        except PersonaControlError as exc:
            self._send_error(request_id, exc.code, exc.message)
        except SourceUntrusted:
            self._send_error(
                request_id,
                "SOURCE_UNTRUSTED",
                "the review reference is not a trusted approval for this body",
            )
        except (RecordNotFound, EventNotFound):
            self._send_error(
                request_id, ERROR_NOT_FOUND, "the profile version does not exist"
            )
        except Redacted:
            self._send_error(
                request_id, ERROR_REDACTED, "the profile is behind a deletion barrier"
            )
        except InvalidInput:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "the profile request was not accepted"
            )
        except (RecordVersionConflict, StateRevisionConflict):
            self._send_error(
                request_id,
                ERROR_REVISION_CONFLICT,
                "the target version changed; read the authoritative state again",
            )
        except DbBusy:
            self._send_error(request_id, ERROR_DB_BUSY, "the host database is busy")
        except CorruptStore:
            self._send_error(
                request_id,
                ERROR_INTERNAL,
                "the host internal persona state is inconsistent",
            )
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            LOGGER.error("profile publish failed class=%s", type(exc).__name__)
            self._send_error(
                request_id,
                ERROR_INTERNAL,
                "the profile publish failed in the host",
            )
        else:
            self._send_ok(request_id, result)

    def _dispatch_preferences(
        self, request_id: str, method: str, params: dict[str, Any]
    ) -> None:
        """UI-F02 preference view/revoke; only the revoke writes, via the service."""

        read = method == "preference.list"
        allowed = _PREFERENCE_LIST_KEYS if read else _PREFERENCE_REVOKE_KEYS
        poisoned = sorted(AUTHORITY_FIELDS.intersection(params))
        if poisoned:
            self._send_error(
                request_id,
                ERROR_AUTHORITY_FIELD,
                "authority and destination fields are host-owned",
            )
            return
        unknown = sorted(set(params) - allowed)
        if unknown:
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                f"{method} accepts only {', '.join(sorted(allowed))}",
            )
            return
        if not _valid_id(params.get("session_id")):
            self._send_error(request_id, ERROR_INVALID_PARAMS, "session_id is required")
            return
        conversation_id = params.get("conversation_id")
        if conversation_id is not None and not _valid_id(conversation_id):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "conversation_id must be a UUID"
            )
            return
        if not read:
            if not _valid_id(conversation_id):
                self._send_error(
                    request_id, ERROR_INVALID_PARAMS, "conversation_id is required"
                )
                return
            if params.get("dimension") not in ("length", "format", "tone"):
                self._send_error(
                    request_id,
                    ERROR_INVALID_PARAMS,
                    "dimension must be length, format or tone",
                )
                return
            reason = params.get("reason")
            if reason is not None and (
                not isinstance(reason, str)
                or len(reason) > 512
                or any(char in reason for char in "\x00\r\n\t")
            ):
                self._send_error(
                    request_id,
                    ERROR_INVALID_PARAMS,
                    "reason must be at most 512 printable characters",
                )
                return
        if self.data_locked:
            self._send_error(
                request_id,
                ERROR_DATA_LOCKED,
                "another process owns this data instance; no preference action ran",
            )
            return
        if self.config is None or self.runtime is None or not self.runtime.ready:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        if params.get("session_id") != self.session_id:
            self._send_error(
                request_id,
                ERROR_SESSION_MISMATCH,
                "the request belongs to a different host session",
            )
            return
        if self.worker is None:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        if read and not self._require_turn_slot(request_id, method):
            return
        frozen = dict(params)
        self.worker.submit(
            lambda: self._run_preferences(request_id, method, frozen)
        )

    def _run_preferences(
        self, request_id: str, method: str, params: dict[str, Any]
    ) -> None:
        runtime = self.runtime
        if runtime is None:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        try:
            if method == "preference.list":
                result = runtime.preference_list(
                    conversation_id=params.get("conversation_id")
                )
            else:
                result = runtime.preference_revoke(
                    conversation_id=params["conversation_id"],
                    dimension=params["dimension"],
                    reason=params.get("reason"),
                )
        except PersonaControlError as exc:
            self._send_error(request_id, exc.code, exc.message)
        except RecordNotFound:
            self._send_error(
                request_id, ERROR_NOT_FOUND, "the conversation does not exist"
            )
        except Redacted:
            self._send_error(
                request_id, ERROR_REDACTED, "the preference is behind a deletion barrier"
            )
        except InvalidInput:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "the preference request was not accepted"
            )
        except DbBusy:
            self._send_error(request_id, ERROR_DB_BUSY, "the host database is busy")
        except CorruptStore:
            self._send_error(
                request_id,
                ERROR_INTERNAL,
                "the host internal preference state is inconsistent",
            )
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            LOGGER.error("preference action failed class=%s", type(exc).__name__)
            self._send_error(
                request_id,
                ERROR_INTERNAL,
                "the preference action failed in the host",
            )
        else:
            self._send_ok(request_id, result)

    def _review_capabilities(self) -> dict[str, bool]:
        if self.runtime is None:
            return {
                "read": False,
                "generate": False,
                "approve": False,
                "reject": False,
                "result": False,
            }
        return self.runtime.review_capabilities()

    def _cognition_capabilities(self) -> dict[str, Any]:
        if self.runtime is None:
            return {
                "enabled": False,
                "configured": False,
                "propose": False,
                "pending_review": False,
                "auto_authorization": False,
                "working_set_limit": 0,
                "pending_limit": 0,
            }
        return self.runtime.cognition_capabilities()

    def _expression_capabilities(self) -> dict[str, Any]:
        """RG07: read-only mode truth; the UI card owns rendering and toggles."""

        if self.runtime is None:
            return {
                "mode": "off",
                "effective_mode": "off",
                "policy_version": "sdrpe.v1",
                "auto_available": False,
                "resolved_reason": None,
            }
        return self.runtime.expression_capabilities()

    def _ping(self) -> dict[str, Any]:
        return {
            "pong": True,
            "pid": os.getpid(),
            "protocol": PROTOCOL_VERSION,
            "uptime_ms": round((time.monotonic() - self.started) * 1000, 3),
            "frozen": bool(getattr(sys, "frozen", False)),
            "session_id": self.session_id,
            "data_locked": self.data_locked,
            "configured": self.config is not None,
            "ready": self._ready(),
            "busy": self._model_lock.locked(),
        }

    # ---------------------------------------------------------- confirmation

    def _dispatch_memory_bridge(
        self, request_id: str, method: str, params: dict[str, Any]
    ) -> None:
        if set(params) - {"limit"}:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "unsupported memory bridge parameter"
            )
            return
        limit = params.get("limit")
        if limit is not None and (
            type(limit) is not int or not 1 <= limit <= 100
        ):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "limit must be 1..100"
            )
            return
        if self.runtime is None or self.worker is None:
            self._send_error(
                request_id, ERROR_UNAVAILABLE, "the trusted runtime is unavailable"
            )
            return
        if self.data_locked:
            self._send_error(
                request_id,
                ERROR_DATA_LOCKED,
                "another process owns this data instance; no bridge job was started",
            )
            return
        self.worker.submit(
            lambda: self._run_memory_bridge(request_id, method, limit)
        )

    def _run_memory_bridge(
        self, request_id: str, method: str, limit: int | None
    ) -> None:
        assert self.runtime is not None
        try:
            if method == "memory.bridge.sync":
                result = self.runtime.memory_bridge_sync(limit=limit or 20)
            else:
                result = self.runtime.memory_bridge_deliver(limit=limit)
        except RuntimeStartupError as exc:
            self._send_error(request_id, exc.code, exc.message)
            return
        except Exception:  # noqa: BLE001 - bounded bridge state
            self._send_error(
                request_id, ERROR_UNAVAILABLE, "the memory bridge is unavailable"
            )
            return
        self._send_ok(request_id, result)

    def _dispatch_memory_search(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        """RG08: bounded read-only memory browse/search.

        Only ``session_id``, ``query``, ``limit`` and an opaque ``cursor`` are
        accepted; the host owns the scope, adapter executable, data directory
        and timeout.  An empty query browses the newest items instead of being
        rejected, so the sidebar can show real memory before the user types.
        """

        if not self._conversation_gate(
            request_id, params, _MEMORY_SEARCH_KEYS, "memory.search"
        ):
            return
        query = params.get("query", "")
        if not isinstance(query, str) or len(query) > 256:
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "query must be a string of at most 256 characters",
            )
            return
        limit = params.get("limit", 10)
        if type(limit) is not int or not 1 <= limit <= 20:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "limit must be 1..20"
            )
            return
        cursor = params.get("cursor")
        if cursor is not None and (
            not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048
        ):
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "cursor must be a cursor string or null",
            )
            return
        if not self._require_turn_slot(request_id, "memory.search"):
            return
        self.worker.submit(
            lambda: self._run_memory_search(request_id, query, limit, cursor)
        )

    def _run_memory_search(
        self, request_id: str, query: str, limit: int, cursor: str | None
    ) -> None:
        assert self.runtime is not None
        try:
            result = self.runtime.memory_search(
                query=query, limit=limit, cursor=cursor
            )
        except RuntimeStartupError as exc:
            self._send_error(request_id, exc.code, exc.message)
            return
        except Exception:  # noqa: BLE001 - bounded read state, never content
            self._send_error(
                request_id,
                "MEMORY_SEARCH_FAILED",
                "memory search did not complete",
            )
            return
        self._send_ok(request_id, result)

    # ------------------------------------------------------------- maintenance

    def _dispatch_maintenance(
        self, request_id: str, method: str, params: dict[str, Any]
    ) -> None:
        """Expose the bounded maintenance entry the host asks for explicitly.

        Nothing here schedules: a call runs one slice (or one state change) and
        returns.  The runtime still refuses when the host did not grant the
        ``maintenance`` permission, so the entry cannot be reached by accident.
        """

        allowed = {
            "maintenance.status": {"task_id"},
            "maintenance.run": {"event_ids", "task_id", "limit"},
            "maintenance.control": {"task_id", "action"},
        }[method]
        unknown = set(params) - allowed
        if unknown:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "unsupported maintenance parameter"
            )
            return
        if self.runtime is None or self.worker is None:
            self._send_error(
                request_id, ERROR_UNAVAILABLE, "the trusted runtime is unavailable"
            )
            return
        if self.data_locked:
            self._send_error(
                request_id,
                ERROR_DATA_LOCKED,
                "another process owns this data instance; no maintenance call was made",
            )
            return

        if method == "maintenance.status":
            task_id = params.get("task_id")
            if task_id is not None and not isinstance(task_id, str):
                self._send_error(
                    request_id, ERROR_INVALID_PARAMS, "task_id must be a string"
                )
                return
            if task_id is None:
                # Answer the service/active view on the reader thread; a DB
                # status call would otherwise queue behind the running model.
                try:
                    view = self.runtime.maintenance_status_fast()
                except RuntimeStartupError as exc:
                    self._send_error(request_id, exc.code, exc.message)
                    return
                view.setdefault("service", {})
                view["service"]["foreground_pending"] = (
                    self._foreground_send_pending()
                )
                view["service"]["resume_pending"] = bool(
                    self._maintenance_resume
                )
                self._send_ok(request_id, view)
                return
            self.worker.submit(
                lambda: self._run_maintenance(
                    request_id, lambda: self.runtime.maintenance_status(task_id=task_id)
                )
            )
            return

        if method == "maintenance.control":
            task_id = params.get("task_id")
            action = params.get("action")
            if not isinstance(task_id, str) or not task_id:
                self._send_error(
                    request_id, ERROR_INVALID_PARAMS, "task_id is required"
                )
                return
            if action not in ("pause", "resume", "cancel", "yield"):
                self._send_error(
                    request_id,
                    ERROR_INVALID_PARAMS,
                    "action must be pause, resume, cancel or yield",
                )
                return
            if action == "yield":
                # Immediate reader-thread signal; the queued job below would
                # be useless while the maintenance worker is blocked.
                requested = self.runtime.request_maintenance_yield("host_yield")
                view = self.runtime.maintenance_service_view()
                view["foreground_pending"] = self._foreground_send_pending()
                view["resume_pending"] = bool(self._maintenance_resume)
                self._send_ok(
                    request_id,
                    {
                        "task_id": task_id,
                        "yield_requested": requested,
                        "service": view,
                    },
                )
                return
            self.worker.submit(
                lambda: self._run_maintenance(
                    request_id,
                    lambda: self.runtime.maintenance_control(
                        task_id=task_id, action=action
                    ),
                )
            )
            return

        event_ids = params.get("event_ids", [])
        limit = params.get("limit")
        if not isinstance(event_ids, list) or any(
            not isinstance(item, str) or not item for item in event_ids
        ):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "event_ids must be a list of ids"
            )
            return
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
            self._send_error(request_id, ERROR_INVALID_PARAMS, "limit must be 1..100")
            return
        task_id = params.get("task_id")
        if task_id is not None and not isinstance(task_id, str):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "task_id must be a string"
            )
            return
        self.worker.submit(
            lambda: self._run_maintenance(
                request_id,
                lambda: self.runtime.maintenance_run(
                    event_ids=tuple(event_ids), task_id=task_id, limit=limit
                ),
            )
        )

    def _run_maintenance(self, request_id: str, call: Any) -> None:
        try:
            result = call()
        except RuntimeStartupError as exc:
            self._send_error(request_id, exc.code, exc.message)
            return
        except InvalidInput as exc:
            # A paused/leased/terminal job is a business-state refusal, not a
            # dead sidecar: the host must be able to tell the two apart and
            # offer resume/cancel instead of retrying the transport.
            self._send_error(
                request_id, ERROR_MAINTENANCE_STATE_INVALID, str(exc)
            )
            return
        except Exception:  # noqa: BLE001 - bounded maintenance state
            self._send_error(
                request_id, ERROR_UNAVAILABLE, "the maintenance entry is unavailable"
            )
            return
        self._send_ok(request_id, result)

    def _dispatch_confirm(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        poisoned = sorted(AUTHORITY_FIELDS.intersection(params))
        if poisoned:
            self._send_error(
                request_id,
                ERROR_AUTHORITY_FIELD,
                "authority and destination fields are host-owned",
            )
            return
        unknown = sorted(set(params) - _CONFIRM_KEYS)
        if unknown:
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "app.confirm_destination accepts only session_id and destination_id",
            )
            return
        session_id = params.get("session_id")
        destination_id = params.get("destination_id")
        if not _valid_id(session_id):
            self._send_error(request_id, ERROR_INVALID_PARAMS, "session_id is required")
            return
        if not isinstance(destination_id, str) or not destination_id:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "destination_id is required"
            )
            return
        if self.data_locked:
            self._send_error(request_id, ERROR_DATA_LOCKED, "the data instance is in use")
            return
        if self.config is None or self.runtime is None or not self.runtime.ready:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        if session_id != self.session_id:
            self._send_error(
                request_id,
                ERROR_SESSION_MISMATCH,
                "the confirmation belongs to a different host session",
            )
            return
        descriptor = self.config.destination
        if destination_id != descriptor.destination_id:
            self._send_error(
                request_id,
                ERROR_DESTINATION_MISMATCH,
                "the confirmation does not match the current host destination",
            )
            return
        with self._confirm_lock:
            self._confirmations[(self.session_id, destination_id)] = _now()
        self._send_ok(
            request_id,
            {
                "confirmed": True,
                "session_id": self.session_id,
                "destination_id": destination_id,
                "confirmation_required": descriptor.confirmation_required,
                "confirmed_at": _now(),
            },
        )

    def _require_confirmed_destination(
        self, request_id: str, confirmed: str | None
    ) -> bool:
        """The one destination-confirmation gate shared by both send paths."""

        assert self.config is not None
        descriptor = self.config.destination
        if descriptor.confirmation_required:
            if confirmed is None:
                self._send_error(
                    request_id,
                    ERROR_CONFIRMATION_REQUIRED,
                    "this external destination has not been confirmed in this session",
                )
                return False
            if confirmed != descriptor.destination_id:
                self._send_error(
                    request_id,
                    ERROR_CONFIRMATION_REQUIRED,
                    "the destination changed; confirm the current destination again",
                )
                return False
            with self._confirm_lock:
                recorded = self._confirmations.get(
                    (self.session_id, descriptor.destination_id)
                )
            if recorded is None:
                self._send_error(
                    request_id,
                    ERROR_CONFIRMATION_REQUIRED,
                    "no host confirmation record exists for this destination",
                )
                return False
        elif confirmed is not None and confirmed != descriptor.destination_id:
            self._send_error(
                request_id,
                ERROR_DESTINATION_MISMATCH,
                "the confirmed destination does not match the current destination",
            )
            return False
        return True

    # -------------------------------------------------------- conversations

    def _turn_slot_busy(self) -> bool:
        """R2: a DB read must never queue behind a running turn."""

        return self._model_lock.locked()

    def _require_turn_slot(self, request_id: str, method: str) -> bool:
        if self._turn_slot_busy():
            self._send_error(
                request_id,
                ERROR_BUSY,
                f"{method} is unavailable while a conversation turn is running",
            )
            return False
        return True

    def _dispatch_conversation_create(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        if not self._conversation_gate(
            request_id, params, _CONVERSATION_CREATE_KEYS, "conversation.create"
        ):
            return
        mode = params.get("mode", "chat")
        if mode not in ("chat", "work"):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "mode must be chat or work"
            )
            return
        if not self._require_turn_slot(request_id, "conversation.create"):
            return
        self.worker.submit(
            lambda: self._run_conversation_read(
                request_id,
                lambda: {
                    "conversation": conversation_dto.conversation_dict(
                        self.runtime.conversation_create(mode=mode)
                    )
                },
            )
        )

    def _dispatch_conversation_list(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        if not self._conversation_gate(
            request_id, params, _CONVERSATION_LIST_KEYS, "conversation.list"
        ):
            return
        limit = params.get("limit")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 50):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "limit must be 1..50"
            )
            return
        cursor = params.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "cursor must be a string"
            )
            return
        if not self._require_turn_slot(request_id, "conversation.list"):
            return
        self.worker.submit(
            lambda: self._run_conversation_read(
                request_id,
                lambda: conversation_dto.conversation_page_dict(
                    self.runtime.conversation_list(
                        limit=20 if limit is None else limit, cursor=cursor
                    )
                ),
            )
        )

    def _dispatch_conversation_get(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        if not self._conversation_gate(
            request_id, params, _CONVERSATION_GET_KEYS, "conversation.get"
        ):
            return
        conversation_id = params.get("conversation_id")
        if not _valid_id(conversation_id):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "conversation_id is required"
            )
            return
        before_turn = params.get("before_turn_no")
        before_seq = params.get("before_seq")
        if (before_turn is None) != (before_seq is None):
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "before_turn_no and before_seq must be supplied together",
            )
            return
        if before_turn is not None and (
            type(before_turn) is not int
            or type(before_seq) is not int
            or before_turn < 1
            or before_seq < 1
        ):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "before must be positive integers"
            )
            return
        limit = params.get("limit")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 200):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "limit must be 1..200"
            )
            return
        if not self._require_turn_slot(request_id, "conversation.get"):
            return
        before = (
            None if before_turn is None else (int(before_turn), int(before_seq))
        )
        self.worker.submit(
            lambda: self._run_conversation_read(
                request_id,
                lambda: self._conversation_get_payload(
                    conversation_id, before, 50 if limit is None else limit
                ),
            )
        )

    def _dispatch_turn_result(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        if not self._conversation_gate(
            request_id, params, _TURN_RESULT_KEYS, "turn.result"
        ):
            return
        turn_id = params.get("turn_id")
        if not _valid_id(turn_id):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "turn_id is required"
            )
            return
        if not self._require_turn_slot(request_id, "turn.result"):
            return
        self.worker.submit(
            lambda: self._run_conversation_read(
                request_id,
                lambda: conversation_dto.turn_result_dict(
                    self.runtime.conversation_turn_result(turn_id=turn_id)
                ),
            )
        )

    def _dispatch_turn_events(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        if not self._conversation_gate(
            request_id, params, _TURN_EVENTS_KEYS, "turn.events"
        ):
            return
        turn_id = params.get("turn_id")
        if not _valid_id(turn_id):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "turn_id is required"
            )
            return
        after_seq = params.get("after_seq", 0)
        limit = params.get("limit", 100)
        if after_seq is None:
            after_seq = 0
        if limit is None:
            limit = 100
        if (
            type(after_seq) is not int
            or not 0 <= after_seq <= 1_000_000
            or type(limit) is not int
            or not 1 <= limit <= 200
        ):
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "after_seq must be 0..1000000 and limit 1..200",
            )
            return
        if self._event_ring.has(turn_id):
            events, next_seq, terminal, truncated = self._event_ring.read(
                turn_id, after_seq=after_seq, limit=limit
            )
            payload: dict[str, Any] = {
                "events": events,
                "next_seq": next_seq,
                "terminal": terminal,
            }
            if truncated is not None:
                payload["truncated_from_seq"] = truncated
            self._send_ok(request_id, payload)
            return
        if self._turn_slot_busy():
            self._send_error(
                request_id,
                ERROR_BUSY,
                "turn.events is unavailable while a conversation turn is running",
            )
            return
        self.worker.submit(
            lambda: self._run_conversation_read(
                request_id,
                lambda: self.runtime.conversation_turn_events(
                    turn_id=turn_id, after_seq=after_seq, limit=limit
                ),
            )
        )

    def _dispatch_turn_steps(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        if not self._conversation_gate(
            request_id, params, _TURN_STEPS_KEYS, "turn.steps"
        ):
            return
        turn_id = params.get("turn_id")
        if not _valid_id(turn_id):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "turn_id is required"
            )
            return
        after_step_no = params.get("after_step_no", 0)
        limit = params.get("limit", 20)
        if after_step_no is None:
            after_step_no = 0
        if limit is None:
            limit = 20
        if (
            type(after_step_no) is not int
            or not 0 <= after_step_no <= 1000
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "after_step_no must be 0..1000 and limit 1..50",
            )
            return
        if not self._require_turn_slot(request_id, "turn.steps"):
            return
        self.worker.submit(
            lambda: self._run_conversation_read(
                request_id,
                lambda: self.runtime.conversation_turn_steps(
                    turn_id=turn_id,
                    after_step_no=after_step_no,
                    limit=limit,
                ),
            )
        )

    def _dispatch_turn_trace(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        if not self._conversation_gate(
            request_id, params, _TURN_TRACE_KEYS, "turn.trace"
        ):
            return
        turn_id = params.get("turn_id")
        if not _valid_id(turn_id):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "turn_id is required"
            )
            return
        if not self._require_turn_slot(request_id, "turn.trace"):
            return
        self.worker.submit(
            lambda: self._run_conversation_read(
                request_id,
                lambda: self.runtime.turn_trace(turn_id=turn_id),
            )
        )

    def _dispatch_conversation_usage(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        if not self._conversation_gate(
            request_id, params, _CONVERSATION_USAGE_KEYS, "conversation.usage"
        ):
            return
        conversation_id = params.get("conversation_id")
        turn_id = params.get("turn_id")
        if not _valid_id(conversation_id):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "conversation_id is required"
            )
            return
        if turn_id is not None and not _valid_id(turn_id):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "turn_id must be an id"
            )
            return
        if not self._require_turn_slot(request_id, "conversation.usage"):
            return
        self.worker.submit(
            lambda: self._run_conversation_read(
                request_id,
                lambda: self.runtime.conversation_usage(
                    conversation_id=conversation_id, turn_id=turn_id
                ),
            )
        )

    def _dispatch_turn_cancel(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        if not self._conversation_gate(
            request_id, params, _TURN_CANCEL_KEYS, "turn.cancel"
        ):
            return
        turn_id = params.get("turn_id")
        if not _valid_id(turn_id):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "turn_id is required"
            )
            return
        with self._cancel_lock:
            event = self._cancel_events.get(turn_id)
            if event is not None:
                event.set()
        if event is not None:
            self._send_ok(
                request_id,
                {
                    "turn_id": turn_id,
                    "cancel_requested": True,
                    "terminal": False,
                },
            )
            return
        # The reader cannot read the DB: do not guess a terminal state.
        self._send_ok(
            request_id,
            {
                "turn_id": turn_id,
                "cancel_requested": False,
                "terminal": None,
                "reason": "NOT_RUNNING",
            },
        )

    # ------------------------------------------------------ RG04 postprocess

    def _dispatch_postprocess_status(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        if not self._conversation_gate(
            request_id,
            params,
            _POSTPROCESS_STATUS_KEYS,
            "postprocess.status",
        ):
            return
        conversation_id = params.get("conversation_id")
        if not _valid_id(conversation_id):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "conversation_id is required"
            )
            return
        raw_turn_ids = params.get("turn_ids")
        turn_ids: list[str] | None = None
        if raw_turn_ids is not None:
            if (
                not isinstance(raw_turn_ids, list)
                or not 1 <= len(raw_turn_ids) <= 20
                or any(not _valid_id(item) for item in raw_turn_ids)
            ):
                self._send_error(
                    request_id,
                    ERROR_INVALID_PARAMS,
                    "turn_ids must be 1..20 ids or null",
                )
                return
            turn_ids = list(raw_turn_ids)
        if not self._require_turn_slot(request_id, "postprocess.status"):
            return
        self.worker.submit(
            lambda: self._run_conversation_read(
                request_id,
                lambda: self.runtime.postprocess_status(
                    conversation_id=conversation_id, turn_ids=turn_ids
                ),
            )
        )

    def _dispatch_postprocess_retry(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        if not self._conversation_gate(
            request_id, params, _POSTPROCESS_RETRY_KEYS, "postprocess.retry"
        ):
            return
        turn_id = params.get("turn_id")
        if not _valid_id(turn_id):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "turn_id is required"
            )
            return
        if self.runtime is None or not self.runtime.memory_bridge_configured():
            self._send_error(
                request_id,
                ERROR_UNAVAILABLE,
                "the memory bridge is not configured for this host",
            )
            return
        if not self._require_turn_slot(request_id, "postprocess.retry"):
            return
        self.worker.submit(
            lambda: self._run_postprocess_retry(request_id, turn_id)
        )

    def _run_postprocess_retry(self, request_id: str, turn_id: str) -> None:
        assert self.runtime is not None
        if self._foreground_send_pending():
            # Q6: never queue postprocess work ahead of a pending send.
            self._send_ok(
                request_id,
                {
                    "skipped": True,
                    "reason": "foreground_send_pending",
                    "turn_id": turn_id,
                },
            )
            return
        try:
            result = self.runtime.postprocess_retry(turn_id=turn_id)
        except BaseException as exc:  # noqa: BLE001 - refusal is a result
            code, message, unknown = _map_conversation_error(exc)
            self._send_error(request_id, code, message, result_unknown=unknown)
            return
        self._send_ok(request_id, result)

    # ------------------------------------------------------ cancel / events

    def _register_cancel(self, turn_id: str) -> None:
        with self._cancel_lock:
            self._cancel_events.setdefault(turn_id, threading.Event())

    def _release_cancel(self, turn_id: str) -> None:
        with self._cancel_lock:
            self._cancel_events.pop(turn_id, None)

    def _cancel_requested(self, turn_id: str) -> bool:
        with self._cancel_lock:
            event = self._cancel_events.get(turn_id)
            return bool(event is not None and event.is_set())

    def _ring_event(self, row: Any, live: Any) -> None:
        """Worker-side sink: the DB row is committed before this runs."""

        self._event_ring.append(
            row.turn_id, conversation_dto.turn_event_dict(row, live)
        )

    def _dispatch_conversation_search(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        if not self._conversation_gate(
            request_id, params, _CONVERSATION_SEARCH_KEYS, "conversation.search"
        ):
            return
        query = params.get("query")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 256:
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "query must be 1..256 characters",
            )
            return
        limit = params.get("limit", 5)
        if type(limit) is not int or not 1 <= limit <= 10:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "limit must be 1..10"
            )
            return
        include_full = params.get("include_full", False)
        if type(include_full) is not bool:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "include_full must be a boolean"
            )
            return
        conversation_id = params.get("conversation_id")
        if conversation_id is not None and not _valid_id(conversation_id):
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "conversation_id must be an id or null",
            )
            return
        before = params.get("before")
        if before is not None and (
            not isinstance(before, str) or not 1 <= len(before) <= 2048
        ):
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "before must be a cursor string or null",
            )
            return
        if not self._require_turn_slot(request_id, "conversation.search"):
            return
        self.worker.submit(
            lambda: self._run_conversation_read(
                request_id,
                lambda: self.runtime.conversation_search(
                    query=query,
                    limit=limit,
                    conversation_id=conversation_id,
                    before=before,
                    include_full=include_full,
                ),
            )
        )

    def _dispatch_conversation_send(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        poisoned = sorted(_CONVERSATION_AUTHORITY_FIELDS.intersection(params))
        if poisoned:
            self._send_error(
                request_id,
                ERROR_AUTHORITY_FIELD,
                "authority and destination fields are host-owned",
            )
            return
        unknown = sorted(set(params) - _CONVERSATION_SEND_KEYS)
        if unknown:
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "conversation.send accepts only session_id, conversation_id, "
                "client_request_id, text and confirmed_destination_id",
            )
            return
        session_id = params.get("session_id")
        conversation_id = params.get("conversation_id")
        client_request_id = params.get("client_request_id")
        text = params.get("text")
        if not _valid_id(session_id):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "session_id is required"
            )
            return
        if not _valid_id(conversation_id):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "conversation_id is required"
            )
            return
        if (
            not isinstance(client_request_id, str)
            or not 1 <= len(client_request_id) <= 128
            or any(char in client_request_id for char in "\x00\r\n\t")
        ):
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "client_request_id must be 1..128 printable characters",
            )
            return
        if not isinstance(text, str) or not text.strip():
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "text must be a non-empty string"
            )
            return
        max_chars = (
            self.config.max_question_chars
            if self.config is not None
            else MAX_QUESTION_CHARS
        )
        if len(text) > max_chars:
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                f"text exceeds {max_chars} characters",
            )
            return
        if "\x00" in text:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "text must not contain NUL"
            )
            return
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "text is not valid text"
            )
            return
        confirmed = params.get("confirmed_destination_id")
        if confirmed is not None and not isinstance(confirmed, str):
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "confirmed_destination_id must be a string or null",
            )
            return

        if self.data_locked:
            self._send_error(
                request_id,
                ERROR_DATA_LOCKED,
                "another process owns this data instance; no turn was started",
            )
            return
        if self.config is None or self.runtime is None:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        if not self.runtime.ready:
            self._send_error(
                request_id,
                ERROR_HOST_NOT_READY,
                "the trusted host is not ready for a conversation turn",
            )
            return
        if session_id != self.session_id:
            self._send_error(
                request_id,
                ERROR_SESSION_MISMATCH,
                "the request belongs to a different host session",
            )
            return
        if not self._require_confirmed_destination(request_id, confirmed):
            return

        text_digest = sha256_hex(text)
        with self._turn_replay_lock:
            entry = self._turn_replay.get((conversation_id, client_request_id))
        if entry is not None:
            turn_id, cached_digest, turn_no, state = entry[0], entry[1], entry[2], entry[3]
            if cached_digest != text_digest:
                self._send_error(
                    request_id,
                    ERROR_CLIENT_REQUEST_CONFLICT,
                    "client_request_id was already used with another payload",
                )
                return
            self._send_ok(
                request_id,
                {
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "turn_no": turn_no,
                    "client_request_id": client_request_id,
                    "state": state,
                    "reused": True,
                },
            )
            return

        if not self._model_lock.acquire(blocking=False):
            LOGGER.info("conversation busy request_id=%s", request_id)
            self._send_error(
                request_id,
                ERROR_BUSY,
                "a conversation turn is already running; it is not queued",
            )
            return
        # Mark the foreground pending before signalling: a maintenance job that
        # starts concurrently sees the Q6 gate, and an already-active attempt
        # gets the Event from the reader thread.  Neither path touches the chat
        # model or the worker queue.
        self._mark_send_pending(1)
        self._request_maintenance_yield()
        try:
            self.worker.submit(
                lambda: self._run_conversation_send(
                    request_id,
                    conversation_id,
                    client_request_id,
                    text,
                    text_digest,
                )
            )
        except BaseException:
            self._mark_send_pending(-1)
            self._model_lock.release()
            raise

    def _run_conversation_send(
        self,
        request_id: str,
        conversation_id: str,
        client_request_id: str,
        text: str,
        text_digest: str,
    ) -> None:
        assert self.runtime is not None
        try:
            conversation, creation = self.runtime.conversation_send(
                conversation_id=conversation_id,
                client_request_id=client_request_id,
                text=text,
            )
        except BaseException as exc:  # noqa: BLE001 - refusal is a result
            code, message, unknown = _map_conversation_error(exc)
            self._model_lock.release()
            self._send_error(
                request_id, code, message, result_unknown=unknown
            )
            return
        # T1 is committed: the idempotent replay entry exists before the
        # accepted response leaves the process.
        self._remember_turn(
            conversation_id, client_request_id, creation, text_digest
        )
        # The cancel flag exists before the UI can learn the turn id.
        self._register_cancel(creation.turn.turn_id)
        try:
            self._send_ok(request_id, conversation_dto.accepted_dict(creation))
        except BaseException:  # noqa: BLE001 - the turn still runs
            LOGGER.exception("accepted response could not be written")
        try:
            execution = self.runtime.conversation_execute(
                conversation=conversation, creation=creation
            )
            self._update_replay_state(
                conversation_id, client_request_id, execution.turn.state
            )
        except BaseException:  # noqa: BLE001 - keep the DB thread alive
            LOGGER.exception("conversation turn failed outside the orchestrator")
            try:
                self.runtime.store.fail_turn(
                    namespace=self.runtime.config.authority.namespace,
                    turn_id=creation.turn.turn_id,
                    reason="turn:host_failure",
                )
            except BaseException:  # noqa: BLE001 - already failing
                LOGGER.exception("turn could not be marked failed")
        finally:
            self._release_cancel(creation.turn.turn_id)
            self._model_lock.release()
            self._mark_send_pending(-1)
            # Q6: the send is no longer pending; a delayed sweep may now run.
            self._schedule_postprocess_sweep(0.8)
            # RG08 R-E: one bounded cognition-review attempt for this terminal
            # turn.  It is delayed so the existing memory delivery can land
            # first; pending delivery is an honest no/incomplete result.
            self._queue_maintenance_turn(creation.turn.turn_id)
            self._schedule_maintenance_review(1.8)
            # If a previous attempt was cancelled by an earlier foreground
            # send, resume it only now that the foreground is idle.  One
            # bounded attempt; no immediate retry and no loop.
            self._schedule_maintenance_resume(3.0)

    def _run_conversation_read(self, request_id: str, call: Any) -> None:
        try:
            result = call()
        except BaseException as exc:  # noqa: BLE001 - refusal is a result
            code, message, unknown = _map_conversation_error(exc)
            self._send_error(
                request_id, code, message, result_unknown=unknown
            )
            return
        self._send_ok(request_id, result)

    def _conversation_gate(
        self,
        request_id: str,
        params: dict[str, Any],
        keys: frozenset[str],
        method: str,
    ) -> bool:
        poisoned = sorted(_CONVERSATION_AUTHORITY_FIELDS.intersection(params))
        if poisoned:
            self._send_error(
                request_id,
                ERROR_AUTHORITY_FIELD,
                "authority and destination fields are host-owned",
            )
            return False
        unknown = sorted(set(params) - keys)
        if unknown:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "unsupported parameter"
            )
            return False
        session_id = params.get("session_id")
        if not _valid_id(session_id):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "session_id is required"
            )
            return False
        if self.data_locked:
            self._send_error(
                request_id,
                ERROR_DATA_LOCKED,
                "another process owns this data instance",
            )
            return False
        if self.config is None or self.runtime is None or not self.runtime.ready:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return False
        if session_id != self.session_id:
            self._send_error(
                request_id,
                ERROR_SESSION_MISMATCH,
                "the request belongs to a different host session",
            )
            return False
        return True

    def _conversation_get_payload(
        self, conversation_id: str, before: Any, limit: int
    ) -> dict[str, Any]:
        conversation, turns, messages = self.runtime.conversation_get(
            conversation_id=conversation_id, before=before, limit=limit
        )
        payload = {
            "conversation": conversation_dto.conversation_dict(conversation),
            "turns": [conversation_dto.turn_dict(item) for item in turns],
        }
        payload.update(conversation_dto.message_page_dict(messages))
        return payload

    # ------------------------------------------------------- replay cache

    def _remember_turn(
        self,
        conversation_id: str,
        client_request_id: str,
        creation: Any,
        text_digest: str,
    ) -> None:
        key = (conversation_id, client_request_id)
        with self._turn_replay_lock:
            if key not in self._turn_replay:
                self._turn_replay_order.append(key)
                while len(self._turn_replay_order) > _TURN_REPLAY_LIMIT:
                    oldest = self._turn_replay_order.pop(0)
                    self._turn_replay.pop(oldest, None)
            self._turn_replay[key] = (
                creation.turn.turn_id,
                text_digest,
                creation.turn.turn_no,
                creation.turn.state,
            )

    def _update_replay_state(
        self, conversation_id: str, client_request_id: str, state: str
    ) -> None:
        key = (conversation_id, client_request_id)
        with self._turn_replay_lock:
            entry = self._turn_replay.get(key)
            if entry is None:
                return
            self._turn_replay[key] = (entry[0], entry[1], entry[2], state)

    # -------------------------------------------------------------- consult

    def _dispatch_consult(self, request_id: str, params: dict[str, Any]) -> None:
        poisoned = sorted(_CONSULT_AUTHORITY_FIELDS.intersection(params))
        if poisoned:
            LOGGER.warning(
                "consult authority field rejected fields=%s request_id=%s",
                ",".join(poisoned),
                request_id,
            )
            self._send_error(
                request_id,
                ERROR_AUTHORITY_FIELD,
                "authority and destination fields are host-owned",
            )
            return
        unknown = sorted(set(params) - _CONSULT_KEYS)
        if unknown:
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "consult.interact accepts only session_id, question and "
                "confirmed_destination_id",
            )
            return
        session_id = params.get("session_id")
        if not _valid_id(session_id):
            self._send_error(request_id, ERROR_INVALID_PARAMS, "session_id is required")
            return
        question = params.get("question")
        if not isinstance(question, str) or not question.strip():
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "question must be a non-empty string"
            )
            return
        max_chars = (
            self.config.max_question_chars
            if self.config is not None
            else MAX_QUESTION_CHARS
        )
        if len(question) > max_chars:
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                f"question exceeds {max_chars} characters",
            )
            return
        if "\x00" in question:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "question must not contain NUL"
            )
            return
        try:
            question.encode("utf-8")
        except UnicodeEncodeError:
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "question is not valid text"
            )
            return
        confirmed = params.get("confirmed_destination_id")
        if confirmed is not None and not isinstance(confirmed, str):
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "confirmed_destination_id must be a string or null",
            )
            return

        if self.data_locked:
            self._send_error(
                request_id,
                ERROR_DATA_LOCKED,
                "another process owns this data instance; no consult was started",
            )
            return
        if self.config is None or self.runtime is None:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        if not self.runtime.ready:
            self._send_error(
                request_id,
                ERROR_HOST_NOT_READY,
                "the trusted host is not ready for a consult",
            )
            return
        if session_id != self.session_id:
            self._send_error(
                request_id,
                ERROR_SESSION_MISMATCH,
                "the request belongs to a different host session",
            )
            return

        if not self._require_confirmed_destination(request_id, confirmed):
            return

        if not self._model_lock.acquire(blocking=False):
            LOGGER.info("consult busy request_id=%s", request_id)
            self._send_error(
                request_id,
                ERROR_BUSY,
                "a consult interaction is already running; it is not queued",
            )
            return
        try:
            self.worker.submit(lambda: self._run_consult(request_id, question))
        except BaseException:
            self._model_lock.release()
            raise

    def _run_consult(self, request_id: str, question: str) -> None:
        assert self.runtime is not None
        self._run_model_call(
            request_id, "consult.interact", lambda: self.runtime.consult(question)
        )

    def _run_model_call(
        self, request_id: str, method: str, call: Callable[[], dict[str, Any]]
    ) -> None:
        """Finish runtime work and release admission before publishing its outcome.

        The single DB worker still serializes execution. Transport failures must
        escape, not produce a second response after a possibly partial write.
        """
        error = None
        result = None
        try:
            result = call()
        except (InputRegistrationError, ReviewRequestError) as exc:
            error = (exc.code, exc.message, False)
        except BaseException as exc:  # noqa: BLE001 - contain runtime failures
            if method == "consult.interact":
                error = _map_model_error(exc, default_unknown=True)
            else:
                error = _map_review_error(exc, default_unknown=True)
        finally:
            self._model_lock.release()
        # No runtime exception handler surrounds transport or post-response work.
        if error is None:
            self._send_ok(request_id, result)
        else:
            code, message, unknown = error
            self._send_error(request_id, code, message, result_unknown=unknown)

    # ----------------------------------------------------------------- review

    def _dispatch_model_test(
        self, request_id: str, params: dict[str, Any]
    ) -> None:
        """One explicit, user-triggered DeepSeek connectivity probe.

        This is not a PCS interaction.  It does not write a Context, Attempt,
        Belief, Interaction or Artifact; it only proves that the configured
        endpoint and saved credential work.
        """

        forbidden = sorted(AUTHORITY_FIELDS.intersection(params))
        if forbidden:
            self._send_error(
                request_id,
                ERROR_AUTHORITY_FIELD,
                "authority, approval and grant fields are host-owned",
            )
            return
        unknown = sorted(set(params) - _MODEL_TEST_KEYS)
        if unknown:
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                f"model.test_connection accepts only {', '.join(sorted(_MODEL_TEST_KEYS))}",
            )
            return
        session_id = params.get("session_id")
        if not _valid_id(session_id):
            self._send_error(request_id, ERROR_INVALID_PARAMS, "session_id is required")
            return
        if self.data_locked:
            self._send_error(
                request_id, ERROR_DATA_LOCKED, "another process owns this data instance"
            )
            return
        if self.config is None or self.runtime is None:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        if session_id != self.session_id:
            self._send_error(
                request_id,
                ERROR_SESSION_MISMATCH,
                "the request belongs to a different host session",
            )
            return
        if self.worker is None:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        if not self._model_lock.acquire(blocking=False):
            self._send_error(
                request_id,
                ERROR_BUSY,
                "a model operation is already running; the connection test was not started",
            )
            return
        try:
            self.worker.submit(lambda: self._run_model_test(request_id))
        except BaseException:
            self._model_lock.release()
            raise

    def _run_model_test(self, request_id: str) -> None:
        assert self.runtime is not None
        result = None
        error = None
        try:
            result = self.runtime.test_model_connection()
        except ReviewRequestError as exc:
            error = (exc.code, exc.message)
        except BaseException as exc:  # noqa: BLE001 - contain probe failures
            error = (
                "MODEL_TEST_FAILED",
                f"连接测试在可信宿主内失败（{type(exc).__name__}）",
            )
        finally:
            self._model_lock.release()
        if error is None:
            self._send_ok(request_id, result)
        else:
            code, message = error
            self._send_error(request_id, code, message)

    def _dispatch_review(
        self, request_id: str, method: str, params: dict[str, Any]
    ) -> None:
        """Validate one ``review.*`` request, then run it on the DB worker.

        The frontend supplies identities and the user's decision only.  Every
        permission, host instance, destination confirmation, Approval/grant,
        candidate body, model and provider stay host-owned.
        """

        if method == "review.generate" and "feedback" in params:
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                "review.generate does not accept feedback",
            )
            return

        allowed = {
            "review.work.list": _WORK_LIST_KEYS,
            "review.candidates.list": _CANDIDATE_LIST_KEYS,
            "review.candidate.detail": _CANDIDATE_DETAIL_KEYS,
            "review.generate": _REVIEW_GENERATE_KEYS,
            "review.resolve": _REVIEW_RESOLVE_KEYS,
            "review.result": _REVIEW_RESULT_KEYS,
        }[method]
        forbidden = {
            "review.work.list": AUTHORITY_FIELDS,
            "review.candidates.list": AUTHORITY_FIELDS,
            "review.candidate.detail": AUTHORITY_FIELDS,
            "review.generate": AUTHORITY_FIELDS,
            "review.resolve": _RESOLVE_AUTHORITY_FIELDS,
            "review.result": _REVIEW_RESULT_AUTHORITY_FIELDS,
        }[method]
        poisoned = sorted(forbidden.intersection(params))
        if poisoned:
            LOGGER.warning(
                "review authority field rejected method=%s fields=%s request_id=%s",
                method,
                ",".join(poisoned),
                request_id,
            )
            self._send_error(
                request_id,
                ERROR_AUTHORITY_FIELD,
                "authority, approval and grant fields are host-owned",
            )
            return
        unknown = sorted(set(params) - allowed)
        if unknown:
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                f"{method} accepts only {', '.join(sorted(allowed))}",
            )
            return

        session_id = params.get("session_id")
        if not _valid_id(session_id):
            self._send_error(request_id, ERROR_INVALID_PARAMS, "session_id is required")
            return

        if method == "review.candidates.list":
            work_id = params.get("work_id")
            if not _valid_id(work_id):
                self._send_error(
                    request_id, ERROR_INVALID_PARAMS, "work_id is required"
                )
                return
            generation = params.get("generation")
            if generation is not None and not _valid_generation(generation):
                self._send_error(
                    request_id,
                    ERROR_INVALID_PARAMS,
                    "generation must be a non-negative integer",
                )
                return
        elif method == "review.candidate.detail":
            if not _valid_id(params.get("candidate_id")):
                self._send_error(
                    request_id, ERROR_INVALID_PARAMS, "candidate_id is required"
                )
                return
            if not _valid_version(params.get("artifact_version")):
                self._send_error(
                    request_id,
                    ERROR_INVALID_PARAMS,
                    "artifact_version must be a positive integer",
                )
                return
        elif method == "review.generate":
            if not _valid_id(params.get("work_id")):
                self._send_error(
                    request_id, ERROR_INVALID_PARAMS, "work_id is required"
                )
                return
            if not _valid_id(params.get("event_id")):
                self._send_error(
                    request_id, ERROR_INVALID_PARAMS, "event_id is required"
                )
                return
            confirmed = params.get("confirmed_destination_id")
            if confirmed is not None and (
                not isinstance(confirmed, str) or not confirmed
            ):
                self._send_error(
                    request_id,
                    ERROR_INVALID_PARAMS,
                    "confirmed_destination_id must be a non-empty string or null",
                )
                return
        elif method == "review.resolve":
            if not _valid_id(params.get("candidate_id")):
                self._send_error(
                    request_id, ERROR_INVALID_PARAMS, "candidate_id is required"
                )
                return
            if not _valid_version(params.get("artifact_version")):
                self._send_error(
                    request_id,
                    ERROR_INVALID_PARAMS,
                    "artifact_version must be a positive integer",
                )
                return
            if params.get("decision") not in ("approve", "reject"):
                self._send_error(
                    request_id,
                    ERROR_INVALID_PARAMS,
                    "decision must be approve or reject",
                )
                return
        elif method == "review.result":
            if not _valid_id(params.get("interaction_id")):
                self._send_error(
                    request_id, ERROR_INVALID_PARAMS, "interaction_id is required"
                )
                return

        if self.data_locked:
            self._send_error(
                request_id,
                ERROR_DATA_LOCKED,
                "another process owns this data instance",
            )
            return
        if self.config is None or self.runtime is None or not self.runtime.ready:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        if session_id != self.session_id:
            self._send_error(
                request_id,
                ERROR_SESSION_MISMATCH,
                "the request belongs to a different host session",
            )
            return
        if method == "review.generate":
            failure = self._destination_confirmation_error(
                params.get("confirmed_destination_id")
            )
            if failure is not None:
                code, message = failure
                self._send_error(request_id, code, message)
                return

        if self.worker is None:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return

        runtime = self.runtime
        if method == "review.work.list":
            self.worker.submit(
                lambda: self._run_review_read(
                    request_id, "review.work.list", runtime.review_work_list
                )
            )
            return
        if method == "review.candidates.list":
            work_id = params.get("work_id")
            generation = params.get("generation")
            self.worker.submit(
                lambda: self._run_review_read(
                    request_id,
                    "review.candidates.list",
                    lambda: runtime.review_candidates(
                        work_id=work_id, generation=generation
                    ),
                )
            )
            return
        if method == "review.candidate.detail":
            candidate_id = params.get("candidate_id")
            artifact_version = params.get("artifact_version")
            self.worker.submit(
                lambda: self._run_review_read(
                    request_id,
                    "review.candidate.detail",
                    lambda: runtime.review_candidate_detail(
                        candidate_id=candidate_id,
                        artifact_version=artifact_version,
                    ),
                )
            )
            return
        if method == "review.result":
            interaction_id = params.get("interaction_id")
            self.worker.submit(
                lambda: self._run_review_result(request_id, interaction_id)
            )
            return
        if method == "review.generate":
            work_id = params.get("work_id")
            event_id = params.get("event_id")
            if not self._model_lock.acquire(blocking=False):
                self._send_error(
                    request_id,
                    ERROR_BUSY,
                    "a model interaction is already running; it is not queued",
                )
                return
            try:
                self.worker.submit(
                    lambda: self._run_review_generate(
                        request_id, work_id, event_id
                    )
                )
            except BaseException:
                self._model_lock.release()
                raise
            return
        # review.resolve
        candidate_id = params.get("candidate_id")
        artifact_version = params.get("artifact_version")
        decision = params.get("decision")
        if not self._model_lock.acquire(blocking=False):
            self._send_error(
                request_id,
                ERROR_BUSY,
                "a model interaction is already running; the decision was not submitted",
            )
            return
        try:
            self.worker.submit(
                lambda: self._run_review_resolve(
                    request_id, candidate_id, artifact_version, decision
                )
            )
        except BaseException:
            self._model_lock.release()
            raise

    def _destination_confirmation_error(
        self, confirmed: Any
    ) -> tuple[str, str] | None:
        descriptor = self.config.destination if self.config is not None else None
        if descriptor is None:
            return ERROR_HOST_NOT_READY, "the trusted host has no destination"
        if descriptor.confirmation_required:
            if confirmed is None:
                return (
                    ERROR_CONFIRMATION_REQUIRED,
                    "this external destination has not been confirmed in this session",
                )
            if confirmed != descriptor.destination_id:
                return (
                    ERROR_CONFIRMATION_REQUIRED,
                    "the destination changed; confirm the current destination again",
                )
            with self._confirm_lock:
                recorded = self._confirmations.get(
                    (self.session_id, descriptor.destination_id)
                )
            if recorded is None:
                return (
                    ERROR_CONFIRMATION_REQUIRED,
                    "no host confirmation record exists for this destination",
                )
        elif confirmed is not None and confirmed != descriptor.destination_id:
            return (
                ERROR_DESTINATION_MISMATCH,
                "the confirmed destination does not match the current destination",
            )
        return None

    def _run_review_read(
        self, request_id: str, method: str, call: Callable[[], dict[str, Any]]
    ) -> None:
        try:
            result = call()
        except ReviewRequestError as exc:
            self._send_error(request_id, exc.code, exc.message)
        except BaseException as exc:  # noqa: BLE001 - a read must not kill the worker
            self._review_failure(request_id, method, exc, unknown=False)
        else:
            self._send_ok(request_id, result)

    def _run_review_generate(
        self, request_id: str, work_id: str, event_id: str
    ) -> None:
        assert self.runtime is not None
        self._run_model_call(
            request_id,
            "review.generate",
            lambda: self.runtime.review_generate(work_id=work_id, event_id=event_id),
        )

    def _run_review_resolve(
        self,
        request_id: str,
        candidate_id: str,
        artifact_version: int,
        decision: str,
    ) -> None:
        assert self.runtime is not None
        self._run_model_call(
            request_id,
            "review.resolve",
            lambda: self.runtime.review_resolve(
                candidate_id=candidate_id,
                artifact_version=artifact_version,
                decision=decision,
            ),
        )

    def _run_review_result(self, request_id: str, interaction_id: str) -> None:
        assert self.runtime is not None
        try:
            result = self.runtime.review_result(interaction_id=interaction_id)
        except ReviewRequestError as exc:
            self._send_error(request_id, exc.code, exc.message)
        except BaseException as exc:  # noqa: BLE001 - never crash the reader
            self._review_failure(
                request_id, "review.result", exc, unknown=True
            )
        else:
            self._send_ok(request_id, result)

    def _review_failure(
        self,
        request_id: str,
        method: str,
        exc: BaseException,
        *,
        unknown: bool,
    ) -> None:
        code, message, result_unknown = _map_review_error(
            exc, default_unknown=unknown
        )
        LOGGER.warning(
            "review method failed method=%s code=%s class=%s",
            method,
            code,
            type(exc).__name__,
        )
        self._send_error(
            request_id, code, message, result_unknown=result_unknown
        )

    # ------------------------------------------------------------- cognition

    def _dispatch_cognition(
        self, request_id: str, method: str, params: dict[str, Any]
    ) -> None:
        """Validate one RG05 cognition request and run it on the DB worker.

        The WebView supplies a session id and -- for a decision -- the candidate
        identity plus the human's choice.  Every approval envelope, statement
        event and approval scope stays host-owned.
        """

        read = method in _COGNITION_READ_METHODS
        allowed = _COGNITION_LIST_KEYS if read else _COGNITION_DECIDE_KEYS
        forbidden = AUTHORITY_FIELDS if read else _COGNITION_DECIDE_FORBIDDEN
        poisoned = sorted(forbidden.intersection(params))
        if poisoned:
            LOGGER.warning(
                "cognition authority field rejected method=%s fields=%s "
                "request_id=%s",
                method,
                ",".join(poisoned),
                request_id,
            )
            self._send_error(
                request_id,
                ERROR_AUTHORITY_FIELD,
                "approval, statement and scope fields are host-owned",
            )
            return
        unknown = sorted(set(params) - allowed)
        if unknown:
            self._send_error(
                request_id,
                ERROR_INVALID_PARAMS,
                f"{method} accepts only {', '.join(sorted(allowed))}",
            )
            return

        session_id = params.get("session_id")
        if not _valid_id(session_id):
            self._send_error(request_id, ERROR_INVALID_PARAMS, "session_id is required")
            return
        limit = params.get("limit", 20)
        if read and (type(limit) is not int or not 1 <= limit <= 20):
            self._send_error(
                request_id, ERROR_INVALID_PARAMS, "limit must be 1..20"
            )
            return
        candidate_id = params.get("candidate_id")
        artifact_version = params.get("artifact_version")
        decision = params.get("decision")
        if not read:
            if not _valid_id(candidate_id):
                self._send_error(
                    request_id, ERROR_INVALID_PARAMS, "candidate_id is required"
                )
                return
            if not _valid_version(artifact_version):
                self._send_error(
                    request_id,
                    ERROR_INVALID_PARAMS,
                    "artifact_version must be a positive integer",
                )
                return
            if decision not in ("approve", "reject"):
                self._send_error(
                    request_id,
                    ERROR_INVALID_PARAMS,
                    "decision must be approve or reject",
                )
                return
            if self._model_lock.locked():
                # RG05/Q5: a decision moves the watermarks a running turn's
                # Context was built on.  Refuse honestly instead of breaking it.
                self._send_error(
                    request_id,
                    ERROR_BUSY,
                    "a turn is running; review is disabled until it finishes",
                )
                return

        if self.data_locked:
            self._send_error(
                request_id,
                ERROR_DATA_LOCKED,
                "another process owns this data instance",
            )
            return
        if self.config is None or self.runtime is None or not self.runtime.ready:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return
        if session_id != self.session_id:
            self._send_error(
                request_id,
                ERROR_SESSION_MISMATCH,
                "the request belongs to a different host session",
            )
            return
        if self.worker is None:
            self._send_error(
                request_id, ERROR_HOST_NOT_READY, "the trusted host is not ready"
            )
            return

        runtime = self.runtime
        if read:
            self.worker.submit(
                lambda: self._run_cognition_read(
                    request_id,
                    method,
                    lambda: runtime.cognition_candidates(limit=limit),
                )
            )
            return
        self.worker.submit(
            lambda: self._run_cognition_read(
                request_id,
                method,
                lambda: runtime.cognition_decide(
                    candidate_id=candidate_id,
                    artifact_version=artifact_version,
                    decision=decision,
                ),
            )
        )

    def _run_cognition_read(
        self, request_id: str, method: str, call: Callable[[], dict[str, Any]]
    ) -> None:
        try:
            result = call()
        except ReviewRequestError as exc:
            self._send_error(request_id, exc.code, exc.message)
        except BaseException as exc:  # noqa: BLE001 - never crash the worker
            self._review_failure(request_id, method, exc, unknown=False)
        else:
            self._send_ok(request_id, result)

    # ------------------------------------------------------------------ ids

    def _claim_id(self, request_id: str) -> bool:
        with self._seen_lock:
            if request_id in self._seen:
                return False
            self._seen.add(request_id)
            self._seen_order.append(request_id)
            if len(self._seen_order) > _MAX_SEEN_IDS:
                oldest = self._seen_order.pop(0)
                self._seen.discard(oldest)
            return True

    # ---------------------------------------------------------------- output

    def _send_ok(self, request_id: str, result: dict[str, Any]) -> None:
        self._write(
            {
                "v": PROTOCOL_VERSION,
                "id": request_id,
                "ok": True,
                "result": result,
            }
        )

    def _send_error(
        self,
        request_id: str | None,
        code: str,
        message: str,
        *,
        result_unknown: bool = False,
    ) -> None:
        self._write(
            {
                "v": PROTOCOL_VERSION,
                "id": request_id,
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                    "result_unknown": result_unknown,
                },
            }
        )

    def _write(self, value: dict[str, Any]) -> None:
        try:
            encoded = framing.encode_frame(value)
        except Exception as exc:  # noqa: BLE001 - an unencodable response is fatal
            LOGGER.error("response encode failed class=%s", type(exc).__name__)
            raise _Stop() from exc
        with self._write_lock:
            try:
                self.stdout.write(encoded)
                self.stdout.flush()
            except (BrokenPipeError, OSError) as exc:
                self._stopping.set()
                raise _Stop() from exc


def _pcs_version() -> str:
    try:
        from quintessence_pcs import PROTOCOL_VERSION as pcs_version

        return str(pcs_version)
    except Exception:  # noqa: BLE001 - informational only
        return "unknown"


def _now() -> str:
    from quintessence_pcs.foundation.timeutil import utc_now_rfc3339

    return utc_now_rfc3339()


def _valid_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(char in _ID_CHARS for char in value)
    )


def _valid_version(value: Any) -> bool:
    return type(value) is int and value >= 1


def _valid_generation(value: Any) -> bool:
    return type(value) is int and value >= 0


def _map_model_error(
    exc: BaseException, *, default_unknown: bool
) -> tuple[str, str, bool]:
    """Map ModelPort failures without leaking provider bodies or credentials."""

    if isinstance(exc, ModelNotAuthorizedError):
        reason = getattr(exc, "reason", None)
        if reason == "authorization_expired":
            return (
                "MODEL_AUTHORIZATION_EXPIRED",
                "模型授权已过期；请重新授权后再试",
                False,
            )
        if reason == "authorization_mismatch":
            return (
                "MODEL_AUTHORIZATION_MISMATCH",
                "模型授权与当前 provider/model/manifest 不匹配；请重新授权",
                False,
            )
        return (
            "MODEL_NOT_AUTHORIZED",
            "模型调用未获得 C09 授权；没有发送请求",
            False,
        )
    if isinstance(exc, ModelBudgetExceededError):
        reason = getattr(exc, "reason", None)
        if reason in ("calls_exhausted", "calls_or_walltime"):
            return (
                "MODEL_CALLS_EXHAUSTED",
                "模型调用次数或时长已达授权额度；需要新的授权",
                False,
            )
        if reason == "injection_budget":
            return (
                "MODEL_CONTEXT_BUDGET_EXCEEDED",
                "注入材料超出 PCS 上下文预算；请减小上下文或提高预算上限",
                False,
            )
        if reason == "request_budget":
            return (
                "MODEL_REQUEST_BUDGET_EXCEEDED",
                "整个请求超出预算；请减小上下文或输出预留",
                False,
            )
        return (
            "MODEL_BUDGET_EXCEEDED",
            "模型调用已达授权额度；没有继续发送请求",
            False,
        )
    if isinstance(exc, ModelConfigurationError):
        text = str(exc).lower()
        if "is not set" in text:
            return ("MODEL_KEY_MISSING", "尚未保存 API Key", False)
        if "api key" in text or "printable ascii" in text:
            return ("MODEL_KEY_INVALID", "API Key 格式无效", False)
        return (
            "MODEL_CONFIG_INVALID",
            "模型配置无效；请检查设置",
            False,
        )
    if isinstance(exc, ModelResponseError):
        return (
            "MODEL_INVALID_RESPONSE",
            "模型输出不是有效的 collab.v1 结构；未提交任何内容",
            False,
        )
    if isinstance(exc, ModelTransportError):
        text = str(exc).lower()
        if "http_401" in text or "http_403" in text:
            return (
                "MODEL_AUTH_FAILED",
                "DeepSeek 鉴权失败；请检查 API Key",
                False,
            )
        if "http_429" in text:
            return (
                "MODEL_RATE_LIMITED",
                "DeepSeek 限流或配额不足；请稍后再试",
                False,
            )
        if "http_5" in text:
            return (
                "MODEL_PROVIDER_ERROR",
                "DeepSeek 服务端错误；结果未提交",
                False,
            )
        if "timeout" in text or "did not answer" in text or "wall-clock" in text:
            return (
                "MODEL_TIMEOUT",
                "DeepSeek 请求超时；结果未知，请刷新权威状态",
                True,
            )
        if "network" in text:
            return (
                "MODEL_NETWORK",
                "无法连接 DeepSeek；结果未知，请刷新权威状态",
                True,
            )
        if "http_" in text:
            return (
                "MODEL_REQUEST_REJECTED",
                "DeepSeek 拒绝了请求；没有提交任何内容",
                False,
            )
        return (
            "MODEL_TRANSPORT_FAILED",
            "DeepSeek 请求失败；结果未知，请刷新权威状态",
            default_unknown,
        )
    if isinstance(exc, ModelAdapterError):
        return (
            "MODEL_ADAPTER_FAILED",
            "可信模型适配器失败；结果未知，请刷新权威状态",
            default_unknown,
        )
    return (
        ERROR_INTERNAL,
        "审查或咨询操作在可信宿主内失败；结果未知，请刷新权威状态",
        default_unknown,
    )


def _map_conversation_error(
    exc: BaseException,
) -> tuple[str, str, bool]:
    """Map a conversation refusal without leaking bodies, paths or credentials."""

    if isinstance(exc, ConversationRequestError):
        return exc.code, exc.message, False
    if isinstance(exc, ClientRequestConflict):
        return (
            ERROR_CLIENT_REQUEST_CONFLICT,
            "client_request_id was already used with another payload",
            False,
        )
    if isinstance(exc, RecordNotFound):
        return (
            ERROR_NOT_FOUND,
            "no such conversation or turn exists in this instance",
            False,
        )
    if isinstance(exc, InvalidInput):
        return (ERROR_INVALID_PARAMS, "conversation parameters are not accepted", False)
    if isinstance(exc, DbBusy):
        return (ERROR_DB_BUSY, "the host database is busy; no result was produced", False)
    if isinstance(exc, TurnStateConflict):
        return (
            ERROR_INTERNAL,
            "the turn state changed; read the authoritative state",
            True,
        )
    if isinstance(exc, CorruptStore):
        return (
            ERROR_INTERNAL,
            "the host state is inconsistent; the result is unknown",
            True,
        )
    return (
        ERROR_INTERNAL,
        "the conversation request failed inside the trusted host; result unknown",
        True,
    )


def _map_review_error(
    exc: BaseException, *, default_unknown: bool
) -> tuple[str, str, bool]:
    """Map one trusted-host failure to a public review label.

    Known refusals are definite failures.  An unexpected failure on a mutating
    call (generate/resolve) or on result recovery is reported as unknown: a
    write may have happened and the UI must refresh/recover, never retry.
    """

    if isinstance(exc, ModelAdapterError):
        return _map_model_error(exc, default_unknown=default_unknown)
    if isinstance(exc, AuthorizationRequired):
        return (
            ERROR_AUTHORIZATION_REQUIRED,
            "当前操作者缺少审查权限或许可；没有提交任何内容",
            False,
        )
    if isinstance(exc, (StateRevisionConflict, RecordVersionConflict)):
        return (
            ERROR_REVISION_CONFLICT,
            "目标版本已变化，需要重新获取权威状态",
            False,
        )
    if isinstance(exc, ContextStale):
        return (
            ERROR_CANDIDATE_STALE,
            "上下文或候选已过期，需要刷新候选",
            False,
        )
    if isinstance(exc, CandidateTransitionDenied):
        return (
            ERROR_CANDIDATE_STALE,
            "候选状态已变化，需要刷新候选",
            False,
        )
    if isinstance(exc, RecordNotFound):
        return (ERROR_NOT_FOUND, "未找到对应的审查记录", False)
    if isinstance(exc, Redacted):
        return (ERROR_REDACTED, "该内容受删除或隐私屏障保护", False)
    if isinstance(exc, InvalidInput):
        return (ERROR_INVALID_PARAMS, "审查请求参数不被接受", False)
    if isinstance(exc, DbBusy):
        return ("DB_BUSY", "宿主数据库忙；没有取得权威结果", False)
    if isinstance(exc, CorruptStore):
        return (
            ERROR_INTERNAL,
            "宿主内部状态不一致；结果未知，请刷新权威状态",
            default_unknown,
        )
    return (
        ERROR_INTERNAL,
        "审查操作在可信宿主内失败；结果未知，请刷新权威状态",
        default_unknown,
    )


def _config_path(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    from_env = os.environ.get("QUINTESSENCE_DESKTOP_CONFIG", "").strip()
    return from_env or None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcs-desktop-bridge",
        description="QUINTESSENCE desktop Consult PCS sidecar",
    )
    parser.add_argument("--config", default=None, help="private host configuration file")
    parser.add_argument("--prepare-instance", default=None, help="prepare a selected instance folder")
    parser.add_argument("--adopt-legacy-by", default=None)
    parser.add_argument("--adopt-legacy-reason", default=None)
    parser.add_argument("--declare-component-by", default=None)
    parser.add_argument("--declare-component-reason", default=None)
    parser.add_argument("--never-created-component", action="append", choices=("pcs", "memory"), default=[])
    parser.add_argument("--memory-adapter", action="store_true", help="run the bundled memory adapter")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Frozen Windows processes ignore the interpreter's PYTHONUTF8 switch.
    # CLI JSON and the bundled memory adapter still use fixed UTF-8 pipes.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    args = _parser().parse_args(argv)
    if args.memory_adapter:
        from quintessence_pcs.adapters.scope_recall.cli import main as adapter_main
        return adapter_main()
    if args.prepare_instance:
        from instance_manager import InstanceError, prepare
        try:
            raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise InstanceError("INSTANCE_REQUEST_INVALID", "实例配置请求过大。")
            template = json.loads(raw.decode("utf-8")) if raw.strip() else None
            adoption = None
            if args.adopt_legacy_by is not None or args.adopt_legacy_reason is not None:
                adoption = {"by": args.adopt_legacy_by, "reason": args.adopt_legacy_reason}
            declaration = None
            if args.never_created_component or args.declare_component_by is not None or args.declare_component_reason is not None:
                declaration = {"by": args.declare_component_by, "reason": args.declare_component_reason, "components": args.never_created_component}
            result = prepare(args.prepare_instance, template, legacy_adoption=adoption, component_declaration=declaration)
            print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
            return 0
        except InstanceError as exc:
            print(json.dumps({"ok": False, "error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False))
            return 1
        except Exception as exc:
            print(json.dumps({"ok": False, "error": {"code": "INSTANCE_PREPARE_FAILED", "message": "实例准备失败（" + type(exc).__name__ + "）；未执行重置，请检查原有状态后重试。"}}, ensure_ascii=False))
            return 1
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        bridge = Bridge(
            _config_path(args.config), sys.stdin.buffer, sys.stdout.buffer
        )
        return bridge.serve()
    except BaseException as exc:  # noqa: BLE001 - startup failure is a hard exit
        LOGGER.error("sidecar startup failed class=%s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
