"""RG08: the read-only Scope Recall search adapter and its domain mapping.

The CLI test runs the real CLI file against a synthetic ``scope_recall``
package on ``PYTHONPATH``; it verifies the request shape, the public
management call and the bounded response without requiring the pinned upstream
distribution or a real store.  The domain test verifies the host-side mapping
and refusal classes.  Neither test reads SQLite directly or calls a model.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quintessence_pcs.domains.memory.scope_recall_search import (  # noqa: E402
    MAX_CONTENT_CHARS,
    MemorySearchCursorStale,
    MemorySearchFailed,
    MemorySearchForbidden,
    MemorySearchUnavailable,
    search_scope_recall,
)

CLI = SRC / "quintessence_pcs" / "adapters" / "scope_recall" / "cli.py"


class FakeAdapter:
    def __init__(self, outcome=None):
        self.calls = []
        self.outcome = outcome or {"kind": "ok", "result": _page()}

    def search(self, *, query, limit, cursor, timeout_seconds):
        self.calls.append(
            {
                "query": query,
                "limit": limit,
                "cursor": cursor,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.outcome


def _page(items=None, cursor=None):
    return {
        "items": _items() if items is None else items,
        "examined": 4,
        "scan_complete": cursor is None,
        "stop_reason": "complete" if cursor is None else "limit",
        "next_cursor": cursor,
        "memory_epoch": 3,
    }


def _items():
    return [
        {
            "kind": "event",
            "ref": "event-" + "a" * 64,
            "revision": 1,
            "scope_id": "scope:rg08",
            "state": "current",
            "content": "蓝鲸" * 10,
            "content_truncated": False,
            "sources": [
                {
                    "ref": "event-" + "a" * 64,
                    "revision": 1,
                    "entry_id": "local",
                    "agent": "agent:rg08",
                    "session": "session:rg08",
                    "occurred_at": "2026-10-29T17:00:00+00:00",
                    "recorded_at": "2026-10-29T17:00:01+00:00",
                }
            ],
        }
    ]


class DomainMappingTests(unittest.TestCase):
    def test_page_fields_are_mapped_and_bounded(self):
        long_item = dict(_items()[0])
        long_item["content"] = "x" * (MAX_CONTENT_CHARS + 50)
        long_item["sources"] = list(_items()[0]["sources"]) * 12
        page = search_scope_recall(
            FakeAdapter({"kind": "ok", "result": _page([long_item])}),
            query="蓝鲸",
            scope_id="scope:rg08",
            limit=5,
        )
        self.assertEqual(1, len(page.items))
        item = page.items[0]
        self.assertEqual("event", item.kind)
        self.assertEqual("scope:rg08", item.scope_id)
        self.assertEqual("current", item.state)
        self.assertEqual(MAX_CONTENT_CHARS, len(item.content))
        self.assertTrue(item.content_truncated)
        self.assertEqual(8, len(item.sources))
        self.assertEqual("local", item.sources[0].entry_id)
        self.assertEqual(4, page.examined)
        self.assertEqual(3, page.memory_epoch)

    def test_empty_query_is_a_browse_and_is_forwarded(self):
        adapter = FakeAdapter()
        page = search_scope_recall(adapter, query="", scope_id="scope:rg08")
        self.assertEqual("", adapter.calls[0]["query"])
        self.assertEqual("scope:rg08", page.items[0].scope_id)

    def test_items_are_unattributed_until_the_host_labels_them(self):
        # The upstream page carries no speaker, so the domain value must not
        # invent one; an empty subject is the honest default.
        page = search_scope_recall(
            FakeAdapter(), query="蓝鲸", scope_id="scope:rg08"
        )
        self.assertEqual("", page.items[0].subject)
        self.assertEqual("", page.as_dict()["items"][0]["subject"])

    def test_with_subjects_labels_known_refs_and_leaves_others_alone(self):
        page = search_scope_recall(
            FakeAdapter(), query="蓝鲸", scope_id="scope:rg08"
        )
        ref = page.items[0].ref
        labelled = page.with_subjects(
            {ref: ("assistant", "collab_artifact:0ef8c6ff@1")}
        )
        self.assertEqual("assistant", labelled.items[0].subject)
        self.assertEqual("collab_artifact:0ef8c6ff@1", labelled.items[0].subject_ref)
        self.assertEqual(
            "assistant", labelled.as_dict()["items"][0]["subject"]
        )
        # An unknown ref is not labelled at all, and the original page is not
        # mutated (the page is a value).
        self.assertEqual("", page.with_subjects({}).items[0].subject)
        self.assertEqual("", page.items[0].subject)

    def test_refusals_map_to_typed_errors(self):
        for kind, expected in (
            ("blocked", MemorySearchForbidden),
            ("conflict", MemorySearchCursorStale),
            ("unavailable", MemorySearchUnavailable),
            ("unknown", MemorySearchUnavailable),
            ("error", MemorySearchFailed),
        ):
            with self.subTest(kind=kind):
                with self.assertRaises(expected):
                    search_scope_recall(
                        FakeAdapter({"kind": kind}),
                        query="q",
                        scope_id="scope:rg08",
                    )

    def test_malformed_and_over_wide_answers_are_refused(self):
        with self.assertRaises(MemorySearchFailed):
            search_scope_recall(
                FakeAdapter({"kind": "ok", "result": {"items": "no"}}),
                query="q",
                scope_id="scope:rg08",
            )
        with self.assertRaises(MemorySearchFailed):
            search_scope_recall(
                FakeAdapter({"kind": "ok", "result": {"items": [{}] * 21}}),
                query="q",
                scope_id="scope:rg08",
            )
        with self.assertRaises(MemorySearchFailed):
            search_scope_recall(
                FakeAdapter(), query="q" * 257, scope_id="scope:rg08"
            )
        with self.assertRaises(MemorySearchFailed):
            search_scope_recall(
                FakeAdapter(), query="q", scope_id="scope:rg08", limit=21
            )

    def test_unknown_item_kind_is_dropped_not_invented(self):
        rows = _items() + [{"kind": "not-a-kind", "content": "x"}]
        page = search_scope_recall(
            FakeAdapter({"kind": "ok", "result": _page(rows)}),
            query="q",
            scope_id="scope:rg08",
        )
        self.assertEqual(1, len(page.items))


FAKE_CONTRACTS = '''
class ContractError(Exception):
    def __init__(self, code, field=None):
        self.code = code
        self.field = field
        super().__init__(code)


class InstanceBinding:
    def __init__(self, agent_id, installation_id, data_directory, scope_ids, test_mode):
        self.agent_id = agent_id
        self.installation_id = installation_id
        self.data_directory = data_directory
        self.scope_ids = scope_ids
        self.test_mode = test_mode


class TrustedContext:
    def __init__(self, binding, session_id, scope_ids, actor_origin):
        self.binding = binding
        self.session_id = session_id
        self.scope_ids = scope_ids
        self.actor_origin = actor_origin
'''

FAKE_CORE = '''
from .management import MemoryManagement
from .management_query import MemoryFilter
from .restore import InstallationMaintenance


class CoreConfig:
    def __init__(self, binding, write_timeout_seconds=1.0):
        self.binding = binding
        self.write_timeout_seconds = write_timeout_seconds


class MemoryCore:
    def __init__(self, config):
        self.config = config

    def initialize(self):
        raise RuntimeError("search must not initialize or migrate the store")
'''

FAKE_MANAGEMENT = '''
from .management_query import MemoryFilter


class MemoryManagement:
    def __init__(self, core, authority):
        self.core = core
        self.authority = authority

    def list(self, filters, *, limit=30, cursor=None):
        if filters.query == "denied":
            from scope_recall.contracts import ContractError
            raise ContractError("ACCESS_DENIED")
        if filters.query == "conflict":
            from scope_recall.contracts import ContractError
            raise ContractError("VERSION_CONFLICT")
        if filters.query == "missing":
            from scope_recall.contracts import ContractError
            raise ContractError("STORAGE_UNAVAILABLE")
        sources = [
            {
                "ref": "event-" + "a" * 64,
                "revision": 1,
                "entry_id": "local",
                "agent": "agent:rg08",
                "session": "session:rg08",
                "occurred_at": "2026-10-29T17:00:00+00:00",
                "recorded_at": None,
            }
        ] * 12
        return {
            "items": [
                {
                    "kind": "event",
                    "ref": "event-" + "a" * 64,
                    "revision": 1,
                    "scope_id": filters.scope,
                    "project_id": None,
                    "branch_id": None,
                    "suppressed": 0,
                    "sort_time": "2026-10-29T17:00:00+00:00",
                    "content": "MEMORY:" + filters.query + ":" + ("y" * 900),
                    "sources": sources,
                    "state": "current",
                }
            ],
            "memory_epoch": 2,
            "examined": 7,
            "scan_complete": False,
            "stop_reason": "limit",
            "next_cursor": cursor,
        }
'''

FAKE_MANAGEMENT_QUERY = '''
class MemoryFilter:
    def __init__(self, kind=None, scope=None, agent=None, session=None, since=None, until=None, query=""):
        self.kind = kind
        self.scope = scope
        self.agent = agent
        self.session = session
        self.since = since
        self.until = until
        self.query = query
'''

FAKE_RESTORE = '''
class InstallationMaintenance:
    def __init__(self, context):
        self.context = context
'''


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="rg08-adapter-"))
        package = cls.root / "scope_recall"
        (package / "core").mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "contracts.py").write_text(FAKE_CONTRACTS, encoding="utf-8")
        (package / "core" / "__init__.py").write_text(FAKE_CORE, encoding="utf-8")
        (package / "core" / "management.py").write_text(
            FAKE_MANAGEMENT, encoding="utf-8"
        )
        (package / "core" / "management_query.py").write_text(
            FAKE_MANAGEMENT_QUERY, encoding="utf-8"
        )
        (package / "core" / "restore.py").write_text(FAKE_RESTORE, encoding="utf-8")
        cls.data = cls.root / "instance" / "memory"
        cls.data.mkdir(parents=True)
        (cls.data / "memory.sqlite3").write_bytes(b"")

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.root, ignore_errors=True)

    def _binding(self):
        return {
            "agent_id": "agent:rg08",
            "installation_id": "install:rg08",
            "data_directory": str(self.data),
            "scope_ids": ["scope:rg08"],
        }

    def _run(self, payload):
        env = {
            "PYTHONPATH": str(self.root),
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        completed = subprocess.run(
            [sys.executable, str(CLI)],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            timeout=30,
            env=env,
            cwd=str(self.root),
        )
        self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8", "replace"))
        return json.loads(completed.stdout.decode("utf-8"))

    def test_search_forwards_query_and_bounds_the_answer(self):
        answer = self._run(
            {
                "action": "search",
                "binding": self._binding(),
                "query": "蓝鲸",
                "scope_id": "scope:rg08",
                "limit": 5,
                "cursor": None,
                "timeout_seconds": 2,
            }
        )
        self.assertEqual("ok", answer["kind"], answer)
        result = answer["result"]
        self.assertEqual(1, len(result["items"]))
        item = result["items"][0]
        self.assertTrue(item["content"].startswith("MEMORY:蓝鲸:"))
        self.assertTrue(item["content_truncated"])
        self.assertEqual("scope:rg08", item["scope_id"])
        self.assertEqual("current", item["state"])
        self.assertEqual(8, len(item["sources"]))
        self.assertEqual(7, result["examined"])
        self.assertFalse(result["scan_complete"])
        self.assertIsNone(result["next_cursor"])

    def test_browse_with_an_empty_query_is_allowed(self):
        answer = self._run(
            {
                "action": "search",
                "binding": self._binding(),
                "query": "",
                "scope_id": "scope:rg08",
                "limit": 10,
                "timeout_seconds": 2,
            }
        )
        self.assertEqual("ok", answer["kind"], answer)
        self.assertIn("MEMORY::", answer["result"]["items"][0]["content"])

    def test_upstream_refusals_are_classified(self):
        for query, expected in (
            ("denied", "blocked"),
            ("conflict", "conflict"),
            ("missing", "unavailable"),
        ):
            with self.subTest(query=query):
                answer = self._run(
                    {
                        "action": "search",
                        "binding": self._binding(),
                        "query": query,
                        "scope_id": "scope:rg08",
                        "limit": 5,
                        "timeout_seconds": 2,
                    }
                )
                self.assertEqual(expected, answer["kind"], answer)

    def test_missing_store_is_unavailable_without_creating_it(self):
        empty = self.root / "empty" / "memory"
        empty.mkdir(parents=True)
        binding = dict(self._binding(), data_directory=str(empty))
        answer = self._run(
            {
                "action": "search",
                "binding": binding,
                "query": "x",
                "scope_id": "scope:rg08",
                "limit": 5,
                "timeout_seconds": 2,
            }
        )
        self.assertEqual("unavailable", answer["kind"], answer)
        self.assertEqual("SOURCE_MISSING", answer["error"])
        self.assertFalse((empty / "memory.sqlite3").exists())

    def test_scope_outside_the_binding_and_bad_action_are_refused(self):
        answer = self._run(
            {
                "action": "search",
                "binding": self._binding(),
                "query": "x",
                "scope_id": "scope:other",
                "limit": 5,
                "timeout_seconds": 2,
            }
        )
        self.assertEqual("error", answer["kind"], answer)
        self.assertEqual("invalid_request", answer["error"])
        answer = self._run({"action": "delete", "binding": self._binding()})
        self.assertEqual("error", answer["kind"], answer)
        self.assertEqual("invalid_request", answer["error"])

    def test_unknown_action_is_refused(self):
        answer = self._run({"action": "delete", "binding": self._binding()})
        self.assertEqual("error", answer["kind"], answer)
        self.assertEqual("invalid_request", answer["error"])


if __name__ == "__main__":
    unittest.main()
