"""Real folder lifecycle regressions; all private data is synthetic and external."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "src"), str(REPO / "apps/desktop/sidecar")]
from instance_manager import (
    CONFIG, DREAMING, DREAMING_OWNER, MANIFEST, InstanceError,
    _publish_dreaming_directory, prepare, prepare_dreaming_root,
)
from host_config import load_config
from quintessence_pcs.store import CognitionStore
from quintessence_pcs.domains.persona.profiles import PersonaProfileService
from tests.desktop.support import (
    BridgeProcess, SIDECAR_DIR, SRC_DIR, config_document, review_proposal, seed_work,
)
from tests.store.support import append, RECORD_ID


class InstanceFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="folder-instance-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "instance"
        self.root.mkdir()

    def metadata(self):
        return json.loads((self.root / MANIFEST).read_text(encoding="utf-8"))

    def save_metadata(self, manifest):
        (self.root / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

    def expect(self, code):
        with self.assertRaises(InstanceError) as caught:
            prepare(self.root)
        self.assertEqual(code, caught.exception.code)

    def _snapshot(self, *, include_dreaming=False):
        files = {}
        for path in self.root.rglob("*"):
            relative = path.relative_to(self.root)
            if include_dreaming or relative.parts[0] != DREAMING:
                stat = path.stat()
                identity = (stat.st_dev, stat.st_ino)
                files[relative] = (
                    (*identity, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
                    if path.is_file() else identity
                )
        return files

    def _instance_cli(self, action="--prepare-dreaming-startup", *, root=None, flags=()):
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(SRC_DIR), str(SIDECAR_DIR), env.get("PYTHONPATH", "")]
        ).strip(os.pathsep)
        result = subprocess.run(
            [sys.executable, "-B", str(SIDECAR_DIR / "pcs_bridge.py"),
             action, str(self.root if root is None else root), *flags],
            env=env, input="", capture_output=True, encoding="utf-8", timeout=30, check=False,
        )
        self.assertTrue(result.stdout, result.stderr)
        return result.returncode, json.loads(result.stdout)

    def _bridge(self, *, extra_env=None):
        bridge = BridgeProcess(config_path=self.root / CONFIG, extra_env=extra_env)
        self.addCleanup(bridge.close)
        bridge.wait_ready()
        return bridge

    def _prepare_scripted_folder(self, *, external=True, review_script=None):
        template = config_document(
            self.root / "pcs.sqlite", script=[{"action": "final", "reply": "合成答复"}],
            external=external, purposes=["consult", "reasoning", "review", "maintenance"],
            desktop={"managed": False, "provision": False},
            review_script=review_script,
        )
        template["destination"]["manifest"].update(
            thinking="enabled", reasoning_effort="high",
            protocol_version="EXP-01-THINKING-CANDIDATE-v1",
            max_output_tokens_per_call=8192,
        )
        template["destination"]["manifest"].pop("temperature")
        first = prepare(self.root, template)
        config = load_config(first["config_path"])
        # Seed through the normal old writable runtime before taking the snapshot.
        from runtime import DesktopRuntime
        writer = DesktopRuntime(config)
        writer.close()
        return first

    def test_dreaming_upgrade_and_restart_preserve_legacy_file_identity_and_bytes(self):
        first = self._prepare_scripted_folder()
        (self.root / "logs").mkdir()
        (self.root / "logs/desktop.log").write_bytes(b"synthetic log\n" * 150_000)
        (self.root / "logs/desktop.1.log").write_bytes(b"synthetic previous log\n")
        (self.root / "ui-preferences.json").write_bytes(b'{"conversation_ui":{}}\n')
        (self.root / "host-settings.json").write_bytes(b'{"schema":"synthetic-settings"}\n')
        for database in ("pcs.sqlite", "memory/memory.sqlite3"):
            (self.root / f"{database}-wal").write_bytes(b"")
            (self.root / f"{database}-shm").write_bytes(bytes(32_768))
        before = self._snapshot()

        for _ in range(2):
            code, response = self._instance_cli()
            self.assertEqual(0, code, response)
            self.assertTrue(response["result"]["logs_ready"])
            self.assertEqual(first["instance_id"], json.loads(
                (self.root / DREAMING / DREAMING_OWNER).read_text(encoding="utf-8")
            )["instance_id"])
            bridge = self._bridge()
            info = bridge.request("app.info")["result"]
            self.assertTrue(info["host"]["ready"], info["host"])
            self.assertEqual(first["namespace"], info["host"]["namespace"])
            session = {"session_id": info["session_id"]}
            for method, extra in (
                ("bridge.ping", None),
                ("conversation.list", {}),
                ("profile.list", {}),
                ("cognition.candidates.list", {}),
                ("review.work.list", {}),
            ):
                with self.subTest(method=method):
                    response = bridge.request(method, None if extra is None else {**session, **extra})
                    self.assertTrue(response["ok"], response)
            browse = bridge.request("memory.search", {**session, "query": "", "limit": 1})
            self.assertTrue(browse["ok"], browse)
            synthetic_id = "11111111-1111-4111-8111-111111111111"
            for method, extra in (
                ("review.candidate.detail", {"candidate_id": synthetic_id, "artifact_version": 1}),
                ("review.result", {"interaction_id": synthetic_id}),
            ):
                with self.subTest(method=method):
                    response = bridge.request(method, {**session, **extra})
                    self.assertFalse(response["ok"], response)
                    self.assertEqual("NOT_FOUND", response["error"]["code"])
            other = self._bridge()
            self.assertEqual("DATA_LOCKED", other.request("app.info")["result"]["host"]["reason_code"])
            other.close()
            bridge.close()
            self.assertEqual(before, self._snapshot())
        self.assertEqual([self.root], list(self.base.iterdir()))

    def test_pending_journals_refuse_before_bootstrap_or_legacy_mutation(self):
        prepare(self.root)
        for name in ("pcs.sqlite-wal", "pcs.sqlite-journal", "memory/memory.sqlite3-wal"):
            with self.subTest(journal=name):
                journal = self.root / name
                journal.write_bytes(b"synthetic pending journal")
                before = self._snapshot(include_dreaming=True)
                for action in ("--prepare-dreaming-startup", "--connect-instance"):
                    code, response = self._instance_cli(action)
                    self.assertNotEqual(0, code)
                    self.assertEqual("INSTANCE_RECOVERY_REQUIRED", response["error"]["code"])
                bridge = self._bridge()
                info = bridge.request("app.info")["result"]
                self.assertEqual("INSTANCE_RECOVERY_REQUIRED", info["host"]["reason_code"])
                bridge.close()
                self.assertEqual(before, self._snapshot(include_dreaming=True))
                journal.unlink()

    def test_cold_start_respects_the_scope_recall_restore_fence_and_binding(self):
        from scope_recall.core.restore import MARKER_FILENAME

        prepare(self.root)
        marker = self.root / "memory" / MARKER_FILENAME
        marker.write_bytes(b"synthetic pending restore")
        before = self._snapshot(include_dreaming=True)
        code, response = self._instance_cli()
        self.assertNotEqual(0, code)
        self.assertEqual("INSTANCE_RECOVERY_REQUIRED", response["error"]["code"])
        self.assertEqual(before, self._snapshot(include_dreaming=True))
        marker.unlink()

        from contextlib import closing
        with closing(sqlite3.connect(self.root / "memory/memory.sqlite3")) as connection:
            connection.execute("UPDATE instance_meta SET agent_id = 'synthetic:foreign'")
            connection.commit()
        before = self._snapshot(include_dreaming=True)
        code, response = self._instance_cli()
        self.assertNotEqual(0, code)
        self.assertEqual("INSTANCE_IDENTITY_MISMATCH", response["error"]["code"])
        bridge = self._bridge()
        self.assertEqual(
            "INSTANCE_IDENTITY_MISMATCH", bridge.request("app.info")["result"]["host"]["reason_code"]
        )
        bridge.close()
        self.assertEqual(before, self._snapshot(include_dreaming=True))

    def test_dreaming_directory_collision_preserves_the_raced_directory(self):
        first = prepare(self.root)
        destination = self.root / DREAMING
        raced_identity = None

        def create_competing_directory(staging, path):
            nonlocal raced_identity
            path.mkdir()
            raced_identity = path.stat().st_ino
            _publish_dreaming_directory(staging, path)

        with patch("instance_manager._publish_dreaming_directory", create_competing_directory):
            with self.assertRaises(InstanceError) as caught:
                prepare_dreaming_root(self.root, first["instance_id"])
        self.assertEqual("DREAMING_PATH_COLLISION", caught.exception.code)
        self.assertEqual(raced_identity, destination.stat().st_ino)
        self.assertEqual([], list(destination.iterdir()))
        self.assertEqual([], list(self.root.glob(".dreaming-*.pending")))
        code, response = self._instance_cli()
        self.assertNotEqual(0, code)
        self.assertEqual("DREAMING_PATH_COLLISION", response["error"]["code"])

    def test_dreaming_log_hard_link_cannot_route_rotation_into_legacy_log(self):
        first = prepare(self.root)
        directory = prepare_dreaming_root(self.root, first["instance_id"]) / "logs"
        directory.mkdir()
        original = self.root / "existing.log"
        original.write_bytes(b"synthetic legacy log")
        os.link(original, directory / "desktop.log")
        before = self._snapshot(include_dreaming=True)
        code, response = self._instance_cli()
        self.assertNotEqual(0, code)
        self.assertEqual("DREAMING_PATH_COLLISION", response["error"]["code"])
        self.assertEqual(before, self._snapshot(include_dreaming=True))

    def test_existing_folder_selection_uses_read_only_connection(self):
        code, created = self._instance_cli("--connect-instance")
        self.assertEqual(0, code, created)
        self.assertEqual("created", created["result"]["state"])
        before = self._snapshot(include_dreaming=True)
        code, connected = self._instance_cli("--connect-instance")
        self.assertEqual(0, code, connected)
        self.assertEqual("connected", connected["result"]["state"])
        self.assertEqual(created["result"]["instance_id"], connected["result"]["instance_id"])
        self.assertEqual(before, self._snapshot(include_dreaming=True))

        code, response = self._instance_cli(
            "--connect-instance",
            flags=("--adopt-legacy-by", "synthetic:operator",
                   "--adopt-legacy-reason", "synthetic repeat refusal"),
        )
        self.assertNotEqual(0, code)
        self.assertEqual("INSTANCE_ADOPTION_REFUSED", response["error"]["code"])
        self.assertEqual(before, self._snapshot(include_dreaming=True))

        target = self.base / "moved"
        shutil.copytree(self.root, target)
        code, moved = self._instance_cli("--connect-instance", root=target)
        self.assertEqual(0, code, moved)
        self.assertEqual("restored", moved["result"]["state"])
        self.assertEqual(connected["result"]["instance_id"], moved["result"]["instance_id"])
        self.assertEqual(before, self._snapshot(include_dreaming=True))

    def test_connection_never_prepares_unknown_lifecycle_state(self):
        prepare(self.root)
        manifest = self.metadata()
        manifest["components"]["memory"]["state"] = "unrecognized"
        self.save_metadata(manifest)
        before = self._snapshot(include_dreaming=True)
        code, response = self._instance_cli("--connect-instance")
        self.assertNotEqual(0, code)
        self.assertEqual("INSTANCE_METADATA_INVALID", response["error"]["code"])
        self.assertEqual(before, self._snapshot(include_dreaming=True))

    def test_dreaming_resumes_past_unknown_staging_without_overwriting_it(self):
        first = prepare(self.root)
        staged = self.root / (".dreaming-" + uuid.uuid4().hex + ".pending")
        staged.mkdir()
        (staged / DREAMING_OWNER).write_bytes(b"synthetic incomplete marker")
        before = self._snapshot()
        code, response = self._instance_cli()
        self.assertEqual(0, code, response)
        owner = self.root / DREAMING / DREAMING_OWNER
        self.assertEqual(first["instance_id"], json.loads(owner.read_bytes())["instance_id"])
        self.assertEqual(before, self._snapshot())

    def test_dreaming_does_not_claim_a_hard_linked_owner(self):
        first = prepare(self.root)
        directory = prepare_dreaming_root(self.root, first["instance_id"])
        os.link(directory / DREAMING_OWNER, self.root / "existing-owner.json")
        before = self._snapshot(include_dreaming=True)
        code, response = self._instance_cli()
        self.assertNotEqual(0, code)
        self.assertEqual("DREAMING_PATH_COLLISION", response["error"]["code"])
        self.assertEqual(before, self._snapshot(include_dreaming=True))

    def test_forged_manifest_uuid_does_not_override_pcs_registration(self):
        prepare(self.root)
        manifest = self.metadata()
        manifest["instance_id"] = str(uuid.uuid4())
        self.save_metadata(manifest)
        before = self._snapshot(include_dreaming=True)
        for action in ("--prepare-dreaming-startup", "--connect-instance"):
            code, response = self._instance_cli(action)
            self.assertNotEqual(0, code)
            self.assertEqual("INSTANCE_IDENTITY_MISMATCH", response["error"]["code"])
        bridge = self._bridge()
        self.assertEqual(
            "INSTANCE_IDENTITY_MISMATCH", bridge.request("app.info")["result"]["host"]["reason_code"]
        )
        bridge.close()
        self.assertEqual(before, self._snapshot(include_dreaming=True))

    def test_registered_folder_model_probe_does_not_promote_the_pcs(self):
        from tests.desktop.test_model_connection import _MockProvider

        provider = _MockProvider()
        self.addCleanup(provider.close)
        prepare(self.root, {
            "destination": {"manifest": {
                "base_url": f"http://127.0.0.1:{provider.port}/v1",
                "api_key_env": "Q_PCS_SYNTHETIC_MODEL_PROBE",
            }},
        })
        before = self._snapshot()
        bridge = self._bridge(extra_env={"Q_PCS_SYNTHETIC_MODEL_PROBE": "synthetic-offline-token"})
        info = bridge.request("app.info")["result"]
        result = bridge.request("model.test_connection", {"session_id": info["session_id"]})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["result"]["connected"])
        self.assertEqual(1, len(provider.requests))
        bridge.close()
        self.assertEqual(before, self._snapshot())

    def test_cold_review_returns_existing_result_and_defers_audited_detail(self):
        self._prepare_scripted_folder(external=False)
        work = seed_work(self.root / "pcs.sqlite")
        # Bind the scripted proposal to the fixture's real event.
        config = json.loads((self.root / CONFIG).read_bytes())
        config["destination"]["transport"]["review_script"] = [review_proposal(work["event_id"])]
        (self.root / CONFIG).write_text(json.dumps(config), encoding="utf-8")
        bridge = self._bridge()
        session = {"session_id": bridge.request("app.info")["result"]["session_id"]}
        generated = bridge.request("review.generate", {
            **session, "work_id": work["work_id"], "event_id": work["event_id"],
        })
        self.assertTrue(generated["ok"], generated)
        self.assertEqual("pending", generated["result"]["state"])
        bridge.close()

        before = self._snapshot()
        bridge = self._bridge()
        session = {"session_id": bridge.request("app.info")["result"]["session_id"]}
        result = bridge.request(
            "review.result", {**session, "interaction_id": generated["result"]["interaction_id"]},
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual("pending", result["result"]["state"])
        detail = bridge.request("review.candidate.detail", {
            **session, "candidate_id": generated["result"]["candidate_id"], "artifact_version": 1,
        })
        self.assertFalse(detail["ok"], detail)
        self.assertEqual("FOREGROUND_REQUIRED", detail["error"]["code"])
        self.assertFalse(detail["error"]["result_unknown"])
        bridge.close()
        self.assertEqual(before, self._snapshot())

    def test_explicit_folder_actions_signal_foreground_before_worker_starts(self):
        from pcs_bridge import Bridge

        bridge = Bridge(None, None, None)
        queued = []
        signals = []
        bridge.config = SimpleNamespace()
        bridge.runtime = SimpleNamespace(ready=True)
        bridge.worker = SimpleNamespace(submit=queued.append)
        bridge._dreaming = SimpleNamespace(
            foreground_queued=lambda: signals.append("queued"),
            foreground_finished=lambda: signals.append("finished"),
        )
        session = {"session_id": bridge.session_id}
        with patch.object(bridge, "_run_memory_search") as search:
            bridge._dispatch_memory_search("request", {**session, "query": "synthetic"})
            self.assertEqual(["queued"], signals)
            search.assert_not_called()
            queued.pop()()
            self.assertEqual(["queued", "finished"], signals)
            search.assert_called_once_with("request", "synthetic", 10, None)
        with patch.object(bridge, "_run_conversation_read") as read:
            bridge._dispatch_conversation_list("poll", session)
            self.assertEqual(["queued", "finished"], signals)
            queued.pop()()
            read.assert_called_once()
            self.assertEqual(["queued", "finished"], signals)

    def test_registered_folder_can_run_foreground_chat_then_restart_read_only(self):
        first = self._prepare_scripted_folder(external=False)
        bridge = self._bridge()
        info = bridge.request("app.info")["result"]
        self.assertTrue(info["host"]["ready"], info["host"])
        session = {"session_id": info["session_id"]}
        result = bridge.request("conversation.create", {**session, "mode": "chat"})
        self.assertTrue(result["ok"], result)
        conversation = {"conversation_id": result["result"]["conversation"]["conversation_id"]}
        sent = bridge.request(
            "conversation.send",
            {**session, **conversation, "client_request_id": "synthetic-chat", "text": "合成问题"},
        )
        self.assertTrue(sent["ok"], sent)
        turn = {"turn_id": sent["result"]["turn_id"]}
        deadline = time.monotonic() + 30
        while bridge.request("bridge.ping")["result"]["busy"] and time.monotonic() < deadline:
            time.sleep(0.05)
        result = bridge.request("turn.result", {**session, **turn})
        self.assertTrue(result["ok"], result)
        self.assertEqual("completed", result["result"]["state"])
        self.assertEqual("合成答复", result["result"]["final"]["text"])
        bridge.close()

        before = self._snapshot()
        bridge = self._bridge()
        info = bridge.request("app.info")["result"]
        self.assertEqual(first["namespace"], info["host"]["namespace"])
        result = bridge.request("conversation.get", {"session_id": info["session_id"], **conversation})
        self.assertTrue(result["ok"], result)
        self.assertEqual(["user", "final"], [row["kind"] for row in result["result"]["messages"]])
        bridge.close()
        self.assertEqual(before, self._snapshot())

    def test_registered_folder_publishes_a_cold_persona_preview_on_confirmation(self):
        from runtime import _generic_profile_body

        self._prepare_scripted_folder()
        draft = _generic_profile_body({})
        draft.update(profile_id="example.synthetic.import", identity_summary="Synthetic profile.")
        source = self.root / "persona-import" / "synthetic.json"
        source.write_text(json.dumps(draft), encoding="utf-8")
        bridge = self._bridge()
        info = bridge.request("app.info")["result"]
        session = {"session_id": info["session_id"]}
        preview = bridge.request("profile.import.preview", {**session, "name": source.name})
        self.assertTrue(preview["ok"], preview)
        published = bridge.request(
            "profile.publish_reviewed",
            {**session, "import_id": preview["result"]["import_id"], "confirm": True},
        )
        self.assertTrue(published["ok"], published)
        self.assertEqual(draft["profile_id"], published["result"]["profile_id"])
        self.assertFalse(published["result"]["active_changed"])
        self.assertEqual(draft, json.loads(source.read_text(encoding="utf-8")))

    def test_empty_folder_creates_real_stores_and_a_guide(self):
        first = prepare(self.root)
        config = load_config(first["config_path"])
        self.assertEqual(first["namespace"], config.authority.namespace)
        self.assertTrue((self.root / "pcs.sqlite").is_file())
        self.assertTrue((self.root / "memory/memory.sqlite3").is_file())
        self.assertTrue((self.root / "ledger/baseline.json").is_file())
        self.assertTrue((self.root / "persona/README.md").is_file())
        self.assertFalse((self.root / "cognition").exists())
        self.assertFalse(config.memory_bridge.test_mode)

    def test_repeated_connect_keeps_identity_config_and_guide(self):
        first = prepare(self.root)
        paths = [self.root / MANIFEST, self.root / CONFIG, self.root / "persona/README.md"]
        before = [p.read_bytes() for p in paths]
        second = prepare(self.root)
        self.assertEqual(first["instance_id"], second["instance_id"])
        self.assertEqual(before, [p.read_bytes() for p in paths])

    def test_two_empty_instances_get_distinct_identities(self):
        other = self.base / "other"
        other.mkdir()
        first, second = prepare(self.root), prepare(other)
        self.assertNotEqual(first["instance_id"], second["instance_id"])
        self.assertNotEqual(first["namespace"], second["namespace"])

    def test_move_preserves_identity_and_business_record_with_audited_binding(self):
        first = prepare(self.root)
        store = CognitionStore.open(self.root / "pcs.sqlite")
        append(store, namespace=first["namespace"], version=1, body={"text": "migration evidence"})
        store.close()
        target = self.base / "moved"
        shutil.copytree(self.root, target)
        second = prepare(target)
        self.assertEqual(first["instance_id"], second["instance_id"])
        config = load_config(second["config_path"])
        self.assertEqual(target, config.instance_root)
        reopened = CognitionStore.open(target / "pcs.sqlite")
        try:
            self.assertEqual("migration evidence", reopened.get_current(first["namespace"], RECORD_ID).body["text"])
        finally:
            reopened.close()
        audit = (target / "instance-history.jsonl").read_text(encoding="utf-8")
        self.assertIn('"anchor_path_rebound"', audit)
        self.assertIn('"memory_path_rebound"', audit)
        self.assertIn('"instance_moved"', audit)
        prepare(target)
        self.assertEqual(audit, (target / "instance-history.jsonl").read_text(encoding="utf-8"))

    def test_missing_memory_is_not_recreated(self):
        prepare(self.root)
        (self.root / "memory/memory.sqlite3").unlink()
        self.expect("INSTANCE_COMPONENT_MISSING")
        self.assertFalse((self.root / "memory/memory.sqlite3").exists())

    def test_missing_pcs_is_not_recreated(self):
        prepare(self.root)
        (self.root / "pcs.sqlite").unlink()
        self.expect("INSTANCE_COMPONENT_MISSING")
        self.assertFalse((self.root / "pcs.sqlite").exists())

    def test_missing_identity_does_not_generate_a_replacement(self):
        prepare(self.root)
        (self.root / MANIFEST).unlink()
        self.expect("INSTANCE_IDENTITY_MISSING")
        self.assertFalse((self.root / MANIFEST).exists())

    def test_missing_config_does_not_restore_stale_initial_settings(self):
        prepare(self.root)
        (self.root / CONFIG).unlink()
        self.expect("INSTANCE_COMPONENT_MISSING")
        self.assertFalse((self.root / CONFIG).exists())

    def test_lost_baseline_is_not_recreated(self):
        prepare(self.root)
        (self.root / "ledger/baseline.json").unlink()
        self.expect("INSTANCE_RECOVERY_INVALID")
        self.assertFalse((self.root / "ledger/baseline.json").exists())

    def test_corrupt_anchor_is_preserved_and_refused(self):
        prepare(self.root)
        anchor = next((self.root / "anchor").glob("*.json"))
        anchor.write_text("corrupt", encoding="utf-8")
        self.expect("INSTANCE_METADATA_INVALID")
        self.assertEqual("corrupt", anchor.read_text(encoding="utf-8"))

    def test_corrupt_memory_is_preserved_and_refused(self):
        prepare(self.root)
        memory = self.root / "memory/memory.sqlite3"
        memory.write_bytes(b"not a database")
        before = hashlib.sha256(memory.read_bytes()).hexdigest()
        self.expect("INSTANCE_COMPONENT_INVALID")
        self.assertEqual(before, hashlib.sha256(memory.read_bytes()).hexdigest())

    def test_foreign_database_cannot_replace_the_instance_identity(self):
        prepare(self.root)
        other = self.base / "other"
        other.mkdir()
        prepare(other)
        shutil.copyfile(other / "pcs.sqlite", self.root / "pcs.sqlite")
        self.expect("INSTANCE_IDENTITY_MISMATCH")

    def test_missing_readme_is_filled_and_existing_guide_is_preserved(self):
        prepare(self.root)
        guide = self.root / "persona/README.md"
        guide.unlink()
        prepare(self.root)
        self.assertIn("模型本身", guide.read_text(encoding="utf-8"))
        guide.write_text("my own guide", encoding="utf-8")
        prepare(self.root)
        self.assertEqual("my own guide", guide.read_text(encoding="utf-8"))

    def test_persona_only_import_preserves_source_and_activates_the_profile(self):
        from runtime import _generic_profile_body
        persona = self.root / "persona"
        persona.mkdir()
        body = _generic_profile_body({"ref_type":"event","namespace":"example.source","event_id":"11111111-1111-4111-8111-111111111111"})
        body.update(profile_id="example.folder.persona", profile_version="7", identity_summary="Synthetic initial persona.")
        path = persona / "profile.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        before = path.read_bytes()
        result = prepare(self.root)
        self.assertEqual(before, path.read_bytes())
        store = CognitionStore.open(self.root / "pcs.sqlite")
        try:
            active = PersonaProfileService(store).get_active_profile(result["namespace"])
            self.assertIsNotNone(active.ref)
            version = store.get_version(result["namespace"], active.ref.record_id, active.ref.record_version)
            self.assertEqual("example.folder.persona", version.body["profile_id"])
            self.assertEqual("7", version.body["profile_version"])
        finally:
            store.close()

    def test_existing_pcs_with_never_created_memory_is_completed_without_record_loss(self):
        from quintessence_pcs.store.recovery import record_anchor
        namespace = "example.partial.instance"
        store = CognitionStore.open(self.root / "pcs.sqlite")
        append(store, namespace=namespace, version=1, body={"text": "existing history"})
        store.deletion_ledger.ensure_ready(namespace, 0)
        record_anchor(store, namespace, 0)
        store.close()
        self.assertFalse((self.root / "memory").exists())
        self.expect("INSTANCE_MEMORY_HISTORY_UNKNOWN")
        result = prepare(self.root, component_declaration={"by":"operator:test", "reason":"fixture was created without Memory", "components":["memory"]})
        self.assertEqual(namespace, result["namespace"])
        reopened = CognitionStore.open(self.root / "pcs.sqlite")
        try:
            self.assertEqual("existing history", reopened.get_current(namespace, RECORD_ID).body["text"])
        finally:
            reopened.close()
        self.assertTrue((self.root / "memory/memory.sqlite3").exists())

    def test_existing_memory_with_never_created_pcs_is_completed_without_rebinding_identity(self):
        from scope_recall.contracts import InstanceBinding
        from scope_recall.core import MemoryCore, CoreConfig
        directory = self.root / "memory"
        binding = InstanceBinding("agent:existing", "install:existing", directory, frozenset(["scope:existing"]), False)
        MemoryCore(CoreConfig(binding)).initialize()
        path = directory / "memory.sqlite3"
        before = path.read_bytes()
        self.expect("INSTANCE_PCS_HISTORY_UNKNOWN")
        result = prepare(self.root, component_declaration={"by":"operator:test", "reason":"standalone Memory fixture never had PCS", "components":["pcs"]})
        config = load_config(result["config_path"])
        self.assertEqual("agent:existing", config.memory_bridge.agent_id)
        self.assertEqual("install:existing", config.memory_bridge.installation_id)
        self.assertEqual(before, path.read_bytes())
        self.assertTrue((self.root / "pcs.sqlite").exists())

    def test_explicit_legacy_adoption_records_unprovable_history_and_keeps_records(self):
        namespace = "example.legacy.adoption"
        store = CognitionStore.open(self.root / "pcs.sqlite")
        append(store, namespace=namespace, version=1, body={"text":"preserved legacy record"})
        store.close()
        shutil.rmtree(self.root / "ledger")
        shutil.rmtree(self.root / "anchor")
        self.expect("INSTANCE_RECOVERY_REQUIRED")
        with self.assertRaises(InstanceError) as caught:
            prepare(self.root, legacy_adoption={"by":"operator:test", "reason":"PS06 consent alone is not component history"})
        self.assertEqual("INSTANCE_MEMORY_HISTORY_UNKNOWN",caught.exception.code)
        self.assertFalse((self.root / MANIFEST).exists())
        self.assertFalse((self.root / "ledger").exists())
        result = prepare(self.root, legacy_adoption={"by":"operator:test", "reason":"explicitly accept unknown pre-baseline history"}, component_declaration={"by":"operator:test", "reason":"fixture was created without Memory", "components":["memory"]})
        self.assertEqual(namespace, result["namespace"])
        manifest = self.metadata()
        self.assertEqual("unprovable", manifest["legacy_adoption"]["pre_baseline_deletions"])
        reopened = CognitionStore.open(self.root / "pcs.sqlite")
        try:
            self.assertTrue(reopened.restore_decision.is_open)
            self.assertEqual("preserved legacy record", reopened.get_current(namespace, RECORD_ID).body["text"])
        finally:
            reopened.close()
        with self.assertRaises(InstanceError):
            prepare(self.root, legacy_adoption={"by":"operator:test", "reason":"must not repeat"})

    def test_unknown_component_history_refuses_before_metadata_or_store_creation(self):
        from quintessence_pcs.store.recovery import record_anchor
        store = CognitionStore.open(self.root / "pcs.sqlite")
        append(store, namespace="example.unknown", version=1, body={"text":"keep"})
        store.deletion_ledger.ensure_ready("example.unknown", 0)
        record_anchor(store, "example.unknown", 0)
        store.close()
        before = (self.root / "pcs.sqlite").read_bytes()
        self.expect("INSTANCE_MEMORY_HISTORY_UNKNOWN")
        self.assertEqual(before, (self.root / "pcs.sqlite").read_bytes())
        self.assertFalse((self.root / MANIFEST).exists())
        self.assertFalse((self.root / "memory").exists())

    def test_ready_component_cannot_be_declared_never_created_after_loss(self):
        prepare(self.root)
        (self.root / "memory/memory.sqlite3").unlink()
        before = (self.root / MANIFEST).read_bytes()
        with self.assertRaises(InstanceError):
            prepare(self.root, component_declaration={"by":"operator:test", "reason":"must not reset", "components":["memory"]})
        self.assertEqual(before, (self.root / MANIFEST).read_bytes())
        self.assertFalse((self.root / "memory/memory.sqlite3").exists())

    def test_surviving_memory_files_refuse_a_never_created_declaration(self):
        from quintessence_pcs.store.recovery import record_anchor
        store = CognitionStore.open(self.root / "pcs.sqlite")
        append(store, namespace="example.unknown", version=1, body={"text":"keep"})
        store.deletion_ledger.ensure_ready("example.unknown", 0)
        record_anchor(store, "example.unknown", 0)
        store.close()
        (self.root / "memory").mkdir()
        witness = self.root / "memory/instances.json"
        witness.write_text("preserve surviving metadata",encoding="utf-8")
        with self.assertRaises(InstanceError) as caught:
            prepare(self.root, component_declaration={"by":"operator:test", "reason":"must not ignore remnants", "components":["memory"]})
        self.assertEqual("INSTANCE_COMPONENT_MISSING",caught.exception.code)
        self.assertEqual("preserve surviving metadata",witness.read_text(encoding="utf-8"))
        self.assertFalse((self.root / MANIFEST).exists())

    def test_known_pending_memory_can_complete_without_a_new_declaration(self):
        result = prepare(self.root)
        manifest = self.metadata()
        manifest["components"]["memory"]["state"] = "pending"
        self.save_metadata(manifest)
        shutil.rmtree(self.root / "memory")
        self.assertEqual(result["instance_id"], prepare(self.root)["instance_id"])
        self.assertTrue((self.root / "memory/memory.sqlite3").is_file())

    def test_move_preserves_instance_display_preferences(self):
        first = prepare(self.root)
        scope = hashlib.sha256(("quintessence.instance.ui.v1:" + first["instance_id"]).encode()).hexdigest()
        preferences = {"ai_display_name":"Synthetic assistant", "ai_display_name_explicit":True,
                       "conversation_ui":{scope:{"archived":[],"titles":{"11111111-1111-4111-8111-111111111111":"My title"}}}}
        path = self.root / "ui-preferences.json"
        path.write_text(json.dumps(preferences),encoding="utf-8")
        target = self.base / "moved"
        shutil.copytree(self.root,target)
        prepare(target)
        self.assertEqual(path.read_bytes(),(target / path.name).read_bytes())

    def test_unknown_nonempty_folder_is_not_initialized(self):
        (self.root / "unrelated.txt").write_text("keep me", encoding="utf-8")
        self.expect("INSTANCE_UNRECOGNIZED")
        self.assertFalse((self.root / MANIFEST).exists())

    def test_interrupted_initialization_does_not_replace_a_missing_store(self):
        prepare(self.root)
        manifest = self.metadata()
        manifest["components"]["memory"]["state"] = "initializing"
        self.save_metadata(manifest)
        (self.root / "memory/memory.sqlite3").unlink()
        self.expect("INSTANCE_COMPONENT_MISSING")

    def test_unknown_manifest_version_is_not_rewritten(self):
        prepare(self.root)
        manifest = self.metadata()
        manifest["schema"] = "quintessence.instance.v99"
        self.save_metadata(manifest)
        before = (self.root / MANIFEST).read_bytes()
        self.expect("INSTANCE_VERSION_UNSUPPORTED")
        self.assertEqual(before, (self.root / MANIFEST).read_bytes())


if __name__ == "__main__":
    unittest.main()
