"""C06a/C06b/C06c: trusted commit, review resolution and result recovery.

The package has exactly one commit execution point
(:meth:`CollaborationService.commit_candidate`), one approve/reject entry point
(:meth:`CollaborationService.resolve_candidate`) and one recovery entry point
(:meth:`CollaborationService.get_result`).
"""

from __future__ import annotations

from ..store.collaboration import CandidateState, InteractionRow
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
    "InteractionResult",
    "InteractionRow",
    "PROPOSAL_OPERATIONS",
    "RESOLVE_DECISIONS",
    "Resolution",
]
