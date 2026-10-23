"""Packaged one-folder sidecar smoke tests (opt-in through an environment path)."""

from __future__ import annotations

import os
import unittest

from tests.desktop.support import DesktopCase

EXE = os.environ.get("QUINTESSENCE_DESKTOP_SIDECAR_EXE", "")


@unittest.skipUnless(EXE, "packaged sidecar executable is not selected")
class PackagedSidecarTests(DesktopCase):
    def test_packaged_sidecar_runs_the_real_consult(self) -> None:
        process = self.start(executable=EXE)
        info = process.request("app.info")["result"]
        self.assertTrue(info["host"]["ready"], info["host"])
        self.assertEqual(info["pcs"]["schema_migrations"], 10)
        confirm = process.request(
            "app.confirm_destination",
            {
                "session_id": info["session_id"],
                "destination_id": info["destination"]["destination_id"],
            },
        )
        self.assertTrue(confirm["ok"], confirm)
        reply = process.request(
            "consult.interact",
            {
                "session_id": info["session_id"],
                "question": "打包 sidecar 的合成问题",
                "confirmed_destination_id": info["destination"]["destination_id"],
            },
        )
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["state"], "answered")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM collab_interactions"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM collab_inputs"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM collab_artifacts"), 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM model_attempts"), 2)

    def test_packaged_sidecar_reports_data_locked(self) -> None:
        first = self.start(executable=EXE)
        info = first.request("app.info")["result"]
        self.assertTrue(info["host"]["ready"])
        second = self.start(executable=EXE)
        locked = second.request("app.info")["result"]
        self.assertFalse(locked["host"]["ready"])
        self.assertEqual(locked["host"]["reason_code"], "DATA_LOCKED")
        reply = second.request(
            "consult.interact",
            {
                "session_id": locked["session_id"],
                "question": "第二个打包 sidecar",
                "confirmed_destination_id": locked["destination"]["destination_id"],
            },
        )
        self.assertEqual(reply["error"]["code"], "DATA_LOCKED")
        self.assertEqual(first.request("bridge.ping")["ok"], True)


if __name__ == "__main__":
    unittest.main()
