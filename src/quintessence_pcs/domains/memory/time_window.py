"""RG08 R-C1: deterministic message/event time anchoring.

Pure functions only: no store, no adapter, no model, no clock of its own.  The
caller supplies the anchor (the message's ``observed_at``, or its
``recorded_at`` when absent) and the configured zone; every window is a whole
local day ``[00:00, next 00:00)`` converted to UTC for comparison.

The classification comes first: only a plainly stated, already-happened event
writes ``event_time``.  A plan ("tomorrow I will go"), an advance mention, a
quote, a hypothesis, a question or several different days never becomes an
occurred event; a plan may keep a ``planned_for`` day but never an occurrence.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

PARSER = "rg08-time-v2"
DEFAULT_TIMEZONE = "Asia/Shanghai"

#: A whole day cannot be read further back than this without a year.
_MONTH_DAY_REACH = 183
#: A query longer than this is not read for its days.
_MAX_QUERY_CHARS = 512
_MAX_DAYS = 3
_MAX_N_DAYS = 30
_MAX_FUTURE_DAYS = 366

_ASCII_WORD = r"A-Za-z0-9_./-"

#: Supported past whole-day expressions, resolved against the local anchor day.
_RELATIVE = (
    ("大前天", 3),
    ("前天", 2),
    ("昨天", 1),
    ("昨日", 1),
    ("今天", 0),
    ("今日", 0),
)
_N_DAYS = re.compile(r"(\d{1,2})\s*天前")
_ISO_DATE = re.compile(
    rf"(?<![{_ASCII_WORD}])(\d{{4}})[-/年](\d{{1,2}})[-/月](\d{{1,2}})(?:日)?(?![{_ASCII_WORD}])"
)
_MONTH_DAY = re.compile(r"(?<![0-9年月])(\d{1,2})月(\d{1,2})日")

#: Future single days: a plan may name them, never an occurrence.
_FUTURE_RELATIVE = (
    ("大后天", 3),
    ("后天", 2),
    ("明天", 1),
    ("明日", 1),
)
_N_DAYS_AFTER = re.compile(r"(\d{1,2})\s*天后")
_FUTURE_RANGE = re.compile(
    r"下周|下星期|下礼拜|下个月|明年|后年|将来|以后|未来"
)

#: Recognised but not read as a whole past day in V1.
_UNSUPPORTED = re.compile(
    r"上周|上星期|上礼拜|本周|这周|这星期|这礼拜|"
    r"周[一二三四五六日天]|星期[一二三四五六日天]|礼拜[一二三四五六日天]|"
    r"上个月|这个月|本月|"
    r"\d+个?月前|\d+年前|"
    r"凌晨|早上|上午|中午|下午|傍晚|晚上|夜里|半夜|今晚|今早|今晨|昨晚"
)

_QUOTE_CHARS = re.compile(r"[「」『』“”\"']")
_QUOTE_CUES = ("他说", "她说", "据说", "听说", "根据", "按照", "引用", "原文", "转述", "自称")
_HYPOTHETICAL = (
    "如果",
    "假如",
    "要是",
    "万一",
    "梦到",
    "梦见",
    "本想",
    "本来想",
    "本来要",
    "差点",
)
_PLAN_CUES = (
    "打算",
    "准备",
    "计划",
    "安排",
    "要去",
    "要来",
    "要买",
    "要做",
    "要去",
    "将去",
    "将要",
    "将会",
    "想去",
    "想要",
)
_QUESTION_CUES = ("什么", "怎么", "怎样", "如何", "哪", "谁", "多少", "吗")

_MESSAGE_CUES = ("聊", "说", "讲", "提", "问", "讨论", "对话", "消息", "记录", "告诉", "回答")
_EVENT_CUES = (
    "发生",
    "做",
    "干",
    "去",
    "来",
    "见",
    "吃",
    "买",
    "修",
    "部署",
    "上线",
    "提交",
    "住院",
    "生病",
    "面试",
    "开会",
    "出差",
    "旅行",
    "招聘会",
    "结婚",
    "搬家",
    "考试",
    "看病",
)

_GENERIC = re.compile(
    r"什么|啥|哪些|哪儿|哪里|哪|怎么样|怎么|如何|为什么|干嘛|干吗|发生|聊过|聊了|聊|"
    r"说过|说|讲过|讲|讨论|提到|提|做|干|忙|事情|事|工作|问题|内容|话题|东西|方面|"
    r"进展|进度|情况|一下|看看|告诉我|帮我|请|请问|查一下|回顾|总结|汇总|复盘|梳理|盘点|"
    r"我|我们|咱们|你|你们|都|主要|具体|一共|大概|总共|分别|又|还|一起|到底|究竟|"
    r"了|的|吗|呢|吧|啊|呀|有|过|在"
)
_NON_CONTENT = re.compile(r"[\s\W_]+", re.UNICODE)


@dataclass(frozen=True)
class TimeWindow:
    """One whole local day as a half-open UTC instant range."""

    start: datetime
    end: datetime
    precision: str = "day"
    label: str = ""


@dataclass(frozen=True)
class TimeQuery:
    """What a question names of when, and what it asks once that is removed."""

    windows: tuple[TimeWindow, ...]
    intent: str  # "message" | "event"
    rest: str
    time_only: bool
    reason: str
    future: bool = False


@dataclass(frozen=True)
class _Mention:
    span: tuple[int, int]
    day: date | None
    label: str
    future: bool = False


@dataclass(frozen=True)
class EventTime:
    """The PCS-side classification of one captured message."""

    event_type: str  # occurred | planned | hypothetical | quoted | unknown
    event_time: str | None
    planned_for: str | None
    time_precision: str
    time_basis: str
    reason: str


def resolve_zone(name: str) -> tzinfo:
    """Resolve a configured zone; fixed offsets when Windows has no tzdata."""

    if not isinstance(name, str) or not name or len(name) > 64:
        raise ValueError("timezone")
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - fixed-offset fallback when tzdata is absent
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8))
        if name == "UTC":
            return timezone.utc
        raise ValueError("timezone") from None


def parse_timestamp(value: Any) -> datetime | None:
    """An aware instant, or ``None``: naive or malformed is never guessed."""

    if not isinstance(value, str) or not 1 <= len(value) <= 48:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None


def message_time(observed_at: Any, recorded_at: Any) -> tuple[str | None, str]:
    """The message's own instant and the honest basis of that value."""

    observed = parse_timestamp(observed_at)
    if observed is not None:
        return observed.isoformat(), "observed_at"
    recorded = parse_timestamp(recorded_at)
    if recorded is not None:
        # The old row has no real observation time; only a write-time proxy.
        return recorded.isoformat(), "recorded_at_fallback"
    return None, "unknown"


