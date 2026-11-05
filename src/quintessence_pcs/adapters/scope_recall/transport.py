"""Subprocess transport for the minimal Scope Recall adapter.

The same pinned CLI entry serves three frozen actions: ``capture`` (write),
``search`` (read-only browse) and ``recall`` (read-only automatic recall).  This
module only builds the request, runs the bounded subprocess and normalizes the
envelope; it never reads a store, a database file or a credential.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class AdapterConfig:
    python_executable: str
    entry: str
    package_path: str
    data_directory: str
    agent_id: str
    installation_id: str
    scope_ids: tuple[str, ...]
    scope_id: str
    session_id: str
    test_mode: bool = True


class SubprocessCaptureAdapter:
    """Run one bounded Scope Recall CLI invocation and normalize its result."""

    def __init__(self, config: AdapterConfig):
        self._config = config

    def _binding(self) -> dict[str, Any]:
        return {
            **({"test_mode": False} if not self._config.test_mode else {}),
            "agent_id": self._config.agent_id,
            "installation_id": self._config.installation_id,
            "data_directory": self._config.data_directory,
            "scope_ids": list(self._config.scope_ids),
        }

    def capture(
        self,
        event: dict[str, Any],
        *,
        actor_origin: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        if event.get("origin") != actor_origin:
            return {"kind": "blocked", "error": "origin_mismatch"}
        request = {
            "action": "capture",
            "binding": self._binding(),
            "context": {
                "session_id": self._config.session_id,
                "actor_origin": actor_origin,
            },
            "scope_id": self._config.scope_id,
            "event": event,
            "timeout_seconds": float(timeout_seconds),
        }
        return self._invoke(request, timeout_seconds)

    def search(
        self,
        *,
        query: str,
        limit: int,
        cursor: str | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Ask the CLI for one bounded read-only page; the host owns the scope.

        The WebView never supplies a scope, binding, path or executable: this
        method only forwards query, limit and an opaque continuation cursor
        under the configured binding.
        """

        request = {
            "action": "search",
            "binding": self._binding(),
            "query": query,
            "limit": limit,
            "cursor": cursor,
            "scope_id": self._config.scope_id,
            "timeout_seconds": float(timeout_seconds),
        }
        return self._invoke(request, timeout_seconds)

    def recall(
        self,
        *,
        query: str,
        max_items: int,
        budget_tokens: int,
        timeout_seconds: float,
        timezone: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Ask the CLI for one bounded automatic-recall packet.

        The host owns binding/scope/timeout.  This call's hard subprocess
        timeout is exactly ``timeout_seconds`` (no +5s grace): a 2s turn budget
        must not be extended by process startup.
        """

        request = {
            "action": "recall",
            "binding": self._binding(),
            "context": {
                "session_id": self._config.session_id,
                "actor_origin": "host_generated",
            },
            "scope_id": self._config.scope_id,
            "request_id": request_id,
            "query": query,
            "max_items": max_items,
            "budget_tokens": budget_tokens,
            "timeout_seconds": float(timeout_seconds),
            "timezone": timezone,
            "since": None,
            "until": None,
        }
        return self._invoke(request, timeout_seconds, grace_seconds=0.0)

    def time_window(
        self,
        *,
        since: str | None,
        until: str | None,
        limit: int,
        pages: int,
        timeout_seconds: float,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """One bounded read-only inventory page set over the public time filter.

        The host computes the window in its configured zone; this method only
        forwards it under the configured binding.  An event-time caller passes
        ``until=None``: the source occurrence the upstream filter reads is the
        message time, which can be later than the event itself.
        """

        request = {
            "action": "time_window",
            "binding": self._binding(),
            "scope_id": self._config.scope_id,
            "request_id": request_id,
            "since": since,
            "until": until,
            "limit": limit,
            "pages": pages,
            "timeout_seconds": float(timeout_seconds),
        }
        return self._invoke(request, timeout_seconds, grace_seconds=0.0)

    def _invoke(
        self,
        request: dict[str, Any],
        timeout_seconds: float,
        *,
        grace_seconds: float = 5.0,
    ) -> dict[str, Any]:
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = self._config.package_path
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUTF8"] = "1"
        try:
            completed = subprocess.run(
                [self._config.python_executable,
                 "--memory-adapter" if self._config.entry == "builtin:scope-recall" else self._config.entry],
                input=payload,
                capture_output=True,
                timeout=max(0.05, float(timeout_seconds) + float(grace_seconds)),
                env=env,
                cwd=str(Path(self._config.data_directory).parent),
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired:
            return {"kind": "unknown", "error": "timeout"}
        except OSError:
            return {"kind": "blocked", "error": "adapter_unavailable"}
        raw = completed.stdout or b""
        if len(raw) > MAX_RESPONSE_BYTES:
            return {"kind": "error", "error": "response_too_large"}
        if completed.returncode != 0:
            return {"kind": "error", "error": "adapter_exit"}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {"kind": "error", "error": "invalid_adapter_response"}
        if not isinstance(decoded, dict) or "kind" not in decoded:
            return {"kind": "error", "error": "invalid_adapter_response"}
        return decoded


__all__ = ["AdapterConfig", "SubprocessCaptureAdapter"]
