"""The Episode body shape (memory guide §3.1) for this card's single source.

An Episode is a unified Record with ``kind='episode'``.  This module only builds
and parses the deterministic, model-free body:

* ``event_refs`` is the exact typed source reference;
* ``summary`` is a **derived structure** (event type, source kind, observed and
  recorded time, generator label).  No event payload text is copied into the
  Episode, because a copy would be a resurrection path after deletion;
* ``started_at`` / ``ended_at`` are the source ``observed_at`` and stay null
  when the source time is unknown; this card never invents a time;
* ``source_completeness`` reports whether the material is complete
  (``complete``), missing its observation time (``partial``) or absent
  (``missing``).  It says nothing about truth;
* ``observation_refs``, ``outcome_refs``, ``correction_refs`` and
  ``participants_or_entities`` stay empty: no Observation layer, outcome model
  or entity extraction exists in this card, so filling them would be invented;
* single source only.  Cross-episode consolidation is PCS2-008/012.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ...foundation import identifiers
from ...foundation.errors import CorruptStore, InvalidInput
from ...store.domain import EventHeader
from ...store.records import RecordVersion
from ..refs import EventRef

EPISODE_KIND = "episode"
EPISODE_SCHEMA_VERSION = "1"
EPISODE_STATUS_ACTIVE = "active"

#: Deterministic template identity; no model is called by this card.
GENERATOR = "pcs2-007.template.v1"
GENERATOR_REF = "generator:pcs2-007-template-v1"

COMPLETENESS_COMPLETE = "complete"
COMPLETENESS_PARTIAL = "partial"
COMPLETENESS_MISSING = "missing"
SOURCE_COMPLETENESS_VALUES = (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_PARTIAL,
    COMPLETENESS_MISSING,
)

#: Declared uses of an episode.  This card does not filter retrieval by this
#: field (see the retrieval policy note); it records the current scope only.
ALLOWED_USES = ("reasoning", "history")

EPISODE_BODY_KEYS = frozenset(
    {
        "event_refs",
        "observation_refs",
        "started_at",
        "ended_at",
        "participants_or_entities",
        "summary",
        "outcome_refs",
        "correction_refs",
        "source_completeness",
        "generator_ref",
        "allowed_uses",
    }
)


@dataclass(frozen=True)
class EpisodeShape:
    """The validated parts of an Episode body that retrieval needs."""

    event_refs: tuple[EventRef, ...]
    started_at: str | None
    ended_at: str | None
    event_type: str
    source_kind: str
    observed_at: str | None
    recorded_at: str
    generator: str
    source_completeness: str
    generator_ref: str
    allowed_uses: tuple[str, ...]


def build_episode_body(
    *, event_ref: EventRef, header: EventHeader
) -> dict[str, Any]:
    """Build the deterministic Episode body from one source event.

    Only envelope metadata is used.  ``header`` comes from
    ``DomainStore.event_header``, which never loads the source payload.
    """

    completeness = (
        COMPLETENESS_PARTIAL
        if header.observed_at is None
        else COMPLETENESS_COMPLETE
    )
    return {
        "event_refs": [event_ref.to_dict()],
        "observation_refs": [],
        # No invention: an unknown source time stays unknown.
        "started_at": header.observed_at,
        "ended_at": header.observed_at,
        # No entity extraction exists in this card.
        "participants_or_entities": [],
        "summary": {
            "event_type": header.event_type,
            "source_kind": header.source_kind,
            "observed_at": header.observed_at,
            "recorded_at": header.recorded_at,
            "generator": GENERATOR,
        },
        "outcome_refs": [],
        "correction_refs": [],
        "source_completeness": completeness,
        "generator_ref": GENERATOR_REF,
        "allowed_uses": list(ALLOWED_USES),
    }


def parse_episode_body(version: RecordVersion) -> EpisodeShape:
    """Parse a stored Episode body defensively; never return a half-shape.

    Corruption is reported as ``CORRUPT_STORE`` rather than skipped: a stored
    Episode that cannot be read faithfully must not silently disappear from a
    retrieval that claims to cover all active episodes.
    """

    body = version.body
    if not isinstance(body, dict):
        raise CorruptStore(f"episode {version.record_id} body is not a JSON object")
    missing = EPISODE_BODY_KEYS - set(body)
    if missing:
        raise CorruptStore(
            f"episode {version.record_id} body is missing field(s): "
            f"{', '.join(sorted(missing))}"
        )
    try:
        refs_raw = _require_list(body["event_refs"], "event_refs")
        event_refs = tuple(
            EventRef.from_value(item, field="event_refs[]") for item in refs_raw
        )
        if not event_refs:
            raise CorruptStore(
                f"episode {version.record_id} has no event_refs"
            )
        for field in ("observation_refs", "outcome_refs", "correction_refs"):
            _require_list(body[field], field)
        _require_list(body["participants_or_entities"], "participants_or_entities")
        started_at = _require_nullable_timestamp(body["started_at"], "started_at")
        ended_at = _require_nullable_timestamp(body["ended_at"], "ended_at")

        summary = body["summary"]
        if not isinstance(summary, Mapping):
            raise CorruptStore(
                f"episode {version.record_id} summary is not an object"
            )
        event_type = _require_text(summary.get("event_type"), "summary.event_type", 128)
        source_kind = identifiers.require_source_kind(summary.get("source_kind"))
        observed_at = _require_nullable_timestamp(
            summary.get("observed_at"), "summary.observed_at"
        )
        recorded_at = _require_timestamp(
            summary.get("recorded_at"), "summary.recorded_at"
        )
        generator = _require_text(summary.get("generator"), "summary.generator", 128)
        completeness = body["source_completeness"]
        if completeness not in SOURCE_COMPLETENESS_VALUES:
            raise CorruptStore(
                f"episode {version.record_id} has an unknown source_completeness"
            )
        generator_ref = _require_text(body["generator_ref"], "generator_ref", 256)
        allowed_uses = tuple(
            _require_text(item, "allowed_uses[]", 64)
            for item in _require_list(body["allowed_uses"], "allowed_uses")
        )
    except InvalidInput as exc:
        raise CorruptStore(
            f"episode {version.record_id} has an invalid body: {exc}"
        ) from exc

    return EpisodeShape(
        event_refs=event_refs,
        started_at=started_at,
        ended_at=ended_at,
        event_type=event_type,
        source_kind=source_kind,
        observed_at=observed_at,
        recorded_at=recorded_at,
        generator=generator,
        source_completeness=completeness,
        generator_ref=generator_ref,
        allowed_uses=allowed_uses,
    )


def verify_summary_matches(shape: EpisodeShape, header: EventHeader) -> None:
    """A derived summary must still equal the immutable source envelope."""

    if (
        shape.event_type != header.event_type
        or shape.source_kind != header.source_kind
        or shape.observed_at != header.observed_at
        or shape.recorded_at != header.recorded_at
    ):
        raise CorruptStore(
            f"episode {header.event_id} summary disagrees with its source event"
        )


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise InvalidInput(f"{field} must be an array")
    return value


def _require_text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise InvalidInput(f"{field} must be at most {max_length} characters")
    return value


def _require_timestamp(value: Any, field: str) -> str:
    return identifiers.require_recorded_at(value)


def _require_nullable_timestamp(value: Any, field: str) -> str | None:
    return identifiers.require_observed_at(value)


__all__ = [
    "ALLOWED_USES",
    "COMPLETENESS_COMPLETE",
    "COMPLETENESS_MISSING",
    "COMPLETENESS_PARTIAL",
    "EPISODE_BODY_KEYS",
    "EPISODE_KIND",
    "EPISODE_SCHEMA_VERSION",
    "EPISODE_STATUS_ACTIVE",
    "EpisodeShape",
    "GENERATOR",
    "GENERATOR_REF",
    "SOURCE_COMPLETENESS_VALUES",
    "build_episode_body",
    "parse_episode_body",
    "verify_summary_matches",
]