def _day_start(day: date, zone: tzinfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=zone)


def _window(day: date, zone: tzinfo, label: str) -> TimeWindow:
    start = _day_start(day, zone)
    end = _day_start(day + timedelta(days=1), zone)
    return TimeWindow(
        start=start.astimezone(timezone.utc),
        end=end.astimezone(timezone.utc),
        precision="day",
        label=label,
    )


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _local_date(anchor: datetime, zone: tzinfo) -> date:
    return anchor.astimezone(zone).date()


def _calendar_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _month_day(month: int, day: int, today: date) -> date | None:
    named = _calendar_date(today.year, month, day)
    if named is None:
        return None
    if named > today:
        named = _calendar_date(today.year - 1, month, day)
    if named is None or named < today - timedelta(days=_MONTH_DAY_REACH):
        return None
    return named


def _mentions(text: str, today: date) -> tuple[list[_Mention], list[Any], bool]:
    """Past and future mentions plus raw unsupported matches.

    Returns ``(mentions, unsupported, future_range)``; ``future_range`` is true
    for a recognised future span that is not one day ("next week").
    """

    found: list[_Mention] = []
    taken: list[tuple[int, int]] = []

    def add(span: tuple[int, int], day: date | None, label: str, *, future: bool = False) -> None:
        for start, end in taken:
            if span[0] < end and start < span[1]:
                return
        taken.append(span)
        found.append(_Mention(span, day, label, future=future))

    for word, delta in _RELATIVE:
        start = 0
        while (index := text.find(word, start)) >= 0:
            add((index, index + len(word)), today - timedelta(days=delta), word)
            start = index + len(word)

    for word, delta in _FUTURE_RELATIVE:
        start = 0
        while (index := text.find(word, start)) >= 0:
            add((index, index + len(word)), today + timedelta(days=delta), word, future=True)
            start = index + len(word)

    for match in _N_DAYS.finditer(text):
        count = int(match.group(1))
        if 1 <= count <= _MAX_N_DAYS:
            add((match.start(), match.end()), today - timedelta(days=count), match.group(0))
        else:
            return found, [match], False

    for match in _N_DAYS_AFTER.finditer(text):
        count = int(match.group(1))
        if 1 <= count <= _MAX_FUTURE_DAYS:
            add((match.start(), match.end()), today + timedelta(days=count), match.group(0), future=True)
        else:
            return found, [match], False

    for match in _ISO_DATE.finditer(text):
        day = _calendar_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        add(
            (match.start(), match.end()),
            day,
            match.group(0),
            future=day is not None and day > today,
        )

    for match in _MONTH_DAY.finditer(text):
        add((match.start(), match.end()), _month_day(int(match.group(1)), int(match.group(2)), today), match.group(0))

    unsupported = list(_UNSUPPORTED.finditer(text))
    future_range = _FUTURE_RANGE.search(text) is not None
    return found, unsupported, future_range


