"""RG08 R-C1: deterministic message/event time classification."""

from __future__ import annotations

import unittest
from datetime import datetime

from quintessence_pcs.domains.memory.time_window import (
    message_time,
    parse_event_time,
    parse_time_query,
)

ANCHOR = "2026-10-30T10:00:00+08:00"


def event(body: str, *, anchor=ANCHOR, observed=None, recorded=None, tz="Asia/Shanghai"):
    return parse_event_time(
        body,
        observed_at=anchor if observed is None else observed,
        recorded_at=recorded,
        timezone_name=tz,
    )


class EventTimeClassificationTests(unittest.TestCase):
    def test_yesterday_is_occurred_on_the_prior_day(self):
        record = event("我昨天去了招聘会。")
        self.assertEqual("occurred", record["event_type"])
        self.assertEqual("2026-10-29T00:00:00+08:00", record["event_time"])
        self.assertIsNone(record["planned_for"])
        self.assertEqual("day", record["time_precision"])
        self.assertEqual("relative_day", record["time_basis"])
        self.assertEqual("observed_at", record["message_time_basis"])
        self.assertEqual(ANCHOR, record["anchor"])

    def test_today_and_preday(self):
        self.assertEqual(
            "2026-10-30T00:00:00+08:00", event("我今天去了招聘会。")["event_time"]
        )
        self.assertEqual(
            "2026-10-28T00:00:00+08:00", event("我前天去了招聘会。")["event_time"]
        )

    def test_explicit_dates(self):
        self.assertEqual(
            "2026-10-28T00:00:00+08:00",
            event("2026-10-28 我去了招聘会。")["event_time"],
        )
        self.assertEqual(
            "explicit_date", event("10月28日我去了招聘会。")["time_basis"]
        )

    def test_tomorrow_plan_is_not_occurred(self):
        record = event("我明天去招聘会。")
        self.assertEqual("planned", record["event_type"])
        self.assertIsNone(record["event_time"])
        self.assertEqual("2026-10-31T00:00:00+08:00", record["planned_for"])

    def test_next_week_plan_keeps_no_day(self):
        record = event("我下周有个招聘会。")
        self.assertEqual("planned", record["event_type"])
        self.assertIsNone(record["event_time"])
        self.assertIsNone(record["planned_for"])

    def test_plan_verb_with_past_phrase_is_still_a_plan(self):
        record = event("我昨天要去招聘会。")
        self.assertEqual("planned", record["event_type"])
        self.assertIsNone(record["event_time"])

    def test_plan_cue_without_a_day_is_planned(self):
        record = event("我打算去招聘会。")
        self.assertEqual("planned", record["event_type"])
        self.assertIsNone(record["planned_for"])

    def test_quote_and_hypothesis_are_not_occurred(self):
        self.assertEqual("quoted", event("他说我昨天去了招聘会。")["event_type"])
        self.assertEqual("hypothetical", event("如果我昨天去了招聘会。")["event_type"])
        self.assertIsNone(event("他说我昨天去了招聘会。")["event_time"])

    def test_question_is_not_occurred(self):
        record = event("昨天发生了什么？")
        self.assertEqual("unknown", record["event_type"])
        self.assertEqual("question", record["reason"])

    def test_multiple_dates_are_unknown(self):
        record = event("我昨天去了招聘会，前天去面试。")
        self.assertEqual("unknown", record["event_type"])
        self.assertEqual("multiple_dates", record["reason"])

    def test_unsupported_expression_is_unknown(self):
        record = event("我昨天晚上去了招聘会。")
        self.assertEqual("unknown", record["event_type"])
        self.assertEqual("unsupported", record["reason"])

    def test_identifier_date_is_not_a_day(self):
        record = event("release-2026-09-18 修了什么？")
        self.assertEqual("unknown", record["event_type"])
        self.assertIsNone(record["event_time"])

    def test_vague_phrase_stays_unknown(self):
        record = event("之前去了招聘会。")
        self.assertEqual("unknown", record["event_type"])
        self.assertEqual("no_time", record["reason"])

    def test_assistant_or_artifact_is_never_parsed(self):
        record = event("我昨天去了招聘会。", observed=ANCHOR)
        self.assertEqual("occurred", record["event_type"])
        from quintessence_pcs.domains.memory.time_window import parse_event_time

        assistant = parse_event_time(
            "我昨天去了招聘会。",
            observed_at=ANCHOR,
            recorded_at=None,
            origin_kind="model_output",
            ref_type="collab_artifact",
        )
        self.assertEqual("unknown", assistant["event_type"])
        self.assertIsNone(assistant["event_time"])

    def test_recorded_at_fallback_never_derives_an_event_or_plan(self):
        for body in (
            "我昨天去了招聘会。",
            "我明天去招聘会。",
            "2026-10-28 我去了招聘会。",
            "下周有个招聘会。",
        ):
            record = parse_event_time(
                body,
                observed_at=None,
                recorded_at="2026-10-30T10:00:00+08:00",
            )
            self.assertEqual("recorded_at_fallback", record["message_time_basis"], body)
            self.assertEqual("unknown", record["event_type"], body)
            self.assertIsNone(record["event_time"], body)
            self.assertIsNone(record["planned_for"], body)
            self.assertEqual("unknown", record["time_precision"], body)
            self.assertEqual("recorded_at_fallback", record["reason"], body)

    def test_explicit_future_date_is_not_occurred_or_planned(self):
        record = event("2026-10-31 我去了招聘会。")
        self.assertEqual("unknown", record["event_type"])
        self.assertEqual("future", record["reason"])
        self.assertIsNone(record["event_time"])
        self.assertIsNone(record["planned_for"])

    def test_absurd_future_plan_is_unknown(self):
        record = event("2099-01-01 我打算去招聘会。")
        self.assertEqual("unknown", record["event_type"])
        self.assertEqual("future", record["reason"])
        self.assertIsNone(record["planned_for"])

    def test_event_time_is_never_later_than_the_message(self):
        anchor = datetime.fromisoformat(ANCHOR)
        for body in (
            "我今天去了招聘会。",
            "我昨天去了招聘会。",
            "我前天去了招聘会。",
            "2026-10-28 我去了招聘会。",
        ):
            record = event(body)
            self.assertEqual("occurred", record["event_type"], body)
            moment = datetime.fromisoformat(record["event_time"])
            self.assertLessEqual(moment, anchor, body)

    def test_timezone_changes_the_day_boundary(self):
        # 00:30+08 on 10-30 is still 10-29 in UTC.
        record = event("昨天去了招聘会。", anchor="2026-10-30T00:30:00+08:00", tz="UTC")
        self.assertEqual("2026-10-28T00:00:00+00:00", record["event_time"])

    def test_message_time_choice(self):
        value, basis = message_time("2026-10-30T10:00:00+08:00", "2026-10-31T00:00:00+08:00")
        self.assertEqual("2026-10-30T10:00:00+08:00", value)
        self.assertEqual("observed_at", basis)
        value, basis = message_time(None, "2026-10-31T00:00:00+08:00")
        self.assertEqual("recorded_at_fallback", basis)


