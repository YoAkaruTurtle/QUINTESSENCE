"""Bounded instance-local continuity with live PCS Experience reads."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..domains.cognition.maintenance_triggers import (
    MAX_TRIGGER_PAGE,
    TriggerCandidate,
    evaluate_triggers,
)
from ..domains.cognition.questions import QuestionView
from ..domains.memory.time_window import parse_timestamp
from ..domains.refs import RecordRef
from ..domains.scope import scope_matches
from ..foundation import identifiers
from ..foundation.errors import CorruptStore, InvalidInput, RecordNotFound, Redacted
from ..foundation.jsonutil import canonical_json_text, strict_json_loads
from ..store.experience import (
    CollaborationRef,
    DreamingExperienceRef,
    DreamingReadAudit,
    ExperienceAccess,
    ExperienceMixin,
    ExperienceView,
)
from .dreaming import DreamingState, _stamp, _utc


RECENT_WINDOW = timedelta(hours=24)
_REFRESH_OVERLAP = timedelta(minutes=15)
_RECONCILE_AFTER = timedelta(hours=1)
_FIRST_EXPERIENCE = datetime(1900, 1, 1, tzinfo=UTC)
_PAGE_SIZE = 20
_MAX_ITEMS = 12
_MAX_EXCERPT_CHARS = 480
_MAX_RECENT_REFS = 120
_AUDIT_KEEP = timedelta(days=7)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS dreaming_history_progress (
    scope_key TEXT NOT NULL,
    stream TEXT NOT NULL,
    since TEXT NOT NULL,
    until TEXT NOT NULL,
    cursor TEXT,
    generation INTEGER NOT NULL,
    reconciled_at TEXT,
    PRIMARY KEY (scope_key, stream)
);
CREATE TABLE IF NOT EXISTS dreaming_recent_refs (
    scope_key TEXT NOT NULL,
    ref_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    PRIMARY KEY (scope_key, ref_json)
);
CREATE INDEX IF NOT EXISTS dreaming_recent_order
    ON dreaming_recent_refs (scope_key, occurred_at, ref_json);
CREATE TABLE IF NOT EXISTS dreaming_manual_review (
    scope_key TEXT PRIMARY KEY,
    subject_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dreaming_reviewed_triggers (
    scope_key TEXT NOT NULL,
    subject_json TEXT NOT NULL,
    trigger_json TEXT NOT NULL,
    PRIMARY KEY (scope_key, subject_json, trigger_json)
);
CREATE TABLE IF NOT EXISTS dreaming_history_reads (
    read_id INTEGER PRIMARY KEY,
    namespace TEXT NOT NULL,
    host_instance TEXT NOT NULL,
    access_ref TEXT NOT NULL,
    purpose TEXT NOT NULL,
    ref_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    read_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dreaming_history_reads_time ON dreaming_history_reads (read_at);
"""


@dataclass(frozen=True)
class HistoryItem:
    source: DreamingExperienceRef
    excerpt: str
    origin_kind: str
    processing: str
    truncated: bool


@dataclass(frozen=True)
class RecentHistory:
    items: tuple[HistoryItem, ...]


@dataclass(frozen=True)
class RecentAdvance:
    added: int
    has_more: bool
    refs: tuple[DreamingExperienceRef, ...] = ()


@dataclass(frozen=True)
class _Progress:
    since: datetime
    until: datetime
    cursor: str | None
    generation: int
    reconciled_at: datetime | None = None


@dataclass(frozen=True)
class DeepBatch:
    question: QuestionView | None = None
    trigger: TriggerCandidate | None = None
    items: tuple[HistoryItem, ...] = ()
    next_cursor: str | None = None
    trigger_sources: tuple[RecordRef, ...] = ()
    _progress: _Progress | None = None
    _scope_key: str | None = None
    _stream: str = "deep"

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None

    @property
    def checkpointable(self) -> bool:
        return self._scope_key is not None


@dataclass(frozen=True)
class _QuestionTriggerView:
    ref: RecordRef
    paused: bool
    review_after: str | None
    last_evaluated_at: str | None
    source_revision: int | None


