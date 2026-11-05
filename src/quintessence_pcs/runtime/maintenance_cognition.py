"""RG08 R-E: bounded cognition review inside one maintenance slice.

The worker is a host adapter, not a second cognition authority:

* it re-verifies the material and builds a maintenance consult Context;
* it asks the configured legacy ``ModelPort`` for one strict proposal
  (``propose_belief`` / ``revise_belief`` / ``wait``), with at most one format
  repair;
* it bounds the claim type/strength/evidence before any candidate exists;
* it registers through the existing ``propose_from_material`` helper and runs
  the existing Review policy + Tx B commit through ``settle_candidate``.

Nothing here commits directly.  A stale source, an unavailable model, a
malformed proposal or an E2 decision is recorded as an honest item outcome; the
supervisor (``run_slice``) decides whether the job can succeed or must pause.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import nullcontext
from typing import Any, Callable, Mapping, Sequence

from ..collaboration.change_service import (
    ChangeAccess,
    CognitionChangeService,
    settle_candidate,
)
from ..domains.cognition.beliefs import parse_belief_body
from ..domains.cognition.epistemic import EpistemicAccess
from ..domains.cognition.errors import EvidenceMissing, UnsupportedOperation
from ..domains.cognition.maintenance_review import propose_from_material
from ..domains.cognition.working_set import build_working_set
from ..domains.memory.maintenance_material import TurnMaterial
from ..domains.memory.maintenance_source import SourceMaterial
from ..domains.refs import EventRef, RecordRef
from ..foundation.errors import (
    ContextStale,
    CorruptStore,
    DbBusy,
    InvalidInput,
    ScopeViolation,
)
from ..foundation.jsonutil import strict_json_loads
from ..store.governance import GovernanceStore
from ..store.maintenance import (
    LocalTimeout,
    MaintenanceStore,
    OperationRecovery,
    RateLimited,
    TaskCancelled,
    reconcile_operation,
)
from ..store.tx_b import CommitReceipt
from .context import (
    PROPOSAL_CONTRACT_COMPACT,
    ContextService,
    cognition_material_from_working_set,
)
from .model_port import (
    ModelAdapterError,
    ModelCancelled,
    ModelTransportError,
)
from .proposal_generation import parse_proposal, repair_feedback, validate_proposal
from .revision_contract import (
    MAINTENANCE_BUNDLE_NOTE,
    MAINTENANCE_REVISION_RULE,
    SURFACE_MAINTENANCE,
    check_revision_target,
)

#: The only claim type maintenance may submit in R-E.
ALLOWED_CLAIM_TYPES = ("general_hypothesis",)

#: Strength ceiling: one material source can justify at most "limited"; claiming
#: more would be graded E2 by the existing impact policy anyway.
ALLOWED_STRENGTHS = ("insufficient", "limited")

#: Stable detail codes never carry model text or a source body.
MAX_DETAIL = 1024

#: Q3: the slice deadline is cooperative but checked before/after every model
#: call, not only at item boundaries.  The transport cap must match this value.
MAINTENANCE_MODEL_CALL_CAP = 7.0
MAINTENANCE_MODEL_CALL_RESERVE = 1.0

#: The maintenance-only model envelope around existing change operations.
_BUNDLE_KEYS = frozenset({"operation", "summary", "changes"})
_MAX_BUNDLE_CHANGES = 2

#: ``premise_refs`` is one of the ten required Belief body fields, but this
#: maintenance review accepts no premise beliefs: an EventRef is never a
#: premise, and material events are cited through ``evidence_links[].ref``.
#: The R-G3B real response put the material EventRef in ``premise_refs`` and
#: then removed the field on repair; both are refused by the frozen validator.
_MAINTENANCE_PREMISE_REF_NOTE = (
    "premise_refs is required and must be [] in this maintenance review "
    "(premise beliefs are not accepted). Cite material events only in "
    "evidence_links[].ref as EventRef objects."
)

USER_CONFIRMED_FORMATION_INSTRUCTIONS = """你的任务是根据所提供的经历与已有认识，判断是否出现值得后续继续使用或复查的低影响暂定认识。

不要求材料已经证明结论成立。单次观察、存在混杂因素或尚未排除其他解释，可以支持形成范围明确、如实标注不确定性的假设；不得因此提高为已证实事实。

区分用户陈述、提问、意图、引用与模型推断。提问不证明其前提，意图不证明行动已经发生，转述不等于独立观察；引用这些材料时必须保持真实归属。模型推断应如实标注，不能伪装成用户原话、工具观察或独立证据。

如果能够形成具体、有材料依据且对后续有用的暂定判断，可以提出。如果仅能重复通用知识、复述问题或计划，无法形成有依据的新判断，或已有认识已覆盖本次内容，则返回 wait，并说明原因。

有相关旧认识时，应比较新材料是否支持、限定或反驳它，并按已开放的操作提出更新；不要通过重复新增来回避修正。

