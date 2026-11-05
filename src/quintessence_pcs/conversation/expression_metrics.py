"""RG08 T0M: pure observation metrics for the expression stage (RG08 §7.3–§7.5).

This module is the **single** implementation of the sentence-level metrics.
`expression_check` (runtime) and the Lab offline analyzer both import it; no
other copy of the splitting / alignment / marker tables is allowed.

What these numbers are
----------------------
They are **observation metrics**, never quality scores.  A high
``retention_ratio`` does not mean the reply kept the right content, and a low
one does not mean it deleted the right content.  They answer "was a sentence
kept, rewritten or dropped", not "was that the right call" -- the latter only
comes from the human blind review.

Semantic classes are heuristics
-------------------------------
``advice_like`` / ``analysis_like`` / ``question`` come from small marker
tables.  They over- and under-fire on real text (measured recall on the V13
corpus: questions 3/5, analysis 1/2, advice 3/4 -- see plan §7.0).  They are
therefore named ``*_like`` and must stay "activity descriptions", never
assertions that a sentence *is* advice or analysis.

Purity
------
No store, no model, no file, no clock, no global mutable state.  Every function
is deterministic: the same two strings always produce the same numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Sequence

#: Sentences considered per side.  The metric stays bounded even for a very
#: long technical answer; the tail is ignored rather than truncated mid-way.
MAX_SENTENCES = 80

#: Per-pair similarity thresholds inside a ``replace`` block.
RETAINED_RATIO = 0.85
REWRITTEN_RATIO = 0.45

#: One sentence ends at one of these characters (the delimiter is kept).
_SENTENCE_END = re.compile(r"(?<=[。！？!?；;])")
_LINE_BREAK = re.compile(r"[\r\n]+")
_WHITESPACE = re.compile(r"\s+")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")

QUESTION_ENDS = ("？", "?")
#: A sentence that ends with an explicit request for information counts as a
#: question even without a question mark (V13 round 7: "是什么课，说来听听").
REQUEST_TAILS = (
    "说来听听",
    "说说看",
    "跟我说说",
    "讲讲",
    "告诉我",
    "说说吧",
    "说说",
    "聊聊",
)
#: A-not-A questions ("是不是…", "要不要…") are questions regardless of the
#: closing particle.
A_NOT_A = (
    "是不是",
    "要不要",
    "能不能",
    "会不会",
    "有没有",
    "可不可以",
    "行不行",
    "对不对",
    "好不好",
)
#: Interrogative words.  They only count when the sentence does not end with a
#: declarative particle -- "那得看是什么课了" is a statement, "是吃了什么好
#: 东西" is a question (V13 rounds 1 and 7).
INTERROGATIVES = (
    "什么",
    "为什么",
    "怎么",
    "哪",
    "谁",
    "多少",
    "几点",
    "何时",
)
#: Endings that turn an interrogative word back into a statement.
_DECLARATIVE_ENDS = ("。", "了", "的", "地", "得", "吧")
#: "是…还是…" is the written form of a no-question-mark alternative question
#: (V13 round 3).
_IS_OR = re.compile(r"是[^。！？!?；;]{0,40}?还是")

ADVICE_MARKERS = (
    "建议",
    "不如",
    "要不",
    "早点",
    "最好",
    "记得",
    "别忘了",
    "应该",
    "可以试",
    "先别",
)
ANALYSIS_MARKERS = (
    "因为",
    "所以",
    "说明",
    "意味着",
    "其实",
    "无非",
    "说到底",
    "也就是说",
    "难怪",
)
NEGATION_MARKERS = ("不", "没", "别", "未", "无法", "不能")
CONDITION_MARKERS = ("如果", "要是", "若", "除非", "只有", "一旦")
UNCERTAINTY_MARKERS = ("不确定", "不知道", "可能", "大概", "应该", "估计", "似乎")
PENDING_MARKERS = (
    "待",
    "未提交",
    "还没",
    "暂",
    "正在",
    "进行中",
    "审核中",
    "未完成",
    "失败",
)

#: Names of the protected signals, in the order used by the retention ratio.
PROTECTED_SIGNAL_NAMES = ("numbers", "negation", "condition", "uncertainty", "pending")

#: Alignment statuses.
RETAINED = "retained"
REWRITTEN = "rewritten"
DROPPED = "dropped"
STATUSES = (RETAINED, REWRITTEN, DROPPED)


@dataclass(frozen=True)
class Alignment:
    """One sentence-level alignment result.

    ``statuses[i]`` describes ``draft_sentences[i]``.  ``added_reply_indices``
    are reply sentences that no draft sentence maps to (an inserted block, or
    the surplus of a replace block).  ``paired`` maps ``(draft_index,
    reply_index)`` for the sentences that did align, so callers can ask what a
    given reply sentence replaced.
    """

    statuses: tuple[str, ...]
    added_reply_indices: tuple[int, ...]
    paired: tuple[tuple[int, int], ...] = ()

    @property
    def added(self) -> int:
        return len(self.added_reply_indices)


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------


def normalise_text(text: str) -> str:
    """Collapse whitespace runs to one space and strip both ends."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return _WHITESPACE.sub(" ", text).strip()


