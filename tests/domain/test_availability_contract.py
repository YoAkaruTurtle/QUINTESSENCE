"""J01: the shared "currently usable" contract for Belief delivery.

The tests cover two surfaces:

* C10 ``query_current_beliefs`` / ``query_exact_belief`` must stop delivering a
  Belief version once a source it cites is behind a deletion barrier, without
  rewriting the stored status and without exposing the omission to a model.
* ``reevaluate_belief_against_live_sources`` must compute the surviving-evidence
  verdict through the same derivation rules as the commit path, and must write
  nothing.

Every fixture is synthetic and local: no model call, no network, no private
asset.  AC numbers refer to the J01 task card v1.5.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import (  # noqa: E402
    NS,
    NS_B,
    NOW,
    CognitionTestCase,
)

from quintessence_pcs.domains import EpisodeService  # noqa: E402
from quintessence_pcs.domains.cognition import (  # noqa: E402
    ALL_PRIVACY,
    BeliefCurrentQuery,
    BeliefExactQuery,
    CognitionQueryService,
    QueryAccess,
    SELF_VIEW_FRESH,
    SELF_VIEW_UNAVAILABLE,
    reevaluate_belief_against_live_sources,
)
from quintessence_pcs.domains.cognition import query as query_module  # noqa: E402
from quintessence_pcs.domains.refs import RecordRef  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    AuthorizationRequired,
    InvalidInput,
    ScopeViolation,
)

LATER = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


class AvailabilityContractTests(CognitionTestCase):
    """One synthetic Belief store with the C10 service wired to the fixed clock."""

    def setUp(self) -> None:
        super().setUp()
        self.queries = CognitionQueryService(self.store, clock=self.clock)
        self.episodes = EpisodeService(self.store)

    # ------------------------------------------------------------- fixtures

    def access(
        self,
        *,
        purposes: tuple[str, ...] = ("consult",),
        privacy: frozenset[str] | None = None,
        namespace: str = NS,
    ) -> QueryAccess:
        return QueryAccess(
            namespace=namespace,
            privacy_classes=ALL_PRIVACY if privacy is None else privacy,
            purposes=frozenset(purposes),
        )

    def ref_for(self, version: Any) -> RecordRef:
        return RecordRef(
            namespace=version.namespace,
            record_id=version.record_id,
            record_version=version.record_version,
        )

    def exact(
        self,
        ref: RecordRef,
        *,
        access: QueryAccess | None = None,
        scope: dict[str, Any] | None = None,
        service: CognitionQueryService | None = None,
    ):
        return (service or self.queries).query_exact_belief(
            access if access is not None else self.access(),
            BeliefExactQuery(
                ref=ref,
                scope={"scene": "test.scene"} if scope is None else scope,
            ),
        )

    def current(
        self,
        *,
        query: BeliefCurrentQuery | None = None,
        access: QueryAccess | None = None,
        service: CognitionQueryService | None = None,
    ):
        return (service or self.queries).query_current_beliefs(
            access if access is not None else self.access(),
            BeliefCurrentQuery(scope={"scene": "test.scene"}, limit=20)
            if query is None
            else query,
        )

    def propose_with_events(
        self,
        *,
        events: int = 1,
        valid_until: str | None = None,
    ) -> tuple[Any, list[str]]:
        event_ids = [
            self.ingest(event_type="CapabilityObserved:file-read").event_id
            for _ in range(events)
        ]
        body = self.belief_body(
            evidence_links=[
                self.evidence_link(self.event_ref(event_id))
                for event_id in event_ids
            ],
            valid_until=valid_until,
        )
        return self.propose(body=body), event_ids

    def build_episode(self, event_id: str) -> Any:
        return self.episodes.build_episode_from_event(
            namespace=NS,
            event_id=event_id,
            scope={"scene": "test.scene"},
            authorization_ref="auth:test-episode",
        )

    def liveness_spy(self):
        """Count real ``belief_evidence_is_live`` calls at the C10 call site."""

        original = query_module.belief_evidence_is_live
        return mock.patch.object(
            query_module, "belief_evidence_is_live", wraps=original
        )

    # ------------------------------------------------------------------ ACs

    def test_ac01_current_query_omits_belief_after_sole_evidence_deleted(
        self,
    ) -> None:
        version, (event_id,) = self.propose_with_events(events=1)
        self.assertEqual(1, len(self.current().items))

        self.delete_event(event_id)

        result = self.current()
        self.assertEqual("empty", result.status)
        self.assertEqual((), result.items)
        body_text = result.to_body()
        self.assertNotIn(version.record_id, body_text)
        self.assertNotIn("capability:file-read", body_text)
        self.assertEqual([], json.loads(body_text)["omissions"])
        # Both serializations keep only the public reason set (existing
        # Consult/artifact contract); the audit count stays on the in-memory
        # result, which is never persisted or rendered.
        self.assertEqual([], json.loads(result.to_artifact_body())["omissions"])
        self.assertEqual(
            [{"reason": "evidence_not_live", "count": 1}],
            [
                {"reason": omission.reason, "count": omission.count}
                for omission in result.omissions
            ],
        )

    def test_ac02_current_query_omits_belief_when_any_evidence_deleted(
        self,
    ) -> None:
        version, (first, second) = self.propose_with_events(events=2)
        stored = self.store.get_current(NS, version.record_id)
        self.assertEqual(2, len(stored.body["evidence_links"]))

        self.delete_event(first)

        result = self.current()
        self.assertEqual("empty", result.status)
        self.assertEqual((), result.items)
        self.assertNotIn(version.record_id, result.to_body())
        # A surviving second link must not re-open a partially supported item.
        self.assertNotIn("capability:file-read", result.to_body())

    def test_ac03_episode_evidence_source_deletion_blocks_belief(self) -> None:
        receipt = self.ingest(event_type="ObservationRecorded")
        episode = self.build_episode(receipt.event_id)
        body = self.belief_body(
            evidence_links=[self.evidence_link(self.record_ref(episode))]
        )
        version = self.propose(body=body)
        self.assertEqual(1, len(self.current().items))

        self.delete_event(receipt.event_id)

        result = self.current()
        self.assertEqual("empty", result.status)
        self.assertNotIn(version.record_id, result.to_body())

    def test_ac04_exact_matches_missing_ref_after_evidence_deletion(self) -> None:
        version, (event_id,) = self.propose_with_events(events=1)
        self.delete_event(event_id)

        deleted_evidence = self.exact(self.ref_for(version))
        missing = self.exact(
            RecordRef(namespace=NS, record_id=str(uuid.uuid4()), record_version=1)
        )

        self.assertEqual("unavailable", deleted_evidence.status)
        self.assertEqual((), deleted_evidence.items)
        self.assertEqual(missing.to_body(), deleted_evidence.to_body())

    def test_ac05_historical_version_survives_when_its_own_evidence_does(
        self,
    ) -> None:
        version, (old_event,) = self.propose_with_events(events=1)
        new_event = self.ingest(event_type="CapabilityObserved:file-read-2")
        replacement = self.belief_body(
            evidence_links=[self.evidence_link(self.event_ref(new_event.event_id))],
            strength_reason="synthetic revision with different evidence",
        )
        revised = self.revise(self.record_ref(version), body=replacement)
        self.assertGreater(revised.record_version, version.record_version)

        self.delete_event(new_event.event_id)

        historical = self.exact(self.ref_for(version))
        self.assertEqual("ok", historical.status)
        self.assertEqual(1, len(historical.items))
        self.assertFalse(historical.items[0].current)
        self.assertEqual("historical", historical.items[0].applicability)

        blocked = self.exact(self.ref_for(revised))
        self.assertEqual("unavailable", blocked.status)
        self.assertEqual((), blocked.items)

    def test_ac06_deleted_belief_record_is_absent_and_unavailable(self) -> None:
        version, _events = self.propose_with_events(events=1)
        self.delete_record(version.record_id)

        self.assertEqual("empty", self.current().status)
        self.assertEqual("unavailable", self.exact(self.ref_for(version)).status)

    def test_ac07_deletion_rewrites_no_status_and_appends_no_version(self) -> None:
        version, (event_id,) = self.propose_with_events(events=1)
        revision_before = self.revision()
        versions_before = self.count_rows("record_versions")

        self.delete_event(event_id)

        stored = self.store.get_current(NS, version.record_id)
        self.assertEqual("supported", stored.status)
        self.assertEqual(version.record_version, stored.record_version)
        self.assertEqual(versions_before, self.count_rows("record_versions"))
        # store/deletion.py (module doc, lines 8-12): Tx1 advances the state
        # revision exactly once; this records the actual contract value.
        self.assertEqual(revision_before + 1, self.revision())

    def test_ac08_public_body_never_carries_the_internal_reason(self) -> None:
        _version, (event_id,) = self.propose_with_events(events=1)
        self.delete_event(event_id)

        result = self.current()
        self.assertIn("evidence_not_live", query_module.INTERNAL_OMISSION_REASONS)
        self.assertNotIn("evidence_not_live", query_module.PUBLIC_OMISSION_REASONS)
        self.assertEqual([], json.loads(result.to_body())["omissions"])
        self.assertNotIn("evidence_not_live", result.to_artifact_body())
        self.assertIn(
            "evidence_not_live",
            [omission.reason for omission in result.omissions],
        )

    def test_ac09_expiry_semantics_are_unchanged(self) -> None:
        version, (event_id,) = self.propose_with_events(
            events=1, valid_until="2026-10-18T00:00:00Z"
        )
        later_service = CognitionQueryService(self.store, clock=lambda: LATER)

        included = self.current(
            service=later_service,
            query=BeliefCurrentQuery(
                scope={"scene": "test.scene"}, include_expired=True, limit=20
            ),
        )
        self.assertEqual("ok", included.status)
        self.assertTrue(included.items[0].expired)
        self.assertEqual("expired", included.items[0].applicability)

        excluded = self.current(
            service=later_service,
            query=BeliefCurrentQuery(
                scope={"scene": "test.scene"}, include_expired=False, limit=20
            ),
        )
        self.assertEqual("empty", excluded.status)
        self.assertEqual(
            [{"reason": "expired", "count": 1}],
            json.loads(excluded.to_body())["omissions"],
        )

        # Expiry and a dead source stay distinct: with include_expired the dead
        # source wins and is counted under its own internal reason.
        self.delete_event(event_id)
        blocked = self.current(
            service=later_service,
            query=BeliefCurrentQuery(
                scope={"scene": "test.scene"}, include_expired=True, limit=20
            ),
        )
        self.assertEqual("empty", blocked.status)
        self.assertIn(
            "evidence_not_live",
            [omission.reason for omission in blocked.omissions],
        )
        self.assertNotIn("expired", [omission.reason for omission in blocked.omissions])
        self.assertNotIn(version.record_id, blocked.to_body())

    def test_ac10_status_whitelist_is_unchanged(self) -> None:
        self.assertEqual(
            ("supported", "insufficient", "contradicted"),
            query_module.CURRENT_BELIEF_STATUSES,
        )
        body = self.belief_body(evidence_links=[])
        unsupported = self.propose(body=body)
        stored = self.store.get_current(NS, unsupported.record_id)
        self.assertEqual("proposed", stored.status)

        self.assertEqual("empty", self.current().status)
        with self.assertRaises(InvalidInput):
            self.current(query=BeliefCurrentQuery(statuses=("proposed",)))

    def test_ac11_context_and_self_view_still_filter_dead_evidence(self) -> None:
        version, (event_id,) = self.propose_with_events(events=1)
        self.self_view.build_self_view(namespace=NS)
        self.assertEqual(SELF_VIEW_FRESH, self.self_view.get_self_view(NS).freshness)

        self.delete_event(event_id)

        state = self.self_view.get_self_view(NS)
        self.assertEqual(SELF_VIEW_UNAVAILABLE, state.freshness)

        context = self.build_context()
        self.assertNotIn(
            version.record_id,
            [ref.record_id for ref in context.belief_refs],
        )
        self.assertNotIn("capability:file-read", context.rendered_input)

    def test_ac14_scope_mismatch_stays_empty_with_a_public_reason(self) -> None:
        version, (event_id,) = self.propose_with_events(events=1)
        self.delete_event(event_id)

        result = self.current(
            query=BeliefCurrentQuery(scope={"scene": "other.scene"}, limit=20)
        )
        self.assertEqual("empty", result.status)
        self.assertEqual(
            [{"reason": "scope_mismatch", "count": 1}],
            json.loads(result.to_body())["omissions"],
        )
        self.assertNotIn(version.record_id, result.to_body())

    def test_ac15_gate_never_runs_when_gate_one_fails(self) -> None:
        version, (event_id,) = self.propose_with_events(events=1)
        self.delete_event(event_id)
        restricted = frozenset({"public-example"})

        # Positive control: the spy really observes the gate when it does run.
        with self.liveness_spy() as control:
            self.current()
        self.assertEqual(1, control.call_count)

        cases: list[tuple[str, Any]] = []

        def run_current_privacy() -> Any:
            return self.current(access=self.access(privacy=restricted))

        def run_current_purpose() -> Any:
            return self.current(access=self.access(purposes=()))

        def run_current_scope() -> Any:
            return self.current(
                query=BeliefCurrentQuery(scope={"scene": "other.scene"}, limit=20)
            )

        def run_exact_purpose() -> Any:
            return self.exact(self.ref_for(version), access=self.access(purposes=()))

        def run_exact_privacy() -> Any:
            return self.exact(
                self.ref_for(version), access=self.access(privacy=restricted)
            )

        def run_exact_scope() -> Any:
            return self.exact(
                self.ref_for(version), scope={"scene": "other.scene"}
            )

        def run_exact_namespace() -> Any:
            return self.exact(
                RecordRef(namespace=NS_B, record_id=version.record_id, record_version=1)
            )

        cases = [
            ("current_privacy", run_current_privacy),
            ("current_purpose", run_current_purpose),
            ("current_scope", run_current_scope),
            ("exact_purpose", run_exact_purpose),
            ("exact_privacy", run_exact_privacy),
            ("exact_scope", run_exact_scope),
            ("exact_namespace", run_exact_namespace),
        ]
        expected_errors = {
            "current_purpose": AuthorizationRequired,
            "exact_purpose": AuthorizationRequired,
            "exact_namespace": ScopeViolation,
        }
        for name, call in cases:
            with self.subTest(case=name):
                spy = self.liveness_spy()
                with spy as patched:
                    if name in expected_errors:
                        with self.assertRaises(expected_errors[name]):
                            call()
                    else:
                        call()
                self.assertEqual(
                    0,
                    patched.call_count,
                    f"{name} must not reach the source-liveness gate",
                )

        # The two non-exception shapes are still the existing ones.
        privacy_result = run_current_privacy()
        self.assertEqual("empty", privacy_result.status)
        self.assertIn(
            "not_authorized",
            [omission.reason for omission in privacy_result.omissions],
        )
        scope_result = run_exact_scope()
        self.assertEqual("empty", scope_result.status)
        self.assertEqual(
            [{"reason": "scope_mismatch", "count": 1}],
            json.loads(scope_result.to_body())["omissions"],
        )

    def test_ac16_one_event_shared_by_two_beliefs_blocks_both(self) -> None:
        shared = self.ingest(event_type="CapabilityObserved:shared").event_id
        link = self.evidence_link(self.event_ref(shared))
        first = self.propose(body=self.belief_body(evidence_links=[link]))
        second = self.propose(
            body=self.belief_body(
                claim="capability:shared-other",
                evidence_links=[link],
            )
        )
        self.assertEqual(2, len(self.current().items))

        self.delete_event(shared)

        result = self.current()
        self.assertEqual("empty", result.status)
        body = result.to_body()
        self.assertNotIn(first.record_id, body)
        self.assertNotIn(second.record_id, body)

    def test_ac17_episode_rebuild_is_idempotent(self) -> None:
        receipt = self.ingest(event_type="ObservationRecorded")
        first = self.build_episode(receipt.event_id)
        second = self.build_episode(receipt.event_id)
        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(first.record_version, second.record_version)
        self.assertEqual(1, second.record_version)

    def test_ac18_reevaluation_matches_a_fresh_proposal_and_writes_nothing(
        self,
    ) -> None:
        version, (kept, dropped) = self.propose_with_events(events=2)
        self.delete_event(dropped)
        stored = self.store.get_current(NS, version.record_id)

        revision_before = self.revision()
        versions_before = self.count_rows("record_versions")
        pointer_before = self.store.record_pointer(NS, version.record_id)

        result = reevaluate_belief_against_live_sources(
            self.store, namespace=NS, body=stored.body, clock=self.clock
        )

        self.assertEqual(revision_before, self.revision())
        self.assertEqual(versions_before, self.count_rows("record_versions"))
        pointer_after = self.store.record_pointer(NS, version.record_id)
        self.assertEqual(
            pointer_before.current_version, pointer_after.current_version
        )

        fresh_body = self.belief_body(
            evidence_links=[self.evidence_link(self.event_ref(kept))],
            strength="moderate",
        )
        fresh = self.propose(body=fresh_body)
        fresh_stored = self.store.get_current(NS, fresh.record_id)

        self.assertEqual(fresh_stored.status, result.status)
        self.assertEqual(fresh_stored.body["strength"], result.strength)
        self.assertEqual(1, result.live_link_count)
        self.assertEqual(1, len(result.retired_refs))
        self.assertEqual("event", result.retired_refs[0]["ref_type"])
        self.assertEqual(dropped, result.retired_refs[0]["event_id"])

    def test_ac19_repeated_queries_after_deletion_are_stable(self) -> None:
        version, (event_id,) = self.propose_with_events(events=1)
        self.assertEqual(1, len(self.current().items))

        self.delete_event(event_id)
        first = self.current()
        second = self.current()

        self.assertEqual("empty", first.status)
        self.assertEqual(first.to_body(), second.to_body())
        self.assertEqual((), first.items)
        self.assertNotIn(version.record_id, first.to_body())

    def test_ac20_five_unavailable_shapes_are_byte_identical(self) -> None:
        evidence_version, (event_id,) = self.propose_with_events(events=1)
        record_version, _events = self.propose_with_events(events=1)
        self.delete_record(record_version.record_id)
        self.delete_event(event_id)
        episode_ref = self.record_ref(self.build_episode(
            self.ingest(event_type="ObservationRecorded").event_id
        ))

        shapes = {
            "deleted": self.exact(self.ref_for(record_version)),
            "missing": self.exact(
                RecordRef(namespace=NS, record_id=str(uuid.uuid4()), record_version=1)
            ),
            "not_belief": self.exact(
                RecordRef(
                    namespace=episode_ref["namespace"],
                    record_id=episode_ref["record_id"],
                    record_version=episode_ref["record_version"],
                )
            ),
            "private": self.exact(
                self.ref_for(evidence_version),
                access=self.access(privacy=frozenset({"public-example"})),
            ),
            "evidence": self.exact(self.ref_for(evidence_version)),
        }

        bodies = {name: shape.to_body() for name, shape in shapes.items()}
        self.assertEqual(1, len(set(bodies.values())), msg=sorted(bodies))
        for name, shape in shapes.items():
            self.assertEqual("unavailable", shape.status, msg=name)
            self.assertEqual((), shape.items, msg=name)
            self.assertNotIn(evidence_version.record_id, bodies[name])
            self.assertNotIn("capability:file-read", bodies[name])

    def test_query_policy_version_is_v2(self) -> None:
        _version, _events = self.propose_with_events(events=1)
        result = self.current()
        self.assertEqual("collab.v1.query.v2", result.query_policy_version)
        self.assertEqual(
            query_module.QUERY_POLICY_VERSION, result.query_policy_version
        )


if __name__ == "__main__":
    unittest.main()
