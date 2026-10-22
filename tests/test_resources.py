"""Runtime resources and explicit Unicode output paths, in source and wheels."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from quintessence_pcs import PROTOCOL_VERSION
from quintessence_pcs.foundation.migrations import discover_migrations
from quintessence_pcs.store.records import RecordStore

class ResourceTests(unittest.TestCase):
    def test_sql_resources_are_contiguous_and_exact(self):
        self.assertEqual('pcs.v2', PROTOCOL_VERSION)
        migrations = discover_migrations()
        self.assertEqual(list(range(1, 11)), [m.version for m in migrations])
        for migration in migrations:
            self.assertEqual(hashlib.sha256(migration.path.read_bytes()).hexdigest(), migration.checksum)
            self.assertEqual('migrations', migration.path.parent.name)
            self.assertEqual('resources', migration.path.parent.parent.name)

    def test_unicode_space_database_path_and_idempotent_reopen(self):
        with tempfile.TemporaryDirectory(prefix='合成空间 路径-') as directory:
            path = Path(directory) / '状态 合成.sqlite'
            store = RecordStore.open(path)
            ledger = [tuple(r) for r in store.connection.execute('SELECT migration_id,name,checksum FROM schema_migrations ORDER BY migration_id')]
            store.close()
            again = RecordStore.open(path)
            try:
                self.assertEqual(ledger, [tuple(r) for r in again.connection.execute('SELECT migration_id,name,checksum FROM schema_migrations ORDER BY migration_id')])
                self.assertEqual(10, len(ledger))
            finally:
                again.close()

if __name__ == '__main__':
    unittest.main()
