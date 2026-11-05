"""The agent-mode protocol: one conversation round with tools available.

This module is deliberately separate from ``model_port``.  ``ModelPort.propose``
is a single-text-in / single-JSON-out contract that the proposal path depends
on; widening it would change that path's guarantees.  Agent mode needs a
different shape -- a message list that grows, assistant turns that carry
``reasoning_content`` and ``tool_calls``, and a loop driven by ``finish_reason``
-- so it gets its own port and its own request builder.

Both builders are exported; only the agent one may carry ``tools``.
"""

from __future__ import annotations

import re

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .manifest import (
    TOOL_MODE_AGENT,
    TOOL_MODE_FREE,
    TOOL_MODES,
    ModelManifest,
    _reject_tool_keys,
)
from .model_port import ModelConfigurationError
from ..foundation.jsonutil import canonical_json_text

#: One round may ask for at most this many tools.  A provider that returns more
#: is not wrong, but the loop refuses to execute an unbounded batch.
MAX_TOOL_CALLS_PER_ROUND = 8

#: Message keys the agent path understands.  Anything else is refused rather
#: than silently dropped: a dropped ``tool_call_id`` would break the next round.
_USER_KEYS = frozenset({"role", "content"})
_ASSISTANT_KEYS = frozenset({"role", "content", "reasoning_content", "tool_calls"})
_TOOL_KEYS = frozenset({"role", "content", "tool_call_id"})

_ROLES = ("user", "assistant", "tool")


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation requested by the model."""

    call_id: str
    name: str
    arguments: str

    def arguments_mapping(self) -> Mapping[str, Any]:
        """Parse ``arguments`` as a JSON object."""

        import json

        try:
            parsed = json.loads(self.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ModelConfigurationError(
                "tool call arguments are not valid JSON"
            ) from exc
        if not isinstance(parsed, Mapping):
            raise ModelConfigurationError("tool call arguments must be a JSON object")
        return parsed


@dataclass(frozen=True)
class StepOutcome:
    """What one agent round produced.

    ``content`` may be empty while ``tool_calls`` is not: that is a normal
    tool-call round.  Callers must branch on ``finish_reason``, never on
    ``content``.
    """

    finish_reason: str
    content: str
    reasoning_content: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)

    def is_final(self) -> bool:
        return self.finish_reason == "stop"


@runtime_checkable
class AgentPort(Protocol):
    """One conversation step.  Implementations own transport and budgeting."""

    def step(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> StepOutcome: ...


def tool_call_message(outcome: StepOutcome) -> dict[str, Any]:
    """The assistant turn to append after a tool-call round.

    ``reasoning_content`` is preserved on purpose: the provider requires it to
    be echoed back while ``tools`` is present, and the live probe showed the
    server does **not** reject a request that omits it, so nothing downstream
    would catch the loss.
    """

    return {
        "role": "assistant",
        "content": outcome.content,
        "reasoning_content": outcome.reasoning_content,
        "tool_calls": [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in outcome.tool_calls
        ],
    }


def tool_result_message(*, call_id: str, content: str) -> dict[str, Any]:
    """The ``role: "tool"`` turn that answers one call."""

    if not isinstance(call_id, str) or not call_id:
        raise ModelConfigurationError("tool result needs the call id it answers")
    if not isinstance(content, str):
        raise ModelConfigurationError("tool result content must be text")
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def validate_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return a normalised copy, or refuse.

    Refusals are structural, not stylistic: a message whose ``role`` is unknown,
    or whose keys do not match its role, would produce a request the provider
    reads differently from what we logged.
    """

    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise ModelConfigurationError("messages must be a sequence")
    if not messages:
        raise ModelConfigurationError("messages must not be empty")
    normalised: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise ModelConfigurationError(f"messages[{index}] must be an object")
        role = message.get("role")
        if role not in _ROLES:
            raise ModelConfigurationError(f"messages[{index}].role is unknown")
        keys = set(message)
        if role == "user":
            allowed = _USER_KEYS
        elif role == "assistant":
            allowed = _ASSISTANT_KEYS
        else:
            allowed = _TOOL_KEYS
        extra = keys - allowed
        if extra:
            raise ModelConfigurationError(
                f"messages[{index}] carries unsupported keys: {sorted(extra)}"
            )
        if role != "assistant" and message.get("content") is None:
            raise ModelConfigurationError(f"messages[{index}].content is required")
        normalised.append(dict(message))
    return normalised


