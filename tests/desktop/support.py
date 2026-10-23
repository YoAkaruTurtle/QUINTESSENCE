"""Shared fixtures for the desktop bridge tests.

Every test runs a real sidecar process over real pipes against a temporary
synthetic store outside the checkout.  Nothing is mocked: framing, authority
checks, input registration, the ConsultFacade, Context/Attempt/Artifact rows
and the data-instance lock are the same code the installed sidecar uses.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SIDECAR_DIR = REPO_ROOT / "apps" / "desktop" / "sidecar"
SRC_DIR = REPO_ROOT / "src"
for _path in (SRC_DIR, SIDECAR_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import framing  # noqa: E402
from quintessence_pcs.store import CognitionStore  # noqa: E402

NAMESPACE = "example.test.desktop.lab"
HOST_INSTANCE = "host:desktop-lab"
ACCESS_REF = "authorization:host:desktop-lab"
OPERATOR = "operator:desktop-lab"
POLICY_REF = "policy:collab.v1"
PROVIDER = "synthetic"
MODEL = "scripted-collab-v1"
MODEL_KEY_ENV = "QUINTESSENCE_DESKTOP_LAB_MODEL_KEY"
LAB_BELIEF_CLAIM = "capability:lab-file-read"
REVIEW_PERMISSIONS = [
    "work.scan",
    "experience.read",
    "review.generate",
    "review.approve",
    "review.reject",
    "result.read",
]

COUNTED_TABLES = (
    "collab_interactions",
    "collab_inputs",
    "collab_artifacts",
    "model_attempts",
    "context_snapshots",
    "pending_work",
    "work_progress",
    "processed_commands",
    "records",
    "record_versions",
    "event_journal",
)


def manifest() -> dict[str, Any]:
    return {
        "provider": PROVIDER,
        "base_url": "http://127.0.0.1:9/v1",
        "model": MODEL,
        "api_key_env": MODEL_KEY_ENV,
        "max_calls": 64,
        "max_output_tokens_per_call": 512,
        "max_wall_seconds": 120,
        "thinking": "disabled",
        "temperature": 0,
    }


def reply_script(reply: str = "合成回答：已核对可披露材料。") -> list[dict[str, Any]]:
    return [
        {"action": "query", "brief_reason": "deterministic belief lookup"},
        {"action": "none", "reply": reply},
    ]


def review_proposal(
    event_id: str,
    *,
    claim: str = LAB_BELIEF_CLAIM,
    claim_type: str = "capability",
    strength: str = "moderate",
    strength_reason: str = "synthetic desktop review fixture",
    scope: dict[str, Any] | None = None,
    valid_until: str | None = None,
) -> dict[str, Any]:
    """One synthetic ``propose_belief`` output for the scripted review model."""

    body = {
        "claim": claim,
        "claim_type": claim_type,
        "evidence_links": [
            {
                "ref": {
                    "ref_type": "event",
                    "namespace": NAMESPACE,
                    "event_id": event_id,
                },
                "relation": "supports",
                "origin_group_id": f"event:{event_id}",
                "applicability_reason": "synthetic desktop review fixture",
            }
        ],
        "strength": strength,
        "strength_reason": strength_reason,
        "inference_basis": "observation",
        "premise_refs": [],
        "alternative_group_id": None,
        "valid_until": valid_until,
        "review_condition": "review when the synthetic fixture changes",
    }
    return {
        "operation": "propose_belief",
        "body": body,
        "scope": {"scene": "lab.synthetic"} if scope is None else dict(scope),
    }


def lab_belief_record_id() -> str:
    """The deterministic record id the Lab seed uses for its synthetic Belief."""

    import uuid

    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"desktop-lab-belief:{LAB_BELIEF_CLAIM}")
    )


def seed_work(
    db_path: Path,
    *,
    managed: bool = True,
    event_type: str = "CapabilityObserved:review",
    privacy_class: str = "private",
) -> dict[str, Any]:
    """Create one real pending Work outside the sidecar, then close the store.

    This is test-seeding of the trusted host's own Work (an operational
    fixture), not an alternate review implementation.  A managed Work has the
    exact production ``collab_work_control`` row; a legacy Work deliberately has
    none.
    """

    store = CognitionStore.open(db_path)
    try:
        receipt = store.ingest_event(
            namespace=NAMESPACE,
            source_kind="host_observation",
            source_instance="desktop-review-test",
            source_event_id=f"review-source-{event_type}-{time.time_ns()}",
            event_type=event_type,
            payload={"text": "synthetic desktop review event"},
            privacy_class=privacy_class,
            observed_at=utc_now(),
        )
        works = [
            item
            for item in store.unfinished_work_for_event(NAMESPACE, receipt.event_id)
        ]
        if not works:
            raise RuntimeError("the seeded event produced no pending Work")
        work = works[0]
        if managed:
            store.register_work_control(
                namespace=NAMESPACE,
                work_id=work.work_id,
                generation=0,
            )
        return {
            "event_id": receipt.event_id,
            "work_id": work.work_id,
            "kind": work.kind,
            "status": work.status,
            "created_at": work.created_at,
        }
    finally:
        store.close()


def utc_now() -> str:
    from quintessence_pcs.foundation.timeutil import utc_now_rfc3339

    return utc_now_rfc3339()


_UNSET = object()


def config_document(
    database: Path,
    *,
    script: list[dict[str, Any]] | None = None,
    query_plan: Any = _UNSET,
    external: bool = True,
    delay_ms: int = 0,
    profile: str = "lab",
    transport_kind: str = "scripted",
    max_question_chars: int = 8000,
    privacy_classes: list[str] | None = None,
    purposes: list[str] | None = None,
    review_permissions: list[str] | None = None,
    review_script: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if query_plan is _UNSET:
        query_plan = {
            "kind": "belief_current",
            "claim_types": ["capability"],
            "scope": {"scene": "lab.synthetic"},
            "limit": 5,
        }
    transport: dict[str, Any] = {"kind": transport_kind}
    if transport_kind == "scripted":
        transport["script"] = (
            reply_script() if script is None else list(script)
        )
        transport["delay_ms"] = delay_ms
        if review_script is not None:
            transport["review_script"] = list(review_script)
    if purposes is None:
        purposes = ["consult", "reasoning"]
        if review_permissions:
            purposes.append("review")
    document: dict[str, Any] = {
        "schema": "desktop.host.v1",
        "app": {"profile": profile, "label": "QUINTESSENCE Lab"},
        "store": {"database": str(database)},
        "host": {
            "namespace": NAMESPACE,
            "host_instance": HOST_INSTANCE,
            "operator": OPERATOR,
            "access_ref": ACCESS_REF,
            "privacy_classes": privacy_classes or ["private"],
            "purposes": purposes,
            "disclose": True,
            "retention": "standard",
            "policy_ref": POLICY_REF,
        },
        "consult": {
            "query_plan": query_plan,
            "max_question_chars": max_question_chars,
        },
        "destination": {
            "provider": PROVIDER,
            "model": MODEL,
            "model_protocol": "collab.v1",
            "external": external,
            "manifest": manifest(),
            "transport": transport,
        },
    }
    if review_permissions is not None:
        document["review"] = {"permissions": list(review_permissions)}
    if profile == "lab":
        document["lab"] = {
            "seed": True,
            "profile_id": "example.synthetic.desktop.lab",
            "profile_version": "0.0.1",
            "authorization_ref": "authorization:desktop-lab-model",
            "belief": {
                "claim": LAB_BELIEF_CLAIM,
                "claim_type": "capability",
                "strength": "strong",
                "scope": {"scene": "lab.synthetic"},
                "reason": "synthetic desktop Lab fixture; not a real belief",
            },
        }
    return document


def write_config(
    directory: Path, document: dict[str, Any], *, name: str = "host.json"
) -> Path:
    path = directory / name
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


class ProtocolFailure(AssertionError):
    pass


class _Tee:
    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.raw = bytearray()
        self.lock = threading.Lock()

    def read(self, size: int) -> bytes:
        data = self.stream.read(size)
        with self.lock:
            self.raw.extend(data)
        return data

    def snapshot(self) -> bytes:
        with self.lock:
            return bytes(self.raw)


class BridgeProcess:
    """A real sidecar child process with framed stdio helpers."""

    def __init__(
        self,
        *,
        config_path: str | Path,
        executable: str | Path | None = None,
        extra_env: dict[str, str] | None = None,
        verbose: bool = False,
    ) -> None:
        self.config_path = Path(config_path)
        self.verbose = verbose
        self._next_id = 0
        self.stderr_lines: list[str] = []
        self._inbox: dict[str | None, list[dict[str, Any]]] = {}
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()

        env = os.environ.copy()
        env.pop("PYTHONHOME", None)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(SRC_DIR), str(SIDECAR_DIR), env.get("PYTHONPATH", "")]
        ).strip(os.pathsep)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if extra_env:
            env.update(extra_env)
        self.env = env

        if executable is None:
            command = [sys.executable, str(SIDECAR_DIR / "pcs_bridge.py")]
            cwd = str(SIDECAR_DIR)
        else:
            command = [str(Path(executable).resolve())]
            cwd = str(Path(executable).resolve().parent)
        creationflags = 0
        if os.name == "nt":
            creationflags = 0x08000000  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            command + ["--config", str(self.config_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            bufsize=0,
            creationflags=creationflags,
        )
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        assert self.proc.stderr is not None
        self._tee = _Tee(self.proc.stdout)
        self._reader = threading.Thread(
            target=self._read_stdout, name="stdout-reader", daemon=True
        )
        self._stderr = threading.Thread(
            target=self._read_stderr, name="stderr-reader", daemon=True
        )
        self._reader.start()
        self._stderr.start()

    def _read_stdout(self) -> None:
        while not self._stop.is_set():
            try:
                body = framing.read_frame(self._tee)
            except Exception as exc:  # noqa: BLE001 - surfaced to the test
                self._responses.put(
                    {
                        "v": "internal.test",
                        "id": None,
                        "ok": False,
                        "error": {
                            "code": "TEST_READ_ERROR",
                            "message": type(exc).__name__,
                            "result_unknown": True,
                        },
                    }
                )
                return
            if body is None:
                return
            try:
                document = json.loads(body.decode("utf-8"))
            except ValueError:
                self._responses.put(
                    {
                        "v": "internal.test",
                        "id": None,
                        "ok": False,
                        "error": {
                            "code": "TEST_DECODE_ERROR",
                            "message": "response was not JSON",
                            "result_unknown": True,
                        },
                    }
                )
                return
            self._responses.put(document)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in iter(self.proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            self.stderr_lines.append(line)
            if self.verbose:
                print(f"[sidecar stderr] {line}")

    def send_raw(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def send(self, document: dict[str, Any]) -> None:
        self.send_raw(framing.encode_frame(document))

    def send_request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> str:
        with self._lock:
            self._next_id += 1
            request_id = f"t-{self._next_id}"
        self.send(
            {
                "v": "desktop.v1",
                "id": request_id,
                "method": method,
                "params": {} if params is None else params,
            }
        )
        return request_id

    def wait_for(
        self, request_id: str | None, *, timeout: float = 60.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._lock:
            buffered = self._inbox.get(request_id)
            if buffered:
                return buffered.pop(0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no response for {request_id!r}")
            try:
                document = self._responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"no response for {request_id!r}") from exc
            if document.get("id") == request_id:
                return document
            with self._lock:
                self._inbox.setdefault(document.get("id"), []).append(document)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        return self.wait_for(self.send_request(method, params), timeout=timeout)

    def wait_ready(self, *, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any("bridge ready" in line for line in self.stderr_lines):
                return
            if self.proc.poll() is not None:
                raise RuntimeError(
                    "sidecar exited before ready: "
                    + " | ".join(self.stderr_lines[-5:])
                )
            time.sleep(0.02)
        raise TimeoutError("sidecar did not become ready")

    def close_stdin(self) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def wait_exit(self, *, timeout: float = 30.0) -> int:
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            code = self.proc.wait(timeout=10)
        self._join()
        return code

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()

    def _join(self) -> None:
        self._reader.join(timeout=5)
        self._stderr.join(timeout=5)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.close_stdin()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.kill()
                self.proc.wait(timeout=10)
        self._stop.set()
        self._join()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def raw_stdout(self) -> bytes:
        return self._tee.snapshot()

    def parsed_stdout(self) -> list[dict[str, Any]]:
        raw = self.raw_stdout()
        documents: list[dict[str, Any]] = []
        offset = 0
        while offset < len(raw):
            if len(raw) - offset < 4:
                raise ProtocolFailure(f"{len(raw) - offset} trailing byte(s) after frames")
            length = int.from_bytes(raw[offset : offset + 4], "big")
            offset += 4
            if length <= 0 or length > framing.MAX_FRAME_BYTES:
                raise ProtocolFailure(f"invalid frame length {length}")
            if offset + length > len(raw):
                raise ProtocolFailure("stdout ends inside a declared frame")
            body = raw[offset : offset + length]
            offset += length
            documents.append(json.loads(body.decode("utf-8")))
        return documents


class DesktopCase(unittest.TestCase):
    """Base class: a fresh temporary store and optional sidecar process."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="desktop-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.db_path = self.tmp_path / "desktop.sqlite"
        self.config_path = write_config(
            self.tmp_path, config_document(self.db_path)
        )
        self.processes: list[BridgeProcess] = []

    def tearDown(self) -> None:
        for process in reversed(self.processes):
            process.close()

    def start(
        self,
        *,
        config_path: str | Path | None = None,
        executable: str | Path | None = None,
        extra_env: dict[str, str] | None = None,
        db_path: str | Path | None = None,
        document: dict[str, Any] | None = None,
        verbose: bool = False,
    ) -> BridgeProcess:
        path = None if config_path is None else Path(config_path)
        if path is None:
            if document is not None:
                path = write_config(
                    self.tmp_path,
                    document,
                    name=f"host-{len(self.processes)}.json",
                )
            else:
                path = self.config_path
        if db_path is not None:
            self.db_path = Path(db_path)
        process = BridgeProcess(
            config_path=path,
            executable=executable,
            extra_env=extra_env,
            verbose=verbose,
        )
        self.processes.append(process)
        process.wait_ready()
        return process

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        if not self.db_path.exists():
            return []
        try:
            connection = sqlite3.connect(str(self.db_path))
            connection.execute("PRAGMA query_only = ON")
        except sqlite3.OperationalError:
            return []
        connection.row_factory = sqlite3.Row
        try:
            return list(connection.execute(sql, params))
        except sqlite3.OperationalError:
            return []
        finally:
            connection.close()

    def scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        rows = self.query(sql, params)
        return None if not rows else rows[0][0]

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in COUNTED_TABLES:
            exists = self.scalar(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            counts[table] = 0 if not exists else int(self.scalar(f"SELECT COUNT(*) FROM {table}"))
        return counts

    def wait_until(self, predicate: Any, *, timeout: float = 30.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        raise TimeoutError("condition was not met before the deadline")


__all__ = [
    "ACCESS_REF",
    "BridgeProcess",
    "COUNTED_TABLES",
    "DesktopCase",
    "HOST_INSTANCE",
    "LAB_BELIEF_CLAIM",
    "MODEL",
    "MODEL_KEY_ENV",
    "NAMESPACE",
    "OPERATOR",
    "POLICY_REF",
    "PROVIDER",
    "ProtocolFailure",
    "REVIEW_PERMISSIONS",
    "config_document",
    "framing",
    "lab_belief_record_id",
    "manifest",
    "reply_script",
    "review_proposal",
    "seed_work",
    "utc_now",
    "write_config",
]
