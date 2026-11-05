"""RG08: controlled plain-text final fallback after a content parse failure.

``collab.v2`` asks the content stage for exactly one JSON action envelope, but a
provider can answer the user completely (``finish_reason == "stop"``) while
ignoring that envelope.  Before this module such a turn failed with
``format:format_exhausted`` even though a complete ordinary answer existed.

The fallback is deliberately narrow and format-only:

* only a complete non-empty plain-text answer is eligible;
* anything that looks like JSON, an action envelope, DSML or a tool call is
  refused, so a damaged structured output never becomes a final answer;
* a text that claims an operation already happened is refused: the fallback is
  not evidence that a tool ran, and it must not turn a model claim into a
  published fact;
* a valid tool call, an empty/truncated response and any non-``stop`` finish
  reason never reach this path.

The module is pure.  It does not read the store, call a model, extract a tool or
write anything; the orchestrator owns the event, the reply artifact and the
normal expression/publishing chain.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..runtime.collab_protocol import (
    COLLAB_V2_PROTOCOL_VERSION,
    MAX_REPLY_CHARS,
)

#: Stable event reason for the fallback; the event kind is the same token.
FALLBACK_REASON = "plain_text_final"

#: A native or DeepSeek tool envelope is never ordinary prose.
_TOOL_MARKERS = (
    "｜｜DSML｜｜",
    "<tool_call",
    "</tool_call>",
    "<function_call",
    "</function_call>",
    "<parameter",
    "</parameter>",
    "<invoke",
    "</invoke>",
)

#: A key that belongs to the action envelope.  The colon is required so normal
#: prose that mentions a word like "action" is not rejected by name alone.
_PROTOCOL_KEY = re.compile(
    r'"(?:action|tool_call|tool_calls|function_call|arguments)"\s*:'
)
_CALL_TOOL_ACTION = re.compile(r'"action"\s*:\s*"call_tool"')
_TOOL_KEY = re.compile(r'"(?:tool_call|tool_calls|function_call)"\s*:')
_FENCED_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Claims that an operation already happened.  The fallback never verified one,
#: so such text is refused instead of being published as a completed action.
#: The subject guard keeps ordinary second-person encouragement ("你已经完成")
#: from being read as the assistant's own operation claim.
_OPERATION_CLAIM = re.compile(
    r"(?<![你他她])"
    r"(?:"
    r"(?:已经|已)\s*"
    r"(?:保存|记录|记住|写入|查询|查看|查(?:过|到|了)?|完成|发送|删除|"
    r"更新|打开|读取|执行|调用|提交|发布|安装|创建|修改|处理)"
    r"|(?:我|这边)\s*(?:已经|已)?\s*"
    r"(?:保存好了|记录好了|办好了)"
    r"|查(?:到|过|好)了"
    r")"
)


@dataclass(frozen=True)
class PlainTextFinal:
    """One admitted ordinary final answer."""

    text: str
    reason: str = FALLBACK_REASON


def classify_parse_rejection(raw: Any) -> str:
    """Classify a rejected content output as ``"tool"`` or ``"reply"``.

    The distinction is public-event vocabulary only.  It never decides whether
    anything executes: a tool-shaped output was *not* parsed, so no step, no
    handler and no tool result can exist for it.
    """

    if not isinstance(raw, str) or not raw:
        return "reply"
    if any(marker in raw for marker in _TOOL_MARKERS):
        return "tool"
    try:
        document = json.loads(raw)
    except ValueError:
        if _CALL_TOOL_ACTION.search(raw) or _TOOL_KEY.search(raw):
            return "tool"
        return "reply"
    if not isinstance(document, dict):
        return "reply"
    action = document.get("action")
    if action == "call_tool":
        return "tool"
    if action in ("final", "wait"):
        return "reply"
    if action is None and any(
        key in document for key in ("tool_call", "tool_calls", "function_call")
    ):
        return "tool"
    return "reply"


def plain_text_final(
    raw: Any,
    *,
    finish_reason: str | None,
    protocol_version: str,
) -> PlainTextFinal | None:
    """Return the admitted text, or ``None`` when the format is not safe."""

    if protocol_version != COLLAB_V2_PROTOCOL_VERSION:
        return None
    if finish_reason != "stop":
        return None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > MAX_REPLY_CHARS:
        return None
    if not any(char.isalnum() for char in text):
        return None
    if _CONTROL.search(text):
        return None
    if _looks_structured(text):
        return None
    if _OPERATION_CLAIM.search(text):
        return None
    return PlainTextFinal(text=text)


def _looks_structured(text: str) -> bool:
    if text[:1] in ("{", "["):
        return True
    try:
        json.loads(text)
    except ValueError:
        pass
    else:
        # Even a bare JSON string/number is not an ordinary final answer.
        return True
    if any(marker in text for marker in _TOOL_MARKERS):
        return True
    if _PROTOCOL_KEY.search(text):
        return True
    for block in _FENCED_BLOCK.findall(text):
        if "{" in block or "[" in block:
            return True
    return False


__all__ = [
    "FALLBACK_REASON",
    "PlainTextFinal",
    "classify_parse_rejection",
    "plain_text_final",
]
