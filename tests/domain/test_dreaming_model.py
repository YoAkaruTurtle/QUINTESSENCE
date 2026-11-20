"""Dreaming uses the real PCS model policy and a loopback provider."""

from datetime import timedelta
import json
import threading
from unittest.mock import patch
from uuid import uuid4

from tests.domain.model_support import ModelPortTestCase, NS, make_manifest

from quintessence_pcs.runtime.collab_protocol import ConsultSendPolicy
from quintessence_pcs.runtime.dreaming import DreamingState
from quintessence_pcs.runtime.dreaming_model import DreamingModel, ReflectionMaterial
from quintessence_pcs.store.cognition import CognitionStore


class DreamingModelTests(ModelPortTestCase):
    def setUp(self):
        super().setUp()
        self.directory = self.tmp_path / "dreaming"
        self.directory.mkdir()
        self.state = DreamingState(self.directory, instance_id=str(uuid4()))
        self.dream_manifest = make_manifest(self.manifest.base_url, max_calls=1)
        self.grant = self.store.grant_model_authorization(
            namespace=NS,
            authorization_ref="synthetic-operator-dreaming",
            provider=self.dream_manifest.provider,
            model=self.dream_manifest.model,
            manifest_hash=self.dream_manifest.manifest_hash,
            max_calls=1,
            max_output_tokens_per_call=512,
            max_wall_seconds=30,
            granted_at=self.clock().isoformat(timespec="seconds").replace("+00:00", "Z"),
        )
        self.allowed = True
        self.cancel = threading.Event()
        self.generation = str(uuid4())
        self.model = DreamingModel(
            self.store,
            self.state,
            self.dream_manifest,
            authorization_id=self.grant.authorization_id,
            foreground_manifest=self.manifest,
            read_policy=lambda: ConsultSendPolicy(
                namespace=NS,
                host_instance="synthetic-dreaming-host",
                access_ref="synthetic-dreaming-access",
                privacy_classes=frozenset({"private"}),
                purposes=frozenset({"dreaming"}),
                disclose=self.allowed,
            ),
            request_budget_units=20_000,
            output_reserve_units=512,
        )

    def reserve(self):
        attempt = self.state.reserve_wake(
            generation=self.generation,
            now=self.clock(),
            idle_since=self.clock() - timedelta(minutes=20),
            foreground_busy=False,
            pending_work=False,
            eligible=True,
        )
        self.assertIsNotNone(attempt)
        return attempt

    def reflect(self, attempt, material):
        return self.model.reflect(
            attempt,
            read_material=material,
            current_generation=lambda: self.generation,
            cancel=self.cancel,
            clock=self.clock,
        )

    def legacy_files(self):
        return {
            path.relative_to(self.tmp_path): (path.stat().st_ino, path.read_bytes())
            for path in self.tmp_path.rglob("*")
            if path.is_file() and self.directory not in path.parents
        }

    def test_empty_queue_can_propose_without_legacy_writes(self):
        self.enqueue_content(json.dumps({
            "decision": "propose",
            "action": "Revisit the archived draft",
            "reason": "The next step was left open.",
            "effect": "Local reading",
        }))
        # Open the same authoritative database through the production read-only
        # path and replace only the provider with the loopback fixture.
        self.store.deletion_ledger.ensure_ready(NS, 0)
        self.store.close()
        self.store = CognitionStore.open_read_only(self.db_path)
        self.addCleanup(self.store.close)
        self.model._store = self.store
        before = self.legacy_files()
        attempt = self.reserve()
        material = ReflectionMaterial("", {})
        self.assertEqual(self.reflect(attempt, lambda _policy: material), "proposed")
        self.assertEqual(self.provider.hits, 1)
        self.assertEqual(self.legacy_files(), before)
        self.assertEqual(self.store.count_model_attempts(self.grant.authorization_id), 0)
        self.assertEqual(self.state.count_model_requests(self.grant.authorization_id), 1)
        self.assertEqual(
            self.state.read_proposal(attempt.attempt_id, now=self.clock()).action,
            "Revisit the archived draft",
        )

    def test_source_change_before_send_refuses(self):
        first = ReflectionMaterial("synthetic source", {})
        values = iter((first, ReflectionMaterial("", {})))
        attempt = self.reserve()
        self.assertEqual(self.reflect(attempt, lambda _policy: next(values)), "unavailable")
        self.assertEqual(self.provider.hits, 0)
        self.assertEqual(self.state.count_model_requests(self.grant.authorization_id), 0)

    def test_revoked_model_grant_refuses(self):
        attempt = self.reserve()
        self.store.revoke_model_authorization(self.grant.authorization_id)
        self.assertEqual(
            self.reflect(attempt, lambda _policy: ReflectionMaterial("", {})),
            "unavailable",
        )
        self.assertEqual(self.provider.hits, 0)

    def test_transport_helper_rechecks_foreground_before_opening_socket(self):
        from quintessence_pcs.runtime import http_model

        attempt = self.reserve()
        request = http_model.urllib.request.Request

        def cancel_before_opener(*args, **kwargs):
            self.cancel.set()
            return request(*args, **kwargs)

        with patch.object(http_model.urllib.request, "Request", side_effect=cancel_before_opener):
            result = self.reflect(attempt, lambda _policy: ReflectionMaterial("", {}))
        self.assertEqual(result, "paused")
        self.assertEqual(self.provider.hits, 0)
        self.assertEqual(self.state.count_model_requests(self.grant.authorization_id), 1)

    def test_result_with_changed_authority_is_discarded(self):
        self.enqueue_content('{"decision":"wait"}')
        attempt = self.reserve()
        before = ReflectionMaterial("", {})
        reads = 0

        def material(_policy):
            nonlocal reads
            reads += 1
            return before if reads <= 2 else ReflectionMaterial("changed", {})

        self.assertEqual(self.reflect(attempt, material), "discarded")
        self.assertEqual(self.provider.hits, 1)
        self.assertEqual(self.state.count_model_requests(self.grant.authorization_id), 1)
        self.assertIsNone(self.state.read_proposal(attempt.attempt_id, now=self.clock()))
