"""C05b: the collaboration deletion barrier and resumable cleanup.

The barrier is a *read* barrier: after Tx1 commits, every controlled read of
the target (and of every artifact/Context that derives from it) fails, even
though the bytes are still on disk.  Cleanup is a separate, retryable phase
that physically clears the payloads.  Exact-version semantics are tested
against the whole-object record/event rule from the C05b addendum.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from support import append  # noqa: E402
from test_collaboration_store import (  # noqa: E402
    MANIFEST,
    NS,
    RECORD_ID,
    CollaborationTestCase,
)

from quintessence_pcs.foundation.errors import Redacted  # noqa: E402
from quintessence_pcs.store.deletion import (  # noqa: E402
    DELETION_CLEANED,
    DELETION_PENDING_CLEANUP,
)


class CollaborationDeletionTests(CollaborationTestCase):
    # ------------------------------------------------------------- fixtures

    def attempt(self, context_id: str) -> str:
        authorization = self.store.grant_model_authorization(
            namespace=NS,
            authorization_ref="the user allowed one synthetic call",
            provider="synthetic-provider",
            model="synthetic-model",
            manifest_hash=MANIFEST,
            max_calls=2,
            max_output_tokens_per_call=100,
            max_wall_seconds=10,
        )
        reservation = self.store.reserve_model_attempt(
            run_id=str(uuid.uuid4()),
            namespace=NS,
            context_id=context_id,
            authorization_id=authorization.authorization_id,
            provider="synthetic-provider",
            model_requested="synthetic-model",
            manifest_hash=MANIFEST,
            adapter_kind="fake-model",
            policy_version="collab.v1",
            request_body="synthetic request body",
            manifest_max_calls=2,
            manifest_max_output_tokens_per_call=100,
            manifest_max_wall_seconds=10,
        )
        return reservation.attempt.attempt_id

    def delete_input(self, row, *, authorization: str = "auth:delete:1"):
        return self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=row.input_id,
            ref_version=row.content_version,
            authorization_ref=authorization,
        )

    def attempt_payload_length(self, attempt_id: str) -> int:
        row = self.store.connection.execute(
            "SELECT length(body_json) FROM private_payloads WHERE payload_ref = "
            "(SELECT request_payload_ref FROM model_attempts WHERE attempt_id = ?)",
            (attempt_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        return int(row[0])

    # ---------------------------------------------------------------- tests

    def test_delete_input_barrier_refuses_reads_before_cleanup(self) -> None:
        source = self.new_input()
        payload = self.payload_ref(
            "collab_inputs",
            "body_payload_ref",
            input_id=source.input_id,
            content_version=1,
        )
        target = self.delete_input(source)
        self.assertEqual(DELETION_PENDING_CLEANUP, target.status)
        self.assertEqual(1, target.target_version)
        with self.assertRaises(Redacted):
            self.store.load_input(
                namespace=NS, input_id=source.input_id, content_version=1
            )
        self.assertGreater(self.payload_length(payload), 0, "barrier is not cleanup")
        self.store.cleanup_deletions(namespace=NS)
        self.assertEqual(0, self.payload_length(payload))
        with self.assertRaises(Redacted):
            self.store.load_input(
                namespace=NS, input_id=source.input_id, content_version=1
            )

    def test_barrier_refuses_dependent_artifact_before_cleanup(self) -> None:
        source = self.new_input()
        artifact = self.new_artifact(sources=[self.input_ref(source)])
        artifact_payload = self.payload_ref(
            "collab_artifacts",
            "body_payload_ref",
            artifact_id=artifact.artifact_id,
        )
        self.delete_input(source)
        with self.assertRaises(Redacted):
            self.store.load_artifact(
                namespace=NS, artifact_id=artifact.artifact_id, artifact_version=1
            )
        self.assertGreater(
            self.payload_length(artifact_payload), 0, "dependent body remains before cleanup"
        )
        self.store.cleanup_deletions(namespace=NS)
        self.assertEqual(0, self.payload_length(artifact_payload))
        with self.assertRaises(Redacted):
            self.store.load_artifact(
                namespace=NS, artifact_id=artifact.artifact_id, artifact_version=1
            )

    def test_barrier_refuses_multilevel_dependent_artifact(self) -> None:
        source = self.new_input()
        level_b = self.new_artifact(sources=[self.input_ref(source)])
        level_c = self.new_artifact(sources=[self.artifact_ref(level_b)])
        payload_b = self.payload_ref(
            "collab_artifacts", "body_payload_ref", artifact_id=level_b.artifact_id
        )
        payload_c = self.payload_ref(
            "collab_artifacts", "body_payload_ref", artifact_id=level_c.artifact_id
        )
        self.delete_input(source)
        for artifact in (level_b, level_c):
            with self.assertRaises(Redacted):
                self.store.load_artifact(
                    namespace=NS, artifact_id=artifact.artifact_id, artifact_version=1
                )
        self.assertGreater(self.payload_length(payload_b), 0)
        self.assertGreater(self.payload_length(payload_c), 0)
        self.store.cleanup_deletions(namespace=NS)
        self.assertEqual(0, self.payload_length(payload_b))
        self.assertEqual(0, self.payload_length(payload_c))

    def test_barrier_refuses_context_delivery_and_attempt_payload(self) -> None:
        for version in (1, 2):
            append(
                self.store,
                version=version,
                body={"text": f"record version {version}"},
                namespace=NS,
            )
        context = self.make_context(records=[("record", RECORD_ID, 2)])
        attempt = self.attempt(context)
        self.assertEqual(
            "synthetic request body",
            self.store.load_model_attempt_payload(attempt),
        )
        self.store.request_deletion(
            target_kind="record",
            namespace=NS,
            target_id=RECORD_ID,
            authorization_ref="auth:record",
        )
        with self.assertRaises(Redacted):
            self.store.require_context_deliverable(context)
        with self.assertRaises(Redacted):
            self.store.load_model_attempt_payload(attempt)
        self.assertGreater(
            self.attempt_payload_length(attempt), 0, "payload remains before cleanup"
        )
        self.store.cleanup_deletions(namespace=NS)
        self.assertEqual(0, self.attempt_payload_length(attempt))

    def test_delete_exact_version_leaves_other_versions_readable(self) -> None:
        first = self.new_input(version=1)
        second = self.new_input(version=2, body="second keeps living")
        artifact_one = self.new_artifact(sources=[self.input_ref(first)])
        artifact_two = self.new_artifact(sources=[self.input_ref(second)])
        self.delete_input(first)
        with self.assertRaises(Redacted):
            self.store.load_artifact(
                namespace=NS, artifact_id=artifact_one.artifact_id, artifact_version=1
            )
        readable = self.store.load_artifact(
            namespace=NS, artifact_id=artifact_two.artifact_id, artifact_version=1
        )
        self.assertEqual((self.input_ref(second),), readable.source_refs)
        self.store.cleanup_deletions(namespace=NS)
        self.assertEqual(
            "synthetic artifact body",
            self.store.load_artifact(
                namespace=NS, artifact_id=artifact_two.artifact_id, artifact_version=1
            ).body,
        )

    def test_cleanup_clears_body_and_proposal_payloads(self) -> None:
        source = self.new_input()
        candidate = self.new_candidate(sources=[self.input_ref(source)])
        body_payload = self.payload_ref(
            "collab_artifacts", "body_payload_ref", artifact_id=candidate.artifact_id
        )
        proposal_payload = self.payload_ref(
            "collab_artifacts", "proposal_payload_ref", artifact_id=candidate.artifact_id
        )
        self.delete_input(source)
        self.store.cleanup_deletions(namespace=NS)
        self.assertEqual(0, self.payload_length(body_payload))
        self.assertEqual(0, self.payload_length(proposal_payload))
        row = self.store.connection.execute(
            "SELECT status FROM collab_artifacts WHERE artifact_id = ?",
            (candidate.artifact_id,),
        ).fetchone()
        self.assertEqual("stale", row[0])

    def test_upstream_record_deletion_clears_collab_artifact_body(self) -> None:
        append(self.store, version=1, body={"text": "the upstream record"}, namespace=NS)
        artifact = self.new_artifact(sources=[self.record_ref(1)])
        payload = self.payload_ref(
            "collab_artifacts", "body_payload_ref", artifact_id=artifact.artifact_id
        )
        self.store.request_deletion(
            target_kind="record",
            namespace=NS,
            target_id=RECORD_ID,
            authorization_ref="auth:record",
        )
        with self.assertRaises(Redacted):
            self.store.load_artifact(
                namespace=NS, artifact_id=artifact.artifact_id, artifact_version=1
            )
        self.assertGreater(self.payload_length(payload), 0)
        self.store.cleanup_deletions(namespace=NS)
        self.assertEqual(0, self.payload_length(payload))

    def test_cleanup_is_retryable_and_idempotent(self) -> None:
        source = self.new_input()
        artifact = self.new_artifact(sources=[self.input_ref(source)])
        payload = self.payload_ref(
            "collab_artifacts", "body_payload_ref", artifact_id=artifact.artifact_id
        )
        self.delete_input(source)
        first = self.store.cleanup_deletions(namespace=NS)
        self.assertEqual(1, first.cleaned_count)
        self.assertEqual(0, first.failed_count)
        self.assertEqual(0, self.payload_length(payload))
        second = self.store.cleanup_deletions(namespace=NS)
        self.assertEqual((), second.results)
        watermarks_before = self.store.connection.execute(
            "SELECT state_revision, privacy_epoch FROM namespace_state "
            "WHERE namespace = ?",
            (NS,),
        ).fetchone()
        again = self.delete_input(source)
        self.assertEqual(DELETION_CLEANED, again.status)
        watermarks_after = self.store.connection.execute(
            "SELECT state_revision, privacy_epoch FROM namespace_state "
            "WHERE namespace = ?",
            (NS,),
        ).fetchone()
        self.assertEqual(tuple(watermarks_before), tuple(watermarks_after))

    def test_record_barrier_blocks_artifact_linked_at_any_version(self) -> None:
        for version in (1, 2):
            append(
                self.store,
                version=version,
                body={"text": f"record version {version}"},
                namespace=NS,
            )
        context = self.make_context(records=[("record", RECORD_ID, 2)])
        artifact = self.new_artifact(
            context_id=context, sources=[self.record_ref(2)]
        )
        self.store.request_deletion(
            target_kind="record",
            namespace=NS,
            target_id=RECORD_ID,
            authorization_ref="auth:record",
        )
        with self.assertRaises(Redacted):
            self.store.load_artifact(
                namespace=NS, artifact_id=artifact.artifact_id, artifact_version=1
            )
        with self.assertRaises(Redacted):
            self.store.require_context_deliverable(context)

        # Contrast: a collaboration input is deleted at one exact version.
        first = self.new_input(version=1)
        second = self.new_input(version=2, body="version two survives")
        collab_context = self.make_context(collab=[self.input_ref(second)])
        survivor = self.new_artifact(
            context_id=collab_context, sources=[self.input_ref(second)]
        )
        self.delete_input(first)
        loaded = self.store.load_artifact(
            namespace=NS, artifact_id=survivor.artifact_id, artifact_version=1
        )
        self.assertEqual("synthetic artifact body", loaded.body)
        self.store.require_context_deliverable(collab_context)


if __name__ == "__main__":
    unittest.main()
