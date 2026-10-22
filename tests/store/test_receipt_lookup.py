"""PCS2-010 C04: read-only lookup of an already committed command receipt.

The lookup is one ``SELECT`` on ``processed_commands``: it proves the command
once committed, not that its Context is still fresh or that the records it
names are still readable.  Every test uses ``ControlStore``, the PCS2-006
composition, and a fresh temporary database.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from support import OTHER_RECORD_ID, RECORD_ID, StoreTestCase  # noqa: E402

from quintessence_pcs.foundation.errors import (  # noqa: E402
    CorruptStore,
    InvalidInput,
    Redacted,
)
from quintessence_pcs.foundation.jsonutil import canonical_json_text  # noqa: E402
from quintessence_pcs.store.cognition import CognitionStore  # noqa: E402
from quintessence_pcs.store.control import ControlStore  # noqa: E402
from quintessence_pcs.store.tx_b import CommitReceipt  # noqa: E402

NS = "example.test.receipt-lookup"
NS_B = "example.test.receipt-lookup.other"
NS_C = "example.test.receipt-lookup.third"
OPERATION = "proposal.submit"
PAYLOAD = {"kind": "Observation", "body": {"text": "first"}}
SECRET = "synthetic-lookup-body-must-not-return"


def new_request_id() -> str:
    return str(uuid.uuid4())


def append_apply(
    *, record_id: str = RECORD_ID, body: object = None, version: int = 1
):
    body = {"text": "written"} if body is None else body

    def apply(scope: object) -> None:
        scope.append_record_version(  # type: ignore[attr-defined]
            record_id=record_id,
            record_version=version,
            kind="Observation",
            body=body,
        )

    return apply


class ReceiptLookupTests(StoreTestCase):
    store_class = ControlStore

    # ----------------------------------------------------------- helpers

    def commit(
        self,
        *,
        snapshot: object,
        request_id: str,
        apply: object,
        operation: str = OPERATION,
        payload: object = PAYLOAD,
        namespace: str | None = None,
    ) -> CommitReceipt:
        return self.store.commit_command(
            context_id=snapshot.context_id,  # type: ignore[attr-defined]
            request_id=request_id,
            operation=operation,
            request_payload=payload,  # type: ignore[arg-type]
            apply=apply,  # type: ignore[arg-type]
            namespace=namespace,
        )

    def lookup(
        self, request_id: str, namespace: str = NS
    ) -> CommitReceipt | None:
        return self.store.lookup_command_receipt(
            namespace=namespace, request_id=request_id
        )

    def receipt_json(self, request_id: str, namespace: str = NS) -> str:
        row = self.store.connection.execute(
            "SELECT receipt_json FROM processed_commands "
            "WHERE namespace = ? AND request_id = ?",
            (namespace, request_id),
        ).fetchone()
        self.assertIsNotNone(row)
        return str(row["receipt_json"])

    def overwrite_receipt_json(
        self, request_id: str, text: str, namespace: str = NS
    ) -> None:
        cursor = self.store.connection.execute(
            "UPDATE processed_commands SET receipt_json = ? "
            "WHERE namespace = ? AND request_id = ?",
            (text, namespace, request_id),
        )
        self.assertEqual(1, cursor.rowcount)

    # ------------------------------------------------------------- tests

    def test_lookup_returns_the_stored_receipt(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        request_id = new_request_id()
        committed = self.commit(
            snapshot=snapshot, request_id=request_id, apply=append_apply()
        )

        found = self.lookup(request_id)

        self.assertIsInstance(found, CommitReceipt)
        self.assertEqual(committed, found)
        self.assertEqual(request_id, found.request_id)
        self.assertEqual(NS, found.namespace)
        # The method lives on the mixin, so every composed store has it.
        self.assertTrue(hasattr(CognitionStore, "lookup_command_receipt"))

    def test_lookup_miss_returns_none_and_invalid_arguments_raise(self) -> None:
        self.assertIsNone(self.lookup(new_request_id()))
        with self.assertRaises(InvalidInput):
            self.store.lookup_command_receipt(
                namespace="Bad Namespace", request_id=new_request_id()
            )
        with self.assertRaises(InvalidInput):
            self.store.lookup_command_receipt(namespace=NS, request_id="not-a-uuid")

    def test_lookup_is_namespace_isolated(self) -> None:
        request_id = new_request_id()
        first = self.commit(
            snapshot=self.store.capture_snapshot(NS),
            request_id=request_id,
            apply=append_apply(),
        )
        second = self.commit(
            snapshot=self.store.capture_snapshot(NS_B),
            request_id=request_id,
            apply=lambda scope: None,
        )
        self.assertEqual(NS, first.namespace)
        self.assertEqual(NS_B, second.namespace)

        found_first = self.lookup(request_id, NS)
        found_second = self.lookup(request_id, NS_B)
        self.assertIsNotNone(found_first)
        self.assertIsNotNone(found_second)
        self.assertEqual(NS, found_first.namespace)
        self.assertEqual(NS_B, found_second.namespace)
        self.assertEqual(first, found_first)
        self.assertEqual(second, found_second)

        # The same request id never leased a third namespace.
        self.assertIsNone(self.lookup(request_id, NS_C))

    def test_lookup_ignores_a_moved_watermark(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        committed = self.commit(
            snapshot=snapshot, request_id=new_request_id(), apply=append_apply()
        )
        self.assertEqual(1, committed.state_revision)

        with self.store.transaction():
            self.store.append_record_version(
                namespace=NS,
                record_id=OTHER_RECORD_ID,
                record_version=1,
                kind="Observation",
                body={"text": "second"},
            )
        self.assertEqual(2, self.store.current_state_revision(NS))

        found = self.lookup(committed.request_id)

        self.assertEqual(committed, found)
        self.assertEqual(1, found.state_revision)
        self.assertEqual(1, found.record_refs[0].record_version)
        self.assertEqual(2, self.store.current_state_revision(NS))

    def test_lookup_still_returns_the_receipt_after_the_source_is_deleted(
        self,
    ) -> None:
        snapshot = self.store.capture_snapshot(NS)
        committed = self.commit(
            snapshot=snapshot,
            request_id=new_request_id(),
            apply=append_apply(record_id=RECORD_ID, body={"text": SECRET}),
        )
        receipts_before = self.count_rows("processed_commands")

        self.store.request_deletion(
            target_kind="record",
            namespace=NS,
            target_id=RECORD_ID,
            authorization_ref="auth:test-lookup",
        )
        with self.assertRaises(Redacted):
            self.store.get_version(NS, RECORD_ID, 1)

        found = self.lookup(committed.request_id)

        self.assertEqual(committed, found)
        self.assertEqual(
            ((RECORD_ID, 1),),
            tuple((ref.record_id, ref.record_version) for ref in found.record_refs),
        )
        self.assertEqual(receipts_before, self.count_rows("processed_commands"))
        self.assertNotIn(SECRET, repr(found))

    def test_corrupt_receipt_raises_corrupt_store(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        committed = self.commit(
            snapshot=snapshot, request_id=new_request_id(), apply=append_apply()
        )
        original = self.receipt_json(committed.request_id)

        # (a) The column only checks ``length >= 2``, so two bytes are enough.
        self.overwrite_receipt_json(committed.request_id, "{{")
        with self.assertRaises(CorruptStore) as caught:
            self.lookup(committed.request_id)
        self.assertEqual("CORRUPT_STORE", caught.exception.code)

        # (b) A well-formed receipt that names another namespace is corrupt too.
        tampered = json.loads(original)
        tampered["namespace"] = NS_B
        self.overwrite_receipt_json(
            committed.request_id, canonical_json_text(tampered)
        )
        with self.assertRaises(CorruptStore) as caught_name:
            self.lookup(committed.request_id)
        self.assertEqual("CORRUPT_STORE", caught_name.exception.code)
        self.assertIsNotNone(caught_name.exception)

    def test_lookup_has_no_side_effects_and_takes_no_write_lock(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        committed = self.commit(
            snapshot=snapshot, request_id=new_request_id(), apply=append_apply()
        )
        miss_id = new_request_id()

        def counts() -> tuple[int, int, int, int]:
            return (
                self.store.current_state_revision(NS),
                self.count_rows("processed_commands"),
                self.count_rows("record_versions"),
                self.count_rows("context_snapshots"),
            )

        before = counts()

        self.assertEqual(committed, self.lookup(committed.request_id))
        self.assertIsNone(self.lookup(miss_id))
        self.assertEqual(committed, self.lookup(committed.request_id))

        self.assertEqual(before, counts())

        # A second connection holds BEGIN IMMEDIATE.  A lookup that tried to
        # open a write transaction (or take a write lock) would fail here.
        other = ControlStore.open(self.db_path)
        self.addCleanup(other.close)
        with other.transaction():
            self.assertEqual(committed, self.lookup(committed.request_id))
            self.assertIsNone(self.lookup(miss_id))

        self.assertEqual(before, counts())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