def _intent(rest: str) -> str:
    text = rest or ""
    message = any(cue in text for cue in _MESSAGE_CUES)
    event = any(cue in text for cue in _EVENT_CUES)
    if event and not message:
        return "event"
    return "message"


def _content_chars(rest: str) -> int:
    stripped = _GENERIC.sub("", rest or "")
    stripped = _NON_CONTENT.sub("", stripped)
    return len(stripped)


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    result = text
    for start, end in sorted(spans, reverse=True):
        result = result[:start] + " " + result[end:]
    return " ".join(result.split())


def _is_question(text: str) -> bool:
    stripped = text.strip()
    if stripped.endswith("?") or stripped.endswith("？"):
        return True
    return any(cue in stripped for cue in _QUESTION_CUES)


def parse_time_query(
    text: Any, *, anchor: Any, timezone_name: str = DEFAULT_TIMEZONE
) -> TimeQuery | None:
    """The time a question names, or ``None`` when it names none.

    A future question returns ``future=True`` with no windows: this batch does
    not retrieve plans.  A recognised but unsupported expression returns no
    windows and ``reason="unsupported"``; the host must not guess a range.
    """

    if not isinstance(text, str) or not text.strip():
        return None
    normalized = _normalize(text)
    if len(normalized) > _MAX_QUERY_CHARS:
        return None
    moment = parse_timestamp(anchor)
    if moment is None:
        return None
    try:
        zone = resolve_zone(timezone_name)
    except ValueError:
        return None
    today = _local_date(moment, zone)
    mentions, unsupported, future_range = _mentions(normalized, today)
    past = [mention for mention in mentions if not mention.future]
    future = [mention for mention in mentions if mention.future]
    if future or future_range:
        rest = _remove_spans(normalized, [mention.span for mention in mentions])
        return TimeQuery(
            windows=(),
            intent=_intent(rest),
            rest=rest,
            time_only=_content_chars(rest) == 0,
            reason="future",
            future=True,
        )
    if unsupported:
        return TimeQuery((), _intent(normalized), normalized, False, "unsupported")
    if not past:
        return None
    days: list[date] = []
    for mention in past:
        if mention.day is None:
            return TimeQuery((), _intent(normalized), normalized, False, "unsupported")
        if mention.day not in days:
            days.append(mention.day)
    if len(days) > _MAX_DAYS:
        return TimeQuery((), _intent(normalized), normalized, False, "unsupported")
    windows = tuple(
        sorted(
            (_window(day, zone, label) for day, label in zip(days, _labels(past, days))),
            key=lambda window: window.start,
        )
    )
    rest = _remove_spans(normalized, [mention.span for mention in past])
    return TimeQuery(
        windows=windows,
        intent=_intent(rest),
        rest=rest,
        time_only=_content_chars(rest) == 0,
        reason=past[0].label,
    )


