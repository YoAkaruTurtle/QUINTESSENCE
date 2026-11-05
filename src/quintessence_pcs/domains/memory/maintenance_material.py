"""RG08 R-E: build bounded, authorization-proven maintenance material.

The maintenance review consumes only bridge-delivered ``event`` sources.  This
module turns a terminal turn's exact experience refs (and the bridge events the
turn's final content Context already delivered) into the same
``MemoryRecallMaterial`` shape R-B uses, plus a ``SourceMaterial`` digest for the
maintenance receipt.  It never invents content and never reads a claim/episode/
artifact body:

* the event must be in the journal, not deleted, and carry a usable projection;
* its outbox row must be ``applied`` or ``verified``;
* the exact collaboration ref must still be readable under the host access;
* the source body's digest must match the projection's ``content_digest``;
* a message that also contains a question, a quotation, a third-party viewpoint
  or an expression preference is **kept**: the body stays intact and the item
  carries existence flags only (J11 consolidation).  A question is not a lack of
  fact, and a quotation does not make the whole message untrustworthy.

No DB mutation happens here.  A source that vanishes or moves is reported as an
unusable material with a stable reason; the caller records the reason and never
turns it into a candidate.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ...foundation.errors import EventNotFound, InvalidInput, RecordNotFound, Redacted
from ...store.experience import (
    READABILITY_AVAILABLE,
    TERMINAL_TURN_STATES,
    ExperienceAccess,
)
from ..refs import EventRef
from .experience_bridge import ALLOWED_RETENTION, role_for_origin
from .maintenance_source import SourceMaterial
from .scope_recall_recall import MemoryRecallItem, MemoryRecallMaterial
from .time_window import parse_timestamp, resolve_zone

#: Per-item body excerpt.  The full body is only hashed; it is never stored in
#: a maintenance row or a log line.
MAX_CONTENT_CHARS = 640

#: R-E's product page.  The store accepts larger pages; the host does not.
MAX_MAINTENANCE_ITEMS = 2

#: R-G3: the Context may see the current turn plus the two most recent terminal
#: user inputs.  Only the current turn is processed as an item; prior inputs are
#: bounded context material so a new observation can be compared with earlier
#: ones without turning every prior turn into another candidate.
MAX_CONTEXT_ITEMS = 3
MAX_PRIOR_TURNS = 2

#: Only sources that are legitimate evidence for a general_hypothesis may enter.
#: Kept local so the memory layer does not import the cognition domain at module
#: import time (the cognition rules still re-check this at commit).
TRUSTED_MATERIAL_SOURCE_KINDS = (
    "host_observation",
    "user_report",
    "system_internal",
    "imported_memory",
)

_ALLOWED_ORIGINS = frozenset({"user_statement", "host_supplied"})
_DELIVERED_STATES = frozenset({"applied", "verified"})


@dataclass(frozen=True)
class TurnMaterial:
    """One bounded page plus the accounting the caller records."""

    material: MemoryRecallMaterial
    sources: tuple[SourceMaterial, ...]
    dropped: tuple[dict[str, Any], ...]
    #: The current terminal turn's own input sources; the rest of ``sources`` is
    #: bounded prior/Context material and is never processed as an item.
    primary_sources: tuple[SourceMaterial, ...] = ()

    @property
    def usable_sources(self) -> tuple[SourceMaterial, ...]:
        return tuple(item for item in self.sources if item.usable)

    @property
    def primary_usable_sources(self) -> tuple[SourceMaterial, ...]:
        return tuple(item for item in self.primary_sources if item.usable)


def _text_field(value: Any, *, maximum: int = 512) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= maximum else None


def _privacy_classes(projection: Mapping[str, Any]) -> frozenset[str] | None:
    raw = projection.get("privacy_classes_json")
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(item, str) and item for item in value):
        return None
    return frozenset(value)


def _source_times(
    store: Any,
    *,
    namespace: str,
    ref: Mapping[str, Any],
    header: Any,
    projection: Mapping[str, Any],
    timezone: str,
) -> tuple[str | None, str, str, str | None, str, str]:
    """Message time plus the stored R-C1 time classification.

    Mirrors ``scope_recall_recall._row_times``: an old row without a real
    ``observed_at`` is labelled ``recorded_at_fallback``; ``event_time`` comes
    only from the projection's ``request_json.pcs_time`` and only for
    ``event_type=occurred``.
    """

    try:
        zone = resolve_zone(timezone)
    except ValueError:
        zone = None
    observed_at = header.observed_at
    recorded_at = header.recorded_at
    if ref.get("ref_type") == "collab_input":
        try:
            row = store.load_input(
                namespace=namespace,
                input_id=str(ref["input_id"]),
                content_version=int(ref["content_version"]),
            )
        except (RecordNotFound, Redacted):
            row = None
        if row is not None:
            observed_at = row.observed_at or observed_at
            recorded_at = row.recorded_at or recorded_at
    observed = parse_timestamp(observed_at)
    recorded = parse_timestamp(recorded_at)
    chosen = observed if observed is not None else recorded
    message_time: str | None = None
    message_time_basis = "unknown"
    if chosen is not None:
        message_time = (
            chosen.astimezone(zone).isoformat() if zone is not None else chosen.isoformat()
        )
        message_time_basis = (
            "observed_at" if observed is not None else "recorded_at_fallback"
        )

    event_type = "unknown"
    event_time: str | None = None
    precision = "unknown"
    time_basis = "unknown"
    raw = projection.get("request_json")
    if isinstance(raw, str) and 2 <= len(raw) <= 16384:
        try:
            document = json.loads(raw)
        except (TypeError, ValueError):
            document = None
        if isinstance(document, Mapping):
            pcs = document.get("pcs_time")
            if isinstance(pcs, Mapping):
                candidate_type = pcs.get("event_type")
                if candidate_type in (
                    "occurred",
                    "planned",
                    "hypothetical",
                    "quoted",
                    "unknown",
                ):
                    event_type = candidate_type
                candidate_precision = pcs.get("time_precision")
                if candidate_precision in ("instant", "day", "approximate", "unknown"):
                    precision = candidate_precision
                candidate_basis = pcs.get("time_basis")
                if candidate_basis in ("relative_day", "explicit_date", "unknown"):
                    time_basis = candidate_basis
                candidate_event = pcs.get("event_time")
                if event_type == "occurred" and parse_timestamp(candidate_event) is not None:
                    event_time = str(candidate_event)
    return message_time, message_time_basis, event_type, event_time, precision, time_basis


def _unusable(
    event_id: str,
    reason: str,
    *,
    source_kind: str | None = None,
    event_type: str | None = None,
) -> SourceMaterial:
    return SourceMaterial(
        event_id=event_id,
        source_kind=source_kind,
        event_type=event_type,
        observed_at=None,
        payload_digest=None,
        usable=False,
        reason=reason,
    )


def material_for_event(
    store: Any,
    access: ExperienceAccess,
    *,
    namespace: str,
    event_id: str,
    timezone: str = "Asia/Shanghai",
    current: bool = True,
) -> SourceMaterial:
    """Verify one event and load its authorized body, or return the reason."""

    projection = store.projection_for_event(namespace, event_id)
    if projection is None:
        return _unusable(event_id, "no_projection")
    if projection.get("invalidated_at") is not None:
        return _unusable(event_id, "projection_invalid")
    ref_type = projection.get("ref_type")
    ref_id = projection.get("ref_id")
    ref_version = projection.get("ref_version")
    if (
        ref_type != "collab_input"
        or not isinstance(ref_id, str)
        or type(ref_version) is not int
    ):
        return _unusable(event_id, "unsupported_kind")
    ref = {
        "ref_type": "collab_input",
        "namespace": namespace,
        "input_id": ref_id,
        "content_version": int(ref_version),
    }
    delivery = store.delivery_for_ref(
        namespace,
        ref_type="collab_input",
        ref_id=ref_id,
        ref_version=int(ref_version),
    )
    if delivery is None:
        return _unusable(event_id, "pending_delivery")
    state = str(delivery.get("state") or "")
    if state not in _DELIVERED_STATES:
        reason = "pending_delivery" if state == "pending" else f"delivery_{state or 'unknown'}"
        return _unusable(event_id, reason)
    if delivery.get("invalidated_at") is not None:
        return _unusable(event_id, "delivery_obsolete")
    if store.event_is_deleted(namespace, event_id):
        return _unusable(event_id, "source_deleted")
    header = store.event_header(namespace, event_id)
    if header is None:
        return _unusable(event_id, "source_gone")
    if not store.projection_is_usable(namespace, event_id):
        return _unusable(event_id, "projection_invalid")
    try:
        entry = store.get_event(namespace, event_id)
    except (EventNotFound, RecordNotFound, Redacted):
        return _unusable(event_id, "source_gone")
    payload = entry.payload if isinstance(entry.payload, Mapping) else {}
    experience_ref = payload.get("experience_ref")
    if not isinstance(experience_ref, Mapping):
        return _unusable(event_id, "ref_mismatch")
    try:
        same_version = int(experience_ref.get("ref_version") or 0) == int(ref_version)
    except (TypeError, ValueError):
        same_version = False
    if (
        experience_ref.get("ref_type") != "collab_input"
        or experience_ref.get("ref_id") != ref_id
        or not same_version
    ):
        return _unusable(event_id, "ref_mismatch")

    try:
        view = store.read_experience(access, ref=ref, purpose="review")
    except (InvalidInput, RecordNotFound, Redacted):
        return _unusable(event_id, "source_unavailable")
    if (
        getattr(view, "readability", None) != READABILITY_AVAILABLE
        or not isinstance(getattr(view, "body", None), str)
        or not view.body
    ):
        return _unusable(event_id, "source_unavailable")
    body = view.body
    origin_kind = str(getattr(view, "origin_kind", "") or "")
    if origin_kind not in _ALLOWED_ORIGINS:
        return _unusable(
            event_id, "untrusted_source", source_kind=header.source_kind
        )
    if header.source_kind not in TRUSTED_MATERIAL_SOURCE_KINDS:
        return _unusable(
            event_id, "untrusted_source", source_kind=header.source_kind
        )
    classes = _privacy_classes(projection)
    if classes is None or not classes <= access.privacy_classes:
        return _unusable(event_id, "privacy_not_authorized")
    retention = str(projection.get("retention") or "")
    if retention not in ALLOWED_RETENTION:
        return _unusable(event_id, "retention_not_supported")

    # J11 consolidation: a message is *material* even when it also contains a
    # question or a quotation.  Dropping the whole event lost the user's own
    # experience ("我妈妈今天和我聊了很久，她说……" is both a lived event and
    # a third party view).  We keep the full body and attach two *structural*
    # marks only -- a question mark and a quotation mark -- because those are
    # unambiguous in the text.  Who said what is the model's reading of the
    # body, and the provenance of a candidate is Review's business; the host
    # no longer runs a person-reading lexicon over the message.
    contains_question, contains_quotation = _existence_flags(body)

    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    expected = payload.get("content_digest")
    if not isinstance(expected, str) or expected != digest:
        return _unusable(event_id, "digest_mismatch")

    message_time, message_time_basis, event_type, event_time, precision, time_basis = (
        _source_times(
            store,
            namespace=namespace,
            ref=ref,
            header=header,
            projection=projection,
            timezone=timezone,
        )
    )
    subject = role_for_origin("collab_input", origin_kind) or "unknown"
    content = body[:MAX_CONTENT_CHARS]
    return SourceMaterial(
        event_id=event_id,
        source_kind=header.source_kind,
        event_type=header.event_type,
        observed_at=message_time,
        payload_digest="sha256:" + digest,
        usable=True,
        source_ref=ref,
        external_ref=(
            delivery.get("external_ref")
            if isinstance(delivery.get("external_ref"), str)
            else None
        ),
        external_revision=(
            int(delivery["external_revision"])
            if type(delivery.get("external_revision")) is int
            else None
        ),
        content=content,
        content_truncated=len(body) > MAX_CONTENT_CHARS,
        subject=subject,
        delivery_state=state,
        from_prior_turn=not current,
        message_time=message_time,
        message_time_basis=message_time_basis,
        pcs_event_type=event_type,
        event_time=event_time,
        time_precision=precision,
        time_basis=time_basis,
        contains_question=contains_question,
        contains_quotation=contains_quotation,
    )


#: J11 existence-only markers.  These are *hints for the model*, never a filter
#: and never a re-attribution: the body stays intact and the event keeps its own
#: source identity.  The preference classifier only reports "third_party" when a
#: preference word sits in the same sentence, so a plain reported view
#: ("她说我应该认真考虑未来") needs its own cue list here.
_QUOTE_MARKS = ("「", "」", "『", "』", "“", "”", "‘", "’", "\"")
_QUESTION_MARKS = ("？", "?")


def _existence_flags(body: str) -> tuple[bool, bool]:
    """Report two structural facts about the text, nothing about meaning."""

    return (
        any(mark in body for mark in _QUESTION_MARKS),
        any(mark in body for mark in _QUOTE_MARKS),
    )


def _recall_item(
    source: SourceMaterial, *, current: bool, delivery_state: str
) -> MemoryRecallItem:
    from ...runtime.budget import estimate_units

    assert source.source_ref is not None
    return MemoryRecallItem(
        upstream_ref=(source.external_ref or f"pcs:{source.event_id}")[:240],
        upstream_revision=(
            source.external_revision
            if type(source.external_revision) is int
            else int(source.source_ref.get("content_version") or 0)
        ),
        upstream_kind="event",
        content=source.content or "",
        content_truncated=source.content_truncated,
        subject=source.subject,
        subject_ref=(
            f"collab_input:{source.source_ref['input_id']}@"
            f"{source.source_ref['content_version']}"
        ),
        temporal_status="current" if current else "historical",
        occurred_at=source.observed_at,
        recorded_at=None,
        scope_id="",
        source_event_id=source.event_id,
        source_ref=dict(source.source_ref),
        delivery_state=delivery_state,
        visible_ref=EventRef(
            namespace=str(source.source_ref["namespace"]), event_id=source.event_id
        ).to_dict(),
        units=estimate_units(source.content or ""),
        message_time=source.message_time,
        message_time_basis=source.message_time_basis,
        event_type=source.pcs_event_type,
        event_time=source.event_time,
        time_precision=source.time_precision,
        time_basis=source.time_basis,
        contains_question=source.contains_question,
        contains_quotation=source.contains_quotation,
    )


def _assemble(
    sources: Sequence[SourceMaterial],
    dropped: Sequence[dict[str, Any]],
    *,
    primary_sources: Sequence[SourceMaterial] = (),
) -> TurnMaterial:
    usable = [item for item in sources if item.usable]
    items = []
    for source in usable:
        assert source.source_ref is not None
        items.append(
            _recall_item(
                source,
                current=not source.from_prior_turn,
                delivery_state=source.delivery_state or "applied",
            )
        )
    status = "ok" if not dropped else ("partial" if items else "no_match")
    material = MemoryRecallMaterial(
        status=status,
        items=tuple(items),
        dropped=tuple(dropped[:32]),
    )
    return TurnMaterial(
        material=material,
        sources=tuple(sources),
        dropped=tuple(dropped),
        primary_sources=tuple(primary_sources),
    )


def _bounded_drop(
    dropped: list[dict[str, Any]], reason: str, event_id: str | None = None
) -> None:
    entry: dict[str, Any] = {"reason": reason[:48]}
    if event_id is not None:
        entry["event_id"] = event_id[:36]
    dropped.append(entry)


def _load_input_source(
    store: Any,
    access: ExperienceAccess,
    *,
    namespace: str,
    ref: Mapping[str, Any],
    timezone: str,
    current: bool,
    seen_refs: set[tuple[str, int]],
    seen_events: set[str],
    dropped: list[dict[str, Any]],
    drop_unsupported: bool,
) -> SourceMaterial | None:
    if ref.get("ref_type") != "collab_input":
        if drop_unsupported:
            _bounded_drop(dropped, "unsupported_kind")
        return None
    ref_key = (str(ref["input_id"]), int(ref["content_version"]))
    if ref_key in seen_refs:
        return None
    seen_refs.add(ref_key)
    projection = store.projection_for_ref(
        namespace,
        ref_type="collab_input",
        ref_id=ref_key[0],
        ref_version=ref_key[1],
    )
    if projection is None:
        _bounded_drop(dropped, "no_projection", ref_key[0])
        return None
    event_id = str(projection.get("event_id") or "")
    if not event_id or event_id in seen_events:
        return None
    seen_events.add(event_id)
    source = material_for_event(
        store,
        access,
        namespace=namespace,
        event_id=event_id,
        timezone=timezone,
        current=current,
    )
    if not source.usable:
        _bounded_drop(dropped, source.reason or "unusable", event_id)
    return source


def load_turn_material(
    store: Any,
    access: ExperienceAccess,
    *,
    namespace: str,
    turn_id: str,
    timezone: str = "Asia/Shanghai",
    max_items: int = MAX_CONTEXT_ITEMS,
    prior_turns: int = MAX_PRIOR_TURNS,
) -> TurnMaterial:
    """Load current input, bounded prior terminal inputs, and recall events.

    Only the current turn's input is a *primary* source (and therefore a job
    item).  The two most recent terminal turns may contribute context material
    so a new observation can be compared with earlier ones without turning
    every old turn into another candidate.  Assistant artifacts are skipped;
    every source still goes through ``material_for_event``'s exact delivery,
    permission, deletion, version and digest checks.
    """

    if type(max_items) is not int or not 1 <= max_items <= 20:
        raise ValueError("max_items must be 1..20")
    if type(prior_turns) is not int or not 0 <= prior_turns <= 10:
        raise ValueError("prior_turns must be 0..10")
    refs = store.turn_experience_refs(access, namespace=namespace, turn_id=turn_id)
    seen_refs: set[tuple[str, int]] = set()
    seen_events: set[str] = set()
    dropped: list[dict[str, Any]] = []
    primary_sources: list[SourceMaterial] = []
    for ref in refs:
        source = _load_input_source(
            store,
            access,
            namespace=namespace,
            ref=ref,
            timezone=timezone,
            current=True,
            seen_refs=seen_refs,
            seen_events=seen_events,
            dropped=dropped,
            drop_unsupported=True,
        )
        if source is not None:
            primary_sources.append(source)

    prior_sources: list[SourceMaterial] = []
    if prior_turns:
        try:
            current_turn = store.turn_for_host(
                namespace=namespace,
                host_instance=access.host_instance,
                turn_id=turn_id,
            )
            details = store.read_turns(
                namespace=namespace,
                host_instance=access.host_instance,
                conversation_id=current_turn.conversation_id,
                limit=prior_turns + 1,
            )
            candidates = [
                detail.turn
                for detail in details
                if detail.turn.turn_no < current_turn.turn_no
                and detail.turn.state in TERMINAL_TURN_STATES
            ]
            for turn in reversed(candidates[-prior_turns:]):
                for ref in store.turn_experience_refs(
                    access, namespace=namespace, turn_id=turn.turn_id
                ):
                    source = _load_input_source(
                        store,
                        access,
                        namespace=namespace,
                        ref=ref,
                        timezone=timezone,
                        current=False,
                        seen_refs=seen_refs,
                        seen_events=seen_events,
                        dropped=dropped,
                        drop_unsupported=False,
                    )
                    if source is not None and source.usable:
                        prior_sources.append(source)
        except Exception:  # noqa: BLE001 - a missing turn link is not a material pass
            prior_sources = []

    context_sources: list[SourceMaterial] = []
    context_id = None
    lookup = getattr(store, "latest_stage_context_id", None)
    if lookup is not None:
        try:
            context_id = lookup(namespace, turn_id, kind="content")
        except Exception:  # a missing conversation link is not a material pass
            context_id = None
    if context_id is not None:
        try:
            snapshot = store.load_context_snapshot(context_id)
        except Exception:
            snapshot = None
        if snapshot is not None:
            for source in snapshot.sources:
                if getattr(source, "ref_kind", None) != "event":
                    continue
                event_id = str(getattr(source, "ref_id", ""))
                if not event_id or event_id in seen_events:
                    continue
                seen_events.add(event_id)
                item = material_for_event(
                    store,
                    access,
                    namespace=namespace,
                    event_id=event_id,
                    timezone=timezone,
                    current=False,
                )
                if not item.usable:
                    _bounded_drop(dropped, item.reason or "unusable", event_id)
                    continue
                context_sources.append(item)

    ordered: list[SourceMaterial] = list(primary_sources)
    context_slots = max(0, max_items - len(primary_sources))
    for source in [*prior_sources, *context_sources]:
        if context_slots <= 0:
            break
        if any(item.event_id == source.event_id for item in ordered):
            # The same event can arrive via a prior-turn ref and a Context
            # source; it is one source, never two independent ones.
            continue
        ordered.append(source)
        context_slots -= 1
    return _assemble(ordered, dropped, primary_sources=primary_sources)


def load_event_material(
    store: Any,
    access: ExperienceAccess,
    *,
    namespace: str,
    event_ids: Sequence[str],
    timezone: str = "Asia/Shanghai",
    max_items: int = MAX_MAINTENANCE_ITEMS,
) -> TurnMaterial:
    """Load explicit event ids for an administrative ``maintenance.run``."""

    if isinstance(event_ids, (str, bytes)):
        raise ValueError("event_ids must be a sequence")
    if type(max_items) is not int or not 1 <= max_items <= 20:
        raise ValueError("max_items must be 1..20")
    seen: set[str] = set()
    sources: list[SourceMaterial] = []
    dropped: list[dict[str, Any]] = []
    for value in event_ids:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        source = material_for_event(
            store, access, namespace=namespace, event_id=value, timezone=timezone
        )
        if not source.usable:
            _bounded_drop(dropped, source.reason or "unusable", value)
        sources.append(source)
        if len([item for item in sources if item.usable]) >= max_items:
            break
    return _assemble(sources, dropped, primary_sources=sources)


__all__ = [
    "MAX_CONTENT_CHARS",
    "MAX_CONTEXT_ITEMS",
    "MAX_MAINTENANCE_ITEMS",
    "MAX_PRIOR_TURNS",
    "TRUSTED_MATERIAL_SOURCE_KINDS",
    "TurnMaterial",
    "load_event_material",
    "load_turn_material",
    "material_for_event",
]
