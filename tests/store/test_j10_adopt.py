"""J10: one-shot adoption of a database that already carries deletions.

Adoption is an explicit relaxation: it records today's watermark as the
baseline and states that earlier deletions cannot be proven.  These cases pin
the four promises the approval attached to it.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from support import append  # noqa: E402

from quintessence_pcs.store.control import ControlStore  # noqa: E402
from quintessence_pcs.store.deletion_ledger import (  # noqa: E402
    PRE_BASELINE_UNPROVABLE,
    DeletionLedger,
)
from quintessence_pcs.store.recovery import (  # noqa: E402
    REASON_ADOPT_INVALID,
    REASON_ALREADY_ADOPTED,
    REASON_BEFORE_BASELINE,
    REASON_LEDGER_MISSING,
    read_json,
)

NS = "example.test.j10.adopt"
AUTH = "auth:j10-adopt"
RECORD_ID = "33333333-3333-4333-8333-333333333333"
ADOPT = {"by": "operator@lab", "reason": "upgrade an existing instance"}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="j10-adopt-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.instance_dir = self.root / "instance"
        self.instance_dir.mkdir()
        self.db_path = self.instance_dir / "pcs.sqlite"
        self.ledger_dir = self.root / "ledger"
        self.anchor_dir = self.root / "anchors"

    # ------------------------------------------------------------------ util

    def open_store(self, *, adopt: dict | None = None, ledger: bool = True) -> ControlStore:
        store = ControlStore.open(
            self.db_path,
            deletion_ledger_dir=self.ledger_dir if ledger else None,
            deletion_anchor_dir=self.anchor_dir if ledger else None,
            deletion_adopt=adopt,
        )
        self.addCleanup(lambda: store.close())
        return store

    def seed_deletion_without_ledger(self) -> None:
        """The state an older release leaves behind: data plus a barrier.

        A pre-J10 instance deleted without a ledger and without an anchor, so
        this helper deletes through the protected path and then removes both
        artefacts, leaving only the database behind.
        """

        store = self.open_store()
        append(store, namespace=NS, record_id=RECORD_ID, version=1, body={"text": "secret-body"})
        store.request_deletion(
            target_kind="record", namespace=NS, target_id=RECORD_ID, authorization_ref=AUTH
        )
        store.close()
        shutil.rmtree(self.ledger_dir, ignore_errors=True)
        shutil.rmtree(self.anchor_dir, ignore_errors=True)

    def snapshot_of(self, store: ControlStore, name: str) -> Path:
        target = self.root / name
        destination = sqlite3.connect(str(target))
        try:
            store.connection.backup(destination)
        finally:
            destination.close()
        return target

    def gate(self) -> dict:
        return read_json(self.instance_dir / "restore-gate.json") or {}


class AdoptionTests(_Base):
    def test_an_instance_with_deletions_refuses_to_open_without_adoption(self) -> None:
        self.seed_deletion_without_ledger()

        store = self.open_store()

        decision = store.restore_decision
        self.assertFalse(decision.is_open)
        self.assertEqual(REASON_LEDGER_MISSING, decision.reason)

    def test_adoption_exports_the_existing_barriers_and_marks_the_baseline(self) -> None:
        self.seed_deletion_without_ledger()

        store = self.open_store(adopt=ADOPT)

        decision = store.restore_decision
        self.assertTrue(decision.is_open, decision.reason)
        self.assertTrue(decision.adopted)
        self.assertEqual(PRE_BASELINE_UNPROVABLE, decision.pre_baseline)

        ledger = DeletionLedger(self.ledger_dir)
        adoption = ledger.adoption()
        self.assertIsNotNone(adoption)
        assert adoption is not None
        self.assertEqual(ADOPT["by"], adoption["adopted_by"])
        self.assertEqual(ADOPT["reason"], adoption["adopt_reason"])
        self.assertEqual(PRE_BASELINE_UNPROVABLE, adoption["pre_baseline_deletions"])
        self.assertIn(NS, adoption["baselines"])
        exported = [entry for entry in ledger.entries() if entry.op == "adopt_target"]
        self.assertEqual(1, len(exported))
        self.assertEqual(RECORD_ID, exported[0].target_id)
        self.assertEqual(AUTH, exported[0].authorization_ref)

        gate = self.gate()
        self.assertEqual("open", gate.get("status"))
        self.assertTrue(gate.get("adopted"))
        self.assertEqual(PRE_BASELINE_UNPROVABLE, gate.get("pre_baseline_deletions"))

    def test_a_second_adoption_is_refused_and_leaves_the_record_untouched(self) -> None:
        self.seed_deletion_without_ledger()
        self.open_store(adopt=ADOPT)

        again = self.open_store(adopt={"by": "someone-else", "reason": "second try"})

        decision = again.restore_decision
        self.assertFalse(decision.is_open)
        self.assertEqual(REASON_ALREADY_ADOPTED, decision.reason)

        adoption = DeletionLedger(self.ledger_dir).adoption()
        assert adoption is not None
        self.assertEqual(ADOPT["by"], adoption["adopted_by"])
        self.assertEqual(ADOPT["reason"], adoption["adopt_reason"])

    def test_an_empty_adoption_request_is_refused(self) -> None:
        self.seed_deletion_without_ledger()

        store = self.open_store(adopt={"by": "", "reason": "  "})

        decision = store.restore_decision
        self.assertFalse(decision.is_open)
        self.assertEqual(REASON_ADOPT_INVALID, decision.reason)

    def test_after_adoption_a_snapshot_before_the_baseline_is_refused(self) -> None:
        # The snapshot is taken *before* the deletion, so it sits below the
        # baseline the adoption records.  The pre-adoption state is built
        # through the protected path and then stripped, which is what an older
        # release actually leaves behind.
        original = self.open_store()
        append(
            original,
            namespace=NS,
            record_id=RECORD_ID,
            version=1,
            body={"text": "secret-body"},
        )
        old = self.snapshot_of(original, "before-deletion.sqlite")
        original.request_deletion(
            target_kind="record", namespace=NS, target_id=RECORD_ID, authorization_ref=AUTH
        )
        original.close()
        shutil.rmtree(self.ledger_dir, ignore_errors=True)
        shutil.rmtree(self.anchor_dir, ignore_errors=True)

        adopted = self.open_store(adopt=ADOPT)
        self.assertTrue(adopted.restore_decision.is_open, adopted.restore_decision.reason)
        adopted.close()

        shutil.copyfile(old, self.db_path)
        reopened = self.open_store()

        self.assertFalse(reopened.restore_decision.is_open)
        self.assertEqual(REASON_BEFORE_BASELINE, reopened.restore_decision.reason)

    def test_adoption_completes_after_a_crash_that_only_wrote_the_anchor(self) -> None:
        self.seed_deletion_without_ledger()
        # A crash between "anchor written" and "ledger installed" leaves no
        # ledger at all; the instance is closed and a re-run can finish.
        first = self.open_store(adopt={"by": "", "reason": ""})
        self.assertFalse(first.restore_decision.is_open)
        self.assertFalse(self.ledger_dir.exists())

        store = self.open_store(adopt=ADOPT)

        self.assertTrue(store.restore_decision.is_open, store.restore_decision.reason)
        self.assertTrue(store.restore_decision.adopted)

    def test_a_staging_directory_left_behind_does_not_fake_an_adoption(self) -> None:
        self.seed_deletion_without_ledger()
        staging = self.ledger_dir.with_name(self.ledger_dir.name + ".adopting-999999")
        staging.mkdir(parents=True)
        (staging / "ledger.jsonl").write_text("partial\n", encoding="utf-8")

        store = self.open_store(adopt=ADOPT)

        self.assertTrue(store.restore_decision.is_open, store.restore_decision.reason)
        self.assertTrue(store.restore_decision.adopted)

    def test_normal_deletions_and_restores_still_work_after_adoption(self) -> None:
        self.seed_deletion_without_ledger()
        store = self.open_store(adopt=ADOPT)
        self.assertTrue(store.restore_decision.is_open)

        second = "44444444-4444-4444-8444-444444444444"
        append(store, namespace=NS, record_id=second, version=1, body={"text": "new-body"})
        store.request_deletion(
            target_kind="record", namespace=NS, target_id=second, authorization_ref=AUTH
        )
        store.close()

        reopened = self.open_store()
        self.assertTrue(reopened.restore_decision.is_open, reopened.restore_decision.reason)
        self.assertTrue(reopened.restore_decision.adopted)
        # The new deletion is in the ledger, above the adopted baseline.
        ledger = DeletionLedger(self.ledger_dir)
        prewrites = [entry for entry in ledger.entries() if entry.is_prewrite]
        self.assertEqual(1, len(prewrites))
        self.assertEqual(second, prewrites[0].target_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
