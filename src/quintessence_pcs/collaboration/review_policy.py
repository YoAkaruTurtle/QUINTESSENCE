"""J06 S6b: impact classification, evidence linkage and the review policy.

This module is deliberately pure.  It never opens a transaction, never reads a
payload body and never grants authority: the rules here decide *how much review a
change needs* and *whether the evidence type mechanically matches the claim*, not
*who may commit it*.  ``accept`` means "worth handing to the authority decision",
never "this is true" and never "this may be committed".

Three vocabularies are frozen here:

* impact levels ``E0 < E1 < E2 < BLOCKED`` — ``BLOCKED`` is not "high risk", it
  is "no authority may commit this in J06" (``derived_rule`` and rule publishing);
* evidence linkage ``verified | unverified | not_applicable`` — a *type* match
  only, never a semantic judgement of the evidence's content;
* support basis ``bound_user_claim | evidence_backed | provisional_hypothesis |
  bound_cancel | none``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from ..domains.cognition.beliefs import (
    SUPPORTING_RELATIONS,
    derive_status,
)
from ..domains.cognition.changes import CHANGE_OPS, ValidatedChange
from ..domains.scope import scope_matches
from ..foundation.errors import CorruptStore, InvalidInput
from ..foundation.jsonutil import canonical_json_text, sha256_hex
#: Re-exported from the store layer: the decision column and this policy
#: must agree on exactly one vocabulary.
from ..store.governance import IMPACT_LEVELS

# ---------------------------------------------------------------------------
# frozen vocabularies
# ---------------------------------------------------------------------------

_IMPACT_RANK = {level: rank for rank, level in enumerate(IMPACT_LEVELS)}

EVIDENCE_LINKAGES = ("verified", "unverified", "not_applicable")

SUPPORT_BASES = (
    "bound_user_claim",
    "evidence_backed",
    "provisional_hypothesis",
    "bound_cancel",
    "none",
)

RECOMMENDATIONS = ("accept", "defer", "reject", "escalate")

#: Claim types that only the user may originate; they always need a binding.
USER_OWNED_CLAIM_TYPES = frozenset({"user_preference", "project_decision"})

#: The event-type prefixes that may mechanically stand behind a claim type.
#: This is a *type* registry, not a relevance judgement: a matching event type
#: does not prove the event supports the claim, only that it is the right kind of
#: observation to be considered.  Hosts may override it, which must change
#: ``policy_version``.
#:
#: Prefixes must stay specific.  A bare ``"ToolResult:"`` matches *any* tool
#: result (a weather lookup, a git status), so a single unrelated tool call would
#: be enough to mark a candidate ``verified`` and send it down the E1 automatic
#: path.  A host that really has structured tool results must register a narrower
#: prefix (for example ``"ToolResult:Capability:"``); registering the bare prefix
#: is equivalent to switching this check off.
EVIDENCE_EVENT_TYPES: Mapping[str, tuple[str, ...]] = {
    "capability": ("CapabilityObserved:", "CapabilityTested:"),
    "environment_state": ("EnvironmentObserved:", "EnvironmentChanged:"),
    "user_preference": ("UserStatement:",),
    "project_decision": ("ProjectDecision:",),
    "general_hypothesis": (),
    "derived_rule": (),
    # J1: "the page says X" is checkable against the captured fetch; "X is true"
    # is not.  Only a fetch event may stand behind an external_report.
    "external_report": ("WebPageFetched:",),
}

BASE_POLICY_ID = "policy:cognition.review.base.v2"
BASE_POLICY_VERSION = "1"
IMPACT_POLICY_ID = "policy:cognition.risk.v1"
IMPACT_POLICY_VERSION = "1"

#: A bundle may be auto-submitted only when it stays this small and this local.
MAX_AUTO_CHANGES = 2
MAX_AUTO_TARGETS = 2


class PolicyConfigurationError(InvalidInput):
    """An impact/review policy configuration is unusable; fail closed."""


# ---------------------------------------------------------------------------
# inputs that the caller resolves from the store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetFact:
    """What the store currently says about a change target.

    The caller reads it inside its own snapshot; the policy never touches the
    store, so a stale fact can only make the classification *more* cautious, and
    the commit guard re-checks versions in the transaction.
    """

    status: str
    claim_type: str


@dataclass(frozen=True)
class ChangeFact:
    """One change reduced to the fields the impact rules need."""

    op: str
    claim_type: str | None
    scope: Mapping[str, Any]
    target_record_id: str | None
    has_support: bool
    has_contradiction: bool
    expired: bool
    #: The claimed support level (``STRENGTHS``) for belief bodies, else ``None``.
    #: It expresses *how well supported* the claim is.  It is deliberately not a
    #: proxy for impact: weak support never lowers a high-impact change, and a
    #: confident label never raises support.
    strength: str | None = None
    #: How many distinct supporting origin groups the change itself carries.  An
    #: independent group is one source of support; the same URL fetched twice is
    #: one group, so it can never be counted as two independent supports.
    support_groups: int = 0


@dataclass(frozen=True)
class ImpactDecision:
    """One impact verdict with the reasons that produced it."""

    level: str
    reasons: tuple[str, ...]
    policy_id: str = IMPACT_POLICY_ID
    policy_version: str = IMPACT_POLICY_VERSION

    @property
    def blocked(self) -> bool:
        return self.level == "BLOCKED"

    @property
    def requires_human(self) -> bool:
        return self.level in ("E2", "BLOCKED")


@dataclass(frozen=True)
class RiskPolicyConfig:
    """Tunable, host-owned impact thresholds.

    Only the host constructs this; it is never read from model output, request
    JSON or a record body.  Changing it must bump ``policy_version``.
    """

    policy_id: str = IMPACT_POLICY_ID
    policy_version: str = IMPACT_POLICY_VERSION
    max_auto_changes: int = MAX_AUTO_CHANGES
    max_auto_targets: int = MAX_AUTO_TARGETS
    unknown_default: str = "E2"
    #: How long a rejected fingerprint keeps forcing emphasis on human review.
    #: A guard window, not a permanent ban: a legitimate change of mind after the
    #: window is a new decision with a new human, not a repeat offence.
    rejection_guard_days: int = 30

    def __post_init__(self) -> None:
        if self.policy_id != IMPACT_POLICY_ID:
            raise PolicyConfigurationError("unexpected impact policy id")
        if not isinstance(self.policy_version, str) or not self.policy_version:
            raise PolicyConfigurationError("impact policy_version must be a string")
        if self.unknown_default not in ("E1", "E2", "BLOCKED"):
            raise PolicyConfigurationError("unknown_default must be E1, E2 or BLOCKED")
        if type(self.rejection_guard_days) is not int or self.rejection_guard_days < 1:
            raise PolicyConfigurationError(
                "rejection_guard_days must be a positive integer"
            )
        for name in ("max_auto_changes", "max_auto_targets"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise PolicyConfigurationError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class PolicyStateView:
    """The bounded, already-authorized facts the policy may look at."""

    predicted_status: str
    evidence_linkage: str
    impact: ImpactDecision
    target_facts: Mapping[str, TargetFact] = field(default_factory=dict)
    has_directive_binding: bool = False
    has_competing_current: bool = False
    target_moved: bool = False
    prior_rejections: int = 0
    now: str | None = None

    def __post_init__(self) -> None:
        if self.predicted_status not in (
            "proposed",
            "supported",
            "contested",
            "contradicted",
            "stale",
            "none",
        ):
            raise InvalidInput("unknown predicted_status")
        if self.evidence_linkage not in EVIDENCE_LINKAGES:
            raise InvalidInput("unknown evidence_linkage")
        if type(self.prior_rejections) is not int or self.prior_rejections < 0:
            raise InvalidInput("prior_rejections must be a non-negative integer")


@dataclass(frozen=True)
class PolicyDecision:
    """The review recommendation.  It carries no authority and no grant."""

    recommendation: str
    support_basis: str
    derived_status: str
    requires_human: bool
    reason_code: str
    policy_id: str
    policy_version: str
    review_after: str | None = None

    def __post_init__(self) -> None:
        if self.recommendation not in RECOMMENDATIONS:
            raise InvalidInput("unknown recommendation")
        if self.support_basis not in SUPPORT_BASES:
            raise InvalidInput("unknown support_basis")


# ---------------------------------------------------------------------------
# scope containment
# ---------------------------------------------------------------------------


def scope_contained(
    candidate: Mapping[str, Any] | None,
    authorized: Mapping[str, Any] | None,
) -> bool:
    """Whether every situation denoted by ``candidate`` is inside ``authorized``.

    The reference semantics is :func:`quintessence_pcs.domains.scope.scope_matches`:
    a record scope narrows the set of queries it applies to, so *more* constraints
    mean a *smaller* denotation.  ``⟦candidate⟧ ⊆ ⟦authorized⟧`` therefore requires
    every constraint of ``authorized`` to be present and equal in ``candidate``
    (for ``entity_ids``: the authorized set is contained in the candidate set).

    An empty candidate denotes everything and can never be contained in a bounded
    authorization; an empty authorization is not a bound at all and is refused.
    Only the five closed scope fields exist: no unions, negations, wildcards or
    aliases are interpreted here.
    """

    if not authorized or not candidate:
        return False
    for key, expected in authorized.items():
        if key not in candidate:
            return False
        if key == "entity_ids":
            if not set(expected) <= set(candidate[key]):
                return False
        elif candidate[key] != expected:
            return False
    return True


# ---------------------------------------------------------------------------
# change reduction and status prediction
# ---------------------------------------------------------------------------


def _parse_timestamp(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:  # pragma: no cover - callers pass validated stamps
        raise InvalidInput("timestamp must be RFC 3339 UTC") from exc


def _is_expired(valid_until: Any, now: datetime) -> bool:
    if not isinstance(valid_until, str) or not valid_until:
        return False
    return _parse_timestamp(valid_until) <= now


def describe_changes(
    validated: ValidatedChange,
    *,
    now: str | None = None,
) -> tuple[ChangeFact, ...]:
    """Reduce a validated bundle to the fields the impact rules need."""

    reference = _parse_timestamp(now) if now is not None else datetime.now(timezone.utc)
    facts: list[ChangeFact] = []
    for change in validated.changes:
        op = change["op"]
        if op not in CHANGE_OPS:  # pragma: no cover - validate_change_bundle owns this
            raise InvalidInput(f"unknown change op {op!r}")
        if op in ("resolve_question", "close_question", "reopen_question"):
            target = change["target"]
            facts.append(
                ChangeFact(
                    op=op,
                    claim_type=None,
                    scope={},
                    target_record_id=target.get("record_id"),
                    has_support=False,
                    has_contradiction=False,
                    expired=False,
                )
            )
            continue
        if op == "retract_belief":
            links = change.get("evidence_links", ())
            target = change["target"]
            facts.append(
                ChangeFact(
                    op=op,
                    claim_type=None,
                    scope={},
                    target_record_id=target.get("record_id"),
                    has_support=any(
                        link.get("relation") in SUPPORTING_RELATIONS for link in links
                    ),
                    has_contradiction=any(
                        link.get("relation") == "contradicts" for link in links
                    ),
                    expired=False,
                )
            )
            continue
        body = change["body"]
        links = body.get("evidence_links", ())
        facts.append(
            ChangeFact(
                op=op,
                claim_type=body.get("claim_type"),
                scope=dict(change.get("scope") or {}),
                target_record_id=(change.get("target") or {}).get("record_id"),
                has_support=any(
                    link.get("relation") in SUPPORTING_RELATIONS for link in links
                ),
                has_contradiction=any(
                    link.get("relation") == "contradicts" for link in links
                ),
                expired=_is_expired(body.get("valid_until"), reference),
                strength=body.get("strength"),
                support_groups=len(
                    {
                        link.get("origin_group_id")
                        for link in links
                        if link.get("relation") in SUPPORTING_RELATIONS
                    }
                ),
            )
        )
    return tuple(facts)


def predict_status(fact: ChangeFact) -> str:
    """The status this change would derive, computed without the store."""

    if fact.op == "retract_belief":
        return "retracted"
    if fact.claim_type is None:  # pragma: no cover - question ops are filtered out
        return "none"
    return derive_status(
        claim_type=fact.claim_type,
        has_support=fact.has_support,
        has_contradiction=fact.has_contradiction,
        expired=fact.expired,
        retract=False,
    )


def _worst_status(statuses: Sequence[str]) -> str:
    """The status that demands the most caution, for a whole bundle."""

    rank = {
        "none": 0,
        "proposed": 1,
        "stale": 2,
        "supported": 3,
        "contested": 4,
        "contradicted": 5,
        "retracted": 6,
    }
    return max(statuses, key=lambda item: rank.get(item, 0), default="none")


# ---------------------------------------------------------------------------
# evidence linkage
# ---------------------------------------------------------------------------


def evidence_linkage_diagnosis(
    *,
    predicted_status: str,
    claim_type: str | None,
    supporting_event_types: Sequence[str],
    registry: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[str, str | None]:
    """Return ``(value, problem)`` for one candidate's evidence.

    ``problem`` is ``None`` for "nothing to say" and for "no evidence at all";
    it is ``evidence_type_mismatch`` only when supporting links exist but at
    least one of them is not the right *kind* of observation for the claim.

    The distinction matters: missing evidence is a legitimate tentative state
    (``proposed``), while mismatched evidence is a wrong association that must
    be refused rather than downgraded.  The frozen three-value column cannot
    express it, so callers record the stable ``reason_code``.
    """

    if predicted_status in ("proposed", "none", "retracted"):
        return "not_applicable", None
    if claim_type is None:
        return "unverified", None
    allowed = (registry or EVIDENCE_EVENT_TYPES).get(claim_type, ())
    types = [item for item in supporting_event_types if isinstance(item, str) and item]
    if not allowed or not types:
        return "unverified", None
    for event_type in types:
        if not any(event_type.startswith(prefix) for prefix in allowed):
            return "unverified", "evidence_type_mismatch"
    return "verified", None


def evidence_linkage(
    *,
    predicted_status: str,
    claim_type: str | None,
    supporting_event_types: Sequence[str],
    registry: Mapping[str, tuple[str, ...]] | None = None,
) -> str:
    """Whether the *type* of the supporting events matches the claim type.

    ``verified`` means "the evidence is the right kind of observation to be
    considered", never "the evidence proves the claim".  A provisional claim does
    not assert support at all, so its linkage is ``not_applicable``.

    A host-supplied ``registry`` is honoured as given, but the same warning as on
    the default registry applies: a bare prefix such as ``"ToolResult:"`` matches
    every tool result, so it turns this check into a no-op.  Narrower prefixes
    (``"ToolResult:Capability:"``) are the supported way to admit tool output.
    """

    if predicted_status == "proposed":
        return "not_applicable"
    if predicted_status in ("none", "retracted"):
        return "not_applicable"
    if claim_type is None:
        return "unverified"
    allowed = (registry or EVIDENCE_EVENT_TYPES).get(claim_type, ())
    types = [item for item in supporting_event_types if isinstance(item, str) and item]
    if not allowed or not types:
        return "unverified"
    for event_type in types:
        if not any(event_type.startswith(prefix) for prefix in allowed):
            return "unverified"
    return "verified"


# ---------------------------------------------------------------------------
# impact classification
# ---------------------------------------------------------------------------


#: Support levels, weakest first.  Mirrors ``beliefs.STRENGTHS``; duplicated here
#: because the policy layer must not import the domain module at runtime.
STRENGTH_RANK = {
    "insufficient": 0,
    "limited": 1,
    "moderate": 2,
    "strong": 3,
}


def _support_cap_rank(groups: int) -> int:
    """The strongest level this many independent support groups can justify."""

    if groups <= 0:
        return 0
    if groups == 1:
        return 1
    if groups == 2:
        return 2
    return 3


def classify_impact(
    validated: ValidatedChange,
    *,
    target_facts: Mapping[str, TargetFact] | None = None,
    authorized_scope: Mapping[str, Any] | None = None,
    has_directive_binding: bool = False,
    evidence_linkage_value: str = "not_applicable",
    now: str | None = None,
    config: RiskPolicyConfig | None = None,
) -> ImpactDecision:
    """Classify a bundle as ``E0``, ``E1``, ``E2`` or ``BLOCKED``.

    The verdict is the worst one any single change earns, so splitting a bundle
    cannot lower an individual change's risk.  The function is pure: everything
    store-dependent (current target status, event types) is passed in by the
    caller.
    """

    settings = config or RiskPolicyConfig()
    facts = describe_changes(validated, now=now)
    target_facts = target_facts or {}
    reasons: list[str] = []
    level = "E0"

    def raise_to(candidate: str, reason: str) -> None:
        nonlocal level
        reasons.append(reason)
        if _IMPACT_RANK[candidate] > _IMPACT_RANK[level]:
            level = candidate

    targets: set[str] = set()
    projects: set[str] = set()
    for index, fact in enumerate(facts):
        if fact.op == "propose_belief":
            targets.add(f"new:{index}")
        elif fact.target_record_id:
            targets.add(fact.target_record_id)
        project = fact.scope.get("project_id")
        if isinstance(project, str) and project:
            projects.add(project)

        if fact.claim_type == "derived_rule":
            raise_to("BLOCKED", "rule_publication_not_supported")

        if fact.claim_type in USER_OWNED_CLAIM_TYPES and not has_directive_binding:
            raise_to("E2", "user_owned_claim_without_binding")

        if fact.scope == {} and fact.op in ("propose_belief", "revise_belief"):
            raise_to("E2", "global_scope")
        elif (
            fact.op in ("propose_belief", "revise_belief")
            and authorized_scope is not None
            and not scope_contained(fact.scope, authorized_scope)
        ):
            raise_to("E2", "scope_not_authorized")

        if fact.op == "resolve_question":
            raise_to("E2", "question_resolution_requires_review")

        if fact.op == "close_question" and not has_directive_binding:
            raise_to("E2", "question_close_without_binding")

        if fact.op == "reopen_question":
            # Re-opening a resolved question is a claim about earlier work, not a
            # new question: it always needs a human, never an automatic authority.
            raise_to("E2", "question_reopen_requires_review")

        if fact.op == "retract_belief":
            record_id = fact.target_record_id
            known = target_facts.get(record_id) if record_id else None
            if known is None:
                raise_to("E2", "retraction_target_unknown")
            elif known.status == "supported" and known.claim_type not in USER_OWNED_CLAIM_TYPES:
                raise_to("E2", "retracts_supported_fact")

        if fact.op == "revise_belief" and fact.target_record_id in target_facts:
            known = target_facts[fact.target_record_id]
            if known.status == "supported" and known.claim_type not in USER_OWNED_CLAIM_TYPES:
                raise_to("E2", "revises_supported_fact")

        # J2: ``strength`` may only ever *raise* the level.  It flags a claim
        # that asserts more support than its own links can justify (one source
        # cannot carry "strong").  Two deliberate limits keep the autonomous
        # tentative path intact:
        #   * a change with no supporting link is not flagged -- declaring
        #     "有限支持" on a provisional hypothesis is the normal case, and the
        #     domain clamps the stored strength anyway;
        #   * nothing here can lower a level, so weak support never turns E2
        #     into E1/E0.
        if (
            fact.op in ("propose_belief", "revise_belief")
            and fact.support_groups > 0
            and fact.strength in STRENGTH_RANK
            and STRENGTH_RANK[fact.strength] > _support_cap_rank(fact.support_groups)
        ):
            raise_to("E2", "strength_overstated")

    if len(targets) > MAX_AUTO_TARGETS:
        raise_to("E2", "too_many_targets")
    if len(projects) >= 2:
        raise_to("E2", "cross_project")
    if len(facts) > settings.max_auto_changes:
        raise_to("E2", "too_many_changes")

    # A retraction is classified by the status of its *target*, not by the
    # "retracted" status it would write, so only proposing/revising facts take
    # part in the derived-status prediction.
    belief_facts = [
        fact for fact in facts if fact.op in ("propose_belief", "revise_belief")
    ]
    predicted = (
        _worst_status([predict_status(fact) for fact in belief_facts])
        if belief_facts
        else "none"
    )

    if level == "E0" and predicted in ("supported", "contested", "contradicted", "stale"):
        if evidence_linkage_value == "verified":
            raise_to("E1", "evidence_type_matches")
        else:
            raise_to("E2", "evidence_linkage_unverified")

    if level == "E0" and belief_facts and predicted not in (
        "proposed",
        "supported",
        "contested",
        "contradicted",
        "stale",
    ):
        raise_to(settings.unknown_default, "unknown_predicted_status")

    if level == "E0" and not belief_facts and any(
        fact.op == "close_question" for fact in facts
    ):
        raise_to("E1", "bound_question_close")

    return ImpactDecision(
        level=level,
        reasons=tuple(reasons) or ("local_provisional_change",),
        policy_id=settings.policy_id,
        policy_version=settings.policy_version,
    )


# ---------------------------------------------------------------------------
# review policy
# ---------------------------------------------------------------------------


class ReviewPolicy(Protocol):
    """A policy decides *whether a change is worth submitting*, not who may."""

    policy_id: str
    policy_version: str

    def evaluate(
        self, candidate: ValidatedChange, state: PolicyStateView
    ) -> PolicyDecision: ...


def _support_basis(
    candidate: ValidatedChange, state: PolicyStateView
) -> str:
    ops = {change["op"] for change in candidate.changes}
    if state.predicted_status == "proposed":
        return "provisional_hypothesis"
    if ops == {"close_question"} and state.has_directive_binding:
        return "bound_cancel"
    if state.has_directive_binding:
        return "bound_user_claim"
    if state.evidence_linkage == "verified":
        return "evidence_backed"
    return "none"


class BaseReviewPolicy:
    """``policy:cognition.review.base.v2`` -- the only shipped policy."""

    policy_id = BASE_POLICY_ID
    policy_version = BASE_POLICY_VERSION

    def evaluate(
        self, candidate: ValidatedChange, state: PolicyStateView
    ) -> PolicyDecision:
        impact = state.impact
        basis = _support_basis(candidate, state)

        def decide(
            recommendation: str,
            reason: str,
            *,
            requires_human: bool,
            status: str | None = None,
        ) -> PolicyDecision:
            return PolicyDecision(
                recommendation=recommendation,
                support_basis=basis,
                derived_status=status or state.predicted_status,
                requires_human=requires_human,
                reason_code=reason,
                policy_id=self.policy_id,
                policy_version=self.policy_version,
            )

        # Rule 0: rule publication is never a J06 submission, whatever the risk.
        if impact.blocked:
            return decide("defer", "rule_publication_not_supported", requires_human=True)
        if state.has_competing_current:
            return decide("escalate", "competing_claim", requires_human=True)
        if state.target_moved:
            return decide("escalate", "target_moved", requires_human=True)
        if state.prior_rejections > 0:
            return decide("escalate", "previously_rejected", requires_human=True)
        if impact.level == "E2":
            return decide("accept", "impact_requires_human", requires_human=True)
        if impact.level == "E1":
            return decide("accept", "supported_change", requires_human=False)
        if state.predicted_status == "proposed":
            return decide("accept", "provisional_change", requires_human=False)
        return decide("defer", "insufficient_basis", requires_human=False)


BASE_POLICY = BaseReviewPolicy()


def evaluate_policy(
    policy: ReviewPolicy, candidate: ValidatedChange, state: PolicyStateView
) -> PolicyDecision:
    """Run a policy and prove it is the implementation it claims to be.

    A policy that reports an id/version it does not implement is corrupt, not
    "unknown": silently trusting it would let a swapped policy masquerade as the
    reviewed one.
    """

    if (
        policy.policy_id != BASE_POLICY_ID
        or policy.policy_version != BASE_POLICY_VERSION
    ):
        raise CorruptStore(
            f"review policy {policy.policy_id!r}/{policy.policy_version!r} does not "
            "match the implemented policy"
        )
    decision = policy.evaluate(candidate, state)
    if (
        decision.policy_id != policy.policy_id
        or decision.policy_version != policy.policy_version
    ):
        raise CorruptStore("review decision does not name the policy that produced it")
    return decision


# ---------------------------------------------------------------------------
# rejection fingerprint
# ---------------------------------------------------------------------------


def _claim_text_digest(change: Mapping[str, Any]) -> str | None:
    body = change.get("body")
    if not isinstance(body, Mapping):
        return None
    claim = body.get("claim")
    if not isinstance(claim, str):
        return None
    return sha256_hex(claim)


def change_fingerprint(validated: ValidatedChange) -> str:
    """A stable digest of *what* a bundle changes, for rejection bookkeeping.

    Only the identity-bearing fields participate: adding a summary or reordering
    an equivalent bundle must not change the fingerprint, while a different target,
    claim type, scope or claim text must.
    """

    items: list[dict[str, Any]] = []
    for change in validated.changes:
        body = change.get("body") if isinstance(change.get("body"), Mapping) else {}
        target = change.get("target") if isinstance(change.get("target"), Mapping) else {}
        scope = change.get("scope") if isinstance(change.get("scope"), Mapping) else {}
        items.append(
            {
                "op": change["op"],
                "target_record_id": target.get("record_id"),
                "claim_type": body.get("claim_type"),
                "scope": dict(scope),
                "claim_digest": _claim_text_digest(change),
            }
        )
    items.sort(key=lambda item: canonical_json_text(item))
    return sha256_hex(canonical_json_text(items))


__all__ = [
    "BASE_POLICY",
    "BASE_POLICY_ID",
    "BASE_POLICY_VERSION",
    "BaseReviewPolicy",
    "ChangeFact",
    "EVIDENCE_EVENT_TYPES",
    "EVIDENCE_LINKAGES",
    "IMPACT_LEVELS",
    "IMPACT_POLICY_ID",
    "IMPACT_POLICY_VERSION",
    "ImpactDecision",
    "PolicyConfigurationError",
    "PolicyDecision",
    "PolicyStateView",
    "RECOMMENDATIONS",
    "ReviewPolicy",
    "RiskPolicyConfig",
    "SUPPORT_BASES",
    "TargetFact",
    "USER_OWNED_CLAIM_TYPES",
    "change_fingerprint",
    "classify_impact",
    "describe_changes",
    "evaluate_policy",
    "evidence_linkage",
    "predict_status",
    "scope_contained",
]
