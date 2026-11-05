"""J08 WP2: bounded Memory delivery inside a maintenance slice.

The worker takes a *bounded page* of source event ids chosen by the caller and
walks the existing Experience pipeline: re-verify the source, build the
single-event Episode, then settle the outbox row that the bridge already
created.  It never invents a second Memory authority and never truncates a page
that is too large — an oversized page is refused, because a truncated page
looks exactly like a complete one to the caller.

The outcome vocabulary is deliberately honest about the two delivery states:

* ``committed``      — the outbox row is ``verified`` (an authoritative ack
  exists).  Only this counts towards a checkpoint.
* ``deferred``       — the material was applied locally but no external ack has
  arrived yet (``applied``).  Applied is *not* verified and blocks the
  checkpoint.
* ``waiting_dependency`` — the event has no usable projection (no adapter is
  wired), so nothing was delivered.
* ``failed``         — the source vanished or the Episode could not be built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from ...foundation.errors import InvalidInput

#: The largest page one worker may hand to :func:`deliver_page`.
MAX_DELIVERY_PAGE = 20

#: Outcomes that may advance a checkpoint (mirrors the maintenance store).
CONFIRMED_OUTCOMES = frozenset({"committed", "no_change"})


@dataclass(frozen=True)
class DeliveryOutcome:
    """What happened to one source event in this page."""

    event_id: str
    outcome: str
    detail: str
    target_kind: str
    target_id: str
    target_version: int = 0
    episode_ref: Mapping[str, Any] | None = None
    delivery_id: str | None = None

    def as_item(self) -> dict[str, Any]:
        """The shape ``run_slice`` records for this outcome."""

        return {
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "target_version": self.target_version,
            "operation_key": f"deliver:{self.event_id}",
            "outcome": self.outcome,
            "detail": self.detail,
        }


def _require_page(event_ids: Sequence[str], limit: int) -> list[str]:
    if isinstance(event_ids, (str, bytes)):
        raise InvalidInput("event_ids must be a sequence of ids")
    if type(limit) is not int or limit < 1 or limit > MAX_DELIVERY_PAGE:
        raise InvalidInput(f"limit must be between 1 and {MAX_DELIVERY_PAGE}")
    ids = list(event_ids)
    if len(ids) > limit:
        raise InvalidInput(
            f"page holds {len(ids)} events but the limit is {limit}; scan in pages"
        )
    return ids


def _delivery_states(store: Any, namespace: str) -> dict[str, str]:
    rows = store.list_deliveries(namespace)
    return {str(row["delivery_id"]): str(row["state"]) for row in rows}


def deliver_page(
    store: Any,
    *,
    namespace: str,
    event_ids: Sequence[str],
    scope: Mapping[str, Any],
    authorization_ref: str,
    episodes: Any | None = None,
    limit: int = MAX_DELIVERY_PAGE,
    lease_seconds: float = 60.0,
) -> tuple[DeliveryOutcome, ...]:
    """Deliver one bounded page of Memory sources, idempotently."""

    ready = getattr(store, "require_ready", None)
    if ready is not None:
        ready("memory_delivery")
    ids = _require_page(event_ids, limit)
    if not ids:
        return ()

    from .episodes import EpisodeService
    from ...foundation.errors import RecordNotFound, StorageError

    service = episodes if episodes is not None else EpisodeService(store)
    states = _delivery_states(store, namespace)
    claimed = {
        str(row["event_id"]): row
        for row in store.claim_delivery(
            namespace, lease_seconds=lease_seconds, limit=len(ids)
        )
    }

    outcomes: list[DeliveryOutcome] = []
    for event_id in ids:
        # AC-21: re-verify the source at the delivery boundary, not at scan time.
        if store.event_is_deleted(namespace, event_id):
            outcomes.append(
                DeliveryOutcome(
                    event_id=event_id,
                    outcome="failed",
                    detail="source_gone",
                    target_kind="event",
                    target_id=event_id,
                )
            )
            continue

        projection = store.projection_for_event(namespace, event_id)
        if projection is None or not store.projection_is_usable(namespace, event_id):
            outcomes.append(
                DeliveryOutcome(
                    event_id=event_id,
                    outcome="waiting_dependency",
                    detail="no_adapter",
                    target_kind="event",
                    target_id=event_id,
                )
            )
            continue

        delivery_id = str(projection["delivery_id"])
        state = states.get(delivery_id)
        if state == "verified":
            outcomes.append(
                DeliveryOutcome(
                    event_id=event_id,
                    outcome="committed",
                    detail="verified",
                    target_kind="memory_delivery",
                    target_id=delivery_id,
                )
            )
            continue
        if state == "obsolete":
            outcomes.append(
                DeliveryOutcome(
                    event_id=event_id,
                    outcome="failed",
                    detail="delivery_obsolete",
                    target_kind="memory_delivery",
                    target_id=delivery_id,
                )
            )
            continue

        try:
            episode = service.build_episode_from_event(
                namespace=namespace,
                event_id=event_id,
                scope=scope,
                authorization_ref=authorization_ref,
            )
        except (RecordNotFound, StorageError) as error:
            outcomes.append(
                DeliveryOutcome(
                    event_id=event_id,
                    outcome="failed",
                    detail=f"episode_refused:{getattr(error, 'code', 'error')}",
                    target_kind="event",
                    target_id=event_id,
                )
            )
            continue

        episode_ref = {
            "record_id": episode.record_id,
            "record_version": episode.record_version,
        }
        row = claimed.get(event_id)
        if row is None:
            # Someone else holds the lease, or the row is not due yet.  Nothing
            # was applied by us, so this stays outstanding.
            outcomes.append(
                DeliveryOutcome(
                    event_id=event_id,
                    outcome="deferred",
                    detail=f"delivery_{state or 'unclaimed'}",
                    target_kind="memory_delivery",
                    target_id=delivery_id,
                    episode_ref=episode_ref,
                    delivery_id=delivery_id,
                )
            )
            continue

        store.finish_delivery(
            delivery_id=delivery_id,
            lease_token=str(row["lease_token"]),
            state="applied",
            receipt={"episode": episode_ref, "ack": "not_received"},
        )
        outcomes.append(
            DeliveryOutcome(
                event_id=event_id,
                outcome="deferred",
                detail="applied_awaiting_external_ack",
                target_kind="memory_delivery",
                target_id=delivery_id,
                episode_ref=episode_ref,
                delivery_id=delivery_id,
            )
        )
    return tuple(outcomes)


def memory_delivery_worker(
    *,
    store: Any,
    namespace: str,
    scope: Mapping[str, Any],
    authorization_ref: str,
    event_ids: Sequence[str],
    episodes: Any | None = None,
    units_per_event: int = 10,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """Build a ``run_slice`` worker over a fixed, already-bounded page.

    The page is fixed by the caller (a trigger decides *what* to look at); the
    worker only walks it within the slice budget and reports what it settled.
    """

    page = list(event_ids)

    def worker(budget: Mapping[str, Any]) -> Mapping[str, Any]:
        capacity = int(budget.get("max_process", len(page)) or 0)
        window = page[: max(capacity, 0)]
        outcomes = deliver_page(
            store,
            namespace=namespace,
            event_ids=window,
            scope=scope,
            authorization_ref=authorization_ref,
            episodes=episodes,
            limit=MAX_DELIVERY_PAGE,
        )
        items = [outcome.as_item() for outcome in outcomes]
        return {
            "scanned": len(window),
            "processed": len(items),
            "units": len(window) * units_per_event,
            "items": items,
            "complete": len(window) == len(page),
            "stop_reason": "delivered" if len(window) == len(page) else "page_limit",
            "remaining": {
                "events_left": len(page) - len(window),
                "awaiting_ack": sum(
                    1 for outcome in outcomes if outcome.detail.startswith("applied")
                ),
            },
        }

    return worker


__all__ = [
    "CONFIRMED_OUTCOMES",
    "DeliveryOutcome",
    "MAX_DELIVERY_PAGE",
    "deliver_page",
    "memory_delivery_worker",
]
