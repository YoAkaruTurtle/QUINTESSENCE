"""0017: additive conversation/turn/history schema and append-only guards.

Every test uses a temporary database.  The 0017 migration must add eight
tables, one nullable column and four append-only triggers without touching a
single old row; the Lab database copy used for the formal D02 evidence lives
outside the checkout and is exercised by the RG02 construction harness.

No model, network or private content is involved.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
SRC_DIR = HERE.parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from quintessence_pcs.foundation.migrations import (  # noqa: E402
    default_migrations_dir,
    discover_migrations,
)
from quintessence_pcs.store.cognition import CognitionStore  # noqa: E402

MIGRATIONS = discover_migrations(default_migrations_dir())

NS = "example.test.conversation"
TS = "2026-10-28T00:00:00Z"
NEW_TABLES = (
    "collab_conversations",
    "collab_turns",
    "collab_turn_stages",
    "collab_stage_contexts",
    "collab_turn_steps",
    "collab_turn_messages",
    "collab_turn_events",
    "collab_context_history",
)
APPEND_ONLY_TRIGGERS = (
    "collab_turn_events_no_update",
    "collab_turn_events_no_delete",
    "collab_context_history_no_update",
    "collab_context_history_no_delete",
)
CONTEXT_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
TURN_ID = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
CONVERSATION_ID = "cccccccc-3333-4333-8333-cccccccccccc"
EVENT_ID = "dddddddd-4444-4444-8444-dddddddddddd"


class ConversationMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rg02-migration-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    # ------------------------------------------------------------------ helpers

    def _store(self, name: str = "pcs.sqlite", **kwargs):
        store = CognitionStore.open(self.tmp_path / name, **kwargs)
        self.addCleanup(store.close)
        return store

    def _old_migrations(self) -> Path:
        target = self.tmp_path / "migrations-0016"
        shutil.copytree(default_migrations_dir(), target)
        (target / "0017_conversation_turns.sql").unlink()
        return target

    @staticmethod
    def _tables(store) -> set[str]:
        return {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    @staticmethod
    def _triggers(store) -> set[str]:
        return {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }

    @staticmethod
    def _ledger(store) -> list[tuple]:
        return [
            tuple(row)
            for row in store.connection.execute(
                "SELECT migration_id, name FROM schema_migrations "
                "ORDER BY migration_id"
            )
        ]

    @staticmethod
    def _insert_bare_context(store, context_id: str = CONTEXT_ID) -> None:
        # A 006-era bare watermark row: only the six original NOT NULL columns.
        store.connection.execute(
            "INSERT INTO context_snapshots (context_id, namespace, "
            "state_revision, context_event_seq, privacy_epoch, created_at) "
            "VALUES (?, ?, 0, 0, 0, ?)",
            (context_id, NS, TS),
        )

    def _seed_old_rows(self, store) -> None:
        store.append_record_version(
            namespace=NS,
            record_id="eeeeeeee-5555-4555-8555-eeeeeeeeeeee",
            record_version=1,
            kind="belief",
            body={"claim": "synthetic old row", "claim_type": "capability"},
            status="supported",
            schema_version="1",
            privacy_class="private",
        )
        store.ingest_event(
            namespace=NS,
            source_kind="host_observation",
            source_instance="rg02-migration-test",
            source_event_id="source-old-row",
            event_type="SyntheticOldRow",
            observed_at=TS,
            payload={"text": "old synthetic event"},
            recorded_at=TS,
        )
        self._insert_bare_context(store)

    def _counts(self, store) -> dict[str, int]:
        names = (
            "context_snapshots",
            "event_journal",
            "records",
            "record_versions",
            "collab_inputs",
            "collab_interactions",
            "collab_artifacts",
            "model_attempts",
            "memory_delivery",
        )
        return {
            name: int(
                store.connection.execute(
                    f"SELECT COUNT(*) FROM {name}"
                ).fetchone()[0]
            )
            for name in names
        }

    # ------------------------------------------------------------------- tests

    def test_fresh_database_has_0017_and_every_object(self) -> None:
        store = self._store()
        ledger = self._ledger(store)
        self.assertEqual([m.version for m in MIGRATIONS], [row[0] for row in ledger])
        self.assertEqual(
            ("0017", "conversation_turns"),
            (f"{ledger[-1][0]:04d}", ledger[-1][1]),
        )
        self.assertTrue(set(NEW_TABLES).issubset(self._tables(store)))
        self.assertTrue(set(APPEND_ONLY_TRIGGERS).issubset(self._triggers(store)))
        columns = [
            row[1]
            for row in store.connection.execute(
                "PRAGMA table_info(context_snapshots)"
            )
        ]
        self.assertIn("renderer_version", columns)
        self.assertEqual(
            [], store.connection.execute("PRAGMA foreign_key_check").fetchall()
        )

    def test_old_database_upgrades_additively(self) -> None:
        old_dir = self._old_migrations()
        old = self._store("old.sqlite", migrations_dir=old_dir)
        self._seed_old_rows(old)
        before = self._counts(old)
        old.close()

        upgraded = self._store("old.sqlite")
        ledger = self._ledger(upgraded)
        self.assertEqual(17, len(ledger))
        self.assertEqual(
            ("0017", "conversation_turns"),
            (f"{ledger[-1][0]:04d}", ledger[-1][1]),
        )
        self.assertEqual(before, self._counts(upgraded))
        for table in NEW_TABLES:
            self.assertEqual(
                0,
                int(
                    upgraded.connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                ),
                table,
            )
        self.assertEqual(
            0,
            int(
                upgraded.connection.execute(
                    "SELECT COUNT(*) FROM context_snapshots "
                    "WHERE renderer_version IS NOT NULL"
                ).fetchone()[0]
            ),
        )
        self.assertTrue(
            set(APPEND_ONLY_TRIGGERS).issubset(self._triggers(upgraded))
        )
        self.assertEqual(
            [], upgraded.connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        self.assertEqual(
            1,
            int(
                upgraded.connection.execute(
                    "SELECT COUNT(*) FROM context_snapshots WHERE context_id = ?",
                    (CONTEXT_ID,),
                ).fetchone()[0]
            ),
        )

    def _insert_conversation_turn_event(self, store) -> None:
        store.connection.execute(
            "INSERT INTO collab_conversations (conversation_id, namespace, "
            "host_instance, mode, title, next_turn_no, created_at, updated_at) "
            "VALUES (?, ?, ?, 'chat', NULL, 1, ?, ?)",
            (CONVERSATION_ID, NS, "host:test", TS, TS),
        )
        store.connection.execute(
            "INSERT INTO collab_turns (turn_id, namespace, conversation_id, "
            "turn_no, client_request_id, client_request_digest, input_ref_json, "
            "state, profile_ref_json, mode, strategy_json, last_event_seq, "
            "created_at, updated_at) VALUES (?, ?, ?, 1, 'req-1', ?, '{}', "
            "'accepted', '{}', 'chat', '{}', 1, ?, ?)",
            (TURN_ID, NS, CONVERSATION_ID, "0" * 64, TS, TS),
        )
        store.connection.execute(
            "INSERT INTO collab_turn_events (namespace, turn_id, seq, event_id, "
            "kind, payload_json, created_at) VALUES (?, ?, 1, ?, "
            "'turn_accepted', '{}', ?)",
            (NS, TURN_ID, EVENT_ID, TS),
        )

    def test_append_only_triggers_refuse_update_and_delete(self) -> None:
        store = self._store()
        self._insert_bare_context(store)
        self._insert_conversation_turn_event(store)
        store.connection.execute(
            "INSERT INTO collab_context_history (namespace, context_id, "
            "ordinal, ref_kind, ref_id, ref_version, created_at) "
            "VALUES (?, ?, 1, 'collab_input', ?, 1, ?)",
            (NS, CONTEXT_ID, str(uuid.uuid4()), TS),
        )
        for statement in (
            "UPDATE collab_turn_events SET kind = 'other'",
            "DELETE FROM collab_turn_events",
            "UPDATE collab_context_history SET ordinal = 2",
            "DELETE FROM collab_context_history",
        ):
            with self.assertRaises(sqlite3.IntegrityError, msg=statement):
                store.connection.execute(statement)

    def test_message_constraints_are_enforced_without_freezing_state(self) -> None:
        # collab_turn_messages deliberately has no append-only trigger: the
        # published -> suppressed transition is a future RG03 concern.  Its
        # CHECKs and the partial unique final index still guard the row.
        store = self._store()
        self._insert_conversation_turn_event(store)
        message_id = "ffffffff-6666-4666-8666-ffffffffffff"
        store.connection.execute(
            "INSERT INTO collab_turn_messages (message_id, namespace, turn_id, "
            "seq, kind, input_ref_json, artifact_id, artifact_version, "
            "attempt_ref_json, status, published_at, created_at) VALUES "
            "(?, ?, ?, 1, 'user', '{}', NULL, NULL, NULL, 'published', ?, ?)",
            (message_id, NS, TURN_ID, TS, TS),
        )
        # kind='final' requires an artifact; the CHECK refuses the transition.
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute(
                "UPDATE collab_turn_messages SET kind = 'final' WHERE "
                "message_id = ?",
                (message_id,),
            )
        # A published final needs an artifact, and only one may exist per turn.
        artifact_id = str(uuid.uuid4())
        for seq in (2, 3):
            statement = (
                "INSERT INTO collab_turn_messages (message_id, namespace, "
                "turn_id, seq, kind, input_ref_json, artifact_id, "
                "artifact_version, attempt_ref_json, status, published_at, "
                "created_at) VALUES (?, ?, ?, ?, 'final', NULL, ?, 1, NULL, "
                "'published', ?, ?)"
            )
            values = (
                str(uuid.uuid4()), NS, TURN_ID, seq, artifact_id, TS, TS,
            )
            if seq == 2:
                store.connection.execute(statement, values)
            else:
                with self.assertRaises(sqlite3.IntegrityError):
                    store.connection.execute(statement, values)


if __name__ == "__main__":
    unittest.main()
