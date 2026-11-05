"""The bounded tool loop.

The loop is driven by ``finish_reason``, never by "did the model say
something": a tool-call round normally carries an empty ``content``, so an
emptiness test would end the conversation one round too early on every tool
call.

Bounds are engineering guards, not permission gates:

* ``MAX_TOOL_STEPS`` rounds, after which the model gets one wrap-up turn with
  the tools removed, and is then stopped;
* a wall-clock deadline;
* a repetition guard for a model that keeps calling the same tool with the same
  arguments.

Every call is recorded with a digest of its arguments, so the audit trail shows
*that* the model asked, without copying user or web content into the log.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .agent_protocol import (
    AgentPort,
    StepOutcome,
    tool_call_message,
    tool_result_message,
)
from .model_port import ModelConfigurationError, ModelBudgetExceededError
from .tool_catalog import ToolCatalog
from ..foundation.jsonutil import canonical_json_text

#: How many tool rounds one request may take before the wrap-up turn.
MAX_TOOL_STEPS = 8

#: Identical (tool, arguments) calls in a row that mean "stuck".
MAX_REPEATED_CALLS = 3

#: Default wall-clock budget for the whole loop.
DEFAULT_DEADLINE_SECONDS = 300.0

WRAP_UP_MESSAGE = (
    "工具预算已用尽。不要再请求调用工具，也不要返回空内容。"
    "请只用已经取得的材料直接回答；如果材料不足以回答，"
    "明确说明你已经查到了什么、还缺什么。"
)

#: Returned when the wrap-up turn still produced no text.  An empty string is
#: not an answer, and a caller that forwards it tells the user nothing.
EXHAUSTED_REPLY = (
    "工具调用预算已用尽，我还没有得出最终结论。"
    "已经执行了 {steps} 轮工具调用（共 {calls} 次）；如需继续，请让我再试一次，"
    "或把问题拆得更具体一些。"
)

STOP_BUDGET = "budget_exceeded"
STOP_FINAL = "final"
STOP_STEPS = "steps_exhausted"
STOP_REPEATED = "repeated_calls"
STOP_TIMEOUT = "timeout"
STOP_MALFORMED = "malformed_tool_round"


@dataclass(frozen=True)
class CallRecord:
    """One executed tool call, with a digest instead of the arguments."""

    call_id: str
    tool: str
    arguments_digest: str
    ok: bool
    code: str
    duration_ms: int


@dataclass(frozen=True)
class LoopResult:
    content: str
    stopped_reason: str
    steps: int
    messages: tuple[Mapping[str, Any], ...]
    calls: tuple[CallRecord, ...] = field(default_factory=tuple)


def arguments_digest(arguments: str) -> str:
    return hashlib.sha256((arguments or "").encode("utf-8")).hexdigest()[:16]


def run_agent_loop(
    port: AgentPort,
    catalog: ToolCatalog,
    *,
    messages: Sequence[Mapping[str, Any]],
    handlers: Mapping[str, Callable[[Mapping[str, Any]], Any]],
    max_steps: int = MAX_TOOL_STEPS,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    tool_budget: Any = None,
    request_preview: Any = None,
    on_delivered: Callable[[Mapping[str, Any]], None] | None = None,
) -> LoopResult:
    """Run until the model stops, the budget ends, or something goes wrong.

    ``request_preview`` is host-supplied and answers one question before every
    send: would the request I am about to build fit the whole-request budget?
    Asking first is what makes the refusal local -- nothing is sent, and no
    attempt is reserved, to discover that it does not fit.

    ``on_delivered`` receives the payload of every result whose *body* entered
    the history.  A ref-only projection is not passed on: the model was told an
    id exists, not what it says, so the caller must not register it as citable
    evidence.
    """

    if type(max_steps) is not int or max_steps < 1:
        raise ModelConfigurationError("max_steps must be a positive integer")
    history: list[Mapping[str, Any]] = [dict(item) for item in messages]
    definitions = catalog.definitions()
    calls: list[CallRecord] = []
    started = clock()
    last_key: tuple[str, str] | None = None
    repeats = 0
    steps = 0

    while steps < max_steps:
        if clock() - started > deadline_seconds:
            return LoopResult("", STOP_TIMEOUT, steps, tuple(history), tuple(calls))
        if request_preview is not None:
            decision = request_preview.check(history, definitions)
            if not decision.fits:
                # The next request cannot be built inside the budget: stop
                # before sending rather than let the adapter refuse mid-round.
                return LoopResult(
                    _budget_message(decision), STOP_BUDGET, steps, tuple(history), tuple(calls)
                )
        steps += 1
        try:
            outcome = port.step(messages=history, tools=definitions)
        except ModelBudgetExceededError as exc:
            return _budget_refusal(exc, steps, history, calls)
        if not outcome.is_final() and not outcome.tool_calls:
            # finish_reason says "tool_calls" but none arrived, or the provider
            # used a reason we do not recognise: stop rather than spin.
            return LoopResult(
                outcome.content, STOP_MALFORMED, steps, tuple(history), tuple(calls)
            )
        if outcome.is_final():
            history.append(_assistant_text(outcome))
            return LoopResult(outcome.content, STOP_FINAL, steps, tuple(history), tuple(calls))
        if len(outcome.tool_calls) > 8:
            return LoopResult("", STOP_MALFORMED, steps, tuple(history), tuple(calls))
        round_messages: list[Mapping[str, Any]] = [tool_call_message(outcome)]
        round_bodies: list[Mapping[str, Any]] = []
        if tool_budget is not None:
            tool_budget.begin_round([call.name for call in outcome.tool_calls])
        for call in outcome.tool_calls:
            key = (call.name, arguments_digest(call.arguments))
            if key == last_key:
                repeats += 1
            else:
                repeats = 1
                last_key = key
            if repeats > MAX_REPEATED_CALLS:
                return LoopResult("", STOP_REPEATED, steps, tuple(history), tuple(calls))
            began = clock()
            if tool_budget is not None and not tool_budget.can_execute():
                # Admission happens before the call: a result we could not
                # deliver is not worth the side effect, and "not executed" must
                # never be reported as executed.
                projection = tool_budget.refusal(tool=call.name)
                calls.append(
                    CallRecord(
                        call_id=call.call_id,
                        tool=call.name,
                        arguments_digest=key[1],
                        ok=False,
                        code=str(projection.payload.get("code", "")),
                        duration_ms=0,
                    )
                )
                round_messages.append(
                    tool_result_message(
                        call_id=call.call_id,
                        content=canonical_json_text(projection.payload),
                    )
                )
                continue
            result = catalog.execute(call, handlers=handlers)
            calls.append(
                CallRecord(
                    call_id=call.call_id,
                    tool=call.name,
                    arguments_digest=key[1],
                    ok=result.ok,
                    code=result.code,
                    duration_ms=int((clock() - began) * 1000),
                )
            )
            raw = result.content()
            if tool_budget is None:
                content = raw
            else:
                projection = tool_budget.project(
                    tool=call.name, payload=result.payload, content=raw
                )
                content = (
                    raw
                    if projection.body_included
                    else canonical_json_text(projection.payload)
                )
            round_messages.append(tool_result_message(call_id=call.call_id, content=content))
            round_bodies.append(dict(result.payload))

        if request_preview is not None:
            # Preview the round as a whole before it becomes history.  A round
            # that cannot be sent is first re-shaped to identity-only results;
            # if even that does not fit, the round is not committed at all.
            candidate = tuple(history) + tuple(round_messages)
            decision = request_preview.check(candidate, definitions)
            if not decision.fits and tool_budget is not None:
                bodies = round_bodies
                round_messages = [round_messages[0]]
                round_bodies = []
                for call, body in zip(outcome.tool_calls, bodies):
                    shrunk = tool_budget.describe_only(tool=call.name, payload=body)
                    round_messages.append(
                        tool_result_message(
                            call_id=call.call_id,
                            content=canonical_json_text(shrunk.payload),
                        )
                    )
                decision = request_preview.check(
                    tuple(history) + tuple(round_messages), definitions
                )
            if not decision.fits:
                return LoopResult(
                    _budget_message(decision), STOP_BUDGET, steps, tuple(history), tuple(calls)
                )
        history.extend(round_messages)
        if on_delivered is not None:
            for message in round_messages[1:]:
                try:
                    payload = json.loads(str(message.get("content") or ""))
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, Mapping):
                    on_delivered(payload)

    # One wrap-up turn with the tools removed: the model gets to say what it
    # knows and what it is missing instead of being cut off mid-thought.
    history.append({"role": "user", "content": WRAP_UP_MESSAGE})
    if clock() - started > deadline_seconds:
        return LoopResult("", STOP_TIMEOUT, steps, tuple(history), tuple(calls))
    # The wrap-up withdraws the tools, and an agent-mode adapter refuses a
    # request that carries no tool schema at all, so there is no HTTP body to
    # price here: the loop's own deadline and the adapter's gate still apply.
    try:
        outcome = port.step(messages=history, tools=())
    except ModelBudgetExceededError as exc:
        return _budget_refusal(exc, steps, history, calls)
    except ModelConfigurationError:
        return LoopResult(_exhausted(steps, calls), STOP_STEPS, steps, tuple(history), tuple(calls))
    if outcome.tool_calls:
        # The model asked for a tool after the tools were withdrawn.  It gets
        # the deterministic account instead of an empty string.
        return LoopResult(_exhausted(steps, calls), STOP_STEPS, steps, tuple(history), tuple(calls))
    history.append(_assistant_text(outcome))
    content = (outcome.content or "").strip()
    if not content:
        # An empty wrap-up is not an answer; say what happened instead.
        content = _exhausted(steps, calls)
    return LoopResult(content, STOP_STEPS, steps, tuple(history), tuple(calls))


def _budget_message(decision) -> str:
    """What the model is told when the next request would not fit."""

    return (
        "请求已因预算不足停止，超限请求未发送。"
        f"该请求需要 {decision.required_units} U，可用的准入上限是 "
        f"{decision.admission_units} U（总预算 {decision.budget_units} U，"
        "其中为后续回合保留了继续空间）。可缩小工作集、改为顺序精确读取，"
        "或由宿主提高预算配置；原始历史仍保留。"
    )


def _budget_refusal(error, steps, history, calls):
    content = ("请求已因预算不足停止，超限请求未发送。" + str(error)
               + "。可缩小工作集、结束当前话题，或由宿主提高预算配置；原始历史仍保留。")
    return LoopResult(content, STOP_BUDGET, steps, tuple(history), tuple(calls))


def _exhausted(steps: int, calls: Sequence["CallRecord"]) -> str:
    return EXHAUSTED_REPLY.format(steps=steps, calls=len(calls))


def _assistant_text(outcome: StepOutcome) -> dict[str, Any]:
    return {"role": "assistant", "content": outcome.content}


__all__ = [
    "CallRecord",
    "DEFAULT_DEADLINE_SECONDS",
    "LoopResult",
    "MAX_REPEATED_CALLS",
    "MAX_TOOL_STEPS",
    "STOP_FINAL",
    "STOP_MALFORMED",
    "STOP_REPEATED",
    "STOP_STEPS",
    "STOP_TIMEOUT",
    "WRAP_UP_MESSAGE",
    "arguments_digest",
    "run_agent_loop",
]
