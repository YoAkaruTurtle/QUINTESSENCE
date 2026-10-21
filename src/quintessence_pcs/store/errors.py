"""Store-layer errors introduced by PCS2-008.

The error-code vocabulary itself is frozen in ``contracts/v2/error.json``
(PCS2-002); this module only adds the classes that map new state-layer
conditions onto codes that already exist.  ``foundation/errors.py`` is left
untouched so the PCS2-004/005/006 mapping stays exactly as frozen.

Why each one exists here rather than in the domain layer:

* :class:`NeedsContext` is raised while *reading* a context snapshot row
  (006-era snapshots are incomplete), so the store must be able to name it;
* :class:`ContextTooLarge` is raised by the context writer before any row is
  inserted;
* :class:`WorkAlreadyCompleted` is the primary-key backstop of
  ``work_progress`` and is raised from inside ``TxScope.complete_work``.
"""

from __future__ import annotations

from ..foundation.errors import StorageError


class NeedsContext(StorageError):
    """The stored snapshot cannot serve as a PCS2-008 Context.

    A PCS2-006 snapshot only carries the three watermarks.  PCS2-008 additionally
    requires the exact J.5 material, the rendered input payload and its hash.
    Treating the missing fields as "empty read_set" or "complete input" would
    silently fabricate a basis, so the read fails closed.
    """

    code = "NEEDS_CONTEXT"


class ContextTooLarge(StorageError):
    """Required context material does not fit the byte budget.

    Optional material (episode summaries) is trimmed first and recorded in
    ``omissions``.  Required material (trigger, profile, pending sources,
    current beliefs) is never silently dropped: exceeding the budget is a
    failure, matching J.5 "necessary constraints must not be deleted".
    """

    code = "CONTEXT_TOO_LARGE"


class WorkAlreadyCompleted(StorageError):
    """Another transaction already wrote the completion row for this work item.

    The ``work_progress`` primary key decides the race.  The caller's whole
    transaction must roll back; the stored work is not reprocessed.
    """

    code = "ID_CONFLICT"


class InvalidWorkCompletion(StorageError):
    """``TxScope.complete_work`` was used in a state where it cannot be true.

    Examples: calling it twice, appending a record version after it, claiming
    ``committed`` with no appended version, or claiming ``checked_no_belief``
    while a version was appended.
    """

    code = "INVALID_TRANSITION"


__all__ = [
    "ContextTooLarge",
    "InvalidWorkCompletion",
    "NeedsContext",
    "WorkAlreadyCompleted",
]
