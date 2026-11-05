"""RG08: the ``memory.search`` sidecar method and its host-owned bounds.

The fake CLI speaks the frozen adapter envelope, so the sidecar path (gate,
worker, runtime, domain mapping) is exercised without the pinned upstream
package.  The real upstream read is covered by the isolated Lab turn.
"""

from __future__ import annotations

import json
import time
import sys
import unittest
import uuid
from pathlib import Path

from tests.desktop import support

from quintessence_pcs.store import CognitionStore

SEARCH_ITEM = {
    "kind": "event",
    "ref": "event-" + "a" * 64,
    "revision": 1,
    "scope_id": "scope-rg08",
    "state": "current",
    "content": "合成记忆：代号蓝鲸",
    "content_truncated": False,
    "sources": [
        {
            "ref": "event-" + "a" * 64,
            "revision": 1,
            "entry_id": "local",
            "agent": "agent:rg08",
            "session": "session:rg08",
            "occurred_at": "2026-10-29T17:00:00+00:00",
            "recorded_at": None,
        }
    ],
}


class MemorySearchBridgeTests(support.DesktopCase):
    def _fake_cli(self, items=None, *, paging: bool = False) -> Path:
        path = self.tmp_path / "fake_scope_recall.py"
        page_items = [SEARCH_ITEM] if items is None else list(items)
        item_json = json.dumps(page_items, ensure_ascii=False)
        if paging:
            script = (
                "import json, sys\n"
                "request = json.loads(sys.stdin.buffer.read())\n"
                f"first = json.loads({item_json!r})\n"
                "second = [dict(first[0], ref='event-' + 'c' * 64,"
                " content='second page item')]\n"
                "if request.get('action') == 'search':\n"
                "    if request.get('cursor'):\n"
                "        json.dump({'kind':'ok','result':{'items':second,"
                "'examined':2,'scan_complete':True,'stop_reason':'complete',"
                "'next_cursor':None,'memory_epoch':1}}, sys.stdout)\n"
                "    else:\n"
                "        json.dump({'kind':'ok','result':{'items':first,"
                "'examined':2,'scan_complete':False,'stop_reason':'limit',"
                "'next_cursor':'upstream-page-2','memory_epoch':1}}, sys.stdout)\n"
                "else:\n"
                "    json.dump({'kind':'ok','receipt':{'disposition':'inserted',"
                "'durability':'persisted','event_refs':[]}}, sys.stdout)\n"
            )
        else:
            script = (
                "import json, sys\n"
                "request = json.loads(sys.stdin.buffer.read())\n"
                f"items = json.loads({item_json!r})\n"
                "if request.get('action') == 'search':\n"
                "    json.dump({'kind':'ok','result':{'items':items,"
                "'examined':2,'scan_complete':True,'stop_reason':'complete',"
                "'next_cursor':None,'memory_epoch':1}}, sys.stdout)\n"
                "else:\n"
                "    json.dump({'kind':'ok','receipt':{'disposition':'inserted',"
                "'durability':'persisted','event_refs':[]}}, sys.stdout)\n"
            )
        path.write_text(script, encoding="utf-8")
        return path

    def _document(self, items=None, *, paging: bool = False):
        return support.config_document(
            self.db_path,
            purposes=["consult", "memory.bridge"],
            memory_bridge={
                "enabled": True,
                "python_executable": sys.executable,
                "entry": str(self._fake_cli(items, paging=paging)),
                "package_path": str(self.tmp_path),
                "data_directory": "memory-external",
                "agent_id": "rg08-search",
                "installation_id": "rg08-search-install",
                "scope_id": "scope-rg08",
                "session_id": "session-rg08",
                "permissions": ["memory_bridge"],
            },
        )

    def _seed_delivered_input(self, external_ref: str) -> str:
        """Create one user input and its applied delivery row, then close.

        This is the exact join the read path uses: the memory archive names an
        event, and only the delivery outbox knows which ``collab_input`` it came
        from.
        """

        store = CognitionStore.open(self.db_path)
        try:
            row = store.register_input(
                namespace=support.NAMESPACE,
                origin_kind="user_statement",
                body="合成记忆：代号蓝鲸",
                privacy_class="private",
                retention="standard",
            )
            now = "2026-10-29T17:00:00Z"
            store._require_connection().execute(
                "INSERT INTO memory_delivery ("
                "delivery_id, namespace, event_id, ref_type, ref_id, ref_version, "
                "source_event_key, source_revision, request_hash, request_json, "
                "state, attempt, external_ref, created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    support.NAMESPACE,
                    str(uuid.uuid4()),
                    "collab_input",
                    row.input_id,
                    row.content_version,
                    f"seed-{uuid.uuid4()}",
                    1,
                    "0" * 64,
                    "{}",
                    "applied",
                    1,
                    external_ref,
                    now,
                    now,
                ),
            )
            store._require_connection().commit()
        finally:
            store.close()
        return row.input_id

    def _start(self, document=None):
        process = self.start(document=document)
        info = process.request("app.info")["result"]
        return process, info

    def test_app_info_publishes_the_method_and_capability(self):
        process, info = self._start(self._document())
        self.assertIn("memory.search", info["methods"])
        self.assertTrue(info["capabilities"]["memory"]["configured"])
        self.assertTrue(info["capabilities"]["memory"]["search"])

    def test_search_returns_the_adapter_page(self):
        process, info = self._start(self._document())
        reply = process.request(
            "memory.search",
            {
                "session_id": info["session_id"],
                "query": "蓝鲸",
                "limit": 5,
                "cursor": None,
            },
        )
        self.assertTrue(reply["ok"], reply)
        result = reply["result"]
        self.assertEqual(1, len(result["items"]))
        self.assertEqual("合成记忆：代号蓝鲸", result["items"][0]["content"])
        self.assertEqual("scope-rg08", result["items"][0]["scope_id"])
        self.assertTrue(result["scan_complete"])

    def test_empty_query_browses(self):
        process, info = self._start(self._document())
        reply = process.request(
            "memory.search", {"session_id": info["session_id"], "query": ""}
        )
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(1, len(reply["result"]["items"]))

    def test_search_labels_the_speaker_from_the_delivery_row(self):
        # RG08 card B: the archive returns events with no role, so the host must
        # recover "who said this" from the delivery outbox.  A hit with no
        # delivery row must stay unattributed, not become the user's words.
        input_id = self._seed_delivered_input(SEARCH_ITEM["ref"])
        unknown = dict(SEARCH_ITEM)
        unknown["ref"] = "event-" + "b" * 64
        process, info = self._start(self._document([SEARCH_ITEM, unknown]))
        reply = process.request(
            "memory.search", {"session_id": info["session_id"], "query": "蓝鲸"}
        )
        self.assertTrue(reply["ok"], reply)
        items = reply["result"]["items"]
        self.assertEqual("user", items[0]["subject"])
        self.assertEqual(f"collab_input:{input_id}@1", items[0]["subject_ref"])
        self.assertEqual("", items[1]["subject"])
        self.assertEqual("", items[1]["subject_ref"])

    def test_bad_parameters_and_authority_are_refused_before_the_adapter(self):
        process, info = self._start(self._document())
        cases = [
            ({"query": "x" * 257}, "INVALID_PARAMS"),
            ({"query": "x", "limit": 21}, "INVALID_PARAMS"),
            ({"query": "x", "cursor": 7}, "INVALID_PARAMS"),
            ({"query": "x", "scope": "scope-other"}, "INVALID_PARAMS"),
            ({"query": "x", "namespace": "other"}, "AUTHORITY_FIELD_REJECTED"),
            ({"query": "x", "conversation_id": "c"}, "INVALID_PARAMS"),
        ]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                reply = process.request(
                    "memory.search",
                    {"session_id": info["session_id"], **extra},
                )
                self.assertFalse(reply["ok"], reply)
                self.assertEqual(expected, reply["error"]["code"], reply)

    def test_a_page_returns_a_cursor_and_the_next_page_continues(self):
        process, info = self._start(self._document(paging=True))
        first = process.request(
            "memory.search", {"session_id": info["session_id"], "query": "蓝鲸"}
        )
        self.assertTrue(first["ok"], first)
        page1 = first["result"]
        self.assertEqual(1, len(page1["items"]))
        self.assertFalse(page1["scan_complete"])
        token = page1["next_cursor"]
        # The model sees a short host token, never the ~280 character upstream
        # cursor, so the page itself still fits the 1 KiB tool excerpt.
        self.assertTrue(token.startswith("p1-"), token)
        self.assertLess(len(token), 32)

        second = process.request(
            "memory.search",
            {"session_id": info["session_id"], "query": "蓝鲸", "cursor": token},
        )
        self.assertTrue(second["ok"], second)
        page2 = second["result"]
        self.assertEqual(1, len(page2["items"]))
        self.assertIsNone(page2["next_cursor"])
        self.assertEqual("second page item", page2["items"][0]["content"])
        self.assertNotEqual(page1["items"][0]["ref"], page2["items"][0]["ref"])

    def test_a_forged_cursor_is_stale_not_an_empty_page(self):
        process, info = self._start(self._document(paging=True))
        reply = process.request(
            "memory.search",
            {"session_id": info["session_id"], "query": "蓝鲸", "cursor": "p1-forged"},
        )
        self.assertFalse(reply["ok"], reply)
        self.assertEqual("MEMORY_CURSOR_STALE", reply["error"]["code"], reply)

    def test_a_cursor_does_not_survive_a_restart(self):
        process, info = self._start(self._document(paging=True))
        first = process.request(
            "memory.search", {"session_id": info["session_id"], "query": "蓝鲸"}
        )
        token = first["result"]["next_cursor"]
        process.close()
        reopened, info2 = self._start(self._document(paging=True))
        reply = reopened.request(
            "memory.search",
            {"session_id": info2["session_id"], "query": "蓝鲸", "cursor": token},
        )
        self.assertFalse(reply["ok"], reply)
        self.assertEqual("MEMORY_CURSOR_STALE", reply["error"]["code"], reply)

    def test_a_cursor_does_not_bypass_the_memory_permission(self):
        process, info = self._start()
        reply = process.request(
            "memory.search",
            {"session_id": info["session_id"], "query": "x", "cursor": "p1-abc"},
        )
        self.assertFalse(reply["ok"], reply)
        self.assertEqual("MEMORY_BRIDGE_FORBIDDEN", reply["error"]["code"])

    def test_the_model_gets_a_page_and_a_cursor_inside_the_tool_excerpt(self):
        # A real turn: the scripted model calls memory.search, and the result the
        # model receives is one page that fits the 1 KiB tool excerpt and carries
        # a short cursor it can continue with.
        document = self._document(paging=True)
        document["destination"]["model_protocol"] = "collab.v2"
        document["destination"]["transport"]["script"] = [
            {
                "action": "call_tool",
                "tool_call": {
                    "name": "memory.search",
                    "arguments": {"query": "蓝鲸"},
                },
            },
            {"action": "final", "reply": "第一页"},
        ]
        process, info = self._start(document)
        if info["destination"]["confirmation_required"]:
            process.request(
                "app.confirm_destination",
                {
                    "session_id": info["session_id"],
                    "destination_id": info["destination"]["destination_id"],
                },
            )
        sent = process.request(
            "conversation.send",
            {
                "session_id": info["session_id"],
                "conversation_id": process.request(
                    "conversation.create",
                    {"session_id": info["session_id"], "mode": "chat"},
                )["result"]["conversation"]["conversation_id"],
                "client_request_id": "page-1",
                "text": "查一下蓝鲸",
                "confirmed_destination_id": (
                    info["destination"]["destination_id"]
                    if info["destination"]["confirmation_required"]
                    else None
                ),
            },
        )
        self.assertTrue(sent["ok"], sent)
        turn_id = sent["result"]["turn_id"]
        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline:
            ping = process.request("bridge.ping")
            if ping["ok"] and not ping["result"]["busy"]:
                if process.request(
                    "turn.result",
                    {"session_id": info["session_id"], "turn_id": turn_id},
                )["ok"]:
                    break
            time.sleep(0.05)
        rows = self.query(
            "SELECT a.body_payload_ref FROM collab_turn_steps s "
            "JOIN collab_artifacts a ON a.artifact_id = s.result_artifact_id "
            "WHERE s.turn_id = ? AND s.tool_name = 'memory.search'",
            (turn_id,),
        )
        self.assertEqual(1, len(rows))
        body = self.query(
            "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
            (rows[0]["body_payload_ref"],),
        )[0]["body_json"]
        # The model sees this artifact body, cut at 1 KiB: the page must fit
        # whole or the cursor it carries would be cut off.
        text = json.loads(body)["body"]
        self.assertLessEqual(len(text), 1024, text)
        outcome = json.loads(text)
        self.assertEqual("succeeded", outcome["status"])
        page = outcome["result"]
        self.assertTrue(page["has_more"])
        self.assertTrue(page["next_cursor"].startswith("p1-"), page["next_cursor"])
        self.assertIn("one literal page", page["note"])
        self.assertEqual(1, page["shown"])

    def test_disabled_bridge_is_unavailable_not_a_fake_empty_page(self):
        process, info = self._start()
        self.assertFalse(info["capabilities"]["memory"]["search"])
        reply = process.request(
            "memory.search", {"session_id": info["session_id"], "query": "x"}
        )
        self.assertFalse(reply["ok"], reply)
        self.assertEqual("MEMORY_BRIDGE_FORBIDDEN", reply["error"]["code"])


if __name__ == "__main__":
    unittest.main()
