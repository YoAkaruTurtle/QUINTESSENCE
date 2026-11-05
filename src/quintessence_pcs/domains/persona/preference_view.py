"""UI-F02: the read-only preference projection behind the settings view.

This module is a *view*, not a second storage and not a governance path.  It
answers exactly one question for the trusted host:

    which expression preferences does this instance currently have, and what is
    each one's real state?

Three honest distinctions are the whole point of the projection:

* **confirmed** — a ``user_preference`` Belief whose marker matches a supported
  expression dimension, whose status is ``supported`` and whose cited evidence
  is still readable.  That is the same test the expression Context uses, so the
  settings view cannot claim a preference is in effect when the model would not
  receive it.
* **tentative** — a conversation-scoped ``expression_preference`` record with
  status ``tentative`` and a live source input.  It is never promoted here.
* **inactive** — everything else that is still a preference: ``retracted``
  confirmed values, revoked/superseded tentative values, a supported Belief
  whose evidence was deleted, and the non-supported statuses.  Each carries its
  real state, so the UI can never render it as "remembered".

The projection returns no raw user text: a preference is identified by its
dimension, canonical value label, host-authored claim (confirmed) and record
reference.  The stored quote/stated phrase stays in the record; the settings
view does not need it and must not become a second transcript.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..cognition.beliefs import (
    BELIEF_KIND,
    belief_evidence_is_live,
    parse_belief_body,
)
from .expression_directives import (
    EXPRESSION_DIMENSIONS,
    canonical_value_label,
    parse_preference_marker,
)
from .expression_preferences import ExpressionPreferenceService

#: A settings page shows a bounded list; the repository never renders an
#: unbounded instance dump.
MAX_ITEMS = 50
MAX_CLAIM_CHARS = 240
MAX_REASON_CHARS = 200

#: Inactive states this projection may report in addition to the stored ones.
STATE_EVIDENCE_DELETED = "evidence_deleted"
STATE_UNSUPPORTED = "unsupported"
STATE_SOURCE_DELETED = "source_deleted"


@dataclass(frozen=True)
class PreferenceGroups:
    """The three honest groups plus whether the list was capped."""

    confirmed: tuple[dict[str, Any], ...]
    tentative: tuple[dict[str, Any], ...]
    inactive: tuple[dict[str, Any], ...]
    truncated: bool


def _record_ref(version: Any) -> dict[str, Any]:
    return {
        "record_id": version.record_id,
        "record_version": int(version.record_version),
        "recorded_at": version.recorded_at,
    }


def _source(source_ref: Any) -> dict[str, Any]:
    if not isinstance(source_ref, Mapping):
        return {"kind": "unknown", "input_id": None}
    input_id = source_ref.get("input_id")
    return {
        "kind": "user_statement",
        "input_id": input_id if isinstance(input_id, str) else None,
    }


def _belief_source(body: Mapping[str, Any]) -> dict[str, Any]:
    for link in body.get("evidence_links") or ():
        if not isinstance(link, Mapping):
            continue
        ref = link.get("ref")
        if isinstance(ref, Mapping) and ref.get("ref_type") == "collab_input":
            return _source(ref)
    return {"kind": "unknown", "input_id": None}


def _bounded(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _sort_key(item: Mapping[str, Any]) -> tuple[int, str]:
    dimension = item.get("dimension")
    index = (
        EXPRESSION_DIMENSIONS.index(dimension)
        if dimension in EXPRESSION_DIMENSIONS
        else len(EXPRESSION_DIMENSIONS)
    )
    return index, str(item.get("record", {}).get("recorded_at") or "")


def list_preference_groups(
    store: Any,
    service: ExpressionPreferenceService,
    *,
    namespace: str,
    conversation_id: str | None,
    privacy_classes: frozenset[str],
) -> PreferenceGroups:
    """Project the current preference records into three honest groups."""

    confirmed: list[dict[str, Any]] = []
    inactive: list[dict[str, Any]] = []
    for version in store.current_versions_of_kind(namespace, BELIEF_KIND):
        if version.privacy_class not in privacy_classes:
            continue
        try:
            body = parse_belief_body(version)
        except Exception:  # noqa: BLE001 - a malformed belief is not a preference
            continue
        if body.get("claim_type") != "user_preference":
            continue
        marker = parse_preference_marker(body.get("review_condition"))
        if marker is None:
            continue
        dimension, value = marker
        status = str(version.status)
        alive = belief_evidence_is_live(store, namespace=namespace, body=body)
        record = _record_ref(version)
        if status == "supported" and alive:
            confirmed.append(
                {
                    "dimension": dimension,
                    "value": value,
                    "label": canonical_value_label(dimension, value),
                    "claim": _bounded(body.get("claim"), MAX_CLAIM_CHARS),
                    "strength": body.get("strength"),
                    "scope": dict(version.scope or {}),
                    "source": _belief_source(body),
                    "record": record,
                }
            )
            continue
        if status == "retracted":
            state = "retracted"
        elif not alive:
            state = STATE_EVIDENCE_DELETED
        else:
            state = STATE_UNSUPPORTED
        inactive.append(
            {
                "kind": "confirmed",
                "state": state,
                "status": status,
                "dimension": dimension,
                "value": value,
                "label": canonical_value_label(dimension, value),
                "reason": None,
                "scope": dict(version.scope or {}),
                "record": record,
                "superseded_by": None,
            }
        )

    tentative: list[dict[str, Any]] = []
    if conversation_id is not None:
        for version in service.current_in_scope(
            namespace=namespace,
            conversation_id=conversation_id,
            privacy_classes=privacy_classes,
        ):
            body = version.body if isinstance(version.body, Mapping) else {}
            dimension = body.get("dimension")
            value = body.get("value")
            if dimension not in EXPRESSION_DIMENSIONS:
                continue
            record = _record_ref(version)
            source_ref = body.get("source_ref")
            alive = (
                service.source_alive(namespace=namespace, ref=source_ref)
                if isinstance(source_ref, Mapping)
                else False
            )
            status = str(version.status)
            if status == "tentative" and alive:
                # J11 consolidation: the host no longer creates these records.
                # An existing one is history: it is listed so the user can see
                # and revoke it, but it no longer feeds the expression material
                # and must never be read as a live instruction.
                tentative.append(
                    {
                        "dimension": dimension,
                        "value": value,
                        "label": canonical_value_label(dimension, value),
                        "status": status,
                        "legacy": True,
                        "scope": dict(version.scope or {}),
                        "source": _source(source_ref),
                        "record": record,
                    }
                )
                continue
            if status == "revoked":
                state = "revoked"
            elif status == "superseded":
                state = "superseded"
            else:
                state = STATE_SOURCE_DELETED
            superseded_by = body.get("superseded_by")
            inactive.append(
                {
                    "kind": "tentative",
                    "state": state,
                    "status": status,
                    "dimension": dimension,
                    "value": value,
                    "label": canonical_value_label(dimension, value),
                    "reason": _bounded(body.get("revoked_reason"), MAX_REASON_CHARS),
                    "scope": dict(version.scope or {}),
                    "record": record,
                    "superseded_by": (
                        dict(superseded_by)
                        if isinstance(superseded_by, Mapping)
                        else None
                    ),
                }
            )

    confirmed.sort(key=_sort_key)
    tentative.sort(key=_sort_key)
    inactive.sort(
        key=lambda item: str(item.get("record", {}).get("recorded_at") or ""),
        reverse=True,
    )
    truncated = (
        len(confirmed) > MAX_ITEMS
        or len(tentative) > MAX_ITEMS
        or len(inactive) > MAX_ITEMS
    )
    return PreferenceGroups(
        confirmed=tuple(confirmed[:MAX_ITEMS]),
        tentative=tuple(tentative[:MAX_ITEMS]),
        inactive=tuple(inactive[:MAX_ITEMS]),
        truncated=truncated,
    )


__all__ = [
    "MAX_ITEMS",
    "PreferenceGroups",
    "STATE_EVIDENCE_DELETED",
    "STATE_SOURCE_DELETED",
    "STATE_UNSUPPORTED",
    "list_preference_groups",
]
