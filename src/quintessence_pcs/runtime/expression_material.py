"""RG05-F02 / J11: build the bounded ``expression`` Context material.

The document is a *view*: confirmed preferences are read from the current
``user_preference`` beliefs the host committed with an expression marker.
Nothing here is a second storage; no record is written.

J11 consolidation removed the conversation-scoped *tentative* preference from
this view: an ordinary remark ("感觉轻松一些") must not be turned into an
expression rule.  Adapting to the current turn is the model's job, using the
user's own words plus the persona style; only an explicit, reviewed, committed
preference is material here.

The material never carries a binding, a digest, a plan, a grant or a decision.
It carries, in order:

1. the current user input;
2. confirmed preferences (explicit lasting directives);
3. the persona's own style.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..domains.cognition.beliefs import (
    BELIEF_KIND,
    belief_evidence_is_live,
    parse_belief_body,
)
from ..domains.persona.expression_directives import (
    EXPRESSION_DIMENSIONS,
    KIND_EXPLICIT_CORRECTION,
    KIND_EXPLICIT_LONG_TERM,
    KIND_SINGLE_SHOT,
    DirectivePlan,
    canonical_value_label,
    parse_preference_marker,
)
from ..foundation.jsonutil import canonical_json_text
from .context import ConsultQueryMaterial, ReadSetEntry

EXPRESSION_MATERIAL_VERSION = "expression.material.v2"
MAX_CONFIRMED = 3
MAX_MATERIAL_BYTES = 4096
MAX_CLAIM_CHARS = 240
MAX_QUOTE_CHARS = 200

EXPRESSION_POLICY = (
    "这些是用户的表达偏好，只影响回答的长度、格式和语气，不改变工具、权限、"
    "隐私或任何外部行动。confirmed 来自用户明确的长期指令，是回答方式的有效"
    "依据。当前用户消息优先于它；它不改变人格核心、事实和承诺。"
    "日常一句话里的语气要求由当前消息本身表达，不需要、也不应当被记成长期偏好。"
)


def build_expression_material(
    store: Any,
    *,
    namespace: str,
    conversation_id: str,
    privacy_classes: frozenset[str],
    plan: DirectivePlan | None = None,
    turn_input_ref: Mapping[str, Any] | None = None,
) -> ConsultQueryMaterial | None:
    """Return the material, or ``None`` when there is nothing to render."""

    confirmed, confirmed_entries = _confirmed_items(
        store, namespace=namespace, privacy_classes=privacy_classes
    )
    directive = _directive_item(plan)
    if not confirmed and directive is None:
        return None

    omitted: list[dict[str, Any]] = []
    while len(confirmed) > MAX_CONFIRMED:
        dropped = confirmed.pop()
        omitted.append(
            {"kind": "confirmed", "dimension": dropped.get("dimension"), "reason": "budget"}
        )

    def document() -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": EXPRESSION_MATERIAL_VERSION,
            "policy": EXPRESSION_POLICY,
            "precedence": [
                "current_user_input",
                "confirmed_preference",
                "persona",
            ],
            "confirmed": list(confirmed),
        }
        if directive is not None:
            payload["directive"] = directive
        if omitted:
            payload["omitted"] = list(omitted)
        return payload

    body = canonical_json_text(document())
    while confirmed and len(body.encode("utf-8")) > MAX_MATERIAL_BYTES:
        dropped = confirmed.pop()
        omitted.append(
            {"kind": "confirmed", "dimension": dropped.get("dimension"), "reason": "budget"}
        )
        body = canonical_json_text(document())
    if len(body.encode("utf-8")) > MAX_MATERIAL_BYTES:
        return None

    read_set: list[ReadSetEntry] = []
    seen: set[tuple[str, int]] = set()
    for ref in confirmed_entries:
        key = (ref.record_id, ref.record_version)
        if key in seen:
            continue
        seen.add(key)
        read_set.append(ReadSetEntry(ref=ref, usage="current"))
    return ConsultQueryMaterial(
        body=body,
        state_revision=store.current_state_revision(namespace),
        read_set=tuple(read_set),
        event_sources=(),
    )


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


def _confirmed_items(
    store: Any, *, namespace: str, privacy_classes: frozenset[str]
) -> tuple[list[dict[str, Any]], list[Any]]:
    from ..domains.refs import RecordRef

    items: list[tuple[str, dict[str, Any], Any]] = []
    for version in store.current_versions_of_kind(namespace, BELIEF_KIND):
        if version.privacy_class not in privacy_classes:
            continue
        if version.status != "supported":
            continue
        try:
            body = parse_belief_body(version)
        except Exception:  # noqa: BLE001 - a malformed belief is not material
            continue
        if body.get("claim_type") != "user_preference":
            continue
        marker = parse_preference_marker(body.get("review_condition"))
        if marker is None:
            continue
        if not belief_evidence_is_live(store, namespace=namespace, body=body):
            continue
        dimension, value = marker
        claim = body.get("claim")
        items.append(
            (
                str(version.recorded_at),
                {
                    "dimension": dimension,
                    "value": value,
                    "claim": (claim[:MAX_CLAIM_CHARS] if isinstance(claim, str) else ""),
                    "scope": dict(version.scope or {}),
                    "ref": RecordRef(
                        namespace=version.namespace,
                        record_id=version.record_id,
                        record_version=version.record_version,
                    ).to_dict(),
                    "source": "user_statement",
                },
                RecordRef(
                    namespace=version.namespace,
                    record_id=version.record_id,
                    record_version=version.record_version,
                ),
            )
        )
    items.sort(key=lambda item: item[0], reverse=True)
    items.sort(key=lambda item: EXPRESSION_DIMENSIONS.index(item[1]["dimension"]))
    return [item[1] for item in items], [item[2] for item in items]


def _directive_item(plan: DirectivePlan | None) -> dict[str, Any] | None:
    """A host instruction to use the governed proposal path, not a memory claim.

    The model can only author a candidate; the host owns the binding.  This
    instruction tells the model *that* an explicit preference exists and that it
    must call the existing tool, without exposing a binding, digest or grant.
    """

    if plan is None or plan.dimension is None:
        return None
    if plan.kind == KIND_EXPLICIT_CORRECTION and plan.value is None:
        return {
            "kind": "explicit_correction",
            "dimension": plan.dimension,
            "value": None,
            "instruction": (
                "用户本轮明确撤回旧表达偏好。请调用 cognition.propose 提交 "
                "retract_belief（目标由宿主核对）；宿主会撤回旧值。不要只口头答应，"
                "也不要把撤回降格为暂定偏好。"
            ),
        }
    if plan.value is None:
        return None
    if plan.kind == KIND_EXPLICIT_LONG_TERM:
        label = canonical_value_label(plan.dimension, plan.value)
        return {
            "kind": "explicit_long_term",
            "dimension": plan.dimension,
            "value": plan.value,
            "instruction": (
                "用户本轮明确提出长期表达偏好（"
                f"{plan.dimension}={plan.value}，{label}）。请调用 cognition.propose "
                "提交 claim_type=user_preference，claim 中明确写出该维度和值；"
                "宿主会绑定本轮用户输入并自动提交。不要只口头答应，也不要声称已记住。"
            ),
        }
    if plan.kind == KIND_EXPLICIT_CORRECTION:
        return {
            "kind": "explicit_correction",
            "dimension": plan.dimension,
            "value": plan.value,
            "instruction": (
                "用户本轮明确纠正旧表达偏好。请调用 cognition.propose 提交新的 "
                "user_preference（claim 中明确写出该维度和值）；宿主会撤回/替换旧值。"
                "不要只口头答应，也不要把纠正降格为暂定偏好。"
            ),
        }
    return None


__all__ = [
    "EXPRESSION_MATERIAL_VERSION",
    "EXPRESSION_POLICY",
    "MAX_MATERIAL_BYTES",
    "build_expression_material",
]
