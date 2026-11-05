"""The tool catalog the model may choose from.

Two rules shape this module.

1. **The catalog is host-generated.**  The model never sees a tool it was not
   given, and never gets to add one: unknown names are refused before any
   handler runs.
2. **The model cannot certify itself.**  A tool schema that asks the model for
   an authority field (`authority_kind`, `approval_id`, `scope`, `actor`,
   `namespace`, ...) is rejected at construction time, and those fields are
   stripped from the arguments a handler receives.  Authority is injected by
   the host, never supplied by the caller.

Tool execution results are data, not exceptions: a failed tool becomes a
``ToolOutcome`` with an error code that flows back to the model, which is how
the model gets to decide whether to retry, change approach, or answer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .agent_protocol import AUTHORITY_FIELDS, ToolCall
from .model_port import ModelConfigurationError
from ..domains.cognition.beliefs import (
    BELIEF_BODY_FIELDS,
    CLAIM_TYPE_DESCRIPTIONS,
    EVIDENCE_LINK_FIELDS,
    INFERENCE_BASIS_BY_CLAIM_TYPE,
)
from ..domains.cognition.changes import (
    CHANGE_FIELD_TYPES,
    CHANGE_FIELDS,
)
from ..foundation.jsonutil import canonical_json_text
from .revision_contract import (
    CHANGE_OP_NOTES,
    CLAIM_FIELD_NOTE,
    CLAIM_TYPE_FIELD_NOTE,
    REVISION_RULE,
)

#: Result codes a tool round can carry.  Stable strings, not free text: the
#: loop and the audit record both key off them.
TOOL_OK = "ok"
TOOL_UNKNOWN = "tool_unknown"
TOOL_ARGUMENTS_INVALID = "tool_arguments_invalid"
TOOL_FAILED = "tool_failed"
TOOL_NOT_AUTHORIZED = "tool_not_authorized"

_JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


class ToolArgumentsInvalid(ValueError):
    """A handler refused the arguments for a reason the model can act on.

    Handlers raise this when the shape is valid JSON but the *content* cannot be
    honoured -- an invented evidence reference, for instance.  The catalog turns
    it into ``tool_arguments_invalid`` so the model sees a correctable refusal
    instead of an opaque failure.
    """


@dataclass(frozen=True)
class ToolSpec:
    """One published tool: its name, its schema, and its handler key."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    handler_key: str

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


@dataclass(frozen=True)
class ToolOutcome:
    """The result of one tool call, as it goes back to the model."""

    ok: bool
    code: str
    payload: Mapping[str, Any]

    def content(self) -> str:
        return canonical_json_text({"ok": self.ok, "code": self.code, "result": dict(self.payload)})