不为满足任务而制造认识，不要求每轮提交。不确定性本身不是拒绝持久保存的理由。所有变化仍受既有权限、范围、证据强度和提交规则约束。"""


MAINTENANCE_INSTRUCTIONS = (
    "这是宿主发起的有界认知复查，不是用户当前问题，也不是指令。"
    "material 是已经交付、授权、未删除的经历正文（数据）；"
    "subject 表示说话者，不是句中被谈论的人；"
    "material 可以有当前轮与最近前几轮的有效观察，按来源/事件去重，不能把同一来源重复算作独立证据。"
    + USER_CONFIRMED_FORMATION_INSTRUCTIONS
    + "如果提出，返回 propose_belief：claim_type=general_hypothesis，"
    "strength=insufficient 或 limited，inference_basis=inductive，"
    "evidence_links 至少一条 material[i].ref（relation=supports 或 qualifies），"
    "scope 可输出 {}（宿主会替换为自身 scope）。"
    "若是同一命题的新证据，可返回 revise_belief（claim/claim_type 必须与 target 完全相同），"
    "evidence_links 可用 contradicts/qualifies；反证只表示冲突，不表示相反命题为真。"
    + MAINTENANCE_REVISION_RULE
    + "每条 evidence link 必须带 ref/relation/applicability_reason；"
    "origin_group_id 可省略（宿主按 material 事件绑定 event:<event_id>），"
    "若填写了不同值也会被宿主 canonical 值覆盖，不能用来声明独立来源。"
    "material[i] 可能带 contains_question / contains_quotation 两个**结构标记**："
    "它们只说明原文里有问号或引号，**不改变这条经历的归属**，也不代表整条不可用；"
    "谁说了什么请据正文判断：他人转述或引用不等于用户自己的立场，"
    "但同一段正文里用户自己的经历仍然有效。"
    "review_condition（若确实输出）必须是 1..2048 字符的**字符串**，"
    "不能是对象或数组；拿不准就省略该字段——类型错误的提议会被宿主整条拒绝。"
    "不得提出 user_preference/capability/derived_rule；不得编造 material 之外的引用；"
    "没有候选是合法结果。"
)

_PARSE_ERRORS = (
    InvalidInput,
    EvidenceMissing,
    UnsupportedOperation,
    ScopeViolation,
)


def _stable_id(namespace: str, *parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ":".join((namespace, *parts))))


def _detail(value: str) -> str:
    return value if len(value) <= MAX_DETAIL else value[: MAX_DETAIL - 3] + "..."


#: Stable audit token: the model declared an ``origin_group_id`` that differed
#: from the canonical value the host derives, so the host bound its own value.
#: The token is recorded in the item detail (never the model's text) so the
#: override is visible instead of silent.
GROUP_REBOUND = "group_rebound"


def _audited(detail: str, rebound: bool) -> str:
    """Append the bounded audit token without letting truncation eat it."""

    if not rebound:
        return detail
    suffix = f":{GROUP_REBOUND}"
    keep = MAX_DETAIL - len(suffix)
    base = detail if len(detail) <= keep else detail[: keep - 3] + "..."
    return base + suffix


def _operation_item(
    *,
    source: SourceMaterial,
    operation_key: str,
    request_id: str,
    outcome: str,
    detail: str | None = None,
    candidate_id: str | None = None,
    decision_id: str | None = None,
    receipt_ref: str | None = None,
) -> dict[str, Any]:
    return {
        "target_kind": "event",
        "target_id": source.event_id,
        "target_version": 0,
        "operation_key": operation_key,
        "request_id": request_id,
        "candidate_id": candidate_id,
        "decision_id": decision_id,
        "receipt_ref": receipt_ref,
        "outcome": outcome,
        "detail": _detail(detail or outcome),
    }


def _decision_outcome(decision: Any) -> str:
    value = str(getattr(decision, "decision", "") or "")
    if value == "committed":
        return "committed"
    if value == "rejected":
        return "rejected"
    if value == "deferred":
        return "deferred"
    if value in ("awaiting_human", "escalated"):
        return "awaiting_human"
    return "awaiting_human"


def _receipt_ref(receipt: CommitReceipt | None) -> str | None:
    return None if receipt is None else f"receipt:{receipt.request_id}"


def _public_result(result: Any) -> tuple[str, str | None, str | None]:
    """Map one settled ``ChangeResult`` to (outcome, candidate_id, detail)."""

    state = str(getattr(result, "state", "") or "")
    candidate_id = getattr(result, "candidate_id", None)
    reason = getattr(result, "reason_code", None) or state.lower() or "unknown"
    if state == "COMMITTED":
        return "committed", candidate_id, "committed"
    if state == "AWAITING_HUMAN":
        return "awaiting_human", candidate_id, f"awaiting_human:{reason}"
    if state == "NOT_AUTHORIZED":
        return "awaiting_human", candidate_id, "not_authorized"
    if state == "REJECTED":
        return "rejected", candidate_id, f"rejected:{reason}"
    if state == "DEFERRED":
        return "deferred", candidate_id, f"deferred:{reason}"
    if state == "CONFLICT":
        return "failed", candidate_id, f"conflict:{reason}"
    return "failed", candidate_id, f"settle:{reason}"


def _primary_material(material: TurnMaterial, index: int) -> SourceMaterial:
    sources = material.primary_usable_sources
    if index >= len(sources):
        raise IndexError("maintenance primary page is shorter than its item index")
    return sources[index]


def _maintenance_task(
    *,
    task_id: str,
    trigger_key: str,
    primary_event_id: str,
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": "maintenance.review.v1",
        "task_id": task_id,
        "trigger_key": trigger_key,
        "primary_event_id": primary_event_id,
        "mode": "cognition.review",
        "instructions": MAINTENANCE_INSTRUCTIONS,
        "proposal_contract": {
            **PROPOSAL_CONTRACT_COMPACT,
            "body_field_notes": {
                "premise_refs": _MAINTENANCE_PREMISE_REF_NOTE,
            },
            "maintenance_bundle": {
                "operation": "change_bundle",
                "summary": "string, 1..512 chars",
                "changes": [
                    "retract_belief",
                    "propose_belief",
                    "revise_belief",
                ],
                "max_changes": 2,
                "host_binds": ["changes[].evidence_links[].origin_group_id"],
                "note": MAINTENANCE_BUNDLE_NOTE,
            },
        },
        "allowed_claim_types": list(ALLOWED_CLAIM_TYPES),
        "strength_ceiling": "limited",
        "scope": dict(scope),
        "bounds": {
            "max_changes": 2,
            "max_model_calls": 2,
            "no_candidate_allowed": True,
        },
    }


def _material_links(
    value: Any,
    *,
    namespace: str,
    materials: Mapping[str, SourceMaterial],
) -> tuple[list[dict[str, Any]] | None, str | None, bool, bool, bool, bool]:
    """Return copied links, relation flags and rebound.

    Every link must name an event in this Context's bounded material.  The
    canonical ``origin_group_id`` is bound per event (R-G1); the model never
    supplies independence.  A repeated event stays one origin group.
    """

    if not isinstance(value, list) or not value:
        return None, "no_material_evidence", False, False, False, False
    links: list[dict[str, Any]] = []
    support = qualify = contradict = rebound = False
    for link in value:
        if not isinstance(link, Mapping):
            return None, "evidence_not_object", False, False, False, False
        ref = link.get("ref")
        if not isinstance(ref, Mapping) or ref.get("ref_type") != "event":
            return None, "evidence_not_event", False, False, False, False
        event_id = ref.get("event_id")
        if not isinstance(event_id, str) or event_id not in materials:
            return None, "evidence_not_material", False, False, False, False
        if ref.get("namespace") != namespace:
            return None, "evidence_other_namespace", False, False, False, False
        relation = link.get("relation")
        if relation not in ("supports", "qualifies", "contradicts"):
            return None, "evidence_relation_not_allowed", False, False, False, False
        canonical = EventRef(namespace=namespace, event_id=event_id).origin_group_id
        declared = link.get("origin_group_id")
        if declared is not None and declared != "" and declared != canonical:
            rebound = True
        support = support or relation == "supports"
        qualify = qualify or relation == "qualifies"
        contradict = contradict or relation == "contradicts"
        links.append({**dict(link), "origin_group_id": canonical})
    return links, None, support, qualify, contradict, rebound


#: The frozen evidence validator reports its whole required key set in one
#: message; the repair hint uses this exact prefix to recognise that refusal.
_EVIDENCE_KEY_MESSAGE = (
    "must have exactly ref, relation, origin_group_id and applicability_reason"
)

#: Keys the frozen Belief body validator requires on every evidence link.
_EVIDENCE_LINK_KEYS = (
    "ref",
    "relation",
    "origin_group_id",
    "applicability_reason",
)


def _proposal_evidence_links(proposal: Any) -> list[Any] | None:
    """Return the model links a standalone change operation carries."""

    if not isinstance(proposal, Mapping):
        return None
    operation = proposal.get("operation")
    if operation in ("propose_belief", "revise_belief"):
        body = proposal.get("body")
        links = body.get("evidence_links") if isinstance(body, Mapping) else None
    elif operation == "retract_belief":
        links = proposal.get("evidence_links")
    else:
        return None
    return links if isinstance(links, list) else None


def _prefill_origin_groups(namespace: str, proposal: Any) -> int:
    """Bind the canonical ``origin_group_id`` where the model omitted it.

    ``MAINTENANCE_INSTRUCTIONS`` says the field may be omitted because the host
    binds ``event:<event_id>``, and ``_material_links`` already canonicalises
    it.  The frozen ``validate_belief_body`` nevertheless requires the key to be
    present, so validating the raw proposal first rejected a contract-conforming
    maintenance output before the host ever saw it.

    Only an absent key is filled: an explicit value, even a wrong one, is left
    untouched so ``_host_bundle`` performs the normal rebind and reports the
    ``group_rebound`` audit token.  A link whose ``ref`` is not a material
    EventRef is not filled either; the frozen validator and ``_host_bundle``
    still refuse it.

    Returns the number of links that were filled (unused by the caller).
    """

    links = _proposal_evidence_links(proposal)
    if links is None:
        return 0
    filled = 0
    for link in links:
        if not isinstance(link, dict) or "origin_group_id" in link:
            continue
        ref = link.get("ref")
        if not isinstance(ref, Mapping) or ref.get("ref_type") != "event":
            continue
        event_id = ref.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            continue
        link["origin_group_id"] = EventRef(
            namespace=namespace, event_id=event_id
        ).origin_group_id
        filled += 1
    return filled


def _validate_maintenance_proposal(
    namespace: str, proposal: Mapping[str, Any]
) -> None:
    """Validate one parsed maintenance proposal after host pre-binding.

    ``change_bundle`` is the maintenance-only envelope and has no frozen
    ``validate_proposal`` shape; ``_host_bundle`` normalises it alone.
    """

    if proposal.get("operation") in ("wait", "change_bundle"):
        return
    _prefill_origin_groups(namespace, proposal)
    validate_proposal(namespace, proposal)


def _evidence_link_note(proposal: Mapping[str, Any] | None) -> str | None:
    """Name the evidence-link keys still missing from a refused proposal.

    The frozen validator reports its entire required set in one message, so the
    repair hint has to point at the actual gap.  A link that still lacks
    ``origin_group_id`` here had no EventRef the host could bind, which is the
    distinction the model needs.
    """

    links = _proposal_evidence_links(proposal)
    if links is None:
        return None
    missing: list[str] = []
    for link in links:
        if not isinstance(link, Mapping):
            continue
        for key in _EVIDENCE_LINK_KEYS:
            if key not in link and key not in missing:
                missing.append(key)
    if not missing:
        return None
    return (
        f"Evidence link field(s) missing: {', '.join(missing)}. Each item "
        "needs a material EventRef in ref, plus relation and "
        "applicability_reason; origin_group_id may be omitted in this "
        "maintenance review because the host binds event:<event_id>."
    )


def _maintenance_repair_feedback(
    error: BaseException, proposal: Mapping[str, Any] | None
) -> str:
    """Add the maintenance-only hints the shared repair text cannot give."""

    feedback = repair_feedback(error)
    message = str(error)
    if "premise_refs" in message:
        return f"{feedback} {_MAINTENANCE_PREMISE_REF_NOTE}"
    if _EVIDENCE_KEY_MESSAGE in message:
        note = _evidence_link_note(proposal)
        if note is not None:
            return f"{feedback} {note}"
    return feedback


def _target(
    store: Any,
    *,
    namespace: str,
    value: Any,
    enforce_claim_type: bool,
) -> tuple[RecordRef | None, dict[str, Any] | None, str | None]:
    if not isinstance(value, Mapping):
        return None, None, "target_not_object"
    try:
        ref = RecordRef.from_value(value, field="target")
    except Exception:  # noqa: BLE001 - a malformed target is a refusal
        return None, None, "target_invalid"
    if ref.namespace != namespace:
        return None, None, "target_other_namespace"
    try:
        version = store.get_version(namespace, ref.record_id, ref.record_version)
        current_record = store.get_current(namespace, ref.record_id)
        current = parse_belief_body(version)
    except Exception:  # noqa: BLE001 - unknown/moved target is a refusal
        return None, None, "target_unavailable"
    if current_record.record_version != ref.record_version:
        return None, None, "target_moved"
    if enforce_claim_type and current.get("claim_type") not in ALLOWED_CLAIM_TYPES:
        return None, None, "target_claim_type_not_allowed"
    if current.get("claim_type") not in ALLOWED_CLAIM_TYPES:
        return None, None, "target_claim_type_not_allowed"
    if version.status in ("retracted", "contradicted"):
        return None, None, "target_terminal"
    return ref, current, None


def _host_change(
    store: Any,
    *,
    namespace: str,
    operation: str,
    raw: Mapping[str, Any],
    scope: Mapping[str, Any],
    materials: Mapping[str, SourceMaterial],
    index: int = 0,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Host-normalise one existing change operation; never trust model scope.

    Returns ``(change, None, rebound)`` or ``(None, reason, rebound)``.  The
    third value reports that a model-supplied ``origin_group_id`` differed from
    the canonical value the host bound, so the caller can record the override
    instead of losing it.
    """

    if operation in ("propose_belief", "revise_belief"):
        body = raw.get("body")
        if not isinstance(body, Mapping):
            return None, "body_not_object", False
        if body.get("claim_type") not in ALLOWED_CLAIM_TYPES:
            return None, "claim_type_not_allowed", False
        if body.get("strength") not in ALLOWED_STRENGTHS:
            return None, "strength_not_allowed", False
        if body.get("premise_refs"):
            return None, "premises_not_allowed", False
        links, reason, support, qualify, contradict, rebound = _material_links(
            body.get("evidence_links"),
            namespace=namespace,
            materials=materials,
        )
        if links is None:
            return None, reason, False
        if operation == "propose_belief" and not (support or qualify):
            return None, "no_supporting_evidence", rebound
        if operation == "revise_belief" and not (support or qualify or contradict):
            return None, "no_supporting_evidence", rebound
        body = {**dict(body), "evidence_links": links}
        if operation == "propose_belief":
            return {
                "op": "propose_belief",
                "scope": dict(scope),
                "body": body,
            }, None, rebound
        ref, _current, reason = _target(
            store,
            namespace=namespace,
            value=raw.get("target"),
            enforce_claim_type=True,
        )
        if ref is None:
            return None, reason, rebound
        # Same shared pre-flight the chat handler calls: a substantive claim
        # change never becomes a candidate here either.
        refusal = check_revision_target(
            store,
            namespace,
            {"op": "revise_belief", "target": ref.to_dict(), "body": body},
            index=index,
            surface=SURFACE_MAINTENANCE,
        )
        if refusal is not None:
            return None, refusal.code, rebound
        return {
            "op": "revise_belief",
            "target": ref.to_dict(),
            "scope": dict(scope),
            "body": body,
        }, None, rebound

    if operation == "retract_belief":
        ref, _current, reason = _target(
            store,
            namespace=namespace,
            value=raw.get("target"),
            enforce_claim_type=True,
        )
        if ref is None:
            return None, reason, False
        links, reason, _support, qualify, contradict, rebound = _material_links(
            raw.get("evidence_links"),
            namespace=namespace,
            materials=materials,
        )
        if links is None:
            return None, reason, False
        if not (qualify or contradict):
            return None, "no_retraction_evidence", rebound
        retract_reason = raw.get("reason")
        if (
            not isinstance(retract_reason, str)
            or not retract_reason.strip()
            or len(retract_reason) > 512
        ):
            return None, "reason_invalid", rebound
        return {
            "op": "retract_belief",
            "target": ref.to_dict(),
            "evidence_links": links,
            "reason": retract_reason.strip(),
        }, None, rebound

    return None, f"operation_not_allowed:{operation}", False


