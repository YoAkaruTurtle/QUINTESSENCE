"""One revision contract for every path that can change a Belief.

The domain's PCS2-008 claim freeze remains the authority.  This module owns
exactly two things:

* the words a model reads about ``revise_belief`` versus a substantive
  replacement (chat, maintenance and the bounded step path); and
* a shared, non-writing pre-flight check that refuses a claim-changing
  ``revise_belief`` before a candidate is registered.

It never accepts a revision, never writes a claim, and never replaces the
domain/commit validation: callers still go through the normal candidate,
Review and Tx B path after this check passes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..domains.cognition.beliefs import (
    CLAIM_TEXT_FROZEN,
    CLAIM_TYPE_FROZEN,
    parse_belief_body,
    revision_claim_conflict,
)
from ..domains.refs import RecordRef
from ..foundation.errors import RecordNotFound

SURFACE_CHAT = "chat"
SURFACE_MAINTENANCE = "maintenance"

#: Shared claim-freeze sentences.  The surfaces below add only their own legal
#: replacement shape; the freeze rule itself is not restated anywhere else.
CLAIM_FROZEN_RULE = (
    "The claim text and claim_type of an existing Belief are frozen in PCS2-008: "
    "revise_belief may only update evidence links, strength, strength_reason, "
    "review_condition or validity for the same claim; body.claim and "
    "body.claim_type must be byte-identical to the target version. Counter-evidence "
    "only conflicts with the current claim; it does not require or prove an opposite "
    "proposition. The host never rewrites or supplies a claim for you."
)

CHAT_REPLACEMENT_RULE = (
    "To change the claim substantively, send ONE cognition.propose call whose "
    "changes contains retract_belief (the exact target record_id and record_version, "
    "live evidence links and a reason) followed by propose_belief (the replacement "
    "body); it is one atomic candidate/commit, not two calls. Keep the target/version "
    "in the retraction and state the replacement in summary/reason. Do not repeat the "
    "same revise_belief."
)

MAINTENANCE_REPLACEMENT_RULE = (
    "要实质改变命题，必须返回一次 change_bundle：changes 为 retract_belief（原 target 的 "
    "record_id/record_version 原样、material 内的 evidence_links、reason）加 "
    "propose_belief（新 claim/body）；最多两个 change，一次原子提交，不要拆成两次。"
    "target/version 必须原样保留，替代理由写入 summary/reason；不要重复同一 revise_belief。"
)

STEP_REPLACEMENT_RULE = (
    "A substantive claim change is never one revise_belief. In a path that supports "
    "composition, send one bundle with retract_belief and propose_belief; in this "
    "bounded step path, retract_belief now and propose_belief the replacement in a "
    "later step. Keep the exact target/version; do not encode both operations as one "
    "revise_belief."
)

REVISION_RULE = " ".join((CLAIM_FROZEN_RULE, CHAT_REPLACEMENT_RULE))
MAINTENANCE_REVISION_RULE = " ".join((CLAIM_FROZEN_RULE, MAINTENANCE_REPLACEMENT_RULE))
STEP_REVISION_RULE = " ".join((CLAIM_FROZEN_RULE, STEP_REPLACEMENT_RULE))

MAINTENANCE_BUNDLE_NOTE = (
    "retract_belief 与 propose_belief 必须在同一个 change_bundle 内；"
    "retract 的 target/version 原样保留，propose 写新 claim；"
    "反证只表示冲突，不表示相反命题为真；宿主不代写 claim。"
)

CLAIM_FIELD_NOTE = (
    "frozen for revise_belief: copy it byte-identical from the target version; "
    "a substantive change is one retract_belief + propose_belief bundle, never a "
    "rewritten claim here"
)
CLAIM_TYPE_FIELD_NOTE = (
    "frozen for revise_belief: copy it byte-identical from the target version; "
    "changing it is not a revision"
)

CHANGE_OP_NOTES: Mapping[str, str] = {
    "propose_belief": (
        "Create a new belief record; in a substantive correction it is the "
        "replacement half of one retract_belief + propose_belief bundle."
    ),
    "revise_belief": (
        "Same proposition only: body.claim and body.claim_type must be "
        "byte-identical to the target version. Rewriting either is not a revision; "
        "use one retract_belief + propose_belief bundle for a substantive change."
    ),
    "retract_belief": (
        "Withdraw the exact current target with live evidence and a reason. This is "
        "not a claim that the opposite is true; when replacing it, pair it with "
        "propose_belief in the same bundle."
    ),
}


@dataclass(frozen=True)
class RevisionRefusal:
    """One pre-flight refusal: stable code, field path and legal alternative."""

    code: str
    field: str
    target_record_id: str
    target_record_version: int
    message: str


def _replacement_rule(surface: str) -> str:
    if surface == SURFACE_MAINTENANCE:
        return MAINTENANCE_REPLACEMENT_RULE
    return CHAT_REPLACEMENT_RULE


def _feedback(
    *,
    code: str,
    field: str,
    record_id: str,
    record_version: int,
    surface: str,
) -> str:
    return (
        f"{field} is frozen for revise_belief ({code}); revise_belief only updates "
        f"evidence/state for the same proposition. Keep target {record_id}@"
        f"{record_version} exact. {_replacement_rule(surface)} "
        "No candidate was created; the host does not write or supply a claim."
    )


def revision_refusal(
    current_body: Mapping[str, Any],
    proposed_body: Mapping[str, Any],
    *,
    index: int,
    target_record_id: str,
    target_record_version: int,
    surface: str = SURFACE_CHAT,
) -> RevisionRefusal | None:
    """Pure claim/claim_type check for one ``revise_belief`` change.

    The caller supplies the already-read current body and the proposed body; no
    store, candidate or Review side effect happens here.
    """

    conflict = revision_claim_conflict(current_body, proposed_body)
    if conflict is None:
        return None
    field = (
        f"changes[{index}].body.claim"
        if conflict == CLAIM_TEXT_FROZEN
        else f"changes[{index}].body.claim_type"
    )
    return RevisionRefusal(
        code=conflict,
        field=field,
        target_record_id=str(target_record_id),
        target_record_version=int(target_record_version),
        message=_feedback(
            code=conflict,
            field=field,
            record_id=str(target_record_id),
            record_version=int(target_record_version),
            surface=surface,
        ),
    )


def check_revision_target(
    store: Any,
    namespace: str,
    change: Mapping[str, Any],
    *,
    index: int,
    surface: str = SURFACE_CHAT,
) -> RevisionRefusal | None:
    """Pre-flight one ``revise_belief`` change against the live target.

    Returns ``None`` when the change is not a revise, when its target is not a
    usable RecordRef (the normal domain validator owns that refusal), or when
    the claim/claim_type match the current version.  A missing or moved target
    is refused here so no candidate is registered for a target that cannot be
    revised anyway.  This is not a substitute for the transaction's own check.
    """

    if change.get("op") != "revise_belief":
        return None
    target = change.get("target")
    if not isinstance(target, Mapping):
        return None
    try:
        ref = RecordRef.from_value(target, field="target")
    except Exception:  # noqa: BLE001 - the domain reports a malformed target
        return None
    version = int(ref.record_version)
    field = f"changes[{index}].target"
    try:
        current = store.get_current(namespace, ref.record_id)
    except RecordNotFound:
        return RevisionRefusal(
            code="target_missing",
            field=field,
            target_record_id=ref.record_id,
            target_record_version=version,
            message=(
                f"{field} names a belief {ref.record_id}@{version} that is not "
                "available; re-read the current belief and use its exact "
                "record_version. No candidate was created."
            ),
        )
    if int(current.record_version) != version:
        return RevisionRefusal(
            code="target_moved",
            field=field,
            target_record_id=ref.record_id,
            target_record_version=version,
            message=(
                f"{field} is stale: belief {ref.record_id} is now at version "
                f"{current.record_version}, not {version}; re-read the current "
                "version. No candidate was created."
            ),
        )
    current_body = parse_belief_body(current)
    proposed_body = change.get("body")
    if not isinstance(proposed_body, Mapping):
        return None
    return revision_refusal(
        current_body,
        proposed_body,
        index=index,
        target_record_id=ref.record_id,
        target_record_version=version,
        surface=surface,
    )


__all__ = [
    "CHANGE_OP_NOTES",
    "CHAT_REPLACEMENT_RULE",
    "CLAIM_FIELD_NOTE",
    "CLAIM_FROZEN_RULE",
    "CLAIM_TYPE_FIELD_NOTE",
    "MAINTENANCE_BUNDLE_NOTE",
    "MAINTENANCE_REPLACEMENT_RULE",
    "MAINTENANCE_REVISION_RULE",
    "REVISION_RULE",
    "RevisionRefusal",
    "STEP_REPLACEMENT_RULE",
    "STEP_REVISION_RULE",
    "SURFACE_CHAT",
    "SURFACE_MAINTENANCE",
    "check_revision_target",
    "revision_refusal",
]
