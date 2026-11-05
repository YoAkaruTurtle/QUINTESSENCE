"""J11: ``CognitionStore.open`` must never swallow a parent option again.

The override used to spell every parent keyword out by hand, so each new option
(``deletion_ledger_dir``, ``deletion_anchor_dir``, ``deletion_adopt``) was
silently dropped until the desktop host failed to start.  These tests pin the
forwarding contract itself, not one option at a time.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quintessence_pcs.store.cognition import CognitionStore
from quintessence_pcs.store.records import RecordStore


class _FakeStore:
    def __init__(self) -> None:
        self.reconciled = 0

    def reconcile_redacted_work(self) -> int:
        self.reconciled += 1
        return 0

    def close(self) -> None:  # pragma: no cover - only on failure
        pass


class CognitionOpenForwardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="j11-open-")
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "store.sqlite"

    def test_an_option_this_class_does_not_know_reaches_the_parent(self) -> None:
        fake = _FakeStore()
        captured: dict[str, object] = {}

        def fake_open(db_path, **options):
            captured.update(options)
            captured["db_path"] = db_path
            return fake

        with mock.patch.object(RecordStore, "open", classmethod(lambda cls, *a, **k: fake_open(*a, **k))):
            store = CognitionStore.open(
                self.path,
                migrations_dir=None,
                apply_migrations=False,
                deletion_ledger_dir="ledger",
                deletion_anchor_dir="anchor",
                deletion_adopt={"by": "operator", "reason": "test"},
                ledger_clock=object(),
                future_option="must survive",
            )

        self.assertIs(store, fake)
        self.assertEqual(captured["deletion_ledger_dir"], "ledger")
        self.assertEqual(captured["deletion_anchor_dir"], "anchor")
        self.assertEqual(captured["deletion_adopt"], {"by": "operator", "reason": "test"})
        self.assertIn("ledger_clock", captured)
        self.assertEqual(captured["future_option"], "must survive")
        self.assertFalse(captured["apply_migrations"])
        self.assertEqual(fake.reconciled, 1)

    def test_the_two_owned_options_stay_named(self) -> None:
        import inspect

        signature = inspect.signature(CognitionStore.open)
        self.assertIn("migrations_dir", signature.parameters)
        self.assertIn("apply_migrations", signature.parameters)
        forwarded = [
            name
            for name, parameter in signature.parameters.items()
            if parameter.kind is inspect.Parameter.VAR_KEYWORD
        ]
        self.assertEqual(forwarded, ["open_options"])


if __name__ == "__main__":
    unittest.main()
