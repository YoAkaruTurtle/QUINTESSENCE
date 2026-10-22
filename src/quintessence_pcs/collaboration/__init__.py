"""C06a/C06b/C06c/C07: commit, review resolution, recovery and the Facade.

The package has exactly one commit execution point
(:meth:`CollaborationService.commit_candidate`), one approve/reject entry point
(:meth:`CollaborationService.resolve_candidate`) and one recovery entry point
(:meth:`CollaborationService.get_result`).  C07 adds the review-only public
Facade (:class:`ReviewFacade`) on top of those trusted APIs; it adds no second
commit or resolution path.
"""

from __future__ import annotations

from ..store.collaboration import CandidateState, InteractionRow
from .facade import (
    FACADE_PERMISSIONS,
    REVIEW_STATES,
    HostAccess,
    ReviewFacade,
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
    "CandidateCommitOutcome",
    "CandidateState",
    "CollaborationService",
    "CommitAuthority",
    "DECISION_APPROVE",
    "DECISION_REJECT",
    "FACADE_PERMISSIONS",
    "HostAccess",
    "InteractionResult",
    "InteractionRow",
    "PROPOSAL_OPERATIONS",
    "RESOLVE_DECISIONS",
    "REVIEW_STATES",
    "Resolution",
    "ReviewFacade",
    "ReviewView",
    "ReviewWorkItem",
    "open_store",
]
