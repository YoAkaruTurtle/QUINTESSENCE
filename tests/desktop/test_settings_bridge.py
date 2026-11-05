"""Real sidecar tests: settings apply at worker boundaries, never at startup."""
import json
import time

from tests.desktop.support import DesktopCase, config_document


class SettingsBridgeTests(DesktopCase):
    def change(self, process, document):
        process.config_path.write_text(json.dumps(document), encoding="utf-8")
        return process.request("settings.reload")

    def test_mode_persists_and_session_does_not_restart(self):
        doc = config_document(self.db_path)
        process = self.start(document=doc)
        before = process.request("app.info")["result"]
        doc["conversation"] = {"sdrpe": {"mode": "on"}}
        self.assertTrue(self.change(process, doc)["ok"])
        after = process.request("app.info")["result"]
        self.assertEqual(after["session_id"], before["session_id"])
        self.assertEqual(after["capabilities"]["expression"]["mode"], "on")
        self.assertEqual(json.loads(process.config_path.read_text())["conversation"]["sdrpe"]["mode"], "on")

    def test_auto_and_authority_changes_are_rejected(self):
        doc = config_document(self.db_path)
        process = self.start(document=doc)
        doc["conversation"] = {"sdrpe": {"mode": "auto"}}
        self.assertFalse(self.change(process, doc)["ok"])
        doc["conversation"]["sdrpe"]["mode"] = "on"
        doc["host"]["namespace"] = "other.namespace"
        self.assertFalse(self.change(process, doc)["ok"])
        self.assertEqual(process.request("app.info")["result"]["capabilities"]["expression"]["mode"], "off")

    def test_invalid_json_does_not_replace_runtime(self):
        process = self.start()
        process.config_path.write_text("{", encoding="utf-8")
        self.assertFalse(process.request("settings.reload")["ok"])
        self.assertTrue(process.request("app.info")["result"]["host"]["ready"])

    def test_output_limit_changes_destination_without_restarting(self):
        doc = config_document(self.db_path)
        process = self.start(document=doc)
        before = process.request("app.info")["result"]
        doc["destination"]["manifest"]["max_output_tokens_per_call"] = 256
        self.assertTrue(self.change(process, doc)["ok"])
        after = process.request("app.info")["result"]
        self.assertEqual(after["session_id"], before["session_id"])
        self.assertNotEqual(after["destination"]["destination_id"], before["destination"]["destination_id"])

    def test_reload_cannot_take_a_config_path_or_secret(self):
        process = self.start()
        self.assertFalse(process.request("settings.reload", {"config_path": "elsewhere"})["ok"])

    def test_running_consult_finishes_before_configuration_changes(self):
        doc = config_document(self.db_path, delay_ms=1200)
        process = self.start(document=doc)
        info = process.request("app.info")["result"]
        process.request("app.confirm_destination", {"session_id": info["session_id"], "destination_id": info["destination"]["destination_id"]})
        first = process.send_request("consult.interact", {"session_id": info["session_id"], "question": "synthetic settings test", "confirmed_destination_id": info["destination"]["destination_id"]})
        time.sleep(.15)
        doc["conversation"] = {"sdrpe": {"mode": "on"}}
        process.config_path.write_text(json.dumps(doc), encoding="utf-8")
        reload_id = process.send_request("settings.reload")
        during = process.request("app.info")["result"]
        self.assertEqual(during["capabilities"]["expression"]["mode"], "off")
        self.assertTrue(process.wait_for(first, timeout=30)["ok"])
        self.assertTrue(process.wait_for(reload_id, timeout=30)["ok"])
        self.assertEqual(process.request("app.info")["result"]["capabilities"]["expression"]["mode"], "on")
