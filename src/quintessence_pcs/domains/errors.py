"""PCS2-007 domain errors.

Every class here maps to a code that already exists in ``contracts/v2/error.json``
(J.4).  The PCS2-004 ``foundation.errors`` module stays untouched; these are the
two codes that only the domain layer needs.
"""

from __future__ import annotations

from ..foundation.errors import StorageError


class HistoricalSearchUnsupported(StorageError):
    """Retrieval was asked for a non-current ``state_revision`` (J.3/J.4).

    This card implements exactly one retrieval basis: the current version of
    each record.  It neither replays an old index nor silently substitutes the
    current revision for the one the caller named.
    """

    code = "HISTORICAL_SEARCH_UNSUPPORTED"


class SourceUntrusted(StorageError):
    """An audit reference does not point at a trusted user-entry event.

    The review event must exist in the same namespace, must not be deleted, and
    must have been ingested through the trusted user entry with the frozen
    review event type.  A model-produced or synthetic event cannot stand in for
    a user approval.
    """

    code = "SOURCE_UNTRUSTED"


__all__ = ["HistoricalSearchUnsupported", "SourceUntrusted"]
