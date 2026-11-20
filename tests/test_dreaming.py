"""Durability and admission checks for untasked Dreaming wakes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quintessence_pcs.domains.refs import EventRef
from quintessence_pcs.foundation.errors import CorruptStore
from quintessence_pcs.foundation.jsonutil import strict_json_loads
from quintessence_pcs.foundation.migrations import default_migrations_dir
from quintessence_pcs.runtime.dreaming import (
    DreamingState,
    MIN_IDLE,
    WAKE_COOLDOWN,
    WAKE_WINDOW,
    WakeProposal,
)

INSTANCE_ID = "a1111111-1111-4111-8111-111111111111"
OTHER_INSTANCE_ID = "b1111111-1111-4111-8111-111111111111"
AUTHORIZATION_ID = "c1111111-1111-4111-8111-111111111111"
OTHER_AUTHORIZATION_ID = "d1111111-1111-4111-8111-111111111111"
REQUEST_HASH = "sha256:" + "a" * 64
SOURCE = EventRef("instance.test", "e1111111-1111-4111-8111-111111111111")


class DreamingWakeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="dreaming-wake-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.database = self.directory / "wake.sqlite3"
        self.state = DreamingState(self.directory, instance_id=INSTANCE_ID)
        self.now = datetime(2026, 2, 2, 12, tzinfo=UTC)

    def reserve(self, state=None, **overrides):
        arguments = {
            "generation": "host-generation-1",
            "now": self.now,
            "idle_since": self.now - MIN_IDLE,
            "foreground_busy": False,
            "pending_work": False,
            "eligible": True,
        }
        arguments.update(overrides)
        state = self.state if state is None else state
        return state.reserve_wake(**arguments)

    def claim(self, attempt, state=None):
        state = self.state if state is None else state
        return state.record_request(
            attempt, request_hash=REQUEST_HASH, authorization_id=AUTHORIZATION_ID,
        )

    def row(self, attempt):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                "SELECT * FROM wake_attempts WHERE attempt_id = ?",
                (attempt.attempt_id,),
            ).fetchone()

    def proposal(self, expires_at=None):
        return WakeProposal(
            action="Compare the two test reports",
            reason="The reports disagree",
            scope={"project_id": "synthetic"},
            source_refs=(SOURCE,),
            effect="A local comparison to inform a later review",
            expires_at=expires_at or (self.now + timedelta(hours=1)),
        )

    def test_idle_threshold_and_foreground_pending_work_or_authority_block(self):
        self.assertIsNone(self.reserve(
            idle_since=self.now - MIN_IDLE + timedelta(microseconds=1),
        ))
        for flag in ("foreground_busy", "pending_work", "eligible"):
            with self.subTest(flag=flag):
                self.assertIsNone(self.reserve(**{flag: flag != "eligible"}))
        # A timestamp expressed in a different zone is the same idle boundary.
        offset = timezone(timedelta(hours=8))
        attempt = self.reserve(now=self.now.astimezone(offset))
        self.assertEqual(attempt.reserved_at, self.now)
        self.assertIsNone(self.reserve(generation="host-generation-2"))

    def test_concurrent_wake_ticks_and_transport_claims_have_one_winner(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.reserve)
            second = pool.submit(self.reserve)
            attempts = [result for result in (first.result(), second.result()) if result]
            self.assertEqual(len(attempts), 1)
            first = pool.submit(self.claim, attempts[0])
            second = pool.submit(self.claim, attempts[0])
            self.assertEqual(sorted((first.result(), second.result())), [False, True])
        self.assertEqual(self.state.count_model_requests(AUTHORIZATION_ID), 1)

    def test_restart_preserves_cooldown_and_rolling_budget_for_crashed_attempts(self):
        attempt = self.reserve()
        self.assertTrue(self.claim(attempt))
        reopened = DreamingState(self.directory, instance_id=INSTANCE_ID.upper())
        self.assertEqual(reopened.instance_id, INSTANCE_ID)
        self.assertEqual(reopened.count_model_requests(AUTHORIZATION_ID), 1)
        self.assertEqual(reopened.count_model_requests(OTHER_AUTHORIZATION_ID), 0)
        self.assertIsNone(self.reserve(
            reopened, generation="host-generation-2",
            now=self.now + WAKE_COOLDOWN - timedelta(microseconds=1),
        ))
        self.assertIsNone(self.reserve(reopened, now=self.now + WAKE_COOLDOWN))
        for slot in range(1, 4):
            self.assertIsNotNone(self.reserve(
                reopened, generation=f"host-restart-{slot}", now=self.now + slot * WAKE_COOLDOWN,
            ))
        self.assertEqual(self.row(attempt)["status"], "discarded")
        self.assertEqual(reopened.count_model_requests(AUTHORIZATION_ID), 1)
        self.assertFalse(self.claim(attempt, reopened))
        self.assertIsNone(self.reserve(
            reopened, generation="next-day", now=self.now + WAKE_WINDOW - timedelta(microseconds=1),
        ))
        self.assertIsNotNone(self.reserve(
            reopened, generation="next-day", now=self.now + WAKE_WINDOW,
        ))

    def test_proposal_keeps_stable_id_sources_and_expiry_after_restart(self):
        attempt = self.reserve()
        self.assertFalse(self.state.finish_wake(
            attempt, now=self.now, current_generation=attempt.generation, proposal=None,
        ))
        self.assertTrue(self.claim(attempt))
        proposal = self.proposal()
        self.assertTrue(self.state.finish_wake(
            attempt, now=self.now, current_generation=attempt.generation,
            proposal=proposal, usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        ))
        self.assertFalse(self.state.finish_wake(
            attempt, now=self.now, current_generation=attempt.generation, proposal=None,
        ))
        reopened = DreamingState(self.directory, instance_id=INSTANCE_ID)
        self.assertEqual(reopened.read_proposal(attempt.attempt_id, now=self.now), proposal)
        self.assertIsNone(reopened.read_proposal(
            attempt.attempt_id, now=self.now - timedelta(microseconds=1),
        ))
        self.assertIsNone(reopened.read_proposal(attempt.attempt_id, now=proposal.expires_at))
        row = self.row(attempt)
        self.assertEqual(row["request_hash"], REQUEST_HASH)
        self.assertEqual(row["provider_outcome"], "ok")
        self.assertEqual(strict_json_loads(row["usage_json"]), {
            "prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8,
        })

    def test_local_model_grant_uses_the_canonical_store_migration_directory(self):
        bundled_migrations = self.directory / "resources" / "migrations"
        bundled_migrations.mkdir(parents=True)
        name = "0007_model_ledger.sql"
        shutil.copyfile(default_migrations_dir() / name, bundled_migrations / name)
        with self.state.authorizations(bundled_migrations, provision=True) as ledger:
            grant = ledger.grant_model_authorization(
                namespace=SOURCE.namespace, authorization_ref="synthetic consent",
                provider="deepseek", model="synthetic-model", manifest_hash=REQUEST_HASH,
                max_calls=4, max_output_tokens_per_call=512, max_wall_seconds=20,
            )
        with self.state.authorizations(bundled_migrations) as ledger:
            self.assertEqual(ledger.model_authorization(grant.authorization_id), grant)

    def test_wake_and_private_checkpoint_publish_or_rollback_together(self):
        attempt = self.reserve()
        self.assertTrue(self.claim(attempt))

        def interrupt(connection):
            connection.execute("CREATE TABLE synthetic_checkpoint (id INTEGER)")
            connection.execute("INSERT INTO synthetic_checkpoint VALUES (1)")
            raise RuntimeError("synthetic crash before commit")

        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self.state.finish_wake(
                attempt, now=self.now, current_generation=attempt.generation,
                proposal=self.proposal(), recap="A cited recap.", complete=interrupt,
            )
        self.assertEqual(self.row(attempt)["status"], "reserved")
        self.assertIsNone(self.state.read_proposal(attempt.attempt_id, now=self.now))
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'synthetic_checkpoint'",
            ).fetchone())

        def complete(connection):
            connection.execute("CREATE TABLE synthetic_checkpoint (id INTEGER)")
            connection.execute("INSERT INTO synthetic_checkpoint VALUES (1)")
            return True

        self.assertTrue(self.state.finish_wake(
            attempt, now=self.now, current_generation=attempt.generation,
            proposal=self.proposal(), recap="A cited recap.", complete=complete,
        ))
        self.assertEqual(self.row(attempt)["status"], "proposed")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT id FROM synthetic_checkpoint").fetchone(), (1,))
            self.assertEqual(connection.execute("SELECT recap FROM dreaming_deep_recaps").fetchone(),
                             ("A cited recap.",))

    def test_moved_generation_revoked_or_expired_results_are_discarded(self):
        for slot, reason in enumerate(("generation", "revoked", "expired")):
            with self.subTest(reason=reason):
                now = self.now + slot * WAKE_COOLDOWN
                attempt = self.reserve(now=now)
                self.assertTrue(self.claim(attempt))
                current = "foreground" if reason == "generation" else attempt.generation
                expiry = now if reason == "expired" else now + timedelta(hours=1)
                self.assertFalse(self.state.finish_wake(
                    attempt, now=now, current_generation=current,
                    proposal=self.proposal(expiry), eligible=reason != "revoked",
                ))
                row = self.row(attempt)
                self.assertEqual(row["status"], "discarded")
                self.assertEqual(row["provider_outcome"], "ok")
                self.assertIsNone(row["proposal_json"])
                self.assertIsNone(self.state.read_proposal(attempt.attempt_id, now=now))

    def test_noop_and_failures_keep_truthful_transport_evidence(self):
        first = self.reserve()
        self.assertFalse(self.state.fail_wake(first, now=self.now, provider_outcome="timeout"))
        self.assertTrue(self.state.fail_wake(first, now=self.now, usage={"total_tokens": 2}))
        self.assertIsNone(self.row(first)["request_hash"])
        self.assertIsNone(self.row(first)["provider_outcome"])
        self.assertIsNone(self.row(first)["usage_json"])
        now = self.now + WAKE_COOLDOWN
        second = self.reserve(now=now)
        self.assertTrue(self.claim(second))
        self.assertTrue(self.state.fail_wake(
            second, now=now, provider_outcome="malformed",
            usage={"prompt_tokens": 9, "total_tokens": True, "raw": "ignored"},
        ))
        row = self.row(second)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["provider_outcome"], "malformed")
        self.assertEqual(strict_json_loads(row["usage_json"]), {"prompt_tokens": 9})
        now += WAKE_COOLDOWN
        third = self.reserve(now=now)
        self.assertTrue(self.claim(third))
        self.assertTrue(self.state.finish_wake(
            third, now=now, current_generation=third.generation, proposal=None,
        ))
        self.assertEqual(self.row(third)["status"], "noop")
        self.assertIsNone(self.state.read_proposal(third.attempt_id, now=now))

    def test_state_from_another_instance_cannot_claim_or_settle_the_attempt(self):
        attempt = self.reserve()
        with self.assertRaises(CorruptStore):
            DreamingState(self.directory, instance_id=OTHER_INSTANCE_ID)
        other_dir = self.directory / "other-instance"
        other_dir.mkdir()
        other = DreamingState(other_dir, instance_id=OTHER_INSTANCE_ID)
        self.assertFalse(self.claim(attempt, other))
        self.assertFalse(other.fail_wake(attempt, now=self.now))
        self.assertEqual(self.row(attempt)["status"], "reserved")


class DreamingStatePathTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="dreaming-path-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "dreaming"
        self.directory.mkdir()
        self.database = self.directory / "wake.sqlite3"

    def test_unknown_database_or_preexisting_sidecar_is_never_replaced(self):
        self.database.write_bytes(b"original file")
        with self.assertRaises(CorruptStore):
            DreamingState(self.directory, instance_id=INSTANCE_ID)
        self.assertEqual(self.database.read_bytes(), b"original file")
        self.database.unlink()
        journal = self.database.with_name("wake.sqlite3-journal")
        journal.write_bytes(b"original sidecar")
        with self.assertRaises(CorruptStore):
            DreamingState(self.directory, instance_id=INSTANCE_ID)
        self.assertEqual(journal.read_bytes(), b"original sidecar")
        self.assertFalse(self.database.exists())

    def test_known_database_hard_link_is_rejected_before_open(self):
        state = DreamingState(self.directory, instance_id=INSTANCE_ID)
        original = self.database.read_bytes()
        alias = self.root / "original.sqlite3"
        os.link(self.database, alias)
        with self.assertRaises(CorruptStore):
            DreamingState(self.directory, instance_id=INSTANCE_ID)
        with self.assertRaises(CorruptStore):
            state.count_model_requests(AUTHORIZATION_ID)
        self.assertEqual(alias.read_bytes(), original)

    def test_linked_journal_and_replaced_database_are_rejected_before_open(self):
        state = DreamingState(self.directory, instance_id=INSTANCE_ID)
        original = self.root / "original-journal"
        original.write_bytes(b"original journal")
        journal = self.database.with_name("wake.sqlite3-journal")
        os.link(original, journal)
        with self.assertRaises(CorruptStore):
            state.count_model_requests(AUTHORIZATION_ID)
        self.assertEqual(original.read_bytes(), b"original journal")
        journal.unlink()
        old_database = self.directory / "old.sqlite3"
        self.database.rename(old_database)
        self.database.write_bytes(old_database.read_bytes())
        with self.assertRaises(CorruptStore):
            state.count_model_requests(AUTHORIZATION_ID)
        self.assertEqual(self.database.read_bytes(), old_database.read_bytes())


if __name__ == "__main__":
    unittest.main()
