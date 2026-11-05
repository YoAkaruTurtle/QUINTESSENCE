"""The 'own events' exception: a turn may only move the event watermark with
events its own tools created, and only when the range is provably complete."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _path in (_ROOT / "src", _ROOT / "tests" / "store", _ROOT / "tests" / "domain"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.foundation.errors import ContextStale  # noqa: E402
from quintessence_pcs.store import CognitionStore  # noqa: E402
from quintessence_pcs.store.tx_b import _require_only_own_events  # noqa: E402

NS = "example.test.cognition"


class OnlyOwnEventsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pcs-appended-")
        self.addCleanup(self._tmp.cleanup)
        self.store = CognitionStore.open(Path(self._tmp.name) / "pcs.sqlite")
        self.addCleanup(self.store.close)

    def _ingest(self, name: str) -> str:
        event = self.store.ingest_event(
            namespace=NS,
            source_kind="host_observation",
            source_instance="test",
            source_event_id=f"src-{name}",
            event_type=f"TestEvent:{name}",
            observed_at="2026-10-27T00:00:00Z",
            payload={"name": name},
            recorded_at="2026-10-27T00:00:00Z",
        )
        return event.event_id

    def _range(self) -> tuple[int, int]:
        row = self.store.connection.execute(
            "SELECT MIN(event_seq), MAX(event_seq) FROM event_journal WHERE namespace = ?",
            (NS,),
        ).fetchone()
        return int(row[0]), int(row[1])

    def test_one_and_two_own_events_pass(self) -> None:
        first = self._ingest("a")
        second = self._ingest("b")
        before, after = self._range()
        _require_only_own_events(self.store, NS, before - 1, after, (first, second))

    def test_a_foreign_event_in_the_range_is_refused(self) -> None:
        mine = self._ingest("mine")
        other = self._ingest("other")
        before, after = self._range()
        with self.assertRaises(ContextStale):
            _require_only_own_events(self.store, NS, before - 1, after, (mine,))

    def test_a_forged_id_is_refused(self) -> None:
        self._ingest("real")
        before, after = self._range()
        with self.assertRaises(ContextStale):
            _require_only_own_events(
                self.store, NS, before - 1, after, ("00000000-0000-4000-8000-000000000000",)
            )

    def test_a_range_with_a_hole_is_refused(self) -> None:
        first = self._ingest("a")
        second = self._ingest("b")
        before, after = self._range()
        # Claim the range is one longer than the rows that exist: the count
        # check must catch the gap even though every real id is whitelisted.
        with self.assertRaises(ContextStale):
            _require_only_own_events(self.store, NS, before - 1, after + 1, (first, second))

    def test_no_whitelist_keeps_the_old_behaviour(self) -> None:
        self._ingest("a")
        before, after = self._range()
        with self.assertRaises(ContextStale):
            _require_only_own_events(self.store, NS, before - 1, after, ())
