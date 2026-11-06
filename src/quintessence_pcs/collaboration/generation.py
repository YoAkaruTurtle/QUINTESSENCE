"""Shared, single-copy generation proof for every chat/consult model call.

RG02 adds a second caller (the conversation orchestrator) for exactly the same
audit chain the consult facade already uses.  The three pieces live here so the
proof cannot drift:

* :func:`generate_once` performs one logical generation and returns the raw
  text, the settled attempt evidence and (when parsing succeeded) the parsed
  output;
* :func:`require_exact_attempt` proves the evidence is the last settled ``ok``
  attempt of *this* run/Context;
* :func:`allowed_references` / :func:`artifact_sources` derive the reference
  vocabulary and the derived-artifact source closure from the persisted
  Context only.

The module never calls a query service, never commits cognition and never
writes a reply artifact.  Callers own those decisions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from ..domains.refs import EventRef, RecordRef
from ..foundation import identifiers
from ..foundation.errors import (
    CorruptStore,
    DbBusy,
    InvalidInput,
    StorageError,
)
from ..foundation.jsonutil import canonical_json_text
from ..runtime.collab_protocol import (
    COLLAB_PROTOCOL_VERSION,
    CollabOutput,
    parse_collab_output,
    repair_feedback,
)
from ..runtime.model_port import ModelBudgetExceededError, ModelNotAuthorizedError

_ATTEMPT_REF_KEYS = frozenset(
    {"authorization_id", "attempt_id", "attempt_no", "request_hash"}
)


@dataclass(frozen=True)
class GenerationResult:
    raw: str
    evidence: Any
    output: CollabOutput | None
    parse_error: InvalidInput | None
    #: The settled attempt's provider finish reason.  ``None`` only for a
    #: caller-constructed result; a real ``ok`` attempt always has one.
    finish_reason: str | None = None


class GenerationFailure(Exception):
    """One generation could not produce a provable, parsed answer."""

    def __init__(self, phase: str, detail: str) -> None:
        super().__init__(f"{phase}:{detail}")
        self.phase = phase
        self.detail = detail


def generate_once(
    store: Any,
    model: Any,
    *,
    context: Any,
    allowed_references: tuple[dict[str, Any], ...],
    feedback: str | None = None,
    protocol_version: str = COLLAB_PROTOCOL_VERSION,
) -> GenerationResult:
    """Run one logical generation and prove its exact attempt."""

    try:
        binder = model.bind_run(namespace=context.namespace, context=context)
        with binder:
            raw = model.propose(
                rendered_input=context.rendered_input,
                references=allowed_references,
                feedback=feedback,
            )
    except (CorruptStore, DbBusy):
        raise
    except Exception as exc:  # noqa: BLE001 - model refusal is a result
        raise GenerationFailure("model", phase_label(exc)) from exc

    try:
        evidence = model.last_attempt_evidence(
            run_id=context.run_id, context_id=context.context_id
        )
    except (CorruptStore, DbBusy):
        raise
    except Exception as exc:  # noqa: BLE001 - a broken adapter is a result
        raise GenerationFailure("attempt_evidence", phase_label(exc)) from exc
    if evidence is None:
        raise GenerationFailure("attempt_evidence", "attempt_evidence_missing")
    if evidence.content != raw:
        raise GenerationFailure("attempt_evidence", "attempt_evidence_mismatch")
    attempt = require_exact_attempt(store, context, evidence)

    try:
        output = parse_collab_output(
            raw,
            mode="consult",
            allowed_references=allowed_references,
            protocol_version=protocol_version,
        )
    except InvalidInput as exc:
        return GenerationResult(
            raw=raw,
            evidence=evidence,
            output=None,
            parse_error=exc,
            finish_reason=attempt.finish_reason,
        )
    return GenerationResult(
        raw=raw,
        evidence=evidence,
        output=output,
        parse_error=None,
        finish_reason=attempt.finish_reason,
    )


def require_exact_attempt(store: Any, context: Any, evidence: Any) -> Any:
    """Prove the evidence is the settled attempt of this exact generation."""

    ref = evidence.attempt_ref
    if not isinstance(ref, dict) or set(ref) != _ATTEMPT_REF_KEYS:
        raise GenerationFailure("attempt_evidence", "attempt_evidence_mismatch")
    attempt_id = ref.get("attempt_id")
    if not isinstance(attempt_id, str):
        raise GenerationFailure("attempt_evidence", "attempt_evidence_mismatch")
    try:
        _require_uuid(attempt_id)
    except StorageError as exc:
        raise GenerationFailure(
            "attempt_evidence", "attempt_evidence_mismatch"
        ) from exc
    attempts = store.model_attempts_for_run(context.run_id)
    matches = [attempt for attempt in attempts if attempt.attempt_id == attempt_id]
    if len(matches) != 1:
        raise GenerationFailure("attempt_evidence", "attempt_evidence_mismatch")
    attempt = matches[0]
    if (
        attempt.namespace != context.namespace
        or attempt.context_id != context.context_id
        or attempt.authorization_id != ref.get("authorization_id")
        or attempt.attempt_no != ref.get("attempt_no")
        or attempt.request_hash != ref.get("request_hash")
        or attempt.outcome != "ok"
        or not attempt.finished
    ):
        raise GenerationFailure("attempt_evidence", "attempt_evidence_mismatch")
    if not attempts or attempts[-1].attempt_no != attempt.attempt_no:
        raise GenerationFailure("attempt_evidence", "attempt_evidence_mismatch")
    return attempt


def allowed_references(context: Any) -> tuple[dict[str, Any], ...]:
    """The parser's closed reference vocabulary, from the formal Context only."""

    refs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(reference: dict[str, Any]) -> None:
        value = dict(reference)
        key = canonical_json_text(value)
        if key not in seen:
            seen.add(key)
            refs.append(value)

    for reference in context.collab_source_refs:
        add(reference)
    for entry in context.read_set:
        add(entry.ref.to_dict())
    for source in context.sources:
        if source.ref_kind == "event":
            add(
                EventRef(
                    namespace=context.namespace, event_id=source.ref_id
                ).to_dict()
            )
    return tuple(refs)


