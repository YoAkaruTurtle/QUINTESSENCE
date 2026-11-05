"""RG07/RG08: the host-built handoff and task text for the expression stage.

The content stage keeps its frozen ``final`` output shape: the host reads the
finished ``reply`` and builds the handoff itself.  The handoff is an explicit
work artifact -- the draft plus the categories it must preserve -- never a
chain of thought and never a second protocol.

Everything here is pure: the caller supplies the draft and the published
message ids, and this module only bounds and serialises them.

RG08 V1.5 decouples persona text from source: this module carries **no**
persona-owned policy.  The expression task policy is assembled by the caller's
frozen :class:`~.policy_bundle.PolicyBundle` and passed in; the only policy
text defined here is the persona-neutral host layer, imported below.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..foundation.errors import InvalidInput
from ..foundation.jsonutil import canonical_json_text
from .expression_host_policy import HOST_EXPRESSION_STAGE_POLICY

HANDOFF_VERSION = "sdrpe.handoff.v1"
TASK_VERSION = "sdrpe.task.v1"

#: The whole handoff document must fit inside one expression Context.
MAX_HANDOFF_BYTES = 45056
MAX_PUBLIC_REFS = 4
MAX_REF_CHARS = 64

#: Categories the expression stage must not change.  Structure is deliberately
#: not here: the expression stage may reorganise paragraphs and remove padding.
MUST_PRESERVE = (
    "facts",
    "numbers",
    "entities",
    "negation",
    "judgments",
    "uncertainty",
    "commitments",
    "refusals",
    "operation_state",
)


def build_content_handoff(
    *,
    draft: str,
    response_intent: str = "answer",
    already_public_refs: Sequence[str] = (),
) -> dict[str, Any]:
    """Build the bounded handoff document for one finished content answer."""

    if not isinstance(draft, str) or not draft.strip():
        raise InvalidInput("the content handoff needs a non-empty draft")
    if response_intent not in ("answer", "clarify"):
        raise InvalidInput("response_intent must be answer or clarify")
    if len(already_public_refs) > MAX_PUBLIC_REFS:
        raise InvalidInput("too many already-public message references")
    refs: list[str] = []
    for value in already_public_refs:
        if not isinstance(value, str) or not value:
            raise InvalidInput("an already-public message reference must be a string")
        if len(value) > MAX_REF_CHARS:
            raise InvalidInput("an already-public message reference is too long")
        if value not in refs:
            refs.append(value)
    document: dict[str, Any] = {
        "version": HANDOFF_VERSION,
        "response_intent": response_intent,
        "public_draft": draft,
        "must_preserve": list(MUST_PRESERVE),
        "already_public_message_refs": refs,
    }
    if handoff_bytes(document) > MAX_HANDOFF_BYTES:
        raise InvalidInput("the content handoff exceeds its byte bound")
    return document


def build_expression_task(
    *, pending_cognition_changes: int, bundle: Any
) -> dict[str, Any]:
    """Build the expression task from the turn's frozen policy bundle.

    The policy text, its version and its digest all come from ``bundle``: this
    module never reads a policy constant and never re-resolves a policy.  The
    persona side is reported as provenance so an auditor can tell which asset
    (if any) produced the expression text of this turn.
    """

    if type(pending_cognition_changes) is not int or pending_cognition_changes < 0:
        raise InvalidInput("pending_cognition_changes must be a non-negative integer")
    persona = getattr(bundle, "persona", None)
    if persona is None:
        persona_state = "absent"
        persona_sha256 = None
        persona_profile_id = None
        persona_profile_version = None
    else:
        persona_state = persona.state
        persona_sha256 = persona.sha256 if persona.state == "present" else None
        persona_profile_id = persona.profile_id
        persona_profile_version = persona.profile_version
    return {
        "version": TASK_VERSION,
        "policy": bundle.expression_text,
        "output": (
            '{"action":"final","reply":"..."} or '
            '{"action":"final","messages":["...","..."]}'
        ),
        "pending_cognition_changes": pending_cognition_changes,
        "policy_version": bundle.version,
        "policy_digest": bundle.digest,
        "persona_policy_state": persona_state,
        "persona_policy_sha256": persona_sha256,
        "persona_profile_id": persona_profile_id,
        "persona_profile_version": persona_profile_version,
    }


def handoff_bytes(document: Mapping[str, Any]) -> int:
    """The canonical serialised size of one handoff document."""

    return len(canonical_json_text(dict(document)).encode("utf-8"))


def published_refs(refs: Sequence[Mapping[str, Any]]) -> list[str]:
    """The artifact ids of already-published messages, in order, de-duplicated."""

    values: list[str] = []
    for ref in refs:
        artifact_id = ref.get("artifact_id") if isinstance(ref, Mapping) else None
        if isinstance(artifact_id, str) and artifact_id and artifact_id not in values:
            values.append(artifact_id)
    return values


__all__ = [
    "HANDOFF_VERSION",
    "HOST_EXPRESSION_STAGE_POLICY",
    "MAX_HANDOFF_BYTES",
    "MUST_PRESERVE",
    "TASK_VERSION",
    "build_content_handoff",
    "build_expression_task",
    "handoff_bytes",
    "published_refs",
]
