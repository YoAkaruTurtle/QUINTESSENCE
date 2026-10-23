"""C06a/C06b/C06c/C07/C11: commit, review resolution, recovery and Facades.

The package has exactly one commit execution point
(:meth:`CollaborationService.commit_candidate`), one approve/reject entry point
(:meth:`CollaborationService.resolve_candidate`) and one recovery entry point
(:meth:`CollaborationService.get_result`).  C07 adds the review-only public
Facade (:class:`ReviewFacade`) on top of those trusted APIs.  C11 adds the
bounded consult Facade (:class:`ConsultFacade`): formal Context -> at most two
logical generations -> at most one deterministic query -> new formal Context ->
final output.  Neither Facade adds a second commit or resolution path.
"""

from __future__ import annotations

from ..store.collaboration import CandidateState, InteractionRow
from .consult import (
    CONSULT_STATES,
    MAX_GENERATIONS,
    MAX_QUERIES,
    TERMINAL_CONSULT_STATES,
    CognitionQuery,
    ConsultFacade,
    ConsultHostAccess,
    ConsultRequest,
    ConsultResult,
)
from .facade import (
    FACADE_PERMISSIONS,
    REVIEW_STATES,
    HostAccess,
    ReviewCandidateDetail,
    ReviewCandidateRef,
    ReviewCandidateSummary,
    ReviewCurrentView,
    ReviewDiffField,
    ReviewFacade,
    ReviewProposalView,
    ReviewSourceView,
    ReviewView,
    ReviewWorkItem,
    open_store,
)
from .policy import PROPOSAL_OPERATIONS, CommitAuthority
from .service import (
    DECISION_APPROVE,
    DECISION_REJECT,
    RESOLVE_DECISIONS,
    CandidateCommitOutcome,
    CollaborationService,
    InteractionResult,
    Resolution,
)

__all__ = [
    "CONSULT_STATES",
    "CandidateCommitOutcome",
    "CandidateState",
    "CognitionQuery",
    "CollaborationService",
    "CommitAuthority",
    "ConsultFacade",
    "ConsultHostAccess",
    "ConsultRequest",
    "ConsultResult",
    "DECISION_APPROVE",
    "DECISION_REJECT",
    "FACADE_PERMISSIONS",
    "HostAccess",
    "InteractionResult",
    "InteractionRow",
    "MAX_GENERATIONS",
    "MAX_QUERIES",
    "PROPOSAL_OPERATIONS",
    "RESOLVE_DECISIONS",
    "REVIEW_STATES",
    "Resolution",
    "ReviewCandidateDetail",
    "ReviewCandidateRef",
    "ReviewCandidateSummary",
    "ReviewCurrentView",
    "ReviewDiffField",
    "ReviewFacade",
    "ReviewProposalView",
    "ReviewSourceView",
    "ReviewView",
    "ReviewWorkItem",
    "TERMINAL_CONSULT_STATES",
    "open_store",
]
