"""SQLite URI parsing must preserve the offline guard's output boundary."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


class OfflineGuardUriTests(unittest.TestCase):
    def test_local_file_uri_inside_output_is_allowed(self):
        with tempfile.TemporaryDirectory(prefix="sqlite-uri-") as directory:
            path = Path(directory) / "inside space.sqlite"
            connection = sqlite3.connect(path.as_uri() + "?mode=rwc", uri=True)
            try:
                connection.execute("CREATE TABLE evidence (value TEXT)")
                connection.commit()
            finally:
                connection.close()
            self.assertTrue(path.is_file())

    def test_encoded_file_uri_cannot_escape_into_the_checkout(self):
        self.assertTrue(getattr(sys, '_quintessence_offline_guard', False))
        path = Path(__file__).resolve().parents[1] / "forbidden-uri-probe.sqlite"
        self.assertFalse(path.exists())
        with self.assertRaises(PermissionError):
            sqlite3.connect(path.as_uri() + "?mode=rwc", uri=True)
        self.assertFalse(path.exists())

    def test_remote_file_authority_is_refused(self):
        with self.assertRaises(PermissionError):
            sqlite3.connect("file://remote.invalid/private.sqlite?mode=ro", uri=True)