def _excerpt(body: str) -> tuple[str, bool]:
    body = body.strip()
    if len(body) > _MAX_EXCERPT_CHARS:
        return body[:_MAX_EXCERPT_CHARS - 1] + "…", True
    return body, False


def read_history_item(
    store: ExperienceMixin,
    access: ExperienceAccess,
    source: DreamingExperienceRef,
    *,
    purpose: str,
    audit: DreamingReadAudit,
) -> HistoryItem | None:
    """Read a bounded, audited excerpt through the PCS exact-ref reader."""
    view = store.read_experience_for_dreaming(access, ref=source.ref, purpose=purpose, audit=audit)
    if not isinstance(view, ExperienceView) or view.body is None:
        return None
    text, truncated = _excerpt(view.body)
    if not text:
        return None
    return HistoryItem(source, text, view.origin_kind, view.processing, truncated)


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_ITEMS:
        raise InvalidInput(f"Dreaming history limit must be 1..{_MAX_ITEMS}")
    return value


def plan_deep_batch(
    views: Sequence[QuestionView],
    history: Sequence[HistoryItem],
    *,
    now: datetime,
    source_revisions: Mapping[str, int] | None = None,
    manual_refs: Iterable[Mapping[str, Any]] = (),
    limit: int = _MAX_ITEMS,
    completed_triggers: frozenset[tuple[str, str]] = frozenset(),
    source_changes: Mapping[RecordRef, tuple[tuple[RecordRef, RecordRef], ...]] | None = None,
) -> DeepBatch:
    """Select one existing PCS trigger or one already authorized history batch."""

    limit = _limit(limit)
    eligible = {view.ref: view for view in views if view.status in ("open", "waiting")}
    requested = {RecordRef.from_value(ref) for ref in manual_refs}
    changed = source_changes or {}
    basis = dict(source_revisions or {})
    automatic = []
    for ref, view in eligible.items():
        sources = changed.get(ref, ())
        current = getattr(view, "source_revision", None)
        if sources:
            previous, newest = sources[0]
            basis[ref.record_id] = previous.record_version
            current = newest.record_version
        automatic.append(_QuestionTriggerView(
            ref, view.paused, _normalised_timestamp(view.review_after),
            _normalised_timestamp(view.last_evaluated_at), current,
        ))
    stamp = _stamp(_utc(now))
    manual = evaluate_triggers(
        views=[view for view in automatic if view.ref in requested], now=stamp,
        manual_refs=[ref.to_dict() for ref in requested],
    )
    source_candidates = tuple(
        trigger for trigger in evaluate_triggers(views=automatic, now=stamp, source_revisions=basis)
        if trigger.reason == "source_revision"
    ) if basis else ()
    due = evaluate_triggers(views=automatic, now=stamp)
    for trigger in (*manual, *source_candidates, *due):
        ref = RecordRef.from_value(trigger.subject_ref)
        if trigger.reason == "source_revision" and ref in changed:
            sources = tuple(newest for _previous, newest in changed[ref])
            # Exact identities distinguish equal revision numbers on different
            # declared sources and subsequent revisions of the same source.
            trigger = replace(trigger, detail=canonical_json_text([source.to_dict() for source in sources]))
        else:
            sources = ()
        subject = canonical_json_text(dict(trigger.subject_ref))
        if (subject, _trigger_key(trigger)) not in completed_triggers:
            return DeepBatch(question=eligible[ref], trigger=trigger, trigger_sources=sources)
    return DeepBatch(items=tuple(history[:limit]))


def _trigger_key(trigger: TriggerCandidate) -> str:
    return canonical_json_text([trigger.reason, trigger.detail])


def _normalised_timestamp(value: str | None) -> str | None:
    parsed = parse_timestamp(value)
    return None if parsed is None else _stamp(parsed.astimezone(UTC))


