"""PCS2-007 B group: Episode faithfulness, scope storage and idempotency.

The parallel-writer race lives in ``test_episode_concurrency.py``; here the
duplicate paths are exercised through two connections and through the barrier
interactions, so a regression in the single-writer behaviour is caught without
spawning processes.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from domain_support import (  # noqa: E402
    NS,
    OBSERVED_AT,
    RECORDED_AT,
    DomainTestCase,
)

from quintessence_pcs.domains import EpisodeService  # noqa: E402
from quintessence_pcs.domains.memory.episode_shape import (  # noqa: E402
    COMPLETENESS_COMPLETE,
    COMPLETENESS_PARTIAL,
    EPISODE_KIND,
    GENERATOR,
    GENERATOR_REF,
)
from quintessence_pcs.foundation.errors import (  # noqa: E402
    AuthorizationRequired,
    EventNotFound,
    InvalidInput,
    Redacted,
    ScopeViolation,
    StandaloneTransactionRequired,
)

SECRET = "episode-secret-that-must-never-be-copied"


class EpisodeBuildTests(DomainTestCase):
    def test_build_is_faithful_to_the_source_envelope(self) -> None:
        receipt = self.ingest(
            source_kind="user_report",
            event_type="UserStatedPreference",
            payload={"text": SECRET},
            observed_at=OBSERVED_AT,
            recorded_at=RECORDED_AT,
        )
        scope = {"session_id": "session-1", "entity_ids": ["entity-a"]}

        episode = self.build_episode(receipt.event_id, scope=scope)

        self.assertEqual(EPISODE_KIND, episode.kind)
        self.assertEqual(1, episode.record_version)
        self.assertEqual("active", episode.status)
        self.assertEqual(scope, episode.scope)
        self.assertEqual(1, self.revision())
        self.assertEqual(1, episode.committed_state_revision)

        body = episode.body
        self.assertEqual(
            [
                {
                    "ref_type": "event",
                    "namespace": NS,
                    "event_id": receipt.event_id,
                }
            ],
            body["event_refs"],
        )
        self.assertEqual([], body["observation_refs"])
        self.assertEqual([], body["outcome_refs"])
        self.assertEqual([], body["correction_refs"])
        self.assertEqual([], body["participants_or_entities"])
        self.assertEqual(OBSERVED_AT, body["started_at"])
        self.assertEqual(OBSERVED_AT, body["ended_at"])
        self.assertEqual(COMPLETENESS_COMPLETE, body["source_completeness"])
        self.assertEqual(GENERATOR_REF, body["generator_ref"])
        self.assertEqual(["reasoning", "history"], body["allowed_uses"])
        self.assertEqual(
            {
                "event_type": "UserStatedPreference",
                "source_kind": "user_report",
                "observed_at": OBSERVED_AT,
                "recorded_at": RECORDED_AT,
                "generator": GENERATOR,
            },
            body["summary"],
        )

        # The source payload text was not copied into the Episode.
        episode_payload = self.store.connection.execute(
            "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
            (episode.body_payload_ref,),
        ).fetchone()["body_json"]
        self.assertNotIn(SECRET, episode_payload)
        self.assertNotIn(SECRET, str(body))

        audit = self.audit_rows()
        self.assertEqual(1, len(audit))
        self.assertEqual("memory.episode.build", audit[0]["operation"])
        self.assertEqual(episode.record_id, audit[0]["target_id"])
        self.assertEqual(1, audit[0]["target_version"])
        self.assertEqual("auth:episode-build", audit[0]["authorization_ref"])

    def test_unknown_observed_at_is_not_invented(self) -> None:
        receipt = self.ingest(observed_at=None)
        episode = self.build_episode(receipt.event_id)

        self.assertIsNone(episode.body["started_at"])
        self.assertIsNone(episode.body["ended_at"])
        self.assertIsNone(episode.body["summary"]["observed_at"])
        self.assertEqual(COMPLETENESS_PARTIAL, episode.body["source_completeness"])
        self.assertEqual(RECORDED_AT, episode.body["summary"]["recorded_at"])

    def test_deleted_event_cannot_become_an_episode(self) -> None:
        receipt = self.ingest()
        self.delete_event(receipt.event_id)
        self.store.cleanup_deletions()
        before = self.revision()

        with self.assertRaises(Redacted) as caught:
            self.build_episode(receipt.event_id)
        self.assertEqual("REDACTED", caught.exception.code)
        self.assertEqual(before, self.revision())
        self.assertEqual(0, self.count_rows("records"))
        self.assertEqual(0, self.count_rows("episode_sources"))

    def test_barriered_event_is_redacted_before_cleanup(self) -> None:
        receipt = self.ingest()
        self.delete_event(receipt.event_id)

        with self.assertRaises(Redacted):
            self.build_episode(receipt.event_id)
        self.assertEqual(0, self.count_rows("records"))

    def test_missing_event_is_not_found(self) -> None:
        with self.assertRaises(EventNotFound) as caught:
            self.build_episode("12121212-1212-4212-8212-121212121212")
        self.assertEqual("NOT_FOUND", caught.exception.code)
        self.assertEqual(0, self.count_rows("records"))

    def test_build_requires_an_explicit_authorization(self) -> None:
        receipt = self.ingest()
        for value in ("", " ", None):
            with self.subTest(value=value):
                with self.assertRaises(AuthorizationRequired) as caught:
                    self.episodes.build_episode_from_event(
                        namespace=NS,
                        event_id=receipt.event_id,
                        scope={},
                        authorization_ref=value,  # type: ignore[arg-type]
                    )
                self.assertEqual("SCOPE_VIOLATION", caught.exception.code)
        self.assertEqual(0, self.count_rows("records"))

    def test_scope_is_validated(self) -> None:
        receipt = self.ingest()
        bad_scopes = (
            None,
            "session-1",
            {"unknown_field": "x"},
            {"session_id": 5},
            {"entity_ids": "entity-a"},
            {"entity_ids": ["entity-a", "entity-a"]},
            {"scene": ""},
        )
        for bad in bad_scopes:
            with self.subTest(scope=bad):
                with self.assertRaises(ScopeViolation):
                    self.episodes.build_episode_from_event(
                        namespace=NS,
                        event_id=receipt.event_id,
                        scope=bad,  # type: ignore[arg-type]
                        authorization_ref="auth:episode-build",
                    )
        self.assertEqual(0, self.count_rows("records"))
        self.assertEqual(0, self.count_rows("episode_sources"))

    def test_scope_is_stored_canonically_and_empty_scope_is_null(self) -> None:
        receipt = self.ingest()
        scope = {"entity_ids": ["entity-b", "entity-a"], "scene": "kitchen"}
        episode = self.build_episode(receipt.event_id, scope=scope)

        self.assertEqual(scope, episode.scope)
        raw = self.store.connection.execute(
            "SELECT scope_json FROM record_versions WHERE record_id = ? "
            "AND record_version = 1",
            (episode.record_id,),
        ).fetchone()["scope_json"]
        self.assertEqual(
            '{"entity_ids":["entity-b","entity-a"],"scene":"kitchen"}', raw
        )

        other = self.build_episode(self.ingest().event_id, scope={})
        raw_empty = self.store.connection.execute(
            "SELECT scope_json FROM record_versions WHERE record_id = ? "
            "AND record_version = 1",
            (other.record_id,),
        ).fetchone()["scope_json"]
        self.assertIsNone(raw_empty)
        self.assertIsNone(other.scope)

    def test_store_rejects_a_non_object_scope(self) -> None:
        with self.assertRaises(InvalidInput):
            self.store.append_record_version(
                namespace=NS,
                record_id="55555555-5555-4555-8555-555555555555",
                record_version=1,
                kind=EPISODE_KIND,
                body={"text": "scope shape is checked at the store boundary"},
                scope="not-an-object",  # type: ignore[arg-type]
            )
        self.assertEqual(0, self.count_rows("records"))

    def test_build_requires_a_standalone_transaction(self) -> None:
        receipt = self.ingest()
        with self.store.transaction():
            with self.assertRaises(StandaloneTransactionRequired) as caught:
                self.build_episode(receipt.event_id)
            self.assertEqual("INVALID_TRANSITION", caught.exception.code)
        self.assertEqual(0, self.revision())
        self.assertEqual(0, self.count_rows("records"))


class EpisodeIdempotencyTests(DomainTestCase):
    def test_duplicate_build_returns_the_mapped_version(self) -> None:
        receipt = self.ingest()
        first = self.build_episode(receipt.event_id)
        before = self.revision()
        audits = len(self.audit_rows())

        second = self.build_episode(receipt.event_id)

        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(first.record_version, second.record_version)
        self.assertEqual(first.body, second.body)
        self.assertEqual(before, self.revision())
        self.assertEqual(1, self.count_rows("records"))
        self.assertEqual(1, self.count_rows("record_versions"))
        self.assertEqual(1, self.count_rows("episode_sources"))
        self.assertEqual(audits, len(self.audit_rows()))

    def test_second_connection_returns_the_same_episode(self) -> None:
        receipt = self.ingest()
        first = self.build_episode(receipt.event_id)

        other_store = self.open_second_store()
        other_episodes = EpisodeService(other_store)
        second = other_episodes.build_episode_from_event(
            namespace=NS,
            event_id=receipt.event_id,
            scope={},
            authorization_ref="auth:episode-build",
        )

        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(1, self.count_rows("record_versions"))
        self.assertEqual(1, self.revision())

    def test_new_event_produces_a_new_episode_and_one_revision(self) -> None:
        first = self.build_episode(self.ingest().event_id)
        second = self.build_episode(self.ingest().event_id)
        self.assertNotEqual(first.record_id, second.record_id)
        self.assertEqual(2, self.revision())
        self.assertEqual(2, self.count_rows("episode_sources"))
        self.assertEqual(2, self.count_rows("record_versions"))

    def test_duplicate_build_after_episode_deletion_is_redacted(self) -> None:
        receipt = self.ingest()
        episode = self.build_episode(receipt.event_id)
        self.delete_record(episode.record_id)
        self.store.cleanup_deletions()
        before = self.revision()

        with self.assertRaises(Redacted):
            self.build_episode(receipt.event_id)
        self.assertEqual(before, self.revision())
        self.assertEqual(1, self.count_rows("records"))
        self.assertEqual(1, self.count_rows("episode_sources"))

    def test_duplicate_build_after_source_deletion_is_redacted(self) -> None:
        receipt = self.ingest()
        self.build_episode(receipt.event_id)
        self.delete_event(receipt.event_id)
        self.store.cleanup_deletions()
        before = self.revision()

        with self.assertRaises(Redacted):
            self.build_episode(receipt.event_id)
        self.assertEqual(before, self.revision())
        self.assertEqual(1, self.count_rows("records"))

    def test_episode_source_mapping_is_immutable(self) -> None:
        receipt = self.ingest()
        episode = self.build_episode(receipt.event_id)

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE episode_sources SET record_version = 2 "
                "WHERE namespace = ? AND event_id = ?",
                (NS, receipt.event_id),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM episode_sources WHERE namespace = ? AND event_id = ?",
                (NS, receipt.event_id),
            )
        mapping = self.store.episode_source(NS, receipt.event_id)
        self.assertEqual(episode.record_id, mapping.record_id)
        self.assertEqual(1, mapping.record_version)

    def test_episode_build_does_not_move_the_active_profile(self) -> None:
        profile = self.publish()
        self.activate(profile)
        before_state = self.persona.get_active_profile(NS)

        self.build_episode(self.ingest().event_id)

        after_state = self.persona.get_active_profile(NS)
        self.assertEqual(before_state, after_state)
        self.assertEqual(3, self.revision())  # publish + activate + episode

    def test_profile_and_episode_are_unified_records(self) -> None:
        profile = self.publish()
        episode = self.build_episode(self.ingest().event_id)

        kinds = {
            row["kind"]
            for row in self.store.connection.execute(
                "SELECT kind FROM records WHERE namespace = ?", (NS,)
            ).fetchall()
        }
        self.assertEqual({"persona_profile", "episode"}, kinds)
        tables = {
            row["name"]
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertNotIn("persona_profiles", tables)
        self.assertNotIn("episodes", tables)
        self.assertNotIn("persona_profile", tables)
        self.assertNotIn("episode", tables)
        self.assertIn("episode_sources", tables)
        self.assertEqual(profile.record_id, self.store.get_current(NS, profile.record_id).record_id)
        self.assertEqual(episode.record_id, self.store.get_current(NS, episode.record_id).record_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
