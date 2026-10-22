"""Re-validate a stored Context inside the Tx B transaction (J.5/J.6).

The Tx B watermarks already reject most stale bases, but three things still
have to be proven against the *stored* Context rather than the proposal:

* the Context is a complete PCS2-008 row (006-era snapshots are
  ``NEEDS_CONTEXT``);
* the rendered input still exists (a deletion cleanup may have cleared it);
* every exact source the Context used is still present and not inside a
  deletion barrier, and every ``read_set`` usage still agrees with the store
  (``current`` must be the current version; ``historical``/``pinned_asset``
  must still resolve exactly).

All checks run inside the Tx B write transaction, so a deletion cannot commit
between the check and the append.  A failure is ``CONTEXT_STALE``: the
proposal basis is no longer valid, and nothing is written.  A structurally
broken reference (record row present but version missing) is
``CORRUPT_STORE``, because that is not a normal staleness.
"""

from __future__ import annotations

from typing import Any

from ...foundation.errors import (
    ContextStale,
    CorruptStore,
    RecordNotFound,
    Redacted,
    ScopeViolation,
    SnapshotNotFound,
)
from ...store.cognition import CognitionStore, ContextSnapshot
from ...store.errors import NeedsContext
from ..refs import EventRef, RecordRef

READ_USAGES = ("current", "historical", "pinned_asset")


def require_live_context(
    store: CognitionStore, *, context_id: str, namespace: str
) -> ContextSnapshot:
    """Load one full Context and prove its basis is still live.

    Raises ``NEEDS_CONTEXT`` for a pre-008 snapshot, ``CONTEXT_STALE`` when the
    basis moved or a source was deleted, ``NOT_FOUND`` for an unknown ID and
    ``CORRUPT_STORE`` for a broken reference structure.
    """

    try:
        snapshot = store.load_context_snapshot(context_id)
    except Redacted as exc:
        raise ContextStale(
            f"context {context_id} rendered input has been cleared by a deletion"
        ) from exc
    except (SnapshotNotFound, NeedsContext):
        raise
    if snapshot.namespace != namespace:
        raise ScopeViolation(
            f"context {context_id} belongs to namespace {snapshot.namespace!r}, "
            f"not {namespace!r}"
        )

    trigger = EventRef.from_value(snapshot.trigger_ref, field="trigger_ref")
    if trigger.namespace != namespace:
        raise ScopeViolation(
            f"context trigger is in namespace {trigger.namespace!r}, not {namespace!r}"
        )
    _require_live_event(store, namespace, trigger.event_id, what="context trigger")

    for entry in snapshot.read_set:
        _check_read_set_entry(store, namespace, entry)

    for source in snapshot.sources:
        if source.ref_kind == "event":
            _require_live_event(
                store, namespace, source.ref_id, what="a context source event"
            )
        else:
            if store.record_is_deleted(namespace, source.ref_id):
                raise ContextStale(
                    "a record this context used has been deleted"
                )
            if source.ref_version < 1:  # pragma: no cover - schema guarantees this
                raise CorruptStore("a context record source has no record_version")
            try:
                store.get_version(namespace, source.ref_id, source.ref_version)
            except RecordNotFound as exc:
                raise CorruptStore(
                    "a context record source points at a missing record version"
                ) from exc

    if store.collab_context_sources_barred(
        namespace=namespace, context_id=context_id
    ):
        raise ContextStale(
            "a collaboration source this context used has been deleted"
        )

    for item in snapshot.pending_source_event_refs:
        ref = EventRef.from_value(item, field="pending_source_event_refs[]")
        if ref.namespace != namespace:
            raise ScopeViolation("a pending source event belongs to another namespace")
        _require_live_event(
            store, namespace, ref.event_id, what="a pending source event"
        )

    return snapshot


def _check_read_set_entry(
    store: CognitionStore, namespace: str, entry: dict[str, Any]
) -> None:
    if not isinstance(entry, dict) or set(entry) != {"ref", "usage"}:
        raise CorruptStore("stored read_set entry does not have exactly ref and usage")
    usage = entry["usage"]
    if usage not in READ_USAGES:
        raise CorruptStore("stored read_set entry has an unknown usage")
    ref = RecordRef.from_value(entry["ref"], field="read_set[].ref")
    if ref.namespace != namespace:
        raise ScopeViolation("a read_set reference belongs to another namespace")
    if store.record_is_deleted(namespace, ref.record_id):
        raise ContextStale("a record listed in read_set has been deleted")
    if usage == "current":
        pointer = store.record_pointer(namespace, ref.record_id)
        if pointer is None:
            raise CorruptStore("a current read_set entry points at a missing record")
        if pointer.current_version != ref.record_version:
            raise ContextStale(
                "a current read_set entry is no longer the current version"
            )
        return
    try:
        store.get_version(namespace, ref.record_id, ref.record_version)
    except RecordNotFound as exc:
        raise CorruptStore(
            "a historical read_set entry points at a missing record version"
        ) from exc


def _require_live_event(
    store: CognitionStore, namespace: str, event_id: str, *, what: str
) -> None:
    if store.event_is_deleted(namespace, event_id):
        raise ContextStale(f"{what} has been deleted")
    if store.event_header(namespace, event_id) is None:
        raise CorruptStore(f"{what} is missing from the event journal")


__all__ = ["READ_USAGES", "require_live_context"]
