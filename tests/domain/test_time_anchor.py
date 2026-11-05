"""RG08 time-anchor observation: suspects, not verdicts."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
for _path in (HERE, SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.conversation.time_anchor import (  # noqa: E402
    find_day_anchors,
    find_suspect_day_anchors,
)


class DayAnchorTests(unittest.TestCase):
    def test_longest_token_wins(self) -> None:
        found = find_day_anchors("昨天晚上说的")
        self.assertEqual([("昨天晚上", "昨天")], found)

    def test_multiple_groups(self) -> None:
        found = dict((group, token) for token, group in find_day_anchors("昨天说好，明天再说"))
        self.assertEqual("昨天", found["昨天"])
        self.assertEqual("明天", found["明天"])


class SuspectTests(unittest.TestCase):
    def test_flags_an_anchor_absent_from_input_and_history(self) -> None:
        flags = find_suspect_day_anchors(
            "我不想起床啊", ["明天还要上课", "只要有你陪着就能接受"], "不过昨晚才说的"
        )
        self.assertEqual(1, len(flags))
        self.assertEqual("昨晚", flags[0]["anchor"])
        self.assertEqual("昨天", flags[0]["day"])

    def test_does_not_flag_when_the_input_has_the_same_day(self) -> None:
        flags = find_suspect_day_anchors(
            "我明天才上课呢", ["明天还要上课"], "明早我陪你起来"
        )
        self.assertEqual([], flags)

    def test_does_not_flag_when_history_has_the_same_day(self) -> None:
        flags = find_suspect_day_anchors(
            "我不想起床", ["明天还要上课"], "那就明天再说"
        )
        self.assertEqual([], flags)

    def test_plain_answers_have_no_flags(self) -> None:
        self.assertEqual([], find_suspect_day_anchors("今天好累", [], "那就早点睡"))

    def test_no_reply_no_flags(self) -> None:
        self.assertEqual([], find_suspect_day_anchors("明天上课", ["明天上课"], ""))


if __name__ == "__main__":
    unittest.main()