def _parse_maintenance_bundle(raw: Any) -> dict[str, Any] | None:
    """Parse the maintenance-only ``change_bundle`` envelope, else return None.

    This is intentionally private to the maintenance adapter.  The ordinary
    chat/legacy proposal parser keeps accepting exactly its existing three
    shapes, so a bundle can never leak into the chat cognition path.
    """

    if not isinstance(raw, str):
        return None
    try:
        value = strict_json_loads(raw)
    except ValueError as error:
        raise InvalidInput(f"model output is not strict JSON: {error}") from error
    if not isinstance(value, dict) or value.get("operation") != "change_bundle":
        return None
    if set(value) != _BUNDLE_KEYS:
        raise InvalidInput(
            "change_bundle must have exactly operation, summary and changes"
        )
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 512:
        raise InvalidInput("change_bundle summary must be 1..512 characters")
    changes = value.get("changes")
    if (
        not isinstance(changes, list)
        or not 1 <= len(changes) <= _MAX_BUNDLE_CHANGES
    ):
        raise InvalidInput("change_bundle must contain 1..2 changes")
    for change in changes:
        if not isinstance(change, Mapping):
            raise InvalidInput("each change must be a JSON object")
        if not isinstance(change.get("op"), str):
            raise InvalidInput("each change must have a string op")
    return {
        "operation": "change_bundle",
        "summary": summary.strip(),
        "changes": [dict(change) for change in changes],
    }


