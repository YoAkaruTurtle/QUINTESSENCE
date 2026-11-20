"""Resident desktop wake using an offline provider and a synthetic ready folder."""

from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests.desktop import support  # installs the sidecar import path
from tests.domain.model_support import FakeProvider, ScriptedResponse

import framing
from dreaming_host import DreamingHost
from instance_manager import CONFIG, default_document, prepare
from pcs_bridge import Bridge
from quintessence_pcs.domains.cognition.questions import QuestionAccess, QuestionService
from quintessence_pcs.domains.refs import EventRef
from quintessence_pcs.runtime.dreaming_journal import CalendarJournalStore, JournalPeriod
from quintessence_pcs.store import CognitionStore


class _Clock:
    def __init__(self):
        self.tick = 0.0
        self.utc = datetime(2026, 2, 2, 12, tzinfo=UTC)

    def monotonic(self):
        return self.tick

    def now(self):
        return self.utc

    def advance(self, seconds):
        self.tick += seconds
        self.utc += timedelta(seconds=seconds)


class _Resident:
    def __init__(self, config, clock):
        server_read, client_write = os.pipe()
        client_read, server_write = os.pipe()
        self.stdin = os.fdopen(client_write, "wb", buffering=0)
        self.stdout = os.fdopen(client_read, "rb", buffering=0)
        self.server_in = os.fdopen(server_read, "rb")
        self.server_out = os.fdopen(server_write, "wb")
        self.next_id = 0
        with patch(
            "pcs_bridge.DreamingHost",
            side_effect=lambda current, *, generation: DreamingHost(
                current, generation=generation, monotonic=clock.monotonic, clock=clock.now,
            ),
        ):
            self.bridge = Bridge(str(config), self.server_in, self.server_out)
        self.thread = threading.Thread(target=self.bridge.serve, daemon=True)
        self.thread.start()

    def request(self, method, params=None):
        self.next_id += 1
        request_id = f"synthetic-{self.next_id}"
        self.stdin.write(framing.encode_frame({
            "v": "desktop.v1", "id": request_id, "method": method, "params": params or {},
        }))
        reply = framing.decode_json(framing.read_frame(self.stdout))
        if reply["id"] != request_id:
            raise AssertionError(reply)
        return reply

    def close(self):
        if self.stdin.closed:
            return
        self.stdin.close()
        self.thread.join(timeout=12)
        if self.thread.is_alive():
            raise AssertionError("resident bridge did not stop")
        self.stdout.close()
        self.server_in.close()
        self.server_out.close()


class DreamingResidentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dreaming-resident-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        self.root.mkdir()
        self.provider = FakeProvider()
        self.addCleanup(self.provider.close)
        self.clock = _Clock()
        env = patch.dict(os.environ, {"QUINTESSENCE_DESKTOP_MODEL_KEY": "synthetic-resident"})
        env.start()
        self.addCleanup(env.stop)
        doc = default_document()
        doc["destination"]["manifest"]["base_url"] = self.provider.url
        prepare(self.root, doc)
        (self.root / "pcs.sqlite-wal").write_bytes(b"")
        (self.root / "pcs.sqlite-shm").write_bytes(b"synthetic old sidecar")
        (self.root / "original.txt").write_text("synthetic original data", encoding="utf-8")
        self.before = self.legacy_files()
        tick = patch("pcs_bridge._DREAMING_TICK_SECONDS", 0.04)
        tick.start()
        self.addCleanup(tick.stop)

    def legacy_files(self):
        return {
            str(path.relative_to(self.root)): (
                path.stat().st_ino, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in self.root.rglob("*") if path.is_file() and path.relative_to(self.root).parts[0] != "dreaming"
        }

    def resident(self):
        resident = _Resident(self.root / CONFIG, self.clock)
        self.addCleanup(resident.close)
        info = resident.request("app.info")["result"]
        self.assertTrue(info["host"]["ready"], info)
        return resident

    def toggle(self, resident, enabled, *, journals=False):
        info = resident.request("app.info")["result"]
        method = "dreaming.configure_journals" if journals else "dreaming.configure"
        response = resident.request(method, {
            "session_id": info["session_id"],
            "destination_id": info["destination"]["destination_id"],
            "enabled": enabled,
        })
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["journals_enabled" if journals else "enabled"], enabled)
        self.wait_for(
            lambda: resident.request("app.info")["result"]["dreaming"],
            lambda view: view["state"] != "foreground",
        )

    def response(self, decision, *, delay=0):
        self.provider.enqueue(ScriptedResponse(delay=delay, body={
            "model": "deepseek-flash",
            "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(decision),
            }}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }))

    def wait_for(self, read, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = read()
            if predicate(result):
                return result
            time.sleep(0.02)
        self.fail(f"unexpected resident state {result!r}")

    def attempts(self):
        with closing(sqlite3.connect(self.root / "dreaming" / "wake.sqlite3")) as connection:
            return connection.execute(
                "SELECT status, request_hash, proposal_json FROM wake_attempts ORDER BY reserved_at",
            ).fetchall()

    def test_registered_idle_bridge_uses_explicit_grant_with_no_legacy_writes(self):
        resident = self.resident()
        self.clock.advance(901)
        self.wait_for(lambda: resident.request("app.info")["result"]["dreaming"], lambda view: view["can_configure"])
        self.assertEqual(self.provider.hits, 0)
        self.toggle(resident, True)
        self.response({
            "decision": "propose", "action": "Read the unfinished draft",
            "reason": "Explore an idle topic", "effect": "Suggest a local follow-up",
        })
        self.clock.advance(899)
        time.sleep(0.1)
        self.assertEqual(self.provider.hits, 0)
        self.clock.advance(2)
        rows = self.wait_for(self.attempts, lambda value: bool(value) and value[0][0] == "proposed")
        self.assertEqual(self.provider.hits, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][2])["action"], "Read the unfinished draft")
        wire = json.loads(self.provider.requests[0]["body"])
        manifest = default_document()["destination"]["manifest"]
        self.assertEqual(wire["max_tokens"], manifest["max_output_tokens_per_call"])
        self.assertNotIn("tools", wire)
        self.assertIn("idle reflection", wire["messages"][0]["content"])
        resident.close()
        self.assertEqual(self.legacy_files(), self.before)

        resident = self.resident()
        self.clock.advance(901)
        self.wait_for(lambda: resident.request("app.info")["result"]["dreaming"], lambda view: view["reason"] == "wake_budget")
        self.assertEqual(self.provider.hits, 1)
        self.toggle(resident, False)
        self.clock.advance(2000)
        time.sleep(0.1)
        self.assertEqual(self.provider.hits, 1)
        resident.close()
        self.assertEqual(self.legacy_files(), self.before)

    def test_larger_chat_output_ceiling_requires_a_new_grant_and_keeps_the_cooldown(self):
        config = self.root / CONFIG
        document = json.loads(config.read_text(encoding="utf-8"))
        chat_ceiling = 12288
        document["destination"]["manifest"]["max_output_tokens_per_call"] = chat_ceiling
        config.write_text(json.dumps(document), encoding="utf-8")
        before = self.legacy_files()
        current_manifest = DreamingHost._manifest

        def legacy_manifest(host):
            manifest = current_manifest(host)
            return replace(manifest, max_output_tokens_per_call=512) if manifest is not None else None

        with patch.object(DreamingHost, "_manifest", legacy_manifest):
            resident = self.resident()
            self.toggle(resident, True)
            self.response({"decision": "wait"})
            self.clock.advance(901)
            self.wait_for(self.attempts, lambda rows: rows and rows[0][0] == "noop")
            self.assertEqual(json.loads(self.provider.requests[0]["body"])["max_tokens"], 512)
            resident.close()

        resident = self.resident()

        def dreaming():
            return resident.request("app.info")["result"]["dreaming"]

        view = self.wait_for(dreaming, lambda value: value["can_configure"] and value["state"] == "disabled")
        self.assertFalse(view["enabled"])
        self.assertEqual(view["reason"], "model_not_authorized")
        self.assertEqual(self.provider.hits, 1)

        self.toggle(resident, True)
        self.clock.advance(901)
        self.wait_for(dreaming, lambda value: value["reason"] == "wake_budget")
        self.assertEqual(self.provider.hits, 1)

        self.response({"decision": "wait"})
        self.clock.advance(901)
        self.wait_for(self.attempts, lambda rows: len(rows) == 2 and rows[1][0] == "noop")
        self.assertEqual(json.loads(self.provider.requests[1]["body"])["max_tokens"], chat_ceiling)
        resident.close()
        self.assertEqual(self.legacy_files(), before)

    def test_idle_reflection_no_longer_uses_six_record_history_stream(self):
        config = json.loads((self.root / CONFIG).read_text(encoding="utf-8"))
        authority = config["host"]
        # Seed a permitted old experience through the existing legacy writer.
        for name in ("pcs.sqlite-wal", "pcs.sqlite-shm"):
            (self.root / name).unlink()
        with closing(CognitionStore.open(self.root / "pcs.sqlite")) as store:
            old = (self.clock.utc - timedelta(days=3)).isoformat(timespec="seconds").replace("+00:00", "Z")
            source = store.register_input(
                namespace=authority["namespace"], origin_kind="user_statement",
                body="The synthetic build is waiting for a review.",
                privacy_class="private", retention="standard", recorded_at=old,
            )
            ref = {
                "ref_type": "collab_input", "namespace": authority["namespace"],
                "input_id": source.input_id, "content_version": source.content_version,
            }
            store.register_interaction(
                namespace=authority["namespace"], mode="consult", activation_kind="user_input",
                host_instance=authority["host_instance"], access_ref=authority["access_ref"],
                policy_ref=authority["policy_ref"], input_refs=[ref], result="answered",
            )
        before = self.legacy_files()
        resident = self.resident()
        self.toggle(resident, True)
        self.response({"decision": "wait"})
        self.clock.advance(901)
        self.wait_for(self.attempts, lambda rows: bool(rows) and rows[0][0] == "noop")
        wire = json.loads(self.provider.requests[0]["body"])
        self.assertNotIn("The synthetic build is waiting for a review.", wire["messages"][0]["content"])
        with closing(sqlite3.connect(self.root / "dreaming" / "wake.sqlite3")) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM dreaming_history_progress WHERE stream = 'deep'",
            ).fetchone())
        resident.close()
        self.assertEqual(self.legacy_files(), before)

    def test_calendar_day_with_no_chat_is_written_after_restart_and_reaches_consult(self):
        for name in ("pcs.sqlite-wal", "pcs.sqlite-shm"):
            (self.root / name).unlink()
        before = self.legacy_files()
        resident = self.resident()
        self.wait_for(
            lambda: resident.request("app.info")["result"]["dreaming"],
            lambda status: status["can_configure_journals"],
        )
        self.toggle(resident, True, journals=True)
        self.assertFalse(resident.request("app.info")["result"]["dreaming"]["enabled"])
        resident.close()
        self.assertEqual(self.legacy_files(), before)

        self.clock.advance(86400)
        journal_text = "I reflected on the synthetic calendar. " * 120 + "End of this calendar entry."
        self.response({"text": journal_text, "tags": ["calendar", "reflection"]})
        resident = self.resident()
        self.clock.advance(6)
        root = self.root / "dreaming"
        journal_owner = json.loads((root / "owner.json").read_text(encoding="utf-8"))
        journals = CalendarJournalStore(root, instance_id=journal_owner["instance_id"])
        last_day = JournalPeriod.containing("day", (self.clock.utc - timedelta(days=1)).date(), "Asia/Shanghai")
        entry = self.wait_for(lambda: journals.get(last_day), lambda value: value is not None)
        self.assertEqual(entry.text, journal_text)
        self.assertEqual(self.provider.hits, 1)
        resident.close()
        self.assertEqual(self.legacy_files(), before)

        resident = self.resident()
        info = resident.request("app.info")["result"]
        session = {"session_id": info["session_id"]}
        confirmed = resident.request("app.confirm_destination", {
            **session, "destination_id": info["destination"]["destination_id"],
        })
        self.assertTrue(confirmed["ok"], confirmed)
        created = resident.request("conversation.create", {**session, "mode": "chat"})
        self.assertTrue(created["ok"], created)
        self.response("The diary can answer this.")
        sent = resident.request("conversation.send", {
            **session, "conversation_id": created["result"]["conversation"]["conversation_id"],
            "client_request_id": "journal-recall-synthetic", "text": "2026年2月2日有什么重要的事吗？",
            "confirmed_destination_id": info["destination"]["destination_id"],
        })
        self.assertTrue(sent["ok"], sent)
        self.wait_for(lambda: self.provider.hits, lambda hits: hits >= 2)
        payload = json.loads(self.provider.requests[1]["body"])
        self.assertIn(journal_text, "\n".join(message.get("content") or "" for message in payload["messages"]))
        resident.close()

    def test_explicit_question_review_survives_restart_and_completes_locally(self):
        authority = json.loads((self.root / CONFIG).read_text(encoding="utf-8"))["host"]
        namespace = authority["namespace"]
        for name in ("pcs.sqlite-wal", "pcs.sqlite-shm"):
            (self.root / name).unlink()
        with closing(CognitionStore.open(self.root / "pcs.sqlite")) as store:
            evidence = store.ingest_event(
                namespace=namespace, source_kind="host_observation", source_instance="synthetic",
                source_event_id=str(uuid4()), event_type="SyntheticObservation",
                payload={"text": "One synthetic event"}, privacy_class="private",
            )
            access = QuestionAccess(
                namespace, authority["host_instance"], authority["access_ref"], frozenset({"private"}),
                frozenset({"question.create", "question.read"}), frozenset({"cognition.question"}),
            )
            question = QuestionService(store).create(
                access, context_id=store.capture_snapshot(namespace).context_id,
                request_id=str(uuid4()), question="What remains to verify?",
                origin="user_requested", origin_refs=[EventRef(namespace, evidence.event_id).to_dict()],
                privacy_class="private",
            ).view
        before = self.legacy_files()
        resident = self.resident()
        self.toggle(resident, True)
        info = resident.request("app.info")["result"]
        request = {
            "session_id": info["session_id"], "destination_id": info["destination"]["destination_id"],
            "question": question.ref.to_dict(),
        }
        queued = resident.request("dreaming.review_question", request)
        self.assertTrue(queued["ok"], queued)
        resident.close()

        resident = self.resident()
        self.response({"decision": "wait", "recap": "The check remains open."})
        self.clock.advance(901)
        self.wait_for(self.attempts, lambda rows: bool(rows) and rows[0][0] == "noop")
        wire = json.loads(self.provider.requests[0]["body"])
        self.assertIn("manual_review", wire["messages"][0]["content"])
        self.assertIn("What remains to verify?", wire["messages"][0]["content"])
        with closing(sqlite3.connect(self.root / "dreaming" / "wake.sqlite3")) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM dreaming_manual_review").fetchone())
        resident.close()
        self.assertEqual(self.legacy_files(), before)

    def test_queued_foreground_preempts_and_discards_late_response(self):
        resident = self.resident()
        self.toggle(resident, True)
        self.response({
            "decision": "propose", "action": "Late action",
            "reason": "Late result must not publish", "effect": "Local review only",
        }, delay=1.2)
        self.clock.advance(901)
        self.assertTrue(self.provider.request_event.wait(8))
        view = resident.request("app.info")["result"]["dreaming"]
        self.assertEqual(view["state"], "thinking")
        self.toggle(resident, False)
        time.sleep(1.3)
        rows = self.attempts()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0][1])
        self.assertIsNone(rows[0][2])
        resident.close()
        self.assertEqual(self.legacy_files(), self.before)
