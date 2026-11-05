"""RG08 time anchor: the v2 Context carries a trusted ``now`` and message times.

The anchor is either injected (tests) or read from the host clock; a message
without a stored timestamp keeps ``message_time: null`` and
``time_basis: "unknown"`` -- it is never back-filled or inferred.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains.persona.profiles import profile_record_id  # noqa: E402
from quintessence_pcs.domains.refs import RecordRef  # noqa: E402
from quintessence_pcs.runtime.context import ContextService  # noqa: E402
from quintessence_pcs.store.conversation import HistoryMessage  # noqa: E402

NOW = datetime(2026, 11, 3, 15, 48, 0, tzinfo=timezone.utc)


class TimeAnchorTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service = ContextService(
            self.store,
            self_view_service=self.self_view,
            profile_service=self.profiles,
            now_provider=lambda: NOW,
            local_timezone="+08:00",
        )
        pointer = self.store.get_current(NS, profile_record_id(NS, "example.synthetic.cognition"))
        self.profile_ref = RecordRef(
            namespace=NS,
            record_id=pointer.record_id,
            record_version=pointer.record_version,
        )
        self.profile_body = dict(pointer.body)

    def render(self, history, *, anchor=True):
        now_utc = NOW if anchor else None
        return self.service._render_consult_document(
            namespace=NS,
            run_id="run-1",
            profile_ref=self.profile_ref,
            profile_body=self.profile_body,
            self_view_ref=None,
            self_view_freshness="unavailable",
            self_view_body=None,
            inputs=(
                {"ref": {}, "untrusted": True, "content": "我不想起床啊"},
            ),
            omissions=(),
            history=tuple(history),
            time_anchor=(
                self.service._time_anchor_section(NOW) if anchor else None
            ),
            now_utc=now_utc,
        )

    def message(self, text, created_at):
        return HistoryMessage(
            role="user",
            text=text,
            ref_kind="collab_input",
            ref_id="11111111-1111-4111-8111-111111111111",
            ref_version=1,
            turn_no=1,
            seq=1,
            created_at=created_at,
        )

    def test_v2_document_has_utc_local_and_basis(self) -> None:
        doc = self.render([self.message("明天还要上课", "2026-11-03T15:42:22Z")])
        now = doc["now"]
        self.assertEqual("2026-11-03T15:48:00Z", now["utc"])
        self.assertEqual("+08:00", now["timezone"])
        self.assertEqual("explicit_offset", now["timezone_basis"])
        self.assertTrue(now["local"].startswith("2026-11-03T23:48:00"))

    def test_history_message_time_and_age_come_from_the_row(self) -> None:
        doc = self.render([self.message("明天还要上课", "2026-11-03T15:42:22Z")])
        entry = doc["history"][0]
        self.assertEqual("2026-11-03T15:42:22Z", entry["message_time"])
        self.assertEqual("message_created_at", entry["time_basis"])
        self.assertEqual(338, entry["age_seconds"])

    def test_missing_timestamp_is_unknown_not_inferred(self) -> None:
        doc = self.render([self.message("某句话", None)])
        entry = doc["history"][0]
        self.assertIsNone(entry["message_time"])
        self.assertEqual("unknown", entry["time_basis"])
        self.assertNotIn("age_seconds", entry)

    def test_without_anchor_the_document_is_the_old_shape(self) -> None:
        doc = self.render([self.message("明天还要上课", "2026-11-03T15:42:22Z")], anchor=False)
        self.assertNotIn("now", doc)
        entry = doc["history"][0]
        self.assertNotIn("message_time", entry)
        self.assertNotIn("time_basis", entry)
        self.assertEqual({"role", "text", "untrusted"}, set(entry))

    def test_no_clock_means_no_section(self) -> None:
        service = ContextService(
            self.store,
            self_view_service=self.self_view,
            profile_service=self.profiles,
            now_provider=lambda: None,
            local_timezone="+08:00",
        )
        doc = service._render_consult_document(
            namespace=NS,
            run_id="run-2",
            profile_ref=self.profile_ref,
            profile_body=self.profile_body,
            self_view_ref=None,
            self_view_freshness="unavailable",
            self_view_body=None,
            inputs=(),
            omissions=(),
            history=(self.message("某句话", "2026-11-03T15:42:22Z"),),
            time_anchor=None,
            now_utc=None,
        )
        self.assertNotIn("now", doc)
        self.assertNotIn("message_time", doc["history"][0])


if __name__ == "__main__":
    unittest.main()
