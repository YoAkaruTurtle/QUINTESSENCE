"""QUINTESSENCE PCS Python core.

``pcs.v2`` is the target protocol (baseline ``PCS-2026-10-17-r3``).  The
package implements storage, domain services and cognition runtime. Schema
sources are maintained under ``contracts/v2``; SQL resources ship with the
package. This library does not provide a command-line interface.
"""

from __future__ import annotations

PROTOCOL_VERSION = "pcs.v2"

__all__ = ["PROTOCOL_VERSION"]
