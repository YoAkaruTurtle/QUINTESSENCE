"""Artifact scanning distinguishes generic code from shipped private state."""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('artifact_scan', REPO/'apps/desktop/scripts/scan_private.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class PackagingPrivacyTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='artifact-scan-')
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)

    def test_public_database_filename_in_program_code_is_not_private_data(self):
        (self.root/'program.exe').write_bytes(b'MZ synthetic code refers to pcs.sqlite')
        self.assertTrue(module.scan(self.root)['passed'])

    def test_database_asset_name_is_refused_even_if_not_a_sqlite_header(self):
        (self.root/'pcs.sqlite').write_bytes(b'synthetic opaque data')
        result=module.scan(self.root)
        self.assertFalse(result['passed'])
        self.assertTrue(any(f['pattern']=='private-database-file' for f in result['findings']))

    def test_renamed_sqlite_database_is_still_refused(self):
        path=self.root/'renamed.asset'
        connection=sqlite3.connect(path)
        try:
            connection.execute('CREATE TABLE synthetic (value TEXT)')
            connection.commit()
        finally:
            connection.close()
        self.assertFalse(module.scan(self.root)['passed'])

    def test_secret_patterns_remain_blocked(self):
        (self.root/'secret.txt').write_text('sk-'+'x'*32,encoding='utf-8')
        self.assertFalse(module.scan(self.root)['passed'])
