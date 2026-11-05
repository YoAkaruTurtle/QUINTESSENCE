"""Real folder lifecycle regressions; all private data is synthetic and external."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "src"), str(REPO / "apps/desktop/sidecar")]
from instance_manager import CONFIG, MANIFEST, InstanceError, prepare
from host_config import load_config
from quintessence_pcs.store import CognitionStore
from quintessence_pcs.domains.persona.profiles import PersonaProfileService
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
