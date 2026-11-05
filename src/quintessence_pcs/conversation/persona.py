"""RG06: the host-side persona startup contract and expression materials.

This module owns three things and nothing else:

* the optional ``app.persona_expected`` contract: an instance may declare the
  one persona it was set up with.  The contract is checked at **startup and
  restart only** (Q2 ruling); a later ``profile.activate`` is a controlled
  management action and takes effect from the next turn.  A miss is an explicit
  ``HOST_NOT_READY`` with ``EXPECTED_PERSONA_UNAVAILABLE`` naming the expected
  id -- never a silent fallback to a neutral persona.
* the current-speaker value: identity is supplied by the trusted host and
  rendered into the Context; it is never inferred from a user message.
* deterministic expression-example selection and the material the Context
  renderer needs (selected examples plus host-generated provenance markers).

No model call, no embedding retrieval, no emotion/intimacy score, no persona
graph and no classifier live here.  Profile bodies keep their existing
top-level keys; ``PROFILE_SCHEMA_VERSION`` is "3" because one example may
carry the optional ``pre_tool_commentary`` (the public line spoken before a
tool call) and the optional ``assistant_messages`` (the ordered bubbles of one
final reply).  Schema-1 and schema-2 bodies remain valid and keep their exact
old shape.  Relationship semantics stay in full natural language inside
``identity_summary``/``principles``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..domains.persona.profiles import (
    PERSONA_PROFILE_KIND,
    PROFILE_ID_PATTERN,
    REVIEW_APPROVE_EVENT_TYPE,
    REVIEW_SOURCE_KIND,
)
from ..foundation.errors import (
    CorruptStore,
    EventNotFound,
    InvalidInput,
    RecordNotFound,
    Redacted,
)

#: Host status codes (never PCS storage error codes).
EXPECTED_PERSONA_UNAVAILABLE = "EXPECTED_PERSONA_UNAVAILABLE"
NEUTRAL_PROVISION_FAILED = "NEUTRAL_PROVISION_FAILED"
PROFILE_CORRUPT = "PROFILE_CORRUPT"
PROFILE_MISSING = "PROFILE_MISSING"

#: The only identities the host may declare for the current speaker.
SPEAKER_IDENTITIES = ("owner", "known_other", "unknown")

#: Fixed scene labels an example may carry.  These describe an *expression*
#: situation and an audience (``owner-*`` vs ``other-*``), never a relationship
#: the model may transfer to someone else.
EXAMPLE_SCENES = (
    "owner-tired",
    "owner-playful",
    "owner-close",
    "owner-decide",
    "other-help",
    "owner-tool-query",
    "owner-software-trial",
    # A general owner demo with no specific situation.  It is never selected by
    # a cue; it only tops up a chat turn that has fewer than two applicable
    # scene demos (see ``select_examples``).
    "owner-base",
)

#: The one scene that is a general expression demo rather than a scene match.
BASE_SCENE = "owner-base"

#: The host's initial expression-demo budget.  Debug parameters, not a contract.
DEFAULT_MAX_EXAMPLES = 2
DEFAULT_MAX_EXAMPLE_BYTES = 2048

#: Cue -> scene tags.  A fixed keyword table is the "simple scene label"
#: selection of the plan; it is deliberately not a classifier.
_CUE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "tired",
        (
            "累",
            "困",
            "难受",
            "沮丧",
            "焦虑",
            "压力",
            "烦",
            "委屈",
            "不舒服",
            "没做出来",
            "好多了",
        ),
    ),
    (
        "playful",
        ("哈哈", "笑", "开玩笑", "逗", "有趣", "好玩", "得意", "保证", "早上好", "叹气"),
    ),
    (
        "decide",
        ("要不", "算了", "还是", "选哪个", "你决定", "帮我选", "哪个好", "做不完"),
    ),
    (
        "tool",
        ("上次", "之前", "记录", "停在", "进度", "翻一下", "查一下", "看看上次"),
    ),
    (
        "technical",
        ("技术", "代码", "报错", "原理", "实现", "调试", "架构", "参数", "算法", "部署", "sql", "api", "函数", "为什么"),
    ),
    ("help", ("没听懂", "解释", "帮我看", "怎么用", "请问", "能不能")),
    ("unknown", ("你是谁", "我们认识吗", "认识我吗", "第一次")),
    (
        "close",
        ("喜欢你", "想你", "想我", "表白", "爱你"),
    ),
    (
        "software_trial",
        ("试聊", "软件", "测试版", "能与你说话", "真正能与你"),
    ),
)
_CUE_SCENES: dict[str, tuple[str, ...]] = {
    "tired": ("owner-tired",),
    "playful": ("owner-playful",),
    "decide": ("owner-decide",),
    "tool": ("owner-tool-query",),
    "technical": ("other-help",),
    "help": ("other-help",),
    "unknown": ("other-help",),
    "close": ("owner-close",),
    "software_trial": ("owner-software-trial",),
}


class PersonaUnavailable(Exception):
    """A startup persona state the host must not paper over."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class PersonaExpectationConflict(Exception):
    """One ``profile.activate`` target that is not allowed (never "version")."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class PersonaExpectation:
    """The instance-level expected persona declared in ``app.persona_expected``."""

    profile_id: str
    profile_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"profile_id": self.profile_id}
        if self.profile_version is not None:
            value["profile_version"] = self.profile_version
        return value

    def label(self) -> str:
        if self.profile_version is None:
            return self.profile_id
        return f"{self.profile_id}@{self.profile_version}"


@dataclass(frozen=True)
class Speaker:
    """The current speaker supplied by the trusted host configuration."""

    identity: str
    display_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"identity": self.identity, "source": "host_config"}
        if self.display_name is not None:
            value["display_name"] = self.display_name
        return value


@dataclass(frozen=True)
class PersonaState:
    """The persona the host will actually use, and how it became active."""

    source: str  # "expected" | "active" | "neutral_provisioned"
    profile_id: str
    profile_version: str
    record_id: str
    record_version: int
    asset_hash: str
    review_event_id: str
    speaker: Speaker | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "state": "ready",
            "source": self.source,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "record_id": self.record_id,
            "record_version": self.record_version,
            "asset_hash": self.asset_hash,
            "speaker": None if self.speaker is None else self.speaker.to_dict(),
        }


def parse_expectation(value: Any) -> PersonaExpectation | None:
    """Validate one ``app.persona_expected`` value (closed key set)."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidInput("app.persona_expected must be a JSON object")
    keys = set(value)
    unknown = sorted(str(key) for key in keys - {"profile_id", "profile_version"})
    if unknown:
        raise InvalidInput(
            "app.persona_expected has unknown field(s): " + ", ".join(unknown)
        )
    profile_id = value.get("profile_id")
    if not isinstance(profile_id, str) or not profile_id:
        raise InvalidInput("app.persona_expected.profile_id must be a non-empty string")
    if PROFILE_ID_PATTERN.fullmatch(profile_id) is None:
        raise InvalidInput(
            "app.persona_expected.profile_id must match "
            "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
        )
    raw_version = value.get("profile_version")
    if raw_version is not None:
        if not isinstance(raw_version, str) or not 1 <= len(raw_version) <= 128:
            raise InvalidInput(
                "app.persona_expected.profile_version must be 1..128 characters"
            )
        if not raw_version.strip():
            raise InvalidInput(
                "app.persona_expected.profile_version must be a non-empty string"
            )
    return PersonaExpectation(profile_id, raw_version)


