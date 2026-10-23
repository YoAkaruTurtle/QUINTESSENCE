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
)
from locking import DataInstanceLock, DataLocked
from runtime import (
    DesktopRuntime,
    InputRegistrationError,
    ReviewRequestError,
    RuntimeStartupError,
)
from quintessence_pcs.foundation.errors import (
    AuthorizationRequired,
    ContextStale,
    CorruptStore,
    DbBusy,
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
from quintessence_pcs.store.errors import CandidateTransitionDenied

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
IMPLEMENTED_METHODS = frozenset(
    {
        "app.info",
        "app.confirm_destination",
        "consult.interact",
        "bridge.ping",
        MODEL_TEST_METHOD,
        *REVIEW_METHODS,
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

_CONSULT_KEYS = frozenset({"session_id", "question", "confirmed_destination_id"})
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

    def __init__(self, config: HostConfig) -> None:
        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._ready = threading.Event()
        self.runtime: DesktopRuntime | None = None
        self.error: RuntimeStartupError | None = None
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
            runtime = DesktopRuntime(config)
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
        self._stopping = threading.Event()

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
            self.worker = _RuntimeWorker(config)
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
        if method == "app.confirm_destination":
            self._dispatch_confirm(request_id, params)
            return
        if method in REVIEW_METHODS:
            self._dispatch_review(request_id, method, params)
            return
        if method == MODEL_TEST_METHOD:
            self._dispatch_model_test(request_id, params)
            return
        self._dispatch_consult(request_id, params)

    # ------------------------------------------------------------------- info

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
                "review": review_caps["read"],
                "review_reason": review_reason,
            },
            "capabilities": {
                "consult": ready,
                "confirm_destination": ready,
                "input_registration": ready,
                "review": review_caps,
            },
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
        }

    # ---------------------------------------------------------- confirmation

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

        descriptor = self.config.destination
        if descriptor.confirmation_required:
            if confirmed is None:
                self._send_error(
                    request_id,
                    ERROR_CONFIRMATION_REQUIRED,
                    "this external destination has not been confirmed in this session",
                )
                return
            if confirmed != descriptor.destination_id:
                self._send_error(
                    request_id,
                    ERROR_CONFIRMATION_REQUIRED,
                    "the destination changed; confirm the current destination again",
                )
                return
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
                return
        elif confirmed is not None and confirmed != descriptor.destination_id:
            self._send_error(
                request_id,
                ERROR_DESTINATION_MISMATCH,
                "the confirmed destination does not match the current destination",
            )
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
        return (
            "MODEL_NOT_AUTHORIZED",
            "模型调用未获得 C09 授权；没有发送请求",
            False,
        )
    if isinstance(exc, ModelBudgetExceededError):
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
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
