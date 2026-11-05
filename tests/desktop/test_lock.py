"""Data-instance lock tests: one writer, no permanent stale lock."""

from __future__ import annotations

import sqlite3
import time
import unittest
from pathlib import Path

from tests.desktop.support import DesktopCase, BridgeProcess, config_document


class DataLockTests(DesktopCase):
    def _info(self, process) -> dict:
        return process.request("app.info")["result"]

    def test_second_sidecar_reports_data_locked_without_writing(self) -> None:
        first = self.start()
        info = self._info(first)
        self.assertTrue(info["host"]["ready"])
        before = self.counts()

        second = self.start()
        locked = self._info(second)
        self.assertFalse(locked["host"]["ready"])
        self.assertEqual(locked["host"]["reason_code"], "DATA_LOCKED")
        self.assertIsNotNone(locked["destination"])

        reply = second.request(
            "consult.interact",
            {
                "session_id": locked["session_id"],
                "question": "第二个 sidecar 的合成问题",
                "confirmed_destination_id": locked["destination"]["destination_id"],
            },
        )
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "DATA_LOCKED")
        self.assertEqual(before, self.counts())
        self.assertTrue(first.request("bridge.ping")["ok"])

    def test_read_only_inspection_is_possible_while_the_owner_writes(self) -> None:
        """Diagnostic SELECTs may run with the writer alive; writes still fail.

        This is the acceptance recipe in ``RG09-BRIDGE-CONTRACT.md``: an
        explicit read-only connection (``mode=ro`` outside the test guard,
        ``PRAGMA query_only`` here), a short timeout and a prompt close.  It is
        not a second writer and it is not a substitute for stopping the GUI
        before authoritative write checks.
        """

        first = self.start()
        self.assertTrue(self._info(first)["host"]["ready"])

        connection = sqlite3.connect(str(self.db_path), timeout=2.0)
        try:
            connection.execute("PRAGMA query_only = ON")
            count = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertGreater(int(count), 0)

        second = self.start()
        self.assertEqual(
            self._info(second)["host"]["reason_code"], "DATA_LOCKED"
        )

    def test_lock_is_released_when_the_owner_exits(self) -> None:
        first = self.start()
        info = self._info(first)
        first.request(
            "consult.interact",
            {
                "session_id": info["session_id"],
                "question": "第一个实例的合成问题",
                "confirmed_destination_id": info["destination"]["destination_id"],
            },
        )
        first.close()
        self.assertTrue((self.db_path.parent / (self.db_path.name + ".lock")).exists())
        third = self.start()
        info = self._info(third)
        self.assertTrue(info["host"]["ready"], info["host"])
        third.request(
            "app.confirm_destination",
            {
                "session_id": info["session_id"],
                "destination_id": info["destination"]["destination_id"],
            },
        )
        reply = third.request(
            "consult.interact",
            {
                "session_id": info["session_id"],
                "question": "恢复后的合成问题",
                "confirmed_destination_id": info["destination"]["destination_id"],
            },
        )
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["state"], "answered")

    def test_abnormal_exit_does_not_leave_a_permanent_lock(self) -> None:
        first = self.start()
        self.assertTrue(self._info(first)["host"]["ready"])
        first.kill()
        first.wait_exit()
        replacement = self.start()
        info = self._info(replacement)
        self.assertTrue(info["host"]["ready"], info["host"])

    def test_lock_is_bound_to_the_database_path(self) -> None:
        first = self.start()
        other_db = Path(self.tmp_path) / "second.sqlite"
        document = config_document(other_db)
        other = self.start(document=document)
        self.assertTrue(self._info(first)["host"]["ready"])
        self.assertTrue(self._info(other)["host"]["ready"])

    def test_lock_file_records_the_owner_without_secrets(self) -> None:
        first = self.start()
        self.assertTrue(self._info(first)["host"]["ready"])
        first.close()
        lock_path = self.db_path.parent / (self.db_path.name + ".lock")
        data = lock_path.read_text(encoding="utf-8")
        self.assertIn('"pid"', data)
        self.assertNotIn("question", data)
        self.assertNotIn("key", data.lower())


if __name__ == "__main__":
    unittest.main()