def _labels(mentions: list[_Mention], days: list[date]) -> list[str]:
    labels: list[str] = []
    for day in days:
        label = next((mention.label for mention in mentions if mention.day == day), "day")
        labels.append(label)
    return labels


def _time_basis(mentions: list[_Mention]) -> str:
    label = mentions[0].label
    if re.fullmatch(r"\d{4}[-/年].*", label) or "月" in label and "日" in label:
        return "explicit_date"
    return "relative_day"


def parse_event_time(
    body: Any,
    *,
    observed_at: Any,
    recorded_at: Any,
    timezone_name: str = DEFAULT_TIMEZONE,
    origin_kind: str = "user_statement",
    ref_type: str = "collab_input",
) -> dict[str, Any]:
    """The PCS-side time record for one captured message (existing JSON only).

    ``event_type="occurred"`` is written only for a plainly stated,
    already-happened, single past day.  A plan keeps ``planned_for`` and never
    an occurrence; every other case keeps both null and a bounded reason.

    A row without a real ``observed_at`` has no provable message time: its
    ``recorded_at`` is a write-time proxy, so this function never derives an
    event or plan date from it, not even an explicit calendar date.
    """

    anchor, anchor_source = message_time(observed_at, recorded_at)
    message_time_basis = (
        "observed_at"
        if anchor_source == "observed_at"
        else (
            "recorded_at_fallback"
            if anchor_source in ("recorded_at", "recorded_at_fallback")
            else "unknown"
        )
    )
    record: dict[str, Any] = {
        "parser": PARSER,
        "event_type": "unknown",
        "event_time": None,
        "planned_for": None,
        "time_precision": "unknown",
        "time_basis": "unknown",
        "anchor": anchor,
        "anchor_source": anchor_source,
        "message_time_basis": message_time_basis,
        "timezone": timezone_name,
        "reason": "no_time",
    }
    if ref_type != "collab_input" or origin_kind != "user_statement":
        record["reason"] = "not_user_statement"
        return record
    if message_time_basis != "observed_at":
        # No real observation time: a write-time proxy cannot anchor "昨天" or
        # even an absolute date for this batch.
        record["reason"] = "recorded_at_fallback"
        return record
    if not isinstance(body, str) or not body.strip():
        record["reason"] = "empty"
        return record
    normalized = _normalize(body)
    moment = parse_timestamp(anchor)
    if moment is None:
        record["reason"] = "no_anchor"
        return record
    try:
        zone = resolve_zone(timezone_name)
    except ValueError:
        record["reason"] = "timezone"
        return record
    today = _local_date(moment, zone)

    if _QUOTE_CHARS.search(normalized) or any(cue in normalized for cue in _QUOTE_CUES):
        record.update(event_type="quoted", reason="quoted")
        return record
    if any(cue in normalized for cue in _HYPOTHETICAL):
        record.update(event_type="hypothetical", reason="hypothetical")
        return record
    if _is_question(normalized):
        record.update(event_type="unknown", reason="question")
        return record

    mentions, unsupported, future_range = _mentions(normalized, today)
    past = [mention for mention in mentions if not mention.future]
    future = [mention for mention in mentions if mention.future]
    plan_cue = any(cue in normalized for cue in _PLAN_CUES)
    planning = future or future_range or plan_cue
    if planning and not past:
        explicit_only = (
            bool(future)
            and not future_range
            and not plan_cue
            and all(_is_explicit_date_label(mention.label) for mention in future)
        )
        if explicit_only:
            # A bare future calendar date is not a plan and is not an
            # occurrence ("2026-10-31 我去了" is contradictory, not evidence).
            record.update(event_type="unknown", reason="future")
            return record
        planned_for = _single_future_day(future, zone)
        if planned_for is not None:
            planned_day = date.fromisoformat(planned_for[:10])
            if (planned_day - today).days > _MAX_FUTURE_DAYS:
                record.update(event_type="unknown", reason="future")
                return record
        record.update(
            event_type="planned",
            planned_for=planned_for,
            time_precision="day" if planned_for else "unknown",
            time_basis=_time_basis(future) if future else "unknown",
            reason="planned",
        )
        return record
    if planning and past:
        # A past plan is still a plan, never an occurrence.
        record.update(
            event_type="planned",
            planned_for=None,
            time_precision="unknown",
            time_basis="unknown",
            reason="planned",
        )
        return record
    if unsupported:
        record.update(event_type="unknown", reason="unsupported")
        return record
    if not past:
        record.update(event_type="unknown", reason="no_time")
        return record
    if any(mention.day is None for mention in past):
        record.update(event_type="unknown", reason="invalid_date")
        return record
    days = {mention.day for mention in past}
    if len(days) > 1:
        record.update(event_type="unknown", reason="multiple_dates")
        return record
    day = next(iter(days))
    assert day is not None
    if day > today or _day_start(day, zone) > moment:
        # The occurred day may never be later than the message instant itself.
        record.update(event_type="unknown", reason="future")
        return record
    record.update(
        event_type="occurred",
        event_time=_local_iso(day, zone),
        planned_for=None,
        time_precision="day",
        time_basis=_time_basis(past),
        reason=past[0].label,
    )
    return record


