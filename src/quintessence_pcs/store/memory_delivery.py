"""J04: Experience projection map, delivery outbox and cleanup todos.

The bridge writes one mapping and one ``pending`` delivery in the same PCS
transaction as the observation Event.  Delivery happens outside that
transaction.  This module owns only the durable state machine; it never calls
the upstream and never stores body text.

Deletion invalidation is wired through the existing
``_after_deletion_barrier_locked`` hook: the mapping rows for the deleted
target (and for any exact source dependency of a projection) are marked
invalid, the delivery becomes ``obsolete`` and a cleanup todo is recorded.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Sequence

from ..foundation.errors import StorageError
from ..foundation.jsonutil import canonical_json_text
from ..foundation.timeutil import utc_now_rfc3339

DELIVERY_STATES = (
    "pending",
    "applied",
    "verified",
    "needs_review",
    "blocked",
    "obsolete",
)

#: The frozen projection format this card emits.
PROJECTION_FORMAT_VERSION = "j04.projection.v1"

#: A fixed UUID namespace keeps delivery ids deterministic across restarts.
_DELIVERY_NAMESPACE = uuid.UUID("5b1f0f3a-6f3e-4a3e-9d6a-0f6a1c9b7e42")


class MemoryDeliveryError(StorageError):
    """A malformed or inconsistent bridge delivery record."""

    code = "MEMORY_DELIVERY"


def delivery_id_for(namespace: str, source_event_key: str, revision: int) -> str:
    return str(
        uuid.uuid5(
            _DELIVERY_NAMESPACE, f"{namespace}|{source_event_key}|{revision}"
        )
    )


def request_hash_for(request: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(
        canonical_json_text(request).encode("utf-8")
    ).hexdigest()


class MemoryDeliveryMixin:
    """Store mixin for J04 bridge rows and the deletion barrier hook."""

    # ------------------------------------------------------------- helpers

    def _memory_tables_ready(self) -> bool:
        row = self._require_connection().execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'memory_delivery'"
        ).fetchone()
        return row is not None

    def _deletion_target_exists(
        self, namespace: str, ref_type: str, ref_id: str, ref_version: int
    ) -> bool:
        row = self._require_connection().execute(
            "SELECT 1 FROM deletion_targets WHERE namespace = ? "
            "AND target_kind = ? AND target_id = ? AND target_version = ?",
            (namespace, ref_type, ref_id, ref_version),
        ).fetchone()
        return row is not None

    # ----------------------------------------------------------- projection

    def projection_for_event(
        self, namespace: str, event_id: str
    ) -> dict[str, Any] | None:
        if not self._memory_tables_ready():
            return None
        row = self._require_connection().execute(
            "SELECT * FROM experience_source_mapping "
            "WHERE namespace = ? AND event_id = ?",
            (namespace, event_id),
        ).fetchone()
        return None if row is None else dict(row)

    def projection_for_ref(
        self, namespace: str, *, ref_type: str, ref_id: str, ref_version: int
    ) -> dict[str, Any] | None:
        """RG04: the mapping of one exact experience ref, or ``None``."""

        if not self._memory_tables_ready():
            return None
        if ref_type not in ("collab_input", "collab_artifact"):
            raise MemoryDeliveryError(f"unknown experience ref_type {ref_type!r}")
        row = self._require_connection().execute(
            "SELECT * FROM experience_source_mapping WHERE namespace = ? "
            "AND ref_type = ? AND ref_id = ? AND ref_version = ?",
            (namespace, ref_type, ref_id, int(ref_version)),
        ).fetchone()
        return None if row is None else dict(row)

    def experience_refs_for_external(
        self, namespace: str, external_refs: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """The experience ref behind each delivered memory ref.

        RG08: Scope Recall's inventory page names events by *its* opaque
        ``event-...`` id and carries no speaker.  This store is the only place
        that knows which `collab_input` or `collab_artifact` a delivered event
        came from, so a read-only caller can recover the speaker instead of
        guessing.  Keys that were never delivered (or whose delivery was
        invalidated by a deletion) are absent rather than invented.

        Read-only; a missing bridge table yields an empty map.
        """

        wanted = {
            ref
            for ref in external_refs
            if isinstance(ref, str) and ref and len(ref) <= 320
        }
        if not wanted or not self._memory_tables_ready():
            return {}
        ordered = sorted(wanted)[:64]
        placeholders = ",".join("?" for _ in ordered)
        rows = self._require_connection().execute(
            "SELECT d.external_ref, d.ref_type, d.ref_id, d.ref_version, "
            "       d.state, i.origin_kind AS input_origin, "
            "       a.origin_kind AS artifact_origin "
            "FROM memory_delivery d "
            "LEFT JOIN collab_inputs i ON i.namespace = d.namespace "
            "  AND i.input_id = d.ref_id AND d.ref_type = 'collab_input' "
            "LEFT JOIN collab_artifacts a ON a.namespace = d.namespace "
            "  AND a.artifact_id = d.ref_id AND d.ref_type = 'collab_artifact' "
            "WHERE d.namespace = ? AND d.invalidated_at IS NULL "
            "  AND d.external_ref IN (" + placeholders + ")",
            (namespace, *ordered),
        ).fetchall()
        found: dict[str, dict[str, Any]] = {}
        for row in rows:
            ref_type = str(row["ref_type"])
            origin_kind = (
                row["input_origin"]
                if ref_type == "collab_input"
                else row["artifact_origin"]
            )
            found[str(row["external_ref"])] = {
                "ref_type": ref_type,
                "ref_id": str(row["ref_id"]),
                "ref_version": int(row["ref_version"]),
                "origin_kind": str(origin_kind) if origin_kind is not None else "",
                "delivery_state": str(row["state"]),
            }
        return found

    def experience_recall_sources(
        self, namespace: str, external_refs: Sequence[str]
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        """Every live delivery row behind each upstream memory ref.

        Unlike :meth:`experience_refs_for_external` this keeps *all* rows for a
        ref, so the caller can refuse an ambiguous mapping instead of silently
        choosing one.  It also carries the exact bridge ``event_id`` and
        ``external_revision`` the automatic-recall path must verify.  Read-only;
        a missing bridge table yields an empty map.
        """

        wanted = {
            ref
            for ref in external_refs
            if isinstance(ref, str) and ref and len(ref) <= 320
        }
        if not wanted or not self._memory_tables_ready():
            return {}
        ordered = sorted(wanted)[:64]
        placeholders = ",".join("?" for _ in ordered)
        rows = self._require_connection().execute(
            "SELECT d.external_ref, d.external_revision, d.event_id, "
            "       d.ref_type, d.ref_id, d.ref_version, d.state, d.request_json, "
            "       i.origin_kind AS input_origin, i.observed_at AS input_observed_at, "
            "       i.recorded_at AS input_recorded_at, "
            "       a.origin_kind AS artifact_origin, a.created_at AS artifact_created_at "
            "FROM memory_delivery d "
            "LEFT JOIN collab_inputs i ON i.namespace = d.namespace "
            "  AND i.input_id = d.ref_id AND d.ref_type = 'collab_input' "
            "LEFT JOIN collab_artifacts a ON a.namespace = d.namespace "
            "  AND a.artifact_id = d.ref_id AND d.ref_type = 'collab_artifact' "
            "WHERE d.namespace = ? AND d.invalidated_at IS NULL "
            "  AND d.external_ref IN (" + placeholders + ") "
            "ORDER BY d.external_ref, d.ref_type, d.ref_id, d.ref_version",
            (namespace, *ordered),
        ).fetchall()
        found: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            ref_type = str(row["ref_type"])
            origin_kind = (
                row["input_origin"]
                if ref_type == "collab_input"
                else row["artifact_origin"]
            )
            revision = row["external_revision"]
            found.setdefault(str(row["external_ref"]), []).append(
                {
                    "ref_type": ref_type,
                    "ref_id": str(row["ref_id"]),
                    "ref_version": int(row["ref_version"]),
                    "origin_kind": str(origin_kind) if origin_kind is not None else "",
                    "state": str(row["state"]),
                    "event_id": str(row["event_id"]),
                    "external_revision": revision if type(revision) is int else None,
                    "observed_at": row["input_observed_at"],
                    "created_at": (
                        row["artifact_created_at"]
                        if ref_type == "collab_artifact"
                        else row["input_recorded_at"]
                    ),
                    "request_json": row["request_json"],
                }
            )
        return {ref: tuple(values) for ref, values in found.items()}

    def delivery_for_ref(
        self, namespace: str, *, ref_type: str, ref_id: str, ref_version: int
    ) -> dict[str, Any] | None:
        """RG04: the outbox row of one exact ref (stable across restarts)."""

        if not self._memory_tables_ready():
            return None
        if ref_type not in ("collab_input", "collab_artifact"):
            raise MemoryDeliveryError(f"unknown experience ref_type {ref_type!r}")
        row = self._require_connection().execute(
            "SELECT * FROM memory_delivery WHERE namespace = ? AND ref_type = ? "
            "AND ref_id = ? AND ref_version = ?",
            (namespace, ref_type, ref_id, int(ref_version)),
        ).fetchone()
        return None if row is None else dict(row)

    def bridge_gap_for_ref(
        self, namespace: str, *, ref_type: str, ref_id: str, ref_version: int
    ) -> dict[str, Any] | None:
        """RG04: the open projection gap of one exact ref, if any."""

        if not self._memory_tables_ready():
            return None
        if ref_type not in ("collab_input", "collab_artifact"):
            raise MemoryDeliveryError(f"unknown experience ref_type {ref_type!r}")
        row = self._require_connection().execute(
            "SELECT * FROM experience_bridge_gap WHERE namespace = ? "
            "AND ref_type = ? AND ref_id = ? AND ref_version = ? "
            "AND resolved_at IS NULL",
            (namespace, ref_type, ref_id, int(ref_version)),
        ).fetchone()
        return None if row is None else dict(row)

    def projection_is_usable(self, namespace: str, event_id: str) -> bool:
        """Whether a bridge Event may still be used as a live source.

        Ordinary Events (no mapping) keep their original rules.  A bridge Event
        fails closed when its mapping is missing, invalid, or its recorded
        exact dependencies touch the deletion barrier, or when the lookup
        itself fails.
        """

        if not self._memory_tables_ready():
            return True
        try:
            row = self.projection_for_event(namespace, event_id)
            if row is None:
                if self._is_bridge_event(namespace, event_id):
                    return False
                return True
            if row["invalidated_at"] is not None:
                return False
            if self.event_is_deleted(namespace, event_id):
                return False
            dependencies = json.loads(row["source_dependencies_json"])
            if not isinstance(dependencies, list):
                return False
            for dependency in dependencies:
                if not isinstance(dependency, dict):
                    return False
                if self._deletion_target_exists(
                    namespace,
                    str(dependency.get("ref_type")),
                    str(dependency.get("ref_id")),
                    int(dependency.get("ref_version")),
                ):
                    return False
            return True
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            return False

    def _is_bridge_event(self, namespace: str, event_id: str) -> bool:
        row = self._require_connection().execute(
            "SELECT event_type FROM event_journal "
            "WHERE namespace = ? AND event_id = ?",
            (namespace, event_id),
        ).fetchone()
        return row is not None and str(row["event_type"]).startswith(
            "experience."
        )

    def record_projection_locked(
        self,
        *,
        namespace: str,
        ref_type: str,
        ref_id: str,
        ref_version: int,
        event_id: str,
        source_event_key: str,
        source_revision: int,
        privacy_classes: tuple[str, ...],
        retention: str,
        origin: str,
        role: str,
        principal_kind: str,
        root_source_key: str,
        root_members: list[dict[str, Any]],
        root_verified: bool,
        source_dependencies: list[dict[str, Any]],
        request: dict[str, Any],
        delivery_id: str,
    ) -> str:
        """Insert mapping + pending delivery inside the caller's transaction."""

        connection = self._require_connection()
        now = utc_now_rfc3339()
        request_hash = request_hash_for(request)
        connection.execute(
            "INSERT INTO experience_source_mapping ("
            "namespace, ref_type, ref_id, ref_version, event_id, "
            "source_event_key, source_revision, privacy_classes_json, "
            "retention, origin, role, principal_kind, root_source_key, "
            "root_members_json, root_verified, source_dependencies_json, "
            "delivery_id, format_version, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                namespace,
                ref_type,
                ref_id,
                ref_version,
                event_id,
                source_event_key,
                source_revision,
                canonical_json_text(list(privacy_classes)),
                retention,
                origin,
                role,
                principal_kind,
                root_source_key,
                canonical_json_text(root_members),
                1 if root_verified else 0,
                canonical_json_text(source_dependencies),
                delivery_id,
                PROJECTION_FORMAT_VERSION,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO memory_delivery ("
            "delivery_id, namespace, event_id, ref_type, ref_id, ref_version, "
            "source_event_key, source_revision, request_hash, request_json, "
            "state, attempt, next_retry_at, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
            (
                delivery_id,
                namespace,
                event_id,
                ref_type,
                ref_id,
                ref_version,
                source_event_key,
                source_revision,
                request_hash,
                canonical_json_text(request),
                now,
                now,
                now,
            ),
        )
        return delivery_id

    def pending_delivery_count(self, namespace: str) -> int:
        if not self._memory_tables_ready():
            return 0
        row = self._require_connection().execute(
            "SELECT count(*) FROM memory_delivery "
            "WHERE namespace = ? AND state = 'pending'",
            (namespace,),
        ).fetchone()
        return int(row[0])

    def pending_delivery_summary(
        self, namespace: str, *, now: str | None = None
    ) -> dict[str, Any]:
        """RG05-F01: read-only outbox backlog for the idle follow-up timer.

        ``due`` counts rows a claim could take right now; ``next_due_at`` is the
        earliest instant any pending row becomes claimable (``now`` when a row
        is already claimable).  This reads state only: no body, no write, no new
        table.  ``lease_until`` is part of the same claim predicate as
        :meth:`claim_delivery`, so a crash-stale lease is reported as a future
        due time instead of being retried in front of another process.
        """

        if not self._memory_tables_ready():
            return {"pending": 0, "due": 0, "next_due_at": None}
        current = now or utc_now_rfc3339()
        row = self._require_connection().execute(
            "SELECT count(*), "
            "sum(CASE WHEN (next_retry_at IS NULL OR next_retry_at <= ?) "
            "AND (lease_until IS NULL OR lease_until <= ?) THEN 1 ELSE 0 END), "
            "min(max(COALESCE(next_retry_at, ?), COALESCE(lease_until, ?))) "
            "FROM memory_delivery WHERE namespace = ? AND state = 'pending'",
            (current, current, current, current, namespace),
        ).fetchone()
        return {
            "pending": int(row[0] or 0),
            "due": int(row[1] or 0),
            "next_due_at": None if row[2] is None else str(row[2]),
        }

    def list_deliveries(self, namespace: str) -> list[dict[str, Any]]:
        if not self._memory_tables_ready():
            return []
        rows = self._require_connection().execute(
            "SELECT * FROM memory_delivery WHERE namespace = ? "
            "ORDER BY created_at, delivery_id",
            (namespace,),
        ).fetchall()
        return [dict(row) for row in rows]

    def delivery_watermark(self, namespace: str) -> str | None:
        """RG04: latest successful delivery timestamp (UTC), if any.

        This is a *delivery* watermark, not the opaque ref-order scan cursor in
        ``experience_bridge_state``.  Dreaming/catch-up consumers can use it for
        "last confirmed delivery" without inventing a new table.
        """

        if not self._memory_tables_ready():
            return None
        row = self._require_connection().execute(
            "SELECT MAX(updated_at) FROM memory_delivery WHERE namespace = ? "
            "AND state IN ('applied', 'verified')",
            (namespace,),
        ).fetchone()
        value = row[0] if row is not None else None
        return None if value is None else str(value)

    # ----------------------------------------------------------- delivery

    def claim_delivery(
        self,
        namespace: str,
        *,
        lease_seconds: float,
        limit: int = 1,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        """Claim due ``pending`` rows in one short transaction."""

        if not self._memory_tables_ready():
            return []
        if limit < 1:
            return []
        current = now or utc_now_rfc3339()
        claimed: list[dict[str, Any]] = []
        with self.transaction():
            connection = self._require_connection()
            rows = connection.execute(
                "SELECT * FROM memory_delivery WHERE namespace = ? "
                "AND state = 'pending' "
                "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                "AND (lease_until IS NULL OR lease_until <= ?) "
                "ORDER BY next_retry_at, created_at, delivery_id LIMIT ?",
                (namespace, current, current, limit),
            ).fetchall()
            for row in rows:
                token = str(uuid.uuid4())
                lease_until = _plus_seconds(current, lease_seconds)
                updated = connection.execute(
                    "UPDATE memory_delivery SET state = 'pending', "
                    "lease_token = ?, lease_until = ?, attempt = attempt + 1, "
                    "updated_at = ? WHERE delivery_id = ? AND state = 'pending' "
                    "AND (lease_until IS NULL OR lease_until <= ?)",
                    (token, lease_until, current, row["delivery_id"], current),
                ).rowcount
                if updated != 1:
                    continue
                claimed.append(
                    {
                        **dict(row),
                        "lease_token": token,
                        "lease_until": lease_until,
                        "attempt": int(row["attempt"]) + 1,
                    }
                )
        return claimed

    def finish_delivery(
        self,
        *,
        delivery_id: str,
        lease_token: str,
        state: str,
        receipt: dict[str, Any] | None = None,
        error: str | None = None,
        next_retry_at: str | None = None,
        external_ref: str | None = None,
        external_revision: int | None = None,
    ) -> bool:
        """Apply a result only when the caller still owns the lease."""

        if state not in DELIVERY_STATES:
            raise MemoryDeliveryError(f"unknown delivery state {state!r}")
        with self.transaction():
            connection = self._require_connection()
            row = connection.execute(
                "SELECT state FROM memory_delivery WHERE delivery_id = ? "
                "AND lease_token = ? AND state = 'pending'",
                (delivery_id, lease_token),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE memory_delivery SET state = ?, receipt_json = ?, "
                "last_error = ?, next_retry_at = ?, external_ref = ?, "
                "external_revision = ?, lease_token = NULL, lease_until = NULL, "
                "updated_at = ? WHERE delivery_id = ? AND lease_token = ? "
                "AND state = 'pending'",
                (
                    state,
                    None if receipt is None else canonical_json_text(receipt),
                    error,
                    next_retry_at,
                    external_ref,
                    external_revision,
                    utc_now_rfc3339(),
                    delivery_id,
                    lease_token,
                ),
            )
        return True

    def mark_delivery_pending(
        self,
        *,
        delivery_id: str,
        lease_token: str,
        error: str,
        next_retry_at: str,
    ) -> bool:
        return self.finish_delivery(
            delivery_id=delivery_id,
            lease_token=lease_token,
            state="pending",
            error=error,
            next_retry_at=next_retry_at,
        )

    # ------------------------------------------------------------- gaps

    def record_bridge_gap(
        self,
        namespace: str,
        ref_type: str,
        ref_id: str,
        ref_version: int,
        reason: str,
    ) -> None:
        now = utc_now_rfc3339()
        with self.transaction():
            self._require_connection().execute(
                "INSERT INTO experience_bridge_gap ("
                "namespace, ref_type, ref_id, ref_version, reason, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(namespace, ref_type, ref_id, ref_version) DO UPDATE SET "
                "reason = excluded.reason, updated_at = excluded.updated_at, "
                "resolved_at = NULL",
                (namespace, ref_type, ref_id, ref_version, reason, now, now),
            )

    def resolve_bridge_gap(
        self, namespace: str, ref_type: str, ref_id: str, ref_version: int
    ) -> None:
        now = utc_now_rfc3339()
        with self.transaction():
            self._require_connection().execute(
                "UPDATE experience_bridge_gap SET resolved_at = ?, updated_at = ? "
                "WHERE namespace = ? AND ref_type = ? AND ref_id = ? "
                "AND ref_version = ? AND resolved_at IS NULL",
                (now, now, namespace, ref_type, ref_id, ref_version),
            )

    def bridge_gaps(
        self, namespace: str, *, include_resolved: bool = False
    ) -> list[dict[str, Any]]:
        if not self._memory_tables_ready():
            return []
        clause = "" if include_resolved else " AND resolved_at IS NULL"
        rows = self._require_connection().execute(
            "SELECT * FROM experience_bridge_gap WHERE namespace = ?"
            + clause
            + " ORDER BY updated_at, ref_type, ref_id, ref_version",
            (namespace,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ cursor

    def bridge_cursor(self, namespace: str) -> str | None:
        if not self._memory_tables_ready():
            return None
        row = self._require_connection().execute(
            "SELECT cursor FROM experience_bridge_state WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        return None if row is None else row["cursor"]

    def set_bridge_cursor(self, namespace: str, cursor: str | None) -> None:
        with self.transaction():
            self._require_connection().execute(
                "INSERT INTO experience_bridge_state (namespace, cursor, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(namespace) DO UPDATE SET "
                "cursor = excluded.cursor, updated_at = excluded.updated_at",
                (namespace, cursor, utc_now_rfc3339()),
            )

    # --------------------------------------------------- deletion barrier

    def _after_deletion_barrier_locked(self, target: Any) -> int:
        cleared = super()._after_deletion_barrier_locked(target)  # type: ignore[misc]
        if not self._memory_tables_ready():
            return cleared
        connection = self._require_connection()
        namespace = target.namespace
        ref_type = target.target_kind
        ref_id = target.target_id
        ref_version = target.target_version
        now = utc_now_rfc3339()
        rows = connection.execute(
            "SELECT d.delivery_id, d.source_event_key, d.source_revision, "
            "d.external_ref, d.external_revision "
            "FROM experience_source_mapping AS m "
            "JOIN memory_delivery AS d ON d.delivery_id = m.delivery_id "
            "WHERE m.namespace = ? AND ("
            "(m.ref_type = ? AND m.ref_id = ? AND m.ref_version = ?) "
            "OR EXISTS (SELECT 1 FROM json_each("
            "m.source_dependencies_json) AS dep "
            "WHERE json_extract(dep.value, '$.ref_type') = ? "
            "AND json_extract(dep.value, '$.ref_id') = ? "
            "AND json_extract(dep.value, '$.ref_version') = ?))",
            (namespace, ref_type, ref_id, ref_version, ref_type, ref_id, ref_version),
        ).fetchall()
        invalidated = 0
        for row in rows:
            connection.execute(
                "UPDATE experience_source_mapping SET invalidated_at = ?, "
                "invalidation_reason = ? WHERE namespace = ? AND delivery_id = ? "
                "AND invalidated_at IS NULL",
                (now, f"source_deleted:{ref_type}", namespace, row["delivery_id"]),
            )
            connection.execute(
                "UPDATE memory_delivery SET state = 'obsolete', "
                "invalidated_at = ?, invalidation_reason = ?, updated_at = ? "
                "WHERE namespace = ? AND delivery_id = ? "
                "AND state IN ('pending', 'applied', 'verified', 'needs_review')",
                (
                    now,
                    f"source_deleted:{ref_type}",
                    now,
                    namespace,
                    row["delivery_id"],
                ),
            )
            exists = connection.execute(
                "SELECT 1 FROM memory_cleanup_todo WHERE namespace = ? "
                "AND delivery_id = ? AND reason = ?",
                (namespace, row["delivery_id"], f"source_deleted:{ref_type}"),
            ).fetchone()
            if exists is None:
                connection.execute(
                    "INSERT INTO memory_cleanup_todo ("
                    "todo_id, namespace, ref_type, ref_id, ref_version, event_id, "
                    "delivery_id, source_event_key, source_revision, external_ref, "
                    "external_revision, unknown_result, reason, created_at"
                    ") VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        namespace,
                        ref_type,
                        ref_id,
                        ref_version,
                        row["delivery_id"],
                        row["source_event_key"],
                        row["source_revision"],
                        row["external_ref"],
                        row["external_revision"],
                        f"source_deleted:{ref_type}",
                        now,
                    ),
                )
            invalidated += 1
        return cleared + invalidated


def _plus_seconds(now_rfc3339: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    parsed = datetime.fromisoformat(now_rfc3339.replace("Z", "+00:00"))
    return (
        parsed + timedelta(seconds=float(seconds))
    ).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "DELIVERY_STATES",
    "MemoryDeliveryError",
    "MemoryDeliveryMixin",
    "PROJECTION_FORMAT_VERSION",
    "delivery_id_for",
    "request_hash_for",
]