def _schema(
    properties: Mapping[str, Any], *, required: Sequence[str] = ()
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def _s(**properties: Any) -> Mapping[str, Any]:
    return properties


#: A belief reference is the record ref the Context supplied.  The schema has to
#: describe the shape the handler really accepts: an earlier "opaque string"
#: description made the model serialise the object and every read failed.
_READ = {
    "type": "object",
    "description": (
        'a record reference object like {"record_id": "<uuid>", '
        '"record_version": 1}; namespace is stamped by the host'
    ),
    # ``namespace`` is deliberately absent: it is an authority field the host
    # stamps on.  The model names *which* record, never *where* it lives.
    "properties": {
        "ref_type": {"type": "string", "enum": ["record"]},
        "record_id": {"type": "string", "minLength": 1},
        "record_version": {"type": "integer", "minimum": 1},
    },
    "required": ["record_id", "record_version"],
    "additionalProperties": False,
}


def _ref_schema(required_id: str, *, allow_input: bool = False) -> dict[str, Any]:
    """A record/event reference the model may name; namespace stays host-side.

    ``allow_input`` adds the one evidence kind a chat turn may cite about the
    user: the exact ``collab_input`` the Context already rendered.  It is off
    for ``premise_refs`` (a premise is a belief record) and for the question
    tools, so a collab_input cannot be smuggled into a non-evidence field.
    """

    variants: list[dict[str, Any]] = [
        {
            "type": "object",
            "properties": {
                "ref_type": {"type": "string", "const": "record"},
                "record_id": {"type": "string", "minLength": 1},
                "record_version": {"type": "integer", "minimum": 1},
            },
            "required": ["record_id", required_id] if required_id != "record_version" else ["record_id", "record_version"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "ref_type": {"type": "string", "const": "event"},
                "event_id": {"type": "string", "minLength": 1},
            },
            "required": ["event_id"],
            "additionalProperties": False,
        },
    ]
    if allow_input:
        variants.append(
            {
                "type": "object",
                "properties": {
                    "ref_type": {"type": "string", "const": "collab_input"},
                    "input_id": {"type": "string", "minLength": 1},
                    "content_version": {"type": "integer", "minimum": 1},
                },
                "required": ["ref_type", "input_id", "content_version"],
                "additionalProperties": False,
            }
        )
    return {"oneOf": variants}


def _belief_body_schema() -> dict[str, Any]:
    """Build the belief body schema from the validator's own field table."""

    properties: dict[str, Any] = {}
    for name, rule in BELIEF_BODY_FIELDS.items():
        properties[name] = dict(rule)
    properties["claim"] = {
        **dict(BELIEF_BODY_FIELDS["claim"]),
        "description": CLAIM_FIELD_NOTE,
    }
    properties["claim_type"] = {
        **dict(BELIEF_BODY_FIELDS["claim_type"]),
        "description": (
            "; ".join(
                f"{name}: {text}" for name, text in CLAIM_TYPE_DESCRIPTIONS.items()
            )
            + ". "
            + CLAIM_TYPE_FIELD_NOTE
        ),
    }
    properties["evidence_links"] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "ref": _ref_schema("record_version", allow_input=True),
                "relation": dict(EVIDENCE_LINK_FIELDS["relation"]),
                "origin_group_id": {
                    **dict(EVIDENCE_LINK_FIELDS["origin_group_id"]),
                    "description": (
                        "the source's own group: event:<event_id> for an event, "
                        "collab_input:<input_id> for the current user input, or "
                        "the episode group the Context supplied"
                    ),
                },
                "applicability_reason": dict(EVIDENCE_LINK_FIELDS["applicability_reason"]),
                "quote": dict(EVIDENCE_LINK_FIELDS["quote"]),
            },
            "required": ["ref", "relation", "origin_group_id", "applicability_reason"],
            "additionalProperties": False,
        },
    }
    properties["premise_refs"] = {"type": "array", "items": _ref_schema("record_version")}
    combinations = [
        {
            "properties": {
                "claim_type": {"const": claim_type},
                "inference_basis": {"const": basis},
            }
        }
        for claim_type, basis in sorted(INFERENCE_BASIS_BY_CLAIM_TYPE.items())
    ]
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(BELIEF_BODY_FIELDS),
        "additionalProperties": False,
        "description": (
            "claim_type and inference_basis move together; the pairs are "
            + ", ".join(f"{k}={v}" for k, v in sorted(INFERENCE_BASIS_BY_CLAIM_TYPE.items()))
        ),
        "allOf": [{"oneOf": combinations}],
    }


def _change_schema() -> dict[str, Any]:
    """Build the ``changes`` schema from the bundle validator's own tables.

    Hand-writing this a second time is how the two drifted apart: the model was
    told ``changes`` was an array of bare objects and had to guess ``op`` and the
    per-operation fields.  ``CHANGE_FIELDS`` and ``CHANGE_FIELD_TYPES`` live in
    ``changes.py`` next to the validator, so adding an operation without
    updating the catalog shows up as a failing consistency test rather than as a
    model that cannot form a valid bundle.
    """

    variants: list[dict[str, Any]] = []
    for op in CHANGE_FIELDS:
        properties: dict[str, Any] = {"op": {"type": "string", "const": op}}
        required = ["op"]
        for field in sorted(CHANGE_FIELDS[op] - {"op"}):
            if field in AUTHORITY_FIELDS:
                # ``scope`` is the host's to set: a model that could choose its
                # own scope could widen its own authorization.
                continue
            if field == "body":
                properties[field] = _belief_body_schema()
                required.append(field)
                continue
            if field == "target":
                properties[field] = {
                    "type": "object",
                    "properties": {
                        "record_id": {"type": "string", "minLength": 1},
                        "record_version": {"type": "integer", "minimum": 1},
                    },
                    "required": ["record_id", "record_version"],
                    "additionalProperties": False,
                }
                required.append(field)
                continue
            kind = CHANGE_FIELD_TYPES.get(field, "object")
            properties[field] = {"type": kind}
            required.append(field)
        variant: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        note = CHANGE_OP_NOTES.get(op)
        if note is not None:
            variant["description"] = note
        variants.append(variant)
    return {"type": "array", "minItems": 1, "maxItems": 4, "items": {"oneOf": variants}}