def _is_explicit_date_label(label: str) -> bool:
    return re.fullmatch(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}(?:日)?", label) is not None


def _single_future_day(mentions: list[_Mention], zone: tzinfo) -> str | None:
    days = {mention.day for mention in mentions if mention.day is not None}
    if len(days) != 1:
        return None
    day = next(iter(days))
    assert day is not None
    return _local_iso(day, zone)


def _local_iso(day: date, zone: tzinfo) -> str:
    return _day_start(day, zone).isoformat()


def in_windows(moment: Any, windows: tuple[tuple[str, str], ...]) -> bool:
    """Whether a stamp falls in one half-open UTC window; unparsable is not in."""

    parsed = parse_timestamp(moment)
    if parsed is None:
        return False
    for start, end in windows:
        left = parse_timestamp(start)
        right = parse_timestamp(end)
        if left is None or right is None:
            continue
        if left <= parsed < right:
            return True
    return False


def window_bounds(windows: tuple[TimeWindow, ...]) -> tuple[str, str]:
    """The enclosing UTC ``[since, until)`` bounds of several whole days."""

    start = min(window.start for window in windows)
    end = max(window.end for window in windows)
    return start.isoformat(), end.isoformat()


def utc_windows(windows: tuple[TimeWindow, ...]) -> tuple[tuple[str, str], ...]:
    return tuple((window.start.isoformat(), window.end.isoformat()) for window in windows)


__all__ = [
    "DEFAULT_TIMEZONE",
    "EventTime",
    "PARSER",
    "TimeQuery",
    "TimeWindow",
    "in_windows",
    "message_time",
    "parse_event_time",
    "parse_time_query",
    "parse_timestamp",
    "resolve_zone",
    "utc_windows",
    "window_bounds",
]
