"""J09 PS06, backup branch: judged separately from the vector branch (D4).

The vector arm of PS06 has nothing to test in this repository -- there is no
vector index -- so it is ``NOT APPLICABLE``.  The **backup** arm does have
something to test, and the answer is not obvious: a cold snapshot taken before
a deletion still contains the deleted bytes, so restoring it can resurrect
content the deletion barrier had removed.  These tests pin that behaviour
instead of assuming it away.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains.cognition.beliefs import BELIEF_KIND  # noqa: E402
from quintessence_pcs.domains.cognition.questions import (  # noqa: E402
    QuestionAccess,
    QuestionService,
)


class _Base(CognitionTestCase):
    def snapshot_to(self, name: str) -> Path:
        """Copy the live database into a cold file, as a backup job would."""
        target = self.tmp_path / name
        destination = sqlite3.connect(str(target))
        try:
            self.store.connection.backup(destination)
        finally:
            destination.close()
        return target

    def current_beliefs(self, store=None):
        target = self.store if store is None else store
        return target.current_versions_of_kind(NS, BELIEF_KIND)


class BackupBeforeDeletionTests(_Base):
    """A pre-deletion snapshot still holds the bytes; opening it is the risk."""

    def test_a_pre_deletion_snapshot_keeps_the_bytes_but_the_deletion_is_replayed(self) -> None:
        version = self.propose(body=self.belief_body(claim="capability:old-window"))
        backup = self.snapshot_to("before-delete.sqlite")

        self.delete_record(version.record_id)
        self.assertEqual(self.current_beliefs(), ())

        # The file-level risk is unchanged: the cold copy still holds the row.
        connection = sqlite3.connect(str(backup))
        try:
            rows = connection.execute(
                "SELECT COUNT(*) FROM record_versions WHERE record_id = ?",
                (version.record_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(1, int(rows[0]), "the cold copy still holds the bytes")

        # Opening it next to the ledger that recorded the deletion replays the
        # deletion instead of serving the resurrected record.
        from quintessence_pcs.store.cognition import CognitionStore

        restored = CognitionStore.open(backup)
        self.addCleanup(restored.close)
        self.assertTrue(restored.restore_decision.is_open, restored.restore_decision.reason)

        revived = restored.current_versions_of_kind(NS, BELIEF_KIND)
        self.assertEqual([], [v.record_id for v in revived])


class BackupAfterDeletionTests(_Base):
    """A snapshot taken *after* the barrier keeps the record out."""

    def test_a_post_deletion_snapshot_does_not_resurrect_anything(self) -> None:
        version = self.propose(body=self.belief_body(claim="capability:new-window"))
        self.delete_record(version.record_id)
        backup = self.snapshot_to("after-delete.sqlite")

        from quintessence_pcs.store.cognition import CognitionStore

        restored = CognitionStore.open(backup)
        self.addCleanup(restored.close)
        self.assertEqual(
            restored.current_versions_of_kind(NS, BELIEF_KIND),
            (),
            "the barrier travels with the snapshot when it was taken after the delete",
        )


class LateWorkAgainstADeletedSourceTests(_Base):
    """Work chosen before the deletion cannot be recorded as success after it."""

    def test_a_question_whose_origin_was_deleted_fails_instead_of_being_answered(self) -> None:
        origin = self.ingest()
        access = QuestionAccess(
            namespace=NS,
            host_instance="synthetic.host",
            access_ref="auth:synthetic.host",
            privacy_classes=frozenset({"private"}),
            permissions=frozenset(
                {"question.read", "question.create", "question.transition", "question.evaluate"}
            ),
            purposes=frozenset({"cognition.question"}),
        )
        context = self.build_context()
        created = QuestionService(self.store).create(
            access,
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            question="synthetic: should the window move?",
            origin="user_requested",
            origin_refs=[self.event_ref(origin.event_id)],
            privacy_class="private",
        )
        self.assertEqual(created.view.status, "open")

        self.delete_event(origin.event_id)

        from quintessence_pcs.domains.cognition.questions import QuestionUnavailable

        with self.assertRaises(QuestionUnavailable):
            QuestionService(self.store).get(access, ref=created.view.ref)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
