"""J04: sidecar memory.bridge commands, permission and config gates."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from tests.desktop import support
from host_config import ConfigError, load_config

NS = support.NAMESPACE
HOST = support.HOST_INSTANCE
ACCESS = support.ACCESS_REF


class MemoryBridgeSidecarTests(support.DesktopCase):
    def _prepare_inputs(self, count: int) -> None:
        from quintessence_pcs.store import CognitionStore

        store = CognitionStore.open(self.db_path)
        try:
            rows = [
                store.register_input(
                    namespace=NS,
                    origin_kind="user_statement",
                    body=f"synthetic J04 sidecar body {index}",
                    privacy_class="private",
                    retention="standard",
                )
                for index in range(count)
            ]
            store.register_interaction(
                namespace=NS,
                mode="consult",
                activation_kind="user_input",
                host_instance=HOST,
                access_ref=ACCESS,
                policy_ref=support.POLICY_REF,
                input_refs=[
                    {
                        "ref_type": "collab_input",
                        "namespace": NS,
                        "input_id": row.input_id,
                        "content_version": row.content_version,
                    }
                    for row in rows
                ],
                result="answered",
            )
        finally:
            store.close()

    def _prepare_input(self) -> None:
        self._prepare_inputs(1)

    def _fake_cli(self) -> Path:
        path = self.tmp_path / "fake_capture.py"
        path.write_text(
            "import json, sys\n"
            "raw = sys.stdin.buffer.read()\n"
            "json.loads(raw)\n"
            "json.dump({'kind':'ok','receipt':{"
            "'disposition':'inserted','durability':'persisted',"
            "'event_refs':[{'ref':'external-sidecar','revision':1}]}}, sys.stdout)\n",
            encoding="utf-8",
        )
        return path

    def _document(self, *, permissions=("memory_bridge",), purpose=True):
        purposes = ["consult"]
        if purpose:
            purposes.append("memory.bridge")
        return support.config_document(
            self.db_path,
            purposes=purposes,
            memory_bridge={
                "enabled": True,
                "python_executable": sys.executable,
                "entry": str(self._fake_cli()),
                "package_path": str(self.tmp_path),
                "data_directory": "memory-external",
                "agent_id": "j04-sidecar",
                "installation_id": "j04-sidecar-install",
                "scope_id": "scope-j04-sidecar",
                "session_id": "session-j04-sidecar",
                "permissions": list(permissions),
            },
        )

    def test_sync_resumes_persisted_cursor_across_calls_and_restart(self):
        self._prepare_inputs(3)
        document = self._document()
        process = self.start(document=document)
        first = process.request("memory.bridge.sync", {"limit": 1})
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["result"]["projected"], 1)
        process.close()

        restarted = self.start(document=document)
        second = restarted.request("memory.bridge.sync", {"limit": 1})
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["result"]["projected"], 1)
        third = restarted.request("memory.bridge.sync", {"limit": 1})
        self.assertTrue(third["ok"], third)
        self.assertEqual(third["result"]["projected"], 1)
        self.assertEqual(
            self.scalar("SELECT count(*) FROM experience_source_mapping"), 3
        )
        self.assertEqual(
            self.scalar("SELECT count(*) FROM experience_source_mapping"), 3
        )

    def test_sync_and_deliver_commands_end_to_end(self):
        self._prepare_input()
        process = self.start(document=self._document())
        sync = process.request("memory.bridge.sync", {"limit": 10})
        self.assertTrue(sync["ok"], sync)
        self.assertEqual(sync["result"]["projected"], 1)
        deliver = process.request("memory.bridge.deliver", {"limit": 10})
        self.assertTrue(deliver["ok"], deliver)
        self.assertEqual(deliver["result"]["applied"], 1)
        delivery = self.query("SELECT state FROM memory_delivery")
        self.assertEqual([row["state"] for row in delivery], ["applied"])
        self.assertEqual(
            self.scalar("SELECT count(*) FROM experience_source_mapping"), 1
        )

    def test_disabled_bridge_refuses_without_writing(self):
        process = self.start()
        before = self.counts()
        reply = process.request("memory.bridge.sync", {"limit": 5})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "MEMORY_BRIDGE_FORBIDDEN")
        self.assertEqual(before, self.counts())

    def test_enabled_bridge_without_permission_is_rejected_by_config(self):
        document = self._document(permissions=())
        path = support.write_config(self.tmp_path, document, name="no-permission.json")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_enabled_bridge_without_purpose_is_rejected_by_config(self):
        document = self._document(purpose=False)
        path = support.write_config(self.tmp_path, document, name="no-purpose.json")
        with self.assertRaises(ConfigError):
            load_config(path)


if __name__ == "__main__":
    unittest.main()
