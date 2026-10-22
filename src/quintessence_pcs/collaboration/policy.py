"""C06a: the closed set of committable proposals and their authority shape.

This module holds no protocol state and performs no I/O.  It only names the
three Belief proposal operations C06a may execute and the pair of mutually
exclusive authorities a trusted host may present.

What the ``isinstance`` checks in :class:`CommitAuthority` prove, and what they
do not:

* they prove the *shape and identity binding* of the object a trusted caller
  constructed: either a real :class:`~quintessence_pcs.store.collaboration.Approval`
  instance or a grant id, never both;
* they are **not authentication**.  There is no ACL, token or identity system in
  this card.

The real trust boundary is architectural: only trusted host/Facade code can
construct an ``Approval`` and call
:class:`~quintessence_pcs.collaboration.service.CollaborationService`.  Model
output, candidate proposals and request JSON are data; they are never an
authority source, and nothing in this module can turn them into one.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..foundation.errors import InvalidInput
from ..store.collaboration import Approval

#: The only proposal operations C06a may turn into a cognitive command.
PROPOSAL_OPERATIONS = frozenset(
    {"propose_belief", "revise_belief", "retract_belief"}
)


@dataclass(frozen=True)
class CommitAuthority:
    """Exactly one manual approval or one commit grant id.

    The two branches are mutually exclusive by construction, so a caller can
    never present both in the hope that one of them wins.
    """

    approval: Approval | None = None
    grant_id: str | None = None

    def __post_init__(self) -> None:
        if (self.approval is None) == (self.grant_id is None):
            raise InvalidInput(
                "exactly one of approval or grant_id is required"
            )
        if self.approval is not None and not isinstance(self.approval, Approval):
            raise InvalidInput("approval must be an Approval instance")


__all__ = ["PROPOSAL_OPERATIONS", "CommitAuthority"]
