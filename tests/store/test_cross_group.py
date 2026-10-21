"""PCS2-006 cross-group tests: deletion vs. Tx B, and a real crash.

Two mandatory checks live here:

1. an end-to-end stale proposal: snapshot -> delete a source inside it ->
   commit with the old snapshot -> ``CONTEXT_STALE``, no new state and no
   deleted content returned or left in any text column;
2. a real process crash: Tx1 commits the barrier in a child process, the
   child is killed, and a second process reopens the database, still sees
   ``REDACTED`` / ``pending_cleanup``, resumes cleanup to ``cleaned`` without
   moving either watermark.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from support import REPO_ROOT, SRC_DIR, all_text_cells  # noqa: E402

from quintessence_pcs.foundation.errors import (  # noqa: E402
    ContextStale,
    Redacted,
    SourceRedacted,
)
from quintessence_pcs.store.control import ControlStore  # noqa: E402
from quintessence_pcs.store.deletion import (  # noqa: E402
    DELETION_CLEANED,
    DELETION_EVENT,
    DELETION_PENDING_CLEANUP,
)

NS = "example.test.cross"
CRASH_NS = "example.test.crash"
SECRET = "cross-group-secret-that-must-not-come-back"


class EndToEndStaleProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pcs2-006-cross-")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "cross.sqlite"
        self.store = ControlStore.open(self.db_path)
        self.addCleanup(self.store.close)

    def test_old_snapshot_after_deletion_is_stale_and_returns_no_content(self) -> None:
        receipt = self.store.ingest_event(
            namespace=NS,
            source_kind="synthetic_fixture",
            source_instance="cross-instance",
            source_event_id="cross-source-1",
            event_type="ObservationRecorded",
            payload={"text": SECRET},
            recorded_at="2026-10-17T00:00:00Z",
        )
        digest = self.store.connection.execute(
            "SELECT sha256 FROM private_payloads WHERE payload_ref = "
            "(SELECT payload_ref FROM event_journal WHERE event_id = ?)",
            (receipt.event_id,),
        ).fetchone()["sha256"]
        snapshot = self.store.capture_snapshot(NS)
        self.assertEqual(
            (0, 1, 0),
            (
                snapshot.state_revision,
                snapshot.context_event_seq,
                snapshot.privacy_epoch,
            ),
        )

        self.store.request_deletion(
            target_kind=DELETION_EVENT,
            namespace=NS,
            target_id=receipt.event_id,
            authorization_ref="auth:cross-1",
        )
        self.store.cleanup_deletions()
        revision_after_delete = self.store.current_state_revision(NS)
        self.assertEqual(1, revision_after_delete)

        apply_calls: list[bool] = []

        def apply(scope: object) -> None:  # pragma: no cover - must not run
            apply_calls.append(True)
            scope.append_record_version(  # type: ignore[attr-defined]
                record_id=str(uuid.uuid4()),
                record_version=1,
                kind="Observation",
                body={"text": SECRET},
            )

        request_id = str(uuid.uuid4())
        with self.assertRaises(ContextStale) as caught:
            self.store.commit_command(
                context_id=snapshot.context_id,
                request_id=request_id,
                operation="proposal.submit",
                request_payload={"kind": "Observation", "body": {"text": SECRET}},
                apply=apply,
            )
        self.assertEqual("CONTEXT_STALE", caught.exception.code)
        self.assertEqual([], apply_calls)
        self.assertNotIn(SECRET, str(caught.exception))

        # No new state, no receipt, no record version.
        self.assertEqual(revision_after_delete, self.store.current_state_revision(NS))
        self.assertEqual(
            0,
            int(self.store.connection.execute(
                "SELECT COUNT(*) FROM processed_commands WHERE request_id = ?", (request_id,)
            ).fetchone()[0]),
        )
        self.assertEqual(
            0,
            int(self.store.connection.execute("SELECT COUNT(*) FROM record_versions").fetchone()[0]),
        )

        # Nothing in the database still carries the deleted sentence or its
        # digest, and the deleted read stays redacted.
        hits = [
            (location, value)
            for location, value in all_text_cells(self.store.connection)
            if SECRET in value or digest in value
        ]
        self.assertEqual([], hits)
        with self.assertRaises(Redacted):
            self.store.get_event(NS, receipt.event_id)

        # A fresh snapshot is a legitimate basis again, but the deleted event
        # is still unavailable and its source key cannot be resurrected.
        fresh = self.store.capture_snapshot(NS)
        self.assertEqual(1, fresh.state_revision)
        self.assertEqual(1, fresh.privacy_epoch)
        with self.assertRaises(SourceRedacted):
            self.store.ingest_event(
                namespace=NS,
                source_kind="synthetic_fixture",
                source_instance="cross-instance",
                source_event_id="cross-source-1",
                event_type="ObservationRecorded",
                payload={"text": SECRET},
                recorded_at="2026-10-17T00:00:00Z",
            )


_CHILD_CODE = """
import sys
import time
from pathlib import Path

from quintessence_pcs.store.control import ControlStore

