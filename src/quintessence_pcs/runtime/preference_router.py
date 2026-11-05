"""RG05-F02: route a model's ``cognition.propose`` into the expression layer.

The model is the only legal author of a ``cognition.change`` candidate (the v1
candidate contract freezes ``origin_kind=model_output``).  This router keeps
that constraint while making the host the authority for *what the user meant*:

* an explicit lasting preference becomes a host-normalized ``user_preference``
  candidate with the current user input as evidence and a host-built
  ``DirectiveBinding`` (the trusted, explicit path);
* a one-shot request never becomes a candidate or a durable record;
* any other preference the model infers from ordinary conversation is left to
  the normal change path -- it is a candidate like any other, reviewed for
  impact, and it never inherits an explicit directive's binding;
* a correction becomes a retract/replace (confirmed) or is already handled by
  the deterministic pre-turn withdrawal/revocation (pending).

Everything store-dependent is host-owned: the statement ref, the evidence
link, the scope, the strength and the claim text.  A model can trigger the
route only by proposing ``claim_type=user_preference``; it cannot supply an
authorization, a scope or a statement reference.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..domains.cognition.beliefs import INFERENCE_BASIS_BY_CLAIM_TYPE
from ..domains.persona.expression_directives import (
    DIRECTIVE_POLICY_ID,
    DIRECTIVE_POLICY_VERSION,
    KIND_AMBIGUOUS,
    KIND_EXPLICIT_CORRECTION,
    KIND_EXPLICIT_LONG_TERM,
    KIND_NONE,
    KIND_OVERREACH,
    KIND_SINGLE_SHOT,
    KIND_TENTATIVE,
    DirectivePlan,
    canonical_claim,
    canonical_value_label,
    match_expression_preference,
    preference_marker,
    quote_text,
)
from ..domains.persona.expression_preferences import ExpressionPreferenceService
from ..foundation import identifiers
from ..foundation.errors import InvalidInput, RecordNotFound, Redacted
from ..foundation.jsonutil import canonical_json_text
from ..collaboration.change_service import (
    ChangeAccess,
    CognitionChangeService,
    DirectiveBinding,
)
from ..collaboration.policy import CommitAuthority
from .tool_catalog import ToolArgumentsInvalid

#: Stable reason codes a route can report to the model.  They are not new
#: public PCS error codes; the tool layer surfaces them as a tool failure.
TURN_ONLY = "TURN_ONLY"
COMMITTED = "COMMITTED"
ALREADY_CONFIRMED = "ALREADY_CONFIRMED"
CORRECTION_WITHDRAWN = "CORRECTION_WITHDRAWN"
DIRECTIVE_MISMATCH = "DIRECTIVE_MISMATCH"
PREFERENCE_OUT_OF_SCOPE = "PREFERENCE_OUT_OF_SCOPE"
PREFERENCE_NOT_USER_STATEMENT = "PREFERENCE_NOT_USER_STATEMENT"
#: The model claims a *lasting expression* preference the host cannot verify
#: from the current input.  Falling through to the ordinary change path would
#: file the user's phrasing as one of the assistant's own beliefs, so this is a
#: refusal, not a silent downgrade.
PREFERENCE_NOT_VERIFIED = "PREFERENCE_NOT_VERIFIED"
#: An expression preference arrived packed with other changes.  The router owns
#: exactly one change per proposal, so a mixed bundle would send the expression
#: preference down the ordinary change path -- i.e. file it as one of the
#: assistant's own beliefs.  The model must propose it on its own.
PREFERENCE_SPLIT_REQUIRED = "PREFERENCE_SPLIT_REQUIRED"


@dataclass(frozen=True)
class PreferenceTarget:
    """One earlier preference a correction may act on."""

    kind: str  # confirmed | pending | tentative
    dimension: str
    value: str | None
    record_id: str | None = None
    record_version: int | None = None
    artifact_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "dimension": self.dimension,
            "value": self.value,
            "record_id": self.record_id,
            "record_version": self.record_version,
            "artifact_id": self.artifact_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "PreferenceTarget | None":
        if not isinstance(value, Mapping):
            return None
        kind = value.get("kind")
        dimension = value.get("dimension")
        if kind not in ("confirmed", "pending", "tentative"):
            return None
        if not isinstance(dimension, str):
            return None
        return cls(
            kind=str(kind),
            dimension=dimension,
            value=value.get("value") if isinstance(value.get("value"), str) else None,
            record_id=value.get("record_id") if isinstance(value.get("record_id"), str) else None,
            record_version=value.get("record_version") if type(value.get("record_version")) is int else None,
            artifact_id=value.get("artifact_id") if isinstance(value.get("artifact_id"), str) else None,
        )


@dataclass(frozen=True)
class TurnPreferenceContext:
    """Host state for one turn; never derived from model output."""

    namespace: str
    conversation_id: str
    turn_id: str
    input_ref: dict[str, Any]
    plan: DirectivePlan
    targets: tuple[PreferenceTarget, ...] = ()


class PreferenceRouter:
    """Turn-scoped bridge between ``cognition.propose`` and the expression layer."""

    def __init__(
        self,
        *,
        store: Any,
        changes: CognitionChangeService,
        preferences: ExpressionPreferenceService,
        directive_access: ChangeAccess,
        turn: TurnPreferenceContext,
        now: Any,
    ) -> None:
        self._store = store
        self._changes = changes
        self._preferences = preferences
        self._access = directive_access
        self._turn = turn
        self._now = now

    # ------------------------------------------------------------------ route

    def route(
        self, args: Mapping[str, Any], *, context_id: str | None = None
    ) -> Mapping[str, Any] | None:
        """Return a result to the model, or ``None`` for the normal path."""

        if context_id:
            object.__setattr__(self, "_context", context_id)
        changes = args.get("changes")
        if not isinstance(changes, list) or not changes:
            return None
        if len(changes) != 1:
            # The router owns one change.  A bundle that carries an expression
            # preference would hand it to the ordinary change path, where it
            # becomes a belief about the user; make the model send it alone.
            for change in changes:
                if self._expression_claim(change) is not None:
                    raise ToolArgumentsInvalid(
                        f"{PREFERENCE_SPLIT_REQUIRED}: an expression preference "
                        "must be proposed as its own single change, not bundled "
                        "with other beliefs"
                    )
            return None
        change = changes[0]
        if not isinstance(change, Mapping):
            return None
        if change.get("op") == "retract_belief":
            return self._retract(change)
        if change.get("op") not in ("propose_belief", "revise_belief"):
            return None
        body = change.get("body")
        if not isinstance(body, Mapping) or body.get("claim_type") != "user_preference":
            return None
        claim = body.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            return None

        plan = self._turn.plan
        if plan.kind == KIND_SINGLE_SHOT:
            return {
                "state": TURN_ONLY,
                "persisted": False,
                "reason": "single_shot_request",
            }
        if plan.kind in (KIND_OVERREACH, KIND_AMBIGUOUS):
            raise ToolArgumentsInvalid(
                f"{PREFERENCE_NOT_USER_STATEMENT}: the current input is not a "
                "usable preference statement"
            )

        claim_match = match_expression_preference(claim)
        if claim_match is None:
            # Not an expression preference the host can verify; leave the
            # normal E2 path in place.
            return None
        claim_dimension, claim_value = claim_match[0], claim_match[1]

        if plan.kind in (KIND_TENTATIVE, KIND_NONE):
            # J11 consolidation: the host no longer keeps a conversation-scoped
            # tentative preference *and* no longer vetoes a model-proposed
            # preference just because the wording was not an explicit directive.
            # The model may infer a lasting preference from an ordinary sentence;
            # that candidate takes the normal change path, where Review decides
            # its impact and whether it may be committed.  An inferred preference
            # never inherits the explicit directive's binding, so it cannot pass
            # itself off as a user authorization.
            return None

        if plan.kind == KIND_EXPLICIT_LONG_TERM:
            return self._explicit(plan, claim_dimension, claim_value)

        if plan.kind == KIND_EXPLICIT_CORRECTION:
            return self._correction(plan, claim_dimension, claim_value)

        # Any other plan kind leaves the change to the ordinary path: the host
        # has no explicit directive to bind, so the candidate is reviewed like
        # any other model proposal.
        return None

    # ------------------------------------------------------------- retraction

    @staticmethod
    def _expression_claim(change: Any) -> tuple[str, str, int, int] | None:
        """The expression preference a change claims, if it claims one."""

        if not isinstance(change, Mapping):
            return None
        body = change.get("body")
        if not isinstance(body, Mapping) or body.get("claim_type") != "user_preference":
            return None
        claim = body.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            return None
        return match_expression_preference(claim)

    def _retract(self, change: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """A model retraction is matched against the host-resolved old record."""

        plan = self._turn.plan
        if plan.kind != KIND_EXPLICIT_CORRECTION or plan.dimension is None:
            return None
        confirmed = self._confirmed_target(plan.dimension)
        if confirmed is None:
            return {
                "state": CORRECTION_WITHDRAWN,
                "dimension": plan.dimension,
                "persisted": False,
                "reason": "no_confirmed_target",
            }
        target = change.get("target")
        if isinstance(target, Mapping):
            record_id = target.get("record_id")
            if (
                isinstance(record_id, str)
                and confirmed.record_id is not None
                and record_id != confirmed.record_id
            ):
                raise ToolArgumentsInvalid(
                    f"{DIRECTIVE_MISMATCH}: the retraction target is not the "
                    "host-resolved preference"
                )
        return self._commit_correction(
            plan.dimension,
            plan.value,
            confirmed,
            reason="correction_confirmed",
        )

    # -------------------------------------------------------------- explicit

    def _explicit(
        self,
        plan: DirectivePlan,
        claim_dimension: str,
        claim_value: str,
    ) -> Mapping[str, Any]:
        if (plan.dimension, plan.value) != (claim_dimension, claim_value):
            raise ToolArgumentsInvalid(
                f"{DIRECTIVE_MISMATCH}: the proposed preference does not match "
                "the user's explicit directive"
            )
        confirmed = self._confirmed_target(plan.dimension)
        if confirmed is not None and confirmed.value == plan.value:
            return {
                "state": ALREADY_CONFIRMED,
                "dimension": plan.dimension,
                "value": plan.value,
                "record_id": confirmed.record_id,
            }
        if confirmed is not None:
            return self._commit_correction(
                plan.dimension, plan.value, confirmed, reason="explicit_change"
            )
        return self._commit_new(
            plan.dimension,
            plan.value,
            binding_kind="state_preference",
            reason="explicit_preference",
        )

    # ------------------------------------------------------------ correction

    def _correction(
        self,
        plan: DirectivePlan,
        claim_dimension: str,
        claim_value: str,
    ) -> Mapping[str, Any]:
        dimension = plan.dimension or claim_dimension
        if plan.value is not None and plan.value != claim_value:
            raise ToolArgumentsInvalid(
                f"{DIRECTIVE_MISMATCH}: the proposed replacement does not match "
                "the user's correction"
            )
        confirmed = self._confirmed_target(dimension)
        if confirmed is None:
            # The pending/tentative old state was already withdrawn/revoked by
            # the deterministic pre-turn step.  A new value can still become a
            # confirmed preference when the model proposed it.
            if plan.value is None:
                return {
                    "state": CORRECTION_WITHDRAWN,
                    "dimension": dimension,
                    "persisted": False,
                    "reason": "old_pending_or_tentative_already_withdrawn",
                }
            return self._commit_new(
                dimension,
                plan.value,
                binding_kind="state_preference",
                reason="correction_new_value",
            )
        return self._commit_correction(
            dimension,
            plan.value,
            confirmed,
            reason="correction_confirmed",
        )

    def _commit_correction(
        self,
        dimension: str,
        value: str | None,
        confirmed: PreferenceTarget,
        *,
        reason: str,
    ) -> Mapping[str, Any]:
        if confirmed.record_id is None or confirmed.record_version is None:
            raise InvalidInput("a confirmed correction target needs an exact version")
        bundle: list[dict[str, Any]] = [
            {
                "op": "retract_belief",
                "target": {
                    "ref_type": "record",
                    "namespace": self._turn.namespace,
                    "record_id": confirmed.record_id,
                    "record_version": confirmed.record_version,
                },
                "evidence_links": [
                    self._evidence_link("contradicts", "用户在本轮明确纠正该表达偏好")
                ],
                "reason": "用户明确纠正了该表达偏好",
            }
        ]
        if value is not None:
            bundle.append(
                {
                    "op": "propose_belief",
                    "body": self._canonical_body(
                        dimension, value, quote=self._turn_quote()
                    ),
                    "scope": dict(self._access_scope()),
                }
            )
        binding = self._binding(
            "revoke_previous",
            dimension,
            value,
            old_record_id=confirmed.record_id,
        )
        return self._propose_and_commit(
            bundle,
            binding,
            dimension=dimension,
            value=value,
            reason=reason,
        )

    def _commit_new(
        self,
        dimension: str,
        value: str,
        *,
        binding_kind: str,
        reason: str,
    ) -> Mapping[str, Any]:
        binding = self._binding(binding_kind, dimension, value)
        return self._propose_and_commit(
            [
                {
                    "op": "propose_belief",
                    "body": self._canonical_body(
                        dimension, value, quote=self._turn_quote()
                    ),
                    "scope": dict(self._access_scope()),
                }
            ],
            binding,
            dimension=dimension,
            value=value,
            reason=reason,
        )

    # -------------------------------------------------------------- commit

    def _propose_and_commit(
        self,
        changes: Sequence[Mapping[str, Any]],
        binding: DirectiveBinding,
        *,
        dimension: str,
        value: str | None,
        reason: str,
    ) -> Mapping[str, Any]:
        bundle = {
            "operation": "cognition.change",
            "summary": f"host-normalized expression preference: {dimension}",
            "changes": list(changes),
        }
        request_id = self._request_id(dimension, value, reason)
        result = self._changes.propose(
            self._access,
            context_id=self._context_id(),
            request_id=request_id,
            bundle=bundle,
        )
        committed = self._changes.commit(
            self._access,
            candidate_id=result.candidate_id,
            authority=CommitAuthority(directive=binding),
            decision_id=self._decision_id(dimension, value, reason),
        )
        # J11 consolidation: nothing supersedes a conversation-scoped tentative
        # record any more.  Legacy records stay readable and revocable, but no
        # active write path creates or supersedes them.
        return {
            "state": committed.state,
            "dimension": dimension,
            "value": value,
            "reason_code": committed.reason_code,
            "impact": committed.impact,
            "authority_kind": committed.authority_kind,
            "record_refs": list(committed.record_refs),
            "reason": reason,
        }

    # ------------------------------------------------------------- host facts

    def _canonical_body(
        self, dimension: str, value: str, *, quote: str | None = None
    ) -> dict[str, Any]:
        label = canonical_value_label(dimension, value)
        source = (quote or label).strip()
        if len(source) > 512:
            source = source[:512]
        return {
            "claim": canonical_claim(dimension, value),
            "claim_type": "user_preference",
            "evidence_links": [
                self._evidence_link("supports", "用户在本轮直接陈述了该表达偏好")
            ],
            "strength": "limited",
            "strength_reason": f"用户在本轮明确要求：{source}",
            "inference_basis": INFERENCE_BASIS_BY_CLAIM_TYPE["user_preference"],
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": preference_marker(dimension, value),
        }

    def _turn_quote(self) -> str | None:
        return quote_text(self._input_text(), self._turn.plan)

    def _evidence_link(self, relation: str, reason: str) -> dict[str, Any]:
        ref = dict(self._turn.input_ref)
        return {
            "ref": ref,
            "relation": relation,
            "origin_group_id": f"collab_input:{ref['input_id']}",
            "applicability_reason": reason,
        }

    def _binding(
        self,
        kind: str,
        dimension: str,
        value: str | None,
        *,
        old_record_id: str | None = None,
    ) -> DirectiveBinding:
        row = self._load_input()
        target: dict[str, Any] = {
            "dimension": dimension,
            "conversation_id": self._turn.conversation_id,
            "value": value or "none",
        }
        if old_record_id is not None:
            target["old_record_id"] = old_record_id
        return DirectiveBinding(
            namespace=self._turn.namespace,
            host_instance=self._access.host_instance,
            statement_ref=dict(self._turn.input_ref),
            statement_digest=row.content_digest,
            directive_kind=kind,
            target=target,
            allowed_claim_types=frozenset({"user_preference"}),
            decision_scope=dict(self._access_scope()),
            bound_at=self._now(),
            binding_policy_id=DIRECTIVE_POLICY_ID,
            binding_policy_version=DIRECTIVE_POLICY_VERSION,
            access_ref=self._access.access_ref,
            # The classifier/lexicon identity is audit-only; it is recorded on
            # the decision row so a later reader can name the exact rule version
            # that produced this binding without joining the turn strategy.
            classifier_policy_id=self._turn.plan.policy_id,
            classifier_policy_version=self._turn.plan.policy_version,
        )

    def _load_input(self) -> Any:
        ref = self._turn.input_ref
        try:
            row = self._store.load_input(
                namespace=self._turn.namespace,
                input_id=str(ref["input_id"]),
                content_version=int(ref["content_version"]),
            )
        except (Redacted, RecordNotFound) as exc:
            raise ToolArgumentsInvalid(
                f"{PREFERENCE_NOT_USER_STATEMENT}: the current input is unavailable"
            ) from exc
        if row.origin_kind != "user_statement":
            raise ToolArgumentsInvalid(
                f"{PREFERENCE_NOT_USER_STATEMENT}: the current input is not a user statement"
            )
        if not self._context_has_input(row.input_id, row.content_version):
            raise ToolArgumentsInvalid(
                f"{PREFERENCE_NOT_USER_STATEMENT}: the current input is not a "
                "source of this Context"
            )
        return row

    def _input_text(self) -> str:
        return self._load_input().body

    def _context_has_input(self, input_id: str, content_version: int) -> bool:
        try:
            sources = self._store.context_collab_sources(
                namespace=self._turn.namespace,
                context_id=self._context_id(),
            )
        except Exception:  # noqa: BLE001 - a missing Context grants nothing
            return False
        return any(
            source.get("ref_type") == "collab_input"
            and source.get("input_id") == input_id
            and int(source.get("content_version", 0)) == int(content_version)
            for source in sources
        )

    def _confirmed_target(self, dimension: str) -> PreferenceTarget | None:
        for target in self._turn.targets:
            if target.kind == "confirmed" and target.dimension == dimension:
                return target
        return None

    def _access_scope(self) -> Mapping[str, Any]:
        # The router is constructed with a scope-bearing access; the actual
        # scope is carried on the turn context through a private attribute set
        # by the sidecar.  A missing scope is a configuration error, not a
        # reason to invent a global one.
        scope = getattr(self, "_scope", None)
        if not scope:
            raise InvalidInput("expression routing requires a non-empty host scope")
        return scope

    def _context_id(self) -> str:
        context_id = getattr(self, "_context", None)
        if not context_id:
            raise InvalidInput("expression routing requires a bound Context")
        return context_id

    def _request_id(self, dimension: str, value: str | None, reason: str) -> str:
        return str(
            uuid.uuid5(
                _ROUTE_NAMESPACE,
                f"{self._turn.namespace}\x1f{self._turn.turn_id}\x1f"
                f"{reason}\x1f{dimension}\x1f{value or 'none'}",
            )
        )

    def _decision_id(self, dimension: str, value: str | None, reason: str) -> str:
        return str(
            uuid.uuid5(
                _ROUTE_NAMESPACE,
                f"{self._turn.namespace}\x1f{self._turn.turn_id}\x1fdecision\x1f"
                f"{reason}\x1f{dimension}\x1f{value or 'none'}",
            )
        )


_ROUTE_NAMESPACE = uuid.UUID("b7a1e2c4-6f3d-5a90-8c21-4d5e6f7a8b9c")


def bind_router_context(
    router: PreferenceRouter,
    *,
    scope: Mapping[str, Any],
    context_id: str = "",
) -> PreferenceRouter:
    """Attach the host scope (and optionally a first Context) to a router."""

    object.__setattr__(router, "_context", context_id)
    object.__setattr__(router, "_scope", dict(scope))
    return router


__all__ = [
    "ALREADY_CONFIRMED",
    "COMMITTED",
    "CORRECTION_WITHDRAWN",
    "DIRECTIVE_MISMATCH",
    "PREFERENCE_NOT_USER_STATEMENT",
    "PREFERENCE_OUT_OF_SCOPE",
    "PreferenceRouter",
    "PreferenceTarget",
    "TURN_ONLY",
    "TurnPreferenceContext",
    "bind_router_context",
]
