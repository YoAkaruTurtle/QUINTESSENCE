"""J08 WP2 / WP3 / AC-21: bounded delivery, judgeable triggers, boundary recheck.

These are the parts that decide *whether* work happens, so each test pins a way
the code could quietly lie:

* a page too large is refused, not truncated into looking complete;
* "applied locally" is never reported as "verified by an ack";
* prose in ``review_condition`` is reported as unjudgeable instead of guessed;
* an authorization revoked or a version moved after the work was chosen stops it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _path in (_ROOT / "src",):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.domains.cognition.maintenance_guard import (  # noqa: E402
    MaintenanceStale,
    require_live_targets,
)
from quintessence_pcs.domains.cognition.maintenance_triggers import (  # noqa: E402
    UNJUDGEABLE_REASON,
    ReviewOpportunity,
    evaluate_triggers,
)
from quintessence_pcs.domains.memory.maintenance_delivery import (  # noqa: E402
    MAX_DELIVERY_PAGE,
    deliver_page,
)
from quintessence_pcs.foundation.errors import InvalidInput  # noqa: E402

NS = "example.test.j08wp"
NOW = "2026-10-27T12:00:00Z"


class _Episode:
    def __init__(self, record_id: str, record_version: int = 1) -> None:
        self.record_id = record_id
        self.record_version = record_version


class _Episodes:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def build_episode_from_event(self, *, namespace, event_id, scope, authorization_ref):
        self.calls.append(event_id)
        if self.fail:
            raise InvalidInput("episode refused")
        return _Episode(f"ep-{event_id}")


class _DeliveryStore:
    """The small slice of the store that ``deliver_page`` actually uses."""

    def __init__(self, *, deleted=(), projections=None, states=None, claimable=()):
        self.deleted = set(deleted)
        self.projections = dict(projections or {})
        self.states = dict(states or {})
        self.claimable = list(claimable)
        self.finished: list[dict] = []

    def event_is_deleted(self, namespace, event_id):
        return event_id in self.deleted

    def projection_for_event(self, namespace, event_id):
        return self.projections.get(event_id)

    def projection_is_usable(self, namespace, event_id):
        return event_id in self.projections

    def list_deliveries(self, namespace):
        return [
            {"delivery_id": key, "state": value} for key, value in self.states.items()
        ]

    def claim_delivery(self, namespace, *, lease_seconds, limit=1, now=None):
        rows = self.claimable[:limit]
        self.claimable = self.claimable[limit:]
        return rows

    def finish_delivery(self, **kwargs):
        self.finished.append(kwargs)
        self.states[kwargs["delivery_id"]] = kwargs["state"]
        return True


class DeliveryPageTests(unittest.TestCase):
    def test_oversized_page_is_refused_instead_of_truncated(self):
        store = _DeliveryStore()
        with self.assertRaises(InvalidInput):
            deliver_page(
                store,
                namespace=NS,
                event_ids=[f"ev-{i}" for i in range(MAX_DELIVERY_PAGE + 1)],
                scope={},
                authorization_ref="auth:1",
            )

    def test_deleted_source_fails_the_item(self):
        store = _DeliveryStore(deleted={"ev-1"})
        outcomes = deliver_page(
            store, namespace=NS, event_ids=["ev-1"], scope={}, authorization_ref="auth:1"
        )
        self.assertEqual(outcomes[0].outcome, "failed")
        self.assertEqual(outcomes[0].detail, "source_gone")

    def test_missing_projection_waits_for_a_dependency(self):
        store = _DeliveryStore()
        outcomes = deliver_page(
            store, namespace=NS, event_ids=["ev-2"], scope={}, authorization_ref="auth:1"
        )
        self.assertEqual(outcomes[0].outcome, "waiting_dependency")
        self.assertEqual(outcomes[0].detail, "no_adapter")

    def test_verified_delivery_counts_as_committed_without_rebuilding(self):
        store = _DeliveryStore(
            projections={"ev-3": {"delivery_id": "d-3"}}, states={"d-3": "verified"}
        )
        episodes = _Episodes()
        outcomes = deliver_page(
            store,
            namespace=NS,
            event_ids=["ev-3"],
            scope={},
            authorization_ref="auth:1",
            episodes=episodes,
        )
        self.assertEqual(outcomes[0].outcome, "committed")
        self.assertEqual(outcomes[0].detail, "verified")
        self.assertEqual(episodes.calls, [])

    def test_applied_locally_is_not_verified(self):
        store = _DeliveryStore(
            projections={"ev-4": {"delivery_id": "d-4"}},
            states={"d-4": "pending"},
            claimable=[{"event_id": "ev-4", "lease_token": "tok-4", "delivery_id": "d-4"}],
        )
        outcomes = deliver_page(
            store,
            namespace=NS,
            event_ids=["ev-4"],
            scope={},
            authorization_ref="auth:1",
            episodes=_Episodes(),
        )
        self.assertEqual(outcomes[0].outcome, "deferred")
        self.assertEqual(outcomes[0].detail, "applied_awaiting_external_ack")
        self.assertEqual(store.finished[0]["state"], "applied")
        self.assertEqual(store.finished[0]["receipt"]["ack"], "not_received")

    def test_lost_ack_is_not_reported_as_success_on_rerun(self):
        """A crash after ``applied`` leaves the row out of ``verified``.

        Re-running must not claim the ack arrived: the second pass sees
        ``applied`` and stays deferred instead of finishing the row again.
        """

        store = _DeliveryStore(
            projections={"ev-5": {"delivery_id": "d-5"}},
            states={"d-5": "pending"},
            claimable=[{"event_id": "ev-5", "lease_token": "tok-5", "delivery_id": "d-5"}],
        )
        episodes = _Episodes()
        deliver_page(
            store,
            namespace=NS,
            event_ids=["ev-5"],
            scope={},
            authorization_ref="auth:1",
            episodes=episodes,
        )
        self.assertEqual(store.states["d-5"], "applied")

        # The process died before the ack was recorded; nothing claims it again.
        again = deliver_page(
            store,
            namespace=NS,
            event_ids=["ev-5"],
            scope={},
            authorization_ref="auth:1",
            episodes=episodes,
        )
        self.assertEqual(again[0].outcome, "deferred")
        self.assertEqual(again[0].detail, "delivery_applied")
        self.assertEqual(len(store.finished), 1)
        self.assertEqual(store.states["d-5"], "applied")

    def test_holding_another_lease_is_deferred(self):
        store = _DeliveryStore(
            projections={"ev-6": {"delivery_id": "d-6"}}, states={"d-6": "pending"}
        )
        outcomes = deliver_page(
            store,
            namespace=NS,
            event_ids=["ev-6"],
            scope={},
            authorization_ref="auth:1",
            episodes=_Episodes(),
        )
        self.assertEqual(outcomes[0].outcome, "deferred")
        self.assertEqual(outcomes[0].detail, "delivery_pending")
        self.assertEqual(store.finished, [])


class _View:
    def __init__(self, record_id, *, version=1, review_after=None, evaluated=None,
                 paused=False, condition=None, source_revision=None):
        self.ref = {"record_id": record_id, "record_version": version}
        self.review_after = review_after
        self.last_evaluated_at = evaluated
        self.paused = paused
        self.review_condition = condition
        self.source_revision = source_revision


class TriggerTests(unittest.TestCase):
    def test_paused_subject_is_never_woken(self):
        view = _View("q-1", review_after="2026-10-01T00:00:00Z", paused=True)
        self.assertEqual(evaluate_triggers(views=[view], now=NOW), ())

    def test_due_review_is_a_trigger(self):
        view = _View("q-2", review_after="2026-10-01T00:00:00Z")
        found = evaluate_triggers(views=[view], now=NOW)
        self.assertEqual([c.reason for c in found], ["review_due"])
        self.assertTrue(found[0].judgeable)

    def test_already_evaluated_after_due_is_not_retriggered(self):
        view = _View(
            "q-3",
            review_after="2026-10-01T00:00:00Z",
            evaluated="2026-10-02T00:00:00Z",
        )
        self.assertEqual(evaluate_triggers(views=[view], now=NOW), ())

    def test_manual_review_is_explicit(self):
        view = _View("q-4")
        found = evaluate_triggers(
            views=[view],
            now=NOW,
            manual_refs=[{"record_id": "q-4", "record_version": 1}],
        )
        self.assertEqual([c.reason for c in found], ["manual_review"])

    def test_prose_condition_is_not_guessed(self):
        view = _View("q-5", condition="when the user seems ready to decide")
        self.assertEqual(evaluate_triggers(views=[view], now=NOW), ())
        found = evaluate_triggers(views=[view], now=NOW, include_unjudgeable=True)
        self.assertEqual([c.reason for c in found], [UNJUDGEABLE_REASON])
        self.assertFalse(found[0].judgeable)

    def test_source_revision_change_is_a_trigger(self):
        view = _View("b-1", source_revision=4)
        found = evaluate_triggers(
            views=[view], now=NOW, source_revisions={"b-1": 3}
        )
        self.assertEqual([c.reason for c in found], ["source_revision"])

    def test_oversized_page_is_refused(self):
        views = [_View(f"q-{i}") for i in range(51)]
        with self.assertRaises(InvalidInput):
            evaluate_triggers(views=views, now=NOW)


class OpportunityTests(unittest.TestCase):
    def test_one_opportunity_per_trigger(self):
        budget = ReviewOpportunity()
        self.assertTrue(budget.consume("k1"))
        self.assertFalse(budget.consume("k1"))
        self.assertTrue(budget.consume("k2"))

    def test_retry_does_not_refill(self):
        budget = ReviewOpportunity()
        budget.consume("k1")
        for _ in range(5):
            self.assertFalse(budget.consume("k1"))

    def test_version_is_visible(self):
        self.assertIn("j08", ReviewOpportunity().version)


class _GuardStore:
    def __init__(self, *, deleted=(), headers=None, pointers=None, deliveries=None):
        self.deleted = set(deleted)
        self.headers = dict(headers or {})
        self.pointers = dict(pointers or {})
        self.deliveries = list(deliveries or [])

    def event_is_deleted(self, namespace, event_id):
        return event_id in self.deleted

    def event_header(self, namespace, event_id):
        return self.headers.get(event_id)

    def record_pointer(self, namespace, record_id):
        class _Pointer:
            def __init__(self, version):
                self.current_version = version

        version = self.pointers.get(record_id)
        return None if version is None else _Pointer(version)

    def list_deliveries(self, namespace):
        return self.deliveries


class GuardTests(unittest.TestCase):
    def test_revoked_authorization_stops_the_work(self):
        with self.assertRaises(MaintenanceStale) as caught:
            require_live_targets(
                _GuardStore(), namespace=NS, targets=[], authorization_valid=False
            )
        self.assertEqual(caught.exception.reason, "authorization_revoked")

    def test_deleted_source_stops_the_work(self):
        store = _GuardStore(deleted={"ev-1"})
        with self.assertRaises(MaintenanceStale) as caught:
            require_live_targets(
                store, namespace=NS, targets=[{"kind": "event", "id": "ev-1"}]
            )
        self.assertEqual(caught.exception.reason, "source_deleted")

    def test_moved_version_stops_the_work(self):
        store = _GuardStore(pointers={"b-1": 3})
        with self.assertRaises(MaintenanceStale) as caught:
            require_live_targets(
                store,
                namespace=NS,
                targets=[{"kind": "record", "id": "b-1", "version": 2}],
            )
        self.assertEqual(caught.exception.reason, "target_version_moved")

    def test_missing_target_stops_the_work(self):
        with self.assertRaises(MaintenanceStale) as caught:
            require_live_targets(
                _GuardStore(), namespace=NS, targets=[{"kind": "record", "id": "gone"}]
            )
        self.assertEqual(caught.exception.reason, "target_gone")

    def test_unbuilt_event_is_gone_not_deleted(self):
        with self.assertRaises(MaintenanceStale) as caught:
            require_live_targets(
                _GuardStore(), namespace=NS, targets=[{"kind": "event", "id": "ev-x"}]
            )
        self.assertEqual(caught.exception.reason, "source_gone")

    def test_obsolete_delivery_stops_the_work(self):
        store = _GuardStore(deliveries=[{"delivery_id": "d-1", "state": "obsolete"}])
        with self.assertRaises(MaintenanceStale) as caught:
            require_live_targets(
                store, namespace=NS, targets=[{"kind": "memory_delivery", "id": "d-1"}]
            )
        self.assertEqual(caught.exception.reason, "delivery_obsolete")

    def test_live_targets_pass(self):
        store = _GuardStore(
            headers={"ev-2": object()},
            pointers={"b-2": 2},
            deliveries=[{"delivery_id": "d-2", "state": "applied"}],
        )
        require_live_targets(
            store,
            namespace=NS,
            targets=[
                {"kind": "event", "id": "ev-2"},
                {"kind": "record", "id": "b-2", "version": 2},
                {"kind": "memory_delivery", "id": "d-2"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