def parse_speaker(value: Any) -> Speaker | None:
    """Validate one ``app.speaker`` value (closed key set)."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidInput("app.speaker must be a JSON object")
    unknown = sorted(str(key) for key in set(value) - {"identity", "display_name"})
    if unknown:
        raise InvalidInput("app.speaker has unknown field(s): " + ", ".join(unknown))
    identity = value.get("identity")
    if identity not in SPEAKER_IDENTITIES:
        raise InvalidInput(
            "app.speaker.identity must be one of: " + ", ".join(SPEAKER_IDENTITIES)
        )
    raw_name = value.get("display_name")
    if raw_name is None:
        return Speaker(identity)
    if not isinstance(raw_name, str) or not 1 <= len(raw_name) <= 64:
        raise InvalidInput("app.speaker.display_name must be 1..64 characters")
    if any(char in raw_name for char in "\x00\r\n\t"):
        raise InvalidInput("app.speaker.display_name must not contain control characters")
    if identity == "unknown":
        raise InvalidInput(
            "app.speaker.display_name is not allowed when identity is 'unknown'"
        )
    return Speaker(identity, raw_name)


def persona_matches_expected(
    state: PersonaState | None, expectation: PersonaExpectation | None
) -> bool | None:
    """Display-only comparison of the active persona against the contract.

    This is never used to reject a turn (Q2): it exists so ``app.info`` and
    ``profile.status`` can show "expected vs active" side by side.
    """

    if expectation is None:
        return None
    if state is None:
        return False
    if state.profile_id != expectation.profile_id:
        return False
    if (
        expectation.profile_version is not None
        and state.profile_version != expectation.profile_version
    ):
        return False
    return True


def _expected_unavailable(
    expectation: PersonaExpectation, cause: str
) -> PersonaUnavailable:
    return PersonaUnavailable(
        EXPECTED_PERSONA_UNAVAILABLE,
        f"the expected persona {expectation.label()} is unavailable ({cause})",
    )


def verify_review_ref(
    store: Any,
    namespace: str,
    body: Mapping[str, Any],
    *,
    code: str = PROFILE_CORRUPT,
    prefix: str = "the persona profile is not verifiable",
) -> str:
    """Prove the profile body's approval event still exists and is trusted."""

    raw = body.get("review_ref")
    if not isinstance(raw, Mapping):
        raise PersonaUnavailable(code, f"{prefix} (review_ref_missing)")
    event_id = raw.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise PersonaUnavailable(code, f"{prefix} (review_ref_missing)")
    try:
        event = store.get_event(namespace, event_id)
    except Redacted as exc:
        raise PersonaUnavailable(code, f"{prefix} (review_ref_redacted)") from exc
    except (EventNotFound, RecordNotFound) as exc:
        raise PersonaUnavailable(code, f"{prefix} (review_ref_missing)") from exc
    if event.source_kind != REVIEW_SOURCE_KIND or (
        event.event_type != REVIEW_APPROVE_EVENT_TYPE
    ):
        raise PersonaUnavailable(code, f"{prefix} (review_ref_untrusted)")
    return event_id