#: The cognition surface this stage publishes.  Write tools produce proposals;
#: none of them commits.  `question.resolve` / `.close` / `.reopen` are absent
#: on purpose: they must travel through a change bundle so they share the same
#: impact grading and approval path as belief changes.
COGNITION_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "cognition.belief.read",
        "Read an exact belief or an authorized state-preserving page. Continue using next_cursor; total counts visible matches; pages are not snapshot-consistent.",
        _schema(
            _s(
                ref=_READ,
                cursor={"type": "string", "minLength": 1, "maxLength": 2048},
                limit={"type": "integer", "minimum": 1, "maximum": 20},
                status={"type": "string", "enum": ["proposed", "supported", "contested", "contradicted", "stale"]},
            )
        ),
        "cognition.belief.read",
    ),
    ToolSpec(
        "cognition.belief.history",
        "Read the version history of one belief record.",
        _schema(_s(record_id={"type": "string", "minLength": 1}, limit={"type": "integer", "minimum": 1, "maximum": 50}), required=("record_id",)),
        "cognition.belief.history",
    ),
    ToolSpec(
        "cognition.partition",
        "Read the epistemic partition: accepted, provisional, contested, stale, questions.",
        _schema(_s(limit={"type": "integer", "minimum": 1, "maximum": 20}, include_questions={"type": "boolean"})),
        "cognition.partition",
    ),
    ToolSpec(
        "cognition.propose",
        "Propose one or more cognition changes. When the user states a lasting expression preference or explicitly corrects an earlier one, call this tool with claim_type=user_preference and name the dimension/value in the claim; the host binds the current input and decides. Evidence may cite a record or event this turn returned, or the exact current user input shown in inputs[].ref (ref_type=collab_input, content_version, origin_group_id=collab_input:<input_id>); a previous turn's input is not citable and an invented source is refused. Returns a candidate and its review result; nothing is committed until the governance path decides. "
        + REVISION_RULE,
        _schema(
            _s(
                summary={"type": "string", "maxLength": 280},
                changes=_change_schema(),
            ),
            required=("changes",),
        ),
        "cognition.propose",
    ),
    ToolSpec(
        "cognition.review",
        "Ask the review policy what it would do with an existing candidate.",
        _schema(_s(candidate_id={"type": "string"}), required=("candidate_id",)),
        "cognition.review",
    ),
    ToolSpec(
        "cognition.result",
        "Read the current result, decision history and receipt of a candidate.",
        _schema(_s(candidate_id={"type": "string"}), required=("candidate_id",)),
        "cognition.result",
    ),
    ToolSpec(
        "question.list",
        "List open or waiting questions.",
        _schema(_s(status={"type": "string", "enum": ["proposed", "open", "waiting"]}, limit={"type": "integer", "minimum": 1, "maximum": 50})),
        "question.list",
    ),
    ToolSpec(
        "question.get",
        "Read one question by reference.",
        _schema(_s(question_id={"type": "string"}), required=("question_id",)),
        "question.get",
    ),
    ToolSpec(
        "question.create",
        "Open a new question. May return an existing question when the same text already exists.",
        _schema(
            _s(
                text={"type": "string", "minLength": 1, "maxLength": 280},
                priority={"type": "string", "enum": ["low", "normal", "high"]},
            ),
            required=("text",),
        ),
        "question.create",
    ),
    ToolSpec(
        "question.transition",
        "Move a question between states that do not need a change candidate (accept/wait/pause/resume/wake/update_triggers).",
        _schema(
            _s(
                question_id={"type": "string"},
                action={"type": "string", "enum": ["accept", "wait", "pause", "resume", "wake", "update_triggers"]},
                expected_version={"type": "integer", "minimum": 1},
                waiting_for={"type": "string"},
                paused_by={"type": "string", "enum": ["user", "host"]},
                new_evidence_refs={"type": "array", "items": _ref_schema("record_version")},
            ),
            required=("question_id", "action", "expected_version"),
        ),
        "question.transition",
    ),
    ToolSpec(
        "attention.list",
        "List what currently holds attention, with the reason each item is there.",
        _schema(_s(limit={"type": "integer", "minimum": 1, "maximum": 50})),
        "attention.list",
    ),
)

WEB_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "web.search",
        "Search the web. Returns titles, URLs, summaries and authority hints; it does not prove anything.",
        _schema(
            _s(
                query={"type": "string", "minLength": 1, "maxLength": 128},
                count={"type": "integer", "minimum": 1, "maximum": 10},
            ),
            required=("query",),
        ),
        "web.search",
    ),
    ToolSpec(
        "web.fetch",
        "Fetch one page as text. With capture='evidence' the page text is stored so a later external_report can cite a quote from it.",
        _schema(
            _s(
                url={"type": "string", "minLength": 8, "maxLength": 8192},
                capture={"type": "string", "enum": ["none", "evidence"]},
            ),
            required=("url",),
        ),
        "web.fetch",
    ),
)

