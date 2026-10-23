"""PCS2-008 cognition domain: Belief revision, SelfView, Context-aware rules.

Layout follows the cognition guide's recommendation ``domains/cognition/``:

* ``beliefs`` — the ten-field Belief body, support rules, evidence
  independence, transitions and the two Tx B entry points;
* ``views`` — the minimal SelfView projection and its freshness verdict;
* ``context_guard`` — re-validation of a stored Context inside Tx B;
* ``query`` — C10 deterministic, read-only Belief/Episode queries;
* ``errors`` — the domain error classes for codes already in the contract.

Question/Focus, cross-episode consolidation, automatic review and reopen are
later cards and are deliberately absent.
"""

from __future__ import annotations

from .beliefs import (
    BELIEF_KIND,
    BELIEF_SCHEMA_VERSION,
    CLAIM_TYPES,
    RELATIONS,
    STRENGTHS,
    BeliefService,
    BeliefSubmission,
    belief_evidence_is_live,
    parse_belief_body,
    validate_belief_body,
)
from .context_guard import READ_USAGES, require_live_context
from .errors import EvidenceMissing, InvalidTransition, UnsupportedOperation
from .query import (
    ALL_PRIVACY,
    BeliefCurrentQuery,
    BeliefExactQuery,
    BeliefItem,
    CognitionQueryService,
    EpisodeItem,
    EpisodeQuery,
    QueryAccess,
    QueryOmission,
    QueryResult,
    QUERY_POLICY_VERSION,
    QUERY_PURPOSES,
)
from .views import (
    SELF_VIEW_FRESH,
    SELF_VIEW_KIND,
    SELF_VIEW_STALE,
    SELF_VIEW_UNAVAILABLE,
    SelfViewService,
    SelfViewState,
    parse_self_view_body,
    self_view_record_id,
)

__all__ = [
    "ALL_PRIVACY",
    "BELIEF_KIND",
    "BELIEF_SCHEMA_VERSION",
    "BeliefCurrentQuery",
    "BeliefExactQuery",
    "BeliefItem",
    "BeliefService",
    "BeliefSubmission",
    "CLAIM_TYPES",
    "CognitionQueryService",
    "EpisodeItem",
    "EpisodeQuery",
    "EvidenceMissing",
    "InvalidTransition",
    "QUERY_POLICY_VERSION",
    "QUERY_PURPOSES",
    "QueryAccess",
    "QueryOmission",
    "QueryResult",
    "READ_USAGES",
    "RELATIONS",
    "SELF_VIEW_FRESH",
    "SELF_VIEW_KIND",
    "SELF_VIEW_STALE",
    "SELF_VIEW_UNAVAILABLE",
    "STRENGTHS",
    "SelfViewService",
    "SelfViewState",
    "UnsupportedOperation",
    "BeliefService",
    "BeliefSubmission",
    "belief_evidence_is_live",
    "parse_belief_body",
    "parse_self_view_body",
    "require_live_context",
    "self_view_record_id",
    "validate_belief_body",
]
