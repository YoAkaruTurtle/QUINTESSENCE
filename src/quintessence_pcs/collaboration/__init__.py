"""C06a/C06b: trusted candidate commit and review resolution.

The package has exactly one commit execution point,
:meth:`CollaborationService.commit_candidate`, and exactly one approve/reject
entry point, :meth:`CollaborationService.resolve_candidate`.  C06b adds the
read-only review gate and the generation-landing check in front of them.
"""

from __future__ import annotations

from .policy import PROPOSAL_OPERATIONS, CommitAuthority
from .service import (
    DECISION_APPROVE,
    DECISION_REJECT,
    RESOLVE_DECISIONS,
    CandidateCommitOutcome,
    CollaborationService,
    Resolution,
)

__all__ = [
    "CandidateCommitOutcome",
    "CollaborationService",
    "CommitAuthority",
    "DECISION_APPROVE",
    "DECISION_REJECT",
    "PROPOSAL_OPERATIONS",
    "RESOLVE_DECISIONS",
    "Resolution",
]
