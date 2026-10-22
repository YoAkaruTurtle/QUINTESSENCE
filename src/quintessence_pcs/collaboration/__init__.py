"""C06a: trusted candidate commit.

The package has exactly one execution point,
:meth:`CollaborationService.commit_candidate`.  It binds the candidate state,
the commit authority, the Context watermarks, the deletion barrier, the
``BeliefService`` Tx B command, the receipt and the candidate's ``committed``
write into one atomic transaction.
"""

from __future__ import annotations

from .policy import PROPOSAL_OPERATIONS, CommitAuthority
from .service import CandidateCommitOutcome, CollaborationService

__all__ = [
    "CandidateCommitOutcome",
    "CollaborationService",
    "CommitAuthority",
    "PROPOSAL_OPERATIONS",
]
