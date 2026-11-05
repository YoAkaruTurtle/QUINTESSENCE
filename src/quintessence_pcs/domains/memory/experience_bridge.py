"""J04: Experience source bridge (projection + bounded delivery).

The bridge is explicit: ``sync`` reads authorized Experience refs and projects
them into a reference-only Event plus an outbox row in one PCS transaction;
``deliver`` claims outbox rows and calls the minimal upstream capture adapter
outside that transaction.  It never copies body text into the mapping, outbox,
logs or receipts.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from ...foundation.errors import StorageError
from ...foundation.timeutil import utc_now_rfc3339
from .time_window import parse_event_time
from ...store.experience import (
    ExperienceAccess,
    ExperiencePage,
    ExperienceUnavailable,
    ExperienceView,
)
from ...store.memory_delivery import (
    DELIVERY_STATES,
    PROJECTION_FORMAT_VERSION,
    delivery_id_for,
    request_hash_for,
)

PURPOSE = "memory.bridge"
EVENT_TYPE = "experience.observed"
MAX_ROOT_DEPTH = 16
#: Scope Recall persists the body; only these source retention levels are
#: compatible with that longer-lived copy.  ``session`` is fail-closed.
ALLOWED_RETENTION = ("standard", "durable")

_SOURCE_KIND = {
    ("collab_input", "user_statement"): "user_report",
    ("collab_input", "host_supplied"): "host_observation",
    ("collab_artifact", "model_output"): "model_inference",
    ("collab_artifact", "deterministic_query"): "host_observation",
}


class BridgeError(StorageError):
    code = "MEMORY_BRIDGE"


@dataclass(frozen=True)
class BridgeConfig:
    """Host-owned bridge settings; no model, network or credentials."""

    scope_id: str
    capture_timeout_seconds: float = 10.0
    batch_timeout_seconds: float = 60.0
    lease_seconds: float = 60.0
    max_attempts: int = 5
    retry_backoff_seconds: float = 30.0
    max_batch: int = 8
    #: RG08 R-C1: the zone a message's relative day ("昨天") is read in.  The
    #: projection writes the result into ``request_json.pcs_time``; it is never
    #: sent to the upstream capture (only that request's whitelist is).
    timezone: str = "Asia/Shanghai"
    #: RG08 R-C1 host rollback switch: when false, projection stores an unknown
    #: time record instead of parsing the message.
    time_enabled: bool = True


@dataclass
class SyncResult:
    scanned: int = 0
    projected: int = 0
    skipped_existing: int = 0
    skipped_deleted: int = 0
    blocked: int = 0
    gaps: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str | None = None
    pending: int = 0


@dataclass
class DeliveryResult:
    claimed: int = 0
    applied: int = 0
    verified: int = 0
    pending: int = 0
    needs_review: int = 0
    blocked: int = 0
    obsolete: int = 0
    errors: list[str] = field(default_factory=list)


class ExperienceBridge:
    def __init__(self, store: Any, adapter: Any, config: BridgeConfig):
        self._store = store
        self._adapter = adapter
        self.config = config

    # ---------------------------------------------------------------- sync

    def sync(
        self,
        access: ExperienceAccess,
        *,
        namespace: str,
        after_cursor: str | None = None,
        limit: int = 20,
    ) -> SyncResult:
        result = SyncResult()
        page: ExperiencePage = self._store.list_experience_refs(
            access, namespace=namespace, after_cursor=after_cursor, limit=limit
        )
        for ref in page.refs:
            self._sync_one(access, namespace, ref, result)
        result.next_cursor = page.next_cursor
        self._store.set_bridge_cursor(namespace, page.next_cursor)
        result.pending = self._store.pending_delivery_count(namespace)
        return result

    def sync_refs(
        self,
        access: ExperienceAccess,
        *,
        namespace: str,
        refs: Iterable[Any],
        limit: int = 20,
    ) -> SyncResult:
        """RG04: project an explicit, already-qualified set of refs.

        The caller (the per-turn postprocess sweep) gets these refs from
        ``turn_experience_refs``, i.e. the same SQL predicate as the global
        enumeration.  This method deliberately does not touch
        ``experience_bridge_state.cursor``: that cursor remains the catch-up
        watermark for the explicit ``memory.bridge.sync`` path, and a per-turn
        projection must not move a global scan position past an unprojected
        page.
        """

        if type(limit) is not int or not 1 <= limit <= 100:
            raise BridgeError("limit must be an integer between 1 and 100")
        result = SyncResult()
        for ref in list(refs)[:limit]:
            self._sync_one(access, namespace, ref, result)
        result.pending = self._store.pending_delivery_count(namespace)
        return result

    def _sync_one(
        self,
        access: ExperienceAccess,
        namespace: str,
        ref: Any,
        result: SyncResult,
    ) -> None:
        """Shared single-ref projection used by ``sync`` and ``sync_refs``."""

        result.scanned += 1
        view = self._store.read_experience(access, ref=ref, purpose=PURPOSE)
        if isinstance(view, ExperienceUnavailable):
            self._persist_gap(namespace, ref, "unavailable")
            result.gaps.append({"ref": dict(ref), "reason": "unavailable"})
            return
        try:
            self._project_one(access, view)
        except _Skip as skip:
            if skip.reason == "existing":
                result.skipped_existing += 1
            elif skip.reason == "deleted":
                self._persist_gap(namespace, view.ref, "deleted")
                result.skipped_deleted += 1
            else:
                self._persist_gap(namespace, view.ref, skip.reason)
                result.blocked += 1
                result.gaps.append({"ref": dict(view.ref), "reason": skip.reason})
            return
        result.projected += 1

    def _persist_gap(
        self, namespace: str, ref: dict[str, Any], reason: str
    ) -> None:
        if ref.get("ref_type") == "collab_input":
            ref_type = "collab_input"
            ref_id = ref["input_id"]
            ref_version = ref["content_version"]
        else:
            ref_type = "collab_artifact"
            ref_id = ref["artifact_id"]
            ref_version = ref["artifact_version"]
        self._store.record_bridge_gap(
            namespace, ref_type, ref_id, ref_version, reason
        )

    def _project_one(self, access: ExperienceAccess, view: ExperienceView) -> None:
        ref = view.ref
        with self._store.transaction():
            meta = self._store._projection_metadata_locked(access, ref)
            if meta is None:
                raise _Skip("unavailable")
            if meta["deleted"]:
                raise _Skip("deleted")
            if meta["unresolvable"]:
                raise _Skip("unresolvable_privacy")
            if meta["retention"] not in ALLOWED_RETENTION:
                raise _Skip("retention_not_supported")
            privacy = meta["privacy_classes"]
            if not isinstance(privacy, frozenset) or len(privacy) != 1:
                raise _Skip("multi_or_empty_privacy")
            if not privacy <= access.privacy_classes:
                raise _Skip("privacy_not_authorized")
            single_privacy = next(iter(privacy))
            if self._mapping_for_ref(access.namespace, meta):
                raise _Skip("existing")
            role, origin, principal = _role_and_origin(meta, access)
            if role is None:
                raise _Skip("unprovable_origin")
            body = view.body
            if body is None:
                raise _Skip("body_unavailable")
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            try:
                roots, dependencies, root_verified = _resolve_roots(
                    self._store, access.namespace, meta
                )
            except BridgeError:
                raise _Skip("unresolvable_roots") from None
            source_event_key = _source_event_key(
                access.host_instance,
                access.namespace,
                meta["ref_type"],
                meta["ref_id"],
                meta["ref_version"],
            )
            source_revision = 1
            delivery_id = delivery_id_for(
                access.namespace, source_event_key, source_revision
            )
            now = utc_now_rfc3339()
            event_payload = {
                "experience_ref": {
                    "ref_type": meta["ref_type"],
                    "ref_id": meta["ref_id"],
                    "ref_version": meta["ref_version"],
                },
                "projection_format": PROJECTION_FORMAT_VERSION,
                "privacy_classes": sorted(privacy),
                "root_source_key": _root_key(
                    access.host_instance, access.namespace, roots, root_verified
                ),
                "root_verified": root_verified,
                "source_dependencies": dependencies,
                "content_digest": digest,
            }
            source_event_id = (
                f"{meta['ref_type']}:{meta['ref_id']}:{meta['ref_version']}"
            )[:512]
            receipt = self._store.ingest_event(
                namespace=access.namespace,
                source_kind=_SOURCE_KIND[(meta["ref_type"], meta["origin_kind"])],
                source_instance=access.host_instance,
                source_event_id=source_event_id,
                event_type=EVENT_TYPE,
                observed_at=meta["created_at"],
                payload=event_payload,
                privacy_class=single_privacy,
                recorded_at=now,
            )
            pcs_time = parse_event_time(
                body if self.config.time_enabled else "",
                observed_at=meta.get("observed_at"),
                recorded_at=meta["created_at"],
                timezone_name=self.config.timezone,
                origin_kind=str(meta["origin_kind"]),
                ref_type=str(meta["ref_type"]),
            )
            request = {
                "protocol_version": "1.1",
                "source_event_key": source_event_key,
                "source_revision": source_revision,
                "origin": origin,
                "role": role,
                "occurred_at": meta["created_at"],
                "recorded_at": now,
                "time_precision": "instant" if meta["created_at"] else "unknown",
                "capture_state": "complete",
                "evidence_refs": [
                    f"{item['ref_type']}:{item['ref_id']}@{item['ref_version']}"
                    for item in dependencies
                ],
                "scope_id": self.config.scope_id,
                "content_digest": digest,
                "projection_format": PROJECTION_FORMAT_VERSION,
                # PCS-side only: never forwarded to the upstream capture (see
                # ``_deliver_one``'s whitelist).  Stored in the existing
                # ``request_json`` column, so no migration is needed.
                "pcs_time": pcs_time,
            }
            self._store.record_projection_locked(
                namespace=access.namespace,
                ref_type=meta["ref_type"],
                ref_id=meta["ref_id"],
                ref_version=meta["ref_version"],
                event_id=receipt.event_id,
                source_event_key=source_event_key,
                source_revision=source_revision,
                privacy_classes=tuple(sorted(privacy)),
                retention=meta["retention"],
                origin=origin,
                role=role,
                principal_kind=principal,
                root_source_key=event_payload["root_source_key"],
                root_members=roots,
                root_verified=root_verified,
                source_dependencies=dependencies,
                request=request,
                delivery_id=delivery_id,
            )
            self._store.resolve_bridge_gap(
                access.namespace,
                meta["ref_type"],
                meta["ref_id"],
                meta["ref_version"],
            )

    def _mapping_for_ref(self, namespace: str, meta: dict[str, Any]) -> None:
        row = self._store._require_connection().execute(
            "SELECT 1 FROM experience_source_mapping WHERE namespace = ? "
            "AND ref_type = ? AND ref_id = ? AND ref_version = ?",
            (namespace, meta["ref_type"], meta["ref_id"], meta["ref_version"]),
        ).fetchone()
        return row

    # ------------------------------------------------------------- deliver

    def deliver(
        self,
        access: ExperienceAccess,
        *,
        namespace: str,
        limit: int | None = None,
        capture_timeout_seconds: float | None = None,
    ) -> DeliveryResult:
        result = DeliveryResult()
        batch = min(limit or self.config.max_batch, self.config.max_batch)
        if capture_timeout_seconds is None:
            capture_cap = self.config.capture_timeout_seconds
        else:
            if isinstance(capture_timeout_seconds, bool) or not isinstance(
                capture_timeout_seconds, (int, float)
            ):
                raise BridgeError("capture_timeout_seconds must be a positive number")
            capture_cap = float(capture_timeout_seconds)
            if capture_cap <= 0:
                raise BridgeError("capture_timeout_seconds must be positive")
        rows = self._store.claim_delivery(
            namespace,
            lease_seconds=self.config.lease_seconds,
            limit=batch,
        )
        result.claimed = len(rows)
        started = time.monotonic()
        for row in rows:
            remaining = self.config.batch_timeout_seconds - (
                time.monotonic() - started
            )
            if remaining <= 0:
                self._store.finish_delivery(
                    delivery_id=row["delivery_id"],
                    lease_token=row["lease_token"],
                    state="pending",
                    error="batch_timeout",
                )
                result.pending += 1
                continue
            timeout = min(capture_cap, remaining)
            try:
                outcome = self._deliver_one(access, row, timeout)
            except Exception as exc:  # noqa: BLE001 - bounded failure is a state
                self._store.finish_delivery(
                    delivery_id=row["delivery_id"],
                    lease_token=row["lease_token"],
                    state="needs_review",
                    error=type(exc).__name__,
                )
                result.needs_review += 1
                result.errors.append(type(exc).__name__)
                continue
            setattr(result, outcome, getattr(result, outcome) + 1)
        return result

    def _deliver_one(
        self, access: ExperienceAccess, row: dict[str, Any], timeout_seconds: float
    ) -> str:
        namespace = row["namespace"]
        if not self._store.projection_is_usable(namespace, row["event_id"]):
            self._store.finish_delivery(
                delivery_id=row["delivery_id"],
                lease_token=row["lease_token"],
                state="obsolete",
                error="source_invalid",
            )
            return "obsolete"
        request = json.loads(row["request_json"])
        ref = {
            "ref_type": row["ref_type"],
            "namespace": namespace,
            "input_id": row["ref_id"],
            "content_version": row["ref_version"],
        }
        if row["ref_type"] == "collab_artifact":
            ref = {
                "ref_type": "collab_artifact",
                "namespace": namespace,
                "artifact_id": row["ref_id"],
                "artifact_version": row["ref_version"],
            }
        view = self._store.read_experience(access, ref=ref, purpose=PURPOSE)
        if isinstance(view, ExperienceUnavailable) or view.body is None:
            self._store.finish_delivery(
                delivery_id=row["delivery_id"],
                lease_token=row["lease_token"],
                state="obsolete",
                error="source_unavailable",
            )
            return "obsolete"
        digest = hashlib.sha256(view.body.encode("utf-8")).hexdigest()
        if digest != request["content_digest"]:
            self._store.finish_delivery(
                delivery_id=row["delivery_id"],
                lease_token=row["lease_token"],
                state="needs_review",
                error="content_changed",
            )
            return "needs_review"
        mapping = self._store.projection_for_event(namespace, row["event_id"])
        if mapping is None or mapping["origin"] != request["origin"]:
            self._store.finish_delivery(
                delivery_id=row["delivery_id"],
                lease_token=row["lease_token"],
                state="blocked",
                error="origin_mapping_mismatch",
            )
            return "blocked"
        if (
            view.retention not in ALLOWED_RETENTION
            or view.retention != mapping["retention"]
        ):
            self._store.finish_delivery(
                delivery_id=row["delivery_id"],
                lease_token=row["lease_token"],
                state="blocked",
                error="retention_changed",
            )
            return "blocked"
        event = {
            "protocol_version": "1.1",
            "source_event_key": request["source_event_key"],
            "source_revision": request["source_revision"],
            "origin": request["origin"],
            "role": request["role"],
            "content": view.body,
            "occurred_at": request["occurred_at"],
            "recorded_at": request["recorded_at"],
            "time_precision": request["time_precision"],
            "capture_state": request["capture_state"],
            "evidence_refs": request["evidence_refs"],
        }
        outcome = self._adapter.capture(
            event,
            actor_origin=request["origin"],
            timeout_seconds=timeout_seconds,
        )
        kind = outcome.get("kind")
        if kind == "ok":
            if not self._store.projection_is_usable(namespace, row["event_id"]):
                self._store.finish_delivery(
                    delivery_id=row["delivery_id"],
                    lease_token=row["lease_token"],
                    state="obsolete",
                    error="source_deleted_during_capture",
                )
                return "obsolete"
            receipt = outcome["receipt"]
            disposition = receipt.get("disposition")
            durability = receipt.get("durability")
            if disposition in ("inserted", "duplicate") and durability == "persisted":
                refs = receipt.get("event_refs") or []
                external_ref = refs[0].get("ref") if refs else None
                external_revision = refs[0].get("revision") if refs else None
                self._store.finish_delivery(
                    delivery_id=row["delivery_id"],
                    lease_token=row["lease_token"],
                    state="applied",
                    receipt=receipt,
                    external_ref=external_ref,
                    external_revision=external_revision,
                )
                return "applied"
            if disposition == "conflict":
                self._store.finish_delivery(
                    delivery_id=row["delivery_id"],
                    lease_token=row["lease_token"],
                    state="needs_review",
                    receipt=receipt,
                    error="conflict",
                )
                return "needs_review"
            self._schedule_retry(row, "receipt_not_persisted")
            return "pending"
        if kind in ("unavailable", "unknown", "timeout"):
            self._schedule_retry(row, kind)
            return "pending"
        if kind == "blocked":
            self._store.finish_delivery(
                delivery_id=row["delivery_id"],
                lease_token=row["lease_token"],
                state="blocked",
                error=str(outcome.get("error")),
            )
            return "blocked"
        self._schedule_retry(row, "adapter_error")
        return "pending"

    def _schedule_retry(self, row: dict[str, Any], reason: str) -> None:
        attempt = int(row["attempt"])
        if attempt >= self.config.max_attempts:
            self._store.finish_delivery(
                delivery_id=row["delivery_id"],
                lease_token=row["lease_token"],
                state="needs_review",
                error=reason,
            )
            return
        delay = min(
            self.config.retry_backoff_seconds * (2 ** max(0, attempt - 1)), 3600
        )
        self._store.finish_delivery(
            delivery_id=row["delivery_id"],
            lease_token=row["lease_token"],
            state="pending",
            error=reason,
            next_retry_at=_plus_seconds(utc_now_rfc3339(), delay),
        )


class _Skip(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def role_for_origin(ref_type: str, origin_kind: str) -> str:
    """Who spoke, from an experience ref and the origin it recorded.

    The role half of :func:`_role_and_origin`, published so the read side can
    label a memory hit with the same speaker the delivery wrote.  ``""`` means
    "not classifiable" and must stay unpublished rather than defaulting to the
    user -- an unknown speaker is not the person typing.
    """

    if ref_type == "collab_input":
        if origin_kind == "user_statement":
            return "user"
        if origin_kind == "host_supplied":
            return "system"
        return ""
    if ref_type == "collab_artifact":
        if origin_kind == "model_output":
            return "assistant"
        if origin_kind == "deterministic_query":
            return "tool"
        return ""
    return ""


def _role_and_origin(
    meta: dict[str, Any], access: ExperienceAccess
) -> tuple[str | None, str, str]:
    if meta["ref_type"] == "collab_input":
        if meta["origin_kind"] == "user_statement":
            bound = (
                meta["host_instance"] == access.host_instance
                and meta["access_ref"] == access.access_ref
            )
            if bound:
                return "user", "human_direct", "host_bound_user"
            return "user", "imported", "unverified"
        if meta["origin_kind"] == "host_supplied":
            return "system", "host_generated", "host"
        return None, "", ""
    if meta["origin_kind"] == "model_output":
        return "assistant", "assistant_visible", "assistant"
    if meta["origin_kind"] == "deterministic_query":
        if meta["source_refs"]:
            return "tool", "tool_observation", "tool"
        return "tool", "origin_unknown", "unverified"
    return None, "", ""


def _source_event_key(
    host_instance: str, namespace: str, ref_type: str, ref_id: str, version: int
) -> str:
    raw = canonical_parts(
        ["pcs-experience-v1", host_instance, namespace, ref_type, ref_id, str(version)]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def canonical_parts(parts: Iterable[str]) -> str:
    return json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))


def _root_key(
    host_instance: str,
    namespace: str,
    roots: list[dict[str, Any]],
    verified: bool,
) -> str:
    if not verified:
        raw = canonical_parts(
            ["unverified-root", host_instance, namespace, json.dumps(roots, sort_keys=True)]
        )
    else:
        raw = canonical_parts(
            [
                "root",
                host_instance,
                namespace,
                json.dumps(
                    sorted(
                        f"{r['ref_type']}:{r['ref_id']}"
                        for r in roots
                    ),
                    ensure_ascii=False,
                ),
            ]
        )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolve_roots(
    store: Any, namespace: str, meta: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    leaves: dict[tuple[str, str, int], dict[str, Any]] = {}
    dependencies: dict[tuple[str, str, int], dict[str, Any]] = {}
    stack: set[tuple[str, str, int]] = set()

    def walk(ref_type: str, ref_id: str, ref_version: int, depth: int) -> None:
        if depth > MAX_ROOT_DEPTH:
            raise BridgeError("source closure too deep")
        key = (ref_type, ref_id, ref_version)
        if key in stack:
            raise BridgeError("source closure cycle")
        if key in dependencies:
            return
        dependencies[key] = {
            "ref_type": ref_type,
            "ref_id": ref_id,
            "ref_version": ref_version,
        }
        stack.add(key)
        try:
            if ref_type == "collab_artifact":
                links = store._artifact_source_refs(
                    namespace=namespace,
                    artifact_id=ref_id,
                    artifact_version=ref_version,
                )
                for link in links:
                    kind = link["ref_type"]
                    if kind == "event":
                        walk("event", link["event_id"], 0, depth + 1)
                    elif kind == "record":
                        walk(
                            "record",
                            link["record_id"],
                            link["record_version"],
                            depth + 1,
                        )
                    elif kind == "collab_input":
                        walk(
                            "collab_input",
                            link["input_id"],
                            link["content_version"],
                            depth + 1,
                        )
                    elif kind == "collab_artifact":
                        walk(
                            "collab_artifact",
                            link["artifact_id"],
                            link["artifact_version"],
                            depth + 1,
                        )
            else:
                leaves[key] = dict(dependencies[key])
        finally:
            stack.discard(key)

    walk(meta["ref_type"], meta["ref_id"], meta["ref_version"], 0)
    ordered_leaves = [leaves[key] for key in sorted(leaves)]
    ordered_dependencies = [dependencies[key] for key in sorted(dependencies)]
    if meta["ref_type"] == "collab_input":
        verified = True
    elif meta["ref_type"] == "collab_artifact":
        verified = bool(meta["source_refs"])
    else:
        verified = bool(ordered_leaves)
    return ordered_leaves, ordered_dependencies, verified


def _plus_seconds(now_rfc3339: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    parsed = datetime.fromisoformat(now_rfc3339.replace("Z", "+00:00"))
    return (
        parsed + timedelta(seconds=float(seconds))
    ).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "BridgeConfig",
    "BridgeError",
    "DeliveryResult",
    "ExperienceBridge",
    "SyncResult",
]
