"""Calendar journal periods, private storage, and checkpoint recovery."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quintessence_pcs.foundation.errors import CorruptStore, InvalidInput
from quintessence_pcs.foundation.jsonutil import canonical_json_text
from quintessence_pcs.runtime.dreaming_journal import CalendarJournalStore, JournalPeriod


INSTANCE = "11001100-0000-4000-8000-000011001100"
TIMEZONE = "Asia/Shanghai"


def instant(year, month, day):
    return datetime(year, month, day, 3, tzinfo=UTC)


class CalendarJournalTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="calendar-journal-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name) / "dreaming"
        self.directory.mkdir()
        (self.directory / "owner.json").write_text(canonical_json_text({
            "schema": "quintessence.dreaming.instance.v1",
            "instance_id": INSTANCE,
        }), encoding="utf-8")

    def reopen(self):
        return CalendarJournalStore(self.directory, instance_id=INSTANCE)

    def drain(self, store, through):
        entries = []
        while (period := store.next_due(through, TIMEZONE)) is not None:
            entries.append(store.save(
                period,
                text=f"My {period.kind} notes from {period.start.isoformat()}",
                tags=("notes",),
                refs=(),
                children=(entry.revision for entry in store.inputs(period)),
            ))
        return entries

    def test_only_witnessed_days_are_due_and_duplicate_observations_are_idempotent(self):
        store = self.reopen()
        self.assertTrue(store.record_running(instant(2026, 11, 6), TIMEZONE))
        self.assertFalse(store.record_running(instant(2026, 11, 6), TIMEZONE))
        self.assertTrue(store.record_running(instant(2026, 11, 8), TIMEZONE))
        self.assertIsNone(store.next_due(instant(2026, 11, 6), TIMEZONE))
        entries = self.drain(self.reopen(), instant(2026, 11, 9))
        self.assertEqual(
            [(entry.period.kind, entry.period.start) for entry in entries],
            [("day", date(2026, 11, 6)), ("day", date(2026, 11, 8)), ("week", date(2026, 11, 2))],
        )
        self.assertEqual(entries[-1].children, tuple(entry.revision for entry in entries[:2]))
        self.assertIsNone(store.get(JournalPeriod.containing("day", date(2026, 11, 7), TIMEZONE)))

    def test_failed_oldest_period_can_wait_without_blocking_later_work(self):
        store = self.reopen()
        monday = instant(2026, 11, 2)
        tuesday = monday + timedelta(days=1)
        wednesday = tuesday + timedelta(days=1)
        store.record_running(monday, TIMEZONE)
        store.record_running(tuesday, TIMEZONE)
        oldest = store.next_due(wednesday, TIMEZONE)
        self.assertEqual(oldest.start, monday.date())
        retry_at = wednesday + timedelta(minutes=1)
        store.defer(oldest, until=retry_at)
        self.assertEqual(self.reopen().next_due(wednesday, TIMEZONE).start, tuesday.date())
        self.assertEqual(self.reopen().next_due(retry_at, TIMEZONE), oldest)

    def test_month_uses_whole_weeks_and_daily_fragments_without_waiting_for_next_month(self):
        store = self.reopen()
        for day in (instant(2026, 1, 1), instant(2026, 1, 7), instant(2026, 1, 31), instant(2026, 2, 1)):
            store.record_running(day, TIMEZONE)
        january = JournalPeriod.containing("month", date(2026, 1, 1), TIMEZONE)
        with self.assertRaisesRegex(InvalidInput, "child"):
            store.inputs(january)
        self.drain(store, instant(2026, 2, 1))
        first = store.get(january)
        children = [store.get_revision(revision) for revision in first.children]
        self.assertEqual(
            [(entry.period.kind, entry.period.start) for entry in children],
            [("day", date(2026, 1, 1)), ("week", date(2026, 1, 5)), ("day", date(2026, 1, 31))],
        )
        self.drain(store, instant(2026, 2, 2))
        crossing = store.get(JournalPeriod.containing("week", date(2026, 1, 31), TIMEZONE))
        self.assertEqual(
            [store.get_revision(revision).period.start for revision in crossing.children],
            [date(2026, 1, 31), date(2026, 2, 1)],
        )
        self.assertEqual(store.get(january), first)

    def test_year_uses_only_completed_months_with_witnessed_days(self):
        store = self.reopen()
        store.record_running(instant(2026, 11, 3), TIMEZONE)
        store.record_running(instant(2026, 12, 31), TIMEZONE)
        entries = self.drain(store, instant(2027, 1, 1))
        annual = entries[-1]
        self.assertEqual((annual.period.kind, annual.period.start, annual.period.end), (
            "year", date(2026, 1, 1), date(2027, 1, 1),
        ))
        self.assertEqual(
            [store.get_revision(revision).period.start for revision in annual.children],
            [date(2026, 11, 1), date(2026, 12, 1)],
        )

    def test_long_journal_and_progress_once_with_exact_sources(self):
        store = self.reopen()
        now = instant(2026, 11, 5)
        store.record_running(now, TIMEZONE)
        tomorrow = now + timedelta(days=1)
        period = store.next_due(tomorrow, TIMEZONE)
        refs = tuple({"ref_type": "synthetic", "id": index} for index in range(400))
        text = "The complete entry includes a long review of the experience.\n" * 4096
        store.save_progress(period, "old", {"fragments": ["first"]})
        self.assertEqual(self.reopen().load_progress(period, "old"), {"fragments": ["first"]})
        self.assertIsNone(store.load_progress(period, "new"))
        store.set_authorization("approved-live-model-route")
        entry = self.reopen().save(period, text=text, tags=["history", "history"], refs=refs, children=())
        self.assertEqual(store.get_revision(entry.revision), entry)
        self.assertEqual(store.recent(tomorrow, TIMEZONE), (entry,))
        self.assertEqual((entry.text, entry.tags, entry.refs), (text, ("history",), refs))
        self.assertIsNone(store.load_progress(period, "old"))
        self.assertEqual(store.save(period, text=text, tags=["history"], refs=refs, children=()), entry)
        with self.assertRaisesRegex(InvalidInput, "already finalized"):
            store.save(period, text="changed", tags=[], refs=[], children=[])
        with self.assertRaisesRegex(InvalidInput, "no pending work"):
            store.save_progress(period, "old", {"fragments": ["second"]})
        self.assertEqual(self.reopen().authorization(), "approved-live-model-route")
        store.set_authorization(None)
        self.assertIsNone(self.reopen().authorization())

    def test_foreign_database_and_sideless_collision_are_preserved(self):
        path = self.directory / "calendar.sqlite3"
        wake_log = self.directory / "wake.sqlite3"
        wake_log.write_bytes(b"prior Dreaming state")
        collision = self.directory / "calendar.sqlite3-wal"
        collision.write_bytes(b"unknown sidecar")
        with self.assertRaises(CorruptStore):
            self.reopen()
        self.assertEqual(collision.read_bytes(), b"unknown sidecar")
        self.assertFalse(path.exists())
        collision.unlink()
        path.write_bytes(b"someone else's data")
        with self.assertRaises(CorruptStore):
            self.reopen()
        self.assertEqual(path.read_bytes(), b"someone else's data")
        self.assertEqual(wake_log.read_bytes(), b"prior Dreaming state")

    def test_foreign_hard_link_is_preserved(self):
        path = self.directory / "calendar.sqlite3"
        original = self.directory / "other-data"
        original.write_bytes(b"unrelated file")
        try:
            os.link(original, path)
        except OSError as exc:
            self.skipTest(f"Hard links are unavailable: {exc}")
        with self.assertRaises(CorruptStore):
            self.reopen()
        self.assertEqual(path.read_bytes(), b"unrelated file")
        self.assertEqual(original.read_bytes(), b"unrelated file")

    def test_corrupt_payload_cannot_be_returned_as_exact_revision(self):
        store = self.reopen()
        store.record_running(instant(2026, 11, 5), TIMEZONE)
        entry = self.drain(store, instant(2026, 11, 6))[0]
        db = sqlite3.connect((self.directory / "calendar.sqlite3").as_uri() + "?mode=rw", uri=True)
        self.addCleanup(db.close)
        with db:
            db.execute("UPDATE periods SET payload = replace(payload, 'My ', 'Your ') WHERE revision = ?", (entry.revision,))
        with self.assertRaises(CorruptStore):
            store.get_revision(entry.revision)

    def test_local_midnight_and_dst_boundaries(self):
        calendar = self.reopen()
        before = datetime(2026, 11, 6, 15, 59, tzinfo=UTC)
        after = before + timedelta(minutes=1)
        calendar.record_running(before, TIMEZONE)
        self.assertIsNone(calendar.next_due(before, TIMEZONE))
        due = calendar.next_due(after, TIMEZONE)
        self.assertEqual(due.start, date(2026, 11, 6))
        self.assertEqual(due.bounds, (
            datetime(2026, 11, 5, 16, tzinfo=UTC), datetime(2026, 11, 6, 16, tzinfo=UTC),
        ))
        try:
            spring = JournalPeriod.containing("day", date(2026, 3, 8), "America/New_York")
            fall = JournalPeriod.containing("day", date(2026, 11, 1), "America/New_York")
        except ValueError:
            self.skipTest("The test interpreter has no IANA tzdata")
        self.assertEqual(spring.bounds[1] - spring.bounds[0], timedelta(hours=23))
        self.assertEqual(fall.bounds[1] - fall.bounds[0], timedelta(hours=25))
