"""J04: Experience bridge projection, delivery and deletion invalidation."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
for entry in (HERE, SRC):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from quintessence_pcs.domains.cognition.beliefs import belief_evidence_is_live  # noqa: E402
from quintessence_pcs.domains.cognition.query import (  # noqa: E402
    CognitionQueryService,
    EpisodeQuery,
    QueryAccess,
)
from quintessence_pcs.domains.memory.experience_bridge import (  # noqa: E402
    BridgeConfig,
    BridgeError,
    ExperienceBridge,
    _resolve_roots,
    role_for_origin,
)
from quintessence_pcs.domains.memory.episodes import EpisodeService  # noqa: E402
from quintessence_pcs.foundation.errors import Redacted  # noqa: E402
from quintessence_pcs.foundation.migrations import (  # noqa: E402
    default_migrations_dir,
)
from quintessence_pcs.runtime.context import ContextService  # noqa: E402
from quintessence_pcs.store.cognition import CognitionStore  # noqa: E402
from quintessence_pcs.store.experience import (  # noqa: E402
    ExperienceAccess,
    ExperienceUnavailable,
    ExperienceView,
)

NS = "test.j04"
HOST = "host:j04"
ACCESS_REF = "authorization:j04"


class FakeAdapter:
    def __init__(self, disposition: str = "inserted"):
        self.calls = []
        self.disposition = disposition

    def capture(self, event, *, actor_origin, timeout_seconds):
        self.calls.append(dict(event))
        return {
            "kind": "ok",
            "receipt": {
                "disposition": self.disposition,
                "durability": "persisted",
                "event_refs": [{"ref": "external-source-1", "revision": 1}],
                "gaps": [],
                "error_code": None,
            },
        }


def access(privacy=frozenset({"private"})) -> ExperienceAccess:
    return ExperienceAccess(
        namespace=NS,
        host_instance=HOST,
        access_ref=ACCESS_REF,
        privacy_classes=frozenset(privacy),
        purposes=frozenset({"memory.bridge"}),
    )


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CognitionStore.open(Path(self.tmp.name) / "pcs.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def make_input(self, *, origin="user_statement", privacy="private", body="synthetic input", retention="standard"):
        row = self.store.register_input(
            namespace=NS,
            origin_kind=origin,
            body=body,
            privacy_class=privacy,
            retention=retention,
        )
        interaction = self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
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
        return row, interaction

    def bridge(self, adapter):
        return ExperienceBridge(
            self.store, adapter, BridgeConfig(scope_id="scope-j04", max_batch=8)
        )

    def test_sync_creates_event_mapping_and_pending_outbox(self):
        row, _ = self.make_input()
        adapter = FakeAdapter()
        result = self.bridge(adapter).sync(access(), namespace=NS, limit=10)
        self.assertEqual(result.projected, 1)
        self.assertEqual(result.pending, 1)
        mapping = self.store._require_connection().execute(
            "SELECT * FROM experience_source_mapping"
        ).fetchone()
        self.assertEqual(mapping["ref_id"], row.input_id)
        self.assertEqual(json.loads(mapping["privacy_classes_json"]), ["private"])
        event = self.store.get_event(NS, mapping["event_id"])
        self.assertEqual(event.event_type, "experience.observed")
        self.assertNotIn("synthetic input", event.payload_text)
        delivery = self.store.list_deliveries(NS)[0]
        self.assertEqual(delivery["state"], "pending")
        self.assertNotIn("synthetic input", delivery["request_json"])
        self.assertTrue(self.store.projection_is_usable(NS, mapping["event_id"]))

    def test_duplicate_sync_is_idempotent(self):
        self.make_input()
        bridge = self.bridge(FakeAdapter())
        bridge.sync(access(), namespace=NS, limit=10)
        second = bridge.sync(access(), namespace=NS, limit=10)
        self.assertEqual(second.skipped_existing, 1)
        self.assertEqual(second.projected, 0)
        count = self.store._require_connection().execute(
            "SELECT count(*) FROM experience_source_mapping"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    # ------------------------------------------------- RG05-F01 backlog

    def _insert_delivery(
        self,
        *,
        state: str = "pending",
        next_retry_at: str | None = None,
        lease_until: str | None = None,
        namespace: str = NS,
    ) -> str:
        delivery_id = str(uuid.uuid4())
        now = "2026-10-29T03:04:54.000000Z"
        with self.store.transaction():
            self.store._require_connection().execute(
                "INSERT INTO memory_delivery ("
                "delivery_id, namespace, event_id, ref_type, ref_id, "
                "ref_version, source_event_key, source_revision, request_hash, "
                "request_json, state, attempt, lease_token, lease_until, "
                "next_retry_at, created_at, updated_at"
                ") VALUES (?, ?, ?, 'collab_input', ?, 1, ?, 1, ?, '{}', ?, 0, "
                "NULL, ?, ?, ?, ?)",
                (
                    delivery_id,
                    namespace,
                    str(uuid.uuid4()),
                    str(uuid.uuid4()),
                    f"key-{delivery_id}",
                    "0" * 64,
                    state,
                    lease_until,
                    next_retry_at,
                    now,
                    now,
                ),
            )
        return delivery_id

    def test_pending_delivery_summary_separates_due_future_and_leased(self):
        now = "2026-10-29T03:04:54.000000Z"
        future = "2026-10-29T03:05:24.000000Z"
        self._insert_delivery()
        self._insert_delivery(next_retry_at=future)
        self._insert_delivery(
            next_retry_at="2026-10-29T03:04:24.000000Z", lease_until=future
        )
        self._insert_delivery(state="applied")
        self._insert_delivery(namespace="test.j04.other")

        summary = self.store.pending_delivery_summary(NS, now=now)
        self.assertEqual(3, summary["pending"])
        self.assertEqual(1, summary["due"])
        self.assertEqual(now, summary["next_due_at"])

    def test_pending_delivery_summary_waits_for_future_retry(self):
        future = "2026-10-29T03:05:24.000000Z"
        self._insert_delivery(next_retry_at=future)
        summary = self.store.pending_delivery_summary(
            NS, now="2026-10-29T03:04:54.000000Z"
        )
        self.assertEqual(1, summary["pending"])
        self.assertEqual(0, summary["due"])
        self.assertEqual(future, summary["next_due_at"])
        empty = self.store.pending_delivery_summary(
            "test.j04.empty", now="2026-10-29T03:04:54.000000Z"
        )
        self.assertEqual({"pending": 0, "due": 0, "next_due_at": None}, empty)

    def test_deliver_applied_and_freezes_request(self):
        self.make_input(body="synthetic delivery body")
        adapter = FakeAdapter()
        bridge = self.bridge(adapter)
        bridge.sync(access(), namespace=NS, limit=10)
        result = bridge.deliver(access(), namespace=NS)
        self.assertEqual(result.applied, 1)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0]["content"], "synthetic delivery body")
        self.assertEqual(adapter.calls[0]["source_revision"], 1)
        delivery = self.store.list_deliveries(NS)[0]
        self.assertEqual(delivery["state"], "applied")
        self.assertEqual(delivery["external_ref"], "external-source-1")
        self.assertEqual(delivery["attempt"], 1)

    def test_second_delivery_has_nothing_to_claim(self):
        self.make_input()
        bridge = self.bridge(FakeAdapter())
        bridge.sync(access(), namespace=NS, limit=10)
        bridge.deliver(access(), namespace=NS)
        again = bridge.deliver(access(), namespace=NS)
        self.assertEqual(again.claimed, 0)

    def test_delivered_external_ref_maps_back_to_its_experience_ref(self):
        # RG08 card B: the memory read path recovers the speaker from this join.
        row, _ = self.make_input()
        bridge = self.bridge(FakeAdapter())
        bridge.sync(access(), namespace=NS, limit=10)
        bridge.deliver(access(), namespace=NS)
        found = self.store.experience_refs_for_external(
            NS, ["external-source-1", "event-never-delivered"]
        )
        self.assertEqual({"external-source-1"}, set(found))
        entry = found["external-source-1"]
        self.assertEqual("collab_input", entry["ref_type"])
        self.assertEqual(row.input_id, entry["ref_id"])
        self.assertEqual(1, entry["ref_version"])
        self.assertEqual("user_statement", entry["origin_kind"])
        self.assertEqual("applied", entry["delivery_state"])
        self.assertEqual(
            "user", role_for_origin(entry["ref_type"], entry["origin_kind"])
        )

    def test_deleted_delivery_no_longer_names_a_speaker(self):
        row, _ = self.make_input()
        bridge = self.bridge(FakeAdapter())
        bridge.sync(access(), namespace=NS, limit=10)
        bridge.deliver(access(), namespace=NS)
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=row.input_id,
            ref_version=row.content_version,
            authorization_ref="authorization:delete:1",
        )
        found = self.store.experience_refs_for_external(NS, ["external-source-1"])
        self.assertEqual({}, found)

    def test_role_for_origin_never_defaults_an_unknown_speaker_to_the_user(self):
        self.assertEqual("user", role_for_origin("collab_input", "user_statement"))
        self.assertEqual("system", role_for_origin("collab_input", "host_supplied"))
        self.assertEqual(
            "assistant", role_for_origin("collab_artifact", "model_output")
        )
        self.assertEqual(
            "tool", role_for_origin("collab_artifact", "deterministic_query")
        )
        self.assertEqual(
            "", role_for_origin("collab_artifact", "something-new")
        )
        self.assertEqual("", role_for_origin("collab_input", "model_output"))
        self.assertEqual("", role_for_origin("other_ref", "user_statement"))

    def test_deletion_invalidates_mapping_and_delivery(self):
        row, _ = self.make_input()
        bridge = self.bridge(FakeAdapter())
        bridge.sync(access(), namespace=NS, limit=10)
        mapping = self.store._require_connection().execute(
            "SELECT * FROM experience_source_mapping"
        ).fetchone()
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=row.input_id,
            ref_version=row.content_version,
            authorization_ref="authorization:delete:1",
        )
        self.assertFalse(self.store.projection_is_usable(NS, mapping["event_id"]))
        delivery = self.store.list_deliveries(NS)[0]
        self.assertEqual(delivery["state"], "obsolete")
        todo = self.store._require_connection().execute(
            "SELECT count(*) FROM memory_cleanup_todo WHERE resolved_at IS NULL"
        ).fetchone()[0]
        self.assertEqual(todo, 1)
        with self.assertRaises(Redacted):
            EpisodeService(self.store).build_episode_from_event(
                namespace=NS,
                event_id=mapping["event_id"],
                scope={"scene": "test.scene"},
                authorization_ref="authorization:episode:1",
            )

    def test_multi_privacy_artifact_is_blocked_without_rows(self):
        first, _ = self.make_input(privacy="private", body="private input")
        second, _ = self.make_input(
            origin="host_supplied", privacy="restricted", body="restricted input"
        )
        context_id = self.store.capture_snapshot(NS).context_id
        with self.store.transaction():
            self.store.register_context_sources(
                namespace=NS,
                context_id=context_id,
                sources=[
                    {
                        "ref_type": "collab_input",
                        "namespace": NS,
                        "input_id": first.input_id,
                        "content_version": first.content_version,
                    },
                    {
                        "ref_type": "collab_input",
                        "namespace": NS,
                        "input_id": second.input_id,
                        "content_version": second.content_version,
                    },
                ],
            )
        interaction = self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:j04",
            context_id=context_id,
            result="answered",
        )
        self.store.register_artifact(
            namespace=NS,
            kind="reply",
            interaction_id=interaction,
            context_id=context_id,
            origin_kind="model_output",
            body="synthetic artifact",
            retention="standard",
            sources=[
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": first.input_id,
                    "content_version": first.content_version,
                },
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": second.input_id,
                    "content_version": second.content_version,
                },
            ],
        )
        result = self.bridge(FakeAdapter()).sync(
            access({"private", "restricted"}), namespace=NS, limit=10
        )
        self.assertGreaterEqual(result.blocked, 1)
        count = self.store._require_connection().execute(
            "SELECT count(*) FROM experience_source_mapping"
        ).fetchone()[0]
        self.assertEqual(count, 2)
        artifact_mapping = self.store._require_connection().execute(
            "SELECT count(*) FROM experience_source_mapping "
            "WHERE ref_type = 'collab_artifact'"
        ).fetchone()[0]
        self.assertEqual(artifact_mapping, 0)


    def test_stale_lease_cannot_overwrite_new_claim(self):
        self.make_input()
        self.bridge(FakeAdapter()).sync(access(), namespace=NS, limit=10)
        first = self.store.claim_delivery(NS, lease_seconds=0, limit=1)
        self.assertEqual(len(first), 1)
        second = self.store.claim_delivery(
            NS, lease_seconds=60, limit=1, now="2999-01-01T00:00:00Z"
        )
        self.assertEqual(len(second), 1)
        self.assertFalse(
            self.store.finish_delivery(
                delivery_id=first[0]["delivery_id"],
                lease_token=first[0]["lease_token"],
                state="applied",
            )
        )
        self.assertTrue(
            self.store.finish_delivery(
                delivery_id=second[0]["delivery_id"],
                lease_token=second[0]["lease_token"],
                state="applied",
            )
        )

    def test_deleting_leaf_source_invalidates_artifact_projection(self):
        source, _ = self.make_input()
        context_id = self.store.capture_snapshot(NS).context_id
        with self.store.transaction():
            self.store.register_context_sources(
                namespace=NS,
                context_id=context_id,
                sources=[
                    {
                        "ref_type": "collab_input",
                        "namespace": NS,
                        "input_id": source.input_id,
                        "content_version": source.content_version,
                    }
                ],
            )
        interaction = self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:j04",
            context_id=context_id,
            result="answered",
        )
        self.store.register_artifact(
            namespace=NS,
            kind="reply",
            interaction_id=interaction,
            context_id=context_id,
            origin_kind="model_output",
            body="synthetic artifact body",
            retention="standard",
            sources=[
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": source.input_id,
                    "content_version": source.content_version,
                }
            ],
        )
        self.bridge(FakeAdapter()).sync(access(), namespace=NS, limit=10)
        artifact = self.store._require_connection().execute(
            "SELECT * FROM experience_source_mapping "
            "WHERE ref_type = 'collab_artifact'"
        ).fetchone()
        self.assertIsNotNone(artifact)
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=source.input_id,
            ref_version=source.content_version,
            authorization_ref="authorization:delete:2",
        )
        self.assertFalse(
            self.store.projection_is_usable(NS, artifact["event_id"])
        )
        delivery = self.store._require_connection().execute(
            "SELECT state FROM memory_delivery WHERE event_id = ?",
            (artifact["event_id"],),
        ).fetchone()
        self.assertEqual(delivery["state"], "obsolete")

    def test_unknown_delivery_retries_without_new_identity(self):
        self.make_input()

        class UnknownAdapter:
            def __init__(self):
                self.calls = 0

            def capture(self, event, *, actor_origin, timeout_seconds):
                self.calls += 1
                return {"kind": "unknown", "error": "timeout"}

        adapter = UnknownAdapter()
        bridge = self.bridge(adapter)
        bridge.sync(access(), namespace=NS, limit=10)
        first = bridge.deliver(access(), namespace=NS)
        self.assertEqual(first.pending, 1)
        self.assertEqual(adapter.calls, 1)
        delivery = self.store.list_deliveries(NS)[0]
        self.assertEqual(delivery["state"], "pending")
        self.assertIsNotNone(delivery["next_retry_at"])
        self.assertEqual(delivery["source_revision"], 1)


    def test_root_key_is_stable_across_input_versions(self):
        first = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="version one",
            privacy_class="private",
            retention="standard",
            content_version=1,
        )
        second = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="version two",
            privacy_class="private",
            retention="standard",
            input_id=first.input_id,
            content_version=2,
            supersedes={
                "ref_type": "collab_input",
                "namespace": NS,
                "input_id": first.input_id,
                "content_version": 1,
            },
        )
        self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:j04",
            input_refs=[
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": first.input_id,
                    "content_version": 1,
                },
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": second.input_id,
                    "content_version": 2,
                },
            ],
            result="answered",
        )
        self.bridge(FakeAdapter()).sync(access(), namespace=NS, limit=10)
        keys = [
            row["root_source_key"]
            for row in self.store._require_connection().execute(
                "SELECT root_source_key FROM experience_source_mapping "
                "ORDER BY ref_version"
            )
        ]
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])
        event_keys = [
            row["source_event_key"]
            for row in self.store._require_connection().execute(
                "SELECT source_event_key FROM experience_source_mapping "
                "ORDER BY ref_version"
            )
        ]
        self.assertNotEqual(event_keys[0], event_keys[1])

    def test_missing_mapping_fails_closed_for_bridge_event(self):
        receipt = self.store.ingest_event(
            namespace=NS,
            source_kind="host_observation",
            source_instance=HOST,
            source_event_id="synthetic-bridge-event-without-mapping",
            event_type="experience.observed",
            observed_at="2026-10-25T00:00:00Z",
            payload={"synthetic": True},
            privacy_class="private",
        )
        self.assertFalse(
            self.store.projection_is_usable(NS, receipt.event_id)
        )


    def make_artifact(self, *, origin_kind="model_output", sources=()):
        context_id = self.store.capture_snapshot(NS).context_id
        if sources:
            with self.store.transaction():
                self.store.register_context_sources(
                    namespace=NS, context_id=context_id, sources=list(sources)
                )
        interaction = self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:j04",
            context_id=context_id,
            result="answered",
        )
        return self.store.register_artifact(
            namespace=NS,
            kind="reply",
            interaction_id=interaction,
            context_id=context_id,
            origin_kind=origin_kind,
            body="synthetic artifact body",
            retention="standard",
            sources=list(sources),
        )

    def test_deterministic_query_artifact_maps_to_tool_observation(self):
        source, _ = self.make_input()
        self.make_artifact(
            origin_kind="deterministic_query",
            sources=[
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": source.input_id,
                    "content_version": source.content_version,
                }
            ],
        )
        result = self.bridge(FakeAdapter()).sync(access(), namespace=NS, limit=10)
        self.assertGreaterEqual(result.projected, 1)
        row = self.store._require_connection().execute(
            "SELECT origin, role FROM experience_source_mapping "
            "WHERE ref_type = 'collab_artifact'"
        ).fetchone()
        self.assertEqual(row["origin"], "tool_observation")
        self.assertEqual(row["role"], "tool")

    def test_delete_before_sync_does_not_project_or_queue(self):
        row, _ = self.make_input()
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=row.input_id,
            ref_version=row.content_version,
            authorization_ref="authorization:delete:before-sync",
        )
        result = self.bridge(FakeAdapter()).sync(access(), namespace=NS, limit=10)
        self.assertEqual(result.projected, 0)
        self.assertEqual(
            self.store._require_connection().execute(
                "SELECT count(*) FROM experience_source_mapping"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store._require_connection().execute(
                "SELECT count(*) FROM memory_delivery"
            ).fetchone()[0],
            0,
        )

    def test_authorization_revoked_is_a_gap_without_projection(self):
        self.make_input()
        revoked = ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
        )
        result = self.bridge(FakeAdapter()).sync(revoked, namespace=NS, limit=10)
        self.assertEqual(result.projected, 0)
        self.assertGreaterEqual(len(result.gaps), 1)
        self.assertEqual(
            self.store._require_connection().execute(
                "SELECT count(*) FROM experience_source_mapping"
            ).fetchone()[0],
            0,
        )

    def test_verified_after_delete_still_invalidates(self):
        row, _ = self.make_input()
        bridge = self.bridge(FakeAdapter())
        bridge.sync(access(), namespace=NS, limit=10)
        bridge.deliver(access(), namespace=NS)
        self.assertEqual(self.store.list_deliveries(NS)[0]["state"], "applied")
        event_id = self.store._require_connection().execute(
            "SELECT event_id FROM experience_source_mapping"
        ).fetchone()["event_id"]
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=row.input_id,
            ref_version=row.content_version,
            authorization_ref="authorization:delete:after-verified",
        )
        self.assertFalse(self.store.projection_is_usable(NS, event_id))
        self.assertEqual(self.store.list_deliveries(NS)[0]["state"], "obsolete")

    def test_lost_response_retries_same_identity_and_then_duplicates(self):
        class SequenceAdapter:
            def __init__(self):
                self.calls = []

            def capture(self, event, *, actor_origin, timeout_seconds):
                self.calls.append(event["source_event_key"])
                if len(self.calls) == 1:
                    return {"kind": "unknown", "error": "timeout"}
                return {
                    "kind": "ok",
                    "receipt": {
                        "disposition": "duplicate",
                        "durability": "persisted",
                        "event_refs": [{"ref": "external-same", "revision": 1}],
                    },
                }

        self.make_input()
        adapter = SequenceAdapter()
        bridge = self.bridge(adapter)
        bridge.sync(access(), namespace=NS, limit=10)
        first = bridge.deliver(access(), namespace=NS)
        self.assertEqual(first.pending, 1)
        self.store._require_connection().execute(
            "UPDATE memory_delivery SET next_retry_at = ?",
            ("2000-01-01T00:00:00Z",),
        )
        second = bridge.deliver(access(), namespace=NS)
        self.assertEqual(second.applied, 1)
        self.assertEqual(adapter.calls[0], adapter.calls[1])
        delivery = self.store.list_deliveries(NS)[0]
        self.assertEqual(delivery["state"], "applied")
        self.assertEqual(delivery["attempt"], 2)

    def test_changed_body_digest_requires_review(self):
        self.make_input()
        bridge = self.bridge(FakeAdapter())
        bridge.sync(access(), namespace=NS, limit=10)
        original = self.store.read_experience

        def changed(access_arg, *, ref, purpose):
            view = original(access_arg, ref=ref, purpose=purpose)
            return ExperienceView(
                ref=view.ref,
                namespace=view.namespace,
                interaction_id=view.interaction_id,
                mode=view.mode,
                origin_kind=view.origin_kind,
                processing=view.processing,
                readability=view.readability,
                body="changed body",
                retention=view.retention,
                context_ref=view.context_ref,
                attempt_ref=view.attempt_ref,
                source_refs=view.source_refs,
                created_at=view.created_at,
            )

        with mock.patch.object(self.store, "read_experience", side_effect=changed):
            result = bridge.deliver(access(), namespace=NS)
        self.assertEqual(result.needs_review, 1)
        self.assertEqual(self.store.list_deliveries(NS)[0]["state"], "needs_review")

    def test_query_episode_source_ref_is_omitted_after_delete(self):
        row, _ = self.make_input()
        bridge = self.bridge(FakeAdapter())
        bridge.sync(access(), namespace=NS, limit=10)
        event_id = self.store._require_connection().execute(
            "SELECT event_id FROM experience_source_mapping"
        ).fetchone()["event_id"]
        EpisodeService(self.store).build_episode_from_event(
            namespace=NS,
            event_id=event_id,
            scope={"scene": "test.scene"},
            authorization_ref="authorization:episode:query",
        )
        query_access = QueryAccess(
            namespace=NS,
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"reasoning", "consult"}),
        )
        service = CognitionQueryService(self.store)
        before = service.query_episodes(
            query_access,
            EpisodeQuery(
                query="experience",
                scope={"scene": "test.scene"},
                purpose="reasoning",
                limit=10,
            ),
        )
        before_refs = [
            ref.event_id
            for item in before.items
            for ref in getattr(item, "source_refs", ())
        ]
        self.assertIn(event_id, before_refs)
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=row.input_id,
            ref_version=row.content_version,
            authorization_ref="authorization:delete:query",
        )
        after = service.query_episodes(
            query_access,
            EpisodeQuery(
                query="experience",
                scope={"scene": "test.scene"},
                purpose="reasoning",
                limit=10,
            ),
        )
        after_refs = [
            ref.event_id
            for item in after.items
            for ref in getattr(item, "source_refs", ())
        ]
        self.assertNotIn(event_id, after_refs)
        self.assertTrue(
            any(
                omission.reason == "source_ref_redacted"
                for omission in after.omissions
            )
        )

    def test_context_projection_omits_deleted_bridge_event(self):
        row, _ = self.make_input()
        self.bridge(FakeAdapter()).sync(access(), namespace=NS, limit=10)
        event_id = self.store._require_connection().execute(
            "SELECT event_id FROM experience_source_mapping"
        ).fetchone()["event_id"]
        header = self.store.event_header(NS, event_id)
        service = ContextService(self.store)
        with self.store.transaction():
            facts, _, omissions = service._project_events_locked(
                namespace=NS,
                trigger_header=header,
                pending=(),
                beliefs=(),
                selected=(),
                profile_body={},
                trigger_as_fact=True,
            )
        self.assertTrue(any(item.ref.event_id == event_id for item in facts))
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=row.input_id,
            ref_version=row.content_version,
            authorization_ref="authorization:delete:context",
        )
        with self.store.transaction():
            facts, _, omissions = service._project_events_locked(
                namespace=NS,
                trigger_header=header,
                pending=(),
                beliefs=(),
                selected=(),
                profile_body={},
                trigger_as_fact=True,
            )
        self.assertFalse(any(item.ref.event_id == event_id for item in facts))
        self.assertTrue(
            any(
                item["ref"]["event_id"] == event_id
                and item["reason"] == "source_redacted"
                for item in omissions
            )
        )

    def test_delivery_uses_per_source_origin(self):
        source, _ = self.make_input()
        self.make_artifact()
        self.make_artifact(
            origin_kind="deterministic_query",
            sources=[
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": source.input_id,
                    "content_version": source.content_version,
                }
            ],
        )
        adapter = FakeAdapter()
        bridge = self.bridge(adapter)
        bridge.sync(access(), namespace=NS, limit=10)
        result = bridge.deliver(access(), namespace=NS)
        self.assertEqual(result.applied, 3)
        origins = {event["origin"] for event in adapter.calls}
        self.assertEqual(
            origins, {"human_direct", "assistant_visible", "tool_observation"}
        )

    def test_adapter_rejects_mismatched_actor_origin(self):
        from quintessence_pcs.adapters.scope_recall.transport import (
            AdapterConfig,
            SubprocessCaptureAdapter,
        )

        adapter = SubprocessCaptureAdapter(
            AdapterConfig(
                python_executable=sys.executable,
                entry="unused.py",
                package_path=".",
                data_directory=self.tmp.name,
                agent_id="a",
                installation_id="i",
                scope_ids=("s",),
                scope_id="s",
                session_id="sess",
            )
        )
        result = adapter.capture(
            {"origin": "human_direct"},
            actor_origin="assistant_visible",
            timeout_seconds=1,
        )
        self.assertEqual(result["kind"], "blocked")
        self.assertEqual(result["error"], "origin_mismatch")

    def test_session_retention_is_blocked_without_objects(self):
        self.make_input(retention="session")
        bridge = self.bridge(FakeAdapter())
        result = bridge.sync(access(), namespace=NS, limit=10)
        self.assertGreaterEqual(result.blocked, 1)
        self.assertIn(
            "retention_not_supported",
            {gap["reason"] for gap in self.store.bridge_gaps(NS)},
        )
        connection = self.store._require_connection()
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM experience_source_mapping"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.execute("SELECT count(*) FROM memory_delivery").fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM event_journal WHERE event_type = ?",
                ("experience.observed",),
            ).fetchone()[0],
            0,
        )

    def test_retention_change_before_delivery_blocks_send(self):
        self.make_input()
        adapter = FakeAdapter()
        bridge = self.bridge(adapter)
        bridge.sync(access(), namespace=NS, limit=10)
        original = self.store.read_experience

        def changed(access_arg, *, ref, purpose):
            view = original(access_arg, ref=ref, purpose=purpose)
            return ExperienceView(
                ref=view.ref,
                namespace=view.namespace,
                interaction_id=view.interaction_id,
                mode=view.mode,
                origin_kind=view.origin_kind,
                processing=view.processing,
                readability=view.readability,
                body=view.body,
                retention="session",
                context_ref=view.context_ref,
                attempt_ref=view.attempt_ref,
                source_refs=view.source_refs,
                created_at=view.created_at,
            )

        with mock.patch.object(self.store, "read_experience", side_effect=changed):
            result = bridge.deliver(access(), namespace=NS)
        self.assertEqual(result.blocked, 1)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(self.store.list_deliveries(NS)[0]["state"], "blocked")

    def test_gap_is_persisted_before_cursor_advances(self):
        self.make_input()
        self.make_input(body="second input")
        self.make_input(body="third input")

        def fake_read(access, *, ref, purpose):
            return ExperienceUnavailable(dict(ref))

        with mock.patch.object(self.store, "read_experience", side_effect=fake_read):
            result = self.bridge(FakeAdapter()).sync(access(), namespace=NS, limit=1)
            self.assertEqual(result.scanned, 1)
            self.assertEqual(len(result.gaps), 1)
            self.assertIsNotNone(self.store.bridge_cursor(NS))
            gaps = self.store.bridge_gaps(NS)
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["reason"], "unavailable")
            # Restarting from the persisted cursor continues with the next
            # page; the gap row remains visible instead of disappearing.
            restarted = self.bridge(FakeAdapter()).sync(
                access(),
                namespace=NS,
                after_cursor=self.store.bridge_cursor(NS),
                limit=1,
            )
            self.assertEqual(restarted.scanned, 1)
            self.assertEqual(len(self.store.bridge_gaps(NS)), 2)

    def test_interrupted_page_keeps_cursor_and_replays(self):
        self.make_input(body="first input")
        self.make_input(body="second input")
        original = self.store.record_projection_locked
        calls = {"n": 0}

        def boom(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected projection failure")
            return original(*args, **kwargs)

        with mock.patch.object(self.store, "record_projection_locked", side_effect=boom):
            with self.assertRaises(RuntimeError):
                self.bridge(FakeAdapter()).sync(access(), namespace=NS, limit=5)
        self.assertIsNone(self.store.bridge_cursor(NS))
        count = self.store._require_connection().execute(
            "SELECT count(*) FROM experience_source_mapping"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        retry = self.bridge(FakeAdapter()).sync(access(), namespace=NS, limit=5)
        self.assertEqual(retry.projected, 1)
        self.assertEqual(retry.skipped_existing, 1)

    def test_projection_failure_rolls_back_event_mapping_and_outbox(self):
        self.make_input()
        with mock.patch.object(
            self.store,
            "record_projection_locked",
            side_effect=RuntimeError("injected mapping failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.bridge(FakeAdapter()).sync(access(), namespace=NS, limit=5)
        connection = self.store._require_connection()
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM event_journal WHERE event_type = ?",
                ("experience.observed",),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM experience_source_mapping"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.execute("SELECT count(*) FROM memory_delivery").fetchone()[0],
            0,
        )
        self.assertIsNone(self.store.bridge_cursor(NS))

    def test_diamond_source_graph_is_not_a_cycle(self):
        class Links:
            def _artifact_source_refs(self, *, namespace, artifact_id, artifact_version):
                graph = {
                    "A": [
                        {"ref_type": "collab_artifact", "artifact_id": "B", "artifact_version": 1},
                        {"ref_type": "collab_artifact", "artifact_id": "C", "artifact_version": 1},
                    ],
                    "B": [
                        {"ref_type": "collab_input", "input_id": "D", "content_version": 1}
                    ],
                    "C": [
                        {"ref_type": "collab_input", "input_id": "D", "content_version": 1}
                    ],
                }
                return graph.get(artifact_id, [])

        meta = {
            "ref_type": "collab_artifact",
            "ref_id": "A",
            "ref_version": 1,
            "source_refs": ({"ref_type": "collab_artifact", "artifact_id": "B"},),
        }
        roots, dependencies, verified = _resolve_roots(Links(), NS, meta)
        self.assertTrue(verified)
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["ref_id"], "D")
        self.assertEqual(len(dependencies), 4)
        self.assertEqual(
            len({(d["ref_type"], d["ref_id"], d["ref_version"]) for d in dependencies}),
            4,
        )

    def test_source_cycle_is_converted_to_a_gap(self):
        class Links:
            def _artifact_source_refs(self, *, namespace, artifact_id, artifact_version):
                return (
                    [{"ref_type": "collab_artifact", "artifact_id": "B", "artifact_version": 1}]
                    if artifact_id == "A"
                    else [{"ref_type": "collab_artifact", "artifact_id": "A", "artifact_version": 1}]
                )

        meta = {"ref_type": "collab_artifact", "ref_id": "A", "ref_version": 1, "source_refs": ({"ref_type": "collab_artifact"},)}
        with self.assertRaises(BridgeError):
            _resolve_roots(Links(), NS, meta)
        first, _ = self.make_input()
        with mock.patch(
            "quintessence_pcs.domains.memory.experience_bridge._resolve_roots",
            side_effect=BridgeError("source closure cycle"),
        ):
            result = self.bridge(FakeAdapter()).sync(access(), namespace=NS, limit=5)
        self.assertGreaterEqual(result.blocked, 1)
        reasons = {gap["reason"] for gap in self.store.bridge_gaps(NS)}
        self.assertIn("unresolvable_roots", reasons)

    def test_concurrent_claim_cannot_steal_an_active_lease(self):
        self.make_input()
        self.bridge(FakeAdapter()).sync(access(), namespace=NS, limit=5)
        other = CognitionStore.open(Path(self.tmp.name) / "pcs.db")
        try:
            first = self.store.claim_delivery(NS, lease_seconds=60, limit=1)
            self.assertEqual(len(first), 1)
            second = other.claim_delivery(NS, lease_seconds=60, limit=1)
            self.assertEqual(second, [])
            later = other.claim_delivery(
                NS, lease_seconds=60, limit=1, now="2999-01-01T00:00:00Z"
            )
            self.assertEqual(len(later), 1)
        finally:
            other.close()

    def test_delete_during_capture_invalidates_and_does_not_retry(self):
        row, _ = self.make_input()

        class DeletingAdapter:
            def capture(self, event, *, actor_origin, timeout_seconds):
                self.store.delete_experience(
                    namespace=NS,
                    ref_type="collab_input",
                    ref_id=row.input_id,
                    ref_version=row.content_version,
                    authorization_ref="authorization:delete:during",
                )
                return {
                    "kind": "ok",
                    "receipt": {
                        "disposition": "inserted",
                        "durability": "persisted",
                        "event_refs": [{"ref": "external", "revision": 1}],
                    },
                }

        adapter = DeletingAdapter()
        adapter.store = self.store
        bridge = self.bridge(adapter)
        bridge.sync(access(), namespace=NS, limit=5)
        result = bridge.deliver(access(), namespace=NS)
        self.assertEqual(result.obsolete, 1)
        delivery = self.store.list_deliveries(NS)[0]
        self.assertEqual(delivery["state"], "obsolete")
        todo = self.store._require_connection().execute(
            "SELECT count(*) FROM memory_cleanup_todo WHERE resolved_at IS NULL"
        ).fetchone()[0]
        self.assertEqual(todo, 1)
        self.assertEqual(bridge.deliver(access(), namespace=NS).claimed, 0)

    def test_belief_evidence_gate_rejects_deleted_projection(self):
        row, _ = self.make_input()
        bridge = self.bridge(FakeAdapter())
        bridge.sync(access(), namespace=NS, limit=5)
        event_id = self.store._require_connection().execute(
            "SELECT event_id FROM experience_source_mapping"
        ).fetchone()["event_id"]
        body = {
            "evidence_links": [
                {
                    "ref": {
                        "ref_type": "event",
                        "namespace": NS,
                        "event_id": event_id,
                    }
                }
            ],
            "premise_refs": [],
        }
        self.assertTrue(
            belief_evidence_is_live(self.store, namespace=NS, body=body)
        )
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=row.input_id,
            ref_version=row.content_version,
            authorization_ref="authorization:delete:belief",
        )
        self.assertFalse(
            belief_evidence_is_live(self.store, namespace=NS, body=body)
        )


    def test_old_0011_database_upgrades_to_latest(self):
        old_dir = Path(self.tmp.name) / "migrations-0011"
        shutil.copytree(default_migrations_dir(), old_dir)
        (old_dir / "0012_bridge_gaps.sql").unlink()
        (old_dir / "0013_cognition_governance.sql").unlink()
        (old_dir / "0014_workless_candidates_and_blocked_impact.sql").unlink()
        (old_dir / "0015_model_attempt_tool_rounds.sql").unlink()
        (old_dir / "0016_maintenance_jobs.sql").unlink()
        (old_dir / "0017_conversation_turns.sql").unlink()
        old_db = Path(self.tmp.name) / "old-0011.sqlite"
        old = CognitionStore.open(old_db, migrations_dir=old_dir)
        try:
            self.assertEqual(
                old.connection.execute(
                    "SELECT MAX(migration_id) FROM schema_migrations"
                ).fetchone()[0],
                11,
            )
            old.record_projection_locked(
                namespace=NS,
                ref_type="collab_input",
                ref_id=str(uuid.uuid4()),
                ref_version=1,
                event_id=str(uuid.uuid4()),
                source_event_key="old-0011-key",
                source_revision=1,
                privacy_classes=("private",),
                retention="standard",
                origin="imported",
                role="user",
                principal_kind="unverified",
                root_source_key="a" * 64,
                root_members=[],
                root_verified=False,
                source_dependencies=[],
                request={},
                delivery_id=str(uuid.uuid4()),
            )
        finally:
            old.close()
        upgraded = CognitionStore.open(old_db)
        try:
            self.assertEqual(
                upgraded.connection.execute(
                    "SELECT MAX(migration_id) FROM schema_migrations"
                ).fetchone()[0],
                17,
            )
            self.assertTrue(upgraded._memory_tables_ready())
            self.assertEqual(
                upgraded.connection.execute(
                    "SELECT count(*) FROM experience_source_mapping"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                upgraded.connection.execute(
                    "SELECT count(*) FROM memory_delivery"
                ).fetchone()[0],
                1,
            )
            upgraded.record_bridge_gap(
                NS, "collab_input", str(uuid.uuid4()), 1, "upgrade_probe"
            )
            self.assertEqual(len(upgraded.bridge_gaps(NS)), 1)
        finally:
            upgraded.close()

    def test_half_migration_leaves_no_ledger_row_or_table(self):
        half = Path(self.tmp.name) / "migrations-half"
        shutil.copytree(default_migrations_dir(), half)
        (half / "0012_bridge_gaps.sql").write_text(
            "CREATE TABLE half_probe (id INTEGER);\nTHIS IS NOT SQL;\n",
            encoding="utf-8",
        )
        db = Path(self.tmp.name) / "half.sqlite"
        with self.assertRaises(Exception):
            CognitionStore.open(db, migrations_dir=half)
        connection = sqlite3.connect(str(db))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT MAX(migration_id) FROM schema_migrations"
                ).fetchone()[0],
                11,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' "
                    "AND name='half_probe'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_blocked_adapter_reports_budget_refusal(self):
        self.make_input()

        class BlockedAdapter:
            def capture(self, event, *, actor_origin, timeout_seconds):
                return {"kind": "blocked", "error": "budget_exhausted"}

        bridge = self.bridge(BlockedAdapter())
        bridge.sync(access(), namespace=NS, limit=10)
        result = bridge.deliver(access(), namespace=NS)
        self.assertEqual(result.blocked, 1)
        self.assertEqual(self.store.list_deliveries(NS)[0]["state"], "blocked")

    def test_unreachable_adapter_is_blocked_without_retry_success(self):
        self.make_input()

        class UnreachableAdapter:
            def capture(self, event, *, actor_origin, timeout_seconds):
                return {"kind": "blocked", "error": "adapter_unavailable"}

        bridge = self.bridge(UnreachableAdapter())
        bridge.sync(access(), namespace=NS, limit=10)
        result = bridge.deliver(access(), namespace=NS)
        self.assertEqual(result.blocked, 1)
        self.assertEqual(self.store.list_deliveries(NS)[0]["state"], "blocked")

    def test_old_lease_cannot_reopen_an_obsolete_delivery(self):
        row, _ = self.make_input()
        bridge = self.bridge(FakeAdapter())
        bridge.sync(access(), namespace=NS, limit=10)
        claimed = self.store.claim_delivery(NS, lease_seconds=60, limit=1)
        self.assertEqual(len(claimed), 1)
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=row.input_id,
            ref_version=row.content_version,
            authorization_ref="authorization:delete:late",
        )
        self.assertFalse(
            self.store.finish_delivery(
                delivery_id=claimed[0]["delivery_id"],
                lease_token=claimed[0]["lease_token"],
                state="applied",
            )
        )
        self.assertEqual(self.store.list_deliveries(NS)[0]["state"], "obsolete")

    def test_missing_source_privacy_row_becomes_a_gap_without_objects(self):
        self.make_artifact()
        original = self.store._projection_metadata_locked

        def metadata(access_arg, ref):
            value = original(access_arg, ref)
            if value is not None and value["ref_type"] == "collab_artifact":
                value = dict(value)
                value["privacy_classes"] = None
                value["unresolvable"] = True
            return value

        with mock.patch.object(
            self.store, "_projection_metadata_locked", side_effect=metadata
        ):
            result = self.bridge(FakeAdapter()).sync(access(), namespace=NS, limit=10)
        self.assertGreaterEqual(result.blocked, 1)
        self.assertIn(
            "unresolvable_privacy",
            {gap["reason"] for gap in self.store.bridge_gaps(NS)},
        )
        connection = self.store._require_connection()
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM experience_source_mapping"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.execute("SELECT count(*) FROM memory_delivery").fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM event_journal WHERE event_type = ?",
                ("experience.observed",),
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
