"""PCS2-008 cognition domain errors.

Only codes that already exist in ``contracts/v2/error.json`` are used.  The
store-level conditions (``NEEDS_CONTEXT``, ``CONTEXT_TOO_LARGE``,
``ID_CONFLICT`` for a work winner) live in :mod:`quintessence_pcs.store.errors`;
these three belong to the belief rules implemented in this package.
"""

from __future__ import annotations

from ...foundation.errors import StorageError


class EvidenceMissing(StorageError):
    """The supplied evidence cannot satisfy the claim type's support rule.

    Also used when an evidence reference does not resolve to a stored event or
    an exact Episode version, and when a support rule needs a declared scope or
    validity deadline that the caller did not supply.  The distinction is
    deliberate: a structurally valid reference that merely does not exist is
    an evidence problem, not a schema error.
    """

    code = "EVIDENCE_MISSING"


class UnsupportedOperation(StorageError):
    """A model output asked for an operation PCS2-008 does not implement."""

    code = "UNSUPPORTED_OPERATION"


class InvalidTransition(StorageError):
    """A belief state transition or rewrite the card forbids.

    Examples: changing the frozen ``claim`` text, changing ``claim_type``,
    revising a ``retracted``/``contradicted`` belief (no reopen in 008), or
    calling ``complete_work`` twice.
    """

    code = "INVALID_TRANSITION"


__all__ = ["EvidenceMissing", "InvalidTransition", "UnsupportedOperation"]