def _state_from_ref(
    store: Any,
    namespace: str,
    ref: Any,
    *,
    source: str,
    code: str,
    prefix: str,
    expectation: PersonaExpectation | None = None,
) -> PersonaState:
    try:
        version = store.get_version(namespace, ref.record_id, ref.record_version)
    except Redacted as exc:
        if expectation is not None:
            raise _expected_unavailable(expectation, "profile_deleted") from exc
        raise PersonaUnavailable(code, f"{prefix} (profile_deleted)") from exc
    except RecordNotFound as exc:
        if expectation is not None:
            raise _expected_unavailable(expectation, "profile_version_missing") from exc
        raise PersonaUnavailable(code, f"{prefix} (profile_version_missing)") from exc
    if version.kind != PERSONA_PROFILE_KIND:
        if expectation is not None:
            raise _expected_unavailable(expectation, "record_kind_mismatch")
        raise PersonaUnavailable(code, f"{prefix} (record_kind_mismatch)")
    body = dict(version.body)
    profile_id = body.get("profile_id")
    profile_version = body.get("profile_version")
    asset_hash = body.get("asset_hash")
    if not isinstance(profile_id, str) or not isinstance(profile_version, str):
        if expectation is not None:
            raise _expected_unavailable(expectation, "profile_body_incomplete")
        raise PersonaUnavailable(code, f"{prefix} (profile_body_incomplete)")
    review_event_id = verify_review_ref(
        store, namespace, body, code=code, prefix=prefix
    )
    return PersonaState(
        source=source,
        profile_id=profile_id,
        profile_version=profile_version,
        record_id=str(ref.record_id),
        record_version=int(ref.record_version),
        asset_hash=str(asset_hash or ""),
        review_event_id=review_event_id,
    )


def read_active_persona(
    profiles: Any, store: Any, namespace: str, *, source: str = "active"
) -> PersonaState:
    """Read the active profile and prove it is a trusted, undeleted version."""

    prefix = "the active persona profile is not verifiable"
    try:
        active = profiles.get_active_profile(namespace)
    except Redacted as exc:
        raise PersonaUnavailable(
            PROFILE_CORRUPT, f"{prefix} (active_profile_deleted)"
        ) from exc
    except CorruptStore as exc:
        raise PersonaUnavailable(
            PROFILE_CORRUPT, f"{prefix} (active_profile_corrupt)"
        ) from exc
    if active.ref is None:
        raise PersonaUnavailable(PROFILE_MISSING, "the store has no active host profile")
    return _state_from_ref(
        store,
        namespace,
        active.ref,
        source=source,
        code=PROFILE_CORRUPT,
        prefix=prefix,
    )


