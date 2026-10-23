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
from runtime import DesktopRuntime, InputRegistrationError, RuntimeStartupError

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

IMPLEMENTED_METHODS = frozenset(
    {"app.info", "app.confirm_destination", "consult.interact", "bridge.ping"}
)

#: Methods that exist in the long-term interaction vocabulary but are not
#: implemented by C12b.  They are answered honestly instead of being faked.
RESERVED_METHODS = frozenset(
    {
        "consult.result",
        "consult.history",
        "consult.resume",
        "consult.cancel",
        "review.generate",
        "review.commit",
        "review.reject",
        "review.result",
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
        self._consult_lock = threading.Lock()
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
                f"{method} is reserved and not implemented in C12b",
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
                "review": False,
                "review_reason": "C12C_NOT_IMPLEMENTED",
            },
            "capabilities": {
                "consult": ready,
                "confirm_destination": ready,
                "input_registration": ready,
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
                "facade": "ConsultFacade",
            },
        }

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

        if not self._consult_lock.acquire(blocking=False):
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
            self._consult_lock.release()
            raise

    def _run_consult(self, request_id: str, question: str) -> None:
        started = time.monotonic()
        LOGGER.info("consult start request_id=%s", request_id)
        assert self.runtime is not None
        try:
            result = self.runtime.consult(question)
            self._send_ok(request_id, result)
            LOGGER.info(
                "consult done request_id=%s state=%s elapsed_ms=%.3f",
                request_id,
                result.get("state"),
                (time.monotonic() - started) * 1000,
            )
        except InputRegistrationError as exc:
            LOGGER.warning(
                "consult input registration failed request_id=%s code=%s",
                request_id,
                exc.code,
            )
            self._send_error(request_id, exc.code, exc.message)
        except BaseException as exc:  # noqa: BLE001 - never crash the reader
            LOGGER.error(
                "consult internal error request_id=%s class=%s",
                request_id,
                type(exc).__name__,
            )
            self._send_error(
                request_id,
                ERROR_INTERNAL,
                f"consult failed inside the trusted host ({type(exc).__name__})",
                result_unknown=True,
            )
        finally:
            self._consult_lock.release()

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
