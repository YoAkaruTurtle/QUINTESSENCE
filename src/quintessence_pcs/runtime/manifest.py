"""PCS2-009 model manifest: *how* to connect, never *whether* we may.

A manifest is a strict, small, JSON-shaped config:

``provider, base_url, model, api_key_env, max_calls,
max_output_tokens_per_call, max_wall_seconds, thinking, temperature``

It never contains the API key itself; ``api_key_env`` names the environment
variable.  The manifest hash covers exactly these fields and is what an
authorization is bound to.  A manifest existing on disk therefore proves
nothing about permission: without an active ``model_authorizations`` row the
adapter refuses before opening a socket.

The request body builder is deliberately boring and fixed by the handoff card:
one user message with the exact rendered input, thinking disabled, temperature
0, ``max_tokens`` capped by the manifest/authorization minimum, non-streaming.
It has no ``tools`` / ``functions`` key by construction, and a final recursive
check rejects any accidental one.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..foundation.jsonutil import canonical_json_text, sha256_hex, strict_json_loads
from .model_port import ModelConfigurationError

MANIFEST_FIELDS = (
    "provider",
    "base_url",
    "model",
    "api_key_env",
    "max_calls",
    "max_output_tokens_per_call",
    "max_wall_seconds",
    "thinking",
    "temperature",
)

#: PCS2-009 only verifies "can call, can record, can parse".  Thinking mode is
#: default-on upstream and makes ``temperature`` ineffective, so this card
#: pins it off.  PCS2-010 may revisit the value, not the meaning of this field.
SUPPORTED_THINKING = "disabled"
SUPPORTED_TEMPERATURE = 0

MAX_HOSTED_MODEL_TOKENS = 393216
MAX_CALLS = 1000
MAX_WALL_SECONDS = 86400

_PROVIDER = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_MODEL = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

_FORBIDDEN_REQUEST_KEYS = frozenset(
    {"tools", "functions", "tool_calls", "function_call"}
)


class ManifestError(ModelConfigurationError):
    """The manifest is structurally or semantically unusable."""


@dataclass(frozen=True)
class ModelManifest:
    """One validated connection manifest (no secret material)."""

    provider: str
    base_url: str
    model: str
    api_key_env: str
    max_calls: int
    max_output_tokens_per_call: int
    max_wall_seconds: int
    thinking: str
    temperature: int
    reasoning_effort: str | None = None
    protocol_version: str | None = None

    @classmethod
    def from_mapping(cls, value: Any) -> "ModelManifest":
        """Validate a plain JSON object; unknown/missing keys are fatal."""

        if not isinstance(value, Mapping):
            raise ManifestError("manifest must be a JSON object")
        keys = set(value)
        expected = set(MANIFEST_FIELDS)
        required = set(expected)
        if value.get("thinking") == "enabled":
            required.discard("temperature")
        missing = sorted(required - keys)
        optional = {"reasoning_effort", "protocol_version"}
        unknown = sorted(keys - expected - optional)
        if missing:
            raise ManifestError(f"manifest is missing: {', '.join(missing)}")
        if unknown:
            raise ManifestError(f"manifest has unknown keys: {', '.join(unknown)}")

        provider = _require_provider(value["provider"])
        base_url = _require_base_url(value["base_url"])
        model = _require_model(value["model"])
        api_key_env = _require_env_name(value["api_key_env"])
        max_calls = _require_int(value["max_calls"], "max_calls", 1, MAX_CALLS)
        max_tokens = _require_int(
            value["max_output_tokens_per_call"],
            "max_output_tokens_per_call",
            1,
            MAX_HOSTED_MODEL_TOKENS,
        )
        max_wall = _require_int(
            value["max_wall_seconds"], "max_wall_seconds", 1, MAX_WALL_SECONDS
        )
        thinking = value["thinking"]
        reasoning_effort = value.get("reasoning_effort")
        protocol_version = value.get("protocol_version")
        if thinking == "enabled":
            if reasoning_effort != "high":
                raise ManifestError("thinking enabled requires reasoning_effort='high'")
            if protocol_version != "EXP-01-THINKING-CANDIDATE-v1":
                raise ManifestError("thinking enabled requires the Thinking protocol version")
            if "temperature" in value:
                raise ManifestError("thinking enabled must omit temperature")
            temperature = 0
        elif thinking == "disabled":
            if reasoning_effort is not None or protocol_version is not None:
                raise ManifestError("disabled protocol must not carry Thinking fields")
            temperature = value["temperature"]
            if type(temperature) not in (int, float) or isinstance(temperature, bool):
                raise ManifestError("temperature must be the number 0")
            if temperature != SUPPORTED_TEMPERATURE:
                raise ManifestError("PCS2-009 requires temperature=0")
        else:
            raise ManifestError("thinking must be disabled or enabled")
        return cls(
            provider=provider,
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
            max_calls=max_calls,
            max_output_tokens_per_call=max_tokens,
            max_wall_seconds=max_wall,
            thinking=thinking,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            protocol_version=protocol_version,
        )

    @classmethod
    def from_json(cls, text: Any) -> "ModelManifest":
        if not isinstance(text, str):
            raise ManifestError("manifest must be JSON text")
        try:
            value = strict_json_loads(text)
        except ValueError as exc:
            raise ManifestError(f"manifest is not strict JSON: {exc}") from exc
        return cls.from_mapping(value)

    def to_dict(self) -> dict[str, Any]:
        """The exact fields covered by :attr:`manifest_hash`."""

        data = {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "max_calls": self.max_calls,
            "max_output_tokens_per_call": self.max_output_tokens_per_call,
            "max_wall_seconds": self.max_wall_seconds,
            "thinking": self.thinking,
        }
        if self.thinking != "enabled":
            data["temperature"] = self.temperature
        if self.reasoning_effort is not None:
            data["reasoning_effort"] = self.reasoning_effort
        if self.protocol_version is not None:
            data["protocol_version"] = self.protocol_version
        return data

    @property
    def canonical_text(self) -> str:
        return canonical_json_text(self.to_dict())

    @property
    def manifest_hash(self) -> str:
        """``sha256:<hex>`` over the canonical manifest fields."""

        return "sha256:" + sha256_hex(self.canonical_text)

    @property
    def endpoint(self) -> str:
        """The one chat-completions URL; the path is never redirected."""

        return self.base_url.rstrip("/") + "/chat/completions"


def build_request_body(
    manifest: ModelManifest,
    *,
    rendered_input: str,
    max_tokens: int,
    previous_output: str | None = None,
    feedback: str | None = None,
) -> str:
    """Build the exact request text (UTF-8 canonical JSON) for one HTTP try.

    First call: one user message containing exactly ``rendered_input``.  A
    format repair adds the previous assistant text and one user feedback
    message; it never adds a system prompt, an example, or a tool definition.
    """

    if not isinstance(rendered_input, str) or not rendered_input:
        raise ModelConfigurationError("rendered_input must be a non-empty string")
    if type(max_tokens) is not int or max_tokens < 1:
        raise ModelConfigurationError("max_tokens must be a positive integer")
    messages: list[dict[str, str]] = [
        {"role": "user", "content": rendered_input}
    ]
    if feedback is not None:
        if not isinstance(feedback, str) or not feedback.strip():
            raise ModelConfigurationError("feedback must be non-empty text")
        if not isinstance(previous_output, str) or not previous_output:
            raise ModelConfigurationError(
                "a format repair requires the previous assistant output"
            )
        messages.append({"role": "assistant", "content": previous_output})
        messages.append({"role": "user", "content": feedback})
    if manifest.thinking == "enabled":
        body = {
            "model": manifest.model,
            "messages": messages,
            "thinking": {"type": "enabled"},
            "reasoning_effort": manifest.reasoning_effort or "high",
            "max_tokens": max_tokens,
            "stream": False,
        }
    else:
        body = {
            "model": manifest.model,
            "messages": messages,
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
        }
    _reject_tool_keys(body)
    return canonical_json_text(body)


def _reject_tool_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in _FORBIDDEN_REQUEST_KEYS:
                raise ModelConfigurationError(
                    f"request body must not contain {key!r}; PCS2-009 is tool-free"
                )
            _reject_tool_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_tool_keys(child)


def _require_provider(value: Any) -> str:
    if not isinstance(value, str) or _PROVIDER.fullmatch(value) is None:
        raise ManifestError(
            "provider must match ^[a-z][a-z0-9._-]{0,63}$"
        )
    return value


def _require_model(value: Any) -> str:
    if not isinstance(value, str) or _MODEL.fullmatch(value) is None:
        raise ManifestError("model must be a short provider model identifier")
    return value


def _require_env_name(value: Any) -> str:
    if not isinstance(value, str) or _ENV_NAME.fullmatch(value) is None:
        raise ManifestError(
            "api_key_env must be an environment variable name, not a key value"
        )
    return value


def _require_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise ManifestError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ManifestError(f"{field} must be between {minimum} and {maximum}")
    return value


def _require_base_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError("base_url must be a non-empty string")
    if value != value.strip():
        raise ManifestError("base_url must not have leading/trailing whitespace")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ManifestError("base_url must use https:// (loopback may use http://)")
    if not parts.hostname:
        raise ManifestError("base_url must name a host")
    if parts.username is not None or parts.password is not None:
        raise ManifestError("base_url must not embed credentials")
    if parts.query or parts.fragment:
        raise ManifestError("base_url must not carry a query or fragment")
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise ManifestError(
            "non-loopback base_url must use https://; refusing to send a key in cleartext"
        )
    # Normalize the trailing slash so endpoint construction is unambiguous.
    return value.rstrip("/")


def _is_loopback(host: str) -> bool:
    lowered = host.lower()
    if lowered == "localhost":
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


__all__ = [
    "MANIFEST_FIELDS",
    "MAX_CALLS",
    "MAX_HOSTED_MODEL_TOKENS",
    "MAX_WALL_SECONDS",
    "ManifestError",
    "ModelManifest",
    "SUPPORTED_TEMPERATURE",
    "SUPPORTED_THINKING",
    "build_request_body",
]
