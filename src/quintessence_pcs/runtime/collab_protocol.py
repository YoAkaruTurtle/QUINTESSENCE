"""C09: the versioned collab.v1 model-output protocol and message renderer.

This module is a pure library.  It parses one versioned consult model output,
renders the explicit protocol messages and builds the canonical request body.
It never opens a socket, reads a store row, writes an attempt or treats model
text as an authoritative ``request_id``, ``read_set``, ``state_revision``,
``context_id`` or authorization.  :class:`ConsultSendPolicy` is a trusted-host
send-policy value, not an ACL table and not a persisted permission.

The accepted output is exactly one closed JSON object::

    {"reply"?: string, "action": "none"|"query"|"propose"|"wait",
     "references"?: [source_ref, ...], "brief_reason"?: string}

``references`` only reuses the four frozen ``source_ref`` branches (event,
record, collab_input, collab_artifact).  Every entry must exactly equal one
reference the Context supplied; the parser never resolves a reference itself,
never executes ``query`` and never commits ``propose``.  In ``consult`` mode
``propose`` is rejected even when its shape is valid: C09 does not turn a
consult reply into a review proposal.

Local deletion boundary (C09 addendum B)
----------------------------------------
The C05b deletion barrier and ``cleanup_deletions`` protect only the PCS
locally persisted copies: the Context rendered text and the model-attempt
request/response payloads.  Deleting a collaboration input can never withdraw
bytes that an external provider already received, and nothing in this module,
the send gate or the tests claims that it can.  There is no provider-side
erase API in this card and no such claim is made.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..foundation import identifiers
from ..foundation.errors import InvalidInput
from ..foundation.jsonutil import canonical_json_text, strict_json_loads
from ..store.experience import (
    MAX_ACCESS_REF_LENGTH,
    MAX_HOST_INSTANCE_LENGTH,
    MAX_PURPOSE_LENGTH,
    ExperienceAccess,
)
from .context import ConsultContextEnvelope
from .manifest import ModelManifest
from .model_port import ModelConfigurationError

#: Frozen protocol / renderer / policy versions recorded in the ledger.
COLLAB_PROTOCOL_VERSION = "collab.v1"
COLLAB_RENDERER_VERSION = "collab.v1.renderer.2"
COLLAB_MODEL_POLICY_VERSION = "collab.v1.direct-http.v2"

#: The ``$id`` of the one schema contract this protocol accepts.
COLLAB_OUTPUT_SCHEMA_ID = "model_output.json"

MAX_REPLY_CHARS = 16384
MAX_BRIEF_REASON_CHARS = 2048
MAX_REFERENCES = 32
COLLAB_ACTIONS = ("none", "query", "propose", "wait")

#: Model text may never supply these; the server owns them.
AUTHORITATIVE_FIELDS = (
    "request_id",
    "read_set",
    "state_revision",
    "authorization",
    "context_id",
)

_OUTPUT_KEYS = frozenset({"reply", "action", "references", "brief_reason"})

_REFERENCE_KEYS = {
    "event": frozenset({"ref_type", "namespace", "event_id"}),
    "record": frozenset({"ref_type", "namespace", "record_id", "record_version"}),
    "collab_input": frozenset(
        {"ref_type", "namespace", "input_id", "content_version"}
    ),
    "collab_artifact": frozenset(
        {"ref_type", "namespace", "artifact_id", "artifact_version"}
    ),
}

_SAFE_EXCEPTION_NAME = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class CollabOutput:
    """One strictly parsed collab.v1 model output (never an authority)."""

    reply: str | None
    action: str
    references: tuple[dict[str, Any], ...]
    brief_reason: str | None


@dataclass(frozen=True)
class ConsultSendPolicy:
    """Trusted-host authority for one consult send.

    ``privacy_classes`` is the set of record/experience privacy classes that
    may be sent; ``purposes`` must contain ``"consult"`` and ``disclose`` must
    be true.  This is a policy value supplied by the trusted host at model
    construction time -- not a persistent ACL and not a per-source grant.
    """

    namespace: str
    host_instance: str
    access_ref: str
    privacy_classes: frozenset[str]
    purposes: frozenset[str]
    disclose: bool

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str):
            raise InvalidInput("send policy namespace must be a string")
        identifiers.require_namespace(self.namespace)
        _require_policy_text(
            self.host_instance, "host_instance", MAX_HOST_INSTANCE_LENGTH
        )
        _require_policy_text(self.access_ref, "access_ref", MAX_ACCESS_REF_LENGTH)
        if not isinstance(self.privacy_classes, frozenset):
            raise InvalidInput("send policy privacy_classes must be a frozenset")
        for privacy in self.privacy_classes:
            identifiers.require_privacy_class(privacy)
        if not isinstance(self.purposes, frozenset):
            raise InvalidInput("send policy purposes must be a frozenset")
        for purpose in self.purposes:
            _require_policy_text(purpose, "purpose", MAX_PURPOSE_LENGTH)
        if type(self.disclose) is not bool:
            raise InvalidInput("send policy disclose must be a boolean")

    def to_experience_access(self) -> ExperienceAccess:
        """The exact read authority the send gate uses for collaboration refs."""

        return ExperienceAccess(
            namespace=self.namespace,
            host_instance=self.host_instance,
            access_ref=self.access_ref,
            privacy_classes=self.privacy_classes,
            purposes=self.purposes,
        )


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def parse_collab_output(
    content: str,
    *,
    mode: str,
    allowed_references: Sequence[Mapping[str, Any]],
) -> CollabOutput:
    """Parse one ``collab.v1`` consult model output or raise ``InvalidInput``.

    Unknown keys, unknown actions, hidden authoritative fields, empty/overlong
    text and any reference that is not exactly one Context-supplied reference
    are refused.  The parser is deliberately not permissive: there is no
    heuristic fallback and no repair loop here (C11 owns orchestration).
    """

    if not isinstance(content, str) or not content:
        raise InvalidInput("collab output must be a non-empty JSON string")
    if mode != "consult":
        raise InvalidInput(
            "C09 parses only mode='consult'; review keeps the legacy parser"
        )
    try:
        document = strict_json_loads(content)
    except ValueError as exc:
        raise InvalidInput("collab output is not strict JSON") from exc
    if not isinstance(document, dict):
        raise InvalidInput("collab output must be exactly one JSON object")

    keys = set(document)
    unknown = keys - _OUTPUT_KEYS
    if unknown:
        first = sorted(unknown)[0]
        if first in AUTHORITATIVE_FIELDS:
            raise InvalidInput(
                "collab output must not carry the authoritative field "
                f"{first!r}"
            )
        raise InvalidInput(
            f"collab output has a key outside the protocol: {first!r}"
        )
    if "action" not in keys:
        raise InvalidInput("collab output is missing the required action")
    action = document["action"]
    if not isinstance(action, str) or action not in COLLAB_ACTIONS:
        raise InvalidInput(
            "collab output action must be one of " + ", ".join(COLLAB_ACTIONS)
        )
    if action == "propose":
        raise InvalidInput("consult cannot propose")

    reply = _optional_text(document, "reply", MAX_REPLY_CHARS)
    brief_reason = _optional_text(
        document, "brief_reason", MAX_BRIEF_REASON_CHARS
    )
    references = _parse_references(document, allowed_references)

    if action == "none" and reply is None and brief_reason is None:
        raise InvalidInput(
            "action 'none' requires a reply or a brief_reason; an empty "
            "output is not a valid consult response"
        )
    return CollabOutput(
        reply=reply,
        action=action,
        references=references,
        brief_reason=brief_reason,
    )


def _optional_text(
    document: Mapping[str, Any], key: str, max_length: int
) -> str | None:
    if key not in document:
        return None
    value = document[key]
    if not isinstance(value, str):
        raise InvalidInput(f"{key} must be a string")
    if not 1 <= len(value) <= max_length:
        raise InvalidInput(f"{key} must be 1..{max_length} characters")
    return value


def _parse_references(
    document: Mapping[str, Any],
    allowed_references: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    allowed_keys = _normalise_allowed_references(allowed_references)
    if "references" not in document:
        return ()
    raw = document["references"]
    if not isinstance(raw, list):
        raise InvalidInput("references must be an array")
    if len(raw) > MAX_REFERENCES:
        raise InvalidInput(f"references must contain at most {MAX_REFERENCES} items")
    normalised: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        reference = _normalise_reference(item, field=f"references[{index}]")
        key = canonical_json_text(reference)
        if key in seen:
            raise InvalidInput("references must not contain duplicates")
        seen.add(key)
        if key not in allowed_keys:
            raise InvalidInput(
                "a reference does not exactly match a reference supplied by "
                "the Context"
            )
        normalised.append(reference)
    return tuple(normalised)


def _normalise_allowed_references(
    allowed_references: Sequence[Mapping[str, Any]],
) -> set[str]:
    if isinstance(allowed_references, (str, bytes)) or not isinstance(
        allowed_references, Sequence
    ):
        raise InvalidInput(
            "allowed_references must be a sequence of typed source references"
        )
    keys: set[str] = set()
    for index, item in enumerate(allowed_references):
        reference = _normalise_reference(
            item, field=f"allowed_references[{index}]"
        )
        keys.add(canonical_json_text(reference))
    return keys


def _normalise_reference(value: Any, *, field: str) -> dict[str, Any]:
    """Validate and normalise one frozen ``source_ref`` branch."""

    if not isinstance(value, Mapping):
        raise InvalidInput(f"{field} must be a typed source reference object")
    reference = dict(value)
    ref_type = reference.get("ref_type")
    if ref_type not in _REFERENCE_KEYS:
        raise InvalidInput(
            f"{field}.ref_type must be one of " + ", ".join(_REFERENCE_KEYS)
        )
    expected = _REFERENCE_KEYS[ref_type]
    if set(reference) != expected:
        raise InvalidInput(
            f"{field} must have exactly " + ", ".join(sorted(expected))
        )
    namespace = identifiers.require_namespace(reference["namespace"])
    if ref_type == "event":
        return {
            "ref_type": "event",
            "namespace": namespace,
            "event_id": identifiers.require_event_id(reference["event_id"]),
        }
    if ref_type == "record":
        return {
            "ref_type": "record",
            "namespace": namespace,
            "record_id": identifiers.require_record_id(reference["record_id"]),
            "record_version": identifiers.require_record_version(
                reference["record_version"]
            ),
        }
    if ref_type == "collab_input":
        return {
            "ref_type": "collab_input",
            "namespace": namespace,
            "input_id": _require_uuid(reference["input_id"], f"{field}.input_id"),
            "content_version": _require_version(
                reference["content_version"], f"{field}.content_version"
            ),
        }
    return {
        "ref_type": "collab_artifact",
        "namespace": namespace,
        "artifact_id": _require_uuid(
            reference["artifact_id"], f"{field}.artifact_id"
        ),
        "artifact_version": _require_version(
            reference["artifact_version"], f"{field}.artifact_version"
        ),
    }


def _require_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str) or identifiers.UUID_PATTERN.fullmatch(value) is None:
        raise InvalidInput(f"{field} must be a canonical lower-case UUID")
    return value


def _require_version(value: Any, field: str) -> int:
    if type(value) is not int or not 1 <= value <= identifiers.MAX_SAFE_INTEGER:
        raise InvalidInput(f"{field} must be a positive JSON-safe integer")
    return value


# --------------------------------------------------------------------------
# repair feedback
# --------------------------------------------------------------------------


def repair_feedback(exc: Exception) -> str:
    """Return fixed, bounded repair text that never echoes the model output.

    Only the exception *type* is named.  Neither the exception message nor the
    rejected content is copied, so a provider body, a Context body or user
    data can never become part of a new request through this channel.
    """

    name = _SAFE_EXCEPTION_NAME.sub("_", type(exc).__name__)[:64] or "Error"
    return (
        f"Your previous output was rejected by {COLLAB_PROTOCOL_VERSION}: "
        f"{name}. Return exactly one JSON object with action in "
        f"{', '.join(COLLAB_ACTIONS)} and no other keys. Do not output "
        "request_id, read_set, state_revision, authorization or context_id."
    )


# --------------------------------------------------------------------------
# renderer
# --------------------------------------------------------------------------


def render_collab_messages(
    context: ConsultContextEnvelope,
    *,
    previous_output: str | None = None,
    feedback: str | None = None,
) -> tuple[dict[str, str], ...]:
    """Render the explicit, versioned protocol messages for one consult call.

    The first message is an auditable JSON declaration of the protocol,
    renderer and output schema.  The second is exactly
    ``context.rendered_input``; nothing is prefixed or truncated.  A repair
    appends the previous assistant answer and the caller's feedback as the
    third and fourth messages.  No additional system message, example or tool
    definition is ever added.
    """

    if not isinstance(context, ConsultContextEnvelope):
        raise ModelConfigurationError(
            "collab.v1 messages require a ConsultContextEnvelope"
        )
    rendered_input = context.rendered_input
    if not isinstance(rendered_input, str) or not rendered_input:
        raise ModelConfigurationError(
            "the bound consult Context has no rendered input to send"
        )
    if (feedback is None) != (previous_output is None):
        raise ModelConfigurationError(
            "a format repair requires both the previous output and feedback"
        )
    if feedback is not None:
        if not isinstance(feedback, str) or not feedback.strip():
            raise ModelConfigurationError("repair feedback must be non-empty text")
        if not isinstance(previous_output, str) or not previous_output:
            raise ModelConfigurationError(
                "a format repair requires the previous assistant output"
            )

    messages = [
        {
            "role": "system",
            "content": canonical_json_text(_protocol_declaration()),
        },
        {"role": "user", "content": rendered_input},
    ]
    if feedback is not None:
        messages.append({"role": "assistant", "content": previous_output})
        messages.append({"role": "user", "content": feedback})
    return tuple(messages)


def _protocol_declaration() -> dict[str, Any]:
    output_schema = {
        "$id": COLLAB_OUTPUT_SCHEMA_ID,
        "type": "object",
        "additionalProperties": False,
        "required": ["action"],
        "properties": {
            "reply": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_REPLY_CHARS,
            },
            "action": {"type": "string", "enum": list(COLLAB_ACTIONS)},
            "references": {
                "type": "array",
                "maxItems": MAX_REFERENCES,
                "uniqueItems": True,
                "items": {"$ref": "_common.json#/$defs/source_ref"},
            },
            "brief_reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_BRIEF_REASON_CHARS,
            },
        },
    }
    return {
        "protocol_version": COLLAB_PROTOCOL_VERSION,
        "renderer_version": COLLAB_RENDERER_VERSION,
        "mode": "consult",
        "output_schema": output_schema,
        "allowed_actions": list(COLLAB_ACTIONS),
        "consult_forbidden_action": "propose",
        "query_policy": {
            "use_query_when": (
                "The user's question may depend on facts, preferences, project "
                "decisions or other cognition previously stored by the PCS, "
                "and the current Context does not contain enough evidence to "
                "answer it. Query before saying unknown or guessing."
            ),
            "do_not_query_when": (
                "The question is ordinary general knowledge unrelated to "
                "stored PCS cognition, or the current Context already contains "
                "sufficient evidence. Do not repeat a query after its result "
                "has been supplied in this interaction."
            ),
            "host_owned_plan": (
                "A query is a request for host-owned evidence. The host "
                "validates and executes its configured deterministic query "
                "plan. You cannot supply, widen or invent a query plan, scope "
                "or result. If no plan applies, the host may return an "
                "incomplete result; do not fabricate one."
            ),
            "after_query": (
                "Use only the disclosed query result. If it is empty, partial "
                "or unavailable, answer from the disclosed material and state "
                "the uncertainty. A query supplies evidence only; it never "
                "creates or commits a Belief."
            ),
        },
        "references_policy": (
            "every references[] entry must exactly equal one reference "
            "supplied by the Context; namespace, id and version must match "
            "exactly"
        ),
        "forbidden_output_fields": list(AUTHORITATIVE_FIELDS),
        "response_format": (
            "Return exactly one JSON object with only these keys; never free "
            "text."
        ),
    }


def build_collab_request_body(
    manifest: ModelManifest,
    *,
    messages: Sequence[Mapping[str, Any]],
    max_tokens: int,
) -> str:
    """Build the exact canonical request text for one collab HTTP try.

    The body carries only ``model``, ``messages``, the manifest's ``thinking``
    branch, ``temperature`` (disabled branch), ``max_tokens`` and
    ``stream=false``.  A tool/function key anywhere in the body is a
    configuration error; the adapter is tool-free by construction.
    """

    if not isinstance(manifest, ModelManifest):
        raise ModelConfigurationError("manifest must be a ModelManifest")
    if type(max_tokens) is not int or max_tokens < 1:
        raise ModelConfigurationError("max_tokens must be a positive integer")
    if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence):
        raise ModelConfigurationError("messages must be a sequence of message objects")
    if not messages:
        raise ModelConfigurationError("messages must not be empty")
    prepared: list[dict[str, str]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping) or set(message) != {"role", "content"}:
            raise ModelConfigurationError(
                f"messages[{index}] must have exactly role and content"
            )
        role = message["role"]
        content = message["content"]
        if role not in ("system", "user", "assistant"):
            raise ModelConfigurationError(
                f"messages[{index}].role is not a conversation role"
            )
        if not isinstance(content, str) or not content:
            raise ModelConfigurationError(
                f"messages[{index}].content must be non-empty text"
            )
        prepared.append({"role": role, "content": content})

    if manifest.thinking == "enabled":
        body: dict[str, Any] = {
            "model": manifest.model,
            "messages": prepared,
            "thinking": {"type": "enabled"},
            "reasoning_effort": manifest.reasoning_effort or "high",
            "max_tokens": max_tokens,
            "stream": False,
        }
    else:
        body = {
            "model": manifest.model,
            "messages": prepared,
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
        }
    _reject_forbidden_keys(body)
    return canonical_json_text(body)


def _reject_forbidden_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"tools", "functions", "tool_calls", "function_call"}:
                raise ModelConfigurationError(
                    f"request body must not contain {key!r}; collab.v1 is tool-free"
                )
            _reject_forbidden_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_forbidden_keys(child)


def _require_policy_text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"send policy {field} must be a non-empty string")
    if len(value) > max_length:
        raise InvalidInput(
            f"send policy {field} must be at most {max_length} characters"
        )
    return value


__all__ = [
    "AUTHORITATIVE_FIELDS",
    "COLLAB_ACTIONS",
    "COLLAB_MODEL_POLICY_VERSION",
    "COLLAB_OUTPUT_SCHEMA_ID",
    "COLLAB_PROTOCOL_VERSION",
    "COLLAB_RENDERER_VERSION",
    "CollabOutput",
    "ConsultSendPolicy",
    "MAX_BRIEF_REASON_CHARS",
    "MAX_REFERENCES",
    "MAX_REPLY_CHARS",
    "build_collab_request_body",
    "parse_collab_output",
    "render_collab_messages",
    "repair_feedback",
]