def _host_bundle(
    store: Any,
    *,
    namespace: str,
    proposal: Mapping[str, Any],
    scope: Mapping[str, Any],
    materials: Sequence[SourceMaterial],
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Validate the model proposal against R-E's narrow host policy.

    Returns ``(bundle, None, rebound)`` or ``(None, stable_reason, rebound)``.
    The model's scope is never trusted; a ``wait`` is a legitimate no-change and
    returns ``None``.  ``rebound`` reports that a model-declared
    ``origin_group_id`` was replaced by the host's canonical value.
    """

    allowed = {item.event_id: item for item in materials if item.usable}
    if not allowed:
        return None, "no_material_evidence", False
    operation = proposal.get("operation")
    if operation == "wait":
        return None, "model_wait", False
    if operation == "change_bundle":
        summary = proposal.get("summary")
        raw_changes = proposal.get("changes")
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or len(summary) > 512
        ):
            return None, "bundle_summary_invalid", False
        if (
            not isinstance(raw_changes, list)
            or not 1 <= len(raw_changes) <= _MAX_BUNDLE_CHANGES
        ):
            return None, "bundle_size_invalid", False
        if len(raw_changes) == 2:
            ops = [item.get("op") for item in raw_changes if isinstance(item, Mapping)]
            if sorted(ops) != ["propose_belief", "retract_belief"]:
                return None, "bundle_shape_not_allowed", False
        changes: list[dict[str, Any]] = []
        rebound_any = False
        for index, raw in enumerate(raw_changes):
            if not isinstance(raw, Mapping):
                return None, "change_not_object", rebound_any
            op = raw.get("op")
            if not isinstance(op, str):
                return None, "change_op_missing", rebound_any
            normalized, reason, rebound = _host_change(
                store,
                namespace=namespace,
                operation=op,
                raw=raw,
                scope=scope,
                materials=allowed,
                index=index,
            )
            rebound_any = rebound_any or rebound
            if normalized is None:
                return None, reason, rebound_any
            changes.append(normalized)
        targets = [
            change["target"]["record_id"]
            for change in changes
            if "target" in change
        ]
        if len(targets) != len(set(targets)):
            return None, "duplicate_target", rebound_any
        return {"summary": summary.strip(), "changes": changes}, None, rebound_any

    if operation not in ("propose_belief", "revise_belief", "retract_belief"):
        return None, f"operation_not_allowed:{operation}", False
    normalized, reason, rebound = _host_change(
        store,
        namespace=namespace,
        operation=operation,
        raw=proposal,
        scope=scope,
        materials=allowed,
        index=0,
    )
    if normalized is None:
        return None, reason, rebound
    return {
        "summary": "bounded maintenance review",
        "changes": [normalized],
    }, None, rebound


class _SliceDeadline(RuntimeError):
    """Cooperative deadline reached before/after a model call."""

    def __init__(self, calls: int) -> None:
        super().__init__("maintenance slice deadline reached")
        self.calls = calls


class MaintenanceCognitionWorker:
    """One callable the supervisor can run once per bounded slice."""

    def __init__(
        self,
        *,
        store: Any,
        maintenance: MaintenanceStore,
        changes: CognitionChangeService,
        access: ChangeAccess,
        namespace: str,
        task_id: str,
        trigger_key: str,
        material: TurnMaterial,
        context_service: ContextService,
        model: Any,
        auto_authorization_id: str | None,
        scope: Mapping[str, Any],
        query_plan: Any,
        epistemic_access: EpistemicAccess,
        experience_access: Any,
        timezone: str = "Asia/Shanghai",
        max_events: int = 2,
        should_yield: Callable[[], bool] | None = None,
        cancel_event: Any | None = None,
        allow_repair: bool = True,
        clock: Callable[[], float] = time.monotonic,
        model_call_cap: float = MAINTENANCE_MODEL_CALL_CAP,
        model_call_reserve: float = MAINTENANCE_MODEL_CALL_RESERVE,
    ) -> None:
        self._store = store
        self._maintenance = maintenance
        self._changes = changes
        self._access = access
        self._namespace = namespace
        self._task_id = task_id
        self._trigger_key = trigger_key
        self._material = material
        self._contexts = context_service
        self._model = model
        self._auto_authorization_id = auto_authorization_id
        self._scope = dict(scope)
        self._query_plan = query_plan
        self._epistemic_access = epistemic_access
        self._experience_access = experience_access
        self._timezone = timezone
        self._max_events = max(1, min(int(max_events), 20))
        self._should_yield = should_yield or (lambda: False)
        self._cancel_event = cancel_event
        self._allow_repair = bool(allow_repair)
        self._clock = clock
        self._model_call_cap = max(0.1, float(model_call_cap))
        self._model_call_reserve = max(0.0, float(model_call_reserve))

    def _check_cancel(self) -> None:
        event = getattr(self, "_cancel_event", None)
        if event is not None and event.is_set():
            raise TaskCancelled("foreground_cancel")

    # ------------------------------------------------------------- helpers

    def _working_material(self) -> tuple[Any, tuple[dict[str, Any], ...]]:
        try:
            working = build_working_set(
                self._store,
                namespace=self._namespace,
                epistemic_access=self._epistemic_access,
                query_plan=self._query_plan,
                experience_access=self._experience_access,
                belief_limit=5,
                pending_limit=3,
            )
        except (CorruptStore, DbBusy):
            raise
        except Exception:  # noqa: BLE001 - missing working set is not a failure
            return None, ()
        if working is None:
            return None, ()
        return cognition_material_from_working_set(working), working.collab_refs

    def _build_context(self, primary: SourceMaterial):
        cognition_material, collab_refs = self._working_material()
        return self._contexts.build_consult_context(
            namespace=self._namespace,
            access=self._experience_access,
            input_refs=(),
            adapter_kind=self._model.adapter_kind,
            cognition_material=cognition_material,
            cognition_collab_refs=collab_refs,
            memory_recall=self._material.material,
            maintenance_task=_maintenance_task(
                task_id=self._task_id,
                trigger_key=self._trigger_key,
                primary_event_id=primary.event_id,
                scope=self._scope,
            ),
        )

    def _generate(
        self, context: Any, *, deadline: float
    ) -> tuple[dict[str, Any], int]:
        """One proposal plus at most one format repair; returns calls used."""

        calls = 0

        def call(feedback: str | None) -> str:
            nonlocal calls
            self._check_cancel()
            now = self._clock()
            if now + self._model_call_cap + self._model_call_reserve > deadline:
                raise _SliceDeadline(calls)
            binder = getattr(self._model, "bind_cancel", None)
            cancel_event = getattr(self, "_cancel_event", None)
            cancel_scope = (
                binder(cancel_event)
                if binder is not None and cancel_event is not None
                else nullcontext()
            )
            try:
                with cancel_scope:
                    with self._model.bind_run(
                        namespace=self._namespace, context=context
                    ):
                        raw = self._model.propose(
                            rendered_input=context.rendered_input,
                            references=(),
                            feedback=feedback,
                        )
            except ModelCancelled as error:
                raise TaskCancelled("foreground_cancel") from error
            calls += 1
            # The response arrived after the foreground asked to yield: discard
            # it before it can become a candidate.  The attempt itself was
            # settled truthfully by the adapter.
            self._check_cancel()
            return raw

        raw = call(None)
        proposal: dict[str, Any] | None = None
        try:
            proposal = _parse_maintenance_bundle(raw) or parse_proposal(raw)
            _validate_maintenance_proposal(self._namespace, proposal)
            return proposal, calls
        except _PARSE_ERRORS as error:
            if not getattr(self, "_allow_repair", True):
                raise
            feedback = _maintenance_repair_feedback(error, proposal)
        if self._clock() + self._model_call_cap + self._model_call_reserve > deadline:
            raise _SliceDeadline(calls)
        raw = call(feedback)
        proposal = _parse_maintenance_bundle(raw) or parse_proposal(raw)
        _validate_maintenance_proposal(self._namespace, proposal)
        return proposal, calls

    # ---------------------------------------------------------------- item

    def _recover(
        self, source: SourceMaterial, *, operation_key: str, request_id: str
    ) -> tuple[dict[str, Any] | None, int]:
        recovery: OperationRecovery = reconcile_operation(
            self._store,
            self._maintenance,
            namespace=self._namespace,
            task_id=self._task_id,
            operation_key=operation_key,
            request_id=request_id,
            target_kind="event",
            target_id=source.event_id,
            lease_token=self._lease_token,
            decision_source=GovernanceStore(self._store),
        )
        if recovery.state == "committed":
            return (
                _operation_item(
                    source=source,
                    operation_key=operation_key,
                    request_id=request_id,
                    outcome="committed",
                    detail="recovered committed receipt",
                    receipt_ref=_receipt_ref(recovery.receipt),
                ),
                0,
            )
        if recovery.state == "decision_recorded":
            outcome = _decision_outcome(recovery.decision)
            return (
                _operation_item(
                    source=source,
                    operation_key=operation_key,
                    request_id=request_id,
                    outcome=outcome,
                    detail=f"recovered decision:{outcome}",
                    candidate_id=recovery.candidate_id,
                    decision_id=getattr(recovery.decision, "decision_id", None),
                ),
                0,
            )
        if recovery.state == "candidate_pending":
            # A candidate was registered but the process lost the reply.  Resume
            # the existing governed path; do not propose a second candidate.
            if recovery.candidate_id is None:  # pragma: no cover - defensive
                return None, 1
            try:
                reviewed = settle_candidate(
                    self._changes,
                    self._access,
                    candidate_id=recovery.candidate_id,
                    review_decision_id=_stable_id(
                        self._namespace, request_id, "review"
                    ),
                    auto_authorization_id=self._auto_authorization_id,
                    commit_decision_id=_stable_id(
                        self._namespace, request_id, "commit"
                    ),
                )
            except Exception as error:  # noqa: BLE001 - a settle refusal is a result
                return (
                    _operation_item(
                        source=source,
                        operation_key=operation_key,
                        request_id=request_id,
                        outcome="failed",
                        detail=f"recover_settle:{type(error).__name__}",
                        candidate_id=recovery.candidate_id,
                    ),
                    1,
                )
            outcome, candidate_id, detail = _public_result(reviewed)
            return (
                _operation_item(
                    source=source,
                    operation_key=operation_key,
                    request_id=request_id,
                    outcome=outcome,
                    detail=f"recovered:{detail}",
                    candidate_id=candidate_id,
                    receipt_ref=(
                        _receipt_ref(getattr(reviewed, "receipt", None))
                    ),
                ),
                1,
            )
        return None, 0

    def _process(
        self, index: int, *, deadline: float
    ) -> tuple[dict[str, Any] | None, int]:
        source = _primary_material(self._material, index)
        operation_key = f"cognition:{source.event_id}"
        request_id = _stable_id(
            self._namespace, "maintenance", self._task_id, operation_key
        )

        self._check_cancel()
        recovered, calls = self._recover(
            source, operation_key=operation_key, request_id=request_id
        )
        if recovered is not None:
            return recovered, calls
        if self._should_yield():
            return None, calls
        if self._clock() >= deadline:
            return (
                _operation_item(
                    source=source,
                    operation_key=operation_key,
                    request_id=request_id,
                    outcome="deferred",
                    detail="slice_deadline_exceeded",
                ),
                calls,
            )
        try:
            context = self._build_context(source)
        except (CorruptStore, DbBusy):
            raise
        except Exception as error:  # noqa: BLE001 - a missing Context is an item result
            return (
                _operation_item(
                    source=source,
                    operation_key=operation_key,
                    request_id=request_id,
                    outcome="failed",
                    detail=f"context_unavailable:{type(error).__name__}",
                ),
                1,
            )
        try:
            proposal, used = self._generate(context, deadline=deadline)
        except _SliceDeadline as stop:
            return (
                _operation_item(
                    source=source,
                    operation_key=operation_key,
                    request_id=request_id,
                    outcome="deferred",
                    detail="slice_deadline_exceeded",
                ),
                stop.calls,
            )
        except _PARSE_ERRORS:
            return (
                _operation_item(
                    source=source,
                    operation_key=operation_key,
                    request_id=request_id,
                    outcome="failed",
                    detail="proposal_invalid",
                ),
                2 if getattr(self, "_allow_repair", True) else 1,
            )
        except ModelTransportError as error:
            label = str(getattr(error, "error_label", ""))
            if label == "TIMEOUT":
                raise LocalTimeout("maintenance model local timeout") from error
            if label == "HTTP_429":
                raise RateLimited("maintenance provider rate limit") from error
            raise RateLimited(
                f"maintenance model transport ({label or 'unknown'})"
            ) from error
        except ModelAdapterError as error:
            # A non-transport adapter refusal is retryable; run_slice records a
            # bounded waiting_dependency rather than a terminal item failure.
            raise RateLimited(
                f"maintenance model unavailable ({type(error).__name__})"
            ) from error
        calls += used
        # The foreground may have arrived while the model was answering.  The
        # late text is discarded here, before any candidate can be registered.
        self._check_cancel()
        if self._clock() >= deadline:
            return (
                _operation_item(
                    source=source,
                    operation_key=operation_key,
                    request_id=request_id,
                    outcome="deferred",
                    detail="slice_deadline_exceeded",
                ),
                calls,
            )
        bundle, reason, rebound = _host_bundle(
            self._store,
            namespace=self._namespace,
            proposal=proposal,
            scope=self._scope,
            materials=self._material.usable_sources,
        )
        if bundle is None:
            # The store's item vocabulary has no ``insufficient_evidence``:
            # "no candidate" is a confirmed ``no_change`` with the stable reason
            # in `detail`, not a fabricated success and not a terminal failure.
            if reason == "model_wait":
                return (
                    _operation_item(
                        source=source,
                        operation_key=operation_key,
                        request_id=request_id,
                        outcome="no_change",
                        detail=_audited("model_wait", rebound),
                    ),
                    calls,
                )
            if reason in (
                "claim_text_frozen",
                "claim_type_frozen",
                "target_missing",
                "target_moved",
            ):
                detail = f"refused:{reason}"
            else:
                detail = f"insufficient_evidence:{reason or 'unknown'}"
            return (
                _operation_item(
                    source=source,
                    operation_key=operation_key,
                    request_id=request_id,
                    outcome="no_change",
                    detail=_audited(detail, rebound),
                ),
                calls,
            )

        self._check_cancel()
        outcomes = propose_from_material(
            self._changes,
            self._access,
            context_id=context.context_id,
            material=(source,),
            build_bundle=lambda _material: bundle,
            request_ids=(request_id,),
        )
        if not outcomes:
            return (
                _operation_item(
                    source=source,
                    operation_key=operation_key,
                    request_id=request_id,
                    outcome="failed",
                    detail=_audited("propose_no_outcome", rebound),
                ),
                calls,
            )
        proposed = outcomes[0]
        if proposed.outcome != "proposed" or proposed.candidate_id is None:
            if proposed.outcome in ("failed", "budget_exhausted"):
                outcome = "failed"
            elif proposed.outcome == "rejected":
                outcome = "rejected"
            else:
                outcome = "no_change"
            # ``propose_from_material`` now carries a bounded stable code
            # (``propose:<code>``) instead of the exception text, so the item
            # keeps the failure class without copying model/exception prose.
            return (
                _operation_item(
                    source=source,
                    operation_key=operation_key,
                    request_id=request_id,
                    outcome=outcome,
                    detail=_audited(
                        proposed.detail or f"propose:{proposed.outcome}", rebound
                    ),
                ),
                calls,
            )
        settled = None
        try:
            settled = settle_candidate(
                self._changes,
                self._access,
                candidate_id=proposed.candidate_id,
                review_decision_id=_stable_id(
                    self._namespace, request_id, "review"
                ),
                auto_authorization_id=self._auto_authorization_id,
                commit_decision_id=_stable_id(
                    self._namespace, request_id, "commit"
                ),
            )
        except Exception as error:  # noqa: BLE001 - a refusal is an item result
            return (
                _operation_item(
                    source=source,
                    operation_key=operation_key,
                    request_id=request_id,
                    outcome="failed",
                    detail=_audited(f"settle:{type(error).__name__}", rebound),
                    candidate_id=proposed.candidate_id,
                ),
                calls,
            )
        outcome, candidate_id, detail = _public_result(settled)
        return (
            _operation_item(
                source=source,
                operation_key=operation_key,
                request_id=request_id,
                outcome=outcome,
                detail=_audited(detail, rebound),
                candidate_id=candidate_id,
                receipt_ref=_receipt_ref(getattr(settled, "receipt", None)),
            ),
            calls,
        )

    # ---------------------------------------------------------------- slice

    def __call__(self, budget: Mapping[str, Any]) -> Mapping[str, Any]:
        self._lease_token = budget.get("lease_token")
        started = self._clock()
        deadline = started + max(float(budget.get("deadline_seconds") or 0.0), 0.0)
        capacity = int(budget.get("max_process") or self._max_events)
        capacity = max(0, min(capacity, self._max_events))
        available = len(self._material.primary_usable_sources)
        page = min(capacity, available)
        items: list[dict[str, Any]] = []
        units = 0
        completed = True
        stop_reason = "reviewed"
        for index in range(page):
            cancel_event = getattr(self, "_cancel_event", None)
            if cancel_event is not None and cancel_event.is_set():
                if not items:
                    self._check_cancel()  # no atomic item yet: cancel attempt
                completed = False
                stop_reason = "foreground_cancel"
                break
            if self._should_yield():
                completed = False
                stop_reason = "foreground_yield"
                break
            if self._clock() >= deadline:
                completed = False
                stop_reason = "slice_deadline_exceeded"
                break
            item, used = self._process(index, deadline=deadline)
            units += max(0, int(used))
            if item is None:
                completed = False
                stop_reason = "foreground_yield"
                break
            items.append(item)
            if (
                item.get("outcome") == "deferred"
                and item.get("detail") == "slice_deadline_exceeded"
            ):
                completed = False
                stop_reason = "slice_deadline_exceeded"
                break
        cancel_event = getattr(self, "_cancel_event", None)
        if cancel_event is not None and cancel_event.is_set():
            completed = False
            stop_reason = "foreground_cancel"
        if page < available and completed:
            completed = False
            stop_reason = "page_limit"
        return {
            "scanned": page,
            "processed": len(items),
            "units": units,
            "items": items,
            "complete": completed,
            "stop_reason": stop_reason,
            "remaining": {
                "events_left": max(0, available - page),
                "page": page,
                "available": available,
            },
        }


def cognition_review_worker(**kwargs: Any) -> MaintenanceCognitionWorker:
    """Factory used by the runtime; keeps the worker type private."""

    return MaintenanceCognitionWorker(**kwargs)


__all__ = [
    "ALLOWED_CLAIM_TYPES",
    "ALLOWED_STRENGTHS",
    "MAINTENANCE_INSTRUCTIONS",
    "MaintenanceCognitionWorker",
    "cognition_review_worker",
]
