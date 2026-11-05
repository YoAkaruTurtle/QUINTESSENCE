"""C06a: the closed set of committable proposals and their authority shape.

This module holds no protocol state and performs no I/O.  It only names the
proposal operations a candidate may carry and the mutually exclusive authorities
a trusted host may present.

What the ``isinstance`` checks in :class:`CommitAuthority` prove, and what they
do not:

* they prove the *shape and identity binding* of the object a trusted caller
  constructed: exactly one of a real
  :class:`~quintessence_pcs.store.collaboration.Approval` instance, a grant id, an
  auto-authorization id or a ``DirectiveBinding``;
* they are **not authentication**.  There is no ACL, token or identity system in
  this card.

The real trust boundary is architectural: only trusted host/Facade code can
construct one of those objects and call
:class:`~quintessence_pcs.collaboration.service.CollaborationService`.  Model
output, candidate proposals and request JSON are data; they are never an
authority source, and nothing in this module can turn them into one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..foundation.errors import InvalidInput
from ..store.collaboration import Approval

#: The proposal operations a candidate may carry.  ``cognition.change`` is the
#: J06 bundle; the three Belief operations remain the C06a single-object path.
PROPOSAL_OPERATIONS = frozenset(
    {
        "propose_belief",
        "revise_belief",
        "retract_belief",
        "cognition.change",
    }
)


@dataclass(frozen=True)
class CommitAuthority:
    """Exactly one of: a manual approval, a grant id, an auto authorization or
    a directive binding.

    The branches are mutually exclusive by construction, so a caller can never
    present two in the hope that one of them wins.  ``directive`` is typed
    loosely here to keep this module free of an import cycle with
    :mod:`quintessence_pcs.collaboration.change_service`; the change service
    rejects anything that is not a ``DirectiveBinding``.
    """

    approval: Approval | None = None
    grant_id: str | None = None
    auto_authorization_id: str | None = None
    directive: Any = None

    def __post_init__(self) -> None:
        chosen = [
            item
            for item in (
                self.approval,
                self.grant_id,
                self.auto_authorization_id,
                self.directive,
            )
            if item is not None
        ]
        if len(chosen) != 1:
            raise InvalidInput(
                "exactly one of approval, grant_id, auto_authorization_id or "
                "directive is required"
            )
        if self.approval is not None and not isinstance(self.approval, Approval):
            raise InvalidInput("approval must be an Approval instance")


__all__ = ["PROPOSAL_OPERATIONS", "CommitAuthority"]
