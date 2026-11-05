"""J08 WP6: the whole loop, driven through the real services.

One synthetic Memory source goes through: bounded verification -> bounded
Cognition proposal -> a real maintenance slice that commits through Tx B ->
the authoritative receipt recorded on the item -> a *new store* reading the
committed tentative belief back after the first connection is gone.

The model port is the only fake: Memory, Cognition, Review, Tx B and the
restart read are the real implementations.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.collaboration.change_service import (  # noqa: E402
    ChangeAccess,
    CognitionChangeService,
)
from quintessence_pcs.collaboration.policy import CommitAuthority  # noqa: E402
from quintessence_pcs.domains.cognition.maintenance_review import (  # noqa: E402
    propose_from_material,
)
from quintessence_pcs.domains.cognition.maintenance_guard import (  # noqa: E402
    require_live_targets,
)
from quintessence_pcs.domains.memory.maintenance_source import (  # noqa: E402
    usable_material,
    verify_source_material,
)
from quintessence_pcs.domains.memory.experience_bridge import (  # noqa: E402
    BridgeConfig,
    ExperienceBridge,
)
from quintessence_pcs.domains.memory.maintenance_delivery import (  # noqa: E402
    deliver_page,
    memory_delivery_worker,
)
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402
from quintessence_pcs.store.governance import (  # noqa: E402
    GovernanceAccess,
    GovernanceStore,
)
from quintessence_pcs.store.maintenance import (  # noqa: E402
    MaintenanceStore,
    SliceLimits,
    run_slice,
)

NOW = "2026-10-27T12:00:00Z"


def access() -> ChangeAccess:
    return ChangeAccess(
        namespace=NS,
        host_instance="synthetic-host",
        operator="operator-a",
        access_ref="access:synthetic",
        permissions=frozenset(
            {"change.propose", "change.review", "change.commit", "change.result"}
        ),
        privacy_classes=frozenset({"private"}),
        purposes=frozenset({"cognition.change"}),
    )


def governance_access() -> GovernanceAccess:
    return GovernanceAccess(
        namespace=NS,
        host_instance="synthetic-host",
        operator="operator-a",
        access_ref="access:governance",
        permissions=frozenset({"governance.admin", "governance.read"}),
    )


def _pass_guard(targets):
    """A guard that always accepts: only for tests of other slice behaviour."""

    return None


class EndToEndTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: NOW
        )
        self.governance = GovernanceStore(self.store)
        self.jobs = MaintenanceStore(self.store, clock=lambda: NOW)

    # ------------------------------------------------------------------ setup

    def _authorization(self) -> str:
        authorization_id = str(uuid.uuid4())
        self.governance.grant_commit_authorization(
            governance_access(),
            authorization_id=authorization_id,
            host_instance="synthetic-host",
            access_ref="access:synthetic",
            operations=("cognition.change",),
            claim_types=("general_hypothesis",),
            scope={"scene": "test.scene"},
            max_impact="E0",
            max_changes_per_bundle=2,
            max_targets_per_bundle=2,
            policy_id="policy:cognition.auto.v2",
            policy_version="1",
            authorization_source="host_config",
            issued_at="2026-10-17T00:00:00Z",
        )
        return authorization_id

    def _job(self):
        return self.jobs.register(
            namespace=NS,
            task_type="memory_organize",
            trigger_key="trigger-e2e-1",
            params={"scope": "test.scene"},
            config_version="j08-v1",
            input_watermark="0",
            target_watermark="1",
        )

    # -------------------------------------------------------------------- e2e

    def test_memory_material_becomes_a_committed_tentative_belief(self) -> None:
        # 1. a legal Memory source
        event = self.ingest(
            event_type="EnvironmentObserved:demo",
            payload={"text": "the synthetic shelf holds one blue box"},
        )
        context = self.build_context()
        authorization_id = self._authorization()
        job = self._job()
        seen: dict = {}

        def build_bundle(material):
            return {
                "summary": "one tentative reading of the synthetic shelf",
                "changes": [
                    {
                        "op": "propose_belief",
                        "scope": {"scene": "test.scene"},
                        "body": self.belief_body(
                            claim="the synthetic shelf holds a blue box",
                            claim_type="general_hypothesis",
                            strength="limited",
                        ),
                    }
                ],
            }

        def worker(budget):
            # AC-21: re-verify the source right before the proposal; the opening
            # check is not a permission that survives the slice.
            require_live_targets(
                self.store,
                namespace=NS,
                targets=[{"kind": "event", "id": event.event_id}],
            )
            # WP2: bounded, re-verified source material
            material = verify_source_material(
                self.store, namespace=NS, event_ids=[event.event_id]
            )
            live = usable_material(material)
            assert len(live) == 1, material

            # WP3: bounded proposal, never a commit
            outcomes = propose_from_material(
                self.changes,
                access(),
                context_id=context.context_id,
                material=live,
                build_bundle=build_bundle,
                request_ids=[str(uuid.uuid4())],
            )
            assert outcomes[0].outcome == "proposed", outcomes
            candidate_id = outcomes[0].candidate_id

            # AC-21: and again immediately before the commit boundary.
            require_live_targets(
                self.store,
                namespace=NS,
                targets=[{"kind": "event", "id": event.event_id}],
            )
            # WP4: the commit goes through the existing governance path
            committed = self.changes.commit(
                access(),
                candidate_id=candidate_id,
                authority=CommitAuthority(auto_authorization_id=authorization_id),
                decision_id=str(uuid.uuid4()),
            )
            seen["candidate_id"] = candidate_id
            seen["state"] = committed.state
            seen["record_refs"] = tuple(committed.record_refs or ())
            return {
                "scanned": 1,
                "processed": 1,
                "units": 1,
                "complete": True,
                "items": [
                    {
                        "target_kind": "event",
                        "target_id": event.event_id,
                        "operation_key": "op-1",
                        "outcome": "committed",
                        "candidate_id": candidate_id,
                        "receipt_ref": (
                            f"receipt:{committed.receipt.request_id}"
                            if committed.receipt is not None
                            else None
                        ),
                    }
                ],
            }

        report = run_slice(
            self.jobs,
            namespace=NS,
            task_id=job.task_id,
            worker=worker,
            limits=SliceLimits(max_seconds=60.0),
            now=NOW,
            guard=lambda targets: require_live_targets(
                self.store, namespace=NS, targets=targets
            ),
        )

        # 2. the slice is a real success, not a hopeful one
        self.assertEqual(report.stop_reason, "complete")
        self.assertEqual(report.state, "succeeded")
        self.assertEqual(seen["state"], "COMMITTED")
        self.assertEqual(len(seen["record_refs"]), 1)
        final = self.jobs.get(NS, job.task_id)
        self.assertEqual(final.success_checkpoint, 1)
        self.assertEqual(final.state, "succeeded")

        # 3. a new store sees the committed tentative belief and its scope
        second = self.open_second_store()
        try:
            record_id = seen["record_refs"][0]["record_id"]
            row = second.connection.execute(
                "SELECT rv.record_version, rv.status, p.body_json "
                "FROM record_versions rv "
                "JOIN private_payloads p ON p.payload_ref = rv.body_payload_ref "
                "WHERE rv.namespace = ? AND rv.record_id = ? "
                "ORDER BY rv.record_version",
                (NS, record_id),
            ).fetchone()
            self.assertIsNotNone(row)
            import json

            body = json.loads(row["body_json"])
            self.assertEqual(body["claim"], "the synthetic shelf holds a blue box")
            self.assertEqual(body["claim_type"], "general_hypothesis")
            # no evidence links -> the contract lowers strength; the loop
            # must not inflate a hypothesis into a supported claim
            self.assertEqual(body["strength"], "insufficient")
            self.assertEqual(row["status"], "proposed")
        finally:
            second.close()

    # ------------------------------------------------------- WP2 delivery chain

    def test_memory_source_is_projected_and_delivered_to_applied(self) -> None:
        """WP2: legal source -> projection -> bounded page -> ``applied``.

        The earlier end-to-end case proved the *proposal to commit* half with a
        raw event.  This one proves the Memory half: a bridge projection must
        exist before ``deliver_page`` can do anything, and the delivery must
        stop at ``applied`` because no external ACK source exists.
        """

        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="the synthetic shelf holds one blue box",
            privacy_class="private",
            retention="standard",
        )
        self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance="synthetic-host",
            access_ref="access:synthetic",
            policy_ref="policy:j04",
            input_refs=[
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": row.input_id,
                    "content_version": row.content_version,
                }
            ],
            result="answered",
        )

        bridge = ExperienceBridge(
            self.store, _StubAdapter(), BridgeConfig(scope_id="scope-j08", max_batch=8)
        )
        synced = bridge.sync(_experience_access(), namespace=NS, limit=10)
        self.assertEqual(synced.projected, 1)
        self.assertEqual(synced.pending, 1)

        event_row = self.store.connection.execute(
            "SELECT event_id FROM experience_source_mapping WHERE namespace = ?",
            (NS,),
        ).fetchone()
        event_id = str(event_row["event_id"])
        self.assertIsNotNone(self.store.projection_for_event(NS, event_id))

        outcomes = deliver_page(
            self.store,
            namespace=NS,
            event_ids=[event_id],
            scope={"scene": "test.scene"},
            authorization_ref="authorization:j08",
        )

        # The episode was built and the outbox row moved to ``applied``; it may
        # not claim ``verified`` without an external acknowledgement.
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].detail, "applied_awaiting_external_ack")
        self.assertEqual(outcomes[0].target_kind, "memory_delivery")
        state = self.store.connection.execute(
            "SELECT state FROM memory_delivery WHERE delivery_id = ?",
            (outcomes[0].target_id,),
        ).fetchone()
        self.assertEqual(state["state"], "applied")

        # The episode is a real record the rest of the system can read back.
        episode_ref = outcomes[0].episode_ref
        self.assertIsNotNone(episode_ref)
        episode = self.store.connection.execute(
            "SELECT rv.record_id FROM record_versions rv "
            "WHERE rv.namespace = ? AND rv.record_id = ?",
            (NS, episode_ref["record_id"]),
        ).fetchone()
        self.assertIsNotNone(episode, "the delivery must persist an Episode record")

        # A replay must not promote an unacknowledged delivery.
        again = deliver_page(
            self.store,
            namespace=NS,
            event_ids=[event_id],
            scope={"scene": "test.scene"},
            authorization_ref="authorization:j08",
        )
        self.assertEqual(len(again), 1)
        self.assertNotEqual(again[0].detail, "verified")
        state = self.store.connection.execute(
            "SELECT state FROM memory_delivery WHERE delivery_id = ?",
            (outcomes[0].target_id,),
        ).fetchone()
        self.assertEqual(state["state"], "applied")

    def test_delivery_slice_stops_at_applied_and_withholds_the_checkpoint(self) -> None:
        """WP2 inside the real maintenance machinery, not a bare function call.

        ``applied`` is not ``verified``: the slice records the item as
        ``deferred`` and the job cannot advance its checkpoint.  That is the
        honest shape of a missing ACK source, and it is asserted rather than
        described.
        """

        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="the synthetic shelf holds one green box",
            privacy_class="private",
            retention="standard",
        )
        self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance="synthetic-host",
            access_ref="access:synthetic",
            policy_ref="policy:j04",
            input_refs=[
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": row.input_id,
                    "content_version": row.content_version,
                }
            ],
            result="answered",
        )
        bridge = ExperienceBridge(
            self.store, _StubAdapter(), BridgeConfig(scope_id="scope-j08", max_batch=8)
        )
        bridge.sync(_experience_access(), namespace=NS, limit=10)
        event_id = str(
            self.store.connection.execute(
                "SELECT event_id FROM experience_source_mapping WHERE namespace = ?",
                (NS,),
            ).fetchone()["event_id"]
        )

        job = self.jobs.register(
            namespace=NS,
            task_type="memory_delivery",
            trigger_key="trigger-delivery-1",
            params={"page": 1},
            config_version="j08-v1",
            input_watermark="0",
            target_watermark="1",
        )
        report = run_slice(
            self.jobs,
            namespace=NS,
            task_id=job.task_id,
            worker=memory_delivery_worker(
                store=self.store,
                namespace=NS,
                scope={"scene": "test.scene"},
                authorization_ref="authorization:j08",
                event_ids=[event_id],
            ),
            limits=SliceLimits(max_seconds=60.0),
            now=NOW,
            guard=_pass_guard,
        )

        items = self.jobs.items(NS, job.task_id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].outcome, "deferred")
        self.assertIn("applied", items[0].detail or "")

        # applied != verified, so the transaction of progress cannot close.
        self.assertEqual(report.stop_reason, "awaits_confirmation")
        self.assertEqual(self.jobs.get(NS, job.task_id).success_checkpoint, 0)
        self.assertEqual(self.jobs.get(NS, job.task_id).state, "paused")


class _StubAdapter:
    """The bridge only needs an adapter for ``deliver``; ``sync`` never calls it."""

    def capture(self, event, *, actor_origin, timeout_seconds):  # pragma: no cover
        raise AssertionError("sync must not reach the delivery adapter")


def _experience_access() -> ExperienceAccess:
    return ExperienceAccess(
        namespace=NS,
        host_instance="synthetic-host",
        access_ref="access:synthetic",
        privacy_classes=frozenset({"private"}),
        purposes=frozenset({"memory.bridge"}),
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