def artifact_sources(context: Any) -> list[dict[str, Any]]:
    """Every exact material source the final Context used, including inputs.

    RG02 history rows are deliberately absent: they are recorded on the
    Context's own history ledger and must not create an artifact-to-artifact
    chain (the closure depth is a frozen limit).
    """

    refs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(reference: dict[str, Any]) -> None:
        value = dict(reference)
        key = canonical_json_text(value)
        if key not in seen:
            seen.add(key)
            refs.append(value)

    for reference in context.collab_source_refs:
        add(reference)
    for source in context.sources:
        if source.ref_kind == "event":
            add(
                EventRef(
                    namespace=context.namespace, event_id=source.ref_id
                ).to_dict()
            )
        else:
            add(
                RecordRef(
                    namespace=context.namespace,
                    record_id=source.ref_id,
                    record_version=int(source.ref_version),
                ).to_dict()
            )
    return refs


def repair_feedback_for(
    error: InvalidInput, *, protocol_version: str = COLLAB_PROTOCOL_VERSION
) -> str:
    return repair_feedback(error, protocol_version=protocol_version)


def phase_label(exc: BaseException) -> str:
    if isinstance(exc, ModelNotAuthorizedError):
        return {
            "authorization_expired": "MODEL_AUTHORIZATION_EXPIRED",
            "authorization_mismatch": "MODEL_AUTHORIZATION_MISMATCH",
            "authorization_missing": "MODEL_AUTHORIZATION_MISSING",
            "key_missing": "MODEL_KEY_MISSING",
        }.get(exc.reason, "MODEL_NOT_AUTHORIZED")
    if isinstance(exc, ModelBudgetExceededError) and exc.reason == "calls_exhausted":
        return "MODEL_CALLS_EXHAUSTED"
    code = getattr(exc, "code", None)
    label = code if isinstance(code, str) and code else type(exc).__name__
    return label


def _require_uuid(value: str) -> str:
    if identifiers.UUID_PATTERN.fullmatch(value) is None:
        raise InvalidInput("attempt_id must be a canonical lower-case UUID")
    return value


__all__ = [
    "GenerationFailure",
    "GenerationResult",
    "allowed_references",
    "artifact_sources",
    "generate_once",
    "phase_label",
    "repair_feedback_for",
    "require_exact_attempt",
]
