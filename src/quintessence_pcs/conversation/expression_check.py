"""RG07: deterministic checks and observation metrics for the expression stage.

The module is pure: the caller supplies the draft, the reply, the expression
material and the examples, and gets back a verdict plus counts.  It never
calls a model, never reads the store and never writes a row.

The hard rules are deliberately few and explainable; everything else is an
observation metric for the Lab report and for the user's own judgement.  These
checks do not prove semantic equivalence -- they catch the failures that are
cheap and repeatable: new numbers, upgraded certainty, dropped uncertainty,
"remembered" claims while something is still pending, runaway length and a
verbatim repeat of an already published short sentence.

RG08 T0B: the sentence-level metric bundle is **not** implemented here.  It
lives in :mod:`quintessence_pcs.conversation.expression_metrics`, the single
implementation shared with the Lab offline analyzer.  This module keeps the
seven hard gates and the few character-level metrics that the shared module
does not own (LCS overlap, warm markers, commentary repetition); the shared
bundle is merged verbatim under its own key names.  Merging metrics never
changes a failure: the observation half stays observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Mapping, Sequence

from .expression_metrics import (
    UNCERTAINTY_MARKERS,
    compute_metrics,
    count_markers as _count_any,
    normalise_text as _normalise,
    numbers_in as _numbers,
)

#: Only this many characters of each side are compared for the longest common
#: substring; long technical answers stay O(n*m) but bounded.
LCS_CHARS = 4000

MAX_REPLY_GROWTH = (3, 600)

#: Hard-rule labels (machine readable, never shown to the user as prose).
FAILURE_EMPTY_REPLY = "empty_reply"
FAILURE_NEW_NUMBERS = "new_numbers"
FAILURE_UNCERTAINTY_DROPPED = "uncertainty_dropped"
FAILURE_CERTAINTY_UPGRADE = "certainty_upgrade"
FAILURE_MEMORY_CLAIM = "memory_claim_while_pending"
FAILURE_TOO_LONG = "too_long"
FAILURE_REPEATS_COMMENTARY = "repeats_commentary"

#: The uncertainty scale is imported from the metrics module: the H3 gate and
#: the ``uncertainty_retained`` signal must never drift apart.
CERTAINTY_MARKERS = ("保证", "一定", "必然", "永远", "绝不会", "百分百", "肯定")
MEMORY_CLAIM_MARKERS = ("已记住", "已经记住", "已保存", "已记录", "已写入", "永久保存")

WARM_MARKERS = ("呀", "啦", "哦", "呢", "嘛", "啊")
WARM_PATTERNS = ("真的好", "超级", "太…了", "太好了")
COMFORT_TEMPLATES = (
    "别急着",
    "先歇一会儿",
    "慢慢来",
    "辛苦了",
    "我听着",
    "想说说吗",
    "已经做得很好了",
    "会好起来的",
)
QUESTION_MARKS = ("？", "?")
EXCLAMATIONS = ("！", "!")


@dataclass(frozen=True)
class ExpressionVerdict:
    ok: bool
    failures: tuple[str, ...]
    metrics: dict[str, int | float]


def _longest_common_substring(left: str, right: str) -> int:
    left = _normalise(left)[:LCS_CHARS]
    right = _normalise(right)[:LCS_CHARS]
    if not left or not right:
        return 0
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    match = matcher.find_longest_match(0, len(left), 0, len(right))
    return int(match.size)


def check_expression(
    draft: str,
    reply: str,
    *,
    published_commentary: Sequence[str] = (),
    expression_material: Mapping[str, Any] | None = None,
    pending_cognition_changes: int = 0,
    examples: Sequence[Mapping[str, Any]] = (),
) -> ExpressionVerdict:
    """Apply the hard rules and collect the observation metrics."""

    failures: list[str] = []
    metrics: dict[str, int | float] = {}

    if not isinstance(reply, str) or not reply.strip():
        failures.append(FAILURE_EMPTY_REPLY)
        reply = reply if isinstance(reply, str) else ""

    draft_text = _normalise(draft)
    reply_text = _normalise(reply)

    # H2: no number that the content stage did not produce.
    new_numbers = sorted(_numbers(reply_text) - _numbers(draft_text))
    metrics["new_numbers"] = len(new_numbers)
    if new_numbers:
        failures.append(FAILURE_NEW_NUMBERS)

    # H3: never drop every uncertainty marker, never add a strong assertion.
    draft_uncertain = _count_any(draft_text, UNCERTAINTY_MARKERS)
    reply_uncertain = _count_any(reply_text, UNCERTAINTY_MARKERS)
    uncertainty_dropped = bool(draft_uncertain) and not reply_uncertain
    metrics["uncertainty_dropped"] = int(uncertainty_dropped)
    if uncertainty_dropped:
        failures.append(FAILURE_UNCERTAINTY_DROPPED)
    certainty_upgrades = _count_any(reply_text, CERTAINTY_MARKERS) - _count_any(
        draft_text, CERTAINTY_MARKERS
    )
    metrics["certainty_upgrades"] = max(0, certainty_upgrades)
    if certainty_upgrades > 0:
        failures.append(FAILURE_CERTAINTY_UPGRADE)

    # H4: an unreviewed change must never be described as remembered.
    memory_claims = _count_any(reply_text, MEMORY_CLAIM_MARKERS)
    metrics["memory_claims"] = memory_claims
    if pending_cognition_changes > 0 and memory_claims:
        failures.append(FAILURE_MEMORY_CLAIM)

    # H5: no runaway growth.  Shrinking is allowed (including deleting
    # repetition and padding); the draft's necessary facts are protected by
    # the prompt and the other hard rules, not by a length floor.
    growth_limit = MAX_REPLY_GROWTH[0] * len(draft_text) + MAX_REPLY_GROWTH[1]
    metrics["length_ratio"] = (
        round(len(reply_text) / len(draft_text), 3) if draft_text else 0.0
    )
    if len(reply_text) > growth_limit:
        failures.append(FAILURE_TOO_LONG)

    # H6: never repeat an already published short sentence verbatim.
    for text in published_commentary:
        normalised = _normalise(text)
        if normalised and normalised in reply_text:
            failures.append(FAILURE_REPEATS_COMMENTARY)
            break

    # ---- observation metrics (never a pass/fail) -------------------------
    warm = _count_any(reply_text, WARM_MARKERS)
    warm += _count_any(reply_text, WARM_PATTERNS)
    warm += _count_any(reply_text, EXCLAMATIONS)
    metrics["warm_markers_per_100"] = (
        round(warm * 100 / len(reply_text), 3) if reply_text else 0.0
    )
    metrics["comfort_template_hits"] = _count_any(reply_text, COMFORT_TEMPLATES)
    metrics["new_questions"] = max(
        0,
        _count_any(reply_text, QUESTION_MARKS)
        - _count_any(draft_text, QUESTION_MARKS),
    )
    metrics["draft_lcs_chars"] = _longest_common_substring(draft_text, reply_text)
    example_lcs = 0
    for example in examples:
        if not isinstance(example, Mapping):
            continue
        assistant = example.get("assistant")
        if isinstance(assistant, str) and assistant:
            example_lcs = max(
                example_lcs, _longest_common_substring(assistant, reply_text)
            )
    metrics["example_lcs_chars"] = example_lcs
    metrics["newline_delta"] = reply_text.count("\n") - draft_text.count("\n")
    metrics["reply_chars"] = len(reply_text)
    metrics["draft_chars"] = len(draft_text)

    # ---- the shared metric bundle (single implementation) ----------------
    # Sentence alignment, question/advice/analysis activity, protected-signal
    # retention and bubble deltas all come from ``expression_metrics``; this
    # module never re-implements them.  The keys keep the module's own names
    # so the ``expression_checked`` event and the Lab analyzer agree byte for
    # byte.  Bubble counts stay at the module defaults until a later task
    # threads the real counts through (the public signature is frozen).
    metrics.update(compute_metrics(draft, reply))

    return ExpressionVerdict(
        ok=not failures,
        failures=tuple(failures),
        metrics=metrics,
    )


__all__ = [
    "CERTAINTY_MARKERS",
    "COMFORT_TEMPLATES",
    "ExpressionVerdict",
    "FAILURE_CERTAINTY_UPGRADE",
    "FAILURE_EMPTY_REPLY",
    "FAILURE_MEMORY_CLAIM",
    "FAILURE_NEW_NUMBERS",
    "FAILURE_REPEATS_COMMENTARY",
    "FAILURE_TOO_LONG",
    "FAILURE_UNCERTAINTY_DROPPED",
    "MEMORY_CLAIM_MARKERS",
    "UNCERTAINTY_MARKERS",
    "check_expression",
]
