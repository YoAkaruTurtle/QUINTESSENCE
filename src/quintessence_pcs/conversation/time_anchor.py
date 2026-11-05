"""RG08 time-anchor observation: flag day references with no source.

This is an **observation** helper, never a gate.  It answers one narrow
question: does the reply mention a day ("昨天", "明早", ...) whose day reference
never appears in the user's current message or in the recent history?  A hit is
a hint that the model invented a time anchor; it is written to the turn event
and does not change, block or rewrite any output.

It deliberately does not judge whether the reply's event state is right --
"已经到家" vs "还没到家" is a different problem and needs its own evidence.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

#: Day reference -> the tokens that can express it.
DAY_GROUPS: dict[str, tuple[str, ...]] = {
    "昨天": ("昨天", "昨晚", "昨儿", "昨日", "昨天晚上"),
    "今天": ("今天", "今晚", "今早", "今儿", "今日", "今天晚上", "今天早上"),
    "明天": ("明天", "明早", "明晚", "明日", "明天早上", "明天晚上"),
    "前天": ("前天", "前晚"),
    "后天": ("后天", "后晚"),
}

#: Longest-first so "昨天晚上" is reported as one anchor, not as "昨天" + "晚上".
_ALL_TOKENS: tuple[tuple[str, str], ...] = tuple(
    sorted(
        ((token, group) for group, tokens in DAY_GROUPS.items() for token in tokens),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)


def find_day_anchors(text: str) -> list[tuple[str, str]]:
    """Every day token in *text*, longest-first, without overlaps."""

    if not isinstance(text, str) or not text:
        return []
    found: list[tuple[str, str]] = []
    taken: list[tuple[int, int]] = []
    for token, group in _ALL_TOKENS:
        start = 0
        while True:
            index = text.find(token, start)
            if index < 0:
                break
            end = index + len(token)
            if not any(index < other_end and other_start < end for other_start, other_end in taken):
                found.append((token, group))
                taken.append((index, end))
            start = end
    return found


def find_suspect_day_anchors(
    user_text: str,
    history_texts: Sequence[str] | Iterable[str],
    reply_text: str,
) -> list[dict[str, Any]]:
    """Day references in the reply with no day reference in input or history."""

    sources = [user_text or ""]
    sources.extend(text or "" for text in history_texts)
    available = {group for text in sources for _token, group in find_day_anchors(text)}
    flagged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for token, group in find_day_anchors(reply_text or ""):
        if group in available or token in seen:
            continue
        seen.add(token)
        flagged.append(
            {
                "anchor": token,
                "day": group,
                "reason": "day_reference_not_in_input_or_history",
            }
        )
    return flagged


__all__ = ["DAY_GROUPS", "find_day_anchors", "find_suspect_day_anchors"]
