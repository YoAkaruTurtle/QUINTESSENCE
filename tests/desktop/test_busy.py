"""BUSY, duplicate-id and reader-responsiveness tests."""

from __future__ import annotations

import time
import unittest

from tests.desktop.support import DesktopCase, config_document, reply_script


class BusyTests(DesktopCase):
    def _ready_process(self, *, delay_ms: int = 1500):
        document = config_document(self.db_path, delay_ms=delay_ms)
        process = self.start(document=document)
        info = process.request("app.info")["result"]
        process.request(
            "app.confirm_destination",
            {
                "session_id": info["session_id"],
                "destination_id": info["destination"]["destination_id"],
            },
        )
        return process, info

    def _consult_params(self, info: dict, question: str) -> dict:
        return {
            "session_id": info["session_id"],
            "question": question,
            "confirmed_destination_id": info["destination"]["destination_id"],
        }

    def test_second_consult_is_busy_and_is_not_queued(self) -> None:
        process, info = self._ready_process()
        first_id = process.send_request(
            "consult.interact", self._consult_params(info, "第一个合成问题")
        )
        time.sleep(0.25)
        second = process.request(
            "consult.interact", self._consult_params(info, "第二个合成问题")
        )
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"]["code"], "BUSY")
        self.assertFalse(second["error"]["result_unknown"])

        ping_started = time.monotonic()
        ping = process.request("bridge.ping")
        self.assertTrue(ping["ok"])
        self.assertLess(time.monotonic() - ping_started, 1.0)

        first = process.wait_for(first_id, timeout=30)
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["result"]["state"], "answered")
        time.sleep(0.5)
        self.assertFalse(
            process.request("bridge.ping")["result"].get("busy", False)
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM collab_interactions"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM collab_inputs"), 1)

    def test_duplicate_request_id_is_rejected_while_the_first_runs(self) -> None:
        process, info = self._ready_process()
        request_id = process.send_request(
            "consult.interact", self._consult_params(info, "重复编号的问题")
        )
        time.sleep(0.2)
        process.send(
            {
                "v": "desktop.v1",
                "id": request_id,
                "method": "consult.interact",
                "params": self._consult_params(info, "重复编号的问题"),
            }
        )
        duplicate = process.wait_for(request_id, timeout=10)
        self.assertEqual(duplicate["error"]["code"], "DUPLICATE_REQUEST_ID")
        # The first response arrives later under the same id; it is not a
        # second interaction.
        first = process.wait_for(request_id, timeout=30)
        self.assertTrue(first["ok"], first)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM collab_interactions"), 1)

    def test_stdout_is_protocol_only_and_stderr_has_no_bodies(self) -> None:
        process, info = self._ready_process(delay_ms=0)
        process.request(
            "consult.interact",
            self._consult_params(info, "绝密问题正文-7f3a9c"),
        )
        documents = process.parsed_stdout()
        self.assertGreaterEqual(len(documents), 3)
        for document in documents:
            self.assertEqual(document["v"], "desktop.v1")
        combined = "\n".join(process.stderr_lines)
        self.assertNotIn("绝密问题正文-7f3a9c", combined)
        self.assertNotIn("合成回答", combined)


if __name__ == "__main__":
    unittest.main()
