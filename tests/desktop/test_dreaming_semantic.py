"""A real upstream metered route, with a synthetic desktop and final opener."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "src"), str(REPO / "apps/desktop/sidecar")]

from dreaming_semantic import DreamingSemanticHost, _DesktopEmbeddingTransport
from instance_manager import prepare_dreaming_root
from quintessence_pcs.domains.refs import RecordRef
from quintessence_pcs.runtime.collab_protocol import ConsultSendPolicy
from quintessence_pcs.runtime.dreaming_history import DreamingHistory
from quintessence_pcs.store.cognition import CognitionStore


NS = "synthetic.dreaming"
HOST = "synthetic-host"
ACCESS = "synthetic-access"
SOURCE = "The scarlet coupe requires petrol."
PARAPHRASE = "What fuel powers the red automobile?"
MODEL = "synthetic-embedding"
CREDENTIAL = "QUINTESSENCE_TEST_EMBEDDING_KEY"


class LocalOpener:
    def __init__(self):
        self.sent = []
        self.headers = []

    def open(self, request, *, timeout):
        payload = json.loads(request.data)
        self.sent.append((payload, timeout))
        self.headers.append({key.lower(): value for key, value in request.header_items()})
        text = payload["input"][0]
        vector = [1.0, 0.0] if SOURCE in text or PARAPHRASE in text else [0.0, 1.0]
        vector += [0.0] * 6
        response = io.BytesIO(json.dumps({
            "data": [{"index": 0, "embedding": vector}], "usage": {"prompt_tokens": 12},
        }).encode())
        response.status = 200
        return response


@unittest.skipUnless(importlib.util.find_spec("scope_recall"), "pinned Scope Recall is not installed")
class DreamingSemanticHostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dreaming-semantic-host-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.instance_id = str(uuid4())
        self.directory = prepare_dreaming_root(self.root, self.instance_id)
        self.legacy_db = self.root / "original.sqlite"
        self.store = CognitionStore.open(self.legacy_db)
        self.addCleanup(self.store.close)
        self.history = DreamingHistory(self.directory, instance_id=self.instance_id, namespace=NS)
        self.opener = LocalOpener()
        self.policy = ConsultSendPolicy(
            namespace=NS, host_instance=HOST, access_ref=ACCESS,
            privacy_classes=frozenset({"private"}), purposes=frozenset({"consult", "dreaming"}),
            disclose=True,
        )
        self.host = self.new_host()
        credential = patch.dict(os.environ, {CREDENTIAL: "synthetic fixture credential"})
        credential.start()
        self.addCleanup(credential.stop)

    def new_host(self):
        return DreamingSemanticHost(
            self.directory, instance_id=self.instance_id, scope_id="scope:synthetic",
            store=self.store, history=self.history, read_policy=lambda: self.policy,
            transport=_DesktopEmbeddingTransport(self.opener),
        )

    def install(self, *, calls=8):
        self.settings = {
            "enabled": True,
            "embedding": {
                "credential_env": CREDENTIAL,
                "model": MODEL,
                "endpoint": "https://embedding.example.invalid/v1/embeddings",
                "dialect": "openai",
                "dimensions": 8,
            },
            "vector_threshold": 0.9,
            "budget": {
                "batch": "synthetic-dreaming", "total_call_cap": calls,
                "total_input_cap": 64_000, "cap_micro_usd": 0,
                "approved_models": [MODEL],
                "pricing": {MODEL: {"input_usd_per_million": "0", "output_usd_per_million": "0"}},
            },
        }
        (self.directory / "embedding.json").write_text(json.dumps(self.settings), encoding="utf-8")

    def historical(self, body=SOURCE, *, retention="standard", privacy="private", host=HOST):
        row = self.store.register_input(
            namespace=NS, origin_kind="user_statement", body=body,
            privacy_class=privacy, retention=retention, content_version=1,
        )
        ref = {
            "ref_type": "collab_input", "namespace": NS,
            "input_id": row.input_id, "content_version": row.content_version,
        }
        self.store.register_interaction(
            namespace=NS, mode="consult", activation_kind="user_input",
            host_instance=host, access_ref=ACCESS, policy_ref="collab.v1", input_refs=[ref], result="answered",
        )
        return ref

    def current_turn(self, *, query=PARAPHRASE, host=HOST, retention="standard"):
        conversation = self.store.create_conversation(namespace=NS, host_instance=host, mode="chat")
        return self.store.register_turn(
            namespace=NS, host_instance=host, conversation_id=conversation.conversation_id,
            client_request_id=str(uuid4()), text=query, privacy_class="private", retention=retention,
            profile_ref=RecordRef(NS, str(uuid4()), 1).to_dict(), strategy={"mode": "chat"},
        )

    def recall(self, turn, *, query=PARAPHRASE, ref=None, allow=lambda: True):
        return self.host.recall(
            query, input_ref=turn.input_ref if ref is None else ref, turn_id=turn.turn.turn_id, allow_query=allow,
        )

    def delete(self, ref):
        self.store.delete_experience(
            namespace=NS, ref_type=ref["ref_type"], ref_id=ref["input_id"],
            ref_version=ref["content_version"], authorization_ref="synthetic-delete",
        )

    def test_provider_vectors_and_budget_are_private_and_persist_after_restart(self):
        self.install(calls=2)
        source = self.historical()
        turn = self.current_turn()
        before = self.legacy_db.read_bytes()
        maintained = self.host.maintain((source,), allow_work=lambda: True, remaining_seconds=4.0)
        self.assertEqual((maintained.admitted, maintained.indexed), (1, 1), maintained)
        self.assertEqual(self.legacy_db.read_bytes(), before)

        self.host = self.new_host()
        result = self.recall(turn)
        self.assertTrue(result.vector_available, result.gaps)
        self.assertEqual([(hit.source.ref, hit.channel) for hit in result.hits], [(source, "vector")])
        self.assertEqual(len(self.opener.sent), 2)
        self.assertEqual(self.legacy_db.read_bytes(), before)

        ledger = self.directory / "embedding-budget.sqlite3"
        with closing(sqlite3.connect(ledger)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM requests").fetchone(), (2,))
        self.assertEqual(self.recall(turn).hits, ())
        self.assertEqual(len(self.opener.sent), 2)
        self.assertNotIn(SOURCE.encode(), ledger.read_bytes())

        self.delete(source)
        rebound = self.host.rebind(
            result, query=PARAPHRASE, input_ref=turn.input_ref,
            turn_id=turn.turn.turn_id, allow_query=lambda: True,
        )
        self.assertEqual(rebound.hits, ())
        self.assertIn("pcs_source_changed", rebound.gaps)

    def test_requires_live_scope_privacy_retention_purpose_and_exact_query(self):
        self.install()
        source = self.historical()
        turn = self.current_turn()
        foreign = self.current_turn(host="another-host")
        session = self.current_turn(retention="session")
        other_scope = self.historical("Other host", host="another-host")
        private = self.historical("Restricted material", privacy="restricted")
        ephemeral = self.historical("Do not keep this", retention="session")
        page = (source, other_scope, private, ephemeral)

        self.policy = replace(self.policy, purposes=frozenset({"consult"}))
        self.assertEqual(self.host.maintain(page, allow_work=lambda: True).gaps, ("purpose_not_allowed",))
        self.assertFalse((self.directory / "embedding-budget.sqlite3").exists())
        self.policy = replace(self.policy, purposes=frozenset({"consult", "dreaming"}))
        result = self.host.maintain(page, allow_work=lambda: True, remaining_seconds=4.0)
        self.assertEqual((result.admitted, result.indexed), (1, 1), result)

        for candidate, kwargs in (
            (turn, {"query": "different text"}),
            (turn, {"ref": source}),
            (foreign, {}),
            (session, {}),
            (turn, {"allow": lambda: False}),
        ):
            with self.subTest(candidate=candidate.turn.state, kwargs=tuple(kwargs)):
                self.assertEqual(self.recall(candidate, **kwargs).gaps, ("query_not_authorized",))
        self.policy = replace(self.policy, disclose=False)
        self.assertEqual(self.recall(turn).gaps, ("query_not_authorized",))
        self.assertEqual(len(self.opener.sent), 1)

    def test_unconfigured_and_collision_degrade_without_opening_old_memory(self):
        turn = self.current_turn()
        source = self.historical()
        self.assertEqual(self.host.maintain((source,), allow_work=lambda: True).gaps, ("embedding_unconfigured",))
        self.assertEqual(self.recall(turn).gaps, ("embedding_unconfigured",))
        self.assertEqual(len(self.opener.sent), 0)
        self.assertFalse((self.directory / "embedding-budget.sqlite3").exists())
        self.install()
        path = self.directory / "embedding-budget.sqlite3"
        path.write_bytes(b"independent file")
        self.assertEqual(self.host.maintain((source,), allow_work=lambda: True).gaps, ("embedding_unavailable",))
        self.assertEqual(path.read_bytes(), b"independent file")
        self.assertEqual(len(self.opener.sent), 0)

    def test_configured_route_cannot_send_to_plaintext_remote_or_follow_a_ledger_link(self):
        self.install()
        source = self.historical()
        settings_path = self.directory / "embedding.json"
        endpoint = self.settings["embedding"]["endpoint"]
        self.settings["embedding"]["endpoint"] = endpoint.replace("https:", "http:")
        settings_path.write_text(json.dumps(self.settings), encoding="utf-8")
        self.assertEqual(self.host.maintain((source,), allow_work=lambda: True).gaps, ("embedding_unavailable",))
        self.assertEqual(self.opener.sent, [])

        self.settings["embedding"]["endpoint"] = endpoint
        settings_path.write_text(json.dumps(self.settings), encoding="utf-8")
        ledger = self.directory / "embedding-budget.sqlite3"
        outside = self.root / "unrelated.sqlite"
        outside.write_bytes(b"original")
        os.link(outside, ledger)
        self.assertEqual(self.host.maintain((source,), allow_work=lambda: True).gaps, ("embedding_unavailable",))
        self.assertEqual(outside.read_bytes(), b"original")
        self.assertEqual(self.opener.sent, [])

    def test_local_plaintext_provider_receives_no_credential(self):
        self.install()
        self.settings["embedding"]["endpoint"] = "http://127.0.0.1:61001/v1/embeddings"
        (self.directory / "embedding.json").write_text(json.dumps(self.settings), encoding="utf-8")
        source = self.historical()
        result = self.host.maintain((source,), allow_work=lambda: True, remaining_seconds=4.0)
        self.assertEqual(result.indexed, 1, result.gaps)
        self.assertEqual(len(self.opener.headers), 1)
        self.assertNotIn("authorization", self.opener.headers[0])
        self.assertNotIn("x-goog-api-key", self.opener.headers[0])

    def test_foreground_cancellation_prevents_late_embedding_result(self):
        self.install()
        source = self.historical()
        cancel = threading.Event()
        original_open = self.opener.open

        def late_open(request, *, timeout):
            response = original_open(request, timeout=timeout)
            cancel.set()
            return response

        self.opener.open = late_open
        maintained = self.host.maintain((source,), allow_work=lambda: not cancel.is_set(), remaining_seconds=4.0)
        self.assertEqual(maintained.indexed, 0, maintained)
        self.assertEqual(len(self.opener.sent), 1)


if __name__ == "__main__":
    unittest.main()
