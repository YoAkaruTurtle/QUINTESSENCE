"""B group: the minimal SelfView projection (cognition guide §2, card B).

A SelfView is a Record with ``kind='self_view'``.  Its body contains exactly
three fields from the guide — ``entity_id``, ``belief_refs`` and
``observed_config_refs`` — and deliberately **no self-description text**: it
may only point at existing Belief versions, never add a conclusion.

The selection policy in PCS2-008 is minimal and replaceable: include current
Beliefs whose ``claim_type`` is ``capability`` and whose status is
``supported`` or ``contested``.  The exact input set is the pair
``(record_id, record_version)``; a change in either (a new version with a new
status, a deleted record, a new belief) makes the stored projection stale.

Freshness semantics (the handoff card's r2 correction):

* input set unchanged -> ``fresh``; rebuilding is a legal no-op and does **not**
  create a version or advance ``state_revision``;
* input set changed -> ``stale``; rebuilding appends a new version and advances
  ``state_revision`` exactly once through the unified write entry;
* a referenced Belief was deleted -> ``unavailable`` (and no content is
  returned); deleting the SelfView record itself is a deletion decision and
  keeps the PCS2-006 ``REDACTED`` barrier;
* only a genuinely broken reference structure (record row missing, version
  chain broken, wrong kind) is ``CORRUPT_STORE``.

``build_self_view`` is a domain projection, not a proposal: it runs in one
store transaction via ``append_record_version`` (the unified write entry) and
does not route through Tx B because it has no Context or command basis.  It is
still subject to the PCS2-006 deletion barrier.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ...foundation import identifiers
from ...foundation.errors import CorruptStore, InvalidInput, RecordNotFound
from ...store.cognition import CognitionStore
from ...store.records import RecordVersion
from ..refs import RecordRef
from .beliefs import (
    BELIEF_KIND,
    belief_evidence_is_live,
    parse_belief_body,
)

SELF_VIEW_KIND = "self_view"
SELF_VIEW_SCHEMA_VERSION = "1"
SELF_ENTITY_ID = "self"

SELF_VIEW_FRESH = "fresh"
SELF_VIEW_STALE = "stale"
SELF_VIEW_UNAVAILABLE = "unavailable"
SELF_VIEW_FRESHNESS = (
    SELF_VIEW_FRESH,
    SELF_VIEW_STALE,
    SELF_VIEW_UNAVAILABLE,
)

#: The minimal SelfView policy of PCS2-008.
SELF_VIEW_CLAIM_TYPE = "capability"
SELF_VIEW_STATUSES = frozenset({"supported", "contested"})

SELF_VIEW_BODY_KEYS = frozenset(
    {"entity_id", "belief_refs", "observed_config_refs"}
)

#: Stable namespace for the deterministic per-namespace SelfView record ID.
SELF_VIEW_RECORD_NAMESPACE = uuid.UUID("6f0c3e2a-1b7d-5c49-8e2f-3a9d4b6c8e10")


@dataclass(frozen=True)
class SelfViewState:
    """Uniform answer of ``get_self_view``; stale is not an error."""

    namespace: str
    ref: RecordRef | None
    freshness: str


def self_view_record_id(namespace: str, entity_id: str = SELF_ENTITY_ID) -> str:
    """Deterministic record ID for one namespace's SelfView projection."""

    ns = identifiers.require_namespace(namespace)
    if not isinstance(entity_id, str) or not entity_id:
        raise InvalidInput("entity_id must be a non-empty string")
    return str(uuid.uuid5(SELF_VIEW_RECORD_NAMESPACE, f"{ns}\x1f{entity_id}"))


