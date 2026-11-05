"""RG05-F02: classify a user's expression-preference statement.

The chat input is the only trusted statement for a preference.  This module is
deliberately pure and deterministic: it never touches a store, never calls a
model and never grants authority.  It answers one question for the trusted host:

    * did the user state a *lasting* expression preference,
    * a *one-shot* request,
    * a *correction* of an earlier preference,
    * an ordinary (tentative) expression preference,
    * or something that must not become a preference at all (a quotation, a
      hypothetical, a third-party report, a question, an out-of-scope request)?

Only three low-impact dimensions are in scope: ``length``, ``format`` and
``tone``.  A preference about tools, permissions, privacy or an external action
is never classified as a directive; the host refuses it separately.

The returned plan never contains the user's raw text.  It records character
offsets and a digest so the plaintext turn strategy can be audited without
copying the message.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Mapping, Sequence

#: The only dimensions a tentative/expression preference may carry.
EXPRESSION_DIMENSIONS = ("length", "format", "tone")

#: The closed value vocabulary per dimension.  A phrase that does not map to one
#: of these values is not an expression preference this card can persist.
EXPRESSION_VALUES: Mapping[str, tuple[str, ...]] = {
    "length": ("concise", "balanced", "detailed"),
    "format": ("plain", "bullets"),
    "tone": ("neutral", "warm", "playful", "formal"),
}

#: Stable labels for the plan ``kind``.
KIND_EXPLICIT_LONG_TERM = "explicit_long_term"
KIND_EXPLICIT_CORRECTION = "explicit_correction"
KIND_SINGLE_SHOT = "single_shot"
KIND_TENTATIVE = "tentative"
KIND_OVERREACH = "overreach"
KIND_AMBIGUOUS = "ambiguous"
KIND_NONE = "none"

PLAN_KINDS = (
    KIND_EXPLICIT_LONG_TERM,
    KIND_EXPLICIT_CORRECTION,
    KIND_SINGLE_SHOT,
    KIND_TENTATIVE,
    KIND_OVERREACH,
    KIND_AMBIGUOUS,
    KIND_NONE,
)

DIRECTIVE_POLICY_ID = "policy:expression.directive.v1"
DIRECTIVE_POLICY_VERSION = "1"

MAX_QUOTE_CHARS = 512

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------


#: ``(keyword, dimension, value)``.  The value is the state the user asks for.
#: A negative phrase such as "别太长" is already resolved to ``concise``: the
#: stored preference is the positive expression, not the negation.
_PREFERENCE_TERMS: tuple[tuple[str, str, str], ...] = (
    # length
    ("简短", "length", "concise"),
    ("短一点", "length", "concise"),
    ("短些", "length", "concise"),
    ("短点", "length", "concise"),
    ("简洁", "length", "concise"),
    ("精简", "length", "concise"),
    ("少说", "length", "concise"),
    ("别太长", "length", "concise"),
    ("不要太长", "length", "concise"),
    ("太长", "length", "concise"),
    ("别啰嗦", "length", "concise"),
    ("不要啰嗦", "length", "concise"),
    ("别展开", "length", "concise"),
    ("不要展开", "length", "concise"),
    ("减少字数", "length", "concise"),
    ("简短一点", "length", "concise"),
    ("详细", "length", "detailed"),
    ("展开", "length", "detailed"),
    ("多说", "length", "detailed"),
    ("长一点", "length", "detailed"),
    ("长一些", "length", "detailed"),
    ("长些", "length", "detailed"),
    ("更长", "length", "detailed"),
    ("深入", "length", "detailed"),
    ("别太短", "length", "detailed"),
    ("不要太短", "length", "detailed"),
    ("适中", "length", "balanced"),
    ("平衡", "length", "balanced"),
    ("不多不少", "length", "balanced"),
    # format
    ("列表", "format", "bullets"),
    ("分点", "format", "bullets"),
    ("要点", "format", "bullets"),
    ("条列", "format", "bullets"),
    ("列点", "format", "bullets"),
    ("条目", "format", "bullets"),
    ("纯文本", "format", "plain"),
    ("不要列表", "format", "plain"),
    ("别列表", "format", "plain"),
    ("别分点", "format", "plain"),
    ("不要分点", "format", "plain"),
    ("不要格式", "format", "plain"),
    ("少格式", "format", "plain"),
    # tone
    ("温柔", "tone", "warm"),
    ("柔和", "tone", "warm"),
    ("亲切", "tone", "warm"),
    ("温和", "tone", "warm"),
    ("平静", "tone", "neutral"),
    ("中性", "tone", "neutral"),
    ("冷静", "tone", "neutral"),
    ("克制", "tone", "neutral"),
    ("别太热情", "tone", "neutral"),
    ("不要太热情", "tone", "neutral"),
    ("别卖萌", "tone", "neutral"),
    ("不要卖萌", "tone", "neutral"),
    ("俏皮", "tone", "playful"),
    ("活泼", "tone", "playful"),
    ("幽默", "tone", "playful"),
    ("轻松", "tone", "playful"),
    ("正式", "tone", "formal"),
    ("书面", "tone", "formal"),
    ("严肃", "tone", "formal"),
)

#: ``(keyword, dimension, value, verbs)``: words that only ask for a longer or
#: more concrete answer *next to a directive verb*.  ``具体`` alone is ordinary
#: language ("我喜欢红色，具体什么色号说不上来"), and treating it as a length
#: preference stores a colour statement as an expression rule.  An occurrence is
#: accepted only when a directive verb is adjacent ("说具体点", "讲得具体") or
#: the word is quantified ("具体一点"), and never when an interrogative or a
#: negation follows ("具体什么…", "具体不…").
_CONTEXTUAL_PREFERENCE_TERMS: tuple[
    tuple[str, str, str, tuple[str, ...]], ...
] = (
    (
        "具体",
        "length",
        "detailed",
        ("说", "讲", "写", "答", "描述", "介绍", "聊", "展开"),
    ),
)

#: What may immediately follow a contextual keyword and still be a request for
#: more: "具体一点", "具体点", "具体些".
_CONTEXTUAL_QUANTIFIERS: tuple[str, ...] = ("一点", "一些", "点", "些")

#: A contextual keyword followed by one of these is a question or a negation,
#: not a request ("具体什么色号", "具体不清楚").
_CONTEXTUAL_REJECT_FOLLOWERS = frozenset("什哪几是否不没别吗呢")

#: Negative phrases that assert *no* positive value ("不要简短了").  They are
#: only meaningful for a correction/revocation, never for a new preference.
_NEGATED_TERMS: tuple[tuple[str, str], ...] = (
    ("不要简短", "length"),
    ("别简短", "length"),
    ("不用简短", "length"),
    ("不要简洁", "length"),
    ("不要精简", "length"),
    ("不要详细", "length"),
    ("别详细", "length"),
    ("不要温柔", "tone"),
    ("别温柔", "tone"),
    ("不要俏皮", "tone"),
    ("别俏皮", "tone"),
)

#: Keywords that already carry their own negation and therefore express a
#: *positive* target value ("别太长" -> concise).  They must not be skipped as a
#: negated mention when a correction looks for the new value.
_NEGATIVE_FORM_VALUES = frozenset(
    {
        "别太长",
        "不要太长",
        "别啰嗦",
        "不要啰嗦",
        "别太短",
        "不要太短",
        "别展开",
        "不要展开",
        "别太热情",
        "不要太热情",
        "别卖萌",
        "不要卖萌",
        "不要列表",
        "别列表",
        "别分点",
        "不要分点",
        "不要格式",
    }
)

EXPLICIT_CUES: tuple[str, ...] = (
    "记住",
    "以后",
    "今后",
    "之后",
    "从现在起",
    "从现在开始",
    "长期",
    "一直",
    "始终",
    "每次",
    "每次都",
    "以后都",
    "往后的回答",
    "永久",
)

SINGLE_SHOT_CUES: tuple[str, ...] = (
    "这次",
    "本次",
    "本轮",
    "这轮",
    "这一轮",
    "这一回",
    "就这一次",
    "这次先",
    "暂时",
    "这一条",
    "这一个问题",
)

CORRECTION_CUES: tuple[str, ...] = (
    "说错了",
    "刚才说错",
    "不是这样",
    "不对",
    "改成",
    "改一下",
    "应该是",
    "收回",
    "取消刚才",
    "刚才那个不算",
    "不要了",
)

NEGATION_CUES: tuple[str, ...] = (
    "不要",
    "别",
    "不用",
    "不想",
    "不需要",
    "别再",
    "不要再",
)

HYPOTHETICAL_CUES: tuple[str, ...] = (
    "如果",
    "假如",
    "要是",
    "假设",
    "万一",
    "是不是",
    "要不要",
    "能不能",
    "可不可以",
    "好吗",
    "可以吗",
)

THIRD_PARTY_CUES: tuple[str, ...] = (
    "他说",
    "她说",
    "他们说",
    "别人说",
    "朋友说",
    "同事说",
    "听说",
    "据说",
    "有人说",
    "你之前说",
)

HEDGE_CUES: tuple[str, ...] = (
    "随便",
    "都行",
    "看情况",
    "也许",
    "可能",
    "不确定",
)

OVERREACH_CUES: tuple[str, ...] = (
    "允许你",
    "你可以访问",
    "联网",
    "上网",
    "发邮件",
    "外发",
    "转发",
    "上传",
    "下载",
    "删除",
    "隐私",
    "权限",
    "工具",
    "API",
    "文件",
    "系统",
    "账号",
)

COREFERENCE_CUES: tuple[str, ...] = ("这样", "这个", "按这个", "刚才说的", "就这个")

_QUOTE_MARKERS: tuple[tuple[str, str], ...] = (
    ("“", "”"),
    ("「", "」"),
    ("『", "』"),
    ('"', '"'),
)

_SENTENCE_ENDS = "。！？!?；;\n"


@dataclass(frozen=True)
class DirectivePlan:
    """One deterministic classification of the current user input."""

    kind: str
    dimension: str | None = None
    value: str | None = None
    quote_start: int | None = None
    quote_end: int | None = None
    quote_digest: str | None = None
    correction_negates: bool = False
    cues: tuple[str, ...] = ()
    guards: tuple[str, ...] = ()
    policy_id: str = DIRECTIVE_POLICY_ID
    policy_version: str = DIRECTIVE_POLICY_VERSION
    reason: str = ""

    def __post_init__(self) -> None:
        if self.kind not in PLAN_KINDS:
            raise ValueError(f"unknown directive kind {self.kind!r}")
        if self.dimension is not None and self.dimension not in EXPRESSION_DIMENSIONS:
            raise ValueError(f"unknown expression dimension {self.dimension!r}")
        if self.value is not None:
            if self.dimension is None or self.value not in EXPRESSION_VALUES[self.dimension]:
                raise ValueError(f"unknown expression value {self.value!r}")
        if self.quote_start is not None and self.quote_end is not None:
            if not (0 <= self.quote_start < self.quote_end):
                raise ValueError("quote offsets are invalid")

    def as_dict(self) -> dict[str, object]:
        """The JSON shape stored in ``turn.strategy`` (no raw user text)."""

        return {
            "kind": self.kind,
            "dimension": self.dimension,
            "value": self.value,
            "quote_start": self.quote_start,
            "quote_end": self.quote_end,
            "quote_digest": self.quote_digest,
            "correction_negates": self.correction_negates,
            "cues": list(self.cues),
            "guards": list(self.guards),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# public helpers
# ---------------------------------------------------------------------------


def match_expression_preference(text: str) -> tuple[str, str, int, int] | None:
    """The earliest ``(dimension, value, start, end)`` preference expression.

    This is the same lexicon the plan uses, exposed so the host can check that
    a model's proposed claim text names the same dimension/value the input did.
    """

    if not isinstance(text, str) or not text:
        return None
    best: tuple[str, str, int, int] | None = None
    for keyword, dimension, value, start in _preference_candidates(text):
        end = start + len(keyword)
        candidate = (dimension, value, start, end)
        if best is None:
            best = candidate
            continue
        # Earliest match wins; on a tie the longer keyword wins.
        if start < best[2] or (start == best[2] and end > best[3]):
            best = candidate
    return best


def _preference_candidates(text: str):
    """Yield ``(keyword, dimension, value, start)`` from both lexicons.

    Plain terms match on their first occurrence, as they always have.  A
    contextual term ("具体") is offered only when it really asks for a longer
    answer, so ordinary prose is not quietly promoted to an expression rule.
    """

    for keyword, dimension, value in _PREFERENCE_TERMS:
        start = text.find(keyword)
        if start >= 0:
            yield keyword, dimension, value, start
    for keyword, dimension, value, verbs in _CONTEXTUAL_PREFERENCE_TERMS:
        start = _contextual_start(text, keyword, verbs)
        if start >= 0:
            yield keyword, dimension, value, start


def _contextual_start(text: str, keyword: str, verbs: tuple[str, ...]) -> int:
    """The first occurrence of ``keyword`` that really asks for a longer answer.

    Returns ``-1`` when every occurrence is ordinary prose.  The verb must be
    *adjacent* ("说具体点", "具体说说", "讲得具体一些") or the keyword must be
    quantified ("具体一点"); a verb four characters away is not adjacency, and
    "具体的红色说不上来" is a colour statement, not an instruction.  An
    interrogative or negated follower rejects the occurrence outright.
    """

    start = text.find(keyword)
    while start >= 0:
        end = start + len(keyword)
        follower = text[end : end + 1]
        if follower and follower in _CONTEXTUAL_REJECT_FOLLOWERS:
            start = text.find(keyword, start + 1)
            continue
        after = text[end : end + 4]
        before = text[max(0, start - 3) : start]
        if after.startswith(_CONTEXTUAL_QUANTIFIERS):
            return start
        if any(after.startswith(verb) for verb in verbs):
            return start
        # "讲得具体" / "说得具体一些": a verb, an optional 得/地/的, then the word.
        if len(before) >= 2 and before[-1] in "得地的" and before[:-1].endswith(verbs):
            return start
        if before.endswith(verbs):
            return start
        start = text.find(keyword, start + 1)
    return -1


def text_is_expression_preference(text: str) -> bool:
    """Whether ``text`` names a low-impact length/format/tone preference."""

    return match_expression_preference(text) is not None


def quote_text(text: str, plan: DirectivePlan) -> str | None:
    """The exact quote a plan points at, or ``None`` when the offsets are stale."""

    if plan.quote_start is None or plan.quote_end is None:
        return None
    if not (0 <= plan.quote_start < plan.quote_end <= len(text)):
        return None
    quote = text[plan.quote_start : plan.quote_end]
    if not quote or quote != quote.strip():
        return None
    if plan.quote_digest is not None:
        digest = "sha256:" + hashlib.sha256(quote.encode("utf-8")).hexdigest()
        if digest != plan.quote_digest:
            return None
    return quote


def plan_from_dict(value: Mapping[str, object] | None) -> DirectivePlan | None:
    """Rebuild a plan from the stored turn strategy (never from model output)."""

    if not isinstance(value, Mapping):
        return None
    kind = value.get("kind")
    if kind not in PLAN_KINDS:
        return None
    try:
        return DirectivePlan(
            kind=str(kind),
            dimension=value.get("dimension") if isinstance(value.get("dimension"), str) else None,
            value=value.get("value") if isinstance(value.get("value"), str) else None,
            quote_start=value.get("quote_start") if type(value.get("quote_start")) is int else None,
            quote_end=value.get("quote_end") if type(value.get("quote_end")) is int else None,
            quote_digest=value.get("quote_digest") if isinstance(value.get("quote_digest"), str) else None,
            correction_negates=bool(value.get("correction_negates")),
            cues=tuple(value.get("cues") or ()),
            guards=tuple(value.get("guards") or ()),
            policy_id=str(value.get("policy_id") or DIRECTIVE_POLICY_ID),
            policy_version=str(value.get("policy_version") or DIRECTIVE_POLICY_VERSION),
            reason=str(value.get("reason") or ""),
        )
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# classifier
# ---------------------------------------------------------------------------


def classify_directive(
    text: str,
    *,
    active_tentative: Mapping[str, str] | None = None,
) -> DirectivePlan:
    """Classify one user message; never raises on ordinary text."""

    raw = text if isinstance(text, str) else ""
    if not raw.strip():
        return _plan(KIND_NONE, reason="empty_input")

    # 1. Out-of-scope: an expression preference that tries to change tool,
    #    privacy or external-action authority is never auto-applied.
    if _has_any(raw, OVERREACH_CUES) and (
        _has_any(raw, (kw for kw, _dim, _val in _PREFERENCE_TERMS))
        or _has_any(raw, EXPLICIT_CUES)
        or _has_any(raw, SINGLE_SHOT_CUES)
    ):
        return _plan(
            KIND_OVERREACH, guards=("out_of_scope",), reason="tool_or_privacy_object"
        )

    # 2. A question is a request for an answer, not a lasting statement.
    preference = match_expression_preference(raw)
    correction_index = _first_index(raw, CORRECTION_CUES + NEGATION_CUES)
    anchor = (
        preference[2]
        if preference is not None
        else (correction_index if correction_index is not None else 0)
    )
    sentence_bounds = _sentence_bounds(raw, anchor)
    sentence = raw[sentence_bounds[0] : sentence_bounds[1]]
    if _is_question(sentence):
        return _plan(KIND_NONE, guards=("question",), reason="question_not_statement")

    # 3. A quoted / hypothetical / third-party / hedged sentence cannot become
    #    a lasting directive or a correction.
    guards = _guards(sentence, max(0, anchor - sentence_bounds[0]))
    if guards:
        dimension = preference[0] if preference else None
        value = preference[1] if preference else None
        return _plan(
            KIND_AMBIGUOUS,
            dimension=dimension,
            value=value,
            guards=guards,
            reason="guarded",
        )

    # 4. Correction/revocation has its own path and wins over "lasting" cues.
    correction = _correction(raw, active_tentative, preference)
    if correction is not None:
        return correction

    if preference is None:
        # "以后就这样" can refer to the only active tentative preference.
        coreference = _explicit_coreference_for_active(
            raw, active_tentative, anchor
        )
        if coreference is not None:
            return coreference
        return _plan(KIND_NONE, reason="no_expression_preference")

    dimension, value, start, _end = preference
    quote_start, quote_end, digest = _quote(raw, sentence_bounds, start)
    clause_bounds = _clause_bounds(raw, start)
    clause = raw[clause_bounds[0] : clause_bounds[1]]
    explicit = _has_any(clause, EXPLICIT_CUES) or _has_any(sentence, EXPLICIT_CUES)
    single = _has_any(clause, SINGLE_SHOT_CUES) or _has_any(sentence, SINGLE_SHOT_CUES)
    clause_explicit = _has_any(clause, EXPLICIT_CUES)
    clause_single = _has_any(clause, SINGLE_SHOT_CUES)

    # A later/earlier clause or sentence that says "以后就这样" upgrades the
    # statement to explicit long-term, even when this clause is one-shot.
    if not clause_explicit:
        upgraded = _explicit_coreference(raw, clause_bounds, dimension, value, start)
        if upgraded:
            return upgraded

    if clause_explicit and clause_single:
        return _plan(
            KIND_AMBIGUOUS,
            dimension=dimension,
            value=value,
            quote_start=quote_start,
            quote_end=quote_end,
            quote_digest=digest,
            guards=("conflicting_cues",),
            reason="explicit_and_one_shot_in_one_clause",
        )
    if explicit and not clause_single:
        return _plan(
            KIND_EXPLICIT_LONG_TERM,
            dimension=dimension,
            value=value,
            quote_start=quote_start,
            quote_end=quote_end,
            quote_digest=digest,
            cues=tuple(cue for cue in EXPLICIT_CUES if cue in (clause + sentence)),
            reason="explicit_cue_with_value",
        )
    if single:
        return _plan(
            KIND_SINGLE_SHOT,
            dimension=dimension,
            value=value,
            quote_start=quote_start,
            quote_end=quote_end,
            quote_digest=digest,
            cues=tuple(cue for cue in SINGLE_SHOT_CUES if cue in (clause + sentence)),
            reason="one_shot_cue_with_value",
        )
    return _plan(
        KIND_TENTATIVE,
        dimension=dimension,
        value=value,
        quote_start=quote_start,
        quote_end=quote_end,
        quote_digest=digest,
        reason="ordinary_expression_preference",
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _plan(kind: str, **kwargs: object) -> DirectivePlan:
    return DirectivePlan(kind=kind, **kwargs)  # type: ignore[arg-type]


def _has_any(text: str, cues) -> bool:
    return any(cue in text for cue in cues)


def _is_question(sentence: str) -> bool:
    stripped = sentence.strip()
    if not stripped:
        return False
    if stripped.endswith(("？", "?")):
        return True
    # "可以简短一点吗" / "好吗" inside a short clause.
    tail = stripped[-6:]
    return "吗" in tail or "呢" in tail


def _sentence_bounds(text: str, index: int) -> tuple[int, int]:
    return _bounds(text, index, _SENTENCE_ENDS)


def _clause_bounds(text: str, index: int) -> tuple[int, int]:
    return _bounds(text, index, _SENTENCE_ENDS + "，,")


def _bounds(text: str, index: int, separators: str) -> tuple[int, int]:
    start = 0
    for position in range(0, max(0, index)):
        if text[position] in separators:
            start = position + 1
    end = len(text)
    for position in range(max(0, index), len(text)):
        if text[position] in separators:
            end = position + 1
            break
    # Trim surrounding whitespace while keeping offsets exact.
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _quote(
    text: str, bounds: tuple[int, int], match_start: int
) -> tuple[int, int, str]:
    start, end = bounds
    if end - start > MAX_QUOTE_CHARS:
        # Keep a bounded window that still contains the matched phrase.
        start = max(start, match_start - MAX_QUOTE_CHARS // 2)
        end = min(len(text), start + MAX_QUOTE_CHARS)
        if match_start >= end:
            end = min(len(text), match_start + 1)
    quote = text[start:end]
    digest = "sha256:" + hashlib.sha256(quote.encode("utf-8")).hexdigest()
    return start, end, digest


def _inside_quote(sentence: str, index: int) -> bool:
    prefix = sentence[:index]
    for opening, closing in _QUOTE_MARKERS:
        if prefix.count(opening) > prefix.count(closing):
            return True
    return False


def _guards(sentence: str, relative: int) -> tuple[str, ...]:
    guards: list[str] = []
    if _inside_quote(sentence, max(0, relative)):
        guards.append("quoted")
    if _has_any(sentence, HYPOTHETICAL_CUES):
        guards.append("hypothetical")
    if _has_any(sentence, HEDGE_CUES):
        guards.append("hedged")
    if _has_any(sentence[: max(0, relative)], THIRD_PARTY_CUES):
        guards.append("third_party")
    return tuple(guards)


def _explicit_coreference(
    text: str,
    bounds: tuple[int, int],
    dimension: str,
    value: str,
    match_start: int,
) -> DirectivePlan | None:
    """Upgrade "以后就这样" in an adjacent sentence to explicit long-term."""

    start, end = bounds
    neighbours: list[tuple[int, int]] = []
    for bounds_fn in (_clause_bounds, _sentence_bounds):
        if end < len(text):
            neighbours.append(bounds_fn(text, end))
        if start > 0:
            neighbours.append(bounds_fn(text, start - 1))
    seen: set[tuple[int, int]] = set()
    for neighbour_start, neighbour_end in neighbours:
        if (neighbour_start, neighbour_end) in seen:
            continue
        seen.add((neighbour_start, neighbour_end))
        sentence = text[neighbour_start:neighbour_end]
        if not _has_any(sentence, EXPLICIT_CUES):
            continue
        if not _has_any(sentence, COREFERENCE_CUES):
            continue
        if _has_any(sentence, SINGLE_SHOT_CUES):
            continue
        quote_start, quote_end, digest = _quote(text, bounds, match_start)
        return _plan(
            KIND_EXPLICIT_LONG_TERM,
            dimension=dimension,
            value=value,
            quote_start=quote_start,
            quote_end=quote_end,
            quote_digest=digest,
            cues=tuple(cue for cue in EXPLICIT_CUES if cue in sentence),
            reason="explicit_coreference_upgrade",
        )
    return None


def _explicit_coreference_for_active(
    text: str,
    active_tentative: Mapping[str, str] | None,
    anchor: int,
) -> DirectivePlan | None:
    """Resolve "以后都这样" against the only active tentative preference."""

    if not active_tentative:
        return None
    if not _has_any(text, COREFERENCE_CUES):
        return None
    if not _has_any(text, EXPLICIT_CUES):
        return None
    dimensions = [dim for dim in EXPRESSION_DIMENSIONS if dim in active_tentative]
    if len(dimensions) != 1:
        return _plan(
            KIND_AMBIGUOUS,
            guards=("coreference_ambiguous",),
            reason="more_than_one_active_tentative",
        )
    dimension = dimensions[0]
    value = str(active_tentative[dimension])
    bounds = _sentence_bounds(text, anchor)
    quote_start, quote_end, digest = _quote(text, bounds, anchor)
    return _plan(
        KIND_EXPLICIT_LONG_TERM,
        dimension=dimension,
        value=value,
        quote_start=quote_start,
        quote_end=quote_end,
        quote_digest=digest,
        cues=tuple(cue for cue in EXPLICIT_CUES if cue in text),
        reason="explicit_coreference_active",
    )


def _correction(
    text: str,
    active_tentative: Mapping[str, str] | None,
    preference: tuple[str, str, int, int] | None,
) -> DirectivePlan | None:
    has_correction_cue = _has_any(text, CORRECTION_CUES)
    negated_dimension = _negated_dimension(text)
    coreference = _coreference_dimension(text, active_tentative)
    if (
        not has_correction_cue
        and negated_dimension is None
        and not (coreference is not None and _has_any(text, NEGATION_CUES))
    ):
        return None

    positive = _positive_preference(text)
    dimension = (
        positive[0]
        if positive is not None
        else (negated_dimension or coreference)
    )
    if dimension is None:
        return _plan(
            KIND_AMBIGUOUS,
            guards=("correction_without_target",),
            reason="correction_cannot_name_a_dimension",
        )
    negates = (
        negated_dimension is not None
        or has_correction_cue
        or _has_any(text, NEGATION_CUES)
    )
    if positive is None:
        if negates:
            return _plan(
                KIND_EXPLICIT_CORRECTION,
                dimension=dimension,
                correction_negates=True,
                cues=tuple(cue for cue in CORRECTION_CUES if cue in text),
                reason="correction_without_new_value",
            )
        return _plan(
            KIND_AMBIGUOUS,
            guards=("correction_without_value",),
            reason="correction_has_no_resolvable_value",
        )
    value = positive[1]
    quote_bounds = _sentence_bounds(text, positive[2])
    quote_start, quote_end, digest = _quote(text, quote_bounds, positive[2])
    return _plan(
        KIND_EXPLICIT_CORRECTION,
        dimension=dimension,
        value=value,
        quote_start=quote_start,
        quote_end=quote_end,
        quote_digest=digest,
        correction_negates=negates,
        cues=tuple(cue for cue in CORRECTION_CUES if cue in text),
        reason="correction_with_value",
    )


def _positive_preference(text: str) -> tuple[str, str, int, int] | None:
    """The first preference phrase that is not a negated mention.

    "不要简短，还是要详细" has two matches; the first is the value being
    revoked, the second is the new value.  A phrase that already carries its
    own negation ("别太长" -> concise) stays a valid positive target.
    """

    best: tuple[str, str, int, int] | None = None
    for keyword, dimension, value, start in _preference_candidates(text):
        if keyword not in _NEGATIVE_FORM_VALUES:
            before = text[max(0, start - 3) : start]
            if _has_any(before, NEGATION_CUES):
                continue
        end = start + len(keyword)
        candidate = (dimension, value, start, end)
        if best is None:
            best = candidate
        elif start < best[2] or (start == best[2] and end > best[3]):
            best = candidate
    return best


def _first_index(text: str, cues: Sequence[str]) -> int | None:
    positions = [text.find(cue) for cue in cues]
    positions = [position for position in positions if position >= 0]
    return min(positions) if positions else None


def _negated_dimension(text: str) -> str | None:
    for keyword, dimension in _NEGATED_TERMS:
        if keyword in text:
            return dimension
    return None


def _coreference_dimension(
    text: str, active_tentative: Mapping[str, str] | None
) -> str | None:
    if not active_tentative or not _has_any(text, COREFERENCE_CUES):
        return None
    dimensions = [dim for dim in EXPRESSION_DIMENSIONS if dim in active_tentative]
    return dimensions[0] if len(dimensions) == 1 else None


def canonical_claim(dimension: str, value: str) -> str:
    """The host-authored claim text for one confirmed expression preference."""

    labels = {
        ("length", "concise"): "用户在聊天中希望回答保持简短。",
        ("length", "balanced"): "用户在聊天中希望回答长度适中。",
        ("length", "detailed"): "用户在聊天中希望回答更详细。",
        ("format", "plain"): "用户在聊天中希望回答使用纯文本、少用列表。",
        ("format", "bullets"): "用户在聊天中希望回答用列表分点。",
        ("tone", "neutral"): "用户在聊天中希望语气平静克制。",
        ("tone", "warm"): "用户在聊天中希望语气温柔。",
        ("tone", "playful"): "用户在聊天中希望语气俏皮轻松。",
        ("tone", "formal"): "用户在聊天中希望语气正式。",
    }
    return labels.get(
        (dimension, value),
        f"用户希望表达偏好为 {dimension}={value}。",
    )


def canonical_value_label(dimension: str, value: str) -> str:
    labels = {
        ("length", "concise"): "简短",
        ("length", "balanced"): "适中",
        ("length", "detailed"): "详细",
        ("format", "plain"): "纯文本",
        ("format", "bullets"): "列表",
        ("tone", "neutral"): "平静",
        ("tone", "warm"): "温柔",
        ("tone", "playful"): "俏皮",
        ("tone", "formal"): "正式",
    }
    return labels.get((dimension, value), value)


def preference_marker(dimension: str, value: str) -> str:
    """The machine-readable marker stored in ``review_condition``."""

    return f"expression_preference:{dimension}={value}"


def parse_preference_marker(text: object) -> tuple[str, str] | None:
    if not isinstance(text, str) or not text.startswith("expression_preference:"):
        return None
    payload = text.split(":", 1)[1]
    if "=" not in payload:
        return None
    dimension, value = payload.split("=", 1)
    if dimension not in EXPRESSION_VALUES:
        return None
    if value not in EXPRESSION_VALUES[dimension]:
        return None
    return dimension, value


__all__ = [
    "CORRECTION_CUES",
    "DIRECTIVE_POLICY_ID",
    "DIRECTIVE_POLICY_VERSION",
    "DirectivePlan",
    "EXPLICIT_CUES",
    "EXPRESSION_DIMENSIONS",
    "EXPRESSION_VALUES",
    "KIND_AMBIGUOUS",
    "KIND_EXPLICIT_CORRECTION",
    "KIND_EXPLICIT_LONG_TERM",
    "KIND_NONE",
    "KIND_OVERREACH",
    "KIND_SINGLE_SHOT",
    "KIND_TENTATIVE",
    "PLAN_KINDS",
    "SINGLE_SHOT_CUES",
    "canonical_claim",
    "canonical_value_label",
    "classify_directive",
    "match_expression_preference",
    "parse_preference_marker",
    "plan_from_dict",
    "preference_marker",
    "quote_text",
    "text_is_expression_preference",
]