def _question_source_changes(
    store: Any, access: ExperienceAccess, views: Sequence[QuestionView],
) -> dict[RecordRef, tuple[tuple[RecordRef, RecordRef], ...]]:
    changes = {}
    for view in views:
        declared = tuple(
            RecordRef.from_value(ref) for ref in view.trigger_spec.get("source_refs", ())
            if ref.get("ref_type") == "record" and ref.get("namespace") == access.namespace
        )
        if not declared or view.paused or view.status not in ("open", "waiting"):
            continue
        pairs = []
        for previous in declared:
            if previous.record_id == view.ref.record_id or store.record_is_deleted(access.namespace, previous.record_id):
                continue
            try:
                recorded = store.get_version(access.namespace, previous.record_id, previous.record_version)
                current = store.get_current(access.namespace, previous.record_id)
            except (RecordNotFound, Redacted):
                continue
            if (
                current.record_version <= previous.record_version
                or current.privacy_class != recorded.privacy_class
                or current.privacy_class not in access.privacy_classes
                or not scope_matches(current.scope or {}, view.scope)
            ):
                continue
            pairs.append((previous, RecordRef(access.namespace, current.record_id, current.record_version)))
        if pairs:
            changes[view.ref] = tuple(sorted(set(pairs), key=lambda pair: (
                pair[0].record_id, pair[0].record_version, pair[1].record_version,
            )))
    return changes