class SelfViewService:
    """Build and read the minimal SelfView for one PCS store."""

    def __init__(self, store: CognitionStore) -> None:
        self._store = store

    # ------------------------------------------------------------------ read

    def get_self_view(self, namespace: str) -> SelfViewState:
        """Return the current projection plus a freshness verdict.

        A stale projection is returned as ``stale`` with its existing ref; a
        projection whose referenced Belief was deleted is ``unavailable`` and
        carries no ref (so no content can leak through it).  A missing record
        or broken version chain is ``CORRUPT_STORE``, never silently "unavailable".
        """

        ns = identifiers.require_namespace(namespace)
        record_id = self_view_record_id(ns)
        with self._store.read_snapshot():
            pointer = self._store.record_pointer(ns, record_id)
            if pointer is None:
                return SelfViewState(ns, None, SELF_VIEW_UNAVAILABLE)
            if pointer.kind != SELF_VIEW_KIND:
                raise CorruptStore(
                    f"self-view record {record_id} is kind {pointer.kind!r}"
                )
            current = self._store.get_version(
                ns, record_id, pointer.current_version
            )
            body = parse_self_view_body(current)
            for ref in body["belief_refs"]:
                if self._store.record_is_deleted(ns, ref.record_id):
                    return SelfViewState(ns, None, SELF_VIEW_UNAVAILABLE)
                try:
                    referenced = self._store.get_version(
                        ns, ref.record_id, ref.record_version
                    )
                except RecordNotFound as exc:
                    raise CorruptStore(
                        f"self-view {record_id} references a missing belief "
                        f"version {ref.record_id}@{ref.record_version}"
                    ) from exc
                if referenced.kind != BELIEF_KIND:
                    raise CorruptStore(
                        f"self-view {record_id} references a non-belief record"
                    )
                if not belief_evidence_is_live(
                    self._store, namespace=ns, body=parse_belief_body(referenced)
                ):
                    # The belief row exists, but a source it used was deleted.
                    # Treat the projection exactly like a deleted reference: it
                    # is unavailable and its content must not be returned.
                    return SelfViewState(ns, None, SELF_VIEW_UNAVAILABLE)
            current_refs = self._input_refs_locked(ns)
            freshness = (
                SELF_VIEW_FRESH
                if _refs_equal(body["belief_refs"], current_refs)
                else SELF_VIEW_STALE
            )
            return SelfViewState(
                namespace=ns,
                ref=RecordRef(
                    namespace=ns,
                    record_id=record_id,
                    record_version=current.record_version,
                ),
                freshness=freshness,
            )

    # ----------------------------------------------------------------- build

    def build_self_view(self, *, namespace: str) -> RecordVersion:
        """Append a new projection version only when the input set changed."""

        ns = identifiers.require_namespace(namespace)
        self._store.require_standalone("build_self_view")
        with self._store.transaction():
            refs = self._input_refs_locked(ns)
            refs_json = [ref.to_dict() for ref in refs]
            record_id = self_view_record_id(ns)
            pointer = self._store.record_pointer(ns, record_id)
            if pointer is None:
                version = 1
            else:
                if pointer.kind != SELF_VIEW_KIND:
                    raise CorruptStore(
                        f"self-view record {record_id} is kind {pointer.kind!r}"
                    )
                current = self._store.get_version(
                    ns, record_id, pointer.current_version
                )
                existing = parse_self_view_body(current)
                if _refs_equal(existing["belief_refs"], refs):
                    return current
                version = current.record_version + 1
            return self._store.append_record_version(
                namespace=ns,
                record_id=record_id,
                record_version=version,
                kind=SELF_VIEW_KIND,
                body={
                    "entity_id": SELF_ENTITY_ID,
                    "belief_refs": refs_json,
                    "observed_config_refs": [],
                },
                status="active",
                schema_version=SELF_VIEW_SCHEMA_VERSION,
                privacy_class="private",
                scope=None,
            )

    # ------------------------------------------------------------- internals

    def _input_refs_locked(self, namespace: str) -> tuple[RecordRef, ...]:
        """Exact current refs of the Beliefs this policy includes."""

        refs: list[RecordRef] = []
        for version in self._store.current_versions_of_kind(namespace, BELIEF_KIND):
            if version.status not in SELF_VIEW_STATUSES:
                continue
            body = parse_belief_body(version)
            if body["claim_type"] != SELF_VIEW_CLAIM_TYPE:
                continue
            if not belief_evidence_is_live(
                self._store, namespace=namespace, body=body
            ):
                # The stored status still says supported/contested, but a
                # source it used was deleted.  Do not present it as current.
                continue
            refs.append(
                RecordRef(
                    namespace=namespace,
                    record_id=version.record_id,
                    record_version=version.record_version,
                )
            )
        refs.sort(key=lambda ref: ref.record_id)
        return tuple(refs)


# --------------------------------------------------------------- body shape


def parse_self_view_body(version: RecordVersion) -> dict[str, Any]:
    """Validate a stored SelfView body defensively."""

    if version.kind != SELF_VIEW_KIND:
        raise CorruptStore(
            f"record {version.record_id} is kind {version.kind!r}, not a self_view"
        )
    if version.schema_version != SELF_VIEW_SCHEMA_VERSION:
        raise CorruptStore(
            f"self-view {version.record_id} has unsupported schema_version "
            f"{version.schema_version!r}"
        )
    body = version.body
    if not isinstance(body, Mapping):
        raise CorruptStore("self-view body is not a JSON object")
    if set(body) != SELF_VIEW_BODY_KEYS:
        raise CorruptStore(
            "self-view body must have exactly entity_id, belief_refs and "
            "observed_config_refs"
        )
    if body["entity_id"] != SELF_ENTITY_ID:
        raise CorruptStore(
            f"self-view entity_id must be {SELF_ENTITY_ID!r}"
        )
    raw_refs = body["belief_refs"]
    if not isinstance(raw_refs, list):
        raise CorruptStore("self-view belief_refs must be an array")
    refs: list[RecordRef] = []
    seen: set[tuple[str, int]] = set()
    for index, item in enumerate(raw_refs):
        try:
            ref = RecordRef.from_value(item, field=f"belief_refs[{index}]")
        except InvalidInput as exc:
            raise CorruptStore(f"self-view has an invalid belief ref: {exc}") from exc
        key = (ref.record_id, ref.record_version)
        if key in seen:
            raise CorruptStore("self-view belief_refs contains a duplicate")
        seen.add(key)
        refs.append(ref)
    observed = body["observed_config_refs"]
    if not isinstance(observed, list) or observed:
        # PCS2-008 has no observed-config layer; filling this would be a
        # fabricated self-description, so only the empty list is accepted.
        raise CorruptStore(
            "self-view observed_config_refs must be an empty array in PCS2-008"
        )
    return {
        "entity_id": body["entity_id"],
        "belief_refs": refs,
        "observed_config_refs": [],
    }


def _refs_equal(
    left: list[RecordRef], right: tuple[RecordRef, ...]
) -> bool:
    if len(left) != len(right):
        return False
    return all(
        a.record_id == b.record_id
        and a.record_version == b.record_version
        and a.namespace == b.namespace
        for a, b in zip(left, right)
    )


__all__ = [
    "SELF_ENTITY_ID",
    "SELF_VIEW_BODY_KEYS",
    "SELF_VIEW_CLAIM_TYPE",
    "SELF_VIEW_FRESH",
    "SELF_VIEW_FRESHNESS",
    "SELF_VIEW_KIND",
    "SELF_VIEW_RECORD_NAMESPACE",
    "SELF_VIEW_SCHEMA_VERSION",
    "SELF_VIEW_STALE",
    "SELF_VIEW_STATUSES",
    "SELF_VIEW_UNAVAILABLE",
    "SelfViewService",
    "SelfViewState",
    "parse_self_view_body",
    "self_view_record_id",
]
