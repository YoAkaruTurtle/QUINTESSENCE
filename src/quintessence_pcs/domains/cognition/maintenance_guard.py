"""J08 AC-21: re-verify every target at each governance boundary.

A maintenance task decides *what* to touch early and then works through several
boundaries (read material, propose, commit, recover).  An authorization revoked
or a source deleted in between must stop the work — the task's opening check is
not a permission that survives the whole run.

This module is deliberately small and pure: it re-reads the targets and raises
``MaintenanceStale`` with a machine-readable reason.  Callers decide whether a
stale item is ``failed`` or ``waiting_dependency``; nothing here mutates state.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from ...foundation.errors import InvalidInput


class MaintenanceStale(InvalidInput):
    """A target changed, vanished or lost authorization after it was chosen."""

    code = "MAINTENANCE_STALE"

    def __init__(self, reason: str, *, target: Mapping[str, Any] | None = None):
        self.reason = reason
        self.target = dict(target or {})
        super().__init__(f"{reason}: {self.target or '(no target)'}")


#: The target kinds a maintenance item may point at.
TARGET_KINDS = frozenset({"record", "event", "memory_delivery", "question"})


def _targets(targets: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    checked: list[Mapping[str, Any]] = []
    for target in targets:
        kind = target.get("kind")
        if kind not in TARGET_KINDS:
            raise InvalidInput(f"unknown target kind {kind!r}")
        if not isinstance(target.get("id"), str) or not target["id"]:
            raise InvalidInput("a target needs a non-empty id")
        checked.append(target)
    return checked


def require_live_targets(
    store: Any,
    *,
    namespace: str,
    targets: Sequence[Mapping[str, Any]],
    authorization_valid: bool = True,
) -> None:
    """Raise unless every target is still live, unchanged and authorized.

    ``target`` mappings look like ``{"kind": "record", "id": ..., "version": n}``.
    A record whose current version moved is stale even if it still exists: the
    candidate was formed against the earlier text.
    """

    if not authorization_valid:
        raise MaintenanceStale("authorization_revoked")
    checked = _targets(targets)

    for target in checked:
        kind = target["kind"]
        target_id = str(target["id"])
        version = target.get("version")

        if kind == "event":
            if store.event_is_deleted(namespace, target_id):
                raise MaintenanceStale("source_deleted", target=target)
            if store.event_header(namespace, target_id) is None:
                raise MaintenanceStale("source_gone", target=target)
            continue

        if kind == "memory_delivery":
            rows = {
                str(row["delivery_id"]): str(row["state"])
                for row in store.list_deliveries(namespace)
            }
            state = rows.get(target_id)
            if state is None:
                raise MaintenanceStale("delivery_gone", target=target)
            if state == "obsolete":
                raise MaintenanceStale("delivery_obsolete", target=target)
            continue

        # ``record`` and ``question`` are both record versions.
        pointer = store.record_pointer(namespace, target_id)
        if pointer is None:
            raise MaintenanceStale("target_gone", target=target)
        if version is not None and int(pointer.current_version) != int(version):
            raise MaintenanceStale("target_version_moved", target=target)


__all__ = ["MaintenanceStale", "TARGET_KINDS", "require_live_targets"]
