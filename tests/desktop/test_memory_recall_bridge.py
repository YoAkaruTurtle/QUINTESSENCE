"""RG08 R-B: automatic recall through the real framed sidecar.

The fake CLI speaks the frozen adapter envelope, so the sidecar path (gate,
orchestrator, Context, DB) is real while the upstream recall packet is
deterministic.  The real Scope Recall read is covered by the isolated Lab turn.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
import uuid
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path

from tests.desktop import support

from quintessence_pcs.store import CognitionStore

EVENT_REF = "event-" + "a" * 64


def recall_item() -> dict:
    return {
        "ref": EVENT_REF,
        "revision": 1,
        "kind": "event",
        "content": "我喜欢红色，尤其是番茄红。",
        "content_truncated": False,
        "temporal_status": "current",
        "origin": "user",
        "applicability": "trusted scope",
        "evidence_refs": [],
        "expandable": False,
        "basis": "direct_report",
        "occurred_at": "2026-10-20T10:00:00+08:00",
        "sources": [],
    }


def recall_packet(status: str = "ok", *, items=None, gaps=(), coverage="unknown") -> dict:
    return {
        "status": status,
        "items": [] if items is None else items,
        "gaps": list(gaps),
        "coverage": coverage,
        "answerability": "supported" if items else "unknown",
        "memory_epoch": 1,
        "unmet_needs": [],
    }


class MemoryRecallBridgeTests(support.DesktopCase):
    def _fake_cli(
        self,
        *,
        behavior: str = "ok",
        log_path: str | None = None,
        request_log_path: str | None = None,
    ) -> str:
        path = self.tmp_path / f"fake_recall_{behavior}.py"
        log = (
            f"log = open({log_path!r}, 'a', encoding='utf-8')\n"
            "log.write(str(request.get('action')) + chr(10))\n"
            "log.close()\n"
            if log_path
            else ""
        )
        request_log = (
            f"rlog = open({request_log_path!r}, 'a', encoding='utf-8')\n"
            "rlog.write(json.dumps(request, sort_keys=True, ensure_ascii=False) + chr(10))\n"
            "rlog.close()\n"
            if request_log_path
            else ""
        )
        if behavior == "timeout":
            body = "import time\ntime.sleep(3)\n"
        elif behavior == "natural":
            ok_packet = recall_packet("ok", items=[recall_item()])
            miss_packet = recall_packet("no_match", items=[])
            search_packet = {
                "status": "ok",
                "items": [],
                "gaps": [],
                "coverage": "unknown",
                "answerability": "unknown",
                "memory_epoch": 1,
                "examined": 0,
                "scan_complete": True,
                "stop_reason": "complete",
                "next_cursor": None,
            }
            body = (
                "import json, sys\n"
                "request = json.loads(sys.stdin.buffer.read())\n"
                + log
                + f"ok_packet = json.loads({json.dumps(ok_packet)!r})\n"
                + f"miss_packet = json.loads({json.dumps(miss_packet)!r})\n"
                + f"search_packet = json.loads({json.dumps(search_packet)!r})\n"
                "if request.get('action') == 'recall':\n"
                "    packet = miss_packet\n"
                "    if request.get('query') == '我喜欢':\n"
                "        packet = dict(ok_packet)\n"
                "        packet['status'] = 'partial'\n"
                "        packet['query_variant'] = 'focus_prefix'\n"
                "    json.dump({'kind':'ok','result':packet}, sys.stdout)\n"
                "elif request.get('action') == 'search':\n"
                "    json.dump({'kind':'ok','result':search_packet}, sys.stdout)\n"
                "else:\n"
                "    json.dump({'kind':'ok','receipt':{'disposition':'inserted',"
                "'durability':'persisted','event_refs':[]}}, sys.stdout)\n"
            )
        elif behavior == "time":
            miss_packet = recall_packet("no_match", items=[])
            window_packet = {
                "status": "ok",
                "items": [recall_item()],
                "examined": 1,
                "scan_complete": True,
                "stop_reason": "complete",
                "pages": 1,
            }
            body = (
                "import json, sys\n"
                "request = json.loads(sys.stdin.buffer.read())\n"
                + log
                + request_log
                + f"miss_packet = json.loads({json.dumps(miss_packet)!r})\n"
                + f"window_packet = json.loads({json.dumps(window_packet)!r})\n"
                "if request.get('action') == 'time_window':\n"
                "    json.dump({'kind':'ok','result':window_packet}, sys.stdout)\n"
                "elif request.get('action') == 'recall':\n"
                "    json.dump({'kind':'ok','result':miss_packet}, sys.stdout)\n"
                "elif request.get('action') == 'search':\n"
                "    json.dump({'kind':'ok','result':{'status':'ok','items':[],"
                "'gaps':[],'coverage':'unknown','answerability':'unknown',"
                "'memory_epoch':1,'examined':0,'scan_complete':True,"
                "'stop_reason':'complete','next_cursor':None}}, sys.stdout)\n"
                "else:\n"
                "    json.dump({'kind':'ok','receipt':{'disposition':'inserted',"
                "'durability':'persisted','event_refs':[]}}, sys.stdout)\n"
            )
        else:
            search_items = (
                [recall_item()] if behavior in ("ok", "dedupe") else []
            )
            packet = recall_packet(
                "ok" if behavior in ("ok", "dedupe") else "no_match",
                items=search_items,
                gaps=["deadline_exceeded_collect"] if behavior == "incomplete" else [],
                coverage="partial" if behavior == "incomplete" else "unknown",
            )
            search_packet = {
                "status": "ok",
                "items": search_items,
                "gaps": [],
                "coverage": "unknown",
                "answerability": "supported" if search_items else "unknown",
                "memory_epoch": 1,
                "examined": 0,
                "scan_complete": True,
                "stop_reason": "complete",
                "next_cursor": None,
            }
            body = (
                "import json, sys\n"
                "request = json.loads(sys.stdin.buffer.read())\n"
                + log
                + f"packet = json.loads({json.dumps(packet)!r})\n"
                + f"search_packet = json.loads({json.dumps(search_packet)!r})\n"
                "if request.get('action') == 'recall':\n"
                "    json.dump({'kind':'ok','result':packet}, sys.stdout)\n"
                "elif request.get('action') == 'search':\n"
                "    json.dump({'kind':'ok','result':search_packet}, sys.stdout)\n"
                "else:\n"
                "    json.dump({'kind':'ok','receipt':{'disposition':'inserted',"
                "'durability':'persisted','event_refs':[]}}, sys.stdout)\n"
            )
        path.write_text(body, encoding="utf-8")
        return str(path)

    def _document(
        self,
        *,
        behavior: str = "ok",
        enabled: bool = True,
        timeout: float = 1.5,
        script=None,
        model_protocol: str = "collab.v1",
        log_path: str | None = None,
        request_log_path: str | None = None,
    ):
        memory_dir = self.tmp_path / "memory-external"
        memory_dir.mkdir(exist_ok=True)
        (memory_dir / "memory.sqlite3").touch()
        document = support.config_document(
            self.db_path,
            script=script
            or [{"action": "none", "reply": "合成回答：已核对可披露材料。"}],
            purposes=["consult", "memory.bridge"],
            memory_bridge={
                "enabled": True,
                "python_executable": sys.executable,
                "entry": self._fake_cli(
                    behavior=behavior,
                    log_path=log_path,
                    request_log_path=request_log_path,
                ),
                "package_path": str(self.tmp_path),
                "data_directory": "memory-external",
                "agent_id": "rg08-rb",
                "installation_id": "rg08-rb-install",
                "scope_id": "scope:rg08-rb",
                "session_id": "session:rg08-rb",
                "permissions": ["memory_bridge"],
            },
            memory={
                "timezone": "Asia/Shanghai",
                "recall": {
                    "enabled": enabled,
                    "max_items": 4,
                    "budget_units": 2048,
                    "timeout_seconds": timeout,
                },
            },
        )
        document["destination"]["model_protocol"] = model_protocol
        return document

    def _seed_source(
        self,
        *,
        external_revision: int = 1,
        body: str = "我喜欢红色，尤其是番茄红。",
        observed_at: str | None = None,
        pcs_time: dict | None = None,
    ) -> dict:
        path = self.db_path
        if not path.exists() or self.scalar(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='memory_delivery'"
        ) is None:
            store = CognitionStore.open(path)
            store.close()
        store = CognitionStore.open(path)
        try:
            row = store.register_input(
                namespace=support.NAMESPACE,
                origin_kind="user_statement",
                body=body,
                privacy_class="private",
                retention="standard",
                observed_at=observed_at,
            )
            store.register_interaction(
                namespace=support.NAMESPACE,
                mode="consult",
                activation_kind="user_input",
                host_instance=support.HOST_INSTANCE,
                access_ref=support.ACCESS_REF,
                policy_ref="policy:collab.v1",
                input_refs=[
                    {
                        "ref_type": "collab_input",
                        "namespace": support.NAMESPACE,
                        "input_id": row.input_id,
                        "content_version": row.content_version,
                    }
                ],
            )
            event = store.ingest_event(
                namespace=support.NAMESPACE,
                source_kind="host_observation",
                source_instance="rg08-rb-test",
                source_event_id=f"source-{uuid.uuid4()}",
                event_type="ExperienceObserved",
                payload={"text": body},
                observed_at="2026-10-20T10:00:00Z",
                recorded_at="2026-10-20T10:00:00Z",
            )
            delivery_id = str(uuid.uuid4())
            source_key = f"source-{uuid.uuid4()}"
            now = "2026-10-20T10:00:00Z"
            request_json = (
                json.dumps({"pcs_time": pcs_time}, ensure_ascii=False)
                if pcs_time is not None
                else "{}"
            )
            connection = store._require_connection()
            connection.execute(
                "INSERT INTO memory_delivery ("
                "delivery_id, namespace, event_id, ref_type, ref_id, ref_version, "
                "source_event_key, source_revision, request_hash, request_json, "
                "state, attempt, external_ref, external_revision, created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    delivery_id,
                    support.NAMESPACE,
                    event.event_id,
                    "collab_input",
                    row.input_id,
                    row.content_version,
                    source_key,
                    1,
                    "0" * 64,
                    request_json,
                    "applied",
                    1,
                    EVENT_REF,
                    external_revision,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO experience_source_mapping ("
                "namespace, ref_type, ref_id, ref_version, event_id, source_event_key, "
                "source_revision, privacy_classes_json, retention, origin, role, "
                "principal_kind, root_source_key, root_members_json, root_verified, "
                "source_dependencies_json, delivery_id, format_version, created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    support.NAMESPACE,
                    "collab_input",
                    row.input_id,
                    row.content_version,
                    event.event_id,
                    source_key,
                    1,
                    '["private"]',
                    "standard",
                    "user_statement",
                    "user",
                    "human",
                    "0" * 64,
                    "[]",
                    1,
                    '[{"ref_type":"collab_input","ref_id":"'
                    + row.input_id
                    + '","ref_version":'
                    + str(row.content_version)
                    + "}]",
                    delivery_id,
                    "j04.projection.v1",
                    now,
                ),
            )
            connection.commit()
            return {
                "input_id": row.input_id,
                "content_version": row.content_version,
                "event_id": event.event_id,
            }
        finally:
            store.close()

    def _start(self, document):
        process = self.start(document=document)
        info = process.request("app.info")["result"]
        if info["destination"]["confirmation_required"]:
            confirmed = process.request(
                "app.confirm_destination",
                {
                    "session_id": info["session_id"],
                    "destination_id": info["destination"]["destination_id"],
                },
            )
            self.assertTrue(confirmed["ok"], confirmed)
        return process, info

    def _turn(self, process, info, *, text: str, request_id: str):
        conversation = process.request(
            "conversation.create",
            {"session_id": info["session_id"], "mode": "chat"},
        )["result"]["conversation"]["conversation_id"]
        sent = process.request(
            "conversation.send",
            {
                "session_id": info["session_id"],
                "conversation_id": conversation,
                "client_request_id": request_id,
                "text": text,
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
                result = process.request(
                    "turn.result",
                    {"session_id": info["session_id"], "turn_id": turn_id},
                )
                if result["ok"]:
                    return turn_id, result["result"]
            time.sleep(0.05)
        raise TimeoutError("turn did not settle")

    def _context_for_turn(self, turn_id: str) -> dict:
        rows = self.query(
            "SELECT c.rendered_input_payload_ref, c.budget_view_json "
            "FROM context_snapshots c "
            "JOIN collab_stage_contexts sc ON sc.context_id = c.context_id "
            "JOIN collab_turn_stages st ON st.stage_id = sc.stage_id "
            "AND st.namespace = sc.namespace "
            "WHERE st.turn_id = ? AND c.context_kind = 'consult' "
            "ORDER BY sc.ordinal DESC LIMIT 1",
            (turn_id,),
        )
        self.assertEqual(1, len(rows), rows)
        payload_ref = rows[0]["rendered_input_payload_ref"]
        body = self.query(
            "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
            (payload_ref,),
        )[0]["body_json"]
        wrapper = json.loads(body)
        document = json.loads(wrapper["rendered_input"])
        document["_budget_view"] = json.loads(rows[0]["budget_view_json"])
        return document

    def _events(self, turn_id: str) -> list[dict]:
        rows = self.query(
            "SELECT kind, payload_json FROM collab_turn_events "
            "WHERE turn_id = ? ORDER BY seq",
            (turn_id,),
        )
        return [
            {"kind": row["kind"], "payload": json.loads(row["payload_json"])}
            for row in rows
        ]

    def test_app_info_publishes_recall_capability(self):
        process, info = self._start(self._document(enabled=False))
        recall = info["capabilities"]["memory"]["recall"]
        self.assertFalse(recall["enabled"])
        self.assertTrue(recall["bridge_configured"])
        self.assertFalse(recall["configured"])
        self.assertEqual(4, recall["max_items"])
        self.assertEqual(2048, recall["budget_units"])
        self.assertEqual("Asia/Shanghai", recall["timezone"])

    def test_missing_bridge_is_unavailable_and_chat_continues(self):
        document = support.config_document(
            self.db_path,
            script=[{"action": "none", "reply": "合成回答：无记忆桥也继续。"}],
            purposes=["consult"],
            memory={
                "timezone": "Asia/Shanghai",
                "recall": {
                    "enabled": True,
                    "max_items": 4,
                    "budget_units": 2048,
                    "timeout_seconds": 1.5,
                },
            },
        )
        process, info = self._start(document)
        recall = info["capabilities"]["memory"]["recall"]
        self.assertFalse(recall["bridge_configured"])
        self.assertFalse(recall["configured"])
        self.assertIn("memory_bridge", recall["missing"])
        turn_id, result = self._turn(
            process, info, text="今天天气怎么样", request_id="rb-recall-no-bridge"
        )
        self.assertEqual("completed", result["state"], result)
        self.assertNotIn("memory_recall", self._context_for_turn(turn_id))

    def test_related_previous_turn_reaches_the_content_context(self):
        self._seed_source()
        process, info = self._start(self._document())
        self.assertTrue(info["capabilities"]["memory"]["recall"]["configured"])
        turn_id, result = self._turn(
            process, info, text="你记得我喜欢什么颜色吗", request_id="rb-recall-1"
        )
        self.assertEqual("completed", result["state"], result)
        document = self._context_for_turn(turn_id)
        self.assertIn("memory_recall", document)
        self.assertEqual("ok", document["memory_recall"]["status"])
        self.assertEqual("user", document["memory_recall"]["items"][0]["subject"])
        self.assertIn("我喜欢红色", document["memory_recall"]["items"][0]["content"])
        self.assertNotIn(EVENT_REF, json.dumps(document, ensure_ascii=False))
        events = [event for event in self._events(turn_id) if event["kind"] == "memory_recall"]
        self.assertTrue(events)
        self.assertEqual("ok", events[0]["payload"]["status"])
        self.assertEqual("[]", self.scalar(
            "SELECT memory_hits_json FROM context_snapshots WHERE context_kind='consult' "
            "ORDER BY created_at DESC LIMIT 1"
        ))

    def test_natural_questions_use_one_literal_focus_prefix(self):
        self._seed_source()
        document = self._document(
            behavior="natural",
            script=[
                {"action": "none", "reply": "合成回答：自然问句一。"},
                {"action": "none", "reply": "合成回答：自然问句二。"},
            ],
        )
        process, info = self._start(document)
        for text, key in (
            ("你记得我喜欢什么颜色吗", "rb-natural-address"),
            ("我喜欢什么颜色", "rb-natural-plain"),
        ):
            turn_id, result = self._turn(process, info, text=text, request_id=key)
            self.assertEqual("completed", result["state"], (result, process.stderr_lines))
            document = self._context_for_turn(turn_id)
            self.assertIn("memory_recall", document, (text, document))
            self.assertTrue(document["memory_recall"]["items"], text)
            self.assertEqual(
                "focus_prefix",
                document["_budget_view"]["memory_recall"]["query_variant"],
                text,
            )
            events = [
                event for event in self._events(turn_id)
                if event["kind"] == "memory_recall"
            ]
            self.assertEqual("partial", events[-1]["payload"]["status"], text)

    def test_no_match_does_not_inject(self):
        process, info = self._start(self._document(behavior="no_match"))
        turn_id, result = self._turn(
            process, info, text="今天天气怎么样", request_id="rb-recall-empty"
        )
        self.assertEqual("completed", result["state"], result)
        document = self._context_for_turn(turn_id)
        self.assertNotIn("memory_recall", document)
        events = [event for event in self._events(turn_id) if event["kind"] == "memory_recall"]
        self.assertEqual("no_match", events[0]["payload"]["status"])

    def test_incomplete_is_not_reported_as_no_match(self):
        process, info = self._start(self._document(behavior="incomplete"))
        turn_id, result = self._turn(
            process, info, text="你记得我喜欢什么颜色吗", request_id="rb-recall-incomplete"
        )
        self.assertEqual("completed", result["state"], result)
        events = [event for event in self._events(turn_id) if event["kind"] == "memory_recall"]
        self.assertEqual("incomplete", events[0]["payload"]["status"])
        self.assertNotIn("memory_recall", self._context_for_turn(turn_id))

    def test_timeout_does_not_block_the_turn(self):
        process, info = self._start(self._document(behavior="timeout", timeout=0.4))
        turn_id, result = self._turn(
            process, info, text="你记得我喜欢什么颜色吗", request_id="rb-recall-timeout"
        )
        self.assertEqual("completed", result["state"], result)
        events = [event for event in self._events(turn_id) if event["kind"] == "memory_recall"]
        self.assertEqual("timeout", events[0]["payload"]["status"])

    def test_auto_and_tool_duplicate_is_counted_once(self):
        self._seed_source()
        log_path = str(self.tmp_path / "recall-calls.log")
        document = self._document(
            behavior="dedupe",
            model_protocol="collab.v2",
            log_path=log_path,
            script=[
                {
                    "action": "call_tool",
                    "tool_call": {
                        "name": "memory.search",
                        "arguments": {"query": "喜欢"},
                    },
                },
                {"action": "final", "reply": "合成回答：只使用一次同源材料。"},
            ],
        )
        process, info = self._start(document)
        turn_id, result = self._turn(
            process, info, text="我喜欢什么", request_id="rb-recall-dedupe"
        )
        self.assertEqual("completed", result["state"], result)
        # The latest content Context is the post-tool one; the tool result
        # already carries the same ref, so automatic recall must not repeat it.
        self.assertNotIn("memory_recall", self._context_for_turn(turn_id))
        events = [event for event in self._events(turn_id) if event["kind"] == "memory_recall"]
        self.assertTrue(Path(log_path).exists(), (events, process.stderr_lines))
        self.assertEqual("incomplete", events[-1]["payload"]["status"], Path(log_path).read_text(encoding="utf-8"))
        calls = [
            line.strip()
            for line in Path(log_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(1, calls.count("recall"), calls)
        self.assertEqual(1, calls.count("search"), calls)

    def test_expression_stage_does_not_recall(self):
        self._seed_source()
        log_path = str(self.tmp_path / "recall-expression.log")
        document = self._document(
            behavior="ok",
            model_protocol="collab.v2",
            log_path=log_path,
            script=[
                {"action": "final", "reply": "第一版回答。"},
                {"action": "final", "reply": "表达阶段回答。"},
            ],
        )
        document["conversation"] = {"sdrpe": {"mode": "on"}}
        process, info = self._start(document)
        turn_id, result = self._turn(
            process, info, text="我喜欢什么", request_id="rb-recall-expression"
        )
        self.assertEqual("completed", result["state"], result)
        calls = [
            line.strip()
            for line in Path(log_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(1, calls.count("recall"), calls)
        events = [event for event in self._events(turn_id) if event["kind"] == "memory_recall"]
        self.assertEqual(1, len(events), events)

    def test_settings_reload_applies_enabled_to_the_next_turn(self):
        self._seed_source()
        document = self._document(enabled=False)
        process, info = self._start(document)
        self.assertFalse(info["capabilities"]["memory"]["recall"]["enabled"])
        document["memory"]["recall"]["enabled"] = True
        process.config_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        applied = process.request("settings.reload")
        self.assertTrue(applied["ok"], applied)
        after = process.request("app.info")["result"]
        self.assertTrue(after["capabilities"]["memory"]["recall"]["enabled"])
        self.assertTrue(after["capabilities"]["memory"]["recall"]["configured"])
        turn_id, result = self._turn(
            process, after, text="你记得我喜欢什么颜色吗", request_id="rb-recall-reload"
        )
        self.assertEqual("completed", result["state"], result)
        document = self._context_for_turn(turn_id)
        self.assertIn("memory_recall", document)

    def test_invalid_timezone_is_not_applied(self):
        process, info = self._start(self._document(enabled=False))
        document = self._document(enabled=True)
        document["memory"]["timezone"] = "Mars/Base"
        process.config_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        applied = process.request("settings.reload")
        self.assertFalse(applied["ok"], applied)
        after = process.request("app.info")["result"]
        self.assertFalse(after["capabilities"]["memory"]["recall"]["enabled"])

    def test_version_mismatch_is_not_injected(self):
        self._seed_source(external_revision=2)
        process, info = self._start(self._document())
        turn_id, result = self._turn(
            process, info, text="你记得我喜欢什么颜色吗", request_id="rb-recall-version"
        )
        self.assertEqual("completed", result["state"], result)
        events = [event for event in self._events(turn_id) if event["kind"] == "memory_recall"]
        self.assertEqual("incomplete", events[0]["payload"]["status"])
        self.assertNotIn("memory_recall", self._context_for_turn(turn_id))

    # ------------------------------------------------------------ R-C1 time

    def _zone(self):
        from quintessence_pcs.domains.memory.time_window import resolve_zone

        return resolve_zone("Asia/Shanghai")

    def _observed_at(self, *, day_offset: int = 0, hour: int = 10) -> str:
        zone = self._zone()
        day = datetime.now(zone).date() + timedelta(days=day_offset)
        local = datetime.combine(day, dt_time(hour), tzinfo=zone)
        return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _midnight(self, *, day_offset: int = 0) -> str:
        zone = self._zone()
        day = datetime.now(zone).date() + timedelta(days=day_offset)
        return datetime.combine(day, dt_time(0), tzinfo=zone).isoformat()

    def _local_message_time(self, observed_at: str) -> str:
        moment = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        return moment.astimezone(self._zone()).isoformat()

    def _requests(self, path: str) -> list[dict]:
        return [
            json.loads(line)
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line
        ]

    def test_time_only_event_query_uses_window_without_until(self):
        observed = self._observed_at()
        self._seed_source(
            body="我昨天去了招聘会。",
            observed_at=observed,
            pcs_time={
                "event_type": "occurred",
                "event_time": self._midnight(day_offset=-1),
                "planned_for": None,
                "time_precision": "day",
                "time_basis": "relative_day",
                "message_time_basis": "observed_at",
            },
        )
        request_log = str(self.tmp_path / "rc1-event-requests.log")
        process, info = self._start(
            self._document(behavior="time", request_log_path=request_log)
        )
        turn_id, result = self._turn(
            process, info, text="昨天发生了什么", request_id="rc1-event-window"
        )
        self.assertEqual("completed", result["state"], result)
        windows = [
            request
            for request in self._requests(request_log)
            if request.get("action") == "time_window"
        ]
        self.assertEqual(1, len(windows), windows)
        self.assertIsNone(windows[0]["until"], windows)
        self.assertEqual(
            datetime.fromisoformat(self._midnight(day_offset=-1)),
            datetime.fromisoformat(windows[0]["since"]),
        )
        document = self._context_for_turn(turn_id)
        item = document["memory_recall"]["items"][0]
        self.assertEqual("occurred", item["event_type"])
        self.assertEqual(self._midnight(day_offset=-1), item["event_time"])
        self.assertEqual(self._local_message_time(observed), item["message_time"])
        self.assertEqual("observed_at", item["message_time_basis"])
        self.assertNotIn("occurred_at", item)
        self.assertEqual(
            "event",
            document["_budget_view"]["memory_recall"]["time"]["intent"],
        )

    def test_time_only_message_query_uses_until(self):
        observed = self._observed_at()
        self._seed_source(body="我去了招聘会。", observed_at=observed)
        request_log = str(self.tmp_path / "rc1-message-requests.log")
        process, info = self._start(
            self._document(behavior="time", request_log_path=request_log)
        )
        turn_id, result = self._turn(
            process, info, text="今天聊了什么", request_id="rc1-message-window"
        )
        self.assertEqual("completed", result["state"], result)
        windows = [
            request
            for request in self._requests(request_log)
            if request.get("action") == "time_window"
        ]
        self.assertEqual(1, len(windows), windows)
        self.assertIsNotNone(windows[0]["until"], windows)
        item = self._context_for_turn(turn_id)["memory_recall"]["items"][0]
        self.assertEqual("message", self._context_for_turn(turn_id)["_budget_view"]["memory_recall"]["time"]["intent"])
        self.assertEqual("unknown", item["event_type"])
        self.assertIsNone(item["event_time"])

    def test_future_plan_query_does_not_open_a_window(self):
        self._seed_source(
            body="我明天去招聘会。",
            observed_at=self._observed_at(),
            pcs_time={
                "event_type": "planned",
                "event_time": None,
                "planned_for": self._midnight(day_offset=1),
                "time_precision": "day",
                "time_basis": "relative_day",
                "message_time_basis": "observed_at",
            },
        )
        request_log = str(self.tmp_path / "rc1-future-requests.log")
        process, info = self._start(
            self._document(behavior="time", request_log_path=request_log)
        )
        turn_id, result = self._turn(
            process, info, text="明天有什么安排", request_id="rc1-future"
        )
        self.assertEqual("completed", result["state"], result)
        self.assertFalse(
            [
                request
                for request in self._requests(request_log)
                if request.get("action") == "time_window"
            ],
            self._requests(request_log),
        )
        events = [
            event for event in self._events(turn_id) if event["kind"] == "memory_recall"
        ]
        self.assertTrue(events[0]["payload"]["time"]["unsupported"], events)
        self.assertNotIn("memory_recall", self._context_for_turn(turn_id))

    def test_planned_material_never_enters_an_event_window(self):
        self._seed_source(
            body="我明天去招聘会。",
            observed_at=self._observed_at(),
            pcs_time={
                "event_type": "planned",
                "event_time": None,
                "planned_for": self._midnight(day_offset=1),
                "time_precision": "day",
                "time_basis": "relative_day",
                "message_time_basis": "observed_at",
            },
        )
        process, info = self._start(self._document(behavior="time"))
        turn_id, result = self._turn(
            process, info, text="昨天发生了什么", request_id="rc1-planned-window"
        )
        self.assertEqual("completed", result["state"], result)
        events = [
            event for event in self._events(turn_id) if event["kind"] == "memory_recall"
        ]
        self.assertEqual("incomplete", events[0]["payload"]["status"], events)
        self.assertEqual(1, events[0]["payload"]["time"]["time_not_occurred"])
        self.assertNotIn("memory_recall", self._context_for_turn(turn_id))

    def test_old_row_fallback_is_visible_to_the_model(self):
        self._seed_source(body="我去了招聘会。", observed_at=None)
        process, info = self._start(self._document(behavior="time"))
        turn_id, result = self._turn(
            process, info, text="今天聊了什么", request_id="rc1-fallback"
        )
        self.assertEqual("completed", result["state"], result)
        item = self._context_for_turn(turn_id)["memory_recall"]["items"][0]
        self.assertEqual("recorded_at_fallback", item["message_time_basis"])
        self.assertEqual("unknown", item["event_type"])
        self.assertIsNone(item["event_time"])


    def test_time_rollback_switch_disables_windows(self):
        request_log = str(self.tmp_path / "rc1-disabled-requests.log")
        document = self._document(behavior="time", request_log_path=request_log)
        document["memory"]["time"] = {"enabled": False}
        process, info = self._start(document)
        self.assertFalse(info["capabilities"]["memory"]["recall"]["time"]["enabled"])
        turn_id, result = self._turn(
            process, info, text="昨天发生了什么", request_id="rc1-disabled"
        )
        self.assertEqual("completed", result["state"], result)
        self.assertFalse(
            [
                request
                for request in self._requests(request_log)
                if request.get("action") == "time_window"
            ],
            self._requests(request_log),
        )
        events = [
            event for event in self._events(turn_id) if event["kind"] == "memory_recall"
        ]
        self.assertFalse(
            (events[0]["payload"].get("time") or {}).get("windows"),
            events,
        )


if __name__ == "__main__":
    unittest.main()
