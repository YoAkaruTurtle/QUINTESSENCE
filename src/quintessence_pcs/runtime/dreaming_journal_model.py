"""Evidence-reading journal rounds over the existing cancellable HTTP transport."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import threading
from typing import Any

from ..foundation.jsonutil import canonical_json_text, strict_json_loads
from .agent_protocol import StepOutcome, build_agent_request_body
from .http_model import DirectHttpModel, _HttpFailure, _validate_agent_response
from .manifest import TOOL_MODE_AGENT
from .model_port import ModelCancelled


class JournalContextOverflow(Exception):
    """The provider could not accept this physical input window."""


@dataclass(frozen=True)
class JournalRound:
    outcome: StepOutcome
    continuation: bool = False


def _input_overflow(error: _HttpFailure) -> bool:
    if error.label == "HTTP_413":
        return True
    if error.label != "HTTP_400" or not error.response_body:
        return False
    # Do not try to solve a credential, model or arbitrary input rejection
    # by repeatedly submitting different portions of private source text.
    detail = error.response_body.casefold()
    return any(label in detail for label in (
        "context_length_exceeded", "maximum context length", "context window",
        "input token limit", "input is too long", "prompt is too long",
    ))


class JournalModel:
    def __init__(self, store: Any, manifest: Any, *, opener: Any = None) -> None:
        self._manifest = replace(manifest, tool_mode=TOOL_MODE_AGENT)
        self._transport = DirectHttpModel(store, self._manifest, opener=opener)

    @property
    def in_flight(self) -> bool:
        return self._transport.in_flight

    def step(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        *,
        cancel: threading.Event,
        live: Callable[[], bool],
    ) -> JournalRound:
        if cancel.is_set() or not live():
            raise ModelCancelled("the journal's source or authorization changed")
        # Use the provider's response default. The chat and legacy reflection
        # ceilings are grants for those purposes, not planner bounds here.
        body = strict_json_loads(build_agent_request_body(
            self._manifest, messages=messages, tools=tools, max_tokens=1,
        ))
        del body["max_tokens"]
        request = canonical_json_text(body)
        key = self._transport._api_key()
        if cancel.is_set() or not live():
            raise ModelCancelled("the journal's source or authorization changed")
        try:
            with self._transport.bind_cancel(cancel):
                raw = self._transport._send(
                    request, timeout=self._manifest.max_wall_seconds, api_key=key,
                )
        except _HttpFailure as exc:
            if _input_overflow(exc):
                raise JournalContextOverflow() from exc
            raise
        if cancel.is_set() or not live():
            raise ModelCancelled("the journal's source or authorization changed")
        document = strict_json_loads(raw)
        choices = document.get("choices") if isinstance(document, dict) else None
        first = choices[0] if isinstance(choices, list) and choices else None
        if isinstance(first, dict) and first.get("finish_reason") == "length":
            message = first.get("message")
            partial = message.get("content") if isinstance(message, dict) else None
            if isinstance(partial, str) and partial and not message.get("tool_calls"):
                reasoning = message.get("reasoning_content")
                return JournalRound(StepOutcome(
                    "length", partial, reasoning if isinstance(reasoning, str) else "",
                ), continuation=True)
            # Some providers exhaust output on reasoning before any usable
            # text. Smaller source windows can make progress in that case.
            raise JournalContextOverflow()
        step = _validate_agent_response(raw)
        return JournalRound(StepOutcome(
            step.finish_reason,
            step.content,
            step.reasoning_content,
            step.tool_calls,
            step.usage or {},
        ))