class TimeQueryTests(unittest.TestCase):
    def query(self, text: str, *, anchor=ANCHOR):
        return parse_time_query(text, anchor=anchor)

    def test_yesterday_window(self):
        parsed = self.query("昨天发生了什么")
        self.assertIsNotNone(parsed)
        self.assertEqual(1, len(parsed.windows))
        self.assertEqual("event", parsed.intent)
        self.assertTrue(parsed.time_only)
        self.assertFalse(parsed.future)
        self.assertEqual("2026-10-28T16:00:00+00:00", parsed.windows[0].start.isoformat())
        self.assertEqual("2026-10-29T16:00:00+00:00", parsed.windows[0].end.isoformat())

    def test_topic_query_keeps_the_topic(self):
        parsed = self.query("昨天面试怎么样")
        self.assertFalse(parsed.time_only)
        self.assertIn("面试", parsed.rest)
        self.assertEqual("event", parsed.intent)

    def test_future_query_is_marked_and_has_no_window(self):
        parsed = self.query("明天有什么安排")
        self.assertTrue(parsed.future)
        self.assertEqual((), parsed.windows)

    def test_unsupported_query_has_no_window(self):
        parsed = self.query("昨晚聊了什么")
        self.assertFalse(parsed.future)
        self.assertEqual((), parsed.windows)
        self.assertEqual("unsupported", parsed.reason)

    def test_multiple_days_are_a_union(self):
        parsed = self.query("昨天和前天发生了什么")
        self.assertEqual(2, len(parsed.windows))

    def test_query_without_time_is_none(self):
        self.assertIsNone(self.query("你记得我喜欢什么颜色吗"))

    def test_today_weather_is_a_today_window_with_topic(self):
        parsed = self.query("今天天气怎么样")
        self.assertIsNotNone(parsed)
        self.assertEqual(1, len(parsed.windows))
        self.assertFalse(parsed.time_only)
        self.assertIn("天气", parsed.rest)


if __name__ == "__main__":
    unittest.main()