db_path, marker_path = sys.argv[1], sys.argv[2]
namespace = "example.test.crash"
store = ControlStore.open(db_path)
receipt = store.ingest_event(
    namespace=namespace,
    source_kind="synthetic_fixture",
    source_instance="crash-child",
    source_event_id="crash-source-1",
    event_type="ObservationRecorded",
    payload={"text": SECRET_VALUE},
    recorded_at="2026-10-17T00:00:00Z",
)
store.request_deletion(
    target_kind="event",
    namespace=namespace,
    target_id=receipt.event_id,
    authorization_ref="auth:crash-1",
)
Path(marker_path).write_text(receipt.event_id, encoding="utf-8")
while True:
    time.sleep(0.05)
"""


class CrashRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pcs2-006-crash-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "crash.sqlite"
        self.marker = self.root / "committed.marker"

    def test_tx1_survives_a_killed_process_and_cleanup_resumes(self) -> None:
        child_code = _CHILD_CODE.replace("SECRET_VALUE", repr(SECRET))
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(SRC_DIR) + (
            os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
        )
        process = subprocess.Popen(
            [sys.executable, "-c", child_code, str(self.db_path), str(self.marker)],
            cwd=str(REPO_ROOT),
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())

        event_id = self._wait_for_marker(process)
        # Kill the writer immediately after the barrier commit: no cleanup, no
        # orderly close, no at-exit handler.
        process.kill()
        process.wait(timeout=10)
        if process.stderr is not None:
            process.stderr.close()
        self.assertIsNotNone(process.returncode)

        store = ControlStore.open(self.db_path)
        self.addCleanup(store.close)

        # 1. Tx1 is durable and the read barrier is already in force.
        with self.assertRaises(Redacted) as caught:
            store.get_event(CRASH_NS, event_id)
        self.assertEqual("REDACTED", caught.exception.code)

        # 2. The target is still pending_cleanup (the child never cleaned).
        target = store.get_deletion_target(
            target_kind=DELETION_EVENT,
            namespace=CRASH_NS,
            target_id=event_id,
        )
        self.assertIsNotNone(target)
        self.assertEqual(DELETION_PENDING_CLEANUP, target.status)
        self.assertEqual("auth:crash-1", target.authorization_ref)

        # 3. The body was not cleared before the kill; the barrier alone is
        #    what made the read unavailable.
        payload_ref = store.connection.execute(
            "SELECT payload_ref FROM event_journal WHERE event_id = ?", (event_id,)
        ).fetchone()["payload_ref"]
        before_cleanup = store.connection.execute(
            "SELECT body_json, sha256 FROM private_payloads WHERE payload_ref = ?",
            (payload_ref,),
        ).fetchone()
        self.assertNotEqual("", before_cleanup["body_json"])

        watermarks = self._watermarks(store)
        self.assertEqual((1, 1, 1), watermarks)

        # 4. A second process (this one) resumes cleanup to cleaned.
        report = store.cleanup_deletions()
        self.assertEqual(1, report.cleaned_count)
        self.assertEqual(0, report.failed_count)
        self.assertEqual(1, report.cleared_payloads)
        target = store.get_deletion_target(
            target_kind=DELETION_EVENT,
            namespace=CRASH_NS,
            target_id=event_id,
        )
        self.assertEqual(DELETION_CLEANED, target.status)
        self.assertEqual(watermarks, self._watermarks(store))
        self.assertEqual("", store.connection.execute(
            "SELECT body_json FROM private_payloads WHERE payload_ref = ?", (payload_ref,)
        ).fetchone()["body_json"])

        # 5. Nothing resurrects the source, and the digest is gone everywhere.
        with self.assertRaises(SourceRedacted):
            store.ingest_event(
                namespace=CRASH_NS,
                source_kind="synthetic_fixture",
                source_instance="crash-child",
                source_event_id="crash-source-1",
                event_type="ObservationRecorded",
                payload={"text": SECRET},
                recorded_at="2026-10-17T00:00:00Z",
            )
        digest = before_cleanup["sha256"]
        hits = [
            (location, value)
            for location, value in all_text_cells(store.connection)
            if SECRET in value or digest in value
        ]
        self.assertEqual([], hits)

    def _wait_for_marker(self, process: subprocess.Popen[str]) -> str:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self.marker.exists():
                text = self.marker.read_text(encoding="utf-8").strip()
                if text:
                    return text
            if process.poll() is not None:
                stderr = process.stderr.read() if process.stderr is not None else ""
                if process.stderr is not None:
                    process.stderr.close()
                self.fail(f"child exited before the barrier marker: {stderr}")
            time.sleep(0.02)
        self.fail("child did not commit the barrier within 30 seconds")

    @staticmethod
    def _watermarks(store: ControlStore) -> tuple[int, int, int]:
        row = store.connection.execute(
            "SELECT state_revision, last_event_seq, privacy_epoch "
            "FROM namespace_state WHERE namespace = ?",
            (CRASH_NS,),
        ).fetchone()
        return (int(row["state_revision"]), int(row["last_event_seq"]), int(row["privacy_epoch"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
