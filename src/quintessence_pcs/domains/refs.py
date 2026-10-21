"""Typed references (J.1).

``pcs.v2`` has exactly two reference shapes, and a bare ID is not
representable.  These small frozen dataclasses are the domain layer's form of
those shapes: they validate on construction and serialize back to the exact
wire object.

``EventRef`` is used for a source event (episode ``event_refs`` and the profile
review reference); ``RecordRef`` is used for an exact record version (the active
profile, and every retrieval hit).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from ..foundation import identifiers
from ..foundation.errors import InvalidInput

_EVENT_REF_KEYS = frozenset(("ref_type", "namespace", "event_id"))
_RECORD_REF_KEYS = frozenset(("ref_type", "namespace", "record_id", "record_version"))


@dataclass(frozen=True)
class EventRef:
    """Typed reference to one event (J.1)."""

    namespace: str
    event_id: str

    ref_type: ClassVar[str] = "event"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_type": self.ref_type,
            "namespace": self.namespace,
            "event_id": self.event_id,
        }

    @classmethod
    def from_value(cls, value: Any, *, field: str = "event_ref") -> "EventRef":
        """Validate a wire object; bare IDs and unknown keys are rejected."""

        if not isinstance(value, Mapping):
            raise InvalidInput(
                f"{field} must be a typed EventRef object, not {type(value).__name__}"
            )
        if set(value) != _EVENT_REF_KEYS:
            raise InvalidInput(
                f"{field} must have exactly ref_type, namespace and event_id"
            )
        if value["ref_type"] != "event":
            raise InvalidInput(f'{field}.ref_type must be "event"')
        return cls(
            namespace=identifiers.require_namespace(value["namespace"]),
            event_id=identifiers.require_event_id(value["event_id"]),
        )


@dataclass(frozen=True)
class RecordRef:
    """Typed reference to one exact record version (J.1)."""

    namespace: str
    record_id: str
    record_version: int

    ref_type: ClassVar[str] = "record"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_type": self.ref_type,
            "namespace": self.namespace,
            "record_id": self.record_id,
            "record_version": self.record_version,
        }

    @classmethod
    def from_value(cls, value: Any, *, field: str = "record_ref") -> "RecordRef":
        """Validate a wire object; ``record_version`` is mandatory."""

        if not isinstance(value, Mapping):
            raise InvalidInput(
                f"{field} must be a typed RecordRef object, not {type(value).__name__}"
            )
        if set(value) != _RECORD_REF_KEYS:
            raise InvalidInput(
                f"{field} must have exactly ref_type, namespace, record_id and "
                "record_version"
            )
        if value["ref_type"] != "record":
            raise InvalidInput(f'{field}.ref_type must be "record"')
        return cls(
            namespace=identifiers.require_namespace(value["namespace"]),
            record_id=identifiers.require_record_id(value["record_id"]),
            record_version=identifiers.require_record_version(value["record_version"]),
        )


__all__ = ["EventRef", "RecordRef"]
