"""C10: deterministic Belief and Episode queries.

The tests follow the card's seven groups: exact Belief reads, current Belief
necessary conditions, Episode metadata reuse, purpose/privacy authorization,
the disclosure boundary (including the restricted 0/1/N construction), the
single read snapshot and clock, and the read-only/no-result invariants.
Every store is synthetic and local; no model, network or private asset is used.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import (  # noqa: E402
    NS,
    NS_B,
    NOW,
    OBSERVED_AT,
    RECORDED_AT,
    CognitionTestCase,
)

from quintessence_pcs.domains import EpisodeService  # noqa: E402
from quintessence_pcs.domains.cognition import (  # noqa: E402
    BeliefCurrentQuery,
    BeliefExactQuery,
    CognitionQueryService,
    EpisodeQuery,
    QueryAccess,
    QUERY_POLICY_VERSION,
)
from quintessence_pcs.domains.cognition import query as query_module  # noqa: E402
from quintessence_pcs.domains.cognition.beliefs import (  # noqa: E402
    INFERENCE_BASIS_BY_CLAIM_TYPE,
)
from quintessence_pcs.domains.memory.search import (  # noqa: E402
    EpisodeHit,
    SearchResult,
    search_episodes,
)
from quintessence_pcs.domains.refs import EventRef, RecordRef  # noqa: E402
from quintessence_pcs.foundation import identifiers  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    AuthorizationRequired,
    CorruptStore,
    InvalidInput,
    ScopeViolation,
)
from quintessence_pcs.foundation.jsonutil import canonical_json_text  # noqa: E402
from quintessence_pcs.store import CognitionStore  # noqa: E402

ALL_PRIVACY = frozenset(identifiers.PRIVACY_CLASSES)
LATER = NOW + timedelta(days=2)
SECRET = "c10-payload-secret-that-must-not-leak"
MAX_HIDDEN = 3
_FIXED = uuid.UUID("8c9e1f2a-3b4d-5e6f-8a90-1b2c3d4e5f60")
VISIBLE_BELIEF_ID = str(uuid.uuid5(_FIXED, "c10-visible"))
HIDDEN_BELIEF_IDS = tuple(
    str(uuid.uuid5(_FIXED, f"c10-hidden-{index}")) for index in range(MAX_HIDDEN)
)


# --------------------------------------------------------------------------
# direct-write fixtures (no service path can create a restricted Belief)
# --------------------------------------------------------------------------


def _synthetic_body(
    claim: str, claim_type: str, *, valid_until: str | None = None
) -> dict[str, Any]:
    return {
        "claim": claim,
        "claim_type": claim_type,
        "evidence_links": [],
        "strength": "limited",
        "strength_reason": "synthetic C10 disclosure fixture",
        "inference_basis": INFERENCE_BASIS_BY_CLAIM_TYPE[claim_type],
        "premise_refs": [],
        "alternative_group_id": None,
        "valid_until": valid_until,
        "review_condition": "review when new evidence appears",
    }


def _append_belief(
    store: CognitionStore,
    *,
    record_id: str,
    claim: str,
    claim_type: str,
    status: str,
    privacy_class: str,
    scope: dict[str, Any] | None = None,
    namespace: str = NS,
) -> Any:
    return store.append_record_version(
        namespace=namespace,
        record_id=record_id,
        record_version=1,
        kind="belief",
        body=_synthetic_body(claim, claim_type),
        status=status,
        schema_version="1",
        privacy_class=privacy_class,
        scope={} if scope is None else scope,
    )


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------


class QueryTestCase(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.queries = CognitionQueryService(self.store, clock=self.clock)
        self.episodes = EpisodeService(self.store)

    # ----------------------------------------------------------------- setup

    def access(
        self,
        *,
        purposes: tuple[str, ...] = ("consult", "reasoning", "history"),
        privacy: frozenset[str] = ALL_PRIVACY,
        namespace: str = NS,
    ) -> QueryAccess:
        return QueryAccess(
            namespace=namespace,
            privacy_classes=privacy,
            purposes=frozenset(purposes),
        )

    def service(self, now: datetime = NOW) -> CognitionQueryService:
        return CognitionQueryService(self.store, clock=lambda: now)

    def ref_for(self, version: Any) -> RecordRef:
        return RecordRef(
            namespace=version.namespace,
            record_id=version.record_id,
            record_version=version.record_version,
        )

    def exact_ref(self, ref: RecordRef, *, scope: Any = None) -> BeliefExactQuery:
        return BeliefExactQuery(ref=ref, scope={} if scope is None else scope)

    def exact(
        self,
        version: Any,
        *,
        scope: Any = None,
        access: QueryAccess | None = None,
        service: CognitionQueryService | None = None,
    ) -> Any:
        return (service or self.queries).query_exact_belief(
            access if access is not None else self.access(),
            self.exact_ref(self.ref_for(version), scope=scope),
        )

    def current(
        self,
        *,
        query: BeliefCurrentQuery | None = None,
        access: QueryAccess | None = None,
        service: CognitionQueryService | None = None,
    ) -> Any:
        return (service or self.queries).query_current_beliefs(
            access if access is not None else self.access(),
            BeliefCurrentQuery(scope={"scene": "test.scene"}, limit=20)
            if query is None
            else query,
        )

    def episodes_query(
        self,
        *,
        query: str = "ObservationRecorded",
        scope: Any = None,
        purpose: str = "reasoning",
        limit: int = 5,
        access: QueryAccess | None = None,
        service: CognitionQueryService | None = None,
    ) -> Any:
        return (service or self.queries).query_episodes(
            access if access is not None else self.access(),
            EpisodeQuery(
                query=query,
                scope={} if scope is None else scope,
                purpose=purpose,
                limit=limit,
            ),
        )

    # ----------------------------------------------------------- belief setup

    def propose_supported(
        self,
        *,
        claim: str = "capability:file-read",
        claim_type: str = "capability",
        scope: Any = None,
        valid_until: str | None = None,
        event_id: str | None = None,
    ) -> Any:
        if event_id is None:
            event_id = self.ingest(event_type="CapabilityObserved:file-read").event_id
        body = self.belief_body(
            claim=claim,
            claim_type=claim_type,
            evidence_links=[self.evidence_link(self.event_ref(event_id))],
            valid_until=valid_until,
        )
        return self.propose(
            body=body,
            scope=self.default_scope(claim_type) if scope is None else scope,
        )

    def revised_once(self, version: Any) -> Any:
        """Append version 2 of a supported capability with a changed reason."""

        body = dict(self.store.get_current(NS, version.record_id).body)
        body["strength_reason"] = "synthetic revision for the exact-history test"
        return self.revise(
            self.record_ref(version),
            body=body,
            scope=self.default_scope(body["claim_type"]),
        )

    def retract(self, version: Any) -> Any:
        body = dict(self.store.get_current(NS, version.record_id).body)
        return self.revise(
            self.record_ref(version),
            body=body,
            scope=self.default_scope(body["claim_type"]),
            retract=True,
        )

    # ---------------------------------------------------------- episode setup

    def build_episode(
        self,
        *,
        event_id: str | None = None,
        scope: Any = None,
        privacy_class: str = "private",
        event_type: str = "ObservationRecorded",
        payload: Any = None,
        source_event_id: str | None = None,
    ) -> tuple[Any, Any]:
        receipt = self.store.ingest_event(
            namespace=NS,
            source_kind="host_observation",
            source_instance="c10-query-tests",
            source_event_id=source_event_id or f"source-{uuid.uuid4()}",
            event_type=event_type,
            observed_at=OBSERVED_AT,
            recorded_at=RECORDED_AT,
            payload={"text": SECRET} if payload is None else payload,
            privacy_class=privacy_class,
        )
        episode = self.episodes.build_episode_from_event(
            namespace=NS,
            event_id=event_id or receipt.event_id,
            scope={} if scope is None else scope,
            authorization_ref="auth:c10-query-test",
        )
        return receipt, episode

    # ------------------------------------------------------------ assertions

    @staticmethod
    def internal_omissions(result: Any) -> dict[str, int]:
        return {omission.reason: omission.count for omission in result.omissions}

    @staticmethod
    def body_dict(result: Any) -> dict[str, Any]:
        return json.loads(result.to_body())

    def assert_no_content(self, text: str, *needles: str) -> None:
        for needle in needles:
            self.assertNotIn(needle, text)


# --------------------------------------------------------------------------
# A. exact Belief
# --------------------------------------------------------------------------


class ExactBeliefQueryTests(QueryTestCase):
    def test_current_version_is_current(self) -> None:
        version = self.propose_supported()

        result = self.queries.query(
            self.access(), self.exact_ref(self.ref_for(version), scope={"scene": "test.scene"})
        )

        self.assertEqual("belief", result.query_kind)
        self.assertEqual("ok", result.status)
        self.assertEqual("complete", result.material_status)
        self.assertEqual(QUERY_POLICY_VERSION, result.query_policy_version)
        self.assertEqual(1, len(result.items))
        item = result.items[0]
        self.assertEqual(self.ref_for(version), item.ref)
        self.assertEqual(version.body["claim"], item.claim)
        self.assertEqual("capability", item.claim_type)
        self.assertEqual("supported", item.stored_status)
        self.assertEqual("limited", item.strength)
        self.assertEqual({"scene": "test.scene"}, item.scope)
        self.assertIsNone(item.valid_until)
        self.assertTrue(item.current)
        self.assertFalse(item.expired)
        self.assertEqual("current", item.applicability)
        self.assertEqual((self.ref_for(version),), item.source_refs)
        self.assertEqual((self.ref_for(version),), result.source_refs)
        self.assertEqual((), result.omissions)
        self.assertFalse(result.truncated)
        self.assertEqual(self.revision(), result.state_revision)
        self.assertEqual("current", self.body_dict(result)["items"][0]["applicability"])

    def test_historical_version_is_returned_with_current_false(self) -> None:
        first = self.propose_supported()
        second = self.revised_once(first)
        self.assertEqual(2, second.record_version)

        result = self.exact(first, scope={"scene": "test.scene"})

        self.assertEqual("ok", result.status)
        item = result.items[0]
        self.assertEqual(1, item.ref.record_version)
        self.assertFalse(item.current)
        self.assertEqual("historical", item.applicability)
        self.assertEqual("historical", self.body_dict(result)["items"][0]["applicability"])

    def test_deleted_exact_ref_is_unavailable_without_content(self) -> None:
        version = self.propose_supported()
        claim = version.body["claim"]
        self.delete_record(version.record_id)

        result = self.exact(version, scope={"scene": "test.scene"})

        self.assertEqual("unavailable", result.status)
        self.assertEqual("unavailable", result.material_status)
        self.assertEqual((), result.items)
        self.assertEqual((), result.source_refs)
        self.assertEqual({"unavailable": 1}, self.internal_omissions(result))
        self.assert_no_content(result.to_body(), claim, "not_found", "exists")

    def test_missing_and_unauthorized_exact_refs_share_one_answer(self) -> None:
        restricted = _append_belief(
            self.store,
            record_id=str(uuid.uuid4()),
            claim="restricted-claim-text",
            claim_type="capability",
            status="supported",
            privacy_class="restricted",
        )
        _receipt, episode = self.build_episode()
        access = self.access(privacy=frozenset({"private"}))
        missing_ref = RecordRef(
            namespace=NS, record_id=str(uuid.uuid4()), record_version=1
        )
        missing_version_ref = RecordRef(
            namespace=NS, record_id=restricted.record_id, record_version=99
        )

        missing_result = self.queries.query_exact_belief(
            access, self.exact_ref(missing_ref)
        )
        unauthorized_result = self.queries.query_exact_belief(
            access, self.exact_ref(self.ref_for(restricted))
        )
        non_belief_result = self.queries.query_exact_belief(
            access, self.exact_ref(self.ref_for(episode))
        )
        missing_version_result = self.queries.query_exact_belief(
            access, self.exact_ref(missing_version_ref)
        )

        results = (
            missing_result,
            unauthorized_result,
            non_belief_result,
            missing_version_result,
        )
        for result in results:
            self.assertEqual("unavailable", result.status)
            self.assertEqual("unavailable", result.material_status)
            self.assertEqual((), result.items)
            self.assertEqual({"unavailable": 1}, self.internal_omissions(result))
        # Same revision, same bytes: no existence, kind or version signal.
        revisions = {result.state_revision for result in results}
        bodies = {result.to_body() for result in results}
        self.assertEqual(1, len(revisions))
        self.assertEqual(1, len(bodies))
        self.assert_no_content(
            unauthorized_result.to_body(),
            "restricted-claim-text",
            "not_found",
            "exists",
        )

    def test_scope_mismatch_is_empty_with_a_public_reason(self) -> None:
        version = self.propose_supported(scope={"scene": "test.scene"})

        result = self.exact(version, scope={"scene": "another.scene"})

        self.assertEqual("empty", result.status)
        self.assertEqual("complete", result.material_status)
        self.assertEqual((), result.items)
        self.assertEqual((), result.source_refs)
        self.assertEqual({"scope_mismatch": 1}, self.internal_omissions(result))
        body = self.body_dict(result)
        self.assertEqual(
            [{"reason": "scope_mismatch", "count": 1}], body["omissions"]
        )
        self.assert_no_content(result.to_body(), version.body["claim"])

    def test_expired_supported_keeps_its_stored_status(self) -> None:
        version = self.propose_supported(
            claim_type="environment_state",
            claim="environment_state:lab-window",
            valid_until="2026-10-18T12:00:00Z",
        )
        self.assertEqual("supported", self.store.get_current(NS, version.record_id).status)

        result = self.exact(
            version, scope={"scene": "test.scene"}, service=self.service(LATER)
        )

        self.assertEqual("ok", result.status)
        item = result.items[0]
        self.assertEqual("supported", item.stored_status)
        self.assertTrue(item.expired)
        self.assertTrue(item.current)
        self.assertEqual("expired", item.applicability)


# --------------------------------------------------------------------------
# B. current Belief
# --------------------------------------------------------------------------


class CurrentBeliefQueryTests(QueryTestCase):
    def test_claim_type_is_a_necessary_condition(self) -> None:
        capability = self.propose_supported(claim="capability:file-read")
        environment = self.propose_supported(
            claim="environment_state:lab-window",
            claim_type="environment_state",
            valid_until="2026-10-20T12:00:00Z",
        )

        only_capability = self.current(
            query=BeliefCurrentQuery(
                claim_types=("capability",),
                scope={"scene": "test.scene"},
                limit=20,
            )
        )
        both = self.current(
            query=BeliefCurrentQuery(scope={"scene": "test.scene"}, limit=20)
        )

        self.assertEqual(
            [self.ref_for(capability)],
            [item.ref for item in only_capability.items],
        )
        self.assertEqual("ok", only_capability.status)
        self.assertEqual(
            {self.ref_for(capability), self.ref_for(environment)},
            {item.ref for item in both.items},
        )

    def test_scope_is_a_necessary_condition(self) -> None:
        matching = self.propose_supported(
            claim="capability:a", scope={"scene": "test.scene"}
        )
        other = self.propose_supported(
            claim="capability:b", scope={"scene": "other.scene"}
        )

        result = self.current(
            query=BeliefCurrentQuery(scope={"scene": "test.scene"}, limit=20)
        )

        self.assertEqual([self.ref_for(matching)], [item.ref for item in result.items])
        self.assertEqual({"scope_mismatch": 1}, self.internal_omissions(result))
        self.assertNotIn(self.ref_for(other), [item.ref for item in result.items])

    def test_project_scope_is_exact_and_does_not_alias_different_ids(self) -> None:
        canonical = _append_belief(
            self.store,
            record_id=str(uuid.uuid4()),
            claim="user_preference:gray47-color",
            claim_type="user_preference",
            status="supported",
            privacy_class="private",
            scope={"scene": "test.scene", "project_id": "gray47"},
        )
        other = _append_belief(
            self.store,
            record_id=str(uuid.uuid4()),
            claim="user_preference:other-color",
            claim_type="user_preference",
            status="supported",
            privacy_class="private",
            scope={"scene": "test.scene", "project_id": "blue57"},
        )

        matching = self.current(
            query=BeliefCurrentQuery(
                claim_types=("user_preference",),
                scope={"scene": "test.scene", "project_id": "gray47"},
                limit=20,
            )
        )
        missing_project = self.current(
            query=BeliefCurrentQuery(
                claim_types=("user_preference",),
                scope={"scene": "test.scene"},
                limit=20,
            )
        )
        wrong_project = self.current(
            query=BeliefCurrentQuery(
                claim_types=("user_preference",),
                scope={"scene": "test.scene", "project_id": "blue57"},
                limit=20,
            )
        )

        self.assertEqual([self.ref_for(canonical)], [item.ref for item in matching.items])
        self.assertEqual("ok", matching.status)
        self.assertEqual((), missing_project.items)
        self.assertEqual({"scope_mismatch": 2}, self.internal_omissions(missing_project))
        self.assertEqual([self.ref_for(other)], [item.ref for item in wrong_project.items])
        self.assertNotIn(self.ref_for(other), [item.ref for item in matching.items])

    def test_status_filter_excludes_retracted(self) -> None:
        supported = self.propose_supported(claim="capability:kept")
        retracted = self.propose_supported(claim="capability:removed")
        self.retract(retracted)
        self.assertEqual("retracted", self.store.get_current(NS, retracted.record_id).status)

        result = self.current(
            query=BeliefCurrentQuery(
                statuses=("supported",),
                scope={"scene": "test.scene"},
                limit=20,
            )
        )

        self.assertEqual([self.ref_for(supported)], [item.ref for item in result.items])
        self.assertEqual({"status_not_current": 1}, self.internal_omissions(result))

        default_result = self.current(
            query=BeliefCurrentQuery(scope={"scene": "test.scene"}, limit=20)
        )
        self.assertEqual(
            [self.ref_for(supported)], [item.ref for item in default_result.items]
        )

    def test_expired_items_are_included_and_flagged_when_requested(self) -> None:
        version = self.propose_supported(
            claim="environment_state:lab-window",
            claim_type="environment_state",
            valid_until="2026-10-18T12:00:00Z",
        )

        result = self.current(
            query=BeliefCurrentQuery(
                scope={"scene": "test.scene"}, include_expired=True
            ),
            service=self.service(LATER),
        )

        self.assertEqual("ok", result.status)
        item = result.items[0]
        self.assertEqual(self.ref_for(version), item.ref)
        self.assertTrue(item.expired)
        self.assertEqual("expired", item.applicability)
        self.assertEqual("supported", item.stored_status)

    def test_expired_items_are_omitted_internally_when_excluded(self) -> None:
        version = self.propose_supported(
            claim="environment_state:lab-window",
            claim_type="environment_state",
            valid_until="2026-10-18T12:00:00Z",
        )

        result = self.current(
            query=BeliefCurrentQuery(
                scope={"scene": "test.scene"}, include_expired=False
            ),
            service=self.service(LATER),
        )

        self.assertEqual("empty", result.status)
        self.assertEqual((), result.items)
        self.assertEqual({"expired": 1}, self.internal_omissions(result))
        body = self.body_dict(result)
        self.assertEqual([{"reason": "expired", "count": 1}], body["omissions"])
        self.assertNotIn(self.ref_for(version).to_dict(), body["source_refs"])

    def test_current_query_never_returns_an_older_version(self) -> None:
        first = self.propose_supported()
        second = self.revised_once(first)

        result = self.current(
            query=BeliefCurrentQuery(scope={"scene": "test.scene"}, limit=20)
        )

        self.assertEqual(1, len(result.items))
        self.assertEqual(2, result.items[0].ref.record_version)
        self.assertEqual(second.record_version, result.items[0].ref.record_version)
        self.assertNotEqual(first.record_version, result.items[0].ref.record_version)

    def test_limit_truncates_with_a_stable_order(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read").event_id
        versions = [
            self.propose_supported(claim=f"capability:item-{index}", event_id=event)
            for index in range(3)
        ]

        limited = self.current(
            query=BeliefCurrentQuery(scope={"scene": "test.scene"}, limit=2)
        )
        full = self.current(
            query=BeliefCurrentQuery(scope={"scene": "test.scene"}, limit=20)
        )

        self.assertTrue(limited.truncated)
        self.assertEqual(2, len(limited.items))
        self.assertEqual(3, len(full.items))
        self.assertFalse(full.truncated)
        expected_ids = sorted(version.record_id for version in versions)
        self.assertEqual(expected_ids, [item.ref.record_id for item in full.items])
        self.assertEqual(expected_ids[:2], [item.ref.record_id for item in limited.items])

    def test_structural_errors_are_invalid_input(self) -> None:
        access = self.access()
        queries = (
            BeliefCurrentQuery(limit=0),
            BeliefCurrentQuery(limit=21),
            BeliefCurrentQuery(limit=True),
            BeliefCurrentQuery(limit="5"),
            BeliefCurrentQuery(claim_types=("not-a-claim-type",)),
            BeliefCurrentQuery(claim_types="capability"),
            BeliefCurrentQuery(statuses=("retracted",)),
            BeliefCurrentQuery(statuses="supported"),
            BeliefCurrentQuery(statuses=["supported"]),
            BeliefCurrentQuery(include_expired="yes"),
            BeliefCurrentQuery(scope={"unknown": "field"}),
            BeliefCurrentQuery(scope=None),
        )
        for query in queries:
            with self.subTest(query=query), self.assertRaises(InvalidInput):
                self.queries.query_current_beliefs(access, query)

        with self.assertRaises(InvalidInput):
            self.queries.query(access, object())

        bad_namespace = BeliefExactQuery(
            ref=RecordRef(
                namespace=NS_B, record_id=str(uuid.uuid4()), record_version=1
            ),
            scope={},
        )
        with self.assertRaises(ScopeViolation):
            self.queries.query_exact_belief(access, bad_namespace)

        bad_uuid = BeliefExactQuery(
            ref=RecordRef(namespace=NS, record_id="not-a-uuid", record_version=1),
            scope={},
        )
        with self.assertRaises(InvalidInput):
            self.queries.query_exact_belief(access, bad_uuid)


# --------------------------------------------------------------------------
# C. Episode
# --------------------------------------------------------------------------


class EpisodeQueryTests(QueryTestCase):
    def test_metadata_match_returns_no_excerpt_or_payload_text(self) -> None:
        receipt, episode = self.build_episode()

        result = self.episodes_query(query="ObservationRecorded")

        self.assertEqual("episode", result.query_kind)
        self.assertEqual("ok", result.status)
        self.assertEqual("complete", result.material_status)
        item = result.items[0]
        self.assertEqual(self.ref_for(episode), item.ref)
        self.assertEqual("observation", item.epistemic_status)
        self.assertEqual({}, item.scope)
        self.assertEqual(OBSERVED_AT, item.observed_at)
        self.assertGreater(item.rank_score, 0)
        self.assertFalse(hasattr(item, "excerpt"))
        self.assertEqual(
            (EventRef(namespace=NS, event_id=receipt.event_id),), item.source_refs
        )
        self.assertEqual(item.source_refs, result.source_refs)
        self.assert_no_content(result.to_body(), SECRET, "excerpt")

        # The reused search itself still returns excerpt=None.
        direct = search_episodes(
            self.store,
            namespace=NS,
            query="ObservationRecorded",
            scope={},
            limit=5,
            purpose="reasoning",
            privacy_classes=ALL_PRIVACY,
        )
        self.assertIsNone(direct.hits[0].excerpt)

    def test_scope_is_a_necessary_condition(self) -> None:
        self.build_episode(scope={"scene": "episode.scene"})

        matching = self.episodes_query(scope={"scene": "episode.scene"})
        other = self.episodes_query(scope={"scene": "another.scene"})

        self.assertEqual("ok", matching.status)
        self.assertEqual(1, len(matching.items))
        self.assertEqual("empty", other.status)
        self.assertEqual({"scope_mismatch": 1}, self.internal_omissions(other))

    def test_privacy_filter_is_internal_only(self) -> None:
        self.build_episode()

        result = self.episodes_query(
            access=self.access(privacy=frozenset({"public-example"}))
        )

        self.assertEqual("empty", result.status)
        self.assertEqual("partial", result.material_status)
        self.assertEqual({"not_authorized": 1}, self.internal_omissions(result))
        body = self.body_dict(result)
        self.assertEqual([], body["omissions"])
        self.assert_no_content(result.to_body(), "not_authorized", "private")

    def test_deleted_source_event_produces_no_item_or_body(self) -> None:
        receipt, _episode = self.build_episode()
        self.delete_event(receipt.event_id)

        result = self.episodes_query(query="ObservationRecorded")

        self.assertEqual("empty", result.status)
        self.assertEqual((), result.items)
        self.assertEqual((), result.source_refs)
        self.assertEqual({"redacted": 1}, self.internal_omissions(result))
        body = self.body_dict(result)
        self.assertEqual([], body["omissions"])
        self.assert_no_content(result.to_body(), "redacted", SECRET, receipt.event_id)

    def test_empty_result_does_not_claim_non_existence(self) -> None:
        result = self.episodes_query(query="word-that-matches-no-metadata")

        self.assertEqual("empty", result.status)
        self.assertEqual((), result.items)
        self.assertFalse(result.truncated)
        self.assertEqual(self.revision(), result.state_revision)
        self.assert_no_content(result.to_body(), "not_found", "exists")

    def test_structural_errors_are_invalid_input(self) -> None:
        access = self.access()
        bad_queries = (
            EpisodeQuery(query=""),
            EpisodeQuery(query="   "),
            EpisodeQuery(query="x" * 1001),
            EpisodeQuery(query=123),  # type: ignore[arg-type]
            EpisodeQuery(query="ok", limit=0),
            EpisodeQuery(query="ok", limit=21),
            EpisodeQuery(query="ok", limit=True),
            EpisodeQuery(query="ok", purpose="consult"),
            EpisodeQuery(query="ok", scope={"unknown": "field"}),
            EpisodeQuery(query="ok", scope=None),
        )
        for query in bad_queries:
            with self.subTest(query=query), self.assertRaises(InvalidInput):
                self.queries.query_episodes(access, query)


# --------------------------------------------------------------------------
# D. authorization
# --------------------------------------------------------------------------


class QueryAuthorizationTests(QueryTestCase):
    def test_belief_queries_require_consult_without_any_read(self) -> None:
        version = self.propose_supported()
        access = self.access(purposes=("reasoning",))
        calls: list[str] = []

        original_revision = self.store.current_state_revision
        original_kinds = self.store.current_versions_of_kind
        original_get = self.store.get_version

        def revision(namespace):
            calls.append("current_state_revision")
            return original_revision(namespace)

        def kinds(namespace, kind):
            calls.append("current_versions_of_kind")
            return original_kinds(namespace, kind)

        def get(namespace, record_id, record_version):
            calls.append("get_version")
            return original_get(namespace, record_id, record_version)

        self.store.current_state_revision = revision
        self.store.current_versions_of_kind = kinds
        self.store.get_version = get
        self.addCleanup(
            setattr, self.store, "current_state_revision", original_revision
        )
        self.addCleanup(
            setattr, self.store, "current_versions_of_kind", original_kinds
        )
        self.addCleanup(setattr, self.store, "get_version", original_get)

        with self.assertRaises(AuthorizationRequired):
            self.queries.query_current_beliefs(access, BeliefCurrentQuery())
        with self.assertRaises(AuthorizationRequired):
            self.queries.query_exact_belief(
                access,
                self.exact_ref(self.ref_for(version), scope={"scene": "test.scene"}),
            )
        self.assertEqual([], calls)

    def test_episode_queries_require_consult_and_the_requested_purpose(self) -> None:
        no_consult = self.access(purposes=("reasoning",))
        no_history = self.access(purposes=("consult",))
        allowed = self.access(purposes=("consult", "history"))

        with self.assertRaises(AuthorizationRequired):
            self.episodes_query(access=no_consult, purpose="reasoning")
        with self.assertRaises(AuthorizationRequired):
            self.episodes_query(access=no_history, purpose="history")
        with self.assertRaises(AuthorizationRequired):
            self.episodes_query(access=no_history, purpose="reasoning")

        result = self.episodes_query(access=allowed, purpose="history")
        self.assertEqual("empty", result.status)


# --------------------------------------------------------------------------
# E. disclosure boundary
# --------------------------------------------------------------------------


class DisclosureBoundaryTests(QueryTestCase):
    def _scenario(self, hidden: int) -> tuple[CognitionStore, Any]:
        directory = self.tmp_path / f"restricted-{hidden}"
        directory.mkdir()
        store = CognitionStore.open(directory / "scenario.sqlite")
        self.addCleanup(store.close)
        # No filler writes: the only difference between the three stores is the
        # number of restricted Beliefs, so their state revisions differ
        # naturally (1, 2 and 4).
        for index in range(hidden):
            _append_belief(
                store,
                record_id=HIDDEN_BELIEF_IDS[index],
                claim=f"restricted-hidden-{index}",
                claim_type="capability",
                status="supported",
                privacy_class="restricted",
            )
        _append_belief(
            store,
            record_id=VISIBLE_BELIEF_ID,
            claim="capability:visible",
            claim_type="capability",
            status="supported",
            privacy_class="private",
        )
        access = QueryAccess(
            namespace=NS,
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
        )
        result = CognitionQueryService(store).query_current_beliefs(
            access, BeliefCurrentQuery(claim_types=("capability",), limit=20)
        )
        return store, result

    def test_restricted_0_1_n_records_do_not_change_the_model_body(self) -> None:
        bodies: list[str] = []
        artifact_bodies: list[str] = []
        revisions: list[int] = []
        internal: list[dict[str, int]] = []
        parsed_bodies: list[dict[str, Any]] = []
        for hidden in (0, 1, MAX_HIDDEN):
            _store, result = self._scenario(hidden)
            self.assertEqual("ok", result.status)
            self.assertEqual(1, len(result.items))
            self.assertEqual("capability:visible", result.items[0].claim)
            self.assertEqual("partial", result.material_status)
            self.assertEqual(1 + hidden, result.state_revision)
            body = result.to_body()
            artifact = result.to_artifact_body()
            bodies.append(body)
            artifact_bodies.append(artifact)
            revisions.append(result.state_revision)
            internal.append(self.internal_omissions(result))
            parsed = json.loads(body)
            parsed_bodies.append(parsed)
            self.assertNotIn("state_revision", parsed)
            self.assertEqual(
                result.state_revision, json.loads(artifact)["state_revision"]
            )
            self.assertEqual(set(json.loads(artifact)), set(parsed) | {"state_revision"})
            self.assert_no_content(body, "not_authorized", "restricted-hidden")
            for index in range(MAX_HIDDEN):
                self.assertNotIn(HIDDEN_BELIEF_IDS[index], body)

        # The revisions are naturally different: no filler write aligned them.
        self.assertEqual([1, 2, 4], revisions)
        self.assertEqual(3, len(set(revisions)))
        # The model-visible body is byte-identical; the internal artifact body
        # differs only because it keeps the (different) revision.
        self.assertEqual(1, len(set(bodies)))
        self.assertEqual(3, len(set(artifact_bodies)))
        self.assertNotIn("state_revision", bodies[0])
        self.assertEqual(
            [0, 1, MAX_HIDDEN],
            [counts.get("not_authorized", 0) for counts in internal],
        )
        self.assertEqual([], parsed_bodies[0]["omissions"])
        # Same visible items, public omissions, material status and refs.
        for parsed in parsed_bodies:
            self.assertEqual([], parsed["omissions"])
            self.assertEqual("partial", parsed["material_status"])
            self.assertEqual(1, len(parsed["items"]))
            self.assertEqual(1, len(parsed["source_refs"]))
        self.assertEqual(
            1,
            len({json.dumps(parsed["items"], sort_keys=True) for parsed in parsed_bodies}),
        )
        self.assertEqual(
            1,
            len(
                {
                    json.dumps(parsed["source_refs"], sort_keys=True)
                    for parsed in parsed_bodies
                }
            ),
        )

    def test_restricted_source_event_ref_never_reaches_the_projection(self) -> None:
        receipt, episode = self.build_episode(privacy_class="restricted")

        result = self.episodes_query(
            access=self.access(privacy=frozenset({"private"}))
        )

        self.assertEqual("ok", result.status)
        item = result.items[0]
        self.assertEqual(self.ref_for(episode), item.ref)
        self.assertEqual((), item.source_refs)
        self.assertEqual((), result.source_refs)
        self.assertEqual(
            {"source_ref_not_authorized": 1}, self.internal_omissions(result)
        )
        self.assertEqual("partial", result.material_status)
        self.assert_no_content(result.to_body(), receipt.event_id, "restricted")

        # A fully authorized source is still filtered only by policy, not by
        # the data: the same partial material status applies.
        private_receipt, _private_episode = self.build_episode()
        private_result = self.episodes_query(
            access=self.access(privacy=frozenset({"private"}))
        )
        self.assertEqual("partial", private_result.material_status)
        self.assertIn(
            EventRef(namespace=NS, event_id=private_receipt.event_id),
            private_result.source_refs,
        )

    def test_deleted_source_event_ref_is_filtered_even_if_search_returns_it(self) -> None:
        _receipt, episode = self.build_episode()
        deleted_receipt, _deleted_episode = self.build_episode(
            event_type="DeletedObservation", privacy_class="restricted"
        )
        deleted_event_id = deleted_receipt.event_id
        self.delete_event(deleted_event_id)
        revision = self.revision()

        def fake_search(store, **kwargs):
            return SearchResult(
                hits=(
                    EpisodeHit(
                        ref=self.ref_for(episode),
                        kind="episode",
                        epistemic_status="observation",
                        scope={},
                        excerpt=None,
                        rank_score=1,
                        source_refs=(
                            EventRef(namespace=NS, event_id=deleted_event_id),
                        ),
                        observed_at=OBSERVED_AT,
                    ),
                ),
                truncated=False,
                omissions=(),
                state_revision=revision,
                limit=5,
                purpose="reasoning",
                retrieval_policy_version="pcs2-007.metadata.v1",
            )

        with patch.object(query_module, "search_episodes", side_effect=fake_search):
            result = self.episodes_query(query="DeletedObservation")

        self.assertEqual("ok", result.status)
        self.assertEqual((), result.items[0].source_refs)
        self.assertEqual((), result.source_refs)
        self.assertEqual(
            {"source_ref_redacted": 1}, self.internal_omissions(result)
        )
        self.assert_no_content(result.to_body(), deleted_event_id, "redacted")

    def test_unavailable_bodies_have_no_existence_fields(self) -> None:
        missing = self.queries.query_exact_belief(
            self.access(),
            self.exact_ref(
                RecordRef(namespace=NS, record_id=str(uuid.uuid4()), record_version=1)
            ),
        )
        restricted = _append_belief(
            self.store,
            record_id=str(uuid.uuid4()),
            claim="restricted-claim",
            claim_type="capability",
            status="supported",
            privacy_class="restricted",
        )
        unauthorized = self.queries.query_exact_belief(
            self.access(privacy=frozenset({"private"})),
            self.exact_ref(self.ref_for(restricted)),
        )
        for body in (missing.to_body(), unauthorized.to_body()):
            self.assert_no_content(body, "not_found", "exists")
            self.assertNotIn("unavailable", json.loads(body)["omissions"])

    def test_material_status_is_a_policy_value_only(self) -> None:
        empty_complete = self.current(
            access=self.access(privacy=ALL_PRIVACY), query=BeliefCurrentQuery()
        )
        empty_partial = self.current(
            access=self.access(privacy=frozenset({"private"})),
            query=BeliefCurrentQuery(),
        )
        # NS_B has never contained a record, restricted or otherwise.
        no_records_complete = self.queries.query_current_beliefs(
            self.access(namespace=NS_B), BeliefCurrentQuery()
        )
        no_records_partial = self.queries.query_current_beliefs(
            self.access(namespace=NS_B, privacy=frozenset({"private"})),
            BeliefCurrentQuery(),
        )

        self.assertEqual("complete", empty_complete.material_status)
        self.assertEqual("partial", empty_partial.material_status)
        self.assertEqual("complete", no_records_complete.material_status)
        self.assertEqual("partial", no_records_partial.material_status)
        self.assertEqual("empty", no_records_partial.status)

        # An exact ref that cannot be used is unavailable regardless of policy.
        unavailable = self.queries.query_exact_belief(
            self.access(namespace=NS_B),
            self.exact_ref(
                RecordRef(namespace=NS_B, record_id=str(uuid.uuid4()), record_version=1)
            ),
        )
        self.assertEqual("unavailable", unavailable.material_status)


# --------------------------------------------------------------------------
# F. snapshot, revision and clock
# --------------------------------------------------------------------------


class QuerySnapshotTests(QueryTestCase):
    def test_every_read_runs_inside_the_same_read_snapshot(self) -> None:
        version = self.propose_supported()
        receipt, episode = self.build_episode()
        revision = self.revision()
        calls: list[tuple[str, bool]] = []

        names = (
            "current_state_revision",
            "get_version",
            "record_pointer",
            "record_is_deleted",
            "current_versions_of_kind",
            "event_is_deleted",
            "event_header",
        )
        for name in names:
            original = getattr(self.store, name)

            def wrapper(*args, __name=name, __original=original, **kwargs):
                calls.append((__name, self.store.connection.in_transaction))
                return __original(*args, **kwargs)

            setattr(self.store, name, wrapper)
            self.addCleanup(setattr, self.store, name, original)

        original_search = query_module.search_episodes

        def search_wrapper(*args, **kwargs):
            calls.append(("search_episodes", self.store.connection.in_transaction))
            return original_search(*args, **kwargs)

        query_module.search_episodes = search_wrapper
        self.addCleanup(
            setattr, query_module, "search_episodes", original_search
        )

        exact = self.exact(version, scope={"scene": "test.scene"})
        current = self.current(
            query=BeliefCurrentQuery(scope={"scene": "test.scene"}, limit=20)
        )
        episodes = self.episodes_query(query="ObservationRecorded")

        self.assertEqual("ok", exact.status)
        self.assertEqual("ok", current.status)
        self.assertEqual("ok", episodes.status)
        self.assertEqual(revision, exact.state_revision)
        self.assertEqual(revision, current.state_revision)
        self.assertEqual(revision, episodes.state_revision)
        self.assertTrue(calls)
        self.assertTrue(all(inside for _name, inside in calls))
        self.assertEqual(set(names) | {"search_episodes"}, {name for name, _ in calls})
        self.assertEqual([self.ref_for(version)], [item.ref for item in current.items])
        self.assertEqual(
            (EventRef(namespace=NS, event_id=receipt.event_id),),
            episodes.source_refs,
        )
        self.assertEqual([self.ref_for(episode)], [item.ref for item in episodes.items])

    def test_clock_is_sampled_once_for_all_expiry_checks(self) -> None:
        first = self.propose_supported(
            claim="environment_state:first-window",
            claim_type="environment_state",
            scope={"scene": "test.scene"},
            valid_until="2026-10-17T12:30:00Z",
        )
        second = self.propose_supported(
            claim="environment_state:second-window",
            claim_type="environment_state",
            scope={"scene": "test.scene"},
            valid_until="2026-10-17T12:15:00Z",
        )
        calls: list[datetime] = []

        def clock() -> datetime:
            value = NOW + timedelta(hours=len(calls))
            calls.append(value)
            return value

        service = CognitionQueryService(self.store, clock=clock)
        result = service.query_current_beliefs(
            self.access(),
            BeliefCurrentQuery(scope={"scene": "test.scene"}, limit=20),
        )

        self.assertEqual(1, len(calls))
        self.assertEqual(2, len(result.items))
        self.assertEqual(
            {self.ref_for(first), self.ref_for(second)},
            {item.ref for item in result.items},
        )
        # Both deadlines (12:15 and 12:30) are after the first ``now`` (12:00)
        # but before a second clock call (13:00).  A per-item clock would mark
        # the later-evaluated item expired; a single fixed ``now`` does not.
        self.assertTrue(all(not item.expired for item in result.items))
        self.assertTrue(all(item.applicability == "current" for item in result.items))

    def test_episode_search_revision_mismatch_is_corruption(self) -> None:
        revision = self.revision()

        def fake_search(store, **kwargs):
            return SearchResult(
                hits=(),
                truncated=False,
                omissions=(),
                state_revision=revision + 1,
                limit=5,
                purpose="reasoning",
                retrieval_policy_version="pcs2-007.metadata.v1",
            )

        with patch.object(query_module, "search_episodes", side_effect=fake_search):
            with self.assertRaises(CorruptStore):
                self.episodes_query()

        self.assertEqual(revision, self.revision())


# --------------------------------------------------------------------------
# G. read-only and canonical projection
# --------------------------------------------------------------------------


class QueryReadOnlyAndProjectionTests(QueryTestCase):
    def test_queries_do_not_write_any_cognition_or_collaboration_rows(self) -> None:
        version = self.propose_supported()
        receipt, _episode = self.build_episode()
        tables = (
            "record_versions",
            "event_journal",
            "processed_commands",
            "pending_work",
            "collab_interactions",
        )
        before = {table: self.count_rows(table) for table in tables}
        revision_before = self.revision()
        self.assertIsNotNone(self.store.unfinished_work_for_event(NS, receipt.event_id))

        self.assertEqual("ok", self.exact(version, scope={"scene": "test.scene"}).status)
        self.assertEqual(
            "ok",
            self.current(
                query=BeliefCurrentQuery(scope={"scene": "test.scene"}, limit=20)
            ).status,
        )
        self.assertEqual("ok", self.episodes_query(query="ObservationRecorded").status)

        self.assertEqual(before, {table: self.count_rows(table) for table in tables})
        self.assertEqual(revision_before, self.revision())
        self.assertIsNotNone(self.store.unfinished_work_for_event(NS, receipt.event_id))
        self.assertFalse(self.store.connection.in_transaction)

    def test_source_refs_are_exact_deduplicated_and_the_body_is_canonical(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read").event_id
        first = self.propose_supported(claim="capability:a", event_id=event)
        second = self.propose_supported(claim="capability:b", event_id=event)

        result = self.current(
            query=BeliefCurrentQuery(scope={"scene": "test.scene"}, limit=20)
        )
        item_refs = tuple(item.ref for item in result.items)
        expected = tuple(sorted(item_refs, key=lambda ref: ref.record_id))

        self.assertEqual(expected, result.source_refs)
        self.assertEqual(len(result.source_refs), len(set(result.source_refs)))
        self.assertEqual({self.ref_for(first), self.ref_for(second)}, set(result.source_refs))
        for item in result.items:
            self.assertEqual((item.ref,), item.source_refs)

        body_text = result.to_body()
        body = json.loads(body_text)
        self.assertEqual(
            {
                "query_kind",
                "status",
                "material_status",
                "items",
                "omissions",
                "truncated",
                "query_policy_version",
                "source_refs",
            },
            set(body),
        )
        self.assertEqual(canonical_json_text(body), body_text)
        self.assertEqual(
            [ref.to_dict() for ref in result.source_refs], body["source_refs"]
        )
        self.assertEqual(
            [item.ref.to_dict() for item in result.items],
            [item["ref"] for item in body["items"]],
        )

    def test_model_body_excludes_revision_and_artifact_body_keeps_it(self) -> None:
        version = self.propose_supported()
        result = self.current(
            query=BeliefCurrentQuery(scope={"scene": "test.scene"}, limit=20)
        )
        self.assertEqual([self.ref_for(version)], [item.ref for item in result.items])

        body_text = result.to_body()
        artifact_text = result.to_artifact_body()
        body = json.loads(body_text)
        artifact = json.loads(artifact_text)

        # The model-visible projection never carries the global revision.
        self.assertNotIn("state_revision", body)
        self.assertNotIn("state_revision", body_text)
        # The internal artifact projection carries it for C11 persistence.
        self.assertEqual(result.state_revision, artifact["state_revision"])
        self.assertEqual(set(artifact), set(body) | {"state_revision"})
        for key in (
            "query_kind",
            "status",
            "material_status",
            "items",
            "omissions",
            "truncated",
            "query_policy_version",
            "source_refs",
        ):
            self.assertEqual(body[key], artifact[key])
        self.assertEqual(canonical_json_text(body), body_text)
        self.assertEqual(canonical_json_text(artifact), artifact_text)

    def test_query_access_is_validated_at_construction(self) -> None:
        with self.assertRaises(InvalidInput):
            QueryAccess(
                namespace="Bad Namespace",
                privacy_classes=ALL_PRIVACY,
                purposes=frozenset({"consult"}),
            )
        with self.assertRaises(InvalidInput):
            QueryAccess(
                namespace=NS,
                privacy_classes={"private"},  # type: ignore[arg-type]
                purposes=frozenset({"consult"}),
            )
        with self.assertRaises(InvalidInput):
            QueryAccess(
                namespace=NS,
                privacy_classes=frozenset({"secret"}),
                purposes=frozenset({"consult"}),
            )
        with self.assertRaises(InvalidInput):
            QueryAccess(
                namespace=NS,
                privacy_classes=ALL_PRIVACY,
                purposes={"consult"},  # type: ignore[arg-type]
            )
        with self.assertRaises(InvalidInput):
            QueryAccess(
                namespace=NS,
                privacy_classes=ALL_PRIVACY,
                purposes=frozenset({""}),
            )
        with self.assertRaises(InvalidInput):
            QueryAccess(
                namespace=NS,
                privacy_classes=ALL_PRIVACY,
                purposes=frozenset({3}),  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
