"""PCS2-004 acceptance tests for the shared version store.

Methods ``test_01`` .. ``test_09`` map one-to-one to the nine required
scenarios in the handoff card.  Extra classes cover the hard storage
requirements that the card states in prose (immutability, payload integrity,
input validation, kind stability).
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from support import (  # noqa: E402
    BODIES,
    NAMESPACE,
    NAMESPACE_B,
    OTHER_RECORD_ID,
    RECORDED_AT,
    RECORD_ID,
    RECORD_ID_B,
    REPO_ROOT,
    SRC_DIR,
    StoreTestCase,
    append,
)

from quintessence_pcs.foundation import identifiers  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    CorruptStore,
    InvalidInput,
    InvalidRecordVersion,
    RecordKindMismatch,
    RecordNotFound,
    RecordVersionConflict,
    RecordVersionExists,
)
from quintessence_pcs.foundation.identifiers import MAX_SAFE_INTEGER  # noqa: E402
from quintessence_pcs.foundation.jsonutil import canonical_json_text, sha256_hex  # noqa: E402

_INSERT_VERSION_SQL = (
    "INSERT INTO record_versions ("
    "namespace, record_id, record_version, kind, schema_version, status, "
    "privacy_class, recorded_at, committed_state_revision, body_payload_ref"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class RequiredStorageScenarios(StoreTestCase):
    """The nine scenarios named by the PCS2-004 card."""

    def test_01_versions_coexist_and_current_pointer_is_three(self) -> None:
        self.seed_three_versions()

        self.assertEqual(3, self.current_pointer())
        for version, expected in BODIES.items():
            with self.subTest(version=version):
                stored = self.store.get_version(NAMESPACE, RECORD_ID, version)
                self.assertEqual(version, stored.record_version)
                self.assertEqual(expected, stored.body)
        self.assertEqual(BODIES[3], self.store.get_current(NAMESPACE, RECORD_ID).body)
        self.assertEqual(3, self.count_rows("record_versions"))

    def test_02_exact_read_bypasses_current_pointer(self) -> None:
        self.seed_three_versions()
        self.assertEqual(3, self.current_pointer())

        # Move the pointer behind the store's back.  Any implementation that
        # reads "the latest version" or follows records.current_version now
        # returns the wrong body for a historical version.
        cursor = self.store.connection.execute(
            "UPDATE records SET current_version = 1 WHERE namespace = ? AND record_id = ?",
            (NAMESPACE, RECORD_ID),
        )
        self.assertEqual(1, cursor.rowcount)

        middle = self.store.get_version(NAMESPACE, RECORD_ID, 2)
        self.assertEqual(BODIES[2], middle.body)
        self.assertEqual(BODIES[3], self.store.get_version(NAMESPACE, RECORD_ID, 3).body)
        self.assertEqual(BODIES[1], self.store.get_version(NAMESPACE, RECORD_ID, 1).body)

        # The pointer was moved on purpose; get_current proves the paths differ.
        self.assertEqual(1, self.store.get_current(NAMESPACE, RECORD_ID).record_version)

        # Structural evidence: the exact read issues no statement against the
        # current-pointer table at all.
        self._assert_exact_read_skips_pointer_table(2)

    def _assert_exact_read_skips_pointer_table(self, version: int):
        statements: list[str] = []
        self.store.connection.set_trace_callback(statements.append)
        try:
            stored = self.store.get_version(NAMESPACE, RECORD_ID, version)
        finally:
            self.store.connection.set_trace_callback(None)
        self.assertTrue(statements)
        pointer_queries = [
            statement
            for statement in statements
            if re.search(r"\brecords\b", statement, re.IGNORECASE)
        ]
        self.assertEqual([], pointer_queries, "exact read must not query records")
        return stored

    def test_03_history_survives_close_and_reopen(self) -> None:
        self.seed_three_versions()
        self.reopen()

        self.assertEqual(3, self.current_pointer())
        for version, expected in BODIES.items():
            with self.subTest(version=version):
                self.assertEqual(
                    expected, self.store.get_version(NAMESPACE, RECORD_ID, version).body
                )
        self.assertEqual(BODIES[3], self.store.get_current(NAMESPACE, RECORD_ID).body)

        # Redo scenario 2 after the restart, including the structural check.
        self.store.connection.execute(
            "UPDATE records SET current_version = 1 WHERE namespace = ? AND record_id = ?",
            (NAMESPACE, RECORD_ID),
        )
        self.assertEqual(BODIES[2], self.store.get_version(NAMESPACE, RECORD_ID, 2).body)
        self.assertEqual(BODIES[3], self.store.get_version(NAMESPACE, RECORD_ID, 3).body)
        self.assertEqual(
            1, self.store.get_current(NAMESPACE, RECORD_ID).record_version
        )
        self._assert_exact_read_skips_pointer_table(2)

    def test_03b_history_survives_a_separate_process(self) -> None:
        self.seed_three_versions()
        self.store.close()

        script = "\n".join(
            [
                "import sys",
                f"sys.path.insert(0, {str(SRC_DIR)!r})",
                "from quintessence_pcs.store.records import RecordStore",
                f"store = RecordStore.open({str(self.db_path)!r})",
                "store.connection.execute("
                "'UPDATE records SET current_version = 1 WHERE record_id = ?', "
                f"({RECORD_ID!r},))",
                f"for version in (1, 2, 3): print(store.get_version({NAMESPACE!r}, {RECORD_ID!r}, version).body['text'])",
                f"print(store.get_current({NAMESPACE!r}, {RECORD_ID!r}).record_version)",
                "store.close()",
            ]
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            ["first", "middle", "latest", "1"],
            completed.stdout.strip().splitlines(),
        )

        self.reopen()
        self.assertEqual(BODIES[2], self.store.get_version(NAMESPACE, RECORD_ID, 2).body)

    def test_04_duplicate_version_is_rejected_not_overwritten(self) -> None:
        self.seed_three_versions()
        payloads_before = self.count_rows("private_payloads")

        with self.assertRaises(RecordVersionExists) as caught:
            append(self.store, version=3, body={"text": "overwrite attempt"})
        self.assertEqual("ID_CONFLICT", caught.exception.code)

        self.assertEqual(3, self.current_pointer())
        self.assertEqual(3, self.count_rows("record_versions"))
        self.assertEqual(payloads_before, self.count_rows("private_payloads"))
        self.assertEqual(BODIES[3], self.store.get_version(NAMESPACE, RECORD_ID, 3).body)

        # The primary key is the final guard: a direct duplicate INSERT fails.
        payload_ref = self.store.get_version(NAMESPACE, RECORD_ID, 1).body_payload_ref
        with self.assertRaises(sqlite3.IntegrityError) as raw:
            self.store.connection.execute(
                _INSERT_VERSION_SQL,
                (
                    NAMESPACE,
                    RECORD_ID,
                    3,
                    "Observation",
                    "1",
                    "active",
                    "private",
                    RECORDED_AT,
                    1,
                    payload_ref,
                ),
            )
        self.assertIn("UNIQUE constraint failed", str(raw.exception))
        self.assertEqual(3, self.count_rows("record_versions"))
        self.assertEqual(BODIES[3], self.store.get_version(NAMESPACE, RECORD_ID, 3).body)

    def test_05_orphan_version_hits_foreign_key_and_pragma_is_enabled(self) -> None:
        append(self.store, version=1, body=BODIES[1])
        self.assertEqual(
            1, self.store.connection.execute("PRAGMA foreign_keys").fetchone()[0]
        )
        payload_ref = self.store.get_version(NAMESPACE, RECORD_ID, 1).body_payload_ref

        orphan = (
            NAMESPACE,
            OTHER_RECORD_ID,
            1,
            "Observation",
            "1",
            "active",
            "private",
            RECORDED_AT,
            1,
            payload_ref,
        )
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.store.connection.execute(_INSERT_VERSION_SQL, orphan)
        self.assertIn("FOREIGN KEY constraint failed", str(caught.exception))
        self.assertEqual(
            0,
            self.store.connection.execute(
                "SELECT COUNT(*) FROM record_versions WHERE record_id = ?",
                (OTHER_RECORD_ID,),
            ).fetchone()[0],
        )

        # foreign_keys is per connection: a reopened store must enforce it too.
        self.reopen()
        self.assertEqual(
            1, self.store.connection.execute("PRAGMA foreign_keys").fetchone()[0]
        )
        with self.assertRaises(sqlite3.IntegrityError) as reopened:
            self.store.connection.execute(_INSERT_VERSION_SQL, orphan)
        self.assertIn("FOREIGN KEY constraint failed", str(reopened.exception))

    def test_06_same_record_id_is_isolated_per_namespace(self) -> None:
        append(self.store, namespace=NAMESPACE, version=1, body={"text": "ns-a"})
        append(self.store, namespace=NAMESPACE_B, version=1, body={"text": "ns-b"})

        self.assertEqual(
            {"text": "ns-a"}, self.store.get_version(NAMESPACE, RECORD_ID, 1).body
        )
        self.assertEqual(
            {"text": "ns-b"}, self.store.get_version(NAMESPACE_B, RECORD_ID, 1).body
        )

        append(self.store, namespace=NAMESPACE, version=2, body={"text": "ns-a-2"})
        self.assertEqual(2, self.store.get_current(NAMESPACE, RECORD_ID).record_version)
        self.assertEqual(1, self.store.get_current(NAMESPACE_B, RECORD_ID).record_version)
        with self.assertRaises(RecordNotFound):
            self.store.get_version(NAMESPACE_B, RECORD_ID, 2)

        rows = self.store.connection.execute(
            "SELECT namespace, current_version FROM records WHERE record_id = ? "
            "ORDER BY namespace",
            (RECORD_ID,),
        ).fetchall()
        self.assertEqual(
            [(NAMESPACE, 2), (NAMESPACE_B, 1)],
            [(row["namespace"], row["current_version"]) for row in rows],
        )

    def test_07_non_positive_or_non_integer_version_is_rejected(self) -> None:
        bad_values = (0, -1, -100, True, False, 1.0, "1", None, MAX_SAFE_INTEGER + 1)
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(InvalidRecordVersion):
                    append(self.store, version=value, body={"text": "nope"})
        self.assertEqual(0, self.count_rows("records"))
        self.assertEqual(0, self.count_rows("record_versions"))

        # The database CHECK is the last line of defence for raw SQL.
        append(self.store, version=1, body=BODIES[1])
        payload_ref = self.store.get_version(NAMESPACE, RECORD_ID, 1).body_payload_ref
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.store.connection.execute(
                _INSERT_VERSION_SQL,
                (
                    NAMESPACE,
                    RECORD_ID,
                    0,
                    "Observation",
                    "1",
                    "active",
                    "private",
                    RECORDED_AT,
                    1,
                    payload_ref,
                ),
            )
        self.assertIn("CHECK constraint failed", str(caught.exception))

    def test_08_failed_second_write_rolls_back_whole_transaction(self) -> None:
        with self.assertRaises(RecordNotFound):
            with self.store.transaction():
                append(
                    self.store,
                    record_id=RECORD_ID,
                    version=1,
                    body={"text": "first record"},
                )
                # Second record: no records row and record_version 2 -> failure.
                append(
                    self.store,
                    record_id=RECORD_ID_B,
                    version=2,
                    body={"text": "second record"},
                )

        self.assertEqual(0, self.count_rows("records"))
        self.assertEqual(0, self.count_rows("record_versions"))
        self.assertEqual(0, self.count_rows("private_payloads"))
        self.assertEqual(0, self.store.current_state_revision(NAMESPACE))

        # The rolled-back transaction does not consume a revision number.
        with self.store.transaction():
            stored = append(self.store, version=1, body={"text": "first record"})
        self.assertEqual(1, stored.committed_state_revision)
        self.assertEqual(1, self.store.current_state_revision(NAMESPACE))

    def test_09_state_revision_monotonic_and_once_per_transaction(self) -> None:
        with self.store.transaction():
            first = self.store.next_state_revision(NAMESPACE)
            second = self.store.next_state_revision(NAMESPACE)
            self.assertEqual(first, second)
        self.assertEqual(first, self.store.current_state_revision(NAMESPACE))

        with self.store.transaction():
            v1 = append(self.store, version=1, body=BODIES[1])
            v2 = append(self.store, version=2, body=BODIES[2])
        self.assertEqual(v1.committed_state_revision, v2.committed_state_revision)
        self.assertEqual(first + 1, v1.committed_state_revision)

        v3 = append(self.store, version=3, body=BODIES[3])
        self.assertEqual(first + 2, v3.committed_state_revision)

        revisions = [
            self.store.get_version(NAMESPACE, RECORD_ID, version).committed_state_revision
            for version in (1, 2, 3)
        ]
        self.assertEqual(sorted(revisions), revisions)
        # v1/v2 shared one transaction, v3 got its own revision.
        self.assertEqual(2, len(set(revisions)))

        # Namespaces allocate independently.
        with self.store.transaction():
            other = append(
                self.store, namespace=NAMESPACE_B, version=1, body={"text": "other"}
            )
        self.assertEqual(1, other.committed_state_revision)
        self.assertEqual(first + 2, self.store.current_state_revision(NAMESPACE))
        self.assertEqual(1, self.store.current_state_revision(NAMESPACE_B))


class ImmutabilityAndIntegrityTests(StoreTestCase):
    def test_version_gap_and_stale_versions_are_rejected(self) -> None:
        append(self.store, version=1, body=BODIES[1])
        with self.assertRaises(RecordVersionConflict):
            append(self.store, version=3, body=BODIES[3])
        append(self.store, version=2, body=BODIES[2])
        with self.assertRaises(RecordVersionConflict):
            append(self.store, version=1, body=BODIES[1])
        self.assertEqual(2, self.current_pointer())
        self.assertEqual(2, self.count_rows("record_versions"))

    def test_kind_cannot_change_between_versions(self) -> None:
        append(self.store, version=1, body=BODIES[1], kind="Observation")
        with self.assertRaises(RecordKindMismatch) as caught:
            append(self.store, version=2, body=BODIES[2], kind="Belief")
        self.assertEqual("INVALID_TRANSITION", caught.exception.code)
        self.assertEqual(1, self.current_pointer())
        self.assertEqual(1, self.count_rows("record_versions"))

    def test_kind_mismatch_is_also_rejected_by_the_database(self) -> None:
        append(self.store, version=1, body=BODIES[1], kind="Observation")
        payload_ref = self.store.get_version(NAMESPACE, RECORD_ID, 1).body_payload_ref
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.store.connection.execute(
                _INSERT_VERSION_SQL,
                (
                    NAMESPACE,
                    RECORD_ID,
                    2,
                    "Belief",
                    "1",
                    "active",
                    "private",
                    RECORDED_AT,
                    1,
                    payload_ref,
                ),
            )
        self.assertIn("must match records.kind", str(caught.exception))

    def test_record_version_rows_cannot_be_updated_or_deleted(self) -> None:
        append(self.store, version=1, body=BODIES[1], status="active")
        update_sql = (
            "UPDATE record_versions SET status = 'retracted' "
            "WHERE namespace = ? AND record_id = ? AND record_version = 1"
        )
        with self.assertRaises(sqlite3.IntegrityError) as updated:
            self.store.connection.execute(update_sql, (NAMESPACE, RECORD_ID))
        self.assertIn("immutable", str(updated.exception))

        delete_sql = (
            "DELETE FROM record_versions "
            "WHERE namespace = ? AND record_id = ? AND record_version = 1"
        )
        with self.assertRaises(sqlite3.IntegrityError) as deleted:
            self.store.connection.execute(delete_sql, (NAMESPACE, RECORD_ID))
        self.assertIn("immutable", str(deleted.exception))

        stored = self.store.get_version(NAMESPACE, RECORD_ID, 1)
        self.assertEqual("active", stored.status)
        self.assertEqual(BODIES[1], stored.body)

    def test_payload_body_tampering_is_reported_as_corruption(self) -> None:
        stored = append(self.store, version=1, body={"text": "original"})
        self.store.connection.execute(
            "UPDATE private_payloads SET body_json = ? WHERE payload_ref = ?",
            ('{"text":"tampered"}', stored.body_payload_ref),
        )
        with self.assertRaises(CorruptStore) as caught:
            self.store.get_version(NAMESPACE, RECORD_ID, 1)
        self.assertEqual("CORRUPT_STORE", caught.exception.code)

    def test_payload_digest_tampering_is_reported_as_corruption(self) -> None:
        stored = append(self.store, version=1, body={"text": "original"})
        self.store.connection.execute(
            "UPDATE private_payloads SET sha256 = ? WHERE payload_ref = ?",
            ("0" * 64, stored.body_payload_ref),
        )
        with self.assertRaises(CorruptStore):
            self.store.get_version(NAMESPACE, RECORD_ID, 1)

    def test_payload_reference_is_opaque_and_not_derived_from_the_digest(self) -> None:
        """PCS2-006 B1: the immutable row keeps an uninformative ID, not a hash."""

        stored = append(self.store, version=1, body={"text": "original"})
        row = self.store.connection.execute(
            "SELECT payload_ref, sha256, body_json FROM private_payloads WHERE payload_ref = ?",
            (stored.body_payload_ref,),
        ).fetchone()
        self.assertTrue(row["payload_ref"].startswith("payload:"))
        self.assertNotEqual(f"sha256:{row['sha256']}", row["payload_ref"])
        self.assertEqual(sha256_hex(row["body_json"]), row["sha256"])

        # A referenced payload cannot be re-addressed: the foreign key holds.
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.store.connection.execute(
                "UPDATE private_payloads SET payload_ref = ? WHERE payload_ref = ?",
                ("payload:99999999-9999-4999-8999-999999999999", stored.body_payload_ref),
            )
        self.assertIn("FOREIGN KEY", str(caught.exception))

    def test_pre_0003_content_addressed_payload_is_refused(self) -> None:
        """0003 cannot rewrite immutable references; such rows demand a rebuild."""

        digest = sha256_hex(canonical_json_text({"text": "legacy"}))
        legacy_ref = f"sha256:{digest}"
        self.store.connection.execute(
            "INSERT INTO private_payloads (payload_ref, sha256, body_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (legacy_ref, digest, canonical_json_text({"text": "legacy"}), RECORDED_AT),
        )
        self.store.connection.execute(
            "INSERT INTO records (namespace, record_id, kind, current_version) "
            "VALUES (?, ?, 'Observation', 1)",
            (NAMESPACE, OTHER_RECORD_ID),
        )
        self.store.connection.execute(
            _INSERT_VERSION_SQL,
            (
                NAMESPACE,
                OTHER_RECORD_ID,
                1,
                "Observation",
                "1",
                "active",
                "private",
                RECORDED_AT,
                1,
                legacy_ref,
            ),
        )
        with self.assertRaises(CorruptStore) as caught:
            self.store.get_version(NAMESPACE, OTHER_RECORD_ID, 1)
        self.assertIn("pre-0003", str(caught.exception))

    def test_pointer_to_a_missing_version_is_reported_as_corruption(self) -> None:
        append(self.store, version=1, body=BODIES[1])
        self.store.connection.execute(
            "UPDATE records SET current_version = 2 WHERE namespace = ? AND record_id = ?",
            (NAMESPACE, RECORD_ID),
        )
        with self.assertRaises(CorruptStore):
            self.store.get_current(NAMESPACE, RECORD_ID)


class InputValidationTests(StoreTestCase):
    def test_invalid_namespaces_and_record_ids_are_rejected(self) -> None:
        for namespace in ("", "Example", "1example", "example private", 7):
            with self.subTest(namespace=namespace):
                with self.assertRaises(InvalidInput):
                    self.store.get_version(namespace, RECORD_ID, 1)
        for record_id in (
            "",
            "not-a-uuid",
            "22222222-2222-4222-8222-22222222222A",
            "22222222-2222-4222-8222-22222222222",
            7,
        ):
            with self.subTest(record_id=record_id):
                with self.assertRaises(InvalidInput):
                    self.store.get_version(NAMESPACE, record_id, 1)

    def test_body_must_be_a_finite_json_object(self) -> None:
        bad_bodies = (
            [],
            "text",
            None,
            {"x": float("nan")},
            {"x": float("inf")},
            {"x": object()},
            {"x": {1, 2}},
        )
        for body in bad_bodies:
            with self.subTest(body=type(body).__name__):
                with self.assertRaises(InvalidInput):
                    append(self.store, version=1, body=body)  # type: ignore[arg-type]
        self.assertEqual(0, self.count_rows("records"))

    def test_invalid_metadata_is_rejected(self) -> None:
        cases = (
            {"status": ""},
            {"status": "x" * 65},
            {"schema_version": ""},
            {"schema_version": "x" * 129},
            {"privacy_class": "secret"},
            {"kind": ""},
            {"kind": "x" * 129},
            {"recorded_at": "2026-10-17T00:00:00"},
            {"recorded_at": "2026-13-17T00:00:00Z"},
            {"recorded_at": "2026-10-17T00:00:00+00:00"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(InvalidInput):
                    append(self.store, version=1, body=BODIES[1], **overrides)
        self.assertEqual(0, self.count_rows("records"))

    def test_valid_metadata_round_trips(self) -> None:
        stored = append(
            self.store,
            version=1,
            body={"text": "round trip"},
            kind="Observation",
            status="candidate",
            schema_version="2",
            privacy_class="restricted",
            recorded_at="2026-10-17T12:34:56.123456Z",
        )
        reread = self.store.get_version(NAMESPACE, RECORD_ID, 1)
        self.assertEqual(stored, reread)
        self.assertEqual("candidate", reread.status)
        self.assertEqual("2", reread.schema_version)
        self.assertEqual("restricted", reread.privacy_class)
        self.assertEqual("2026-10-17T12:34:56.123456Z", reread.recorded_at)

    def test_identifier_constants_match_the_frozen_contract(self) -> None:
        import json

        common = json.loads(
            (REPO_ROOT / "contracts" / "v2" / "_common.json").read_text(encoding="utf-8")
        )
        defs = common["$defs"]
        self.assertEqual(defs["namespace"]["pattern"], identifiers.NAMESPACE_PATTERN.pattern)
        self.assertEqual(defs["uuid"]["pattern"], identifiers.UUID_PATTERN.pattern)
        self.assertEqual(
            defs["timestamp_utc"]["pattern"], identifiers.TIMESTAMP_PATTERN.pattern
        )
        self.assertEqual(
            defs["record_version_int"]["maximum"], identifiers.MAX_SAFE_INTEGER
        )
        self.assertEqual(
            defs["privacy_class"]["enum"], list(identifiers.PRIVACY_CLASSES)
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
