"""RG08: the model-facing read-only ``memory.search`` tool.

The tool is separate from ``conversation.search``.  These tests cover the
catalog gate, the handler's bounded payload, argument authority and failures.
The end-to-end content-stage verification (a real tool result entering the next
Context) is exercised by the isolated Lab turn, not by a fake model here.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quintessence_pcs.conversation.tools import (  # noqa: E402
    MAX_MEMORY_TOOL_ITEMS,
    MEMORY_TOOL,
    TOOL_WHITELIST,
    _memory_search_handler,
    build_catalog,
)
from quintessence_pcs.runtime.agent_protocol import ToolCall  # noqa: E402
from quintessence_pcs.runtime.tool_catalog import (  # noqa: E402
    MEMORY_TOOLS,
    TOOL_ARGUMENTS_INVALID,
    TOOL_NOT_AUTHORIZED,
)

PAGE = {
    "items": [
        {
            "kind": "event",
            "ref": "event-" + "a" * 64,
            "revision": 1,
            "scope_id": "scope:rg08",
            "state": "current",
            "subject": "user",
            "subject_ref": "collab_input:4efe3366-2ba3-4aa8-aa0d-9435ec6b40bf@1",
            "content": "蓝鲸：" + "记" * 400,
            "content_truncated": True,
            "sources": [
                {
                    "entry_id": "local",
                    "session": "session:rg08",
                    "occurred_at": "2026-10-29T17:00:00+00:00",
                }
            ],
        },
        {
            "kind": "claim",
            "ref": "claim-" + "b" * 64,
            "revision": 2,
            "scope_id": "scope:rg08",
            "state": "disputed",
            "subject": "assistant",
            "subject_ref": "collab_artifact:0ef8c6ff-1dfe-49ec-80ad-5d1d54a1c0bb@1",
            "content": "review meeting moved",
            "content_truncated": False,
            "sources": [],
        },
    ],
    "examined": 9,
    "scan_complete": False,
    "stop_reason": "limit",
    "next_cursor": "next-page",
    "memory_epoch": 4,
}


class MemoryToolTests(unittest.TestCase):
    def _call(self, arguments: dict, reader=None):
        catalog = build_catalog(include_memory=True)
        handler = _memory_search_handler(reader or (lambda **kwargs: PAGE))
        call = ToolCall(
            call_id="c1",
            name=MEMORY_TOOL,
            arguments=json.dumps(arguments),
        )
        return catalog.execute(call, handlers={MEMORY_TOOL: handler})

    def test_default_catalog_does_not_publish_memory(self):
        self.assertNotIn(MEMORY_TOOL, build_catalog().names())
        self.assertIn(MEMORY_TOOL, build_catalog(include_memory=True).names())
        self.assertEqual(
            set(TOOL_WHITELIST) | {MEMORY_TOOL},
            set(build_catalog(include_memory=True).names()),
        )
        self.assertEqual(1, len(MEMORY_TOOLS))
        self.assertEqual(MEMORY_TOOL, MEMORY_TOOLS[0].name)

    def test_schema_exposes_no_authority_or_path(self):
        definition = build_catalog(include_memory=True).definitions()
        spec = next(
            item for item in definition if item["function"]["name"] == MEMORY_TOOL
        )
        properties = spec["function"]["parameters"]["properties"]
        self.assertEqual({"query", "limit", "cursor"}, set(properties))
        self.assertEqual(2, properties["limit"]["maximum"])
        self.assertEqual(96, properties["cursor"]["maxLength"])
        for forbidden in ("scope", "scope_id", "session", "agent", "path", "namespace"):
            self.assertNotIn(forbidden, properties)
        # The old description denied that this archive holds conversation text.
        self.assertNotIn("not the conversation transcript", spec["function"]["description"])
        self.assertIn("subject says who said it", spec["function"]["description"])
        self.assertIn("zero-hit answer is a real answer", spec["function"]["description"])
        self.assertIn("ONE page", spec["function"]["description"])

    def test_handler_payload_is_bounded_and_keeps_provenance(self):
        seen = {}

        def reader(**kwargs):
            seen.update(kwargs)
            return PAGE

        outcome = self._call({"query": "蓝鲸", "limit": 2}, reader=reader)
        self.assertTrue(outcome.ok, outcome.code)
        self.assertEqual({"query": "蓝鲸", "limit": 2}, seen)
        payload = dict(outcome.payload)
        self.assertEqual("scope_recall", payload["source"])
        self.assertEqual("scope:rg08", payload["scope_id"])
        self.assertEqual(2, payload["shown"])
        self.assertTrue(payload["has_more"])
        self.assertEqual(9, payload["examined"])
        self.assertEqual("limit", payload["stop_reason"])
        first = payload["items"][0]
        self.assertEqual("event", first["kind"])
        self.assertEqual("current", first["state"])
        self.assertEqual("user", first["subject"])
        self.assertEqual(
            "collab_input:4efe3366-2ba3-4aa8-aa0d-9435ec6b40bf@1",
            first["subject_ref"],
        )
        self.assertTrue(first["content_truncated"])
        self.assertLessEqual(len(first["content"]), 112)
        self.assertEqual(
            [{"entry_id": "local", "occurred_at": "2026-10-29T17:00:00+00:00"}],
            first["sources"],
        )
        self.assertIn("both speakers", payload["note"])
        self.assertIn("who said it", payload["note"])
        # A page says so, and offers the cursor that continues it.
        self.assertEqual("next-page", payload["next_cursor"])
        self.assertIn("one literal page", payload["note"])
        # The whole payload must fit the orchestrator's 1 KiB tool excerpt.
        self.assertLessEqual(len(outcome.content()), 1024, outcome.content())

    def test_authority_arguments_are_stripped_and_cannot_widen_scope(self):
        seen = {}

        def reader(**kwargs):
            seen.update(kwargs)
            return PAGE

        outcome = self._call({"query": "x", "scope": "scope:other"}, reader=reader)
        self.assertTrue(outcome.ok, outcome.code)
        self.assertEqual({"query": "x", "limit": MAX_MEMORY_TOOL_ITEMS}, seen)

    def test_missing_query_and_over_wide_limit_are_refused(self):
        for arguments in ({}, {"query": "", "limit": 2}, {"query": "x", "limit": 3}):
            with self.subTest(arguments=arguments):
                outcome = self._call(arguments)
                self.assertFalse(outcome.ok)
                self.assertEqual(TOOL_ARGUMENTS_INVALID, outcome.code)

    def test_reader_failure_is_a_readable_tool_state(self):
        def unavailable(**kwargs):
            raise RuntimeError("store gone")

        outcome = self._call({"query": "x"}, reader=unavailable)
        self.assertFalse(outcome.ok)
        self.assertEqual("memory_unavailable", outcome.code)
        self.assertNotIn("store gone", json.dumps(dict(outcome.payload)))

    def test_permission_refusal_is_tool_not_authorized(self):
        def forbidden(**kwargs):
            raise PermissionError("not permitted")

        outcome = self._call({"query": "x"}, reader=forbidden)
        self.assertFalse(outcome.ok)
        self.assertEqual(TOOL_NOT_AUTHORIZED, outcome.code)

    def test_limit_defaults_to_the_tool_bound(self):
        seen = {}

        def reader(**kwargs):
            seen.update(kwargs)
            return PAGE

        outcome = self._call({"query": "x"}, reader=reader)
        self.assertTrue(outcome.ok, outcome.code)
        self.assertEqual(MAX_MEMORY_TOOL_ITEMS, seen["limit"])

    def test_cursor_is_forwarded_and_not_invented(self):
        seen = {}

        def reader(**kwargs):
            seen.update(kwargs)
            return PAGE

        outcome = self._call({"query": "x", "cursor": "p1-abc"}, reader=reader)
        self.assertTrue(outcome.ok, outcome.code)
        self.assertEqual("p1-abc", seen["cursor"])
        # Without a cursor the reader is called exactly as before: no empty
        # argument is passed in, so an older reader keeps working.
        seen.clear()
        self._call({"query": "x"}, reader=reader)
        self.assertNotIn("cursor", seen)

    def test_bad_cursor_is_refused_before_the_reader(self):
        called = []

        def reader(**kwargs):
            called.append(kwargs)
            return PAGE

        for cursor in ("", 7, "x" * 97):
            with self.subTest(cursor=repr(cursor)[:12]):
                outcome = self._call({"query": "x", "cursor": cursor}, reader=reader)
                self.assertFalse(outcome.ok)
                self.assertEqual(TOOL_ARGUMENTS_INVALID, outcome.code)
        self.assertEqual([], called)

    def test_stale_cursor_is_its_own_state(self):
        class StaleCursor(Exception):
            code = "MEMORY_CURSOR_STALE"

        def reader(**kwargs):
            raise StaleCursor("expired")

        outcome = self._call({"query": "x", "cursor": "p1-gone"}, reader=reader)
        self.assertFalse(outcome.ok)
        self.assertEqual("memory_cursor_stale", outcome.code)
        self.assertIn("expired", json.dumps(dict(outcome.payload)))

    def test_last_page_has_no_cursor(self):
        last = dict(PAGE)
        last["next_cursor"] = None

        outcome = self._call({"query": "x"}, reader=lambda **kwargs: last)
        self.assertTrue(outcome.ok, outcome.code)
        payload = dict(outcome.payload)
        self.assertEqual("", payload["next_cursor"])
        self.assertFalse(payload["has_more"])


if __name__ == "__main__":
    unittest.main()