def split_sentences(text: str, limit: int = MAX_SENTENCES) -> list[str]:
    """Split ``text`` into ordered sentences, keeping each delimiter.

    Splits on line breaks first (bubbles are joined with two newlines), then on
    ``。！？!?；;``; strips each part, drops empties and caps the count at
    ``limit``.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")

    sentences: list[str] = []
    for line in _LINE_BREAK.split(text):
        for part in _SENTENCE_END.split(line):
            stripped = part.strip()
            if stripped:
                sentences.append(stripped)
            if len(sentences) >= limit:
                return sentences[:limit]
    return sentences[:limit]


def numbers_in(text: str) -> set[str]:
    """Every numeric token, with thousands separators and dots removed."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return {value.replace(",", "").replace(".", "") for value in _NUMBER.findall(text)}


def count_markers(text: str, markers: Sequence[str]) -> int:
    """How many times any marker occurs in ``text`` (occurrences, not lines)."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return sum(text.count(marker) for marker in markers)


def _as_sentences(value: "str | Sequence[str]") -> list[str]:
    """Accept either raw text or an already-split sentence sequence."""

    if isinstance(value, str):
        return split_sentences(value)
    if isinstance(value, (bytes, bytearray)):
        raise TypeError("sentences must be strings")
    sentences: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError("sentences must be strings")
        stripped = item.strip()
        if stripped:
            sentences.append(stripped)
        if len(sentences) >= MAX_SENTENCES:
            break
    return sentences


# --------------------------------------------------------------------------
# sentence classification (heuristic, documented as such)
# --------------------------------------------------------------------------


def is_question_like(sentence: str) -> bool:
    """Whether ``sentence`` looks like a question or a request for information.

    Order of evidence (first hit wins):

    1. ends with ``？``/``?``;
    2. ends with an explicit request ("说来听听", …);
    3. contains an A-not-A form ("是不是", "要不要", …);
    4. matches ``是…还是…`` (written alternative question);
    5. contains an interrogative word **and** does not end with a declarative
       particle (``。`` ``了`` ``的`` ``地`` ``得`` ``吧``).

    Rule 5's ending guard is what separates "是吃了什么好东西" (question) from
    "那得看是什么课了" (statement) -- the exact pair V13 rounds 1 and 7 need.
    """

    if not isinstance(sentence, str):
        raise TypeError("sentence must be a string")
    text = sentence.strip()
    if not text:
        return False
    if text.endswith(QUESTION_ENDS):
        return True
    if text.endswith(REQUEST_TAILS):
        return True
    if any(form in text for form in A_NOT_A):
        return True
    if _IS_OR.search(text) is not None:
        return True
    if any(word in text for word in INTERROGATIVES) and not text.endswith(
        _DECLARATIVE_ENDS
    ):
        return True
    return False


def classify_sentence(sentence: str) -> str:
    """Classify one sentence as ``question`` / ``advice_like`` / ``analysis_like`` / ``plain``.

    Priority is fixed (question first) so one sentence contributes to exactly
    one class.  The two ``*_like`` classes are marker heuristics: "这不是建议"
    still counts as ``advice_like``.
    """

    if not isinstance(sentence, str):
        raise TypeError("sentence must be a string")
    if is_question_like(sentence):
        return "question"
    if any(marker in sentence for marker in ADVICE_MARKERS):
        return "advice_like"
    if any(marker in sentence for marker in ANALYSIS_MARKERS):
        return "analysis_like"
    return "plain"


# --------------------------------------------------------------------------
# alignment
# --------------------------------------------------------------------------


def align_sentences_detail(
    draft: "str | Sequence[str]", reply: "str | Sequence[str]"
) -> Alignment:
    """Two-level alignment: sentence sequence first, then per-pair similarity."""

    draft_sentences = _as_sentences(draft)
    reply_sentences = _as_sentences(reply)
    if not draft_sentences:
        return Alignment((), tuple(range(len(reply_sentences))))

    matcher = SequenceMatcher(None, draft_sentences, reply_sentences, autojunk=False)
    statuses: list[str | None] = [None] * len(draft_sentences)
    added: list[int] = []
    paired_indices: list[tuple[int, int]] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                statuses[i1 + offset] = RETAINED
                paired_indices.append((i1 + offset, j1 + offset))
        elif tag == "delete":
            for offset in range(i2 - i1):
                statuses[i1 + offset] = DROPPED
        elif tag == "insert":
            added.extend(range(j1, j2))
        else:  # replace
            paired = min(i2 - i1, j2 - j1)
            for offset in range(paired):
                ratio = SequenceMatcher(
                    None, draft_sentences[i1 + offset], reply_sentences[j1 + offset],
                    autojunk=False,
                ).ratio()
                statuses[i1 + offset] = (
                    RETAINED
                    if ratio >= RETAINED_RATIO
                    else REWRITTEN
                    if ratio >= REWRITTEN_RATIO
                    else DROPPED
                )
                paired_indices.append((i1 + offset, j1 + offset))
            # ``offset`` is already an absolute draft index here: adding ``i1``
            # again walks past the end of ``statuses`` (real-run crash).
            for index in range(i1 + paired, i2):
                statuses[index] = DROPPED
            added.extend(range(j1 + paired, j2))

    return Alignment(
        tuple(status or DROPPED for status in statuses),
        tuple(sorted(added)),
        tuple(paired_indices),
    )


def align_sentences(
    draft: "str | Sequence[str]", reply: "str | Sequence[str]"
) -> list[str]:
    """Convenience wrapper: the status of every draft sentence, in order."""

    return list(align_sentences_detail(draft, reply).statuses)


# --------------------------------------------------------------------------
# protected signals
# --------------------------------------------------------------------------


def _signal_presence(draft: str) -> dict[str, bool]:
    """Which protected signals the draft actually carries (the ratio's domain)."""

    draft_numbers = numbers_in(draft)
    return {
        "numbers": bool(draft_numbers),
        "negation": count_markers(draft, NEGATION_MARKERS) > 0,
        "condition": count_markers(draft, CONDITION_MARKERS) > 0,
        "uncertainty": count_markers(draft, UNCERTAINTY_MARKERS) > 0,
        "pending": count_markers(draft, PENDING_MARKERS) > 0,
    }


def protected_signal_retention(draft: str, reply: str) -> dict[str, float | int]:
    """Five 0/1 signals: did the draft's signal survive into the reply?

    ``numbers`` is all-or-nothing (every draft number must appear); the other
    four are "not every marker disappeared".  Only categories the draft
    actually carries are meaningful -- see ``_signal_presence``.
    """

    draft_numbers = numbers_in(draft)
    reply_numbers = numbers_in(reply)
    signals = {
        "numbers": int(bool(draft_numbers) and draft_numbers <= reply_numbers),
        "negation": int(
            not (
                count_markers(draft, NEGATION_MARKERS) > 0
                and count_markers(reply, NEGATION_MARKERS) == 0
            )
        ),
        "condition": int(
            not (
                count_markers(draft, CONDITION_MARKERS) > 0
                and count_markers(reply, CONDITION_MARKERS) == 0
            )
        ),
        "uncertainty": int(
            not (
                count_markers(draft, UNCERTAINTY_MARKERS) > 0
                and count_markers(reply, UNCERTAINTY_MARKERS) == 0
            )
        ),
        "pending": int(
            not (
                count_markers(draft, PENDING_MARKERS) > 0
                and count_markers(reply, PENDING_MARKERS) == 0
            )
        ),
    }
    return signals


def _pending_markers_dropped(draft: str, reply: str) -> int:
    """Draft pending markers that no longer appear in the reply."""

    dropped = 0
    for marker in PENDING_MARKERS:
        dropped += max(0, draft.count(marker) - reply.count(marker))
    return dropped


# --------------------------------------------------------------------------
# the metric bundle
# --------------------------------------------------------------------------


def _count_status(statuses: Sequence[str], status: str) -> int:
    return sum(1 for item in statuses if item == status)


def compute_metrics(
    draft: str,
    reply: str,
    *,
    draft_bubbles: int = 1,
    reply_bubbles: int = 1,
) -> dict[str, int | float]:
    """The plan §7.2 observation metrics that are derivable from draft + reply.

    Every value is ``int`` or ``float``; nothing here is a pass/fail.  Bubble
    counts are echoed for the length/bubble deltas and are clamped at zero so a
    malformed caller can never crash a turn (metrics must not block).
    """

    if not isinstance(draft, str) or not isinstance(reply, str):
        raise TypeError("draft and reply must be strings")
    draft_bubbles = max(0, int(draft_bubbles))
    reply_bubbles = max(0, int(reply_bubbles))

    draft_text = normalise_text(draft)
    reply_text = normalise_text(reply)
    draft_sentences = split_sentences(draft)
    reply_sentences = split_sentences(reply)
    alignment = align_sentences_detail(draft_sentences, reply_sentences)

    kept_chars = sum(
        len(sentence)
        for sentence, status in zip(draft_sentences, alignment.statuses)
        if status == RETAINED
    )
    rewritten_chars = sum(
        len(sentence)
        for sentence, status in zip(draft_sentences, alignment.statuses)
        if status == REWRITTEN
    )
    dropped_chars = sum(
        len(sentence)
        for sentence, status in zip(draft_sentences, alignment.statuses)
        if status == DROPPED
    )
    draft_chars = len(draft_text)
    retention_ratio = (
        round(min(1.0, kept_chars / draft_chars), 3) if draft_chars else 0.0
    )

    draft_kinds = [classify_sentence(sentence) for sentence in draft_sentences]
    reply_kinds = [classify_sentence(sentence) for sentence in reply_sentences]

    draft_question = sum(1 for kind in draft_kinds if kind == "question")
    reply_question = sum(1 for kind in reply_kinds if kind == "question")
    questions_retained = sum(
        1
        for kind, status in zip(draft_kinds, alignment.statuses)
        if kind == "question" and status == RETAINED
    )
    questions_rewritten = sum(
        1
        for kind, status in zip(draft_kinds, alignment.statuses)
        if kind == "question" and status == REWRITTEN
    )
    questions_dropped = sum(
        1
        for kind, status in zip(draft_kinds, alignment.statuses)
        if kind == "question" and status == DROPPED
    )
    # A reply question is "added" when it has no draft counterpart at all, or
    # when it took over a slot whose draft sentence was not a question -- the
    # V13 round 7 shape (a statement rewritten into a request for information).
    paired_draft_of_reply = {
        reply_index: draft_index for draft_index, reply_index in alignment.paired
    }
    added_reply_indices = set(alignment.added_reply_indices)
    questions_added = 0
    for reply_index, kind in enumerate(reply_kinds):
        if kind != "question":
            continue
        if reply_index in added_reply_indices:
            questions_added += 1
            continue
        drafted = paired_draft_of_reply.get(reply_index)
        if drafted is not None and draft_kinds[drafted] != "question":
            questions_added += 1

    draft_advice = sum(1 for kind in draft_kinds if kind == "advice_like")
    draft_analysis = sum(1 for kind in draft_kinds if kind == "analysis_like")
    advice_dropped = sum(
        1
        for kind, status in zip(draft_kinds, alignment.statuses)
        if kind == "advice_like" and status == DROPPED
    )
    analysis_dropped = sum(
        1
        for kind, status in zip(draft_kinds, alignment.statuses)
        if kind == "analysis_like" and status == DROPPED
    )

    # A draft with no question at the end that ends in one: the V13 round 7
    # blind spot (a statement converted into a request for information).
    last_draft = draft_sentences[-1] if draft_sentences else ""
    last_reply = reply_sentences[-1] if reply_sentences else ""
    statement_to_question = int(
        bool(last_draft)
        and bool(last_reply)
        and not is_question_like(last_draft)
        and is_question_like(last_reply)
    )

    signals = protected_signal_retention(draft_text, reply_text)
    presence = _signal_presence(draft_text)
    applicable = [signals[name] for name in PROTECTED_SIGNAL_NAMES if presence[name]]
    key_content_retention = (
        round(sum(applicable) / len(applicable), 3) if applicable else 1.0
    )

    metrics: dict[str, int | float] = {
        "draft_sentences": len(draft_sentences),
        "reply_sentences": len(reply_sentences),
        "sentences_retained": _count_status(alignment.statuses, RETAINED),
        "sentences_rewritten": _count_status(alignment.statuses, REWRITTEN),
        "sentences_dropped": _count_status(alignment.statuses, DROPPED),
        "sentences_added": alignment.added,
        "draft_chars_kept_verbatim": kept_chars,
        "draft_chars_rewritten": rewritten_chars,
        "draft_chars_dropped": dropped_chars,
        "retention_ratio": retention_ratio,
        "draft_questions": draft_question,
        "reply_questions": reply_question,
        "questions_retained": questions_retained,
        "questions_rewritten": questions_rewritten,
        "questions_dropped": questions_dropped,
        "questions_added": questions_added,
        "statement_to_question": statement_to_question,
        "advice_like_draft": draft_advice,
        "advice_like_retained": draft_advice - advice_dropped,
        "advice_like_dropped": advice_dropped,
        "analysis_like_draft": draft_analysis,
        "analysis_like_retained": draft_analysis - analysis_dropped,
        "analysis_like_dropped": analysis_dropped,
        "key_content_retention": key_content_retention,
        "numbers_retained": signals["numbers"],
        "negation_retained": signals["negation"],
        "condition_retained": signals["condition"],
        "uncertainty_retained": signals["uncertainty"],
        "pending_retained": signals["pending"],
        "pending_markers_dropped": _pending_markers_dropped(draft_text, reply_text),
        "draft_bubbles": draft_bubbles,
        "reply_bubbles": reply_bubbles,
        "bubble_delta": reply_bubbles - draft_bubbles,
        "length_delta_chars": len(reply_text) - len(draft_text),
    }
    return metrics


__all__ = [
    "ADVICE_MARKERS",
    "Alignment",
    "ANALYSIS_MARKERS",
    "A_NOT_A",
    "CONDITION_MARKERS",
    "DROPPED",
    "INTERROGATIVES",
    "MAX_SENTENCES",
    "NEGATION_MARKERS",
    "PENDING_MARKERS",
    "PROTECTED_SIGNAL_NAMES",
    "QUESTION_ENDS",
    "REQUEST_TAILS",
    "RETAINED",
    "RETAINED_RATIO",
    "REWRITTEN",
    "REWRITTEN_RATIO",
    "STATUSES",
    "UNCERTAINTY_MARKERS",
    "align_sentences",
    "align_sentences_detail",
    "classify_sentence",
    "compute_metrics",
    "count_markers",
    "is_question_like",
    "normalise_text",
    "numbers_in",
    "protected_signal_retention",
    "split_sentences",
]
