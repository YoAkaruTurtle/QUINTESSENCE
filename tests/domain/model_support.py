"""Offline fixture for PCS2-009: a local, scripted fake provider.

The provider is a standard-library ``http.server`` bound to loopback.  It never
contacts the internet and it never logs request headers.  Tests can inspect the
exact request bytes it received, which is what makes "request_hash matches the
bytes on disk" a real comparison instead of a self-referential assertion.

The API key is generated per test, stored only in the process environment and
compared in memory.  No test writes it to a database, a fixture file or an
error message.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import (  # noqa: E402
    NS,
    NS_B,
    CognitionTestCase,
    all_text_cells,
)

from quintessence_pcs.foundation.jsonutil import canonical_json_text  # noqa: E402
from quintessence_pcs.runtime import (  # noqa: E402
    CognitionRuntime,
    DirectHttpModel,
    ModelManifest,
)

DEFAULT_USAGE = {
    "prompt_tokens": 120,
    "completion_tokens": 40,
    "total_tokens": 160,
}


def make_manifest(base_url: str, **overrides: Any) -> ModelManifest:
    values: dict[str, Any] = {
        "provider": "deepseek",
        "base_url": base_url,
        "model": "deepseek-flash",
        "api_key_env": "PCS_TEST_MODEL_API_KEY",
        "max_calls": 8,
        "max_output_tokens_per_call": 512,
        "max_wall_seconds": 30,
        "thinking": "disabled",
        "temperature": 0,
    }
    values.update(overrides)
    return ModelManifest.from_mapping(values)


@dataclass(frozen=True)
class ScriptedResponse:
    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0

    def body_bytes(self) -> bytes:
        if self.body is None:
            return b""
        if isinstance(self.body, bytes):
            return self.body
        if isinstance(self.body, str):
            return self.body.encode("utf-8")
        return canonical_json_text(self.body).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        provider = self.server.provider  # type: ignore[attr-defined]
        provider._record(self.path, dict(self.headers), body)
        response = provider._next_response()
        if response.delay:
            time.sleep(response.delay)
        payload = response.body_bytes()
        try:
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
        except OSError:
            pass

    def do_GET(self) -> None:  # noqa: N802 - redirect target detector
        provider = self.server.provider  # type: ignore[attr-defined]
        provider._record(self.path, dict(self.headers), b"")
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        try:
            self.wfile.write(b"ok")
        except OSError:
            pass

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Deliberately silent: request headers include the synthetic key.
        return


class FakeProvider:
    """One loopback HTTP server with a queue of scripted responses."""

    def __init__(self) -> None:
        self._responses: list[ScriptedResponse] = []
        self._lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.path_hits: dict[str, int] = {}
        self.request_event = threading.Event()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.block_on_close = False
        self._server.provider = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def hits(self) -> int:
        with self._lock:
            return self.path_hits.get("/chat/completions", 0)

    def enqueue(self, *responses: ScriptedResponse) -> None:
        with self._lock:
            self._responses.extend(responses)

    def _next_response(self) -> ScriptedResponse:
        with self._lock:
            if not self._responses:
                return ScriptedResponse(
                    status=500, body={"error": "no scripted response"}
                )
            return self._responses.pop(0)

    def _record(self, path: str, headers: dict[str, str], body: bytes) -> None:
        with self._lock:
            self.requests.append(
                {"path": path, "headers": headers, "body": body}
            )
            self.path_hits[path] = self.path_hits.get(path, 0) + 1
        self.request_event.set()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class ModelPortTestCase(CognitionTestCase):
    """Cognition fixture + fake provider + manifest + optional grant."""

    grant_on_setup = True
    ENV_NAME = "PCS_TEST_MODEL_API_KEY"
    AUTHORIZATION_WORDS = (
        "用户原话（合成测试）：仅授权本地回环假 provider 的离线验证，"
        "不产生任何真实模型请求或费用。"
    )

    def setUp(self) -> None:
        super().setUp()
        self.provider = FakeProvider()
        self.addCleanup(self.provider.close)
        self.sentinel_key = "sk-" + uuid.uuid4().hex
        self._previous_key = os.environ.get(self.ENV_NAME)
        os.environ[self.ENV_NAME] = self.sentinel_key
        self.addCleanup(self._restore_key)

        self.manifest = make_manifest(
            self.provider.url, api_key_env=self.ENV_NAME
        )
        self.auth = (
            self.grant_authorization(manifest=self.manifest)
            if self.grant_on_setup
            else None
        )
        self.model = DirectHttpModel(self.store, self.manifest)
        self.runtime = CognitionRuntime(
            self.store,
            model=self.model,
            belief_service=self.beliefs,
            context_service=self.contexts,
            self_view_service=self.self_view,
            clock=self.clock,
        )

    def _restore_key(self) -> None:
        if self._previous_key is None:
            os.environ.pop(self.ENV_NAME, None)
        else:
            os.environ[self.ENV_NAME] = self._previous_key

    # ------------------------------------------------------------ authorizations

    def grant_authorization(
        self,
        *,
        manifest: ModelManifest | None = None,
        namespace: str = NS,
        **overrides: Any,
    ):
        active = manifest or self.manifest
        values: dict[str, Any] = {
            "namespace": namespace,
            "authorization_ref": self.AUTHORIZATION_WORDS,
            "provider": active.provider,
            "model": active.model,
            "manifest_hash": active.manifest_hash,
            "max_calls": 8,
            "max_output_tokens_per_call": 512,
            "max_wall_seconds": 30,
        }
        values.update(overrides)
        return self.store.grant_model_authorization(**values)

    def reconfigure(self, manifest: ModelManifest, **auth_overrides: Any) -> None:
        """Replace the active manifest/auth/runtime for one test scenario."""

        for existing in self.store.model_authorizations(namespace=NS, status="active"):
            self.store.revoke_model_authorization(existing.authorization_id)
        self.manifest = manifest
        self.auth = self.grant_authorization(manifest=manifest, **auth_overrides)
        self.model = DirectHttpModel(self.store, manifest)
        self.runtime = CognitionRuntime(
            self.store,
            model=self.model,
            belief_service=self.beliefs,
            context_service=self.contexts,
            self_view_service=self.self_view,
            clock=self.clock,
        )

    # ---------------------------------------------------------------- responses

    def response(
        self,
        body: Any,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        delay: float = 0.0,
    ) -> ScriptedResponse:
        return ScriptedResponse(
            status=status,
            body=body,
            headers={} if headers is None else headers,
            delay=delay,
        )

    def proposal(
        self,
        event_id: str,
        *,
        claim: str = "capability:file-read",
        relation: str = "supports",
        namespace: str = NS,
    ) -> dict[str, Any]:
        link = {
            "ref": {
                "ref_type": "event",
                "namespace": namespace,
                "event_id": event_id,
            },
            "relation": relation,
            "origin_group_id": f"event:{event_id}",
            "applicability_reason": "synthetic loopback HTTP fixture",
        }
        body = {
            "claim": claim,
            "claim_type": "capability",
            "evidence_links": [link],
            "strength": "strong",
            "strength_reason": "synthetic loopback HTTP fixture",
            "inference_basis": "observation",
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": "review when independent capability evidence appears",
        }
        return {
            "operation": "propose_belief",
            "body": body,
            "scope": {"scene": "test.scene"},
        }

    def wait_proposal(self, reason: str = "no judgment") -> dict[str, Any]:
        return {"operation": "wait", "reason": reason}

    def enqueue_json(self, value: Any, **kwargs: Any) -> None:
        self.provider.enqueue(self.response(value, **kwargs))

    def completion(
        self,
        content: Any,
        *,
        model: str = "deepseek-flash",
        finish_reason: str | None = "stop",
        usage: Any = DEFAULT_USAGE,
        system_fingerprint: str | None = "fp_synthetic_001",
        reasoning_content: str | None = None,
        tool_calls: Any = None,
        include_usage: bool = True,
    ) -> dict[str, Any]:
        """Build a chat-completion envelope around a proposal/content string."""

        message: dict[str, Any] = {
            "role": "assistant",
            "content": content,
        }
        if reasoning_content is not None:
            message["reasoning_content"] = reasoning_content
        if tool_calls is not None:
            message["tool_calls"] = tool_calls
        document: dict[str, Any] = {
            "id": "chatcmpl-synthetic",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": message,
                }
            ],
        }
        if system_fingerprint is not None:
            document["system_fingerprint"] = system_fingerprint
        if include_usage and usage is not None:
            document["usage"] = usage
        return document

    def proposal_text(self, event_id: str, **kwargs: Any) -> str:
        return canonical_json_text(self.proposal(event_id, **kwargs))

    def enqueue_proposal(self, event_id: str, **kwargs: Any) -> None:
        self.enqueue_json(self.completion(self.proposal_text(event_id, **kwargs)))

    def enqueue_content(self, content: Any, **kwargs: Any) -> None:
        self.enqueue_json(self.completion(content, **kwargs))

    # ------------------------------------------------------------------ running

    def run_and_context(self, event_id: str):
        """Run one step and return ``(result, latest_context_or_none)``."""

        result = self.runtime.run_step(namespace=NS, trigger_event_id=event_id)
        row = self.store.connection.execute(
            "SELECT context_id FROM context_snapshots WHERE namespace = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (NS,),
        ).fetchone()
        context = (
            None if row is None else self.contexts.load_context(row["context_id"])
        )
        return result, context

    def attempts_for(self, context: Any):
        return self.store.model_attempts_for_run(context.run_id)

    def context_count(self, *, namespace: str = NS) -> int:
        return int(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM context_snapshots WHERE namespace = ?",
                (namespace,),
            ).fetchone()[0]
        )

    def assert_key_absent_from_store(self) -> None:
        for location, value in all_text_cells(self.store.connection):
            self.assertNotIn(self.sentinel_key, value, location)


class NoAuthorizationTestCase(ModelPortTestCase):
    """Same fixture, but no grant row is written."""

    grant_on_setup = False


__all__ = [
    "FakeProvider",
    "ModelPortTestCase",
    "NS",
    "NS_B",
    "NoAuthorizationTestCase",
    "ScriptedResponse",
    "make_manifest",
]
