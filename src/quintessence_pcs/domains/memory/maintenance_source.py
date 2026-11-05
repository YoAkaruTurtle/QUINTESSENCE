"""J08 WP2: bounded, read-only source material for maintenance.

Maintenance never invents Memory content.  It reads the events a bounded scan
page already selected, re-verifies that each one is still a live source, and
hands the caller a digest it can cite.  Nothing here writes to Memory or
Cognition: a ``SourceMaterial`` is derived material, not a Belief, and a source
that vanished between the scan and the delivery is reported as ``source_gone``
rather than silently dropped.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ...foundation.errors import EventNotFound, InvalidInput, RecordNotFound

#: The largest page one maintenance slice may verify at once.
MAX_MATERIAL_PAGE = 200


@dataclass(frozen=True)
class SourceMaterial:
    """One verified (or rejected) source event, ready to be cited.

    RG08 R-E extends the original J08 shape with the *authorized* source body and
    the exact collaboration/event refs plus the R-C1 time fields.  The original
    fields stay first so every J08 caller still constructs the same object; the
    new fields default to "not loaded" and are filled only by the maintenance
    material builder.
    """

    event_id: str
    source_kind: str | None
    event_type: str | None
    observed_at: str | None
    payload_digest: str | None
    usable: bool
    reason: str | None = None
    source_ref: dict[str, Any] | None = None
    external_ref: str | None = None
    external_revision: int | None = None
    content: str | None = None
    content_truncated: bool = False
    subject: str = "unknown"
    delivery_state: str = ""
    from_prior_turn: bool = False
    message_time: str | None = None
    message_time_basis: str = "unknown"
    pcs_event_type: str = "unknown"
    event_time: str | None = None
    time_precision: str = "unknown"
    time_basis: str = "unknown"
    contains_question: bool = False
    contains_quotation: bool = False


def _payload_digest(payload: Any) -> str:
    """A stable digest over the payload, so a revision cannot pass as the same."""

    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_source_material(
    store: Any,
    *,
    namespace: str,
    event_ids: Sequence[str],
    limit: int = MAX_MATERIAL_PAGE,
) -> tuple[SourceMaterial, ...]:
    """Re-verify a bounded page of events before any of them is cited.

    ``event_ids`` comes from the caller's bounded scan; this function refuses a
    page larger than ``limit`` instead of quietly truncating it, because a
    truncated page would look like a complete one to the caller.
    """

    if isinstance(event_ids, (str, bytes)):
        raise InvalidInput("event_ids must be a sequence of ids")
    if type(limit) is not int or limit < 1 or limit > MAX_MATERIAL_PAGE:
        raise InvalidInput(f"limit must be between 1 and {MAX_MATERIAL_PAGE}")
    ids = list(event_ids)
    if len(ids) > limit:
        raise InvalidInput(
            f"page holds {len(ids)} events but the limit is {limit}; scan in pages"
        )

    material: list[SourceMaterial] = []
    for event_id in ids:
        try:
            entry = store.get_event(namespace, event_id)
        except (EventNotFound, RecordNotFound):
            material.append(
                SourceMaterial(
                    event_id=event_id,
                    source_kind=None,
                    event_type=None,
                    observed_at=None,
                    payload_digest=None,
                    usable=False,
                    reason="source_gone",
                )
            )
            continue
        payload = getattr(entry, "payload", None)
        material.append(
            SourceMaterial(
                event_id=entry.event_id,
                source_kind=getattr(entry, "source_kind", None),
                event_type=getattr(entry, "event_type", None),
                observed_at=getattr(entry, "observed_at", None),
                payload_digest=_payload_digest(payload),
                usable=True,
            )
        )
    return tuple(material)


def usable_material(
    material: Iterable[SourceMaterial],
) -> tuple[SourceMaterial, ...]:
    """Only the entries whose source is still live at re-verification time."""

    return tuple(item for item in material if item.usable)


__all__ = [
    "MAX_MATERIAL_PAGE",
    "SourceMaterial",
    "usable_material",
    "verify_source_material",
]
