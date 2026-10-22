"""The one implementation of proposal parsing, validation and repair.

``parse_proposal`` (strict structural parse of the model's JSON text),
``validate_proposal`` (scope/target/body validation in front of Tx B) and
``repair_feedback`` (the bounded format-repair hint) live here and only here.
``fake_model``, ``step`` and the package ``__init__`` re-export these exact
objects, so every old import path still resolves to the same function.

This is a pure extraction from PCS2-008's ``fake_model``/``step`` modules: the
accepted shapes, the rejection rules, the exception types and messages, and the
repair text are unchanged.  Semantic retry feedback stays in ``step``; it is a
separate path with its own counter and must not be merged into format repair.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..domains.cognition.beliefs import validate_belief_body
from ..domains.cognition.errors import EvidenceMissing, UnsupportedOperation
from ..domains.errors import SourceUntrusted
from ..domains.refs import RecordRef
from ..domains.scope import SCOPE_FIELDS, normalize_scope
from ..foundation.errors import InvalidInput, ScopeViolation, SourceRedacted
from ..foundation.jsonutil import strict_json_loads
from .context import proposal_format_hint

_MAX_WAIT_REASON = 512

_PROPOSE_KEYS = frozenset({"operation", "body", "scope"})
_REVISE_KEYS = frozenset({"operation", "target", "body", "scope"})
_RETRACT_KEYS = frozenset({"operation", "target", "evidence_links", "reason"})
_WAIT_KEYS = frozenset({"operation", "reason"})


def parse_proposal(text: Any) -> dict[str, Any]:
    """Parse one model output strictly.

    Only three shapes are accepted; every other key is a structural error.  A
    model that tries to supply ``state_revision`` / ``request_id`` /
    ``read_set`` / ``context_id`` therefore fails instead of being believed.
    """

    if not isinstance(text, str):
        raise InvalidInput("model output must be JSON text")
    try:
        value = strict_json_loads(text)
    except ValueError as exc:
        raise InvalidInput(f"model output is not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise InvalidInput("model output must be one JSON object")
    operation = value.get("operation")
    if operation == "wait":
        if set(value) != _WAIT_KEYS:
            raise InvalidInput(
                "a wait object must have exactly operation and reason; "
                "model-supplied state_revision/request_id/read_set are not accepted"
            )
        reason = value["reason"]
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > _MAX_WAIT_REASON
        ):
            raise InvalidInput(
                f"wait.reason must be a non-empty string of at most {_MAX_WAIT_REASON} "
                "characters"
            )
        return {"operation": "wait", "reason": reason}
    if operation == "propose_belief":
        if set(value) != _PROPOSE_KEYS:
            raise InvalidInput(
                "propose_belief must have exactly operation, body and scope; "
                "model-supplied state_revision/request_id/read_set are not accepted"
            )
        body = value["body"]
        scope = value["scope"]
        if not isinstance(body, Mapping) or not isinstance(scope, Mapping):
            raise InvalidInput("propose_belief body and scope must be JSON objects")
        return {
            "operation": "propose_belief",
            "body": dict(body),
            "scope": dict(scope),
        }
    if operation == "revise_belief":
        if set(value) != _REVISE_KEYS:
            raise InvalidInput(
                "revise_belief must have exactly operation, target, body and scope; "
                "model-supplied state_revision/request_id/read_set are not accepted"
            )
        target = value["target"]
        body = value["body"]
        scope = value["scope"]
        if (
            not isinstance(target, Mapping)
            or not isinstance(body, Mapping)
            or not isinstance(scope, Mapping)
        ):
            raise InvalidInput(
                "revise_belief target, body and scope must be JSON objects"
            )
        return {
            "operation": "revise_belief",
            "target": dict(target),
            "body": dict(body),
            "scope": dict(scope),
        }
    if operation == "retract_belief":
        if set(value) != _RETRACT_KEYS:
            raise InvalidInput(
                "retract_belief must have exactly operation, target, "
                "evidence_links and reason; model-supplied "
                "state_revision/request_id/read_set are not accepted"
            )
        target = value["target"]
        evidence_links = value["evidence_links"]
        reason = value["reason"]
        if not isinstance(target, Mapping) or not isinstance(reason, str):
            raise InvalidInput(
                "retract_belief target must be a JSON object and reason a string"
            )
        if (
            not isinstance(evidence_links, list)
            or not evidence_links
            or not all(isinstance(item, Mapping) for item in evidence_links)
        ):
            raise InvalidInput(
                "retract_belief evidence_links must be a non-empty array of objects"
            )
        if not reason.strip() or len(reason) > 2048:
            raise InvalidInput(
                "retract_belief reason must be a non-empty string of at most 2048 "
                "characters"
            )
        return {
            "operation": "retract_belief",
            "target": dict(target),
            "evidence_links": [dict(item) for item in evidence_links],
            "reason": reason,
        }
    raise UnsupportedOperation(
        f"model output operation {operation!r} is not allowed; "
        "PCS2-008 accepts propose_belief, revise_belief, retract_belief and wait"
    )


def validate_proposal(namespace: str, proposal: Mapping[str, Any]) -> None:
    """Reject a structurally parsed proposal that PCS2-008 cannot submit."""

    operation = proposal["operation"]
    if operation == "propose_belief":
        scope = normalize_scope(proposal["scope"])
        validate_belief_body(proposal["body"], scope=scope)
        return
    if operation == "revise_belief":
        target = RecordRef.from_value(proposal["target"], field="target")
        if target.namespace != namespace:
            raise ScopeViolation(
                f"target is in namespace {target.namespace!r}, not {namespace!r}"
            )
        scope = normalize_scope(proposal["scope"])
        validate_belief_body(proposal["body"], scope=scope)
        return
    if operation == "retract_belief":
        target = RecordRef.from_value(proposal["target"], field="target")
        if target.namespace != namespace:
            raise ScopeViolation(
                f"target is in namespace {target.namespace!r}, not {namespace!r}"
            )
        if not proposal.get("evidence_links"):
            raise InvalidInput(
                "retract_belief must cite the evidence that justifies the "
                "withdrawal"
            )
        if not str(proposal.get("reason") or "").strip():
            raise InvalidInput("retract_belief requires a non-empty reason")
        return
    raise UnsupportedOperation(
        f"PCS2-008 cannot submit {operation!r}; allowed: propose_belief, "
        "revise_belief, retract_belief, wait"
    )


def _erroneous_field(message: str, exc: BaseException) -> str | None:
    """Name the field a structural refusal is about, when the message says so."""

    if isinstance(exc, EvidenceMissing):
        if "applicability in the record scope" in message:
            return "scope"
        if "valid_until" in message:
            return "valid_until"
        return None
    if isinstance(exc, (SourceUntrusted, SourceRedacted)):
        return None
    if isinstance(exc, ScopeViolation) or message.startswith("scope"):
        return "scope"
    if message.startswith("claim_type"):
        return "claim_type"
    if message.startswith("inference_basis"):
        return "inference_basis"
    if message.startswith("strength_reason"):
        return "strength_reason"
    if message.startswith("strength"):
        return "strength"
    if message.startswith("review_condition"):
        return "review_condition"
    if message.startswith("claim"):
        return "claim"
    if message.startswith("valid_until"):
        return "valid_until"
    if message.startswith("alternative_group_id"):
        return "alternative_group_id"
    if "belief body is missing required field(s)" in message:
        return "body"
    if "belief body has unknown field(s)" in message:
        return "body"
    if isinstance(exc, UnsupportedOperation) or message.startswith(
        "model output operation"
    ):
        return "operation"
    if "must have exactly operation" in message or "must be exactly operation" in message:
        return "operation/body/scope"
    if "body and scope must be JSON objects" in message:
        return "body/scope"
    return None


def repair_feedback(exc: BaseException) -> str:
    """Specific, contract-derived feedback for the single format repair.

    The old feedback was generic ("return one JSON object"), which did not tell
    the model that ``capability_assessment`` is not a claim type or that
    ``namespace`` is not a scope field.  This function names the erroneous
    field (when the validator message identifies it), states the allowed form
    from the same authoritative constants as the rendered contract, and keeps
    the frozen rejection rules unchanged.
    """

    message = str(exc)
    parts = [f"{type(exc).__name__}: {message}"]
    field = _erroneous_field(message, exc)
    if field is not None:
        parts.append(f"Erroneous field: {field}.")
    if "scope has unknown field(s):" in message:
        bad = message.split("scope has unknown field(s):", 1)[1].strip().rstrip(".")
        parts.append(
            f"scope must not contain {bad!r}; allowed scope fields are: "
            f"{', '.join(SCOPE_FIELDS)}."
        )
    if isinstance(exc, ScopeViolation) and "must be a scope object" in message:
        parts.append("scope must be a JSON object, not a string.")
    evidence_note = (
        "The proposal shape may be valid, but the evidence reference is not "
        "usable; use an EventRef from available_references with "
        "evidence_usable=true, or the exact Episode RecordRef."
    )
    if isinstance(exc, (SourceUntrusted, SourceRedacted)):
        parts.append(evidence_note)
    elif isinstance(exc, EvidenceMissing) and not (
        "scope" in message or "valid_until" in message
    ):
        parts.append(evidence_note)
    else:
        parts.append(proposal_format_hint())
    parts.append(
        "Return exactly one JSON object; do not supply state_revision, "
        "request_id, read_set or context_id."
    )
    return " ".join(parts)


__all__ = ["parse_proposal", "repair_feedback", "validate_proposal"]
