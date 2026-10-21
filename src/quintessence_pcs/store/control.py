"""PCS2-006 composition: Tx B commands plus the deletion barrier.

``ControlStore`` is the store used by this card's control plane.  It inherits
the PCS2-004 version store and the PCS2-005 event store and adds:

* A group (``TxBMixin``): persisted context snapshots, the three-watermark
  check, idempotent commands and restricted ``TxScope`` writes;
* B group (``DeletionMixin``): the two-phase deletion barrier, the read
  barrier and resumable payload cleanup.

The names are re-exported from ``quintessence_pcs.store``.
"""

from __future__ import annotations

from .deletion import DeletionMixin
from .events import EventStore
from .tx_b import TxBMixin


class ControlStore(TxBMixin, DeletionMixin, EventStore):
    """Tx B and deletion control plane on top of the event store."""


__all__ = ["ControlStore"]