def build_agent_request_body(
    manifest: ModelManifest,
    *,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    max_tokens: int,
) -> str:
    """Build the exact request text for one agent round.

    Only a manifest whose ``tool_mode`` is ``agent`` may reach this function's
    tool branch; every other manifest is refused, so a manifest cannot gain
    tools by being passed to the wrong builder.
    """

    if getattr(manifest, "tool_mode", TOOL_MODE_FREE) != TOOL_MODE_AGENT:
        raise ModelConfigurationError(
            "manifest is not in agent mode; tools require tool_mode='agent'"
        )
    if type(max_tokens) is not int or max_tokens < 1:
        raise ModelConfigurationError("max_tokens must be a positive integer")
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
        raise ModelConfigurationError("tools must be a sequence")
    prepared_tools = _validate_tools(tools)
    body: dict[str, Any] = {
        "model": manifest.model,
        "messages": _wire_messages(validate_messages(messages)),
        "tools": prepared_tools,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if manifest.thinking == "enabled":
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = manifest.reasoning_effort or "high"
    else:
        body["thinking"] = {"type": "disabled"}
        body["temperature"] = 0
    return canonical_json_text(body)


#: The provider accepts only ``^[a-zA-Z0-9_-]+$`` in ``function.name``.  The
#: catalog's own names are dotted (``cognition.belief.read``) because that is
#: how they read in logs and tests, so the wire form is derived and reversed
#: here.  ``__`` is the separator: no catalog name may contain it, otherwise a
#: round trip would be ambiguous.
_WIRE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
_WIRE_SEPARATOR = "__"


def to_wire_name(name: str) -> str:
    """Map one catalog tool name onto the provider's accepted charset."""

    if not isinstance(name, str) or not name:
        raise ModelConfigurationError("tool name must be a non-empty string")
    if _WIRE_SEPARATOR in name:
        raise ModelConfigurationError(
            f"tool name {name!r} contains {_WIRE_SEPARATOR!r}; the wire mapping "
            "would not be reversible"
        )
    wire = name.replace(".", _WIRE_SEPARATOR)
    if not _WIRE_NAME_PATTERN.match(wire):
        raise ModelConfigurationError(
            f"tool name {name!r} maps to {wire!r}, which the provider rejects"
        )
    return wire


def from_wire_name(wire: str) -> str:
    """Map a provider-side ``function.name`` back onto the catalog name."""

    if not isinstance(wire, str) or not wire:
        raise ModelConfigurationError("a tool call must name a function")
    return wire.replace(_WIRE_SEPARATOR, ".")


def _wire_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rewrite assistant tool-call names into their wire form for replay."""

    out: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        calls = item.get("tool_calls")
        if isinstance(calls, list) and calls:
            rewritten = []
            for call in calls:
                clone = dict(call)
                function = clone.get("function")
                if isinstance(function, Mapping):
                    fn = dict(function)
                    fn["name"] = to_wire_name(str(fn.get("name") or ""))
                    clone["function"] = fn
                rewritten.append(clone)
            item["tool_calls"] = rewritten
        out.append(item)
    return out


def _validate_tools(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not tools:
        raise ModelConfigurationError("agent mode requires at least one tool")
    prepared: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, tool in enumerate(tools):
        if not isinstance(tool, Mapping):
            raise ModelConfigurationError(f"tools[{index}] must be an object")
        if set(tool) != {"type", "function"}:
            raise ModelConfigurationError(f"tools[{index}] has unexpected keys")
        if tool.get("type") != "function":
            raise ModelConfigurationError(f"tools[{index}].type must be 'function'")
        function = tool.get("function")
        if not isinstance(function, Mapping):
            raise ModelConfigurationError(f"tools[{index}].function must be an object")
        name = function.get("name")
        if not isinstance(name, str) or not name or len(name) > 128:
            raise ModelConfigurationError(f"tools[{index}].function.name is invalid")
        if name in names:
            raise ModelConfigurationError(f"duplicate tool name {name!r}")
        names.add(name)
        parameters = function.get("parameters")
        if not isinstance(parameters, Mapping):
            raise ModelConfigurationError(f"tools[{index}].function.parameters must be an object")
        _reject_authority_keys(parameters, where=f"tools[{index}].function.parameters")
        prepared.append(
            {
                "type": "function",
                "function": {
                    "name": to_wire_name(name),
                    "description": str(function.get("description") or ""),
                    "parameters": dict(parameters),
                },
            }
        )
    return prepared


#: Authority, identity and permission fields belong to the host.  A tool
#: *schema* that asks the model for one of these is itself the bug: the model
#: would be inventing permission.  The catalog refuses to publish such a tool.
AUTHORITY_FIELDS = frozenset(
    {
        "authority",
        "authority_kind",
        "approval_id",
        "auto_authorization_id",
        "grant_id",
        "actor",
        "namespace",
        "scope",
        "permissions",
        "role",
    }
)


def _reject_authority_keys(value: Any, *, where: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in AUTHORITY_FIELDS:
                raise ModelConfigurationError(
                    f"{where} must not expose the authority field {key!r} to the model"
                )
            _reject_authority_keys(child, where=where)
    elif isinstance(value, list):
        for child in value:
            _reject_authority_keys(child, where=where)
