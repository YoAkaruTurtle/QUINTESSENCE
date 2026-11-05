"""RG08 R-B follow-up: one deterministic, non-generative query variant.

The upstream lexical recall rejects some natural questions because its
specificity bar counts the framing and the question topic as query terms.  This
module derives at most one *literal substring* of the user's own text: strip a
closed set of leading conversational phrases, then stop at the first
interrogative marker.  It never adds words, never produces a statement, and
never removes negation/person/entity/time from the retained span.

The caller must still try the original query first.  This helper is policy, not
retrieval: it does not call upstream and does not decide what a hit means.
"""

from __future__ import annotations

import re
import unicodedata

#: Anchored, closed framing phrases.  Longest first so ``你还记得`` wins over
#: ``你记得``.  Nothing after this list is removed.
FRAMING_PREFIXES = (
    "你还记得",
    "你记得",
    "你知道",
    "请问",
    "我想知道",
    "帮我查一下",
    "帮我查",
    "能不能告诉我",
    "可以告诉我",
    "麻烦告诉我",
    "请告诉我",
    "记得",
)

#: First interrogative marker ends the retained content span.
QUESTION_MARKERS = (
    "什么",
    "怎么",
    "怎样",
    "哪",
    "谁",
    "多少",
    "几",
    "吗",
    "呢",
    "吧",
    "？",
    "?",
)

_CONTENT = re.compile(r"[A-Za-z0-9\u3400-\u9fff]")
#: A tail that carries these would lose a named/hard qualifier if cut away.
_HARD_TAIL = re.compile(
    r"[A-Za-z0-9]"
    r"|不|没|未|别|无"
    r"|昨天|今天|明天|前天|后天|上周|下周|上个月|下个月|去年|今年|明年"
    r"|周[一二三四五六日天]|星期|礼拜|上午|下午|晚上|早上|中午"
)


def focus_prefix(query: object) -> str | None:
    """Return one safe literal prefix, or ``None`` when no safe variant exists.

    The result contains at least three CJK/Latin content characters, is a
    contiguous substring of the NFKC-normalized query, and stops before the
    first question marker.  A tail carrying a named, numeric, temporal or
    negative qualifier is refused rather than silently dropped.
    """

    if not isinstance(query, str) or not query.strip() or len(query) > 8192:
        return None
    text = unicodedata.normalize("NFKC", query).strip()
    for _ in range(2):
        for phrase in FRAMING_PREFIXES:
            if text.startswith(phrase):
                text = text[len(phrase) :].lstrip()
                break
        else:
            break

    positions = [text.find(marker) for marker in QUESTION_MARKERS]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return None
    cut = min(positions)
    if cut <= 0:
        return None
    prefix = text[:cut].strip(" \t\r\n，,。.；;：:、")
    if len(_CONTENT.findall(prefix)) < 3:
        return None
    tail = text[cut:]
    if _HARD_TAIL.search(tail):
        return None
    # A variant equal to the whole query is not a variant.
    if prefix == query.strip():
        return None
    return prefix


__all__ = ["FRAMING_PREFIXES", "QUESTION_MARKERS", "focus_prefix"]