#: RG04: the transcript archive is a read-only conversation lookup, not a
#: memory/cognition search.  The result text is the already-stored public
#: transcript; the host stamps namespace/privacy/retention and the store
#: filters deletion before returning anything.
TRANSCRIPT_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "conversation.search",
        (
            "Search the saved public conversation archive (old turns and legacy "
            "single-turn records) for a bounded snippet. This is raw transcript, "
            "not memory or cognition; old assistant statements are not fact "
            "authority, and nothing here is loaded unless you call this tool."
        ),
        _schema(
            _s(
                query={"type": "string", "minLength": 1, "maxLength": 128},
                limit={"type": "integer", "minimum": 1, "maximum": 10},
                conversation_id={
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 36,
                },
                before={"type": "string", "minLength": 1, "maxLength": 2048},
                include_full={"type": "boolean"},
            ),
            required=("query",),
        ),
        "conversation.search",
    ),
)

#: RG08: read-only long-term memory browse/search over the instance's pinned
#: Scope Recall store.  Deliberately separate from ``conversation.search``,
#: though both can hold conversation text: this archive stores *both* speakers,
#: so the payload must say who spoke (``subject``) or an assistant line reads as
#: the user's own statement.  The host publishes this tool only when the memory
#: bridge is configured; the schema carries no scope, agent, session, namespace
#: or path, because the host owns all of them.
MEMORY_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "memory.search",
        (
            "Search this instance's saved long-term memory (Scope Recall) by "
            "literal keyword. One call returns ONE page of literal matches, "
            "not all memory: has_more and next_cursor say whether more exist, "
            "and passing that cursor back continues the same query. Do not "
            "describe a page as the whole record. The archive holds both sides "
            "of past conversations: every item's subject says who said it "
            "(user, assistant, tool or system), so a line the assistant said is "
            "never evidence of what the user prefers. A hit is a stored event "
            "with its own freshness state, not PCS Belief status. Only a "
            "substring match counts and at most two items come back, so use a "
            "shorter keyword, or continue with the cursor. A zero-hit answer is "
            "a real answer: report it instead of guessing, and never present a "
            "memory hit as a committed PCS belief."
        ),
        _schema(
            _s(
                query={"type": "string", "minLength": 1, "maxLength": 256},
                limit={"type": "integer", "minimum": 1, "maximum": 2},
                cursor={"type": "string", "minLength": 1, "maxLength": 96},
            ),
            required=("query",),
        ),
        "memory.search",
    ),
)


class ToolCatalog:
    """A closed set of tools plus the handlers that know how to run them."""

    def __init__(self, specs: Sequence[ToolSpec]) -> None:
        if not specs:
            raise ModelConfigurationError("a tool catalog must not be empty")
        seen: dict[str, ToolSpec] = {}
        for spec in specs:
            if not isinstance(spec, ToolSpec):
                raise ModelConfigurationError("catalog entries must be ToolSpec")
            if spec.name in seen:
                raise ModelConfigurationError(f"duplicate tool {spec.name!r}")
            _reject_schema_authority(spec)
            seen[spec.name] = spec
        self._specs = seen

    @classmethod
    def cognition_and_web(cls) -> "ToolCatalog":
        return cls(COGNITION_TOOLS + WEB_TOOLS)

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def definitions(self) -> tuple[dict[str, Any], ...]:
        return tuple(spec.definition() for spec in self._specs.values())

    def validate(self, call: ToolCall) -> ToolOutcome | None:
        """Validate one call **without executing anything**.

        Returns ``None`` when the call may run, or the refusal the model would
        see.  RG03 uses this before publishing commentary, so an invalid plan
        never produces a public sentence.
        """

        spec = self._specs.get(call.name)
        if spec is None:
            return ToolOutcome(False, TOOL_UNKNOWN, {"message": f"unknown tool {call.name!r}"})
        try:
            arguments = dict(call.arguments_mapping())
        except ModelConfigurationError as exc:
            return ToolOutcome(False, TOOL_ARGUMENTS_INVALID, {"message": str(exc)})
        stripped = _strip_authority_fields(arguments)
        problem = _validate_against(spec.parameters, stripped)
        if problem is not None:
            return ToolOutcome(False, TOOL_ARGUMENTS_INVALID, {"message": problem})
        return None

    def execute(
        self,
        call: ToolCall,
        *,
        handlers: Mapping[str, Callable[[Mapping[str, Any]], Any]],
    ) -> ToolOutcome:
        """Validate one call and run it.

        The returned outcome is what the model sees next round; nothing here
        raises on a bad call, because a refusal the model cannot read is
        indistinguishable from a crash.
        """

        refusal = self.validate(call)
        if refusal is not None:
            return refusal
        spec = self._specs[call.name]
        arguments = dict(call.arguments_mapping())
        stripped = _strip_authority_fields(arguments)
        handler = handlers.get(spec.handler_key)
        if handler is None:
            return ToolOutcome(
                False, TOOL_FAILED, {"message": f"no handler registered for {spec.name!r}"}
            )
        try:
            result = handler(stripped)
        except PermissionError as exc:
            return ToolOutcome(False, TOOL_NOT_AUTHORIZED, {"message": str(exc)})
        except ToolArgumentsInvalid as exc:
            return ToolOutcome(False, TOOL_ARGUMENTS_INVALID, {"message": str(exc)})
        except Exception as exc:  # defensive: a tool must not kill the loop
            return ToolOutcome(False, TOOL_FAILED, {"message": f"{type(exc).__name__}: {exc}"})
        if isinstance(result, ToolOutcome):
            return result
        if not isinstance(result, Mapping):
            return ToolOutcome(False, TOOL_FAILED, {"message": "handler returned non-object"})
        return ToolOutcome(True, TOOL_OK, result)