def resolve_expected_persona(
    profiles: Any,
    store: Any,
    namespace: str,
    expectation: PersonaExpectation,
    *,
    source: str = "expected",
) -> PersonaState:
    """Resolve ``app.persona_expected`` or fail explicitly, naming the id."""

    try:
        active = profiles.get_active_profile(namespace)
    except Redacted as exc:
        raise _expected_unavailable(expectation, "active_profile_deleted") from exc
    except CorruptStore as exc:
        raise _expected_unavailable(expectation, "active_profile_corrupt") from exc
    if active.ref is None:
        raise _expected_unavailable(expectation, "not_active")
    state = _state_from_ref(
        store,
        namespace,
        active.ref,
        source=source,
        code=EXPECTED_PERSONA_UNAVAILABLE,
        prefix=f"the expected persona {expectation.label()} is unavailable",
        expectation=expectation,
    )
    if state.profile_id != expectation.profile_id:
        raise _expected_unavailable(
            expectation, f"active profile is {state.profile_id}"
        )
    if (
        expectation.profile_version is not None
        and state.profile_version != expectation.profile_version
    ):
        raise _expected_unavailable(
            expectation,
            f"active version is {state.profile_version}",
        )
    return state


def verify_activation_target(
    store: Any, namespace: str, record_id: str, record_version: int
) -> str:
    """Prove an activation target's approval before moving the pointer.

    ``PersonaProfileService.activate_profile`` deliberately does not re-check
    the review event (it was verified at publish time).  RG06 adds this
    pre-check so a deleted/untrusted approval cannot be activated later.
    """

    try:
        version = store.get_version(namespace, record_id, record_version)
    except Redacted as exc:
        raise PersonaExpectationConflict("the profile version has been deleted") from exc
    except RecordNotFound as exc:
        raise PersonaExpectationConflict("the profile version does not exist") from exc
    if version.kind != PERSONA_PROFILE_KIND:
        raise PersonaExpectationConflict("the target record is not a persona profile")
    body = dict(version.body)
    try:
        return verify_review_ref(
            store,
            namespace,
            body,
            code=PROFILE_CORRUPT,
            prefix="the profile version is not activatable",
        )
    except PersonaUnavailable as exc:
        raise PersonaExpectationConflict(exc.detail) from exc


# --------------------------------------------------------------- examples


def _scene_audience(scene: str) -> str:
    if scene.startswith("owner-"):
        return "owner"
    if scene.startswith("other-"):
        return "other"
    return ""


def _audience_ok(scene: str, speaker: Speaker | None) -> bool:
    """``owner-*`` demos only for the owner; ``other-*`` for everyone else.

    A missing speaker is treated exactly like an unknown one: the host must not
    assume the private owner, so no owner demo and no private term may be used.
    """

    audience = _scene_audience(scene)
    is_owner = speaker is not None and speaker.identity == "owner"
    if audience == "owner":
        return is_owner
    if audience == "other":
        return not is_owner
    return True


def _detected_cues(user_text: str) -> tuple[str, ...]:
    text = (user_text or "").lower()
    cues: list[str] = []
    for cue, needles in _CUE_RULES:
        if any(needle in text for needle in needles):
            cues.append(cue)
    return tuple(cues)


def _bigrams(text: str) -> set[str]:
    """Character bigrams used only to rank already-matched demos (no classifier)."""

    cleaned = "".join(char for char in text.lower() if not char.isspace())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[index : index + 2] for index in range(len(cleaned) - 1)}


def _overlap_score(user_text: str, example_user: str) -> int:
    """Deterministic lexical overlap between the input and one demo's user line.

    This is a tie-break *inside* scenes already matched by the fixed cue table,
    never a new scene decision.  It keeps a newer, more specific demo from being
    permanently crowded out by an older id when both carry the same scene.
    """

    return len(_bigrams(user_text or "") & _bigrams(example_user))


def _example_size(item: Mapping[str, Any]) -> int:
    """The UTF-8 size charged against the example byte budget."""

    messages = item.get("assistant_messages")
    if isinstance(messages, (list, tuple)):
        size = sum(len(str(message).encode("utf-8")) for message in messages)
    else:
        size = len(str(item.get("assistant", "")).encode("utf-8"))
    size += len(str(item.get("user", "")).encode("utf-8"))
    commentary = item.get("pre_tool_commentary")
    if isinstance(commentary, str):
        size += len(commentary.encode("utf-8"))
    return size


