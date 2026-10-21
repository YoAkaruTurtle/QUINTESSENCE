"""PCS2-007 C group: current-version Episode retrieval.

The assertions cover the four hard edges of the card: current pointer only,
explicit rejection of historical retrieval, J.2 scope applicability, and the
deletion barrier (including a whole-database text scan).
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from domain_support import (  # noqa: E402
    NS,
    NS_B,
    OBSERVED_AT,
    RECORDED_AT,
    DomainTestCase,
    all_text_cells,
)

from quintessence_pcs.domains import (  # noqa: E402
    EventRef,
    HistoricalSearchUnsupported,
    SearchResult,
    search_episodes,
)
from quintessence_pcs.domains.memory.search import (  # noqa: E402
    EPISTEMIC_STATUS_BY_SOURCE_KIND,
    RETRIEVAL_POLICY_VERSION,
)
from quintessence_pcs.foundation.errors import (  # noqa: E402
    ScopeViolation,
    InvalidInput,
    Redacted,
)

SECRET = "retrieval-secret-that-must-not-leak"


class SearchTestCase(DomainTestCase):
    def make(
        self,
        *,
        source_kind: str = "host_observation",
        event_type: str = "ObservationRecorded",
        observed_at: str | None = OBSERVED_AT,
        recorded_at: str = RECORDED_AT,
        scope: dict | None = None,
        payload: object = None,
        namespace: str = NS,
    ):
        receipt = self.ingest(
            namespace=namespace,
            source_kind=source_kind,
            event_type=event_type,
            observed_at=observed_at,
            recorded_at=recorded_at,
            payload=payload,
        )
        episode = self.build_episode(
            receipt.event_id, namespace=namespace, scope=scope
        )
        return receipt, episode


class MetadataSearchTests(SearchTestCase):
    def test_matches_structured_metadata_and_not_event_body_text(self) -> None:
        receipt, episode = self.make(payload={"text": "zebra-unique-word"})

        # Blind spot made explicit: words only in the source payload are not
        # searchable because the Episode never copies them.
        self.assertEqual((), self.search(query="zebra").hits)
        self.assertEqual((), self.search(query="zebra-unique-word").hits)

        for query in (
            "observationrecorded",
            "HOST_OBSERVATION",
            "observation",
            "2026-10-17",
            "complete",
        ):
            with self.subTest(query=query):
                result = self.search(query=query)
                self.assertEqual(1, len(result.hits))
                self.assertEqual(episode.record_id, result.hits[0].ref.record_id)

    def test_epistemic_status_is_derived_from_the_source_kind(self) -> None:
        kinds = [
            "host_observation",
            "user_report",
            "model_inference",
            "synthetic_fixture",
            "imported_memory",
            "system_internal",
        ]
        by_event: dict[str, str] = {}
        for kind in kinds:
            receipt, _episode = self.make(source_kind=kind, event_type="SharedType")
            by_event[receipt.event_id] = kind

        result = self.search(query="sharedtype", limit=20)
        self.assertEqual(len(kinds), len(result.hits))
        statuses = set()
        for hit in result.hits:
            source_kind = by_event[hit.source_refs[0].event_id]
            self.assertEqual(
                EPISTEMIC_STATUS_BY_SOURCE_KIND[source_kind], hit.epistemic_status
            )
            statuses.add(hit.epistemic_status)
        self.assertNotEqual({"observation"}, statuses)
        self.assertEqual(
            {"observation", "report", "inference", "synthetic", "imported", "system"},
            statuses,
        )

    def test_hit_contains_a_complete_typed_reference(self) -> None:
        receipt, episode = self.make(scope={"session_id": "session-1"})
        result = self.search(query="observationrecorded", scope={"session_id": "session-1"})
        self.assertEqual(1, len(result.hits))
        hit = result.hits[0]

        self.assertEqual(
            {
                "ref_type": "record",
                "namespace": NS,
                "record_id": episode.record_id,
                "record_version": 1,
            },
            hit.ref.to_dict(),
        )
        self.assertEqual("episode", hit.kind)
        self.assertEqual("observation", hit.epistemic_status)
        self.assertEqual({"session_id": "session-1"}, hit.scope)
        self.assertIsNone(hit.excerpt)
        self.assertEqual(type(1), type(hit.rank_score))
        self.assertGreaterEqual(hit.rank_score, 1)
        self.assertEqual(
            (EventRef(namespace=NS, event_id=receipt.event_id),), hit.source_refs
        )
        self.assertEqual(OBSERVED_AT, hit.observed_at)

        self.assertIsInstance(result, SearchResult)
        self.assertEqual(self.revision(), result.state_revision)
        self.assertEqual(5, result.limit)
        self.assertEqual("reasoning", result.purpose)
        self.assertEqual(RETRIEVAL_POLICY_VERSION, result.retrieval_policy_version)
        self.assertFalse(result.truncated)
        self.assertEqual((), result.omissions)

    def test_all_tokens_must_match(self) -> None:
        self.make(event_type="ObservationRecorded")

        self.assertEqual(1, len(self.search(query="observationrecorded 2026-10-17").hits))
        self.assertEqual((), self.search(query="observationrecorded nomatch").hits)
        # Duplicate tokens are folded: they neither inflate the rank nor
        # change the AND semantics.
        single = self.search(query="observationrecorded").hits[0]
        duplicated = self.search(query="observationrecorded observationrecorded").hits[0]
        self.assertEqual(single.rank_score, duplicated.rank_score)

    def test_unicode_substring_matching(self) -> None:
        self.make(event_type="用户反馈记录")
        self.assertEqual(1, len(self.search(query="用户").hits))
        self.assertEqual(1, len(self.search(query="反馈").hits))
        self.assertEqual((), self.search(query="不存在").hits)

    def test_limit_defaults_bounds_and_truncation(self) -> None:
        for _index in range(6):
            self.make(event_type="BulkEvent")

        default = self.search(query="bulkevent")
        self.assertEqual(5, len(default.hits))
        self.assertTrue(default.truncated)

        one = self.search(query="bulkevent", limit=1)
        self.assertEqual(1, len(one.hits))
        self.assertTrue(one.truncated)

        six = self.search(query="bulkevent", limit=6)
        self.assertEqual(6, len(six.hits))
        self.assertFalse(six.truncated)

        self.assertEqual(6, len(self.search(query="bulkevent", limit=20).hits))

        for bad in (0, 21, -1, True, 1.5, "5", None):
            with self.subTest(limit=bad):
                with self.assertRaises(InvalidInput):
                    self.search(query="bulkevent", limit=bad)

    def test_purpose_boundaries(self) -> None:
        self.make()
        self.assertEqual(1, len(self.search(query="observationrecorded", purpose="reasoning").hits))
        self.assertEqual(1, len(self.search(query="observationrecorded", purpose="history").hits))
        for bad in ("training", "", None, 1):
            with self.subTest(purpose=bad):
                with self.assertRaises(InvalidInput):
                    self.search(query="observationrecorded", purpose=bad)

    def test_query_boundaries(self) -> None:
        self.make()
        for bad in ("", "   ", "x" * 1001, None, 5):
            with self.subTest(query=bad):
                with self.assertRaises(InvalidInput):
                    self.search(query=bad)

    def test_very_long_but_valid_query_is_not_a_parser_error(self) -> None:
        self.make()
        # 1000 characters is allowed; it simply matches nothing here.
        result = self.search(query="q" * 1000)
        self.assertEqual((), result.hits)

    def test_search_result_carries_current_revision(self) -> None:
        self.make()
        self.make(event_type="SecondType")
        result = self.search(query="observationrecorded")
        self.assertEqual(2, result.state_revision)
        self.assertEqual(2, self.revision())


class CurrentVersionOnlyTests(SearchTestCase):
    def test_old_versions_do_not_participate(self) -> None:
        receipt, first = self.make(observed_at=OBSERVED_AT)
        self.assertEqual("complete", first.body["source_completeness"])

        second_body = dict(first.body)
        second_body["source_completeness"] = "missing"
        second = self.store.append_record_version(
            namespace=NS,
            record_id=first.record_id,
            record_version=2,
            kind="episode",
            body=second_body,
            status="active",
            schema_version="1",
            scope=first.scope,
        )
        self.assertEqual(2, second.record_version)

        # v1 said "complete"; only the current version is searched, so that
        # word must no longer match.
        self.assertEqual((), self.search(query="complete").hits)
        result = self.search(query="missing")
        self.assertEqual(1, len(result.hits))
        self.assertEqual(2, result.hits[0].ref.record_version)
        self.assertEqual(first.record_id, result.hits[0].ref.record_id)

    def test_inactive_status_is_not_searchable(self) -> None:
        self.make(event_type="ActiveType")
        self.store.append_record_version(
            namespace=NS,
            record_id=str(uuid.uuid4()),
            record_version=1,
            kind="episode",
            body={"note": "candidate body is not parsed by retrieval"},
            status="candidate",
            schema_version="1",
        )

        result = self.search(query="activetype")
        self.assertEqual(1, len(result.hits))
        self.assertEqual(
            [("status_not_active", 1)],
            [(omission.reason, omission.count) for omission in result.omissions],
        )
        self.assertEqual((), self.search(query="notparsed").hits)


class RevisionGuardTests(SearchTestCase):
    def test_current_revision_is_accepted(self) -> None:
        self.make()
        current = self.revision()
        result = self.search(query="observationrecorded", state_revision=current)
        self.assertEqual(1, len(result.hits))
        self.assertEqual(current, result.state_revision)

    def test_historical_or_future_revision_is_rejected(self) -> None:
        self.make()
        current = self.revision()
        self.assertGreater(current, 0)
        for revision in (0, current - 1, current + 1, 9999):
            with self.subTest(revision=revision):
                with self.assertRaises(HistoricalSearchUnsupported) as caught:
                    self.search(query="observationrecorded", state_revision=revision)
                self.assertEqual("HISTORICAL_SEARCH_UNSUPPORTED", caught.exception.code)

    def test_revision_guard_also_covers_a_deletion_watermark(self) -> None:
        self.make()
        stale_revision = self.revision()
        receipt = self.ingest(event_type="Unrelated")
        self.delete_event(receipt.event_id)
        self.assertNotEqual(stale_revision, self.revision())

        with self.assertRaises(HistoricalSearchUnsupported):
            self.search(query="observationrecorded", state_revision=stale_revision)
        self.assertEqual(
            1,
            len(self.search(query="observationrecorded", state_revision=self.revision()).hits),
        )

    def test_invalid_revision_types_are_schema_errors(self) -> None:
        for bad in (-1, True, "1", 1.5):
            with self.subTest(revision=bad):
                with self.assertRaises(InvalidInput):
                    self.search(query="observationrecorded", state_revision=bad)


class ScopeFilteringTests(SearchTestCase):
    def test_scope_rule_matrix(self) -> None:
        self.make(event_type="GeneralEvent", scope={})
        self.make(
            event_type="ScopedEvent",
            scope={"session_id": "session-1", "entity_ids": ["entity-a", "entity-b"]},
        )
        self.make(
            event_type="MultiScopeEvent",
            scope={"scene": "kitchen", "goal_id": "goal-1"},
        )

        cases = (
            ("GeneralEvent", {}, 1),
            ("GeneralEvent", {"session_id": "any"}, 1),
            ("ScopedEvent", {}, 0),
            ("ScopedEvent", {"session_id": "session-1"}, 0),
            ("ScopedEvent", {"session_id": "session-2", "entity_ids": ["entity-a", "entity-b"]}, 0),
            ("ScopedEvent", {"session_id": "session-1", "entity_ids": ["entity-a"]}, 0),
            (
                "ScopedEvent",
                {"session_id": "session-1", "entity_ids": ["entity-a", "entity-b", "entity-c"]},
                1,
            ),
            (
                "ScopedEvent",
                {"session_id": "session-1", "entity_ids": ["entity-b", "entity-a"]},
                1,
            ),
            ("MultiScopeEvent", {"scene": "kitchen"}, 0),
            ("MultiScopeEvent", {"scene": "kitchen", "goal_id": "goal-1"}, 1),
            ("MultiScopeEvent", {"scene": "kitchen", "goal_id": "other"}, 0),
        )
        for query, scope, expected in cases:
            with self.subTest(query=query, scope=scope):
                self.assertEqual(expected, len(self.search(query=query, scope=scope).hits))

    def test_extra_query_fields_do_not_narrow_a_general_record(self) -> None:
        self.make(event_type="GeneralEvent", scope={})
        result = self.search(
            query=" generalevent ",
            scope={"project_id": "p", "session_id": "s", "scene": "x", "goal_id": "g"},
        )
        self.assertEqual(1, len(result.hits))

    def test_empty_entity_ids_is_a_present_constraint(self) -> None:
        self.make(event_type="EmptyEntities", scope={"entity_ids": []})
        self.assertEqual((), self.search(query="emptyentities").hits)
        self.assertEqual(
            1,
            len(self.search(query="emptyentities", scope={"entity_ids": []}).hits),
        )
        self.assertEqual(
            1,
            len(self.search(query="emptyentities", scope={"entity_ids": ["entity-x"]}).hits),
        )

    def test_scope_validation(self) -> None:
        self.make()
        for bad in (
            "session-1",
            {"unknown_field": "x"},
            {"session_id": 5},
            {"session_id": ""},
            {"entity_ids": "entity-a"},
            {"entity_ids": ["entity-a", "entity-a"]},
        ):
            with self.subTest(scope=bad):
                with self.assertRaises(ScopeViolation):
                    self.search(query="observationrecorded", scope=bad)
        with self.assertRaises(ScopeViolation):
            self.episodes.search_episodes(
                namespace=NS, query="observationrecorded", scope=None  # type: ignore[arg-type]
            )


class IsolationAndDeletionTests(SearchTestCase):
    def test_namespace_isolation(self) -> None:
        _receipt, episode = self.make(event_type="IsoEvent", namespace=NS)
        _receipt_b, episode_b = self.make(event_type="IsoEvent", namespace=NS_B)

        result = self.search(query="isoevent")
        self.assertEqual(1, len(result.hits))
        self.assertEqual(episode.record_id, result.hits[0].ref.record_id)

        result_b = self.search(query="isoevent", namespace=NS_B)
        self.assertEqual(1, len(result_b.hits))
        self.assertEqual(episode_b.record_id, result_b.hits[0].ref.record_id)

    def test_profiles_are_not_episode_hits(self) -> None:
        self.publish()
        result = self.search(query="synthetic")
        self.assertEqual((), result.hits)
        # Profiles are a different kind, so they are not even an omission.
        self.assertEqual((), result.omissions)

    def test_deleted_episode_vanishes_without_a_placeholder(self) -> None:
        _receipt, first = self.make(event_type="SameType")
        _receipt_b, second = self.make(event_type="SameType")
        self.delete_record(first.record_id)
        self.store.cleanup_deletions()

        result = self.search(query="sametype")
        self.assertEqual(1, len(result.hits))
        self.assertEqual(second.record_id, result.hits[0].ref.record_id)
        self.assertNotIn(
            first.record_id, {hit.ref.record_id for hit in result.hits}
        )
        # No REDACTED placeholder and no hit that names the deleted record.
        self.assertNotIn(first.record_id, repr(result))
        with self.assertRaises(Redacted):
            self.store.get_version(NS, first.record_id, 1)

    def test_episode_with_every_source_deleted_does_not_appear(self) -> None:
        receipt, episode = self.make(payload={"text": SECRET})
        digest = self.store.connection.execute(
            "SELECT sha256 FROM private_payloads WHERE payload_ref = "
            "(SELECT payload_ref FROM event_journal WHERE event_id = ?)",
            (receipt.event_id,),
        ).fetchone()["sha256"]

        # Barrier only: the payload body is still on disk, the result must not
        # contain the Episode or any of its text.
        self.delete_event(receipt.event_id)
        barred = self.search(query="observationrecorded")
        self.assertEqual((), barred.hits)
        self.assertNotIn(SECRET, repr(barred))
        self.assertEqual(
            [("redacted", 1)],
            [(omission.reason, omission.count) for omission in barred.omissions],
        )

        self.store.cleanup_deletions()
        cleaned = self.search(query="observationrecorded")
        self.assertEqual((), cleaned.hits)
        self.assertNotIn(SECRET, repr(cleaned))

        hits = [
            (location, value)
            for location, value in all_text_cells(self.store.connection)
            if SECRET in value or digest in value
        ]
        self.assertEqual([], hits)
        self.assertEqual((), cleaned.hits)
        self.assertEqual(episode.record_id, self.store.get_version(NS, episode.record_id, 1).record_id)

    def test_one_deleted_source_of_a_single_source_episode_hides_it(self) -> None:
        receipt, _episode = self.make()
        self.delete_event(receipt.event_id)
        result = self.search(query="observationrecorded")
        self.assertEqual((), result.hits)
        self.assertEqual(("redacted", 1), (result.omissions[0].reason, result.omissions[0].count))

    def test_deletion_state_does_not_unhide_a_deleted_episode_after_cleanup(self) -> None:
        _receipt, episode = self.make()
        self.delete_record(episode.record_id)
        result_barrier = self.search(query="observationrecorded")
        self.assertEqual((), result_barrier.hits)
        self.store.cleanup_deletions()
        result_clean = self.search(query="observationrecorded")
        self.assertEqual((), result_clean.hits)


class OrderingTests(SearchTestCase):
    def test_ties_break_on_record_id_and_are_stable(self) -> None:
        receipts = []
        episodes = []
        for _index in range(3):
            receipt, episode = self.make(event_type="TieEvent")
            receipts.append(receipt)
            episodes.append(episode)

        first = self.search(query="tieevent")
        expected = sorted(episode.record_id for episode in episodes)
        self.assertEqual(expected, [hit.ref.record_id for hit in first.hits])
        second = self.search(query="tieevent")
        self.assertEqual(first.hits, second.hits)

    def test_higher_token_match_ranks_first(self) -> None:
        # Both episodes match both tokens; the observation one matches the
        # second token in two fields (source_kind and epistemic status).
        self.make(event_type="AlphaEvent", source_kind="host_observation")
        self.make(event_type="AlphaObservation", source_kind="synthetic_fixture")
        result = self.search(query="alpha observation")
        self.assertEqual(2, len(result.hits))
        self.assertEqual("observation", result.hits[0].epistemic_status)
        self.assertGreater(result.hits[0].rank_score, result.hits[1].rank_score)

    def test_empty_namespace_returns_an_empty_result(self) -> None:
        result = self.search(query="anything")
        self.assertEqual((), result.hits)
        self.assertFalse(result.truncated)
        self.assertEqual((), result.omissions)
        self.assertEqual(0, result.state_revision)

    def test_free_function_matches_the_service(self) -> None:
        self.make()
        service_result = self.search(query="observationrecorded")
        function_result = search_episodes(
            self.store, namespace=NS, query="observationrecorded", scope={}
        )
        self.assertEqual(service_result, function_result)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