def _reject_schema_authority(spec: ToolSpec) -> None:
    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key in AUTHORITY_FIELDS:
                    raise ModelConfigurationError(
                        f"tool {spec.name!r} asks the model for the authority field {key!r}"
                    )
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(spec.parameters)


def _strip_authority_fields(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in arguments.items() if key not in AUTHORITY_FIELDS}


def _validate_against(schema: Mapping[str, Any], value: Mapping[str, Any]) -> str | None:
    """Check the small JSON-schema subset the catalog uses.

    Returns ``None`` when valid, or a message the model can act on.
    """

    if schema.get("type") != "object":
        return "internal: tool schema must describe an object"
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    for name in required:
        if name not in value:
            return f"missing required argument {name!r}"
    if schema.get("additionalProperties") is False:
        extra = sorted(set(value) - set(properties))
        if extra:
            return f"unexpected argument(s): {', '.join(extra)}"
    for name, item in value.items():
        rule = properties.get(name)
        if rule is None:
            continue
        problem = _check_type(name, rule, item)
        if problem is not None:
            return problem
    return None


def _check_type(name: str, rule: Mapping[str, Any], value: Any) -> str | None:
    expected = rule.get("type")
    if expected is not None:
        python_type = _JSON_TYPES.get(expected)
        if python_type is None:
            return f"internal: unknown type {expected!r} for {name!r}"
        if expected == "boolean":
            if not isinstance(value, bool):
                return f"{name!r} must be a boolean"
        elif expected in ("integer", "number"):
            if isinstance(value, bool) or not isinstance(value, python_type):
                return f"{name!r} must be a {expected}"
            if expected == "integer" and not isinstance(value, int):
                return f"{name!r} must be an integer"
        elif not isinstance(value, python_type):
            message = f"{name!r} must be a {expected}"
            hint = rule.get("description")
            if hint:
                message = f"{message} ({hint})"
            return message
    if "enum" in rule and value not in rule["enum"]:
        return f"{name!r} must be one of {rule['enum']}"
    if "minimum" in rule and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < rule["minimum"]:
            return f"{name!r} must be >= {rule['minimum']}"
    if "maximum" in rule and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > rule["maximum"]:
            return f"{name!r} must be <= {rule['maximum']}"
    if "minLength" in rule and isinstance(value, str) and len(value) < rule["minLength"]:
        return f"{name!r} must be at least {rule['minLength']} characters"
    if "maxLength" in rule and isinstance(value, str) and len(value) > rule["maxLength"]:
        return f"{name!r} must be at most {rule['maxLength']} characters"
    if "minItems" in rule and isinstance(value, list) and len(value) < rule["minItems"]:
        return f"{name!r} must have at least {rule['minItems']} items"
    if "maxItems" in rule and isinstance(value, list) and len(value) > rule["maxItems"]:
        return f"{name!r} must have at most {rule['maxItems']} items"
    return None


__all__ = [
    "COGNITION_TOOLS",
    "MEMORY_TOOLS",
    "TRANSCRIPT_TOOLS",
    "WEB_TOOLS",
    "TOOL_ARGUMENTS_INVALID",
    "TOOL_FAILED",
    "TOOL_NOT_AUTHORIZED",
    "TOOL_OK",
    "TOOL_UNKNOWN",
    "ToolCatalog",
    "ToolOutcome",
    "ToolSpec",
]