def select_examples(
    body: Mapping[str, Any],
    user_text: str,
    speaker: Speaker | None,
    max_items: int = DEFAULT_MAX_EXAMPLES,
    max_bytes: int = DEFAULT_MAX_EXAMPLE_BYTES,
    mode: str = "chat",
) -> tuple[list[str], list[dict[str, str]]]:
    """Pick at most ``max_items`` synthetic expression demos, deterministically.

    Selection is ``speaker.identity`` + fixed scene labels: ``owner-*`` scenes
    are only eligible for the owner, everything else only for a known-other or
    unknown speaker.  A scene demo must match a cue in the current message.

    ``owner-base`` is the one general demo: it is never matched by a cue and it
    is used **only in chat mode**.  Applicable scene demos always come first; a
    turn with no scene demo is topped up with at most two base demos, a turn
    with exactly one scene demo with at most one, and a turn with two or more
    scene demos gets no base demo.  The base demo demonstrates expression only;
    it never replaces a task demo.  A non-chat turn (``work``) never receives a
    base demo.

    Within each group, character-bigram overlap with the demo's user line (then
    the stable id) decides which demos share the count and byte budget; that
    tie-break is not an emotion or intent classifier.  There is no "owner means
    close" fallback: a scene demo with no cue is never selected.
    Returns ``(selected_ids, omitted)`` where every omitted entry is
    ``{"example_id": ..., "reason": ...}``.
    """

    raw = body.get("examples")
    examples = list(raw) if isinstance(raw, Sequence) and not isinstance(raw, str) else []
    omitted: list[dict[str, str]] = []
    cues = _detected_cues(user_text)
    wanted_scenes: tuple[str, ...] = tuple(
        dict.fromkeys(scene for cue in cues for scene in _CUE_SCENES.get(cue, ()))
    )
    base_enabled = mode == "chat"
    matched: list[tuple[int, str, Mapping[str, Any]]] = []
    base: list[tuple[int, str, Mapping[str, Any]]] = []
    for item in examples:
        if not isinstance(item, Mapping):
            continue
        example_id = item.get("example_id")
        scene = item.get("scene")
        if not isinstance(example_id, str) or not isinstance(scene, str):
            continue
        if item.get("synthetic") is not True:
            omitted.append({"example_id": example_id, "reason": "not_synthetic"})
            continue
        if not _audience_ok(scene, speaker):
            omitted.append({"example_id": example_id, "reason": "speaker_mismatch"})
            continue
        if scene == BASE_SCENE:
            if not base_enabled:
                # The generic demo is a chat-mode top-up only; it is never a
                # substitute for a work turn's task material.
                omitted.append(
                    {"example_id": example_id, "reason": "base_mode_disabled"}
                )
                continue
            base.append(
                (
                    _overlap_score(user_text, str(item.get("user", ""))),
                    example_id,
                    item,
                )
            )
            continue
        if scene not in wanted_scenes:
            omitted.append({"example_id": example_id, "reason": "not_matched"})
            continue
        score = _overlap_score(user_text, str(item.get("user", "")))
        matched.append((score, example_id, item))
    matched.sort(key=lambda entry: (-entry[0], entry[1]))
    selected: list[str] = []
    used_bytes = 0
    limit = max(0, int(max_items))
    byte_budget = max(0, int(max_bytes))

    def take(
        candidates: Sequence[tuple[int, str, Mapping[str, Any]]],
        max_take: int,
    ) -> None:
        nonlocal used_bytes
        taken = 0
        for _score, example_id, item in candidates:
            if len(selected) >= limit or taken >= max_take:
                omitted.append({"example_id": example_id, "reason": "budget"})
                continue
            size = _example_size(item)
            if used_bytes + size > byte_budget:
                omitted.append({"example_id": example_id, "reason": "budget"})
                continue
            used_bytes += size
            selected.append(example_id)
            taken += 1

    take(matched, limit)
    # Scene demos first: only a turn with fewer than two applicable scene demos
    # may be topped up (0 -> at most 2 base demos, 1 -> at most 1).  ``limit``
    # still caps the total, so the existing budget and count bounds hold.
    base_allowance = max(0, DEFAULT_MAX_EXAMPLES - len(matched))
    base.sort(key=lambda entry: (-entry[0], entry[1]))
    take(base, base_allowance)
    return selected, omitted