class DreamingHistory:
    """The history ledger stores refs, occurrence times, cursors and read receipts."""

    def __init__(self, directory: Path, *, instance_id: str, namespace: str) -> None:
        self.namespace = identifiers.require_namespace(namespace)
        self._state = DreamingState(directory, instance_id=instance_id)
        with closing(self._state._connect()) as connection:
            connection.executescript(_SCHEMA)

    def _scope_key(self, access: ExperienceAccess) -> str:
        if access.namespace != self.namespace:
            raise InvalidInput("Dreaming history cannot cross its instance namespace")
        return canonical_json_text([
            access.namespace, access.host_instance, access.access_ref,
            sorted(access.privacy_classes),
        ])

    def _record_audit(
        self,
        connection: sqlite3.Connection,
        access: ExperienceAccess,
        ref: CollaborationRef,
        purpose: str,
        outcome: str,
        now: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO dreaming_history_reads "
            "(namespace, host_instance, access_ref, purpose, ref_json, outcome, read_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (access.namespace, access.host_instance, access.access_ref, purpose,
             canonical_json_text(ref), outcome, _stamp(now)),
        )

    def audit(self, access: ExperienceAccess, ref: CollaborationRef, purpose: str, outcome: str) -> None:
        """Commit one exact PCS read receipt in the owned instance ledger."""
        if access.namespace != self.namespace or ref.get("namespace") != self.namespace:
            raise InvalidInput("Dreaming read audit cannot cross the instance namespace")
        with closing(self._state._connect()) as connection:
            self._record_audit(connection, access, ref, purpose, outcome, datetime.now(UTC))

    def _progress(self, connection: sqlite3.Connection, key: str, stream: str) -> _Progress | None:
        row = connection.execute(
            "SELECT * FROM dreaming_history_progress WHERE scope_key = ? AND stream = ?",
            (key, stream),
        ).fetchone()
        if row is None:
            return None
        since = parse_timestamp(row["since"])
        until = parse_timestamp(row["until"])
        if since is None or until is None or since > until:
            raise CorruptStore("invalid Dreaming history checkpoint")
        return _Progress(since, until, row["cursor"], row["generation"],
                         parse_timestamp(row["reconciled_at"]))

    def _write_progress(
        self, connection: sqlite3.Connection, key: str, stream: str, progress: _Progress,
    ) -> None:
        connection.execute(
            "INSERT INTO dreaming_history_progress VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope_key, stream) DO UPDATE SET since = excluded.since, "
            "until = excluded.until, cursor = excluded.cursor, "
            "generation = excluded.generation, reconciled_at = excluded.reconciled_at",
            (key, stream, _stamp(progress.since), _stamp(progress.until), progress.cursor,
             progress.generation + 1,
             None if progress.reconciled_at is None else _stamp(progress.reconciled_at)),
        )

    def advance_recent(
        self, store: ExperienceMixin, access: ExperienceAccess, *, now: datetime,
        projection_key: str | None = None,
        accept_page: Callable[[tuple[DreamingExperienceRef, ...]], bool] | None = None,
    ) -> RecentAdvance:
        """Commit one page and its watermark together; a later tick can resume it."""

        now = _utc(now)
        floor = now - RECENT_WINDOW
        key = self._scope_key(access)
        stream = "recent" if projection_key is None else f"recent-projection:{projection_key}"
        with closing(self._state._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = self._progress(connection, key, stream)
            if previous is None:
                progress = _Progress(floor, now, None, 0, now)
            elif previous.cursor is not None and floor < previous.until <= now:
                progress = replace(previous, since=max(floor, previous.since))
            elif previous.until == now:
                return RecentAdvance(0, False)
            elif (
                previous.reconciled_at is None or previous.reconciled_at > now
                or now - previous.reconciled_at >= _RECONCILE_AFTER
                or previous.until <= floor or previous.until > now
            ):
                # Catch delayed eligibility and backfilled occurrence times.
                progress = _Progress(floor, now, None, previous.generation, now)
            else:
                start = max(floor, previous.until - _REFRESH_OVERLAP)
                progress = _Progress(start, now, None, previous.generation, previous.reconciled_at)
            page = store.dreaming_experience_page(
                access, since=progress.since, until=progress.until,
                cursor=progress.cursor, limit=_PAGE_SIZE,
            )
            connection.execute("DELETE FROM dreaming_recent_refs WHERE occurred_at < ?", (_stamp(floor),))
            added = 0
            for source in page.refs:
                added += connection.execute(
                    "INSERT OR IGNORE INTO dreaming_recent_refs VALUES (?, ?, ?)",
                    (key, canonical_json_text(source.ref), _stamp(source.occurred_at)),
                ).rowcount
            connection.execute(
                "DELETE FROM dreaming_recent_refs WHERE scope_key = ? AND ref_json IN ("
                "SELECT ref_json FROM dreaming_recent_refs WHERE scope_key = ? "
                "ORDER BY occurred_at DESC, ref_json DESC LIMIT -1 OFFSET ?)",
                (key, key, _MAX_RECENT_REFS),
            )
            if accept_page is None:
                self._write_progress(connection, key, stream, replace(progress, cursor=page.next_cursor))
            connection.execute("COMMIT")
        if accept_page is not None:
            # Source reads/embedding may audit into this ledger. Release the
            # checkpoint transaction while the upstream worker runs.
            if not accept_page(page.refs):
                return RecentAdvance(added, True, page.refs)
            with closing(self._state._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                unchanged = self._progress(connection, key, stream) == previous
                if unchanged:
                    self._write_progress(connection, key, stream, replace(progress, cursor=page.next_cursor))
                connection.execute("COMMIT")
            if not unchanged:
                return RecentAdvance(added, True, page.refs)
        return RecentAdvance(added, page.next_cursor is not None, page.refs)

    def _materialize(
        self,
        store: ExperienceMixin,
        access: ExperienceAccess,
        refs: Iterable[CollaborationRef],
        *,
        now: datetime,
        since: datetime,
        until: datetime,
        purpose: str,
        limit: int,
    ) -> tuple[HistoryItem, ...]:
        items: list[HistoryItem] = []
        with closing(self._state._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")

            def audit(
                authority: ExperienceAccess, ref: CollaborationRef, use: str, outcome: str,
            ) -> None:
                self._record_audit(connection, authority, ref, use, outcome, now)

            connection.execute(
                "DELETE FROM dreaming_history_reads WHERE read_at < ?", (_stamp(now - _AUDIT_KEEP),),
            )
            for ref in refs:
                source = store.dreaming_experience_ref(access, ref=ref)
                if source is None or not since <= source.occurred_at < until:
                    continue
                item = read_history_item(store, access, source, purpose=purpose, audit=audit)
                if item is not None:
                    items.append(item)
                if len(items) == limit:
                    break
            connection.execute("COMMIT")
        return tuple(items)

    def recent(
        self,
        store: ExperienceMixin,
        access: ExperienceAccess,
        *,
        now: datetime,
        purpose: str = "consult",
        limit: int = _MAX_ITEMS,
        include_cached: bool = True,
    ) -> RecentHistory:
        """Combine the cursor's recent refs and a bounded raw page, newest first."""

        now = _utc(now)
        limit = _limit(limit)
        floor = now - RECENT_WINDOW
        key = self._scope_key(access)
        if purpose not in access.purposes:
            return RecentHistory(())
        cached = []
        if include_cached:
            with closing(self._state._connect()) as connection:
                cached = connection.execute(
                    "SELECT ref_json, occurred_at FROM dreaming_recent_refs WHERE scope_key = ? "
                    "AND occurred_at >= ? AND occurred_at < ? ORDER BY occurred_at DESC, ref_json DESC "
                    "LIMIT ?", (key, _stamp(floor), _stamp(now), _PAGE_SIZE),
                ).fetchall()
        sources = {row["ref_json"]: row["occurred_at"] for row in cached}
        # Scan a short tail even with a cache. Fresh user turns can land after
        # the last idle tick. Ineligible tail rows must not hide all progress.
        cursor = None
        for _ in range(2):
            tail = store.dreaming_experience_page(
                access, since=floor, until=now, cursor=cursor, latest=True, limit=_PAGE_SIZE,
            )
            for source in tail.refs:
                sources[canonical_json_text(source.ref)] = _stamp(source.occurred_at)
            cursor = tail.next_cursor
            if len(tail.refs) >= limit or cursor is None:
                break
        ordered = sorted(sources, key=lambda ref: (sources[ref], ref), reverse=True)
        items = self._materialize(
            store, access, (strict_json_loads(ref) for ref in ordered),
            now=now, since=floor, until=now, purpose=purpose, limit=limit,
        )
        return RecentHistory(items)

    def refresh(
        self,
        store: ExperienceMixin,
        access: ExperienceAccess,
        sources: Sequence[DreamingExperienceRef],
        *,
        now: datetime,
        purpose: str,
    ) -> tuple[HistoryItem, ...]:
        """Revalidate an already selected batch at send and result delivery."""

        now = _utc(now)
        self._scope_key(access)
        if not sources or purpose not in access.purposes:
            return ()
        limit = _limit(len(sources))
        return self._materialize(
            store, access, (source.ref for source in sources), now=now,
            since=_FIRST_EXPERIENCE, until=now, purpose=purpose, limit=limit,
        )

    def queue_manual_review(self, access: ExperienceAccess, ref: RecordRef) -> None:
        if ref.namespace != self.namespace:
            raise InvalidInput("Dreaming question cannot cross its instance namespace")
        key = self._scope_key(access)
        with closing(self._state._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO dreaming_manual_review VALUES (?, ?) "
                "ON CONFLICT(scope_key) DO UPDATE SET subject_json = excluded.subject_json",
                (key, canonical_json_text(ref.to_dict())),
            )
            connection.execute(
                "DELETE FROM dreaming_reviewed_triggers WHERE scope_key = ? AND subject_json = ? "
                "AND trigger_json = ?",
                (key, canonical_json_text(ref.to_dict()), canonical_json_text(["manual_review", None])),
            )

    def queued_manual_review(self, access: ExperienceAccess) -> RecordRef | None:
        key = self._scope_key(access)
        with closing(self._state._connect()) as connection:
            row = connection.execute(
                "SELECT subject_json FROM dreaming_manual_review WHERE scope_key = ?", (key,),
            ).fetchone()
        return None if row is None else RecordRef.from_value(strict_json_loads(row["subject_json"]))

    def discard_manual_review(self, access: ExperienceAccess, ref: RecordRef) -> None:
        with closing(self._state._connect()) as connection:
            connection.execute(
                "DELETE FROM dreaming_manual_review WHERE scope_key = ? AND subject_json = ?",
                (self._scope_key(access), canonical_json_text(ref.to_dict())),
            )

    def deep(
        self,
        store: ExperienceMixin,
        access: ExperienceAccess,
        *,
        views: Sequence[QuestionView] = (),
        now: datetime,
        source_revisions: Mapping[str, int] | None = None,
        manual_refs: Iterable[Mapping[str, Any]] = (),
        purpose: str = "dreaming",
        limit: int = _MAX_ITEMS,
        projection_key: str | None = None,
        include_history: bool = True,
    ) -> DeepBatch:
        """Select a triggered Question, or read the next page of older Experience."""

        now = _utc(now)
        limit = _limit(limit)
        key = self._scope_key(access)
        if purpose not in access.purposes:
            return DeepBatch()
        stream = "deep" if projection_key is None else f"projection:{projection_key}"
        if len(views) > MAX_TRIGGER_PAGE:
            raise InvalidInput("Dreaming Questions must be read in bounded pages")
        allowed_views = tuple(view for view in views if view.ref.namespace == access.namespace)
        subjects = tuple(canonical_json_text(view.ref.to_dict()) for view in allowed_views)
        until = now - RECENT_WINDOW
        with closing(self._state._connect()) as connection:
            completed = frozenset()
            if subjects:
                placeholders = ", ".join("?" for _ in subjects)
                completed = frozenset(
                    (row["subject_json"], row["trigger_json"]) for row in connection.execute(
                        "SELECT subject_json, trigger_json FROM dreaming_reviewed_triggers WHERE scope_key = ? "
                        f"AND subject_json IN ({placeholders})", (key, *subjects),
                    )
                )
            previous = self._progress(connection, key, stream)
        selected = plan_deep_batch(
            allowed_views, (), now=now, source_revisions=source_revisions, manual_refs=manual_refs,
            limit=limit, completed_triggers=completed,
            source_changes=_question_source_changes(store, access, allowed_views),
        )
        if selected.question is not None:
            return replace(selected, _scope_key=key)
        if not include_history:
            return DeepBatch()
        if previous is None:
            progress = _Progress(_FIRST_EXPERIENCE, until, None, 0)
        elif previous.cursor is not None:
            progress = previous
        elif previous.until >= until:
            return DeepBatch()
        else:
            progress = _Progress(previous.until, until, None, previous.generation)
        page = store.dreaming_experience_page(
            access, since=progress.since, until=progress.until,
            cursor=progress.cursor, limit=limit,
        )
        items = self._materialize(
            store, access, (source.ref for source in page.refs), now=now,
            since=progress.since, until=progress.until, purpose=purpose, limit=limit,
        )
        return DeepBatch(
            items=items, next_cursor=page.next_cursor,
            _progress=replace(progress, cursor=page.next_cursor), _scope_key=key, _stream=stream,
        )

    def complete_deep(
        self, access: ExperienceAccess, batch: DeepBatch, *,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        """Advance a still-current page, optionally with the local wake receipt."""
        if connection is not None:
            return self._complete_deep_locked(connection, access, batch)
        with closing(self._state._connect()) as local, local:
            local.execute("BEGIN IMMEDIATE")
            return self._complete_deep_locked(local, access, batch)

    def _complete_deep_locked(
        self, connection: sqlite3.Connection, access: ExperienceAccess, batch: DeepBatch,
    ) -> bool:
        key = self._scope_key(access)
        if batch._scope_key != key:
            return False
        if batch.trigger is not None:
            subject = canonical_json_text(dict(batch.trigger.subject_ref))
            if batch.trigger.reason == "manual_review":
                pending = connection.execute(
                    "SELECT subject_json FROM dreaming_manual_review WHERE scope_key = ?", (key,),
                ).fetchone()
                if pending is not None and pending["subject_json"] != subject:
                    return False
            changed = connection.execute(
                "INSERT OR IGNORE INTO dreaming_reviewed_triggers VALUES (?, ?, ?)",
                (key, subject, _trigger_key(batch.trigger)),
            ).rowcount == 1
            if changed and batch.trigger.reason == "manual_review":
                connection.execute(
                    "DELETE FROM dreaming_manual_review WHERE scope_key = ? AND subject_json = ?",
                    (key, subject),
                )
            return changed
        if batch._progress is None:
            return False
        previous = self._progress(connection, key, batch._stream)
        generation = 0 if previous is None else previous.generation
        if generation != batch._progress.generation:
            return False
        self._write_progress(connection, key, batch._stream, batch._progress)
        return True
