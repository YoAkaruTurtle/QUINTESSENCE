"""J11 / PS06: what a missing deletion ledger may and may not mean.

The failure this pins down is a restore, not a crash.  If the database is put
back to a point before a deletion, it forgets the deletion entirely; if the
host configuration that named the ledger is put back too, nothing in the
database says the instance was ever protected.  Treating "no deletion
watermark" as "nothing was ever deleted" would therefore resurrect deleted
content -- so the only default that can be defended is a closed gate.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from support import append  # noqa: E402

from quintessence_pcs.store.control import ControlStore  # noqa: E402
from quintessence_pcs.store.deletion_ledger import DeletionLedger  # noqa: E402
from quintessence_pcs.store.recovery import (  # noqa: E402
    DELETION_SAFETY_ENFORCED,
    DELETION_SAFETY_UNPROTECTED,
    REASON_LEDGER_MISSING,
    REASON_PS06_UNPROTECTED,
    RestoreBlocked,
    read_json,
)

NS = "example.test.ps06.defaults"
AUTH = "auth:ps06-defaults"
RECORD_ID = "44444444-4444-4444-8444-444444444444"
UNPROTECTED = {"by": "operator@lab", "reason": "explicit experiment without a ledger"}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ps06-defaults-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.instance_dir = self.root / "instance"
        self.instance_dir.mkdir()
        self.db_path = self.instance_dir / "pcs.sqlite"
        self.ledger_dir = self.root / "ledger"
        self.anchor_dir = self.root / "anchors"

    def open_store(self, **options) -> ControlStore:
        store = ControlStore.open(self.db_path, **options)
        self.addCleanup(lambda: store.close())
        return store

    def protected(self) -> ControlStore:
        return self.open_store(
            deletion_ledger_dir=self.ledger_dir,
            deletion_anchor_dir=self.anchor_dir,
        )

    def record(self, store: ControlStore, record_id: str = RECORD_ID) -> None:
        append(store, namespace=NS, record_id=record_id, version=1, body={"text": "secret-body"})

    def delete(self, store: ControlStore, record_id: str = RECORD_ID) -> None:
        store.request_deletion(
            target_kind="record",
            namespace=NS,
            target_id=record_id,
            authorization_ref=AUTH,
        )

    def gate(self) -> dict:
        return read_json(self.instance_dir / "restore-gate.json") or {}


class DefaultLedgerTests(_Base):
    def test_a_new_instance_is_protected_by_default(self) -> None:
        # "auto" is the store default: the ledger and the anchor sit next to the
        # database, so a fresh instance is protected without any configuration.
        store = self.open_store()

        decision = store.restore_decision
        self.assertTrue(decision.is_open, decision.reason)
        self.assertEqual(DELETION_SAFETY_ENFORCED, decision.deletion_safety)
        self.assertEqual(Path(self.instance_dir) / "ledger", store.deletion_ledger_dir)
        self.assertTrue((self.ledger_dir.parent / "instance" / "anchor").exists())

        self.record(store)
        self.delete(store)
        self.assertEqual(1, self.epoch(store))

    def epoch(self, store: ControlStore) -> int:
        row = store.connection.execute(
            "SELECT privacy_epoch FROM namespace_state WHERE namespace = ?", (NS,)
        ).fetchone()
        return int(row[0]) if row else 0


class MissingLedgerTests(_Base):
    def test_no_ledger_at_all_closes_the_gate(self) -> None:
        # Case (e): nothing configured and no explicit opt-out.  A fresh-looking
        # database is not evidence, so the gate stays closed.
        store = self.open_store(deletion_ledger_dir=None, deletion_anchor_dir=self.anchor_dir)

        decision = store.restore_decision
        self.assertFalse(decision.is_open)
        self.assertEqual(REASON_LEDGER_MISSING, decision.reason)
        self.assertEqual("not_configured", decision.deletion_safety)
        self.assertEqual(REASON_LEDGER_MISSING, self.gate().get("reason"))
        self.assertEqual("not_configured", self.gate().get("deletion_safety"))

    def test_an_anchor_alone_closes_the_gate(self) -> None:
        # Case (b): the database looks untouched because it is an older copy,
        # but the anchor (which lives outside both) says this path was protected.
        store = self.protected()
        store.close()

        reopened = self.open_store(deletion_ledger_dir=None, deletion_anchor_dir=self.anchor_dir)

        decision = reopened.restore_decision
        self.assertFalse(decision.is_open)
        self.assertEqual(REASON_LEDGER_MISSING, decision.reason)

    def test_a_missing_ledger_with_deletions_closes_the_gate(self) -> None:
        # Case (c): the ledger was configured once and its files are gone.
        store = self.protected()
        self.record(store)
        self.delete(store)
        store.close()
        shutil.rmtree(self.ledger_dir)

        reopened = self.open_store(
            deletion_ledger_dir=self.ledger_dir,
            deletion_anchor_dir=self.anchor_dir,
        )

        decision = reopened.restore_decision
        self.assertFalse(decision.is_open)
        self.assertEqual(REASON_LEDGER_MISSING, decision.reason)


class UnprotectedModeTests(_Base):
    def test_an_explicit_unprotected_run_opens_but_refuses_to_delete(self) -> None:
        # Case (d): the operator may look at the data without a ledger, and the
        # mode is visible.  Deleting is refused: it would accumulate history
        # that no future restore could prove.
        store = self.open_store(
            deletion_ledger_dir=None,
            deletion_anchor_dir=self.anchor_dir,
            ps06_unprotected=UNPROTECTED,
        )

        decision = store.restore_decision
        self.assertTrue(decision.is_open, decision.reason)
        self.assertEqual(REASON_PS06_UNPROTECTED, decision.reason)
        self.assertEqual(DELETION_SAFETY_UNPROTECTED, decision.deletion_safety)
        self.assertEqual("unprotected", self.gate().get("deletion_safety"))

        self.record(store)
        with self.assertRaises(Exception) as caught:
            self.delete(store)
        self.assertIn("deletion", str(caught.exception).lower())
        rows = store.connection.execute("SELECT COUNT(*) FROM deletion_targets").fetchone()
        self.assertEqual(0, int(rows[0]))

    def test_an_unprotected_request_without_operator_and_reason_is_refused(self) -> None:
        store = self.open_store(
            deletion_ledger_dir=None,
            deletion_anchor_dir=self.anchor_dir,
            ps06_unprotected={"by": "", "reason": ""},
        )

        self.assertFalse(store.restore_decision.is_open)
        self.assertEqual("ps06_unprotected_invalid", store.restore_decision.reason)

    def test_unprotected_cannot_hide_an_existing_deletion_history(self) -> None:
        store = self.protected()
        self.record(store)
        self.delete(store)
        store.close()

        reopened = self.open_store(
            deletion_ledger_dir=None,
            deletion_anchor_dir=self.anchor_dir,
            ps06_unprotected=UNPROTECTED,
        )

        self.assertFalse(reopened.restore_decision.is_open)
        self.assertEqual(REASON_LEDGER_MISSING, reopened.restore_decision.reason)


class ResurrectionTests(_Base):
    """Case (f): the scenario the whole gate exists for."""

    def test_restoring_an_old_database_without_the_ledger_config_cannot_resurrect(self) -> None:
        store = self.protected()
        self.record(store)
        before = self.snapshot(store, "before.sqlite")

        self.delete(store)
        store.close()
        self.assertEqual(1, self.count_barriers())

        # Monday's database plus Monday's host configuration: no ledger path at
        # all.  The anchor is the only external witness that survives.
        shutil.copyfile(before, self.db_path)

        reopened = self.open_store(deletion_ledger_dir=None, deletion_anchor_dir=self.anchor_dir)
        decision = reopened.restore_decision
        self.assertFalse(decision.is_open)
        self.assertEqual(REASON_LEDGER_MISSING, decision.reason)

        with self.assertRaises(RestoreBlocked):
            reopened.get_current(NS, RECORD_ID)

    def test_the_same_restore_with_the_ledger_still_applied_stays_closed_to_the_record(self) -> None:
        # Same restore, but the ledger survived: the gate may open only after
        # the deletion is replayed, and the record must not come back.
        store = self.protected()
        self.record(store)
        before = self.snapshot(store, "before2.sqlite")
        self.delete(store)
        store.close()
        shutil.copyfile(before, self.db_path)

        reopened = self.open_store(
            deletion_ledger_dir=self.ledger_dir,
            deletion_anchor_dir=self.anchor_dir,
        )

        self.assertTrue(reopened.restore_decision.is_open, reopened.restore_decision.reason)
        with self.assertRaises(Exception):
            reopened.get_current(NS, RECORD_ID)
        self.assertEqual(1, int(
            reopened.connection.execute(
                "SELECT COUNT(*) FROM deletion_targets WHERE status = 'cleaned'"
            ).fetchone()[0]
        ))

    def test_unprotected_restore_cannot_hide_a_surviving_anchor(self) -> None:
        self._unprotected_restore(ledger=False)

    def test_unprotected_restore_cannot_hide_a_surviving_ledger(self) -> None:
        self._unprotected_restore(ledger=True)

    def _unprotected_restore(self, *, ledger: bool) -> None:
        store = self.protected()
        self.record(store)
        before = self.snapshot(store, "unprotected-before.sqlite")
        self.delete(store)
        store.close()
        shutil.copyfile(before, self.db_path)
        if ledger:
            # Prove the ledger alone suffices, without the anchor's witness.
            shutil.rmtree(self.anchor_dir)
        reopened = self.open_store(
            deletion_ledger_dir=self.ledger_dir if ledger else None,
            deletion_anchor_dir=self.anchor_dir,
            ps06_unprotected=UNPROTECTED,
        )
        self.assertFalse(reopened.restore_decision.is_open)
        self.assertEqual(REASON_LEDGER_MISSING, reopened.restore_decision.reason)
        with self.assertRaises(RestoreBlocked):
            reopened.get_current(NS, RECORD_ID)

    def snapshot(self, store: ControlStore, name: str) -> Path:
        target = self.root / name
        destination = sqlite3.connect(str(target))
        try:
            store.connection.backup(destination)
        finally:
            destination.close()
        return target

    def count_barriers(self) -> int:
        connection = sqlite3.connect(str(self.db_path))
        try:
            return int(connection.execute("SELECT COUNT(*) FROM deletion_targets").fetchone()[0])
        finally:
            connection.close()


class LedgerInitialisationTests(_Base):
    def test_a_new_instance_writes_a_baseline_and_an_anchor(self) -> None:
        store = self.protected()
        store.close()

        ledger = DeletionLedger(self.ledger_dir)
        self.assertEqual(0, ledger.max_epoch(NS))
        anchor_file = next(Path(self.anchor_dir).glob("*.json"))
        payload = json.loads(anchor_file.read_text(encoding="utf-8"))
        self.assertEqual(1, payload.get("version"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