def render_example(body: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    """Host-generated provenance for one expression demo (never model text).

    ``pre_tool_commentary`` (when present) is rendered **before** ``assistant``
    so the model sees the order "short public line, then final reply".  The two
    are never concatenated, and neither is a tool result or a real memory.
    A multi-bubble demo carries ``assistant_messages`` instead of ``assistant``:
    the list is rendered as-is, in order, and is never joined into one string.
    """

    rendered: dict[str, Any] = {
        "example_id": str(item.get("example_id")),
        "scene": str(item.get("scene")),
        "basis": "expression_demo",
        "not_experience": True,
        "source": {
            "kind": "persona_profile",
            "profile_id": body.get("profile_id"),
            "profile_version": body.get("profile_version"),
            "asset_hash": body.get("asset_hash"),
        },
        "user": item.get("user"),
    }
    messages = item.get("assistant_messages")
    if isinstance(messages, Sequence) and not isinstance(messages, str):
        rendered["assistant_messages"] = [str(message) for message in messages]
        return rendered
    commentary = item.get("pre_tool_commentary")
    if isinstance(commentary, str) and commentary.strip():
        rendered["pre_tool_commentary"] = commentary
        # JSON object order is not semantic (and canonical JSON sorts keys), so
        # the spoken order is stated explicitly: short line, then final reply.
        rendered["order"] = ["pre_tool_commentary", "assistant"]
    rendered["assistant"] = item.get("assistant")
    return rendered


def build_persona_material(
    body: Mapping[str, Any], plan: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Turn the T0 persona snapshot into renderer input (or ``None``)."""

    if not plan:
        return None
    asset_hash = plan.get("asset_hash")
    if asset_hash and body.get("asset_hash") != asset_hash:
        raise CorruptStore(
            "the pinned persona asset hash does not match the stored profile version"
        )
    raw = body.get("examples")
    examples = list(raw) if isinstance(raw, Sequence) and not isinstance(raw, str) else []
    by_id = {
        item.get("example_id"): item
        for item in examples
        if isinstance(item, Mapping)
    }
    selected: list[dict[str, Any]] = []
    for example_id in plan.get("example_ids") or ():
        item = by_id.get(example_id)
        if item is None:
            raise CorruptStore(
                f"the pinned example {example_id!r} is missing from the profile version"
            )
        selected.append(render_example(body, item))
    omitted = [
        dict(entry)
        for entry in (plan.get("examples_omitted") or ())
        if isinstance(entry, Mapping)
    ]
    material: dict[str, Any] = {
        "asset_hash": body.get("asset_hash"),
        "examples": selected,
        "examples_omitted": omitted,
        "speaker": plan.get("speaker"),
    }
    return material


def persona_plan(
    *,
    state: PersonaState | None,
    body: Mapping[str, Any],
    user_text: str,
    speaker: Speaker | None,
    max_items: int,
    max_bytes: int,
    mode: str = "chat",
) -> dict[str, Any]:
    """Build the T0 snapshot stored in ``strategy_json["persona"]``.

    Only ids are snapshotted (not example text), so the turn snapshot stays
    small and the examples remain immutable through the pinned profile version.
    ``mode`` is the conversation's stored mode (``chat``/``work``); only a chat
    turn may receive the general ``owner-base`` demo (see ``select_examples``).
    """

    selected, omitted = select_examples(
        body,
        user_text,
        speaker,
        max_items=max_items,
        max_bytes=max_bytes,
        mode=mode,
    )
    return {
        "source": "active" if state is None else state.source,
        "profile_id": body.get("profile_id"),
        "profile_version": body.get("profile_version"),
        "asset_hash": body.get("asset_hash"),
        "speaker": None if speaker is None else speaker.to_dict(),
        "example_ids": selected,
        "examples_omitted": omitted,
    }


def with_source(state: PersonaState, source: str) -> PersonaState:
    return replace(state, source=source)


__all__ = [
    "BASE_SCENE",
    "DEFAULT_MAX_EXAMPLES",
    "DEFAULT_MAX_EXAMPLE_BYTES",
    "EXPECTED_PERSONA_UNAVAILABLE",
    "EXAMPLE_SCENES",
    "NEUTRAL_PROVISION_FAILED",
    "PROFILE_CORRUPT",
    "PROFILE_MISSING",
    "PersonaExpectation",
    "PersonaExpectationConflict",
    "PersonaState",
    "PersonaUnavailable",
    "SPEAKER_IDENTITIES",
    "Speaker",
    "build_persona_material",
    "parse_expectation",
    "parse_speaker",
    "persona_matches_expected",
    "persona_plan",
    "read_active_persona",
    "render_example",
    "resolve_expected_persona",
    "select_examples",
    "verify_activation_target",
    "verify_review_ref",
    "with_source",
]
