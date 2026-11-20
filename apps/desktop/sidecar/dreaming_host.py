"""Resident Dreaming on the existing desktop database worker."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from dreaming_calendar import DesktopCalendar
from dreaming_semantic import DreamingSemanticHost
from quintessence_pcs.adapters.scope_recall.dreaming import require_dreaming_owner
from quintessence_pcs.conversation.persona import PersonaUnavailable, read_active_persona
from quintessence_pcs.domains.cognition.questions import (
    QUESTION_KIND,
    QuestionAccess,
    QuestionError,
    QuestionView,
)
from quintessence_pcs.domains.refs import RecordRef
from quintessence_pcs.domains.memory.time_window import (
    message_time, parse_journal_time_query, parse_time_query, resolve_zone,
)
from quintessence_pcs.domains.scope import scope_matches
from quintessence_pcs.foundation.errors import InvalidInput, RecordNotFound, Redacted, StorageError
from quintessence_pcs.foundation.jsonutil import canonical_json_text, sha256_hex
from quintessence_pcs.runtime.collab_protocol import ConsultSendPolicy
from quintessence_pcs.runtime.context import ContinuityMaterial
from quintessence_pcs.runtime.dreaming import DreamingState, MIN_IDLE, WakeProposal, source_ref_mapping
from quintessence_pcs.runtime.dreaming_history import DeepBatch, DreamingHistory, HistoryItem, RECENT_WINDOW
from quintessence_pcs.runtime.dreaming_journal import CalendarJournalStore, JournalEntry, JournalPeriod
from quintessence_pcs.runtime.dreaming_model import DREAMING_PURPOSE, DreamingModel, ReflectionMaterial
from quintessence_pcs.runtime.manifest import ModelManifest
from quintessence_pcs.runtime.model_port import ModelAdapterError, ModelNotAuthorizedError

if TYPE_CHECKING:
    from runtime import DesktopRuntime

_GRANT_PREFIX = "desktop-dreaming:"
_GRANT_CALLS = 120
_GRANT_LIFETIME = timedelta(days=30)
_SHALLOW_INTERVAL = 60.0
_SHALLOW_IDLE = 5.0
_PROJECTION_INTERVAL = 60.0
_PROJECTION_IDLE = 300.0
_NO_SOURCE = {"embedding_unconfigured", "purpose_not_allowed"}
_QUESTION_PAGE = 20
_JOURNAL_RETRY_DELAY = timedelta(minutes=1)


def consult_policy(runtime: DesktopRuntime) -> ConsultSendPolicy:
    authority = runtime.config.authority
    return ConsultSendPolicy(
        namespace=authority.namespace,
        host_instance=authority.host_instance,
        access_ref=authority.access_ref,
        privacy_classes=authority.privacy_classes,
        purposes=authority.purposes,
        disclose=authority.disclose,
    )


def _history_payload(items: tuple[HistoryItem, ...]) -> list[dict[str, Any]]:
    return [{
        "occurred_at": item.source.occurred_at.isoformat(),
        "time_basis": item.source.time_basis,
        "origin_kind": item.origin_kind,
        "processing": item.processing,
        "source": item.source.ref,
        "excerpt": item.excerpt,
    } for item in items]


class DreamingHost:
    """Share the worker with PCS; only the owned Dreaming component is written."""

    def __init__(
        self,
        current_runtime: Callable[[], DesktopRuntime | None],
        *,
        generation: str,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._current_runtime = current_runtime
        self._generation = generation
        self._monotonic = monotonic
        self._clock = clock
        self._lock = threading.Lock()
        self._foreground = 0
        self._idle_since = monotonic()
        self._cancel = threading.Event()
        self._stopped = False
        self._state: DreamingState | None = None
        self._history: DreamingHistory | None = None
        self._semantic: DreamingSemanticHost | None = None
        self._runtime_basis = None
        self._model: DreamingModel | None = None
        self._model_basis = None
        self._status = "waiting"
        self._reason: str | None = None
        self._enabled = False
        self._can_configure = False
        self._journals_enabled = False
        self._can_configure_journals = False
        self._journal_store: CalendarJournalStore | None = None
        self._journal_owner = None
        self._running_date = None
        self._calendar_basis = None
        self._run_day_lock = threading.Lock()
        self._calendar: DesktopCalendar | None = None
        self._shallow_due = 0.0
        self._projection_due = 0.0
        self._semantic_reason: str | None = None
        self._projection_refs: tuple[Mapping[str, Any], ...] = ()
        self._projection_key: str | None = None
        self._shallow_page: tuple[Mapping[str, Any], ...] | None = None
        self._question_cursor: str | None = None

    @property
    def _runtime(self) -> DesktopRuntime:
        runtime = self._current_runtime()
        if runtime is None:
            raise InvalidInput("Dreaming instance is unavailable")
        return runtime

    def foreground_queued(self) -> None:
        with self._lock:
            self._foreground += 1
            self._cancel.set()

    def foreground_finished(self) -> None:
        with self._lock:
            self._foreground -= 1
            if self._foreground == 0:
                self._idle_since = self._monotonic()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._cancel.set()

    def view(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": "foreground" if self._foreground else self._status,
                "reason": None if self._foreground else self._reason,
                "semantic_reason": self._semantic_reason,
                "enabled": self._enabled,
                "can_configure": self._can_configure,
                "journals_enabled": self._journals_enabled,
                "can_configure_journals": self._can_configure_journals,
            }

    def _report(self, status: str, reason: str | None = None) -> None:
        with self._lock:
            self._status = status
            self._reason = reason

    def _policy(self) -> ConsultSendPolicy:
        runtime = self._runtime
        policy = consult_policy(runtime)
        # Managed desktop hosts already own Consult privacy/identity. The extra
        # purpose authorizes local organization; provider calls separately need
        # their own explicit grant or metered, owner-configured embedding route.
        if runtime.config.managed and "consult" in policy.purposes:
            return replace(policy, purposes=policy.purposes | {DREAMING_PURPOSE})
        return policy

    def _components(self) -> tuple[DreamingState, DreamingHistory, DreamingSemanticHost]:
        runtime = self._runtime
        directory = runtime.dreaming_root
        if directory is None or runtime.instance_id is None:
            raise InvalidInput("Dreaming directory is unavailable")
        policy = self._policy()
        spec = runtime.config.memory_bridge
        scope_id = spec.scope_id if spec is not None and spec.enabled else policy.namespace
        require_dreaming_owner(directory, runtime.instance_id)
        basis = (runtime.store, directory, policy.namespace, scope_id)
        if self._runtime_basis != basis:
            state = DreamingState(directory, instance_id=runtime.instance_id)
            history = DreamingHistory(directory, instance_id=runtime.instance_id, namespace=policy.namespace)
            semantic = DreamingSemanticHost(
                directory, instance_id=runtime.instance_id, scope_id=scope_id, store=runtime.store,
                history=history, read_policy=self._policy,
            )
            self._state, self._history, self._semantic = state, history, semantic
            self._runtime_basis = basis
            self._projection_refs = ()
            self._projection_key = None
            self._shallow_page = None
        return self._state, self._history, self._semantic

    def _journals(self) -> CalendarJournalStore:
        runtime = self._runtime
        if runtime.dreaming_root is None or runtime.instance_id is None:
            raise InvalidInput("Dreaming directory is unavailable")
        owner = runtime.dreaming_root, runtime.instance_id
        if self._journal_owner != owner:
            self._journal_store = CalendarJournalStore(owner[0], instance_id=owner[1])
            self._journal_owner = owner
            self._running_date = None
        assert self._journal_store is not None
        return self._journal_store

    def record_running(self) -> None:
        """Witness actual runtime activity, including a day without chat."""
        if self._stopped:
            return
        try:
            runtime = self._runtime
            timezone = runtime.config.memory.timezone
            now = self._clock()
            marker = timezone, now.astimezone(resolve_zone(timezone)).date()
            with self._run_day_lock:
                if self._journal_owner == (runtime.dreaming_root, runtime.instance_id) and self._running_date == marker:
                    return
                self._journals().record_running(now, timezone)
                self._running_date = marker
        except (OSError, ValueError, StorageError, sqlite3.Error):
            # A calendar file conflict must not prevent normal instance startup.
            return

    def _calendar_service(self) -> DesktopCalendar:
        runtime = self._runtime
        basis = runtime, runtime.config.model.manifest
        if self._calendar_basis != basis:
            wake, history, semantic = self._components()
            self._calendar = DesktopCalendar(
                runtime, store=self._journals(), history=history, semantic=semantic,
                wake=wake, read_policy=self._policy,
            )
            self._calendar_basis = basis
        assert self._calendar is not None
        return self._calendar

    def _journal_route(self) -> str | None:
        config = self._runtime.config
        policy = self._policy()
        if not policy.disclose or DREAMING_PURPOSE not in policy.purposes or config.model.transport_kind != "http":
            return None
        manifest = config.model.manifest
        return sha256_hex(canonical_json_text([
            "calendar-journals", self._authorization_ref(), manifest.provider,
            manifest.model, manifest.base_url, manifest.api_key_env,
        ]))

    def _journal_authorization(self) -> str | None:
        basis = self._journal_route()
        try:
            enabled = basis is not None and self._journals().authorization() == basis
        except (OSError, ValueError, StorageError, sqlite3.Error):
            basis = None
            enabled = False
        with self._lock:
            self._can_configure_journals = basis is not None
            self._journals_enabled = enabled
        return basis if enabled else None

    def configure_journals(self, enabled: bool) -> dict[str, Any]:
        route = self._journal_route() if enabled else None
        if enabled and route is None:
            raise ModelNotAuthorizedError("the desktop host has no journal destination")
        self._journals().set_authorization(route)
        journals = self._journal_authorization()
        if journals is not None or self._enabled:
            self._report("waiting")
        else:
            self._report("disabled", "model_not_authorized")
        return self.view()

    def _manifest(self) -> ModelManifest | None:
        config = self._runtime.config
        policy = self._policy()
        if not policy.disclose or DREAMING_PURPOSE not in policy.purposes or config.model.transport_kind != "http":
            return None
        foreground = config.model.manifest
        manifest = foreground.to_dict()
        for key in ("tool_mode", "reasoning_effort", "protocol_version"):
            manifest.pop(key, None)
        # Inherit the chat output ceiling so the grant hash binds that limit.
        manifest.update(
            max_calls=_GRANT_CALLS,
            max_wall_seconds=min(20, foreground.max_wall_seconds),
            thinking="disabled",
            temperature=0,
        )
        candidate = ModelManifest.from_mapping(manifest)
        if candidate.manifest_hash == foreground.manifest_hash:
            # These grants use a separate PCS ledger. Distinct manifests also
            # keep an operator from binding one by mistake to foreground.
            manifest["max_calls"] -= 1
            candidate = ModelManifest.from_mapping(manifest)
        return candidate

    def _authorization_ref(self) -> str:
        runtime = self._runtime
        policy = self._policy()
        return _GRANT_PREFIX + sha256_hex(canonical_json_text([
            runtime.instance_id, policy.namespace, policy.host_instance,
            policy.access_ref, sorted(policy.privacy_classes),
            runtime.config.authority.operator, runtime.config.destination.destination_id,
        ]))

    def _read_grant(self, authorization_id: str):
        state, _, _ = self._components()
        with state.authorizations(self._runtime.store.migrations_dir) as ledger:
            if ledger is None:
                raise RecordNotFound("no Dreaming grant ledger")
            grant = ledger.model_authorization(authorization_id)
        if grant.authorization_ref != self._authorization_ref():
            raise RecordNotFound("Dreaming grant authority changed")
        return grant

    def _active_model(self, state: DreamingState) -> DreamingModel | None:
        manifest = self._manifest()
        config = self._runtime.config
        namespace = config.authority.namespace
        with state.authorizations(self._runtime.store.migrations_dir) as ledger:
            grant = None if ledger is None or manifest is None else ledger.active_model_authorization(
                namespace=namespace, provider=manifest.provider, model=manifest.model,
                manifest_hash=manifest.manifest_hash, authorization_ref=self._authorization_ref(),
                at=self._clock(),
            )
        exhausted = grant is not None and state.count_model_requests(grant.authorization_id) >= grant.max_calls
        with self._lock:
            self._can_configure = manifest is not None
            self._enabled = grant is not None and not exhausted
        if grant is None or exhausted:
            return None
        basis = (
            self._runtime.store, manifest, config.model.manifest, grant.authorization_id,
            config.request_budget_units, config.output_reserve_units,
        )
        if self._model is None or (not self._model.in_flight and self._model_basis != basis):
            self._model = DreamingModel(
                self._runtime.store,
                state,
                manifest,
                authorization_id=grant.authorization_id,
                foreground_manifest=config.model.manifest,
                read_policy=self._policy,
                read_authorization=self._read_grant,
                request_budget_units=config.request_budget_units,
                output_reserve_units=min(config.output_reserve_units, manifest.max_output_tokens_per_call),
            )
            self._model_basis = basis
        return self._model if self._model_basis == basis else None

    def refresh_configuration(self) -> None:
        """Publish initial state from the new component on the PCS worker."""
        self.record_running()
        try:
            state, _, _ = self._components()
            journals = self._journal_authorization()
            if self._active_model(state) is None and journals is None:
                self._report("disabled", "model_not_authorized")
            else:
                self._report("waiting")
        except (OSError, ValueError, StorageError, ModelAdapterError, sqlite3.Error) as exc:
            self._report("unavailable", getattr(exc, "reason", None) or type(exc).__name__)

    def configure(self, enabled: bool) -> dict[str, Any]:
        """Only a validated foreground settings command can issue or revoke a grant."""
        state, _, _ = self._components()
        config = self._runtime.config
        manifest = self._manifest()
        if enabled and manifest is None:
            raise ModelNotAuthorizedError("the desktop host has no Dreaming destination")
        namespace = config.authority.namespace
        with state.authorizations(self._runtime.store.migrations_dir, provision=enabled) as ledger:
            if ledger is not None:
                current = ledger.model_authorizations(namespace=namespace, status="active")
                now = self._clock()
                already_enabled = False
                for item in current:
                    if not item.authorization_ref.startswith(_GRANT_PREFIX):
                        continue
                    matches = (
                        manifest is not None
                        and item.authorization_ref == self._authorization_ref()
                        and item.manifest_hash == manifest.manifest_hash
                        and item.provider == manifest.provider
                        and item.model == manifest.model
                        and item.expires_at is not None
                        and datetime.fromisoformat(item.expires_at) > now
                        and state.count_model_requests(item.authorization_id) < item.max_calls
                    )
                    if enabled and matches and not already_enabled:
                        already_enabled = True
                    else:
                        ledger.revoke_model_authorization(item.authorization_id)
                if enabled and not already_enabled:
                    ledger.grant_model_authorization(
                        namespace=namespace,
                        authorization_ref=self._authorization_ref(),
                        provider=manifest.provider,
                        model=manifest.model,
                        manifest_hash=manifest.manifest_hash,
                        max_calls=manifest.max_calls,
                        max_output_tokens_per_call=manifest.max_output_tokens_per_call,
                        max_wall_seconds=manifest.max_wall_seconds,
                        granted_at=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                        expires_at=(now + _GRANT_LIFETIME).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    )
        with self._lock:
            self._enabled = enabled
            self._can_configure = manifest is not None
        waiting = enabled or self._journals_enabled
        self._report("waiting" if waiting else "disabled", None if waiting else "model_not_authorized")
        return {
            "state": "waiting" if waiting else "disabled",
            "reason": None if waiting else "model_not_authorized",
            "enabled": enabled,
            "can_configure": manifest is not None,
            "semantic_reason": self._semantic_reason,
        }

    def request_question_review(self, ref: RecordRef) -> dict[str, Any]:
        """Queue one exact PCS Question for the next authorized deep wake."""
        _, history, _ = self._components()
        policy = self._policy()
        runtime = self._runtime
        if not runtime.config.cognition.enabled or DREAMING_PURPOSE not in policy.purposes:
            raise InvalidInput("Dreaming question review is unavailable")
        if not policy.disclose or ref.namespace != policy.namespace:
            raise InvalidInput("Dreaming question is outside the current instance")
        self._read_current_question(policy, ref)
        history.queue_manual_review(policy.to_experience_access(), ref)
        return {"queued": True, "question": ref.to_dict()}

    def _read_current_question(self, policy: ConsultSendPolicy, ref: RecordRef) -> QuestionView:
        runtime = self._runtime
        scope = runtime.config.query_plan.scope if runtime.config.query_plan is not None else {}
        pointer = runtime.store.record_pointer(policy.namespace, ref.record_id)
        if pointer is None or pointer.current_version != ref.record_version:
            raise RecordNotFound("Dreaming question is no longer current")
        view = runtime.questions.get(self._question_access(policy), ref=ref, scope=scope)
        if view.paused or view.status not in {"open", "waiting"}:
            raise InvalidInput("Dreaming question is paused or closed")
        return view

    def _read_journals(self, query: str, anchor: Any, timezone: str, policy: ConsultSendPolicy) -> tuple[JournalEntry, ...]:
        time_query = parse_journal_time_query(query, anchor=anchor, timezone_name=timezone)
        if time_query is None or not time_query.windows:
            return ()
        zone = resolve_zone(timezone)
        entries = []
        for window in time_query.windows:
            period = JournalPeriod(
                window.precision, window.start.astimezone(zone).date(),
                window.end.astimezone(zone).date(), timezone,
            )
            entry = self._journals().get(period)
            if entry is not None and self._journal_live(entry, policy):
                entries.append(entry)
        return tuple(entries)

    def _journal_live(self, entry: JournalEntry, policy: ConsultSendPolicy) -> bool:
        try:
            return (
                not self._stopped and policy == consult_policy(self._runtime)
                and self._calendar_service().live(entry, policy)
            )
        except (OSError, ValueError, StorageError, sqlite3.Error):
            return False

    def continuity(
        self,
        policy: ConsultSendPolicy,
        turn: Any,
        input_ref: Mapping[str, Any],
    ) -> ContinuityMaterial:
        """Compose recent and optional older hits. Context owns final source delivery."""
        if not policy.disclose or "consult" not in policy.purposes or policy != consult_policy(self._runtime):
            return ContinuityMaterial()
        _, history, semantic = self._components()
        store = self._runtime.store
        access = policy.to_experience_access()
        now = self._clock()
        try:
            recent = history.recent(store, access, now=now)
        except sqlite3.Error:
            recent = history.recent(store, access, now=now, include_cached=False)
        older = ()
        memory = self._runtime.config.memory
        if memory.recall.enabled:
            try:
                current = store.load_input(
                    namespace=policy.namespace, input_id=input_ref["input_id"],
                    content_version=input_ref["content_version"],
                )
                def allow_query() -> bool:
                    cancelled = self._runtime._cancel_requested
                    return not self._stopped and (cancelled is None or not cancelled(turn.turn_id))

                if memory.time.enabled:
                    anchor, _ = message_time(current.observed_at, current.recorded_at)
                    journals = self._read_journals(current.body, anchor, memory.timezone, policy)
                    if journals:
                        return ContinuityMaterial(
                            recent=recent.items, audit=history.audit, journals=journals,
                            journal_live=lambda entry: self._journal_live(entry, policy),
                        )
                    time_query = parse_time_query(
                        current.body, anchor=anchor, timezone_name=memory.timezone,
                    )
                    if time_query is not None:
                        # The established recall path interprets explicit date
                        # requests. A vector match cannot establish occurrence.
                        return ContinuityMaterial(recent=recent.items, audit=history.audit)
                result = semantic.recall(
                    current.body, input_ref=input_ref, turn_id=turn.turn_id, allow_query=allow_query,
                )
                sources = []
                for hit in result.hits:
                    source = store.dreaming_experience_ref(access, ref=hit.source.ref)
                    if source is not None and source.occurred_at < now - RECENT_WINDOW:
                        sources.append(source)
                older = history.refresh(store, access, sources[:4], now=now, purpose="consult")
            except (KeyError, OSError, ValueError, StorageError, sqlite3.Error):
                pass  # Existing foreground keyword recall continues independently.
        return ContinuityMaterial(recent=recent.items, recalled=older, audit=history.audit)

    def _question_access(self, policy: ConsultSendPolicy) -> QuestionAccess:
        return QuestionAccess(
            namespace=policy.namespace, host_instance=policy.host_instance,
            access_ref=policy.access_ref, privacy_classes=policy.privacy_classes,
            permissions=frozenset({"question.read"}), purposes=frozenset({"cognition.question"}),
        )

    def _question_page(self, policy: ConsultSendPolicy) -> tuple[QuestionView, ...]:
        runtime = self._runtime
        if not runtime.config.cognition.enabled:
            return ()
        records = runtime.store.current_versions_of_kind(
            policy.namespace, QUESTION_KIND, after_record_id=self._question_cursor,
            limit=_QUESTION_PAGE,
        )
        views = []
        access = self._question_access(policy)
        host_scope = runtime.config.query_plan.scope if runtime.config.query_plan is not None else {}
        for record in records:
            if record.status not in {"open", "waiting"} or record.privacy_class not in access.privacy_classes:
                continue
            if not scope_matches(record.scope or {}, host_scope):
                continue
            try:
                view = runtime.questions.get(
                    access, ref=RecordRef(policy.namespace, record.record_id, record.record_version),
                    scope=host_scope,
                )
            except (QuestionError, Redacted):
                continue
            views.append(view)
        self._question_cursor = records[-1].record_id if len(records) == _QUESTION_PAGE else None
        return tuple(views)

    def _reflection_material(
        self,
        history: DreamingHistory,
        policy: ConsultSendPolicy,
        batch: DeepBatch,
        selected: tuple[HistoryItem, ...],
    ) -> ReflectionMaterial:
        runtime = self._runtime
        store = runtime.store
        scope = {}
        payload: dict[str, Any] = {}
        refs = []
        if batch.question is not None:
            question = batch.question
            host_scope = runtime.config.query_plan.scope if runtime.config.query_plan is not None else {}
            if not runtime.config.cognition.enabled or not scope_matches(question.scope, host_scope):
                raise ModelNotAuthorizedError("the Dreaming review scope changed")
            current = store.get_current(policy.namespace, question.ref.record_id)
            live = runtime.questions.get(
                self._question_access(policy), ref=question.ref, scope=question.scope,
            )
            if current.record_version != question.ref.record_version or live != question or live.paused:
                raise ModelNotAuthorizedError("the Dreaming review question changed")
            scope = question.scope
            refs.append(question.ref)
            payload["question"] = {
                "ref": question.ref.to_dict(), "question": question.question[:480],
                "status": question.status, "waiting_for": question.waiting_for,
                "trigger": batch.trigger.reason if batch.trigger else None,
            }
            for ref in batch.trigger_sources:
                version = store.get_current(ref.namespace, ref.record_id)
                if (version.record_version != ref.record_version or
                    version.privacy_class not in policy.privacy_classes or
                    not scope_matches(version.scope or {}, scope)):
                    raise ModelNotAuthorizedError("the Dreaming trigger source changed")
                refs.append(ref)
            if batch.trigger_sources:
                payload["question"]["changed_sources"] = [ref.to_dict() for ref in batch.trigger_sources]
        else:
            fresh = history.refresh(
                store, policy.to_experience_access(), [item.source for item in selected],
                now=self._clock(), purpose=DREAMING_PURPOSE,
            )
            if fresh != selected:
                raise ModelNotAuthorizedError("Dreaming history changed")
            if fresh:
                refs.extend(item.source.ref for item in fresh)
                payload["experiences"] = _history_payload(fresh)
        active = runtime.profiles.get_active_profile(policy.namespace)
        if active.ref is not None:
            # Reuse the trusted active profile verification. This is bounded
            # persona context and has no bearing on disclosure or actions.
            read_active_persona(runtime.profiles, store, policy.namespace)
            version = store.get_version(policy.namespace, active.ref.record_id, active.ref.record_version)
            if version.privacy_class in policy.privacy_classes:
                identity = version.body.get("identity_summary")
                if isinstance(identity, str) and identity:
                    payload["persona"] = identity[:480]
                    refs.append(active.ref)
        return ReflectionMaterial(
            canonical_json_text(payload) if payload else "", scope, tuple(refs),
            deep_review=batch.question is not None or bool(batch.items),
        )

    def _still_idle(self, cancel: threading.Event) -> bool:
        return not cancel.is_set() and not self._stopped

    def _project_recent(
        self, semantic: DreamingSemanticHost, refs: tuple[Mapping[str, Any], ...],
        can_work: Callable[[], bool],
    ) -> bool:
        if refs != self._shallow_page:
            self._shallow_page = self._projection_refs = refs
        outcome = semantic.maintain(self._projection_refs, allow_work=can_work)
        self._projection_refs = (
            () if _NO_SOURCE.intersection(outcome.gaps) else self._projection_refs[outcome.examined:]
        )
        with self._lock:
            self._semantic_reason = next((
                gap for gap in outcome.gaps
                if gap in {"embedding_unavailable", "scope_recall_unavailable", "vector_unavailable"}
            ), None)
        if can_work() and not self._projection_refs:
            self._shallow_page = None
            return True
        return False

    def run_idle(self, *, pending_work: bool) -> None:
        """Run local maintenance and the next eligible calendar or reflection task."""
        self.record_running()
        with self._lock:
            tick = self._monotonic()
            elapsed = tick - self._idle_since
            if self._stopped or self._foreground or elapsed < _SHALLOW_IDLE:
                return
            self._cancel = cancel = threading.Event()
        try:
            state, history, semantic = self._components()
            policy = self._policy()
            access = policy.to_experience_access()
            store = self._runtime.store
            now = self._clock()
            can_work = lambda: self._still_idle(cancel)
            model = self._active_model(state)
            journal_route = self._journal_authorization()
            if "consult" not in policy.purposes:
                self._report("disabled", "purpose_not_allowed")
                return

            if tick >= self._shallow_due and can_work():
                self._report("shallow")
                key = None
                with self._lock:
                    self._semantic_reason = None
                if policy.disclose and DREAMING_PURPOSE in policy.purposes:
                    try:
                        key = semantic.projection_key()
                    except (OSError, StorageError, ValueError, sqlite3.Error):
                        with self._lock:
                            self._semantic_reason = "embedding_unavailable"
                if self._projection_key != key:
                    self._projection_key = key
                    self._projection_refs = ()
                    self._shallow_page = None
                accept_page = None
                if key is not None:
                    def accept_page(sources):
                        return self._project_recent(semantic, tuple(source.ref for source in sources), can_work)
                advance = history.advance_recent(
                    store, access, now=now, projection_key=key, accept_page=accept_page,
                )
                self._shallow_due = tick + (10.0 if advance.has_more else _SHALLOW_INTERVAL)


            if journal_route is not None and not pending_work and can_work():
                period = self._journals().next_due(now, self._runtime.config.memory.timezone)
                if period is not None:
                    self._report("deep")
                    outcome = self._calendar_service().run(
                        period, policy=policy, cancel=cancel,
                        allow_call=lambda: (
                            can_work() and self._journal_route() == journal_route
                            and self._journals().authorization() == journal_route
                        ),
                        publish_lock=self._lock,
                    )
                    if outcome == "failed":
                        self._journals().defer(period, until=self._clock() + _JOURNAL_RETRY_DELAY)
                    self._report("waiting" if outcome == "published" else outcome)
                    return

            if (
                tick >= self._projection_due and elapsed >= _PROJECTION_IDLE and not pending_work
                and can_work() and policy.disclose and DREAMING_PURPOSE in policy.purposes
            ):
                self._projection_due = tick + _PROJECTION_INTERVAL
                try:
                    key = semantic.projection_key()
                except (OSError, StorageError, ValueError, sqlite3.Error):
                    key = None
                if key is not None:
                    self._report("deep")
                    page = history.deep(store, access, now=now, limit=6, projection_key=key)
                    refs = tuple(item.source.ref for item in page.items)
                    if refs:
                        outcome = semantic.maintain(refs, allow_work=can_work)
                        if outcome.examined == len(refs) and can_work():
                            history.complete_deep(access, page)
                    elif can_work():
                        history.complete_deep(access, page)

            if not can_work():
                return
            if model is None:
                if journal_route is None:
                    self._report("disabled", "model_not_authorized")
                else:
                    self._report("waiting", "maintenance_pending" if pending_work else None)
                return
            if pending_work:
                self._report("waiting", "maintenance_pending")
                return
            if elapsed < MIN_IDLE.total_seconds():
                self._report("waiting")
                return
            if model.in_flight:
                self._report("paused", "transport_draining")
                return
            try:
                model.preflight(now=now)
            except ModelAdapterError as exc:
                if getattr(exc, "reason", None) == "calls_exhausted":
                    with self._lock:
                        self._enabled = False
                raise
            with self._lock:
                if self._foreground or not can_work():
                    return
                attempt = state.reserve_wake(
                    generation=self._generation, now=now, idle_since=now - timedelta(seconds=elapsed),
                    foreground_busy=False, pending_work=False, eligible=True,
                )
            if attempt is None:
                self._report("waiting", "wake_budget")
                return
            try:
                self._report("deep")
                views = self._question_page(policy)
                manual = ()
                queued = history.queued_manual_review(access)
                if queued is not None and self._runtime.config.cognition.enabled:
                    try:
                        live = self._read_current_question(policy, queued)
                    except StorageError:
                        history.discard_manual_review(access, queued)
                    else:
                        views = (live, *(view for view in views if view.ref != queued))
                        manual = (queued.to_dict(),)
                batch = history.deep(
                    store, access, views=views, now=now, manual_refs=manual, include_history=False,
                )
                selected = ()
                if batch.question is None:
                    selected = history.recent(
                        store, access, now=now, purpose=DREAMING_PURPOSE, limit=6,
                    ).items
                self._report("thinking")

                def complete(connection: sqlite3.Connection) -> bool:
                    return history.complete_deep(access, batch, connection=connection)

                result = model.reflect(
                    attempt, read_material=lambda live: self._reflection_material(history, live, batch, selected),
                    current_generation=lambda: self._generation, cancel=cancel, clock=self._clock,
                    publication_lock=self._lock, complete=complete if batch.checkpointable else None,
                )
            except Exception:
                state.fail_wake(attempt, now=self._clock())
                raise
            self._report("waiting" if result in {"proposed", "noop"} else result)
        except (OSError, ValueError, StorageError, ModelAdapterError, sqlite3.Error, PersonaUnavailable) as exc:
            self._report("unavailable", getattr(exc, "reason", None) or type(exc).__name__)

    def action_proposal(self, attempt_id: str) -> WakeProposal | None:
        """Future trusted-host handoff. Revalidate all local proposal dependencies."""
        state, history, _ = self._components()
        now = self._clock()
        proposal = state.read_proposal(attempt_id, now=now)
        authorization_id = state.proposal_authorization(attempt_id)
        manifest = self._manifest()
        if proposal is None or authorization_id is None or manifest is None:
            return None
        policy = self._policy()
        store = self._runtime.store
        try:
            grant = self._read_grant(authorization_id)
        except RecordNotFound:
            return None
        if (
            grant.status != "active" or grant.namespace != policy.namespace
            or grant.manifest_hash != manifest.manifest_hash
            or (grant.expires_at is not None and datetime.fromisoformat(grant.expires_at) <= now)
        ):
            return None
        for source in proposal.source_refs:
            ref = source_ref_mapping(source)
            if ref.get("namespace") != policy.namespace:
                return None
            if isinstance(source, RecordRef):
                try:
                    current = store.get_version(source.namespace, source.record_id, source.record_version)
                    if current.kind == "persona_profile":
                        if self._runtime.profiles.get_active_profile(policy.namespace).ref != source:
                            return None
                        read_active_persona(self._runtime.profiles, store, policy.namespace)
                    else:
                        current = store.get_current(source.namespace, source.record_id)
                        if current.kind == QUESTION_KIND:
                            live = self._runtime.questions.get(
                                self._question_access(policy), ref=source, scope=proposal.scope,
                            )
                            if live.paused or live.status not in {"open", "waiting"}:
                                return None
                except (StorageError, PersonaUnavailable):
                    return None
                if (
                    current.record_version != source.record_version
                    or current.privacy_class not in policy.privacy_classes
                    or not scope_matches(current.scope or {}, proposal.scope)
                ):
                    return None
            elif isinstance(source, Mapping):
                access = policy.to_experience_access()
                exact = store.dreaming_experience_ref(access, ref=ref)
                if exact is None or not history.refresh(store, access, [exact], now=now, purpose=DREAMING_PURPOSE):
                    return None
            else:
                return None
        return proposal
