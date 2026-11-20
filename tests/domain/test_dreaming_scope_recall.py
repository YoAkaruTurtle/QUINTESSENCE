"""Offline verification with the actual pinned Scope Recall worker and vectors."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from quintessence_pcs.adapters.scope_recall.dreaming import (
    DreamingSemanticRecall,
    SemanticEmbedding,
    SemanticSource,
)
from quintessence_pcs.foundation.errors import CorruptStore


SOURCE = "The scarlet coupe requires petrol."
PARAPHRASE = "What fuel powers the red automobile?"
KEYWORD = "scarlet coupe"
NS = "synthetic.dreaming"


class LocalEmbeddings:
    def __init__(self):
        self.documents = []
        self.queries = []
        self.before_send = lambda: None
        self.query_fails = False

    def embed_text(self, text, *, remaining_seconds):
        from scope_recall.core.private import require_outbound

        self.before_send()
        require_outbound()
        self.documents.append(text)
        return (1.0, 0.0) if text == SOURCE else (0.0, 1.0)

    def embed_query(self, text, *, remaining_seconds):
        from scope_recall.core.private import require_outbound

        self.before_send()
        require_outbound()
        if self.query_fails:
            raise TimeoutError()
        self.queries.append(text)
        return (1.0, 0.0)


@unittest.skipUnless(importlib.util.find_spec("scope_recall"), "pinned Scope Recall is not installed")
class DreamingScopeRecallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dreaming-semantic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / "dreaming"
        self.directory.mkdir()
        self.instance_id = str(uuid4())
        (self.directory / "owner.json").write_text(json.dumps({
            "schema": "quintessence.dreaming.instance.v1", "instance_id": self.instance_id,
        }), encoding="utf-8")
        self.legacy = self.root / "Memory"
        self.legacy.mkdir()
        (self.legacy / "memory.sqlite3").write_bytes(b"unrelated original")
        self.now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.sources = {}
        self.embeddings = LocalEmbeddings()
        self.route = SemanticEmbedding(self.embeddings, "a" * 64, 2, 0.9)
        self.adapter = self.adapter_for()

    def adapter_for(self, **kwargs):
        options = {
            "instance_id": self.instance_id, "namespace": NS,
            "read_source": self.read_source, "route": self.route,
        }
        options.update(kwargs)
        return DreamingSemanticRecall(self.directory, **options)

    def read_source(self, ref):
        return self.sources.get(json.dumps(ref, sort_keys=True))

    def add_source(self, text=SOURCE, *, artifact=False):
        ref = {"namespace": NS}
        if artifact:
            ref.update(ref_type="collab_artifact", artifact_id=str(uuid4()), artifact_version=1)
        else:
            ref.update(ref_type="collab_input", input_id=str(uuid4()), content_version=1)
        source = SemanticSource(ref, text, self.now, "tool" if artifact else "user")
        self.sources[json.dumps(ref, sort_keys=True)] = source
        return source

    def revoke(self, source):
        self.sources.pop(json.dumps(source.ref, sort_keys=True), None)

    def recall(self, query, **kwargs):
        options = {"allow_query": lambda: True}
        options.update(kwargs)
        return self.adapter.recall(query, **options)

    def test_actual_upstream_vector_finds_a_paraphrase_after_restart(self):
        source = self.add_source()
        other = self.add_source("Accountant tallied twenty paper invoices.", artifact=True)
        self.assertTrue(self.adapter.project(source.ref))
        self.assertTrue(self.adapter.project(other.ref))
        self.assertEqual(self.adapter.drain().indexed, 2)
        self.assertCountEqual(self.embeddings.documents, (source.text, other.text))

        self.adapter = self.adapter_for()
        result = self.recall(PARAPHRASE)
        self.assertTrue(result.vector_available, result.gaps)
        self.assertEqual(tuple((hit.source, hit.channel) for hit in result.hits), ((source, "vector"),))
        self.assertEqual(self.recall(PARAPHRASE, use_vector=False).hits, ())
        keywords = self.recall(KEYWORD, use_vector=False)
        self.assertFalse(keywords.vector_available)
        self.assertEqual(tuple((hit.source, hit.channel) for hit in keywords.hits), ((source, "keyword"),))
        self.assertEqual((self.legacy / "memory.sqlite3").read_bytes(), b"unrelated original")
        self.assertEqual(list(self.legacy.iterdir()), [self.legacy / "memory.sqlite3"])

    def test_revocation_while_queued_skips_only_the_stale_source(self):
        revoked = self.add_source()
        eligible = self.add_source("A hummingbird can hover over a flower.")
        self.assertTrue(self.adapter.project(revoked.ref))
        self.assertTrue(self.adapter.project(eligible.ref))
        self.revoke(revoked)

        result = self.adapter.drain()
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.indexed, 1)
        self.assertEqual(self.embeddings.documents, [eligible.text])
        self.assertEqual(self.recall(KEYWORD, use_vector=False).hits, ())

    def test_late_revoke_at_provider_guard_and_after_embedding(self):
        source = self.add_source()
        self.assertTrue(self.adapter.project(source.ref))
        self.embeddings.before_send = lambda: self.revoke(source)
        result = self.adapter.drain()
        self.assertEqual((result.skipped, result.indexed), (1, 0))
        self.assertEqual(self.embeddings.documents, [])

        self.embeddings.before_send = lambda: None
        other = self.add_source()
        self.assertTrue(self.adapter.project(other.ref))
        self.assertEqual(self.adapter.drain().indexed, 1)
        self.embeddings.before_send = lambda: self.revoke(other)
        self.assertEqual(self.recall(PARAPHRASE).hits, ())

    def test_query_requires_live_disclosure_and_transport_failure_keeps_lexical(self):
        source = self.add_source()
        self.assertTrue(self.adapter.project(source.ref))
        self.assertEqual(self.adapter.drain().indexed, 1)

        allowed = True

        def disable_disclosure():
            nonlocal allowed
            allowed = False

        self.embeddings.before_send = disable_disclosure
        denied = self.recall(PARAPHRASE, allow_query=lambda: allowed)
        self.assertEqual(denied.hits, ())
        self.assertEqual(denied.gaps, ("query_not_authorized",))
        self.assertEqual(self.embeddings.queries, [])

        self.embeddings.before_send = lambda: None
        self.embeddings.query_fails = True
        fallback = self.recall(KEYWORD)
        self.assertFalse(fallback.vector_available)
        self.assertIn("vector_unavailable", fallback.gaps)
        self.assertEqual(tuple((hit.source, hit.channel) for hit in fallback.hits), ((source, "keyword"),))

    def test_projection_checks_exact_revision_namespace_scope_and_instance(self):
        source = self.add_source()
        self.assertFalse(self.adapter.project(dict(source.ref, namespace="other.instance")))
        self.assertFalse(self.adapter.project(dict(source.ref, content_version=2)))
        self.assertTrue(self.adapter.project(source.ref))
        self.assertEqual(self.adapter.drain().indexed, 1)

        other_scope = self.adapter_for(scope_id="other-project")
        self.assertEqual(other_scope.recall(PARAPHRASE, allow_query=lambda: True).hits, ())
        with self.assertRaises(CorruptStore):
            self.adapter_for(instance_id=str(uuid4())).project(source.ref)

        current_ref = dict(source.ref, content_version=2)
        current = replace(source, ref=current_ref, text="The electric tram runs on wires.")
        self.revoke(source)
        self.sources[json.dumps(current_ref, sort_keys=True)] = current
        self.assertTrue(self.adapter.project(current.ref))
        self.assertEqual(self.adapter.drain().indexed, 1)
        self.assertEqual(self.recall(PARAPHRASE).hits, ())

    def test_disabled_embedding_and_unsafe_files_stay_untouched(self):
        source = self.add_source()
        disabled = self.adapter_for(route=None)
        self.assertFalse(disabled.project(source.ref))
        self.assertEqual(disabled.drain().gaps, ("embedding_unconfigured",))
        self.assertEqual(disabled.recall(KEYWORD, allow_query=lambda: True).gaps, ("embedding_unconfigured",))
        self.assertEqual(tuple(self.directory.iterdir()), (self.directory / "owner.json",))

        self.assertTrue(self.adapter.project(source.ref))
        cache = next(self.directory.glob("scope-recall-*"))
        os.link(self.legacy / "memory.sqlite3", cache / "vectors.sqlite3")
        with self.assertRaises(CorruptStore):
            self.adapter.drain()
        self.assertEqual((self.legacy / "memory.sqlite3").read_bytes(), b"unrelated original")


if __name__ == "__main__":
    unittest.main()
