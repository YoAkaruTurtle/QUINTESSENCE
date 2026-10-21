"""RFC 3339 UTC timestamps for the store.

Section 5.3: the database stores UTC / RFC 3339 instants.  The contract
pattern (``contracts/v2/_common.json``) accepts 20 to 27 characters, so the
microsecond form is the most precise value the contract permits.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now_rfc3339() -> str:
    """Return the current instant as ``YYYY-MM-DDTHH:MM:SS.ffffffZ``."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["utc_now_rfc3339"]
