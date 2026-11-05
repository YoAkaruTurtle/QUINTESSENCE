"""J11: a copied memory instance must fail loudly instead of losing recall.

The upstream memory store records the absolute directory it was created with
inside ``instance_meta``.  Copying an instance leaves that value pointing at the
old place, and the only symptom is ``IDENTITY_UNBOUND`` at the first recall.
These tests pin the two halves of the repair: verification refuses a mismatch,
and an explicitly attributed adoption fixes it once.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from quintessence_pcs.domains.memory.instance_binding import (
    ADOPTION_LOG_NAME,
    IDENTITY_UNBOUND,
    MEMORY_DB_NAME,
    InstanceBindingMismatch,
    InstanceBindingRefused,
    adopt,
    ensure_binding,
    normalize,
    read_recorded_directory,
)


def _make_store(directory: Path, recorded: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MEMORY_DB_NAME
    connection = sqlite3.connect(str(path))
    try:
        with connection:
            connection.execute(
                "CREATE TABLE instance_meta ("
                "singleton INTEGER PRIMARY KEY CHECK (singleton = 1),"
                "agent_id TEXT NOT NULL,"
                "installation_id TEXT NOT NULL,"
                "data_directory TEXT NOT NULL,"
                "test_mode INTEGER NOT NULL)"
            )
            connection.execute(
                "INSERT INTO instance_meta VALUES (1, 'agent:lab', 'install:lab', ?, 1)",
                (recorded,),
            )
    finally:
        connection.close()
    return path


class InstanceBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="j11-binding-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.memory = self.root / "instance" / "memory"

    def test_a_missing_store_has_nothing_to_verify(self) -> None:
        self.assertIsNone(read_recorded_directory(self.memory))
        self.assertIsNone(ensure_binding(self.memory))

    def test_a_matching_store_is_left_alone(self) -> None:
        # The upstream writes the normcased spelling, so this is the shape of a
        # store that already belongs here: nothing to adopt and nothing to fix.
        _make_store(self.memory, normalize(self.memory))
        self.assertIsNone(ensure_binding(self.memory))
        self.assertFalse((self.memory / ADOPTION_LOG_NAME).exists())

    def test_a_copied_store_fails_at_startup_with_the_reason(self) -> None:
        _make_store(self.memory, str(self.root / "old" / "memory"))
        with self.assertRaises(InstanceBindingMismatch) as caught:
            ensure_binding(self.memory)
        error = caught.exception
        self.assertEqual(error.code, IDENTITY_UNBOUND)
        self.assertIn("adopt_data_directory", str(error))
        self.assertIn("IDENTITY_UNBOUND", str(error))

    def test_adoption_rewrites_the_row_and_records_who_and_why(self) -> None:
        old = str(self.root / "old" / "memory")
        _make_store(self.memory, old)
        record = ensure_binding(
            self.memory,
            adopt_by="operator",
            adopt_reason="moved the lab instance",
            now="2026-11-05T00:00:00Z",
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["old_data_directory"], old)
        self.assertEqual(record["adopted_by"], "operator")
        self.assertEqual(record["adopt_reason"], "moved the lab instance")
        self.assertEqual(
            normalize(read_recorded_directory(self.memory)), normalize(self.memory)
        )
        lines = (self.memory / ADOPTION_LOG_NAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["adopted_by"], "operator")

    def test_adoption_is_refused_when_it_is_not_needed(self) -> None:
        _make_store(self.memory, str(self.memory))
        with self.assertRaises(InstanceBindingRefused):
            adopt(self.memory, by="operator", reason="no-op")
        self.assertFalse((self.memory / ADOPTION_LOG_NAME).exists())

    def test_adoption_cannot_be_anonymous(self) -> None:
        _make_store(self.memory, str(self.root / "old" / "memory"))
        for by, reason in (("", "moved"), ("operator", "   ")):
            with self.assertRaises(InstanceBindingRefused):
                adopt(self.memory, by=by, reason=reason)
        self.assertFalse((self.memory / ADOPTION_LOG_NAME).exists())

    def test_adoption_without_a_meta_row_is_refused(self) -> None:
        self.memory.mkdir(parents=True)
        path = self.memory / MEMORY_DB_NAME
        sqlite3.connect(str(path)).close()
        with self.assertRaises(InstanceBindingRefused):
            adopt(self.memory, by="operator", reason="empty store")

    def test_normalisation_is_case_insensitive_on_windows(self) -> None:
        self.assertEqual(
            normalize(Path(self.root.name.upper() + "\\memory")),
            normalize(Path(self.root.name.lower() + "\\memory")),
        )

    def test_a_same_directory_spelling_difference_is_canonicalised(self) -> None:
        """The upstream compares the stored text with SQL equality.

        A copy written by an older build can hold an upper-case drive prefix while the binding
        computes the lower-cased form.  The identity is the same, so no adoption is
        needed — but the raw text must be rewritten or every recall fails with
        a bare ``IDENTITY_UNBOUND``.
        """

        upper = str(self.memory).upper()
        _make_store(self.memory, upper)
        record = ensure_binding(self.memory)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["kind"], "memory_instance_binding_canonicalisation")
        self.assertEqual(record["old_data_directory"], upper)
        recorded = read_recorded_directory(self.memory)
        self.assertEqual(recorded, normalize(self.memory))
        self.assertIsNone(ensure_binding(self.memory))

    def test_adoption_writes_the_normcased_directory(self) -> None:
        _make_store(self.memory, str(self.root / "old" / "memory"))
        adopt(self.memory, by="operator", reason="moved")
        self.assertEqual(read_recorded_directory(self.memory), normalize(self.memory))


if __name__ == "__main__":
    unittest.main()
