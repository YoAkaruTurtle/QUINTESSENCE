"""The trusted Python host wiring behind the desktop Consult sidecar.

This module is an adapter, not a second cognition implementation.  It wires the
existing public PCS services:

* ``CognitionStore`` and its numbered SQL migrations;
* ``PersonaProfileService`` / ``SelfViewService`` / ``ContextService``;
* ``DirectHttpModel`` (``collab.v1``) and ``CognitionQueryService``;
* ``ConsultFacade``.

The only optional substitution is the Lab-only scripted model transport: the
real adapter still builds the request, re-checks the C09 send gate, reserves an
attempt, validates the provider-shaped response and records the attempt.  The
script never writes a Belief, an interaction or an artifact itself.

A production configuration never seeds fixtures and never accepts the scripted
transport; that combination is refused by :mod:`host_config`.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from quintessence_pcs.conversation.orchestrator import (
    ConversationOrchestrator,
    PersonaNotReady,
    resolve_turn_profile,
)
from quintessence_pcs.collaboration import (
    ConsultFacade,
    ConsultHostAccess,
    ConsultRequest,
    HostAccess,
    ReviewFacade,
)
from quintessence_pcs.domains.cognition import (
    CognitionQueryService,
    SelfViewService,
)
from quintessence_pcs.domains.cognition.epistemic import EpistemicAccess
from quintessence_pcs.domains.cognition.query import QueryAccess
from quintessence_pcs.domains.cognition.questions import (
    QuestionAccess,
    QuestionService,
)
from quintessence_pcs.collaboration.change_service import (
    ApprovalAction,
    ChangeAccess,
    CognitionChangeService,
)
from quintessence_pcs.conversation.orchestrator import TurnLimits
from quintessence_pcs.conversation.sdrpe import SdrpePlan
from quintessence_pcs.conversation.tools import WebToolConfig, build_tool_runtime
from quintessence_pcs.runtime.web_tools import DeepSeekWebClient, WebFetchPolicy
from quintessence_pcs.domains.cognition.working_set import build_working_set
from quintessence_pcs.runtime.context import cognition_material_from_working_set
from quintessence_pcs.store.governance import (
    CHANGE_OPERATION,
    GovernanceAccess,
    GovernanceStore,
)
from quintessence_pcs.store.collaboration import COGNITION_CHANGE_POLICY_REF
from quintessence_pcs.collaboration.review_policy import (
    BASE_POLICY_ID,
    BASE_POLICY_VERSION,
)
from quintessence_pcs.domains.cognition.beliefs import (
    CLAIM_TYPES,
    INFERENCE_BASIS_BY_CLAIM_TYPE,
)
from quintessence_pcs.domains.persona.profiles import (
    PERSONA_PROFILE_KIND,
    PROFILE_BODY_MAX_BYTES,
    PROFILE_SCHEMA_VERSION,
    REVIEW_APPROVE_EVENT_TYPE,
    PersonaProfileService,
    profile_body_digest,
    profile_record_id,
    require_profile_id,
    validate_profile_body,
)
from quintessence_pcs.domains.persona.preference_view import list_preference_groups
from quintessence_pcs.domains.refs import EventRef
from quintessence_pcs.domains.persona.expression_directives import (
    KIND_EXPLICIT_CORRECTION,
    KIND_EXPLICIT_LONG_TERM,
    KIND_TENTATIVE,
    classify_directive,
    plan_from_dict,
    quote_text,
)
from quintessence_pcs.domains.persona.expression_preferences import (
    ExpressionPreferenceService,
)
from quintessence_pcs.runtime.expression_material import build_expression_material
from quintessence_pcs.runtime.preference_router import (
    PreferenceRouter,
    PreferenceTarget,
    TurnPreferenceContext,
    bind_router_context,
)
from quintessence_pcs.domains.cognition.maintenance_guard import (
    MaintenanceStale,
    require_live_targets,
)
from quintessence_pcs.domains.memory.maintenance_delivery import (
    memory_delivery_worker,
)
from quintessence_pcs.domains.memory.maintenance_material import (
    MAX_CONTEXT_ITEMS,
    load_event_material,
    load_turn_material,
)
from quintessence_pcs.runtime.maintenance_cognition import cognition_review_worker
from quintessence_pcs.foundation.errors import (
    ContextStale,
    CorruptStore,
    EventNotFound,
    InvalidInput,
    RecordNotFound,
    Redacted,
)
from quintessence_pcs.conversation.persona import (
    PersonaExpectationConflict,
    PersonaState,
    PersonaUnavailable,
    build_persona_material,
    parse_speaker,
    persona_matches_expected,
    persona_plan,
    read_active_persona,
    resolve_expected_persona,
    verify_activation_target,
    with_source,
)
from quintessence_pcs.store.maintenance import (
    LEASABLE_STATES,
    MaintenanceConflict,
    MaintenanceStore,
    SliceLimits,
    run_slice,
)
from quintessence_pcs.foundation.timeutil import utc_now_rfc3339
from quintessence_pcs.foundation.jsonutil import canonical_json_text, strict_json_loads
from quintessence_pcs.runtime import (
    ConsultSendPolicy,
    ContextService,
    DirectHttpModel,
)
from quintessence_pcs.runtime.http_model import (
    DIRECT_HTTP_LEGACY_PROTOCOL,
    MAX_RESPONSE_BYTES,
    _build_opener,
    _redact,
)
from quintessence_pcs.runtime.model_port import (
    ModelAdapterError,
    ModelBudgetExceededError,
    ModelNotAuthorizedError,
)
from dataclasses import asdict, replace

from quintessence_pcs.adapters.scope_recall import SubprocessCaptureAdapter
from quintessence_pcs.adapters.scope_recall.transport import AdapterConfig
from quintessence_pcs.domains.memory.experience_bridge import (
    BridgeConfig,
    ExperienceBridge,
    role_for_origin,
)
from quintessence_pcs.domains.memory.instance_binding import (
    InstanceBindingMismatch,
    InstanceBindingRefused,
    ensure_binding,
)
from quintessence_pcs.domains.memory.postprocess import (
    run_postprocess_sweep,
    turn_postprocess_status,
)
from quintessence_pcs.domains.memory.scope_recall_search import (
    MemorySearchError,
    search_scope_recall,
)
from quintessence_pcs.domains.memory.scope_recall_recall import (
    MemoryRecallMaterial,
    RecallTimeFilter,
    bind_recall_sources,
    recall_scope_recall,
    recall_time_window,
)
from quintessence_pcs.domains.memory.scope_recall_query import focus_prefix
from quintessence_pcs.domains.memory.time_window import (
    parse_time_query,
    utc_windows,
    window_bounds,
)
from quintessence_pcs.store import CognitionStore, DERIVATION_WORK_KINDS
from quintessence_pcs.store.experience import ExperienceAccess

from host_config import REVIEW_PURPOSE, HostConfig, ModelSpec
from entry_compat import check_entry_compatibility, required_actions

LAB_KEY_PREFIX = "lab-"
_MAX_PUBLIC_ITEMS = 100

#: One memory browse/search is a short local read; a slow store is reported as
#: unavailable rather than blocking the single bridge worker indefinitely.
MEMORY_SEARCH_TIMEOUT_SECONDS = 4.0

#: UI-F02 management bounds.  Every listing is bounded in SQL/Python; the
#: settings page never receives an unbounded instance dump.
PROFILE_LIST_MAX_PROFILES = 50
PROFILE_LIST_MAX_VERSIONS = 50

#: The instance manager's fixed persona-import directory.  It lives inside the
#: instance root (like ``memory_bridge.data_directory``), so the WebView never
#: supplies a path and the host never reads outside its own instance.
PERSONA_IMPORT_DIRNAME = "persona-import"
PERSONA_IMPORT_MAX_BYTES = 256 * 1024
PERSONA_IMPORT_MAX_DRAFTS = 4
PERSONA_IMPORT_TTL_SECONDS = 900

#: Placeholder review reference used to shape-check an imported body before the
#: real approval exists; it is never published or stored.
_IMPORT_PLACEHOLDER_EVENT_ID = "00000000-0000-4000-8000-000000000000"


class RuntimeStartupError(Exception):
    """The trusted runtime cannot be built; the bridge stays not-ready."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ConversationRequestError(Exception):
    """A conversation request refused before any turn was created."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InputRegistrationError(Exception):
    """The C05b input registration failed; no interaction was created."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ReviewRequestError(Exception):
    """A structured C12c review refusal before C07 is called.

    The code is a ``desktop.v1`` review label (for example ``CANDIDATE_STALE``),
    never a new PCS error code.  It is safe to show to the operator: it carries
    no body, candidate text, prompt, path or internal identifier.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


#: RG05 supplement 04: the default auto-commit grant is bounded by *impact*,
#: not by claim category.  Every claim type the domain accepts is covered except
#: ``derived_rule`` (the policy classifies it BLOCKED, which no authority may
#: commit).  A user preference without a binding is E2 and therefore still needs
#: a human; a valid binding would make it E1 and eligible.
DEFAULT_AUTO_CLAIM_TYPES = tuple(
    claim for claim in CLAIM_TYPES if claim != "derived_rule"
)
_DEFAULT_GRANT_NAMESPACE = uuid.UUID("0f3d7c1a-2b4e-5a6f-8c9d-1e2f3a4b5c6d")


def default_cognition_grant_id(namespace: str) -> str:
    return str(
        uuid.uuid5(_DEFAULT_GRANT_NAMESPACE, f"{namespace}:default-cognition-grant")
    )


def ensure_default_cognition_grant(store: Any, config: Any) -> str:
    """Idempotently create/read the default narrow auto-commit grant.

    RG05 supplement 03: a cognition-enabled host auto-commits only the safe
    range the existing policy already allows (E0/E1, non-user-owned, in the
    configured scope).  The grant is an ordinary J06 row; a failure here is a
    startup refusal, never a silent downgrade to "ask the human every time".
    """

    namespace = config.authority.namespace
    authorization_id = default_cognition_grant_id(namespace)
    governance = GovernanceStore(store)
    existing = governance.load_commit_authorization(
        namespace=namespace, authorization_id=authorization_id
    )
    now = utc_now_rfc3339()
    if existing is not None and governance.authorization_is_active(existing, now):
        if (
            set(existing.operations) == {CHANGE_OPERATION}
            and set(existing.claim_types) == set(DEFAULT_AUTO_CLAIM_TYPES)
            and dict(existing.scope) == dict(config.query_plan.scope)
            and existing.max_impact == "E1"
        ):
            return authorization_id
    access = GovernanceAccess(
        namespace=namespace,
        host_instance=config.authority.host_instance,
        operator=config.authority.operator,
        access_ref=config.authority.access_ref,
        permissions=frozenset({"governance.admin"}),
    )
    try:
        governance.grant_commit_authorization(
            access,
            authorization_id=authorization_id,
            host_instance=config.authority.host_instance,
            access_ref=config.authority.access_ref,
            operations=(CHANGE_OPERATION,),
            claim_types=DEFAULT_AUTO_CLAIM_TYPES,
            scope=dict(config.query_plan.scope),
            max_impact="E1",
            max_changes_per_bundle=2,
            max_targets_per_bundle=2,
            policy_id=BASE_POLICY_ID,
            policy_version=BASE_POLICY_VERSION,
            authorization_source="host_config",
            issued_at=now,
            issued_by=config.authority.operator,
        )
    except Exception as exc:  # noqa: BLE001 - startup must fail closed
        raise RuntimeStartupError(
            "COGNITION_GRANT_INVALID",
            f"the default cognition grant could not be created ({type(exc).__name__})",
        ) from exc
    return authorization_id


class PersonaControlError(Exception):
    """A structured refusal of one host-controlled persona management action."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def memory_recall_event_payload(material: Any) -> dict[str, Any]:
    """The one durable status event of a turn's automatic memory recall.

    J10 W5: a failed attempt records one bounded ``reason_code`` so a failure is
    diagnosable after the fact; a successful recall records none, which keeps
    "no reason" meaning "no failure".  Only aggregate counts and codes are
    written -- never a query, a memory body or an adapter message.
    """

    if material is None:
        return {}
    reasons: dict[str, int] = {}
    for entry in material.dropped:
        reason = str(entry.get("reason") or "unknown")[:32]
        reasons[reason] = reasons.get(reason, 0) + 1
    payload: dict[str, Any] = {
        "status": material.status,
        "shown": len(material.items),
        "units": material.units,
        "elapsed_ms": material.elapsed_ms,
        "reasons": [
            {"reason": reason, "count": reasons[reason]} for reason in sorted(reasons)
        ],
    }
    reason_code = getattr(material, "reason", "")
    if material.status not in ("ok", "no_match", "partial") and reason_code:
        payload["reason_code"] = reason_code
    if material.time_intent != "none" or material.time_unsupported:
        payload["time"] = {
            "intent": material.time_intent,
            "windows": material.windows,
            "scan_pages": material.scan_pages,
            "scan_complete": material.scan_complete,
            "time_unknown": material.time_unknown,
            "time_not_occurred": sum(
                1
                for entry in material.dropped
                if entry.get("reason") == "time_not_occurred"
            ),
            "time_dropped": material.time_dropped,
            "unsupported": material.time_unsupported,
            "window_start": material.window_start,
            "window_end": material.window_end,
            "anchor_source": material.anchor_source,
        }
    return payload


class _TurnMemoryRecall:
    """One turn's bounded automatic memory recall.

    The upstream call happens at most once.  Every later content-Context rebuild
    re-binds the same packet (delivery/version/permission/deletion checks), so a
    tool turn never gets a second full two-second window.  All failures are
    values, never exceptions: recall must not break a chat turn.
    """


    def __init__(
        self,
        *,
        store: Any,
        namespace: str,
        access: ExperienceAccess,
        adapter: Any,
        bridge_spec: Any,
        recall_spec: Any,
        timezone: str,
        input_ref: Mapping[str, Any],
        time_enabled: bool = True,
    ) -> None:
        self._store = store
        self._namespace = namespace
        self._access = access
        self._adapter = adapter
        self._scope_id = str(getattr(bridge_spec, "scope_id", "") or "")
        self._max_items = int(getattr(recall_spec, "max_items", 4))
        self._budget_units = int(getattr(recall_spec, "budget_units", 2048))
        self._timezone = timezone
        self._time_enabled = bool(time_enabled)
        self._deadline = time.monotonic() + min(
            float(getattr(recall_spec, "timeout_seconds", 2.0)), 2.0
        )
        self._input_ref = dict(input_ref) if isinstance(input_ref, Mapping) else {}
        self._query: str | None = None
        self._recall_query: str | None = None
        self._followup_query: str | None = None
        self._message_time: str | None = None
        self._message_time_basis = "unknown"
        self._time_query: Any = None
        self._disabled = False
        self._page: Any = None
        self._material: MemoryRecallMaterial | None = None
        self._last_event_key: tuple[Any, ...] | None = None
        self._event: dict[str, Any] | None = None
        self._load_query()

    def _load_query(self) -> None:
        ref = self._input_ref
        if not isinstance(ref.get("input_id"), str) or type(ref.get("content_version")) is not int:
            self._disabled = True
            return
        try:
            row = self._store.load_input(
                namespace=self._namespace,
                input_id=ref["input_id"],
                content_version=ref["content_version"],
            )
        except Exception:  # noqa: BLE001 - an unreadable current input is not recallable
            self._disabled = True
            return
        if (
            getattr(row, "origin_kind", "") != "user_statement"
            or getattr(row, "privacy_class", "") not in self._access.privacy_classes
            or not getattr(row, "body", "")
        ):
            self._disabled = True
            return
        from quintessence_pcs.domains.memory.time_window import message_time

        self._query = str(row.body)
        self._message_time, basis = message_time(
            getattr(row, "observed_at", None), getattr(row, "recorded_at", None)
        )
        self._message_time_basis = basis
        self._time_query = (
            parse_time_query(
                self._query,
                anchor=self._message_time,
                timezone_name=self._timezone,
            )
            if self._time_enabled and self._message_time
            else None
        )
        recall_query = self._query
        if (
            self._time_query is not None
            and self._time_query.windows
            and self._time_query.rest
        ):
            # The recognised window replaces the time phrase, not the topic.
            recall_query = self._time_query.rest
        self._recall_query = recall_query
        self._followup_query = focus_prefix(recall_query)

    def _time_filter(self) -> Any:
        query = self._time_query
        if query is None or not query.windows:
            return None
        return RecallTimeFilter(
            windows=utc_windows(query.windows),
            intent=query.intent,
            anchor=self._message_time,
            anchor_source=self._message_time_basis,
            unsupported=False,
            timezone=self._timezone,
        )

    def _annotate(self, material: MemoryRecallMaterial) -> MemoryRecallMaterial:
        query = self._time_query
        if query is None:
            return material
        return replace(
            material,
            time_intent=query.intent,
            windows=len(query.windows),
            anchor_source=self._message_time_basis,
            time_unsupported=bool(
                material.time_unsupported
                or query.reason in ("unsupported", "future")
            ),
        )

    def material(self, *, tool_results: Any = (), history: Any = None) -> Any:
        if self._disabled or not self._query:
            return None
        try:
            if self._material is None:
                query = self._time_query
                time_filter = self._time_filter()
                if query is not None and query.future:
                    # This batch retrieves no plans: keep the ordinary recall and
                    # record the unsupported future request, never a fake window.
                    self._material = self._annotate(
                        self._recall_and_bind(tool_results, history, None)
                    )
                elif query is not None and not query.windows:
                    if query.time_only:
                        self._material = MemoryRecallMaterial(
                            status="incomplete",
                            items=(),
                            dropped=({"reason": "time_unsupported"},),
                            time_intent=query.intent,
                            time_unsupported=True,
                            anchor_source=self._message_time_basis,
                        )
                    else:
                        self._material = self._annotate(
                            self._recall_and_bind(tool_results, history, None)
                        )
                elif query is not None and query.windows and query.time_only:
                    self._material = self._window_only(tool_results, history, time_filter)
                else:
                    self._material = self._annotate(
                        self._recall_and_bind(tool_results, history, time_filter)
                    )
            elif self._page is None:
                # The first attempt ended before a packet existed (timeout or an
                # adapter failure).  Keep that honest state.
                pass
            else:
                # Same packet, fresh source checks; a deletion or moved delivery
                # revision becomes an omission/ContextStale, never a silent reuse.
                self._material = self._bind(
                    self._page, tool_results, history, self._time_filter()
                )
            self._note_event()
            return self._material
        except Exception:  # noqa: BLE001 - recall is optional material
            self._material = MemoryRecallMaterial(
                status="unavailable",
                items=(),
                dropped=({"reason": "internal_error"},),
            )
            self._note_event()
            return self._material

    def _recall_and_bind(
        self, tool_results: Any, history: Any, time_filter: Any
    ) -> MemoryRecallMaterial:
        remaining = self._deadline - time.monotonic()
        if remaining < 0.25:
            return MemoryRecallMaterial(
                status="timeout",
                items=(),
                dropped=({"reason": "no_time_budget"},),
            )
        upstream_budget = max(0.05, remaining - 0.2)
        page = recall_scope_recall(
            self._adapter,
            query=self._recall_query or self._query or "",
            scope_id=self._scope_id,
            max_items=self._max_items,
            budget_units=self._budget_units,
            timeout_seconds=upstream_budget,
            timezone=self._timezone,
            request_id="recall-" + uuid.uuid4().hex,
        )
        material = self._bind(page, tool_results, history, time_filter)
        page, material = self._maybe_followup(
            page, material, tool_results, history, time_filter
        )
        self._page = page
        return material

    def _window_only(
        self, tool_results: Any, history: Any, time_filter: Any
    ) -> MemoryRecallMaterial:
        remaining = self._deadline - time.monotonic()
        if remaining < 0.25:
            return MemoryRecallMaterial(
                status="timeout",
                items=(),
                dropped=({"reason": "no_time_budget"},),
                time_intent=time_filter.intent if time_filter is not None else "none",
                anchor_source=self._message_time_basis,
            )
        since, until = window_bounds(self._time_query.windows)
        if time_filter is not None and time_filter.intent == "event":
            # The upstream occurrence is the message time, which may be later
            # than the event: an upper bound would exclude the correct source.
            until = None
        upstream_budget = max(0.05, remaining - 0.2)
        page = recall_time_window(
            self._adapter,
            since=since,
            until=until,
            limit=20,
            pages=3,
            timeout_seconds=upstream_budget,
            request_id="window-" + uuid.uuid4().hex,
        )
        self._page = page
        return self._bind(page, tool_results, history, time_filter)

    def _maybe_followup(
        self,
        original_page: Any,
        original_material: MemoryRecallMaterial,
        tool_results: Any,
        history: Any,
        time_filter: Any = None,
    ) -> tuple[Any, MemoryRecallMaterial]:
        """One literal focus-prefix query only after zero deliverable events.

        The decision happens after source binding, so an upstream event that PCS
        cannot map, dedupe, or disclose cannot suppress the variant.  The
        original query is always the first call; this is the only second call.
        """

        if (
            not self._followup_query
            or original_page is None
            or original_material.items
            or original_material.status in ("timeout", "unavailable", "invalid")
        ):
            return original_page, original_material
        remaining = self._deadline - time.monotonic()
        if remaining < 0.25:
            return original_page, replace(
                original_material,
                status="incomplete",
                gaps=tuple(original_material.gaps) + ("variant_skipped",),
            )
        page = recall_scope_recall(
            self._adapter,
            query=self._followup_query,
            scope_id=self._scope_id,
            max_items=self._max_items,
            budget_units=self._budget_units,
            timeout_seconds=max(0.05, remaining - 0.1),
            timezone=self._timezone,
            request_id="recall-" + uuid.uuid4().hex,
        )
        page = replace(page, query_variant="focus_prefix")
        material = self._bind(page, tool_results, history, time_filter)
        if material.items:
            return page, replace(
                material, status="partial", query_variant="focus_prefix"
            )
        if material.status in ("timeout", "unavailable", "invalid"):
            return page, replace(
                material, status="incomplete", query_variant="focus_prefix"
            )
        if (
            original_material.status == "no_match"
            and material.status == "no_match"
            and not original_material.items
        ):
            return page, replace(
                material, status="no_match", query_variant="focus_prefix"
            )
        return page, replace(
            material, status="incomplete", query_variant="focus_prefix"
        )

    def _bind(
        self, page: Any, tool_results: Any, history: Any, time_filter: Any = None
    ) -> MemoryRecallMaterial:
        history_refs = tuple(
            getattr(history, "messages", ()) if history is not None else ()
        )
        return bind_recall_sources(
            self._store,
            namespace=self._namespace,
            access=self._access,
            page=page,
            scope_id=self._scope_id,
            max_items=self._max_items,
            budget_units=self._budget_units,
            current_input_ref=self._input_ref,
            history_refs=history_refs,
            tool_results=tuple(tool_results or ()),
            time_filter=time_filter,
            timezone_name=self._timezone,
        )

    def _event_payload(self) -> dict[str, Any]:
        return memory_recall_event_payload(getattr(self, "_material", None))

    def _note_event(self) -> None:
        payload = self._event_payload()
        key = (
            payload.get("status"),
            payload.get("shown"),
            payload.get("units"),
            payload.get("reason_code"),
            tuple(
                (entry.get("reason"), entry.get("count"))
                for entry in payload.get("reasons") or ()
            ),
            json.dumps(payload.get("time"), sort_keys=True, ensure_ascii=False),
        )
        if key == self._last_event_key:
            return
        self._last_event_key = key
        self._event = payload

    def take_event(self) -> dict[str, Any] | None:
        payload, self._event = self._event, None
        return payload


class DesktopRuntime:
    """One sidecar process's store, host services and real ConsultFacade."""

    def __init__(
        self,
        config: HostConfig,
        *,
        event_sink: Any | None = None,
        cancel_requested: Any | None = None,
    ) -> None:
        self.config = config
        self._event_sink = event_sink
        self._cancel_requested = cancel_requested
        self._lab_key_env = _ensure_synthetic_key(config)
        #: One maintenance attempt at a time, and only maintenance.  The chat
        #: model is a separate adapter and is never gated by this state or by
        #: the maintenance transport's ``in_flight`` flag.
        self._maintenance_state_lock = threading.Lock()
        self._maintenance_cancel_event: threading.Event | None = None
        self._maintenance_active: dict[str, Any] | None = None
        self._maintenance_last: dict[str, Any] | None = None
        self._maintenance_health_cache: dict[str, Any] = {}
        self.store: CognitionStore | None = None
        self.experience_bridge = None
        self._memory_adapter = None
        self._memory_spec = None
        self._memory_entry_compat = self._check_memory_entry(config)
        if (
            self._memory_entry_compat is not None
            and self._memory_entry_compat.get("checked")
            and self._memory_entry_compat.get("missing")
        ):
            LOGGER.warning(
                "memory bridge entry lacks actions: %s",
                ",".join(self._memory_entry_compat["missing"]),
            )
        try:
            self.store = CognitionStore.open(
                config.database_path,
                migrations_dir=_migrations_dir(),
                deletion_ledger_dir=getattr(config, "deletion_ledger_dir", None),
                deletion_anchor_dir=getattr(config, "deletion_anchor_dir", None),
                deletion_adopt=getattr(config, "deletion_adopt", None),
                ps06_unprotected=getattr(config, "ps06_unprotected", None),
            )
        except Exception as exc:  # noqa: BLE001 - startup refusal is a host state
            self._remove_synthetic_key()
            raise RuntimeStartupError(
                "STORE_UNAVAILABLE", f"the PCS store could not be opened ({type(exc).__name__})"
            ) from exc

        try:
            row = self.store.connection.execute(
                "SELECT MAX(migration_id) FROM schema_migrations"
            ).fetchone()
            self.migrations_version = (
                0 if row is None or row[0] is None else int(row[0])
            )
            self.profiles = PersonaProfileService(self.store)
            self.expression_preferences = ExpressionPreferenceService(self.store)
            self._active_preference_router = None
            self._directive_access = None
            self._change_scope: dict[str, Any] = {}
            self._import_drafts: dict[str, dict[str, Any]] = {}
            self._expression_error: str | None = None
            self.self_view = SelfViewService(self.store)
            self.contexts = ContextService(
                self.store,
                self_view_service=self.self_view,
                profile_service=self.profiles,
                request_budget_units=config.request_budget_units,
                output_reserve_units=config.output_reserve_units,
                pcs_context_max_bytes=config.pcs_context_max_bytes,
                pcs_belief_coverage=config.pcs_belief_coverage,
                pcs_belief_privacy_classes=config.authority.privacy_classes,
            )
            self.queries = CognitionQueryService(self.store)
            self.persona: PersonaState | None = self._startup_persona(config)
            had_persona = self.persona is not None
            try:
                if config.lab is not None:
                    self._seed_lab(config)
                if config.managed and config.provision:
                    self._provision_managed(config)
            except Exception as exc:  # noqa: BLE001 - startup refusal is a host state
                if had_persona:
                    raise
                raise RuntimeStartupError(
                    "NEUTRAL_PROVISION_FAILED",
                    "the neutral default persona could not be provisioned "
                    f"({type(exc).__name__})",
                ) from exc
            if self.persona is None:
                try:
                    self.persona = with_source(
                        read_active_persona(
                            self.profiles,
                            self.store,
                            config.authority.namespace,
                        ),
                        "neutral_provisioned",
                    )
                except PersonaUnavailable as exc:
                    code = (
                        "NEUTRAL_PROVISION_FAILED"
                        if exc.code == "PROFILE_MISSING"
                        else exc.code
                    )
                    raise RuntimeStartupError(code, exc.detail) from exc
            self.model = _build_model(config, self.store)
            self.review_model = _build_model(config, self.store, review=True)
            self.maintenance_model = _build_model(
                config,
                self.store,
                review=True,
                transport_timeout_cap=(
                    config.maintenance.model_timeout_seconds
                    if config.maintenance is not None
                    and config.maintenance.enabled
                    else 7.0
                ),
                max_retries=0,
            )
            self.facade = ConsultFacade(
                self.store,
                context_service=self.contexts,
                model=self.model,
                query_service=self.queries,
            )
            self.review_facade = ReviewFacade(
                self.store,
                context_service=self.contexts,
                model=self.review_model,
            )
            self._access = ConsultHostAccess(
                namespace=config.authority.namespace,
                host_instance=config.authority.host_instance,
                access_ref=config.authority.access_ref,
                privacy_classes=config.authority.privacy_classes,
                purposes=config.authority.purposes,
                disclose=config.authority.disclose,
            )
            self._review_access = HostAccess(
                namespace=config.authority.namespace,
                host_instance=config.authority.host_instance,
                operator=config.authority.operator,
                permissions=config.authority.review_permissions,
                privacy_classes=config.authority.privacy_classes,
                purposes=config.authority.purposes,
                access_ref=config.authority.access_ref,
            )
            self.questions = QuestionService(self.store)
            self._validate_cognition(config)
            self._tool_runtime = self._build_tool_runtime(config)
            self.conversations = ConversationOrchestrator(
                self.store,
                context_service=self.contexts,
                model=self.model,
                max_history_turns=config.history_max_turns,
                max_history_bytes=config.history_max_bytes,
                protocol_version=config.model.model_protocol,
                limits=_turn_limits(config),
                tool_runtime_factory=self._tool_runtime,
                cognition_material_factory=self._build_cognition_material_factory(
                    config
                ),
                expression_material_factory=self._build_expression_material_factory(
                    config
                ),
                memory_recall_factory=self._build_memory_recall_factory(config),
                cognition_status_factory=self._pending_cognition_changes,
                event_sink=self._event_sink,
                cancel_requested=self._cancel_requested,
                # Trusted host identity: from the loaded HostConfig only.
                host_is_lab=config.is_lab,
            )
            self.interrupted_turns = self.store.interrupt_stale_turns(
                namespace=config.authority.namespace
            )
            self.experience_bridge = self._build_experience_bridge(config)
        except RuntimeStartupError:
            self.store.close()
            self._remove_synthetic_key()
            raise
        except Exception as exc:  # noqa: BLE001 - startup refusal is a host state
            self.store.close()
            self._remove_synthetic_key()
            raise RuntimeStartupError(
                "RUNTIME_UNAVAILABLE",
                f"the trusted runtime could not be built ({type(exc).__name__})",
            ) from exc

        self.ready, self.reason_code, self.reason_detail = self._readiness()

    # ------------------------------------------------------------------ seed

    def _startup_persona(self, config: HostConfig) -> PersonaState | None:
        """RG06: the expected-persona startup contract (never a runtime pin).

        ``app.persona_expected`` is checked here and again on every restart.
        A miss raises ``EXPECTED_PERSONA_UNAVAILABLE`` naming the expected id,
        before any persona is provisioned and before any model could be called.
        With no expectation configured the host either reuses the active
        profile or leaves ``None`` so provisioning may create the neutral one.
        """

        namespace = config.authority.namespace
        try:
            if config.persona_expected is not None:
                return resolve_expected_persona(
                    self.profiles,
                    self.store,
                    namespace,
                    config.persona_expected,
                )
            active = self.profiles.get_active_profile(namespace)
            if active.ref is None:
                return None
            return read_active_persona(self.profiles, self.store, namespace)
        except PersonaUnavailable as exc:
            raise RuntimeStartupError(exc.code, exc.detail) from exc

    def _seed_lab(self, config: HostConfig) -> None:
        seed = config.lab
        assert seed is not None
        namespace = config.authority.namespace
        active = self.profiles.get_active_profile(namespace)
        # RG06: when the instance declares an expected persona, startup already
        # proved it (or refused).  Never auto-create a persona on this path: the
        # synthetic Lab profile is a neutral fixture, not the expected persona.
        if config.persona_expected is None and active.ref is None:
            receipt = self.store.ingest_event(
                namespace=namespace,
                source_kind="user_report",
                source_instance="desktop-lab-synthetic-admin",
                source_event_id=f"desktop-lab-profile-{seed.profile_id}",
                event_type=REVIEW_APPROVE_EVENT_TYPE,
                observed_at=utc_now_rfc3339(),
                payload={"decision": "approve", "synthetic": True},
            )
            review_ref = {
                "ref_type": "event",
                "namespace": namespace,
                "event_id": receipt.event_id,
            }
            body = _profile_body(seed)
            body["review_ref"] = review_ref
            version = self.profiles.publish_profile(
                namespace=namespace,
                profile_id=seed.profile_id,
                body=body,
                review_ref=review_ref,
                authorization_ref=seed.authorization_ref,
            )
            self.profiles.activate_profile(
                namespace=namespace,
                record_id=version.record_id,
                record_version=version.record_version,
                authorization_ref=seed.authorization_ref,
            )
        self._ensure_self_view(namespace)

        belief_body = {
            "claim": seed.belief_claim,
            "claim_type": seed.belief_claim_type,
            "evidence_links": [],
            "strength": seed.belief_strength,
            "strength_reason": seed.belief_reason,
            # The seeded body is read back through ``parse_belief_body`` like any
            # other record, so it has to satisfy the same claim_type/basis rule.
            # Deriving the basis instead of hardcoding "observation" keeps a Lab
            # seed for ``general_hypothesis`` (or any non-observation claim type)
            # from being born corrupt and breaking every later Context build.
            "inference_basis": INFERENCE_BASIS_BY_CLAIM_TYPE[seed.belief_claim_type],
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": "review when the lab fixture changes",
        }
        if not self.store.current_versions_of_kind(namespace, "belief"):
            self.store.append_record_version(
                namespace=namespace,
                record_id=str(
                    _uuid5(f"desktop-lab-belief:{seed.belief_claim}")
                ),
                record_version=1,
                kind="belief",
                body=belief_body,
                status="supported",
                schema_version="1",
                privacy_class=config.authority.input_privacy_class,
                scope=dict(seed.belief_scope),
            )

        manifest = config.model.manifest
        grants = self.store.model_authorizations(
            namespace=namespace, status="active"
        )
        matching = [
            grant
            for grant in grants
            if grant.provider == config.model.provider
            and grant.model == config.model.model
            and grant.manifest_hash == manifest.manifest_hash
        ]
        if not matching:
            self.store.grant_model_authorization(
                namespace=namespace,
                authorization_ref=seed.authorization_ref,
                provider=config.model.provider,
                model=config.model.model,
                manifest_hash=manifest.manifest_hash,
                max_calls=manifest.max_calls,
                max_output_tokens_per_call=manifest.max_output_tokens_per_call,
                max_wall_seconds=manifest.max_wall_seconds,
            )

    def _provision_managed(self, config: HostConfig) -> None:
        """Create only the neutral resources a fresh public install needs.

        This is not a Persona feature.  The existing C05 profile lifecycle
        still owns publication/activation, and C09 still re-checks the model
        authorization for every call.  A private/external host config never
        takes this branch and therefore keeps its own provisioning policy.
        """

        namespace = config.authority.namespace
        active = self.profiles.get_active_profile(namespace)
        # RG06: an expected persona disables neutral provisioning structurally.
        if config.persona_expected is None and active.ref is None:
            receipt = self.store.ingest_event(
                namespace=namespace,
                source_kind="user_report",
                source_instance="desktop-managed-setup",
                source_event_id=f"desktop-managed-profile-{namespace}",
                event_type=REVIEW_APPROVE_EVENT_TYPE,
                observed_at=utc_now_rfc3339(),
                payload={"decision": "approve", "provisioning": True},
            )
            review_ref = {
                "ref_type": "event",
                "namespace": namespace,
                "event_id": receipt.event_id,
            }
            body = _generic_profile_body(review_ref)
            version = self.profiles.publish_profile(
                namespace=namespace,
                profile_id="quintessence.desktop.general",
                body=body,
                review_ref=review_ref,
                authorization_ref=config.authority.access_ref,
            )
            self.profiles.activate_profile(
                namespace=namespace,
                record_id=version.record_id,
                record_version=version.record_version,
                authorization_ref=config.authority.access_ref,
            )
            self._ensure_self_view(namespace)

        manifest = config.model.manifest
        authorization = self.store.active_model_authorization(
            namespace=namespace,
            provider=config.model.provider,
            model=config.model.model,
            manifest_hash=manifest.manifest_hash,
        )
        if authorization is None:
            self.store.grant_model_authorization(
                namespace=namespace,
                authorization_ref=config.authority.access_ref,
                provider=config.model.provider,
                model=config.model.model,
                manifest_hash=manifest.manifest_hash,
                max_calls=manifest.max_calls,
                max_output_tokens_per_call=manifest.max_output_tokens_per_call,
                max_wall_seconds=manifest.max_wall_seconds,
            )

    # --------------------------------------------------------------- consult

    # ------------------------------------------------------- memory bridge

    def _check_memory_entry(self, config: HostConfig) -> dict[str, Any] | None:
        """Read the configured bridge entry's declared action set, once.

        No import, execution, DB read or model call: this is the deployment
        compatibility check that keeps an older capture/search-only entry from
        being advertised as recall-capable.  ``None`` means the bridge is not
        configured at all.
        """

        spec = config.memory_bridge
        if (
            spec is None
            or not spec.enabled
            or "memory_bridge" not in spec.permissions
        ):
            return None
        if spec.entry == "builtin:scope-recall":
            return {"checked": True, "actions": ["capture", "search", "recall", "time_window"], "missing": [], "reason": None}
        return check_entry_compatibility(
            spec.entry,
            required_actions(
                recall_enabled=config.memory.recall.enabled,
                time_enabled=config.memory.time.enabled,
            ),
        )

    def memory_entry_compatibility(self) -> dict[str, Any] | None:
        """Public copy of the non-secret entry compatibility facts."""

        if self._memory_entry_compat is None:
            return None
        return {
            key: (list(value) if isinstance(value, list) else value)
            for key, value in self._memory_entry_compat.items()
        }

    def _memory_entry_supports(self, action: str) -> bool:
        """Whether a *declared* entry covers one action.

        An undeclared wrapper is treated as unknown rather than incompatible;
        the configuration check reports ``checked=false`` so the caller can see
        the difference between "proved compatible" and "not provable here".
        """

        compat = self._memory_entry_compat
        if compat is None or not compat.get("checked"):
            return True
        return action in tuple(compat.get("actions") or ())

    def _validate_cognition(self, config: HostConfig) -> None:
        """RG05 startup gates: the grant is real, the budget can hold the tools.

        Failing here is a configuration refusal, not a runtime surprise: the
        host must never publish ``cognition.propose`` with a grant it cannot
        prove, nor run a turn that cannot fit its own tool catalog.
        """

        spec = config.cognition
        self._cognition_auto_id: str | None = None
        if not spec.enabled:
            if spec.propose or spec.auto_authorization_id is not None:
                raise RuntimeStartupError(
                    "CONFIG_INVALID",
                    "cognition.propose requires cognition.enabled=true",
                )
            return
        if spec.auto_authorization_id is not None:
            governance = GovernanceStore(self.store)
            row = governance.load_commit_authorization(
                namespace=config.authority.namespace,
                authorization_id=spec.auto_authorization_id,
            )
            now = utc_now_rfc3339()
            if row is None or not governance.authorization_is_active(row, now):
                raise RuntimeStartupError(
                    "COGNITION_GRANT_INVALID",
                    "cognition.auto_authorization_id names no active grant",
                )
            if "cognition.change" not in row.operations:
                raise RuntimeStartupError(
                    "COGNITION_GRANT_INVALID",
                    "the grant does not cover cognition.change",
                )
            if row.max_impact not in ("E0", "E1"):
                raise RuntimeStartupError(
                    "COGNITION_GRANT_INVALID",
                    "the grant ceiling must be E0 or E1",
                )
        # RG05 supplement 01: an instance that enables cognition must opt in
        # to the 64 KiB Context budget explicitly.  The default 24,576 stays
        # untouched for general instances, which do not enable cognition.
        if config.pcs_context_max_bytes < 65536:
            raise RuntimeStartupError(
                "CONTEXT_BUDGET_TOO_SMALL",
                "cognition requires budget.pcs_context_max_bytes >= 65536",
            )
        if spec.propose:
            # Auto on by default, but only for the policy's safe range.  An
            # explicit config id is honoured (and was validated above).
            self._cognition_auto_id = (
                spec.auto_authorization_id
                if spec.auto_authorization_id is not None
                else ensure_default_cognition_grant(self.store, config)
            )

    def _build_cognition_material_factory(self, config: HostConfig):
        """One read-only working-set builder; no model and no write inside."""

        authority = config.authority
        epistemic_access = EpistemicAccess(
            namespace=authority.namespace,
            privacy_classes=authority.privacy_classes,
        )

        def factory():
            if not config.cognition.enabled:
                return None, ()
            working_set = build_working_set(
                self.store,
                namespace=authority.namespace,
                epistemic_access=epistemic_access,
                query_plan=config.query_plan,
                experience_access=self._access.to_experience_access(),
                belief_limit=config.cognition.working_set_limit,
                pending_limit=config.cognition.pending_limit,
            )
            if working_set is None:
                return None, ()
            return (
                cognition_material_from_working_set(working_set),
                working_set.collab_refs,
            )

        return factory

    def _build_expression_material_factory(self, config: HostConfig):
        """One read-only expression segment builder; no model and no write.

        Existing expression preferences are conversation-scoped display inputs,
        not cognition updates.  Reading them must not depend on the cognition
        update switch; scope, revocation, deletion and liveness are still
        enforced by ``build_expression_material``.
        """

        authority = config.authority

        def factory(turn: Any):
            strategy = getattr(turn, "strategy", None) or {}
            plan = plan_from_dict(
                strategy.get("expression_plan")
                if isinstance(strategy, Mapping)
                else None
            )
            return build_expression_material(
                self.store,
                namespace=authority.namespace,
                conversation_id=turn.conversation_id,
                privacy_classes=authority.privacy_classes,
                plan=plan,
                turn_input_ref=turn.input_ref,
            )

        return factory

    def _build_tool_runtime(self, config: HostConfig):
        """A host-owned tool runtime factory; the model never supplies access."""

        authority = config.authority
        namespace = authority.namespace
        query_access = QueryAccess(
            namespace=namespace,
            privacy_classes=authority.privacy_classes,
            purposes=frozenset({"consult"}),
        )
        question_access = QuestionAccess(
            namespace=namespace,
            host_instance=authority.host_instance,
            access_ref=authority.access_ref,
            privacy_classes=authority.privacy_classes,
            permissions=frozenset({"question.read", "question.attention"}),
            purposes=frozenset({"cognition.question"}),
        )
        epistemic_access = EpistemicAccess(
            namespace=namespace,
            privacy_classes=authority.privacy_classes,
        )
        cognition = config.cognition
        effective_authorization = getattr(self, "_cognition_auto_id", None)
        permissions = {"change.propose", "change.review"}
        if effective_authorization is not None:
            permissions.add("change.commit")
        change_access = ChangeAccess(
            namespace=namespace,
            host_instance=authority.host_instance,
            operator=authority.operator,
            access_ref=authority.access_ref,
            permissions=frozenset(permissions),
            privacy_classes=authority.privacy_classes,
            purposes=frozenset({"cognition.change"}),
        )
        change_scope = (
            dict(config.query_plan.scope) if config.query_plan is not None else {}
        )
        self._change_scope = dict(change_scope)
        self._directive_access = ChangeAccess(
            namespace=namespace,
            host_instance=authority.host_instance,
            operator=authority.operator,
            access_ref=authority.access_ref,
            permissions=frozenset(
                {"change.propose", "change.review", "change.commit"}
            ),
            privacy_classes=authority.privacy_classes,
            purposes=frozenset({"cognition.change"}),
        )
        transcript_access = ExperienceAccess(
            namespace=namespace,
            host_instance=authority.host_instance,
            access_ref=authority.access_ref,
            privacy_classes=authority.privacy_classes,
            purposes=frozenset({"consult"}),
        )
        # RG08: the web surface exists only when the host configuration enabled
        # it.  The client reads its credential from the named environment
        # variable; a missing credential yields ``None`` so every web call
        # answers ``web_not_configured`` instead of pretending.
        web = _web_tool_config(config)

        def auto_authorization(operation: str) -> str | None:
            if operation == "cognition.change":
                return effective_authorization
            return None

        def factory(context_id: str):
            # One fresh registry per turn: the orchestrator builds the runtime
            # once at the start of a turn, and ``rebind`` keeps these two sets
            # for the turn's later steps.  A new turn starts empty.
            known_refs: set[str] = set()
            appended_events: set[str] = set()
            memory_reader = (
                self.memory_search if self.memory_search_configured() else None
            )
            return build_tool_runtime(
                self.store,
                queries=self.queries,
                questions=self.questions,
                access=change_access,
                query_access=query_access,
                question_access=question_access,
                epistemic_access=epistemic_access,
                change_scope=change_scope,
                context_id=context_id,
                auto_authorization=auto_authorization,
                transcript_access=transcript_access,
                transcript_host_instance=authority.host_instance,
                minimum_retention=authority.retention,
                include_propose=cognition.propose,
                known_refs=known_refs,
                appended_events=appended_events,
                preference_router=self._active_preference_router,
                memory_reader=memory_reader,
                web=web,
            )

        return factory

    def _ensure_self_view(self, namespace: str) -> None:
        """Project the SelfView only when it does not exist yet.

        Rebuilds on every restart would append a record version, move the state
        revision and make every pending cognition candidate unapprovable after a
        restart for no product reason.  A deleted projection is rebuilt.
        """

        try:
            state = self.self_view.get_self_view(namespace)
        except Redacted:
            state = None
        if state is None or state.ref is None:
            self.self_view.build_self_view(namespace=namespace)

    def _build_experience_bridge(self, config: HostConfig):
        spec = config.memory_bridge
        if spec is None or not spec.enabled:
            self._memory_entry_compat = None
            return None
        assert spec.python_executable is not None
        assert spec.entry is not None
        assert spec.package_path is not None
        assert spec.data_directory is not None
        assert spec.agent_id is not None
        assert spec.installation_id is not None
        assert spec.scope_id is not None
        assert spec.session_id is not None
        self._memory_entry_compat = self._check_memory_entry(config)
        if (
            self._memory_entry_compat is not None
            and self._memory_entry_compat.get("checked")
            and self._memory_entry_compat.get("missing")
        ):
            LOGGER.warning(
                "memory bridge entry lacks actions: %s",
                ",".join(self._memory_entry_compat["missing"]),
            )
        # J11: a copied instance keeps the old absolute directory inside
        # ``memory.sqlite3``.  Without this check the mismatch only shows up as
        # ``IDENTITY_UNBOUND`` at the first recall, far from the cause.
        try:
            adoption = ensure_binding(
                spec.data_directory,
                adopt_by=spec.adopt_by,
                adopt_reason=spec.adopt_reason,
            )
        except InstanceBindingMismatch as exc:
            raise RuntimeStartupError(exc.code, str(exc)) from exc
        except InstanceBindingRefused as exc:
            raise RuntimeStartupError(exc.code, str(exc)) from exc
        if adoption is not None:
            LOGGER.warning(
                "adopted memory instance binding: %s -> %s (by %s)",
                adoption.get("old_data_directory"),
                adoption.get("new_data_directory"),
                adoption.get("adopted_by"),
            )
        adapter = SubprocessCaptureAdapter(
            AdapterConfig(
                python_executable=spec.python_executable,
                entry=spec.entry,
                package_path=spec.package_path,
                data_directory=str(spec.data_directory),
                agent_id=spec.agent_id,
                installation_id=spec.installation_id,
                scope_ids=(spec.scope_id,),
                scope_id=spec.scope_id,
                session_id=spec.session_id,
                test_mode=spec.test_mode,
            )
        )
        self._memory_adapter = adapter
        self._memory_spec = spec
        return ExperienceBridge(
            self.store,
            adapter,
            BridgeConfig(
                scope_id=spec.scope_id,
                capture_timeout_seconds=spec.capture_timeout_seconds,
                batch_timeout_seconds=spec.batch_timeout_seconds,
                lease_seconds=spec.lease_seconds,
                max_attempts=spec.max_attempts,
                retry_backoff_seconds=spec.retry_backoff_seconds,
                max_batch=spec.max_batch,
                timezone=config.memory.timezone,
                time_enabled=config.memory.time.enabled,
            ),
        )

    def _bridge_access(self) -> ExperienceAccess:
        authority = self.config.authority
        return ExperienceAccess(
            namespace=authority.namespace,
            host_instance=authority.host_instance,
            access_ref=authority.access_ref,
            privacy_classes=authority.privacy_classes,
            purposes=frozenset({"memory.bridge"}),
        )

    def _require_memory_bridge_permission(self) -> None:
        spec = self.config.memory_bridge
        if (
            spec is None
            or not spec.enabled
            or "memory_bridge" not in spec.permissions
        ):
            raise RuntimeStartupError(
                "MEMORY_BRIDGE_FORBIDDEN",
                "the memory bridge permission is not configured",
            )

    def memory_bridge_sync(self, *, limit: int = 20) -> dict[str, Any]:
        self._require_memory_bridge_permission()
        if self.experience_bridge is None:
            raise RuntimeStartupError(
                "MEMORY_BRIDGE_UNAVAILABLE", "the memory bridge is not configured"
            )
        namespace = self.config.authority.namespace
        result = self.experience_bridge.sync(
            self._bridge_access(),
            namespace=namespace,
            after_cursor=self.store.bridge_cursor(namespace),
            limit=limit,
        )
        return asdict(result)

    def memory_bridge_deliver(self, *, limit: int | None = None) -> dict[str, Any]:
        self._require_memory_bridge_permission()
        if self.experience_bridge is None:
            raise RuntimeStartupError(
                "MEMORY_BRIDGE_UNAVAILABLE", "the memory bridge is not configured"
            )
        result = self.experience_bridge.deliver(
            self._bridge_access(),
            namespace=self.config.authority.namespace,
            limit=limit,
        )
        return asdict(result)

    def memory_search(
        self,
        *,
        query: str,
        limit: int = 10,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """RG08: one bounded read-only browse/search over the same pinned store.

        The host owns the scope, binding, executable and timeout.  The WebView
        supplies only a query, a limit and an opaque page cursor.  A missing
        bridge permission is a refusal, never a silent empty result.

        A cursor is a **host token**, not the upstream envelope: the real
        upstream cursor is around 280 characters, which would push the page
        itself out of the 1 KiB tool excerpt the model receives.  The token maps
        to the pinned upstream cursor for a bounded time, so continuing a page
        still reuses the adapter's own continuation and never re-runs a query.
        An unknown or expired token is ``MEMORY_CURSOR_STALE``; the caller must
        start the search again rather than conclude the archive is empty.
        """

        self._require_memory_bridge_permission()
        if (
            self.experience_bridge is None
            or self._memory_adapter is None
            or self._memory_spec is None
        ):
            raise RuntimeStartupError(
                "MEMORY_UNAVAILABLE", "the memory bridge is not configured"
            )
        upstream_cursor = None
        if cursor is not None:
            upstream_cursor = self._resolve_page_cursor(cursor)
        assert self._memory_spec.capture_timeout_seconds > 0
        timeout = min(
            MEMORY_SEARCH_TIMEOUT_SECONDS,
            float(self._memory_spec.capture_timeout_seconds),
        )
        try:
            page = search_scope_recall(
                self._memory_adapter,
                query=query,
                limit=limit,
                cursor=upstream_cursor,
                scope_id=self._memory_spec.scope_id,
                timeout_seconds=timeout,
                guard=self.store.require_ready,
            )
        except MemorySearchError as exc:
            raise RuntimeStartupError(exc.code, exc.message) from exc
        result = self._label_memory_subjects(page).as_dict()
        if isinstance(result.get("next_cursor"), str) and result["next_cursor"]:
            result["next_cursor"] = self._mint_page_cursor(result["next_cursor"])
        return result

    #: How many live page cursors one runtime keeps, and for how long.  A page
    #: is continued within the same turn or the next few; a cursor from a
    #: previous process is simply unknown, which is a stable refusal.
    _PAGE_CURSOR_LIMIT = 64
    _PAGE_CURSOR_TTL_SECONDS = 600.0

    def _page_cursors(self) -> dict[str, tuple[str, float]]:
        cursors = getattr(self, "_memory_page_cursors", None)
        if cursors is None:
            cursors = {}
            self._memory_page_cursors = cursors
        return cursors

    def _mint_page_cursor(self, upstream: str) -> str:
        """Remember one upstream cursor and return the short token for it."""

        cursors = self._page_cursors()
        now = time.monotonic()
        for token, (_, expires_at) in list(cursors.items()):
            if expires_at <= now:
                cursors.pop(token, None)
        token = "p1-" + uuid.uuid4().hex[:16]
        cursors[token] = (upstream, now + self._PAGE_CURSOR_TTL_SECONDS)
        while len(cursors) > self._PAGE_CURSOR_LIMIT:
            oldest = next(iter(cursors))
            cursors.pop(oldest, None)
        return token

    def _resolve_page_cursor(self, token: str) -> str:
        """The upstream cursor a live token names, or a stable refusal."""

        if not isinstance(token, str) or not token:
            raise RuntimeStartupError(
                "MEMORY_CURSOR_STALE", "the page cursor is not a valid token"
            )
        cursors = self._page_cursors()
        entry = cursors.get(token)
        if entry is None or entry[1] <= time.monotonic():
            cursors.pop(token, None)
            raise RuntimeStartupError(
                "MEMORY_CURSOR_STALE",
                "this page cursor is unknown or has expired; repeat the search "
                "from the beginning",
            )
        return entry[0]

    def _label_memory_subjects(self, page: Any) -> Any:
        """Attach the speaker to each hit from the delivery row that produced it.

        The upstream inventory returns events with no role, so a hit of the
        assistant's own past sentence is otherwise indistinguishable from the
        user's statement.  The delivery outbox is the only stable join; a ref
        that was never delivered stays unlabelled instead of defaulting to the
        user.
        """

        if not page.items:
            return page
        try:
            rows = self.store.experience_refs_for_external(
                self.config.authority.namespace,
                [item.ref for item in page.items],
            )
        except Exception:  # noqa: BLE001 - labels are an aid, never a failure
            return page
        if not rows:
            return page
        subjects: dict[str, tuple[str, str]] = {}
        for item in page.items:
            row = rows.get(item.ref)
            if row is None:
                continue
            subject = role_for_origin(row["ref_type"], row["origin_kind"])
            if not subject:
                continue
            subjects[item.ref] = (
                subject,
                "{ref_type}:{ref_id}@{ref_version}".format(**row),
            )
        return page.with_subjects(subjects)

    # ------------------------------------------------------ turn postprocess

    def memory_bridge_configured(self) -> bool:
        """Whether this host may project/deliver memory for a terminal turn."""

        spec = self.config.memory_bridge
        return bool(
            spec is not None
            and spec.enabled
            and "memory_bridge" in spec.permissions
            and self.experience_bridge is not None
            and self._memory_entry_supports("capture")
        )

    def memory_search_configured(self) -> bool:
        """Whether the same bridge can also serve a read-only browse/search."""

        return bool(
            self.memory_bridge_configured()
            and self._memory_adapter is not None
            and self._memory_spec is not None
            and self._memory_entry_supports("search")
        )

    def memory_recall_configured(self) -> bool:
        """Whether this host may run automatic recall right now.

        A configured switch is not enough: an older capture/search-only entry
        must not be advertised as recall-capable.  An entry that declares no
        action set stays ``checked=false`` and is not rejected here.
        """

        if not (
            self.memory_search_configured()
            and self.config.memory.recall.enabled
            and self._memory_entry_supports("recall")
        ):
            return False
        if (
            self.config.memory.time.enabled
            and not self._memory_entry_supports("time_window")
        ):
            return False
        return True

    def _build_memory_recall_factory(self, config: HostConfig):
        """One per-turn recall session; the adapter call happens at most once."""

        recall_spec = config.memory.recall
        if not recall_spec.enabled:
            return None
        if not self._memory_entry_supports("recall"):
            return None
        if (
            config.memory.time.enabled
            and not self._memory_entry_supports("time_window")
        ):
            return None
        namespace = config.authority.namespace
        access = self._access.to_experience_access()

        def factory(turn: Any, input_ref: Mapping[str, Any]):
            adapter = self._memory_adapter
            spec = self._memory_spec
            if adapter is None or spec is None:
                return None
            return _TurnMemoryRecall(
                store=self.store,
                namespace=namespace,
                access=access,
                adapter=adapter,
                bridge_spec=spec,
                recall_spec=recall_spec,
                timezone=config.memory.timezone,
                input_ref=input_ref,
                time_enabled=config.memory.time.enabled,
            )

        return factory

    def postprocess_sweep(self) -> dict[str, Any]:
        """One bounded RG04 slice plus the RG05-F01 read-only backlog signal.

        The sweep itself is unchanged (2 turns / 8 refs / 1 delivery / 1.5 s
        capture).  ``backlog`` is an internal return addition consumed only by
        the bridge; it is not an RPC method and it never writes a state row.
        """

        if not self.memory_bridge_configured():
            raise RuntimeStartupError(
                "MEMORY_BRIDGE_UNAVAILABLE", "the memory bridge is not configured"
            )
        config = self.config
        namespace = config.authority.namespace
        max_turns = 2
        report = run_postprocess_sweep(
            self.store,
            self.experience_bridge,
            access=self._bridge_access(),
            namespace=namespace,
            host_instance=config.authority.host_instance,
            max_turns=max_turns,
            max_refs=8,
            deliver_limit=1,
            capture_timeout_seconds=1.5,
        )
        payload = asdict(report)
        try:
            summary = self.store.pending_delivery_summary(namespace)
        except Exception:  # noqa: BLE001 - delivery already committed; scheduling only
            summary = None
        payload["backlog"] = (
            None
            if summary is None
            else {
                "projection_capped": report.turns_projected >= max_turns,
                "pending": summary["pending"],
                "due": summary["due"],
                "next_due_at": summary["next_due_at"],
            }
        )
        return payload

    def postprocess_status(
        self,
        *,
        conversation_id: str,
        turn_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read-only per-turn status; never body text and never a write."""

        config = self.config
        namespace = config.authority.namespace
        host = config.authority.host_instance
        self.store.get_conversation(
            namespace=namespace, host_instance=host, conversation_id=conversation_id
        )
        if turn_ids is None:
            turns = [
                detail.turn
                for detail in self.store.read_turns(
                    namespace=namespace,
                    host_instance=host,
                    conversation_id=conversation_id,
                    limit=20,
                )
            ]
        else:
            turns = []
            for turn_id in turn_ids:
                turn = self.store.turn_for_host(
                    namespace=namespace, host_instance=host, turn_id=turn_id
                )
                if turn.conversation_id != conversation_id:
                    raise InvalidInput("turn does not belong to the conversation")
                turns.append(turn)
        access = self._access.to_experience_access()
        configured = self.memory_bridge_configured()
        statuses = [
            turn_postprocess_status(
                self.store,
                namespace=namespace,
                host_instance=host,
                turn_id=turn.turn_id,
                access=access,
                configured=configured,
            ).as_dict()
            for turn in turns
        ]
        return {
            "conversation_id": conversation_id,
            "configured": configured,
            "watermark": self.store.delivery_watermark(namespace),
            "bridge_cursor": self.store.bridge_cursor(namespace),
            "turns": statuses,
        }

    def postprocess_retry(self, *, turn_id: str) -> dict[str, Any]:
        """Try one bounded projection/delivery for a specific terminal turn."""

        if not self.memory_bridge_configured():
            raise RuntimeStartupError(
                "MEMORY_BRIDGE_UNAVAILABLE", "the memory bridge is not configured"
            )
        config = self.config
        namespace = config.authority.namespace
        host = config.authority.host_instance
        turn = self.store.turn_for_host(
            namespace=namespace, host_instance=host, turn_id=turn_id
        )
        access = self._bridge_access()
        refs = self.store.turn_experience_refs(
            access, namespace=namespace, turn_id=turn.turn_id
        )
        missing = [ref for ref in refs if self._missing_projection(namespace, ref)]
        sync_result = (
            self.experience_bridge.sync_refs(
                access, namespace=namespace, refs=missing, limit=8
            )
            if missing
            else None
        )
        delivery = self.experience_bridge.deliver(
            access,
            namespace=namespace,
            limit=2,
            capture_timeout_seconds=1.5,
        )
        status = turn_postprocess_status(
            self.store,
            namespace=namespace,
            host_instance=host,
            turn_id=turn.turn_id,
            access=self._access.to_experience_access(),
            configured=True,
        ).as_dict()
        return {
            "status": status,
            "sync": None if sync_result is None else asdict(sync_result),
            "delivery": asdict(delivery),
        }

    def _missing_projection(self, namespace: str, ref: dict[str, Any]) -> bool:
        if ref.get("ref_type") == "collab_input":
            ref_type, ref_id, ref_version = (
                "collab_input",
                str(ref["input_id"]),
                int(ref["content_version"]),
            )
        else:
            ref_type, ref_id, ref_version = (
                "collab_artifact",
                str(ref["artifact_id"]),
                int(ref["artifact_version"]),
            )
        return (
            self.store.projection_for_ref(
                namespace,
                ref_type=ref_type,
                ref_id=ref_id,
                ref_version=ref_version,
            )
            is None
        )

    # ------------------------------------------------------------ maintenance

    def _maintenance_store(self) -> MaintenanceStore:
        return MaintenanceStore(self.store)

    def _require_maintenance_permission(self) -> None:
        spec = self.config.maintenance
        if spec is None or not spec.enabled or "maintenance" not in spec.permissions:
            raise RuntimeStartupError(
                "MAINTENANCE_FORBIDDEN",
                "the bounded maintenance entry is not configured",
            )

    def _begin_maintenance_attempt(self, turn_id: str | None) -> threading.Event:
        """Create the one per-attempt cancellation handle.

        Only maintenance uses this.  A foreground send sets the event from the
        reader thread; it never waits for, or acquires, the maintenance model
        lock/queue.
        """

        with self._maintenance_state_lock:
            if self._maintenance_active is not None:
                raise InvalidInput("a maintenance attempt is already active")
            event = threading.Event()
            self._maintenance_cancel_event = event
            self._maintenance_active = {
                "turn_id": turn_id,
                "task_id": None,
                "started_at": utc_now_rfc3339(),
                "cancel_requested": False,
                "cancel_reason": None,
            }
            return event

    def _note_maintenance_task(self, task_id: str | None) -> None:
        if not task_id:
            return
        with self._maintenance_state_lock:
            if self._maintenance_active is not None:
                self._maintenance_active["task_id"] = task_id

    def _end_maintenance_attempt(
        self, event: threading.Event, result: Mapping[str, Any] | None
    ) -> None:
        with self._maintenance_state_lock:
            if self._maintenance_cancel_event is not event:
                return
            payload = dict(result or {})
            self._maintenance_last = {
                "turn_id": (
                    self._maintenance_active or {}
                ).get("turn_id"),
                "task_id": payload.get("task_id"),
                "status": payload.get("status"),
                "state": payload.get("state"),
                "stop_reason": payload.get("stop_reason"),
                "cancel_requested": bool(
                    (self._maintenance_active or {}).get("cancel_requested")
                ),
                "finished_at": utc_now_rfc3339(),
            }
            self._maintenance_active = None
            self._maintenance_cancel_event = None
        try:
            self._maintenance_health_cache = dict(
                self._maintenance_store().health(self.config.authority.namespace)
            )
        except Exception:  # noqa: BLE001 - a status cache must not hide a result
            pass

    def request_maintenance_yield(self, reason: str = "foreground_send") -> bool:
        """Ask the active maintenance attempt to stop waiting.

        This is safe from the reader thread and returns immediately.  It does
        not touch the worker queue, the chat model, or any SQLite connection.
        """

        with self._maintenance_state_lock:
            event = self._maintenance_cancel_event
            if event is None:
                return False
            if self._maintenance_active is not None:
                self._maintenance_active["cancel_requested"] = True
                self._maintenance_active["cancel_reason"] = reason
            event.set()
            return True

    def maintenance_service_view(self) -> dict[str, Any]:
        """In-memory service state for status; no model call, no DB read."""

        with self._maintenance_state_lock:
            active = (
                dict(self._maintenance_active)
                if self._maintenance_active is not None
                else None
            )
            last = dict(self._maintenance_last) if self._maintenance_last else None
        in_flight = bool(getattr(self.maintenance_model, "in_flight", False))
        paused_foreground = bool(
            last
            and last.get("stop_reason") == "foreground_cancel"
            and active is None
        )
        return {
            "enabled": self.maintenance_configured(),
            "running": active is not None,
            "cancelling": bool(active and active.get("cancel_requested")),
            "draining": in_flight,
            "paused_foreground": paused_foreground,
            "active": active,
            "last": last,
            "health": dict(self._maintenance_health_cache),
        }

    def maintenance_status_fast(self) -> dict[str, Any]:
        """Status for the reader thread: no worker/DB wait.

        The job list is intentionally empty here; an operator who names a
        ``task_id`` gets the DB-backed view through ``maintenance.status``.
        """

        self._require_maintenance_permission()
        service = self.maintenance_service_view()
        health = service.pop("health", {})
        return {
            "namespace": self.config.authority.namespace,
            "health": health,
            "service": service,
            "jobs": [],
        }

    def maintenance_status(self, *, task_id: str | None = None) -> dict[str, Any]:
        """Read-only view of the maintenance jobs this host may observe."""
        self._require_maintenance_permission()
        maintenance = self._maintenance_store()
        namespace = self.config.authority.namespace
        health = dict(maintenance.health(namespace))
        self._maintenance_health_cache = health
        service = self.maintenance_service_view()
        if task_id is None:
            return {
                "namespace": namespace,
                "health": health,
                "service": service,
                "jobs": [],
            }
        job = maintenance.get(namespace, task_id)
        return {
            "namespace": namespace,
            "health": health,
            "service": service,
            "jobs": [_job_view(job, maintenance.attempts(namespace, task_id))],
        }

    def maintenance_configured(self) -> bool:
        """Whether the automatic bounded review entry may run at all."""

        spec = self.config.maintenance
        return bool(
            spec is not None
            and spec.enabled
            and "maintenance" in spec.permissions
            and self.config.cognition.enabled
            and self.config.cognition.propose
            and self.memory_bridge_configured()
        )

    def _maintenance_experience_access(self) -> ExperienceAccess:
        authority = self.config.authority
        purposes = set(authority.purposes) | {"review", "consult"}
        return ExperienceAccess(
            namespace=authority.namespace,
            host_instance=authority.host_instance,
            access_ref=authority.access_ref,
            privacy_classes=authority.privacy_classes,
            purposes=frozenset(purposes),
        )

    def _maintenance_change_access(self) -> ChangeAccess:
        authority = self.config.authority
        permissions = {"change.propose", "change.review"}
        if getattr(self, "_cognition_auto_id", None) is not None:
            permissions.add("change.commit")
        return ChangeAccess(
            namespace=authority.namespace,
            host_instance=authority.host_instance,
            operator=authority.operator,
            access_ref=authority.access_ref,
            permissions=frozenset(permissions),
            privacy_classes=authority.privacy_classes,
            purposes=frozenset({"cognition.change"}),
        )

    def _maintenance_epistemic_access(self) -> EpistemicAccess:
        return EpistemicAccess(
            namespace=self.config.authority.namespace,
            privacy_classes=self.config.authority.privacy_classes,
        )

    def _record_maintenance_event(
        self, *, turn_id: str, payload: Mapping[str, Any]
    ) -> None:
        try:
            self.store.record_turn_event(
                namespace=self.config.authority.namespace,
                turn_id=turn_id,
                kind="maintenance",
                payload=dict(payload),
            )
        except Exception:  # noqa: BLE001 - an audit event must not fail a turn
            LOGGER.warning("maintenance turn event could not be recorded")

    def _maintenance_job_page(self, job: Any) -> tuple[str, ...]:
        raw = job.scan_position
        if not isinstance(raw, str):
            return ()
        try:
            document = json.loads(raw)
        except ValueError:
            return ()
        if not isinstance(document, Mapping):
            return ()
        values = document.get("event_ids")
        if not isinstance(values, list):
            return ()
        return tuple(str(item) for item in values if isinstance(item, str) and item)

    def _maintenance_due_job(self, maintenance: MaintenanceStore) -> Any | None:
        namespace = self.config.authority.namespace
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        candidates = maintenance.list_jobs(
            namespace,
            states=tuple(LEASABLE_STATES) + ("leased",),
            limit=50,
        )
        for job in candidates:
            if job.task_type != "cognition.review":
                continue
            if job.state == "leased":
                # A crashed worker leaves a live lease.  Only an expired lease
                # is recoverable; a live one belongs to another attempt.
                if job.lease_until is not None and job.lease_until <= now:
                    return job
                continue
            if job.state == "pending" or job.next_retry_at is None or job.next_retry_at <= now:
                return job
        return None

    def _maintenance_preflight(self, *, task_id: str) -> str | None:
        """Refuse before a slice if the model authorization cannot run it."""

        try:
            self.maintenance_model.preflight(
                namespace=self.config.authority.namespace
            )
        except ModelBudgetExceededError:
            self._maintenance_store().set_state(
                self.config.authority.namespace,
                task_id,
                "waiting_budget",
                reason="model_budget_exhausted",
            )
            return "waiting_budget"
        except ModelNotAuthorizedError:
            self._maintenance_store().set_state(
                self.config.authority.namespace,
                task_id,
                "waiting_authorization",
                reason="model_not_authorized",
            )
            return "waiting_authorization"
        except ModelAdapterError:
            self._maintenance_store().set_state(
                self.config.authority.namespace,
                task_id,
                "waiting_dependency",
                reason="model_unavailable",
            )
            return "waiting_dependency"
        return None

    def _run_maintenance_review_slice(
        self,
        *,
        task_id: str,
        trigger_key: str,
        material: Any,
        should_yield: Callable[[], bool] | None = None,
        cancel_event: threading.Event | None = None,
        allow_repair: bool = False,
    ) -> dict[str, Any]:
        namespace = self.config.authority.namespace
        spec = self.config.maintenance
        assert spec is not None
        self._note_maintenance_task(task_id)
        if getattr(self.maintenance_model, "in_flight", False):
            # A cancelled transport helper from the previous attempt is still
            # draining.  Only maintenance is held back; the chat model is a
            # different adapter and the foreground worker is already free.
            return {
                "status": "draining",
                "task_id": task_id,
                "state": "waiting",
                "stop_reason": "transport_draining",
            }
        maintenance = self._maintenance_store()
        page = tuple(source.event_id for source in material.usable_sources)
        if not page:
            maintenance.set_state(
                namespace, task_id, "paused", reason="no_usable_material"
            )
            return {
                "status": "no_material",
                "task_id": task_id,
                "state": "paused",
                "stop_reason": "no_usable_material",
            }
        stopped = self._maintenance_preflight(task_id=task_id)
        if stopped is not None:
            return {
                "status": stopped,
                "task_id": task_id,
                "state": stopped,
                "stop_reason": stopped,
            }
        worker = cognition_review_worker(
            store=self.store,
            maintenance=maintenance,
            changes=CognitionChangeService(self.store),
            access=self._maintenance_change_access(),
            namespace=namespace,
            task_id=task_id,
            trigger_key=trigger_key,
            material=material,
            context_service=self.contexts,
            model=self.maintenance_model,
            auto_authorization_id=getattr(self, "_cognition_auto_id", None),
            scope=(
                dict(self.config.query_plan.scope)
                if self.config.query_plan is not None
                else {}
            ),
            query_plan=self.config.query_plan,
            epistemic_access=self._maintenance_epistemic_access(),
            experience_access=self._maintenance_experience_access(),
            timezone=self.config.memory.timezone,
            max_events=min(spec.max_events, 2),
            model_call_cap=spec.model_timeout_seconds,
            should_yield=should_yield,
            cancel_event=cancel_event,
            allow_repair=allow_repair,
        )
        try:
            report = run_slice(
                maintenance,
                namespace=namespace,
                task_id=task_id,
                worker=worker,
                limits=SliceLimits(
                    max_scan=len(page),
                    max_process=len(page),
                    max_pages=1,
                    max_seconds=spec.slice_seconds,
                    max_total_units=8,
                    max_slices=3,
                ),
                guard=lambda targets: require_live_targets(
                    self.store, namespace=namespace, targets=targets
                ),
            )
        except InvalidInput as error:
            reason = str(error)
            target_state = (
                "waiting_budget" if "budget" in reason or "limit" in reason else "paused"
            )
            try:
                maintenance.set_state(
                    namespace, task_id, target_state, reason=reason[:120]
                )
            except InvalidInput:
                pass
            return {
                "status": target_state,
                "task_id": task_id,
                "state": target_state,
                "stop_reason": reason[:120],
            }
        return {
            "status": "ran",
            "task_id": task_id,
            "state": report.state,
            "stop_reason": report.stop_reason,
            "scanned": report.scanned,
            "processed": report.processed,
            "units": report.units,
            "deadline_exceeded": report.deadline_exceeded,
        }

    def maintenance_after_turn(
        self,
        turn_id: str,
        *,
        should_yield: Callable[[], bool] | None = None,
        resume_task_id: str | None = None,
        allow_repair: bool = False,
    ) -> dict[str, Any]:
        """One bounded cognition-review attempt with its own cancel handle."""

        self._require_maintenance_permission()
        if resume_task_id is not None:
            try:
                self._maintenance_store().resume(
                    self.config.authority.namespace, resume_task_id
                )
            except InvalidInput:
                # Already pending/waiting: the due-job path below will pick it
                # up.  A terminal/cancelled job is left untouched.
                pass
        event = self._begin_maintenance_attempt(turn_id)
        result: dict[str, Any] | None = None
        try:
            result = self._maintenance_after_turn_impl(
                turn_id,
                should_yield=should_yield,
                cancel_event=event,
                allow_repair=allow_repair,
            )
            return result
        finally:
            self._end_maintenance_attempt(event, result)

    def _maintenance_after_turn_impl(
        self,
        turn_id: str,
        *,
        should_yield: Callable[[], bool] | None = None,
        cancel_event: threading.Event | None = None,
        allow_repair: bool = False,
    ) -> dict[str, Any]:
        """One bounded cognition-review attempt after one terminal turn.

        Called by the desktop bridge only when the foreground is idle.  It never
        raises for an ordinary refusal: the caller gets a status and the turn
        event records an honest reason.  A missing permission is the one
        configuration error the caller must handle.
        """

        self._require_maintenance_permission()
        namespace = self.config.authority.namespace
        host = self.config.authority.host_instance
        try:
            turn = self.store.turn_for_host(
                namespace=namespace, host_instance=host, turn_id=turn_id
            )
        except (RecordNotFound, InvalidInput):
            self._record_maintenance_event(
                turn_id=turn_id, payload={"status": "turn_missing"}
            )
            return {"status": "turn_missing", "turn_id": turn_id}
        if turn.state != "completed":
            self._record_maintenance_event(
                turn_id=turn_id,
                payload={"status": "turn_not_completed", "state": turn.state},
            )
            return {
                "status": "turn_not_completed",
                "turn_id": turn_id,
                "turn_state": turn.state,
            }
        maintenance = self._maintenance_store()
        due = self._maintenance_due_job(maintenance)
        if due is not None:
            page = self._maintenance_job_page(due)
            if not page:
                maintenance.set_state(
                    namespace, due.task_id, "paused", reason="scan_position_missing"
                )
                self._record_maintenance_event(
                    turn_id=turn_id,
                    payload={"status": "job_page_missing", "task_id": due.task_id},
                )
                return {
                    "status": "job_page_missing",
                    "turn_id": turn_id,
                    "task_id": due.task_id,
                }
            source_turn_id = turn_id
            try:
                scan = json.loads(due.scan_position or "{}")
                if isinstance(scan, dict) and isinstance(scan.get("turn_id"), str):
                    source_turn_id = str(scan["turn_id"])
            except (TypeError, ValueError):
                pass
            material = load_turn_material(
                self.store,
                self._maintenance_experience_access(),
                namespace=namespace,
                turn_id=source_turn_id,
                timezone=self.config.memory.timezone,
                max_items=MAX_CONTEXT_ITEMS,
            )
            primary_ids = {
                source.event_id for source in material.primary_usable_sources
            }
            if not page or not (set(page) & primary_ids):
                material = load_event_material(
                    self.store,
                    self._maintenance_experience_access(),
                    namespace=namespace,
                    event_ids=page,
                    timezone=self.config.memory.timezone,
                    max_items=min(self.config.maintenance.max_events, 2),
                )
            result = self._run_maintenance_review_slice(
                task_id=due.task_id,
                trigger_key=due.trigger_key,
                material=material,
                should_yield=should_yield,
                cancel_event=cancel_event,
                allow_repair=allow_repair,
            )
            result["resumed"] = True
            self._record_maintenance_event(
                turn_id=turn_id,
                payload={
                    "status": result.get("status"),
                    "task_id": due.task_id,
                    "state": result.get("state"),
                    "stop_reason": result.get("stop_reason"),
                },
            )
            return result

        material = load_turn_material(
            self.store,
            self._maintenance_experience_access(),
            namespace=namespace,
            turn_id=turn_id,
            timezone=self.config.memory.timezone,
            max_items=MAX_CONTEXT_ITEMS,
        )
        usable = material.primary_usable_sources
        if not usable:
            status = "no_material" if not material.dropped else "incomplete"
            payload = {
                "status": status,
                "turn_id": turn_id,
                "eligible": len(usable),
                "dropped": len(material.dropped),
                "reasons": sorted(
                    {str(item.get("reason") or "unknown") for item in material.dropped}
                )[:8],
            }
            self._record_maintenance_event(turn_id=turn_id, payload=payload)
            return payload
        event_ids = tuple(source.event_id for source in usable)
        trigger_key = f"turn:{turn_id}"
        scope = (
            dict(self.config.query_plan.scope)
            if self.config.query_plan is not None
            else {}
        )
        try:
            job = maintenance.register(
                namespace=namespace,
                task_type="cognition.review",
                trigger_key=trigger_key,
                params={"event_ids": sorted(event_ids), "scope": scope},
                config_version="rg08.maintenance.v1",
                input_watermark=turn_id,
                budget_units=8,
            )
        except MaintenanceConflict:
            self._record_maintenance_event(
                turn_id=turn_id,
                payload={"status": "trigger_conflict", "trigger_key": trigger_key},
            )
            return {"status": "trigger_conflict", "turn_id": turn_id}
        # Persist the exact page on the existing job row before the first slice
        # so a crash/resume never has to guess it from a transient closure.
        try:
            leased, _ = maintenance.lease(namespace, job.task_id, phase="page")
            maintenance.set_scan_position(
                namespace,
                job.task_id,
                {"turn_id": turn_id, "event_ids": list(event_ids)},
                lease_token=leased.lease_token,
            )
            maintenance.finish_attempt(
                namespace,
                job.task_id,
                outcome="waiting",
                next_state="pending",
                stop_reason="page_registered",
                lease_token=leased.lease_token,
            )
        except InvalidInput:
            pass
        result = self._run_maintenance_review_slice(
            task_id=job.task_id,
            trigger_key=trigger_key,
            material=material,
            should_yield=should_yield,
            cancel_event=cancel_event,
            allow_repair=allow_repair,
        )
        result["turn_id"] = turn_id
        self._record_maintenance_event(
            turn_id=turn_id,
            payload={
                "status": result.get("status"),
                "task_id": job.task_id,
                "state": result.get("state"),
                "stop_reason": result.get("stop_reason"),
                "eligible": len(usable),
            },
        )
        return result

    def _manual_cognition_review(
        self, job: Any, maintenance: MaintenanceStore
    ) -> dict[str, Any]:
        """Run one bounded cognition slice for an explicit ``run`` request.

        This is the software resume entry: a ``paused`` job (including one left
        by ``foreground_cancel``) is moved back to ``pending`` here, then one
        slice runs with the manual repair allowance.  No host.json edit and no
        second scheduler are involved.
        """

        namespace = self.config.authority.namespace
        if job.state == "paused":
            job = maintenance.resume(namespace, job.task_id)
        if job.state == "leased":
            raise InvalidInput("maintenance job is leased by an active attempt")
        page = self._maintenance_job_page(job)
        if not page:
            maintenance.set_state(
                namespace, job.task_id, "paused", reason="scan_position_missing"
            )
            return {
                "status": "job_page_missing",
                "task_id": job.task_id,
                "state": "paused",
                "stop_reason": "scan_position_missing",
            }
        source_turn_id: str | None = None
        try:
            scan = json.loads(job.scan_position or "{}")
            if isinstance(scan, dict) and isinstance(scan.get("turn_id"), str):
                source_turn_id = str(scan["turn_id"])
        except (TypeError, ValueError):
            pass
        material = None
        if source_turn_id is not None:
            try:
                candidate = load_turn_material(
                    self.store,
                    self._maintenance_experience_access(),
                    namespace=namespace,
                    turn_id=source_turn_id,
                    timezone=self.config.memory.timezone,
                    max_items=MAX_CONTEXT_ITEMS,
                )
            except Exception:  # noqa: BLE001 - fall back to the persisted page
                candidate = None
            if candidate is not None:
                primary = {
                    source.event_id
                    for source in candidate.primary_usable_sources
                }
                if set(page) & primary:
                    material = candidate
        if material is None:
            material = load_event_material(
                self.store,
                self._maintenance_experience_access(),
                namespace=namespace,
                event_ids=page,
                timezone=self.config.memory.timezone,
                max_items=min(self.config.maintenance.max_events, 2),
            )
        event = self._begin_maintenance_attempt(None)
        result: dict[str, Any] | None = None
        try:
            result = self._run_maintenance_review_slice(
                task_id=job.task_id,
                trigger_key=job.trigger_key,
                material=material,
                should_yield=lambda: False,
                cancel_event=event,
                allow_repair=True,
            )
            return result
        finally:
            self._end_maintenance_attempt(event, result)

    def maintenance_run(
        self,
        *,
        event_ids: Sequence[str] = (),
        task_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Run exactly one bounded slice through the production code path.

        The entry exists so the host can ask for maintenance explicitly; it
        deliberately does **not** schedule anything.  ``guard`` is always the
        real ``require_live_targets`` -- there is no branch that runs a slice
        without it, which is what makes the J08 enforcement hold here too.
        """

        self._require_maintenance_permission()
        spec = self.config.maintenance
        assert spec is not None
        page = [str(item) for item in event_ids if isinstance(item, str) and item]
        if limit is not None:
            page = page[: max(0, min(int(limit), spec.max_events))]
        page = page[: spec.max_events]

        maintenance = self._maintenance_store()
        namespace = self.config.authority.namespace
        if task_id is None and not page:
            # "Run once" is also the software resume entry after a restart:
            # pick the newest paused cognition review instead of creating an
            # unrelated memory-delivery job.  No job is created when one is
            # already waiting, and no cognition is regenerated here.
            for paused_job in maintenance.list_jobs(
                namespace, states=("paused",), limit=20
            ):
                if paused_job.task_type == "cognition.review":
                    return self._manual_cognition_review(paused_job, maintenance)
        if task_id is not None:
            existing = maintenance.get(namespace, task_id)
            if existing.task_type == "cognition.review":
                return self._manual_cognition_review(existing, maintenance)
            if existing.terminal:
                raise InvalidInput(f"maintenance job is {existing.state}")
            if existing.state == "paused":
                maintenance.resume(namespace, task_id)
            if not page:
                page = list(self._maintenance_job_page(existing))
        if task_id is None:
            job = maintenance.register(
                namespace=namespace,
                task_type="memory_delivery",
                trigger_key="host:maintenance.run",
                params={"event_ids": sorted(page)},
                config_version="j09.host.v1",
            )
            task_id = job.task_id

        # The boundary recheck runs *before* the worker touches anything: a
        # page whose sources are already gone must not deliver first and be
        # blamed afterwards.  ``run_slice`` still re-checks every target just
        # before it records the batch, so the window between the two checks is
        # the only exposure.
        try:
            require_live_targets(
                self.store,
                namespace=namespace,
                targets=[{"kind": "event", "id": event_id} for event_id in page],
            )
        except MaintenanceStale as error:
            # A page whose sources are already gone is stopped before the
            # worker runs: no delivery, no item, no checkpoint -- only the
            # reason, so the host can show why nothing happened.
            job = maintenance.set_state(
                namespace, task_id, "paused", reason=f"stale_target:{error}"
            )
            return {
                "task_id": task_id,
                "state": job.state,
                "stop_reason": "stale_target",
                "scanned": 0,
                "processed": 0,
                "units": 0,
                "deadline_exceeded": False,
                "job": _job_view(job, maintenance.attempts(namespace, task_id)),
            }

        worker = memory_delivery_worker(
            store=self.store,
            namespace=namespace,
            scope={"scene": namespace},
            authorization_ref=self.config.authority.access_ref,
            event_ids=page,
        )
        report = run_slice(
            maintenance,
            namespace=namespace,
            task_id=task_id,
            worker=worker,
            limits=SliceLimits(
                max_process=max(len(page), 1),
                max_seconds=spec.slice_seconds,
            ),
            guard=lambda targets: require_live_targets(
                self.store, namespace=namespace, targets=targets
            ),
        )
        return {
            "task_id": task_id,
            "state": report.state,
            "stop_reason": report.stop_reason,
            "scanned": report.scanned,
            "processed": report.processed,
            "units": report.units,
            "deadline_exceeded": report.deadline_exceeded,
            "job": _job_view(report.job, maintenance.attempts(namespace, task_id)),
        }

    def maintenance_control(
        self, *, task_id: str, action: str
    ) -> dict[str, Any]:
        """Pause, resume, cancel or yield one job; never a hidden scheduler.

        The bridge whitelist for this method is exactly ``task_id`` and
        ``action`` (``pcs_bridge.py``) — unlike ``maintenance.status`` and
        ``maintenance.run`` it does **not** accept ``session_id``.  Passing one
        is refused as ``INVALID_PARAMS`` before this function runs, which is
        why callers must address a job by its ``task_id``.
        """

        self._require_maintenance_permission()
        maintenance = self._maintenance_store()
        namespace = self.config.authority.namespace
        if action == "pause":
            job = maintenance.set_state(namespace, task_id, "paused", reason="host_pause")
        elif action == "resume":
            job = maintenance.resume(namespace, task_id)
        elif action == "cancel":
            job = maintenance.set_state(namespace, task_id, "cancelled", reason="host_cancel")
        elif action == "yield":
            return {
                "task_id": task_id,
                "yield_requested": self.request_maintenance_yield("host_yield"),
                "service": self.maintenance_service_view(),
            }
        else:
            raise InvalidInput(f"unknown maintenance action {action!r}")
        return {"task_id": task_id, "job": _job_view(job)}

    def consult(self, question: str) -> dict[str, Any]:
        """Register the input through C05b, then run the real ConsultFacade."""

        config = self.config
        namespace = config.authority.namespace
        try:
            input_now = utc_now_rfc3339()
            input_row = self.store.register_input(
                namespace=namespace,
                origin_kind="user_statement",
                body=question,
                privacy_class=config.authority.input_privacy_class,
                retention=config.authority.retention,
                observed_at=input_now,
                recorded_at=input_now,
                content_version=1,
            )
        except Exception as exc:  # noqa: BLE001 - registration failure is a result
            raise InputRegistrationError(
                "INPUT_REGISTRATION_FAILED",
                f"the host could not register the input ({type(exc).__name__})",
            ) from exc

        input_ref = {
            "ref_type": "collab_input",
            "namespace": namespace,
            "input_id": input_row.input_id,
            "content_version": input_row.content_version,
        }
        request = ConsultRequest(
            input_refs=(input_ref,),
            query_plan=config.query_plan,
            policy_ref=config.authority.policy_ref,
            activation_kind="user_input",
            retention=config.authority.retention,
        )
        result = self.facade.interact(self._access, request)
        return self._public_result(result, input_ref)

    def _public_result(
        self, result: Any, input_ref: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "interaction_id": result.interaction_id,
            "context_id": result.context_id,
            "state": result.state,
            "reply": result.reply,
            "detail": result.detail,
            "query": _query_summary(result.query_body),
            "query_source_refs": list(result.query_source_refs),
            "query_result_ref": result.query_result_ref,
            "reply_artifact_ref": result.reply_artifact_ref,
            "input": {
                "input_ref": input_ref,
                "origin_kind": "user_statement",
                "privacy_class": self.config.authority.input_privacy_class,
                "retention": self.config.authority.retention,
            },
            "destination": self.config.destination.to_public_dict(),
        }

    # ---------------------------------------------------------- conversation

    def conversation_create(self, *, mode: str = "chat"):
        config = self.config
        return self.store.create_conversation(
            namespace=config.authority.namespace,
            host_instance=config.authority.host_instance,
            mode=mode,
        )

    def conversation_list(self, *, limit: int = 20, cursor: str | None = None):
        config = self.config
        return self.store.list_conversations(
            namespace=config.authority.namespace,
            host_instance=config.authority.host_instance,
            limit=limit,
            cursor=cursor,
        )

    def conversation_get(
        self,
        *,
        conversation_id: str,
        before: tuple[int, int] | None = None,
        limit: int = 50,
    ):
        config = self.config
        namespace = config.authority.namespace
        host = config.authority.host_instance
        conversation = self.store.get_conversation(
            namespace=namespace,
            host_instance=host,
            conversation_id=conversation_id,
        )
        messages = self.store.read_messages(
            namespace=namespace,
            host_instance=host,
            conversation_id=conversation_id,
            access=self._access.to_experience_access(),
            before=before,
            limit=limit,
        )
        # RG08 card C2: the message page is the window.  Return exactly the
        # turns needed to order and group those messages, not the latest 50
        # turns.  The old latest-turn window left empty turn groups above the
        # latest message page, and an older page could reference turns outside
        # the latest window.
        page_turn_ids: list[str] = []
        seen_turn_ids: set[str] = set()
        for view in messages.messages:
            if view.turn_id not in seen_turn_ids:
                seen_turn_ids.add(view.turn_id)
                page_turn_ids.append(view.turn_id)
        turns = (
            self.store.read_turns_by_ids(
                namespace=namespace,
                host_instance=host,
                conversation_id=conversation_id,
                turn_ids=page_turn_ids,
            )
            if page_turn_ids
            else ()
        )
        return conversation, turns, messages

    def conversation_search(
        self,
        *,
        query: str,
        limit: int = 5,
        conversation_id: str | None = None,
        before: str | None = None,
        include_full: bool = False,
    ) -> dict[str, Any]:
        """RG04: bounded transcript-archive search; never a memory query."""

        config = self.config
        page = self.store.search_transcript(
            namespace=config.authority.namespace,
            host_instance=config.authority.host_instance,
            access=self._access.to_experience_access(),
            query=query,
            limit=limit,
            conversation_id=conversation_id,
            before=before,
            include_full=include_full,
            minimum_retention=config.authority.retention,
        )
        return page.as_dict()

    def conversation_send(
        self,
        *,
        conversation_id: str,
        client_request_id: str,
        text: str,
    ):
        """T0 + T1.  Returns before any model call; ``accepted`` is sent next."""

        config = self.config
        namespace = config.authority.namespace
        try:
            profile_ref = resolve_turn_profile(self.profiles, namespace)
        except PersonaNotReady as exc:
            # RG06: the active persona was resolved at startup; losing it now is
            # a corrupt instance, never a reason to recreate a neutral persona.
            code = (
                "PROFILE_CORRUPT"
                if config.persona_expected is not None
                else "PROFILE_MISSING"
            )
            raise ConversationRequestError("HOST_NOT_READY", code) from exc
        conversation = self.store.get_conversation(
            namespace=namespace,
            host_instance=config.authority.host_instance,
            conversation_id=conversation_id,
        )
        persona = self._persona_plan(
            namespace, profile_ref, text, conversation.mode
        )
        plan, targets = self._expression_turn_plan(
            text, conversation.conversation_id
        )
        sdrpe = SdrpePlan.resolve(
            config.sdrpe.mode, protocol_version=config.model.model_protocol
        )
        creation = self.store.register_turn(
            namespace=namespace,
            host_instance=config.authority.host_instance,
            conversation_id=conversation.conversation_id,
            client_request_id=client_request_id,
            text=text,
            privacy_class=config.authority.input_privacy_class,
            retention=config.authority.retention,
            profile_ref=profile_ref,
            strategy=_turn_strategy(
                conversation.mode,
                config.model.model_protocol,
                _turn_limits(config),
                persona=persona,
                sdrpe=sdrpe.to_strategy(),
                expression_plan=plan.as_dict() if plan is not None else None,
                expression_targets=[item.as_dict() for item in targets],
            ),
            on_event=self._event_sink,
        )
        return conversation, creation

    def _expression_turn_plan(
        self, text: str, conversation_id: str
    ) -> tuple[Any, tuple[PreferenceTarget, ...]]:
        """T0: classify the current input and snapshot the old preference(s).

        The classification is a pure function of the user's text; the model never
        contributes to it.  The targets name the *exact* old versions a
        correction may act on, so the later model proposal cannot be matched to
        a different record.  J11 consolidation: the conversation-scoped tentative
        record is gone, so nothing is snapshotted for it and no coreference to an
        active tentative value is resolved; an explicit directive or correction
        still snapshots the confirmed and pending targets it may replace.
        """

        plan = classify_directive(text)
        targets: list[PreferenceTarget] = []
        if plan.dimension is not None and plan.kind in (
            KIND_EXPLICIT_LONG_TERM,
            KIND_EXPLICIT_CORRECTION,
        ):
            confirmed = self._confirmed_preference_target(plan.dimension)
            if confirmed is not None:
                targets.append(confirmed)
            targets.extend(self._pending_preference_targets(plan.dimension))
        return plan, tuple(targets)

    def _confirmed_preference_target(
        self, dimension: str
    ) -> PreferenceTarget | None:
        from quintessence_pcs.domains.cognition.beliefs import (
            BELIEF_KIND,
            belief_evidence_is_live,
            parse_belief_body,
        )
        from quintessence_pcs.domains.persona.expression_directives import (
            parse_preference_marker,
        )

        namespace = self.config.authority.namespace
        found: list[Any] = []
        for version in self.store.current_versions_of_kind(namespace, BELIEF_KIND):
            if version.status != "supported":
                continue
            if version.privacy_class not in self.config.authority.privacy_classes:
                continue
            try:
                body = parse_belief_body(version)
            except Exception:  # noqa: BLE001 - a malformed belief is not a target
                continue
            if body.get("claim_type") != "user_preference":
                continue
            marker = parse_preference_marker(body.get("review_condition"))
            if marker is None or marker[0] != dimension:
                continue
            if not belief_evidence_is_live(
                self.store, namespace=namespace, body=body
            ):
                continue
            found.append((version.recorded_at, version, marker[1]))
        if not found:
            return None
        found.sort(key=lambda item: item[0], reverse=True)
        _, version, value = found[0]
        return PreferenceTarget(
            kind="confirmed",
            dimension=dimension,
            value=value,
            record_id=version.record_id,
            record_version=version.record_version,
        )

    def _pending_preference_targets(
        self, dimension: str
    ) -> list[PreferenceTarget]:
        from quintessence_pcs.domains.persona.expression_directives import (
            parse_preference_marker,
        )
        from quintessence_pcs.store.governance import GovernanceStore

        namespace = self.config.authority.namespace
        governance = GovernanceStore(self.store)
        targets: list[PreferenceTarget] = []
        for row in self.store.pending_change_candidates(namespace, limit=20):
            artifact_id = str(row["artifact_id"])
            if governance.lifecycle(namespace, artifact_id) != "pending":
                continue
            try:
                artifact = self.store.load_artifact(
                    namespace=namespace,
                    artifact_id=artifact_id,
                    artifact_version=int(row["artifact_version"]),
                )
            except Exception:  # noqa: BLE001 - an unreadable candidate is skipped
                continue
            proposal = getattr(artifact, "proposal", None) or {}
            changes = proposal.get("changes") if isinstance(proposal, Mapping) else None
            if not isinstance(changes, Sequence):
                continue
            for change in changes:
                body = change.get("body") if isinstance(change, Mapping) else None
                if not isinstance(body, Mapping) or body.get("claim_type") != "user_preference":
                    continue
                marker = parse_preference_marker(body.get("review_condition"))
                if marker is None or marker[0] != dimension:
                    continue
                targets.append(
                    PreferenceTarget(
                        kind="pending",
                        dimension=dimension,
                        value=marker[1],
                        artifact_id=artifact_id,
                    )
                )
                break
        return targets

    def _persona_plan(
        self,
        namespace: str,
        profile_ref: dict[str, Any],
        text: str,
        mode: str,
    ) -> dict[str, Any]:
        """T0: read the pinned profile body once and snapshot the persona plan.

        ``mode`` is the conversation row's stored mode (``chat``/``work``); it
        gates the general ``owner-base`` demo so only chat turns can receive it.
        """

        try:
            version = self.store.get_version(
                namespace,
                profile_ref["record_id"],
                int(profile_ref["record_version"]),
            )
        except (RecordNotFound, Redacted) as exc:
            raise ConversationRequestError(
                "HOST_NOT_READY", "PROFILE_CORRUPT"
            ) from exc
        if version.kind != "persona_profile":
            raise ConversationRequestError("HOST_NOT_READY", "PROFILE_CORRUPT")
        body = dict(version.body)
        return persona_plan(
            state=self.persona,
            body=body,
            user_text=text,
            speaker=self.config.speaker,
            max_items=self.config.persona_examples_max_items,
            max_bytes=self.config.persona_examples_max_bytes,
            mode=mode,
        )

    def conversation_execute(self, *, conversation, creation):
        config = self.config
        self._text_expression_turn(creation.turn, creation.input_ref)
        router = self._build_preference_router(creation.turn, creation.input_ref)
        self._active_preference_router = router
        try:
            return self.conversations.execute(
                conversation=conversation,
                turn=creation.turn,
                input_ref=creation.input_ref,
                profile_ref=creation.turn.profile_ref,
                access=self._access.to_experience_access(),
                retention=config.authority.retention,
                policy_ref=config.authority.policy_ref,
            )
        finally:
            self._active_preference_router = None

    # ---------------------------------------------------- expression prefs

    def _text_expression_turn(self, turn: Any, input_ref: Mapping[str, Any]) -> None:
        """Apply the deterministic part of the directive plan before Context."""

        if not self.config.cognition.enabled:
            return
        plan = plan_from_dict(
            (turn.strategy or {}).get("expression_plan")
            if isinstance(turn.strategy, Mapping)
            else None
        )
        if plan is None:
            return
        targets = [
            target
            for target in (
                PreferenceTarget.from_dict(item)
                for item in (
                    (turn.strategy or {}).get("expression_targets") or ()
                    if isinstance(turn.strategy, Mapping)
                    else ()
                )
            )
            if target is not None
        ]
        try:
            text = self.store.load_input(
                namespace=self.config.authority.namespace,
                input_id=str(input_ref["input_id"]),
                content_version=int(input_ref["content_version"]),
            ).body
        except (Redacted, RecordNotFound):
            return
        try:
            if plan.kind in (KIND_EXPLICIT_LONG_TERM, KIND_EXPLICIT_CORRECTION):
                for target in targets:
                    # J11 consolidation: tentative records no longer exist, so
                    # an explicit statement has nothing to revoke there.  Only a
                    # pending candidate is withdrawn.
                    if target.kind == "pending" and target.artifact_id:
                        CognitionChangeService(self.store).withdraw_candidate(
                            self._directive_access,
                            candidate_id=target.artifact_id,
                            decision_id=str(
                                uuid.uuid5(
                                    uuid.NAMESPACE_URL,
                                    f"{self.config.authority.namespace}:"
                                    f"{turn.turn_id}:withdraw:{target.artifact_id}",
                                )
                            ),
                            reason_code="superseded_by_user_correction",
                        )
            # J11 consolidation: the host no longer records a conversation-
            # scoped tentative preference from an ordinary remark.  Adapting to
            # the current turn belongs to the model + persona; only an explicit,
            # reviewed, committed preference becomes lasting cognition.
            self._expression_error = None
        except Exception as exc:  # noqa: BLE001 - chat must not fail on a pref write
            self._expression_error = f"{type(exc).__name__}: {exc}"

    def _build_preference_router(self, turn: Any, input_ref: Mapping[str, Any]):
        if not self.config.cognition.enabled or not self.config.cognition.propose:
            return None
        strategy = turn.strategy or {}
        plan = plan_from_dict(
            strategy.get("expression_plan") if isinstance(strategy, Mapping) else None
        )
        if plan is None:
            return None
        targets = [
            target
            for target in (
                PreferenceTarget.from_dict(item)
                for item in (
                    strategy.get("expression_targets") or ()
                    if isinstance(strategy, Mapping)
                    else ()
                )
            )
            if target is not None
        ]
        context = TurnPreferenceContext(
            namespace=self.config.authority.namespace,
            conversation_id=turn.conversation_id,
            turn_id=turn.turn_id,
            input_ref=dict(input_ref),
            plan=plan,
            targets=tuple(targets),
        )
        router = PreferenceRouter(
            store=self.store,
            changes=CognitionChangeService(self.store),
            preferences=self.expression_preferences,
            directive_access=self._directive_access,
            turn=context,
            now=utc_now_rfc3339,
        )
        return bind_router_context(router, scope=self._change_scope)

    def conversation_turn_result(self, *, turn_id: str):
        config = self.config
        return self.store.turn_result(
            namespace=config.authority.namespace,
            host_instance=config.authority.host_instance,
            turn_id=turn_id,
            access=self._access.to_experience_access(),
        )

    def conversation_turn_events(
        self, *, turn_id: str, after_seq: int = 0, limit: int = 100
    ):
        from quintessence_pcs.conversation import dto as conversation_dto

        config = self.config
        page = self.store.read_turn_events(
            namespace=config.authority.namespace,
            host_instance=config.authority.host_instance,
            turn_id=turn_id,
            after_seq=after_seq,
            limit=limit,
        )
        return conversation_dto.turn_event_page_dict(page)

    def conversation_turn_steps(
        self, *, turn_id: str, after_step_no: int = 0, limit: int = 20
    ):
        from quintessence_pcs.conversation import dto as conversation_dto

        config = self.config
        page = self.store.read_turn_steps(
            namespace=config.authority.namespace,
            host_instance=config.authority.host_instance,
            turn_id=turn_id,
            access=self._access.to_experience_access(),
            after_step_no=after_step_no,
            limit=limit,
        )
        return conversation_dto.step_page_dict(page)

    def conversation_usage(
        self, *, conversation_id: str, turn_id: str | None = None
    ) -> dict[str, Any]:
        """Read-only token/speed/Context-byte projection for one conversation."""

        from quintessence_pcs.conversation import dto as conversation_dto

        config = self.config
        view = self.store.conversation_usage(
            namespace=config.authority.namespace,
            host_instance=config.authority.host_instance,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )
        return conversation_dto.usage_dict(view)

    def turn_trace(self, *, turn_id: str) -> dict[str, Any]:
        """Read-only stages/attempts/final-origin projection for one turn."""

        from quintessence_pcs.conversation import dto as conversation_dto

        config = self.config
        view = self.store.turn_trace(
            namespace=config.authority.namespace,
            host_instance=config.authority.host_instance,
            turn_id=turn_id,
        )
        return conversation_dto.trace_dict(view)

    # --------------------------------------------------------------- persona

    def persona_status(self) -> dict[str, Any]:
        """Read-only persona summary for ``app.info`` / ``profile.status``.

        Uses the in-memory startup resolution (plus the refresh after a
        successful ``profile.activate``); no DB read happens on the reader
        thread.  It never contains the profile body.
        """

        config = self.config
        state = self.persona
        expected = config.persona_expected
        return {
            "state": "ready" if state is not None else "not_ready",
            "source": None if state is None else state.source,
            "profile_id": None if state is None else state.profile_id,
            "profile_version": None if state is None else state.profile_version,
            "record_id": None if state is None else state.record_id,
            "record_version": None if state is None else state.record_version,
            "asset_hash": None if state is None else state.asset_hash,
            "expected": None if expected is None else expected.to_dict(),
            "matches_expected": persona_matches_expected(state, expected),
            "speaker": (
                None if config.speaker is None else config.speaker.to_dict()
            ),
            "reason_code": None if state is not None else self.reason_code,
        }

    def persona_capabilities(self) -> dict[str, bool]:
        config = self.config
        publish = bool(self.ready and config.persona_control_publish)
        activate = bool(self.ready and config.persona_control_activate)
        return {
            "read": bool(self.ready),
            "publish": publish,
            "activate": activate,
            # UI-F02: the instance manager may open the full body only when the
            # instance enabled persona management at all (publish or activate).
            # No new config key exists; a neutral instance keeps the summary.
            "read_body": bool(self.ready and (publish or activate)),
            "import": bool(self.ready and publish),
        }

    def preference_capabilities(self) -> dict[str, bool]:
        """The low-impact preference view/write booleans ``app.info`` discloses."""

        return {"read": bool(self.ready), "revoke": bool(self.ready)}

    def profile_read(self) -> dict[str, Any]:
        """The active profile **summary**; the body never leaves the host (Q4)."""

        namespace = self.config.authority.namespace
        try:
            state = read_active_persona(self.profiles, self.store, namespace)
        except PersonaUnavailable as exc:
            raise PersonaControlError(exc.code, exc.detail) from exc
        try:
            version = self.store.get_version(
                namespace, state.record_id, state.record_version
            )
        except (RecordNotFound, Redacted) as exc:
            raise PersonaControlError(
                "PROFILE_CORRUPT", "the active profile version is not readable"
            ) from exc
        body = dict(version.body)
        style = body.get("style") if isinstance(body.get("style"), Mapping) else {}
        verbosity = (
            style.get("verbosity_by_context")
            if isinstance(style.get("verbosity_by_context"), Mapping)
            else {}
        )
        principles = body.get("principles")
        examples = body.get("examples")
        return {
            "profile_id": state.profile_id,
            "profile_version": state.profile_version,
            "source": state.source,
            "record_id": state.record_id,
            "record_version": state.record_version,
            "asset_hash": state.asset_hash,
            "renderer_compatibility": body.get("renderer_compatibility"),
            "style_summary": {
                "language": style.get("language"),
                "tone": style.get("tone"),
                "verbosity_by_context": {
                    "casual": verbosity.get("casual"),
                    "technical": verbosity.get("technical"),
                },
            },
            "principles_count": len(principles) if isinstance(principles, list) else 0,
            "examples_count": len(examples) if isinstance(examples, list) else 0,
            "speaker": (
                None
                if self.config.speaker is None
                else self.config.speaker.to_dict()
            ),
        }

    def profile_publish(
        self, *, profile_id: str, body: Any, review_event_id: str
    ) -> dict[str, Any]:
        """Publish one profile version through the real reviewed service."""

        config = self.config
        if not config.persona_control_publish:
            raise PersonaControlError(
                "PERSONA_CONTROL_DISABLED",
                "this host has not enabled persona publishing",
            )
        namespace = config.authority.namespace
        if not isinstance(body, Mapping):
            raise InvalidInput("profile body must be a JSON object")
        payload = dict(body)
        # The host owns the review reference; the client supplies only the id.
        payload["review_ref"] = {
            "ref_type": "event",
            "namespace": namespace,
            "event_id": review_event_id,
        }
        version = self.profiles.publish_profile(
            namespace=namespace,
            profile_id=profile_id,
            body=payload,
            review_ref=payload["review_ref"],
            authorization_ref=config.authority.access_ref,
        )
        return {
            "profile_id": profile_id,
            "record_id": version.record_id,
            "record_version": version.record_version,
            "active_changed": False,
        }

    def profile_activate(self, *, profile_id: str, record_version: int) -> dict[str, Any]:
        """Activate one exact version; the next turn uses it (Q2)."""

        config = self.config
        if not config.persona_control_activate:
            raise PersonaControlError(
                "PERSONA_CONTROL_DISABLED",
                "this host has not enabled persona activation",
            )
        namespace = config.authority.namespace
        record_id = profile_record_id(namespace, profile_id)
        try:
            verify_activation_target(
                self.store, namespace, record_id, int(record_version)
            )
        except PersonaExpectationConflict as exc:
            raise PersonaControlError(
                "PERSONA_EXPECTATION_CONFLICT", exc.detail
            ) from exc
        self.profiles.activate_profile(
            namespace=namespace,
            record_id=record_id,
            record_version=int(record_version),
            authorization_ref=config.authority.access_ref,
        )
        self._refresh_persona_after_activate()
        return {
            "profile_id": profile_id,
            "record_version": int(record_version),
            "active": True,
        }

    def _refresh_persona_after_activate(self) -> None:
        """Keep the status cache truthful; the DB stays authoritative."""

        namespace = self.config.authority.namespace
        source = self.persona.source if self.persona is not None else "active"
        try:
            state = read_active_persona(self.profiles, self.store, namespace)
            self.persona = with_source(state, source)
        except PersonaUnavailable:
            self.persona = None

    # ------------------------------------------------- persona management
    #
    # UI-F02: the instance manager's view of the published profiles and the
    # reviewed publish path.  Nothing here invents a profile, a version or an
    # approval: the list comes from persisted versions, activation is read from
    # ``namespace_state.active_profile_*`` (a version row's own ``status`` is
    # always ``active`` and means nothing about which version is in use), and a
    # publish still goes through ``PersonaProfileService.publish_profile`` with
    # a real ``persona.review.approve`` event.

    def profile_list(self) -> dict[str, Any]:
        """Bounded profile/version summary; the body is never included."""

        namespace = self.config.authority.namespace
        active = self._active_profile_ref(namespace)
        profiles: list[dict[str, Any]] = []
        truncated = False
        for version in self.store.current_versions_of_kind(
            namespace, PERSONA_PROFILE_KIND
        ):
            if len(profiles) >= PROFILE_LIST_MAX_PROFILES:
                truncated = True
                break
            body = version.body if isinstance(version.body, Mapping) else {}
            profile_id = body.get("profile_id")
            if not isinstance(profile_id, str):
                continue
            versions, versions_truncated = self._profile_versions(
                namespace, version.record_id, active
            )
            profiles.append(
                {
                    "profile_id": profile_id,
                    "record_id": version.record_id,
                    "is_active": bool(
                        active is not None
                        and active["record_id"] == version.record_id
                    ),
                    "current": {
                        "record_version": int(version.record_version),
                        "profile_version": body.get("profile_version"),
                        "schema_version": version.schema_version,
                        "record_status": version.status,
                        "recorded_at": version.recorded_at,
                        "asset_hash": body.get("asset_hash"),
                        "renderer_compatibility": body.get(
                            "renderer_compatibility"
                        ),
                        "style_summary": _profile_style_summary(body),
                        "principles_count": _list_count(body.get("principles")),
                        "examples_count": _list_count(body.get("examples")),
                    },
                    "versions": versions,
                    "versions_total": self.store.count_versions_of_record(
                        namespace, version.record_id
                    ),
                    "versions_truncated": versions_truncated,
                }
            )
        profiles.sort(key=lambda item: (not item["is_active"], item["profile_id"]))
        return {"active": active, "profiles": profiles, "truncated": truncated}

    def _active_profile_ref(self, namespace: str) -> dict[str, Any] | None:
        pointer = self.store.active_profile_pointer(namespace)
        if pointer.record_id is None or pointer.record_version is None:
            return None
        profile_id: str | None = None
        degraded = False
        try:
            version = self.store.get_version(
                namespace, pointer.record_id, pointer.record_version
            )
            body = version.body if isinstance(version.body, Mapping) else {}
            if isinstance(body.get("profile_id"), str):
                profile_id = body["profile_id"]
        except (RecordNotFound, Redacted):
            degraded = True
        return {
            "record_id": pointer.record_id,
            "record_version": int(pointer.record_version),
            "profile_id": profile_id,
            "degraded": degraded,
        }

    def _profile_versions(
        self,
        namespace: str,
        record_id: str,
        active: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], bool]:
        versions: list[dict[str, Any]] = []
        for version in self.store.versions_of_record(
            namespace, record_id, limit=PROFILE_LIST_MAX_VERSIONS
        ):
            body = version.body if isinstance(version.body, Mapping) else {}
            review_ref = (
                body.get("review_ref")
                if isinstance(body.get("review_ref"), Mapping)
                else {}
            )
            review_event_id = review_ref.get("event_id")
            versions.append(
                {
                    "record_version": int(version.record_version),
                    "profile_version": body.get("profile_version"),
                    "schema_version": version.schema_version,
                    "recorded_at": version.recorded_at,
                    "record_status": version.status,
                    "is_active_version": bool(
                        active is not None
                        and active["record_id"] == record_id
                        and active["record_version"] == int(version.record_version)
                    ),
                    "review_event_id": (
                        review_event_id
                        if isinstance(review_event_id, str)
                        else None
                    ),
                }
            )
        total = self.store.count_versions_of_record(namespace, record_id)
        return versions, total > len(versions)

    def profile_read_body(
        self, *, record_id: str, record_version: int
    ) -> dict[str, Any]:
        """Return one exact stored body to the instance manager, on request.

        This is the only place a profile body may leave the host.  It requires
        the management capability, writes one audit row (no body, no path) and
        never logs or truncates the text: a body above the host limit is an
        explicit refusal.
        """

        if not self.persona_capabilities()["read_body"]:
            raise PersonaControlError(
                "PERSONA_CONTROL_DISABLED",
                "this host has not enabled instance-manager persona access",
            )
        namespace = self.config.authority.namespace
        version = self.store.get_version(namespace, record_id, record_version)
        if version.kind != PERSONA_PROFILE_KIND:
            raise PersonaControlError("NOT_FOUND", "the record is not a persona profile")
        # UI-F02 review: the internal review reference is approval metadata, not
        # persona text.  The manager sees the body, never the event id it points
        # at; the digest and size are defined on the same review-ref-free body
        # the publish path binds.
        body = {key: value for key, value in dict(version.body).items() if key != "review_ref"}
        size_bytes = len(canonical_json_text(body).encode("utf-8"))
        if size_bytes > PROFILE_BODY_MAX_BYTES:
            raise PersonaControlError(
                "INVALID_PARAMS",
                "the stored profile body exceeds the 64 KiB host limit",
            )
        with self.store.transaction():
            self.store.record_authorization(
                namespace=namespace,
                operation="persona.profile.read_body",
                target_kind="record",
                target_id=record_id,
                target_version=int(record_version),
                authorization_ref=self.config.authority.access_ref,
            )
        return {
            "record_id": record_id,
            "record_version": int(record_version),
            "profile_id": body.get("profile_id"),
            "profile_version": body.get("profile_version"),
            "schema_version": version.schema_version,
            "body": body,
            "body_sha256": profile_body_digest(body),
            "size_bytes": size_bytes,
        }

    # ---------------------------------------------------- persona import
    #
    # "Import an existing configuration" means: the instance manager puts one
    # JSON profile body into ``<instance root>/persona-import/``; the host lists
    # the directory, reads and validates the file itself, keeps the body in
    # memory only, and publishes it through the reviewed path.  The WebView
    # never supplies a path and never receives the body before an explicit,
    # capability-gated body read.

    def profile_import_list(self) -> dict[str, Any]:
        if not self.persona_capabilities()["import"]:
            raise PersonaControlError(
                "PERSONA_CONTROL_DISABLED",
                "this host has not enabled persona publishing",
            )
        try:
            directory = self._persona_import_dir()
        except PersonaControlError as exc:
            return {
                "available": False,
                "reason": exc.code,
                "dir_label": PERSONA_IMPORT_DIRNAME,
                "files": [],
            }
        files: list[dict[str, Any]] = []
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            if len(files) >= PROFILE_LIST_MAX_PROFILES:
                break
            if entry.is_symlink() or not entry.is_file():
                continue
            if not valid_persona_import_name(entry.name):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            files.append(
                {
                    "name": entry.name,
                    "size_bytes": int(stat.st_size),
                    "modified_at": _import_modified_at(stat.st_mtime),
                }
            )
        return {
            "available": True,
            "reason": None,
            "dir_label": PERSONA_IMPORT_DIRNAME,
            "files": files,
        }

    def profile_import_preview(self, *, name: str) -> dict[str, Any]:
        if not self.persona_capabilities()["import"]:
            raise PersonaControlError(
                "PERSONA_CONTROL_DISABLED",
                "this host has not enabled persona publishing",
            )
        directory = self._persona_import_dir()
        if not valid_persona_import_name(name):
            raise PersonaControlError(
                "IMPORT_INVALID", "the file name is not an accepted JSON name"
            )
        path = directory / name
        if path.is_symlink():
            raise PersonaControlError("IMPORT_INVALID", "the file is a link")
        resolved = path.resolve()
        try:
            resolved.relative_to(directory)
        except ValueError as exc:
            raise PersonaControlError(
                "IMPORT_INVALID", "the file escapes the persona-import directory"
            ) from exc
        if not resolved.is_file():
            raise PersonaControlError("IMPORT_INVALID", "the file does not exist")
        try:
            size = resolved.stat().st_size
        except OSError as exc:
            raise PersonaControlError(
                "IMPORT_INVALID", "the file cannot be read"
            ) from exc
        if size > PERSONA_IMPORT_MAX_BYTES:
            raise PersonaControlError(
                "IMPORT_INVALID", "the file exceeds the 256 KiB import limit"
            )
        try:
            text = resolved.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PersonaControlError(
                "IMPORT_INVALID", "the file is not UTF-8 JSON"
            ) from exc
        except OSError as exc:
            raise PersonaControlError(
                "IMPORT_INVALID", "the file cannot be read"
            ) from exc
        try:
            document = strict_json_loads(text)
        except Exception:  # noqa: BLE001 - a malformed file is a refusal
            raise PersonaControlError(
                "IMPORT_INVALID", "the file is not valid JSON"
            ) from None
        if not isinstance(document, Mapping):
            raise PersonaControlError(
                "IMPORT_INVALID", "the file must contain one JSON object"
            )
        warnings: list[str] = []
        if "review_ref" in document:
            warnings.append("review_ref_replaced")
        content = {key: value for key, value in document.items() if key != "review_ref"}
        body = self._validated_import_body(content)
        profile_id = str(body["profile_id"])
        digest = profile_body_digest(body)
        body_size = len(canonical_json_text(body).encode("utf-8"))
        if body_size > PROFILE_BODY_MAX_BYTES:
            raise PersonaControlError(
                "IMPORT_INVALID", "the profile body exceeds the 64 KiB host limit"
            )
        self._prune_import_drafts()
        if len(self._import_drafts) >= PERSONA_IMPORT_MAX_DRAFTS:
            oldest = min(
                self._import_drafts.items(),
                key=lambda item: item[1]["expires_monotonic"],
            )[0]
            self._import_drafts.pop(oldest, None)
        import_id = str(uuid.uuid4())
        self._import_drafts[import_id] = {
            "body": body,
            "digest": digest,
            "size_bytes": body_size,
            "profile_id": profile_id,
            "expires_monotonic": time.monotonic() + PERSONA_IMPORT_TTL_SECONDS,
        }
        return {
            "import_id": import_id,
            "profile_id": profile_id,
            "profile_version": body.get("profile_version"),
            "schema_version": PROFILE_SCHEMA_VERSION,
            "asset_hash": body.get("asset_hash"),
            "renderer_compatibility": body.get("renderer_compatibility"),
            "style_summary": _profile_style_summary(body),
            "principles_count": _list_count(body.get("principles")),
            "examples_count": _list_count(body.get("examples")),
            "body_sha256": digest,
            "size_bytes": body_size,
            "expires_at": _import_expires_at(),
            "warnings": warnings,
        }

    def _validated_import_body(self, content: Mapping[str, Any]) -> dict[str, Any]:
        """Shape-check one imported body without the manager's real approval."""

        try:
            profile_id = require_profile_id(content.get("profile_id"))
            placeholder = EventRef.from_value(
                {
                    "ref_type": "event",
                    "namespace": self.config.authority.namespace,
                    "event_id": _IMPORT_PLACEHOLDER_EVENT_ID,
                }
            ).to_dict()
            body = dict(content)
            body["review_ref"] = placeholder
            validated = validate_profile_body(
                body,
                profile_id=profile_id,
                review_ref=EventRef.from_value(placeholder),
                schema_version=PROFILE_SCHEMA_VERSION,
            )
        except InvalidInput as exc:
            raise PersonaControlError(
                "IMPORT_INVALID", f"the profile body is not valid ({exc})"
            ) from exc
        validated.pop("review_ref", None)
        return validated

    def _persona_import_dir(self) -> Path:
        root = Path(self.config.instance_root)
        candidate = root / PERSONA_IMPORT_DIRNAME
        if candidate.is_symlink():
            raise PersonaControlError(
                "IMPORT_UNAVAILABLE", "the persona-import directory is a link"
            )
        if not candidate.exists() or not candidate.is_dir():
            raise PersonaControlError(
                "IMPORT_UNAVAILABLE",
                "the instance has no persona-import directory",
            )
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise PersonaControlError(
                "IMPORT_UNAVAILABLE",
                "the persona-import directory escapes the instance root",
            ) from exc
        return resolved

    def _prune_import_drafts(self) -> None:
        now = time.monotonic()
        for key in [
            key
            for key, draft in self._import_drafts.items()
            if draft["expires_monotonic"] <= now
        ]:
            self._import_drafts.pop(key, None)

    def _ingest_profile_approval(
        self, profile_id: str, digest: str, size_bytes: int
    ) -> str:
        """Record the manager's real approval as a trusted ``user_report`` event.

        The event names the exact body digest, so the approval cannot be reused
        for different content.  ``source_event_id`` is deterministic, which
        makes a retry an idempotent duplicate instead of a second approval.
        """

        namespace = self.config.authority.namespace
        source_event_id = "persona-review-" + str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{namespace}\x1f{profile_id}\x1f{digest}\x1f"
                f"{self.config.authority.access_ref}",
            )
        )
        receipt = self.store.ingest_event(
            namespace=namespace,
            source_kind="user_report",
            source_instance=self.config.authority.host_instance,
            source_event_id=source_event_id,
            event_type=REVIEW_APPROVE_EVENT_TYPE,
            observed_at=utc_now_rfc3339(),
            payload={
                "decision": "approve",
                "profile_id": profile_id,
                "body_sha256": digest,
                "body_size": int(size_bytes),
                "schema_version": PROFILE_SCHEMA_VERSION,
                "surface": "desktop.settings.persona",
            },
            privacy_class="private",
        )
        return receipt.event_id

    def profile_publish_reviewed(
        self, *, import_id: str, profile_id: str | None, confirm: Any
    ) -> dict[str, Any]:
        """Publish one imported body after an explicit manager confirmation.

        The host writes the ``persona.review.approve`` event itself (the same
        precedent as ``cognition_decide``), then calls the existing
        ``publish_profile``; the active pointer is never touched.  Publishing
        the identical body again returns the existing version instead of
        appending a duplicate.
        """

        if not self.persona_capabilities()["publish"]:
            raise PersonaControlError(
                "PERSONA_CONTROL_DISABLED",
                "this host has not enabled persona publishing",
            )
        if confirm is not True:
            raise PersonaControlError(
                "CONFIRMATION_REQUIRED", "publishing requires explicit confirmation"
            )
        self._prune_import_drafts()
        draft = self._import_drafts.get(import_id)
        if draft is None:
            raise PersonaControlError(
                "NOT_FOUND", "the imported draft does not exist or has expired"
            )
        if profile_id is not None and profile_id != draft["profile_id"]:
            raise PersonaControlError(
                "INVALID_PARAMS", "profile_id does not match the imported body"
            )
        namespace = self.config.authority.namespace
        published_id = str(draft["profile_id"])
        body = dict(draft["body"])
        digest = str(draft["digest"])
        record_id = profile_record_id(namespace, published_id)
        current = self._current_profile_body(namespace, record_id)
        if current is not None and profile_body_digest(current) == digest:
            self._import_drafts.pop(import_id, None)
            review_ref = (
                current.get("review_ref")
                if isinstance(current.get("review_ref"), Mapping)
                else {}
            )
            review_event_id = review_ref.get("event_id")
            # UI-F02 review: an old approval may predate the digest binding.
            # Only an event that actually names this body digest is a safe
            # reference to hand back; a legacy provenance-only event is not,
            # because the legacy ``profile.publish`` path would accept it for a
            # different body.
            bound = (
                isinstance(review_event_id, str)
                and self._review_event_binds_body(
                    namespace, review_event_id, digest
                )
            )
            return {
                "profile_id": published_id,
                "record_id": record_id,
                "record_version": self._current_profile_version(
                    namespace, record_id
                ),
                "review_event_id": review_event_id if bound else None,
                "review_event_bound": bound,
                "review_body_sha256": digest,
                "active_changed": False,
                "reused": True,
            }
        review_event_id = self._ingest_profile_approval(
            published_id, digest, int(draft["size_bytes"])
        )
        review_reference = {
            "ref_type": "event",
            "namespace": namespace,
            "event_id": review_event_id,
        }
        body["review_ref"] = review_reference
        version = self.profiles.publish_profile(
            namespace=namespace,
            profile_id=published_id,
            body=body,
            review_ref=review_reference,
            authorization_ref=self.config.authority.access_ref,
        )
        self._import_drafts.pop(import_id, None)
        return {
            "profile_id": published_id,
            "record_id": version.record_id,
            "record_version": int(version.record_version),
            "review_event_id": review_event_id,
            "review_event_bound": True,
            "review_body_sha256": digest,
            "active_changed": False,
            "reused": False,
        }

    def _review_event_binds_body(
        self, namespace: str, event_id: str, digest: str
    ) -> bool:
        """Whether one existing approval event names exactly this body digest."""

        try:
            event = self.store.get_event(namespace, event_id)
        except (EventNotFound, RecordNotFound, Redacted):
            return False
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        return payload.get("body_sha256") == digest

    def _current_profile_body(
        self, namespace: str, record_id: str
    ) -> dict[str, Any] | None:
        pointer = self.store.record_pointer(namespace, record_id)
        if pointer is None or pointer.kind != PERSONA_PROFILE_KIND:
            return None
        try:
            version = self.store.get_version(
                namespace, record_id, pointer.current_version
            )
        except (RecordNotFound, Redacted):
            return None
        return dict(version.body)

    def _current_profile_version(self, namespace: str, record_id: str) -> int:
        pointer = self.store.record_pointer(namespace, record_id)
        return 0 if pointer is None else int(pointer.current_version)

    # ------------------------------------------------------- preferences

    def preference_list(self, *, conversation_id: str | None = None) -> dict[str, Any]:
        """Bounded confirmed/tentative/inactive view for the settings page."""

        namespace = self.config.authority.namespace
        conversation: dict[str, Any] | None = None
        if conversation_id is not None:
            row = self.store.get_conversation(
                namespace=namespace,
                host_instance=self.config.authority.host_instance,
                conversation_id=conversation_id,
            )
            conversation = {
                "conversation_id": row.conversation_id,
                "title": row.title,
            }
        groups = list_preference_groups(
            self.store,
            self.expression_preferences,
            namespace=namespace,
            conversation_id=(
                conversation["conversation_id"] if conversation is not None else None
            ),
            privacy_classes=self.config.authority.privacy_classes,
        )
        cognition_enabled = bool(self.config.cognition.enabled)
        propose = bool(cognition_enabled and self.config.cognition.propose)
        return {
            "confirmed": list(groups.confirmed),
            "tentative": list(groups.tentative),
            "inactive": list(groups.inactive),
            "conversation": conversation,
            "expression_recording": {
                # S2 retired the tentative write path: no new
                # ``kind=expression_preference`` record can be created, so
                # reporting it as enabled would tell the UI that recording
                # works when it does not.
                "enabled": False,
                "reason": "EXPRESSION_RECORDING_RETIRED",
            },
            "confirmed_recording": {
                "enabled": propose,
                "reason": (
                    None
                    if propose
                    else (
                        "COGNITION_DISABLED"
                        if not cognition_enabled
                        else "COGNITION_PROPOSE_DISABLED"
                    )
                ),
            },
            "truncated": groups.truncated,
        }

    def preference_revoke(
        self,
        *,
        conversation_id: str,
        dimension: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Append one ``revoked`` version of a conversation-scoped preference.

        The existing domain service owns the write; the host only checks that
        the target is really still tentative.  No approval is required for a
        low-impact preference and no other record is touched.
        """

        namespace = self.config.authority.namespace
        self.store.get_conversation(
            namespace=namespace,
            host_instance=self.config.authority.host_instance,
            conversation_id=conversation_id,
        )
        current = self.expression_preferences.current(
            namespace=namespace,
            conversation_id=conversation_id,
            dimension=dimension,
        )
        if current is None:
            raise PersonaControlError(
                "NOT_FOUND",
                "there is no expression preference for this dimension in this conversation",
            )
        status = str(current.status)
        if status != "tentative":
            raise PersonaControlError(
                "PREFERENCE_NOT_ACTIVE",
                f"the preference is {status}, not tentative",
            )
        text = (reason or "用户在设置中撤销该表达偏好").strip()
        version = self.expression_preferences.revoke(
            namespace=namespace,
            conversation_id=conversation_id,
            dimension=dimension,
            reason=text,
            recorded_at=utc_now_rfc3339(),
        )
        if version is None:  # pragma: no cover - current() proved it exists
            raise PersonaControlError("NOT_FOUND", "the preference disappeared")
        return {
            "revoked": True,
            "previous_status": status,
            "record": {
                "record_id": version.record_id,
                "record_version": int(version.record_version),
                "recorded_at": version.recorded_at,
            },
        }

    # ---------------------------------------------------------------- review

    def review_capabilities(self) -> dict[str, bool]:
        """The host-derived review booleans ``app.info`` may disclose.

        They are derived from the private host configuration and the startup
        readiness; no token, grant, access reference or permission list leaves
        the host.  ``can_*`` flags from a candidate detail are *not* included:
        they never replace these host permissions.
        """

        authority = self.config.authority
        read = bool(
            self.ready
            and {"work.scan", "experience.read"}
            <= authority.review_permissions
            and REVIEW_PURPOSE in authority.purposes
        )
        return {
            "read": read,
            "generate": bool(read and "review.generate" in authority.review_permissions),
            "approve": bool(read and "review.approve" in authority.review_permissions),
            "reject": bool(read and "review.reject" in authority.review_permissions),
            "result": bool(read and "result.read" in authority.review_permissions),
        }

    def review_workspace_reason(self) -> str | None:
        """One public label explaining why Review is unavailable, if it is."""

        if not self.ready:
            return self.reason_code or "HOST_NOT_READY"
        authority = self.config.authority
        if not authority.review_permissions:
            return "REVIEW_NOT_CONFIGURED"
        if not {"work.scan", "experience.read"} <= authority.review_permissions:
            return "REVIEW_READ_PERMISSION_MISSING"
        if REVIEW_PURPOSE not in authority.purposes:
            return "REVIEW_PURPOSE_MISSING"
        return None

    def _last_candidate_status(self, work_id: str) -> str | None:
        """Latest visible candidate state for the Work-list projection.

        C07 intentionally leaves a collaboration Work in ``pending_work``
        after commit.  The Review list must therefore show the Candidate
        lifecycle, not keep claiming that an already submitted Work is merely
        "待处理".
        """

        rows = self.review_facade.candidates(
            self._review_access, work_id=work_id
        )
        if not rows:
            return None
        return rows[-1].status

    def review_work_list(self) -> dict[str, Any]:
        """Reviewable work plus the read-only history a person could review.

        Review exists for candidates.  A *derivation chore* (``event.derive*``)
        is internal bookkeeping: it can never own a candidate, ``review.generate``
        refuses it, and every ingested event creates one, so listing them made
        Review look full while it held nothing.  They are counted here and left
        out of the list; managed work (which can generate a candidate) and any
        non-chore legacy work still appear.
        """

        managed = self.review_facade.managed_work(self._review_access)
        legacy = self.review_facade.legacy_work(self._review_access)
        reviewable = [item for item in legacy if item.kind not in DERIVATION_WORK_KINDS]
        return {
            "managed": [
                {
                    "work_id": item.work_id,
                    "event_id": item.event_id,
                    "generation": item.generation,
                    "blocked": bool(item.blocked),
                    # The reference value is an authorization reference.  The
                    # host exposes only its existence; C12a §8.6 forbids
                    # showing the reference itself.
                    "rerun_registered": item.rerun_authorization_ref is not None,
                    "last_candidate_status": self._last_candidate_status(item.work_id),
                }
                for item in managed
            ],
            "legacy": [
                {
                    "work_id": item.work_id,
                    "event_id": item.event_id,
                    "kind": item.kind,
                    "status": item.status,
                    "created_at": item.created_at,
                }
                for item in reviewable
            ],
            # Visible, but explicitly not review work: the surface must not
            # count these as pending candidates.
            "derivation_backlog": len(legacy) - len(reviewable),
        }

    def review_candidates(
        self, *, work_id: str, generation: int | None = None
    ) -> dict[str, Any]:
        rows = self.review_facade.candidates(
            self._review_access, work_id=work_id, generation=generation
        )
        return {"candidates": [_candidate_summary(row) for row in rows]}

    def review_candidate_detail(
        self, *, candidate_id: str, artifact_version: int
    ) -> dict[str, Any]:
        detail = self.review_facade.candidate_detail(
            self._review_access,
            candidate_id=candidate_id,
            artifact_version=artifact_version,
        )
        return _candidate_detail(detail)

    def review_generate(self, *, work_id: str, event_id: str) -> dict[str, Any]:
        """Start a review only for a managed Work that has no live candidate.

        Legacy Work is read-only in C12c: it has no collaboration control and
        the host must not turn it into a candidate.  A managed Work with a
        pending or committed candidate is not started twice; a rejected Work
        remains gated by C05d/C07, which alone decides whether a registered
        rerun authorization is still valid.
        """

        managed = {
            item.work_id: item
            for item in self.review_facade.managed_work(self._review_access)
        }
        item = managed.get(work_id)
        if item is None:
            legacy = {
                legacy_item.work_id
                for legacy_item in self.review_facade.legacy_work(self._review_access)
            }
            if work_id in legacy:
                raise ReviewRequestError(
                    "LEGACY_WORK_READ_ONLY",
                    "旧工作项没有审查控制行；本窗口不把它转成候选",
                )
            raise ReviewRequestError(
                "WORK_NOT_PENDING",
                "该工作项已不是待处理的审查工作",
            )
        if item.event_id != event_id:
            raise ReviewRequestError(
                "INVALID_PARAMS",
                "事件不属于该工作项",
            )
        rows = self.review_facade.candidates(
            self._review_access, work_id=work_id, generation=item.generation
        )
        if any(row.status == "pending" for row in rows):
            raise ReviewRequestError(
                "CANDIDATE_EXISTS",
                "该工作项已有待决定的候选；请继续当前审查",
            )
        if any(row.status == "committed" for row in rows):
            raise ReviewRequestError(
                "WORK_ALREADY_RESOLVED",
                "该工作项已有已提交的候选；不能重复发起审查",
            )
        view = self.review_facade.review(
            self._review_access,
            work_id=work_id,
            trigger_event_id=event_id,
            activation_kind="manual_review",
        )
        return _review_view(view)

    def review_resolve(
        self,
        *,
        candidate_id: str,
        artifact_version: int,
        decision: str,
    ) -> dict[str, Any]:
        """Re-prove the exact candidate/version/Work/current binding, then C07.

        This is a pre-check, not a second authority: C07 still re-checks the
        candidate, generation, Approval, Context, deletion barrier and receipt
        inside its own transaction.  The host never supplies an Approval, a
        grant, a candidate body, a model or a provider.
        """

        if decision not in ("approve", "reject"):
            raise ReviewRequestError(
                "INVALID_PARAMS", "decision must be approve or reject"
            )
        try:
            detail = self.review_facade.candidate_detail(
                self._review_access,
                candidate_id=candidate_id,
                artifact_version=artifact_version,
            )
        except RecordNotFound:
            raise ReviewRequestError(
                "CANDIDATE_VERSION_MISMATCH",
                "候选版本已变化或不存在；请刷新候选",
            ) from None
        summary = detail.candidate
        ref = summary.candidate
        if (
            ref.candidate_id != candidate_id
            or ref.artifact_version != artifact_version
        ):
            raise ReviewRequestError(
                "CANDIDATE_VERSION_MISMATCH",
                "候选身份与所选版本不一致；请刷新候选",
            )
        if detail.redacted:
            raise ReviewRequestError(
                "CANDIDATE_STALE",
                "候选内容不可读；没有提交任何决定",
            )
        if ref.artifact_version != 1:
            raise ReviewRequestError(
                "UNSUPPORTED_VERSION",
                "此候选版本不在 C12c 支持范围；不能批准或拒绝",
            )
        if summary.status == "committed":
            # A repeated click (or a retried request) finds the committed
            # candidate.  C07 is the authority that returns its stored receipt;
            # this pre-check only proves the exact Work generation again.
            self._require_review_work(ref)
            view = self.review_facade.resolve(
                self._review_access,
                candidate_id=candidate_id,
                decision=decision,
                decision_id=f"decision:{uuid.uuid4()}",
            )
            return _review_view(view)
        if summary.status != "pending":
            raise ReviewRequestError(
                "CANDIDATE_STALE",
                "候选已不是待决定状态；请刷新候选",
            )
        if detail.eligibility_reason == "stale_generation":
            raise ReviewRequestError(
                "CANDIDATE_STALE",
                "工作轮次已变化；请刷新候选",
            )
        if detail.eligibility_reason == "unsupported_version":
            raise ReviewRequestError(
                "UNSUPPORTED_VERSION",
                "此候选版本不在 C12c 支持范围；不能批准或拒绝",
            )
        if decision == "approve":
            if not detail.can_approve:
                reason = detail.eligibility_reason or "not_approvable"
                if reason in (
                    "target_moved",
                    "current_redacted",
                    "current_missing",
                    "current_terminal",
                ):
                    raise ReviewRequestError(
                        "REVISION_CONFLICT",
                        "当前目标版本已变化或不可用；请刷新权威状态",
                    )
                raise ReviewRequestError(
                    "CANDIDATE_STALE",
                    "候选当前不可批准；请刷新候选",
                )
            if detail.current_state in ("redacted", "missing"):
                raise ReviewRequestError(
                    "REVISION_CONFLICT",
                    "当前目标版本不可读或不存在；请刷新权威状态",
                )
        else:
            if not detail.can_reject:
                raise ReviewRequestError(
                    "CANDIDATE_STALE",
                    "候选当前不可拒绝；请刷新候选",
                )

        self._require_review_work(ref)

        view = self.review_facade.resolve(
            self._review_access,
            candidate_id=candidate_id,
            decision=decision,
            decision_id=f"decision:{uuid.uuid4()}",
        )
        return _review_view(view)

    def _require_review_work(self, ref: Any) -> None:
        managed = {
            item.work_id: item
            for item in self.review_facade.managed_work(self._review_access)
        }
        work = managed.get(ref.work_id)
        if work is None:
            raise ReviewRequestError(
                "WORK_NOT_PENDING",
                "该工作项已不是待处理状态；没有提交决定",
            )
        if work.generation != ref.work_generation:
            raise ReviewRequestError(
                "CANDIDATE_STALE",
                "工作轮次与候选不一致；请刷新候选",
            )

    # ------------------------------------------------------- RG05 cognition

    def cognition_capabilities(self) -> dict[str, Any]:
        authority = self.config.authority
        pending = bool(
            self.ready
            and "experience.read" in authority.review_permissions
            and REVIEW_PURPOSE in authority.purposes
            and (
                "review.approve" in authority.review_permissions
                or "review.reject" in authority.review_permissions
            )
        )
        return {
            "enabled": self.config.cognition.enabled,
            "configured": self.config.cognition.enabled,
            "propose": bool(self.config.cognition.propose),
            "pending_review": pending,
            "auto_authorization": getattr(self, "_cognition_auto_id", None)
            is not None,
            "working_set_limit": self.config.cognition.working_set_limit,
            "pending_limit": self.config.cognition.pending_limit,
        }

    def _pending_cognition_changes(self) -> int:
        """RG07: count the cognition changes still awaiting review.

        The expression stage may only learn the *count* (never a claim, never a
        candidate body).  A candidate whose lifecycle cannot be read counts as
        pending, so the host errs towards "do not say it was remembered".
        """

        config = self.config
        if not config.cognition.enabled:
            return 0
        namespace = config.authority.namespace
        rows = self.store.pending_change_candidates(namespace, limit=20)
        if not rows:
            return 0
        governance = GovernanceStore(self.store)
        pending = 0
        for row in rows:
            try:
                lifecycle = governance.lifecycle(
                    namespace, str(row["artifact_id"])
                )
            except Exception:  # noqa: BLE001 - a read failure is not permission
                pending += 1
                continue
            if lifecycle == "pending":
                pending += 1
        return pending

    def expression_capabilities(self) -> dict[str, Any]:
        """RG07: the read-only truth about the configured expression mode."""

        from quintessence_pcs.conversation.sdrpe import AUTO_IMPLEMENTED

        plan = SdrpePlan.resolve(
            self.config.sdrpe.mode,
            protocol_version=self.config.model.model_protocol,
        )
        return {
            "mode": plan.requested_mode,
            "effective_mode": plan.effective_mode,
            "policy_version": plan.policy_version,
            "auto_available": AUTO_IMPLEMENTED,
            "resolved_reason": plan.resolved_reason,
        }

    def apply_generation_settings(self, config: HostConfig) -> None:
        """Host-owned reload at the single worker boundary, without startup recovery.

        Only model/manifest, SDRPE and the web surface may change. All authority,
        instance, persona contracts, tool enablement and budgets must remain
        identical.  A web change rebuilds the tool runtime so the very next turn
        sees the new surface; a live turn keeps the runtime it started with.
        """
        from dataclasses import replace

        previous = self.config
        if replace(config, model=previous.model, destination=previous.destination,
                   sdrpe=previous.sdrpe, web=previous.web,
                   memory=previous.memory,
                   maintenance=previous.maintenance) != previous:
            raise RuntimeStartupError("SETTINGS_INVALID", "only generation settings may change")
        if config.sdrpe.mode not in {"off", "on"}:
            raise RuntimeStartupError("SETTINGS_INVALID", "auto is unavailable")
        model_changed = config.model != previous.model
        web_changed = config.web != previous.web
        memory_changed = config.memory != previous.memory
        maintenance_changed = config.maintenance != previous.maintenance
        if not model_changed and not web_changed and not memory_changed and not maintenance_changed:
            self.config = config
            return
        if model_changed:
            model = _build_model(config, self.store)
            review_model = _build_model(config, self.store, review=True)
            maintenance_model = _build_model(
                config,
                self.store,
                review=True,
                transport_timeout_cap=(
                    config.maintenance.model_timeout_seconds
                    if config.maintenance is not None
                    and config.maintenance.enabled
                    else 7.0
                ),
                max_retries=0,
            )
            # Only existing provisioning policy can authorize a changed manifest.
            if config.managed and config.provision:
                self._provision_managed(config)
            self.model = model
            self.review_model = review_model
            self.maintenance_model = maintenance_model
            self.facade = ConsultFacade(self.store, context_service=self.contexts,
                                        model=model, query_service=self.queries)
            self.review_facade = ReviewFacade(self.store, context_service=self.contexts,
                                             model=review_model)
        if maintenance_changed and not model_changed:
            # The switch/timeout are read from ``self.config`` by the slice, but
            # the transport cap was fixed at construction; rebuild only the
            # maintenance adapter so the next run sees the new bound.
            self.maintenance_model = _build_model(
                config,
                self.store,
                review=True,
                transport_timeout_cap=(
                    config.maintenance.model_timeout_seconds
                    if config.maintenance is not None and config.maintenance.enabled
                    else 7.0
                ),
                max_retries=0,
            )
        if web_changed:
            self._tool_runtime = self._build_tool_runtime(config)
        if memory_changed:
            # Projection parses "昨天" with the configured zone; rebuilding at
            # the same worker boundary makes a timezone change apply next turn.
            self.experience_bridge = self._build_experience_bridge(config)
        self.conversations = ConversationOrchestrator(
            self.store, context_service=self.contexts, model=self.model,
            max_history_turns=config.history_max_turns, max_history_bytes=config.history_max_bytes,
            protocol_version=config.model.model_protocol, limits=_turn_limits(config),
            tool_runtime_factory=self._tool_runtime,
            cognition_material_factory=self._build_cognition_material_factory(config),
            expression_material_factory=self._build_expression_material_factory(config),
            memory_recall_factory=self._build_memory_recall_factory(config),
            cognition_status_factory=self._pending_cognition_changes,
            event_sink=self._event_sink, cancel_requested=self._cancel_requested,
            host_is_lab=config.is_lab,
        )
        self.config = config
        self.ready, self.reason_code, self.reason_detail = self._readiness()

    def cognition_candidates(self, *, limit: int = 20) -> dict[str, Any]:
        """List visible pending cognition candidates; never a body or a prompt."""

        config = self.config
        namespace = config.authority.namespace
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ReviewRequestError("INVALID_PARAMS", "limit must be 1..20")
        rows = self.store.pending_change_candidates(namespace, limit=limit * 2)
        governance = GovernanceStore(self.store)
        experience = self._review_experience_access()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            candidate_id = str(row["artifact_id"])
            version = int(row["artifact_version"])
            if governance.lifecycle(namespace, candidate_id) != "pending":
                continue
            ref = {
                "ref_type": "collab_artifact",
                "namespace": namespace,
                "artifact_id": candidate_id,
                "artifact_version": version,
            }
            view = self.store.read_experience(
                experience, ref=ref, purpose="review"
            )
            if getattr(view, "readability", None) != "available" or not getattr(
                view, "body", None
            ):
                continue
            proposal = _strict_json(view.body)
            if not isinstance(proposal, dict):
                continue
            claim, claim_type, target = _proposal_claim(proposal)
            if claim is None:
                continue
            decision = governance.latest_decision(namespace, candidate_id, version)
            state = "PROPOSED" if decision is None else str(decision.decision).upper()
            turn_id = None
            context_id = row.get("context_id")
            if isinstance(context_id, str):
                turn_id = self.store.turn_id_for_context(namespace, context_id)
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "artifact_version": version,
                    "claim": claim,
                    "claim_type": claim_type,
                    "target": target,
                    "created_at": str(row["created_at"]),
                    "state": state,
                    "requires_human": bool(decision.requires_human)
                    if decision
                    else False,
                    "impact": decision.impact if decision else None,
                    "reason_code": decision.reason_code if decision else None,
                    "turn_id": turn_id,
                }
            )
            if len(candidates) >= limit:
                break
        return {
            "candidates": candidates,
            "pending_review": self.cognition_capabilities()["pending_review"],
        }

    def cognition_decide(
        self,
        *,
        candidate_id: str,
        artifact_version: int,
        decision: str,
    ) -> dict[str, Any]:
        """Approve or reject one *awaiting-human* workless chat candidate.

        This is the only human-review path a chat candidate has: it owns no
        ``collab_work_control`` row, so ``review.*`` cannot resolve it.  The
        envelope stays host-built (a real ``user_report`` statement records the
        click, the approval scope comes from the stored proposal).

        Three things are deliberately narrow:

        * **Source.** Only a candidate proposed by a consult interaction on this
          host, with the cognition-change policy, in its own Context is
          accepted.  A managed Work candidate, another host's candidate or any
          other lineage is refused as an unsupported path -- never disguised as
          a stale one.
        * **Basis.** The commit may only name events the candidate itself
          caused: that turn's memory-bridge projections and this candidate's own
          earlier review statements.  A later turn's traffic still makes the
          basis stale, exactly as before.
        * **Idempotence.** A repeated click returns the state it already has;
          crossing from committed to rejected (or back) is refused as decided.
        """

        if decision not in ("approve", "reject"):
            raise ReviewRequestError(
                "INVALID_PARAMS", "decision must be approve or reject"
            )
        if type(artifact_version) is not int or artifact_version < 1:
            raise ReviewRequestError(
                "INVALID_PARAMS", "artifact_version must be a positive integer"
            )
        config = self.config
        namespace = config.authority.namespace
        governance = GovernanceStore(self.store)
        lifecycle = governance.lifecycle(namespace, candidate_id)
        if lifecycle == "committed":
            if decision == "approve":
                return {
                    "state": "COMMITTED",
                    "candidate_id": candidate_id,
                    "artifact_version": artifact_version,
                    "reason_code": "already_committed",
                    "requires_human": False,
                    "effective": True,
                    "replayed": True,
                }
            raise ReviewRequestError(
                "CANDIDATE_ALREADY_DECIDED",
                "该候选已批准生效；不能再改成拒绝",
            )
        if lifecycle == "rejected":
            if decision == "reject":
                return {
                    "state": "REJECTED",
                    "candidate_id": candidate_id,
                    "artifact_version": artifact_version,
                    "reason_code": "already_rejected",
                    "requires_human": False,
                    "effective": False,
                    "replayed": True,
                }
            raise ReviewRequestError(
                "CANDIDATE_ALREADY_DECIDED",
                "该候选已被拒绝；不能再改成批准",
            )
        if lifecycle != "pending":
            raise ReviewRequestError(
                "CANDIDATE_STALE", "候选已不是待决定状态；请刷新候选"
            )
        try:
            artifact = self.store.load_artifact(
                namespace=namespace,
                artifact_id=candidate_id,
                artifact_version=artifact_version,
            )
        except RecordNotFound:
            raise ReviewRequestError(
                "CANDIDATE_VERSION_MISMATCH", "候选版本不存在或已变化；请刷新候选"
            ) from None
        except Redacted:
            raise ReviewRequestError(
                "CANDIDATE_STALE", "候选内容受删除屏障保护；没有提交任何决定"
            ) from None
        except Exception:  # noqa: BLE001 - hidden/removed candidate
            raise ReviewRequestError(
                "CANDIDATE_STALE", "候选不可读；没有提交任何决定"
            ) from None
        if artifact.kind != "candidate" or artifact.proposal is None:
            raise ReviewRequestError(
                "CANDIDATE_STALE", "候选不可读；没有提交任何决定"
            )
        self._require_workless_candidate_source(
            artifact=artifact, namespace=namespace
        )
        latest = governance.latest_decision(namespace, candidate_id, artifact_version)
        if latest is None or latest.decision not in ("awaiting_human", "escalated"):
            raise ReviewRequestError(
                "CANDIDATE_STALE", "该候选不在待人工状态；没有提交任何决定"
            )
        operations, claim_types, scopes = _approval_scope(artifact.proposal)
        now = utc_now_rfc3339()
        statement = self.store.ingest_event(
            namespace=namespace,
            source_kind="user_report",
            source_instance=config.authority.host_instance,
            source_event_id=f"rg05-review-{uuid.uuid4()}",
            event_type="UserStatement:review_decision",
            observed_at=now,
            payload={
                "decision": decision,
                "candidate_id": candidate_id,
                "artifact_version": artifact_version,
            },
            privacy_class="private",
        )
        action = ApprovalAction(
            approval_id=str(uuid.uuid4()),
            candidate_id=candidate_id,
            artifact_version=artifact_version,
            proposal_digest=str(artifact.proposal_digest),
            decision=decision,
            approval_scope={
                "operations": operations,
                "claim_types": claim_types,
                "scope": scopes,
            },
            statement_ref={
                "ref_type": "event",
                "namespace": namespace,
                "event_id": statement.event_id,
            },
            approved_at=now,
            expires_at=_expires_in_minutes(now, 10),
            access_ref=config.authority.access_ref,
        )
        basis = self.store.workless_candidate_basis_events(
            namespace,
            context_id=str(artifact.context_id),
            candidate_id=candidate_id,
        )
        appended = tuple(dict.fromkeys((statement.event_id, *basis)))
        try:
            result = CognitionChangeService(self.store).commit(
                self._human_change_access(),
                candidate_id=candidate_id,
                authority=action,
                decision_id=str(uuid.uuid4()),
                appended_events=appended,
            )
        except ContextStale as exc:
            raise ReviewRequestError(
                "CANDIDATE_STALE",
                "候选的提交依据已变化（后续来源或版本移动）；没有提交任何决定",
            ) from exc
        return {
            "state": result.state,
            "candidate_id": candidate_id,
            "artifact_version": artifact_version,
            "reason_code": result.reason_code,
            "requires_human": result.requires_human,
            "effective": result.state == "COMMITTED",
            "replayed": False,
        }

    def _require_workless_candidate_source(self, *, artifact: Any, namespace: str) -> None:
        """Prove the candidate comes from this host's own consult turn.

        The workless path exists precisely for candidates a chat turn proposed;
        it must not become a second door into managed review.  Each refusal
        names which binding failed, so the UI can say "this candidate has no
        such review path" instead of "candidate stale".
        """

        authority = self.config.authority
        if artifact.work_id is not None:
            raise ReviewRequestError(
                "REVIEW_PATH_UNSUPPORTED",
                "该候选属于受控工作项；请使用审查工作路径，而不是聊天候选路径",
            )
        interaction = self.store.interaction(
            namespace=namespace, interaction_id=str(artifact.interaction_id)
        )
        if interaction is None:
            raise ReviewRequestError(
                "CANDIDATE_SOURCE_MISMATCH",
                "候选所属的交互已不存在；没有提交任何决定",
            )
        if interaction.work_id is not None:
            raise ReviewRequestError(
                "REVIEW_PATH_UNSUPPORTED",
                "该候选的交互绑定了工作项；请使用审查工作路径",
            )
        if interaction.mode != "consult":
            raise ReviewRequestError(
                "REVIEW_PATH_UNSUPPORTED",
                f"该候选来自 {interaction.mode} 交互，聊天候选路径只处理 consult",
            )
        if interaction.policy_ref != COGNITION_CHANGE_POLICY_REF:
            raise ReviewRequestError(
                "REVIEW_PATH_UNSUPPORTED",
                "该候选不是认知变更交互产生的；聊天候选路径不适用",
            )
        if interaction.access_ref != authority.access_ref:
            raise ReviewRequestError(
                "CANDIDATE_SOURCE_MISMATCH",
                "该候选由另一个宿主访问引用创建；本宿主不能决定它",
            )
        if interaction.namespace != namespace:
            raise ReviewRequestError(
                "CANDIDATE_SOURCE_MISMATCH",
                "该候选不在本宿主的命名空间内",
            )
        if interaction.context_id != artifact.context_id:
            raise ReviewRequestError(
                "CANDIDATE_SOURCE_MISMATCH",
                "候选的 Context 不属于它所属的交互；来源无法闭合",
            )

    def _review_experience_access(self) -> ExperienceAccess:
        authority = self.config.authority
        return ExperienceAccess(
            namespace=authority.namespace,
            host_instance=authority.host_instance,
            access_ref=authority.access_ref,
            privacy_classes=authority.privacy_classes,
            purposes=frozenset({REVIEW_PURPOSE}),
        )

    def _human_change_access(self) -> ChangeAccess:
        authority = self.config.authority
        return ChangeAccess(
            namespace=authority.namespace,
            host_instance=authority.host_instance,
            operator=authority.operator,
            access_ref=authority.access_ref,
            permissions=frozenset(
                {"change.review", "change.commit", "change.result"}
            ),
            privacy_classes=authority.privacy_classes,
            purposes=frozenset({"cognition.change"}),
        )

    def review_result(self, *, interaction_id: str) -> dict[str, Any]:
        view = self.review_facade.result(
            self._review_access, interaction_id=interaction_id
        )
        return _review_view(view)

    def test_model_connection(self) -> dict[str, Any]:
        """Make one explicit, side-effect-free DeepSeek API probe.

        This does not create a Context, Attempt, Interaction or Artifact and
        does not write a Belief.  It is deliberately separate from the formal
        Consult/Review ModelPort path; the latter still performs every C09,
        context and ledger check for a real model call.
        """

        config = self.config
        if config.model.transport_kind != "http" or config.is_lab:
            raise ReviewRequestError(
                "MODEL_TEST_UNAVAILABLE",
                "当前宿主不是真实 HTTP 模型配置，不能测试真实连接",
            )
        manifest = config.model.manifest
        key = os.environ.get(manifest.api_key_env, "")
        if not key:
            raise ReviewRequestError(
                "MODEL_KEY_MISSING",
                "尚未保存 API Key；请先在设置中保存",
            )
        if key != key.strip() or not key.isascii() or any(
            ord(character) < 0x20
            or ord(character) == 0x7F
            or character.isspace()
            for character in key
        ):
            raise ReviewRequestError(
                "MODEL_KEY_INVALID",
                "保存的 API Key 不是有效的可打印 ASCII 文本",
            )
        payload = json.dumps(
            {
                "model": manifest.model,
                "messages": [{"role": "user", "content": "ping"}],
                "temperature": 0,
                "max_tokens": 1,
                "stream": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            manifest.endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": "quintessence-desktop/c13b-1 (connection-test)",
            },
            method="POST",
        )
        opener = _build_opener()
        started = time.monotonic()
        try:
            with opener.open(request, timeout=min(manifest.max_wall_seconds, 30)) as response:
                status = int(response.status)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(MAX_RESPONSE_BYTES + 1)
            finally:
                exc.close()
            text = _redact(_decode_utf8(body), key) or ""
            if exc.code in (401, 403):
                raise ReviewRequestError(
                    "MODEL_AUTH_FAILED",
                    "DeepSeek 拒绝了 API Key（鉴权失败）",
                ) from None
            if exc.code == 429:
                raise ReviewRequestError(
                    "MODEL_RATE_LIMITED",
                    "DeepSeek 限流或配额不足；请稍后再试",
                ) from None
            if 500 <= exc.code < 600:
                raise ReviewRequestError(
                    "MODEL_PROVIDER_ERROR",
                    "DeepSeek 服务端返回错误；请稍后再试",
                ) from None
            raise ReviewRequestError(
                "MODEL_REQUEST_REJECTED",
                f"DeepSeek 拒绝了连接测试请求（HTTP {exc.code}）",
            ) from None
        except (socket.timeout, TimeoutError):
            raise ReviewRequestError(
                "MODEL_TIMEOUT",
                "DeepSeek 连接测试超时",
            ) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise ReviewRequestError(
                    "MODEL_TIMEOUT",
                    "DeepSeek 连接测试超时",
                ) from None
            raise ReviewRequestError(
                "MODEL_NETWORK",
                "无法连接 DeepSeek；请检查网络",
            ) from None
        except (OSError, ValueError):
            raise ReviewRequestError(
                "MODEL_NETWORK",
                "无法连接 DeepSeek；请检查网络",
            ) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ReviewRequestError(
                "MODEL_INVALID_RESPONSE",
                "DeepSeek 返回了过大的响应",
            )
        if status != 200:
            raise ReviewRequestError(
                "MODEL_PROVIDER_ERROR",
                f"DeepSeek 返回了 HTTP {status}",
            )
        text = _decode_utf8(raw)
        if text is None:
            raise ReviewRequestError(
                "MODEL_INVALID_RESPONSE",
                "DeepSeek 响应不是有效 UTF-8",
            )
        if key in text:
            raise ReviewRequestError(
                "MODEL_INVALID_RESPONSE",
                "DeepSeek 响应回显了凭据；已拒绝",
            )
        try:
            document = json.loads(text)
        except ValueError:
            raise ReviewRequestError(
                "MODEL_INVALID_RESPONSE",
                "DeepSeek 响应不是有效 JSON",
            ) from None
        choices = document.get("choices") if isinstance(document, dict) else None
        if not isinstance(choices, list) or not choices:
            raise ReviewRequestError(
                "MODEL_INVALID_RESPONSE",
                "DeepSeek 响应缺少 chat completion choices",
            )
        return {
            "provider": manifest.provider,
            "model": manifest.model,
            "base_url": manifest.base_url,
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "connected": True,
        }

    # -------------------------------------------------------------- readiness

    def _readiness(self) -> tuple[bool, str | None, str | None]:
        config = self.config
        namespace = config.authority.namespace
        if config.model.transport_kind == "http":
            key = os.environ.get(config.model.manifest.api_key_env, "")
            if not key:
                return (
                    False,
                    "MODEL_KEY_MISSING",
                    "the host has not provided the configured model credential",
                )
        try:
            active = self.profiles.get_active_profile(namespace)
        except (Redacted, CorruptStore):
            return (
                False,
                "PROFILE_CORRUPT",
                "the active host profile is not verifiable",
            )
        except Exception:  # noqa: BLE001 - a broken profile read is not ready
            return (
                False,
                "PROFILE_CORRUPT",
                "the active host profile could not be read",
            )
        if active is None or active.ref is None:
            return (
                False,
                "PROFILE_MISSING",
                "the store has no active host profile",
            )
        authorization = self.store.active_model_authorization(
            namespace=namespace,
            provider=config.model.provider,
            model=config.model.model,
            manifest_hash=config.model.manifest.manifest_hash,
        )
        if authorization is None:
            return (
                False,
                "MODEL_AUTHORIZATION_MISSING",
                "no active host authorization matches the configured destination",
            )
        preflight = getattr(self.model, "preflight", None)
        if callable(preflight):
            try:
                preflight(namespace=namespace)
            except Exception as exc:  # noqa: BLE001 - preflight refusal is a state
                code = getattr(exc, "code", None)
                return (
                    False,
                    str(code) if isinstance(code, str) else type(exc).__name__,
                    "the configured model cannot start a call",
                )
        return True, None, None

    # -------------------------------------------------------------- lifecycle

    def close(self) -> None:
        try:
            if self.store is not None:
                self.store.close()
        finally:
            self._remove_synthetic_key()

    def _remove_synthetic_key(self) -> None:
        if self._lab_key_env is not None:
            os.environ.pop(self._lab_key_env, None)
            self._lab_key_env = None


def _web_tool_config(config: HostConfig) -> WebToolConfig | None:
    """RG08: build the host-owned web surface, or ``None`` when it is off.

    The credential is read from the configured environment variable and lives
    only inside the client.  It is never logged, persisted or echoed into a tool
    result.  Both the per-request timeout and the whole-call deadline are the
    host's ``web.timeout_seconds`` (validated to be inside ``tool_wall_seconds``)
    so retries cannot outlive the per-tool wall budget.
    """

    spec = config.web
    if not spec.enabled:
        return None
    key = os.environ.get(spec.api_key_env, "").strip()
    client = None
    if key:
        timeout = float(spec.timeout_seconds)
        client = DeepSeekWebClient(
            api_key=key, timeout=timeout, total_timeout=timeout
        )
    return WebToolConfig(
        client=client,
        policy=WebFetchPolicy(allowed_hosts=spec.allowed_hosts),
        search=spec.search,
        fetch=spec.fetch,
        capture_default=spec.capture_default,
    )


def _turn_limits(config: HostConfig) -> TurnLimits:
    """The host-owned per-turn budgets (validated by the config parser)."""

    return TurnLimits(
        max_model_requests=config.max_model_requests_per_turn,
        max_tool_calls=config.max_tool_calls_per_turn,
        turn_wall_seconds=float(config.turn_wall_seconds),
        tool_wall_seconds=float(config.tool_wall_seconds),
        commentary_max_per_turn=config.commentary_max_per_turn,
        commentary_max_chars=config.commentary_max_chars,
        tool_result_excerpt_bytes=config.tool_result_excerpt_bytes,
        tool_results_total_bytes=config.tool_results_total_bytes,
        tools_enabled=config.tools_enabled,
    )


def _turn_strategy(
    mode: str,
    protocol_version: str | None = None,
    limits: TurnLimits | None = None,
    persona: Mapping[str, Any] | None = None,
    sdrpe: Mapping[str, Any] | None = None,
    expression_plan: Mapping[str, Any] | None = None,
    expression_targets: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The host-owned per-turn policy snapshot (never supplied by the UI)."""

    from quintessence_pcs.runtime.collab_protocol import (
        COLLAB_PROTOCOL_VERSION,
        COLLAB_V2_PROTOCOL_VERSION,
        protocol_policy_version,
        protocol_renderer_version,
    )

    resolved = limits or TurnLimits()
    protocol = protocol_version or COLLAB_PROTOCOL_VERSION
    tools = protocol == COLLAB_V2_PROTOCOL_VERSION and resolved.tools_enabled
    strategy: dict[str, Any] = {
        "protocol_version": protocol,
        "renderer_version": protocol_renderer_version(protocol),
        "model_policy_version": protocol_policy_version(protocol),
        "mode": mode,
        "commentary": bool(
            tools and mode == "chat" and resolved.commentary_max_per_turn > 0
        ),
        "tools": tools,
        "sdrpe": dict(sdrpe) if sdrpe is not None else SdrpePlan.resolve(
            "off", protocol_version=protocol
        ).to_strategy(),
        "max_generations": 2,
        "max_model_requests": resolved.max_model_requests,
        "max_tool_calls": resolved.max_tool_calls,
        "turn_wall_seconds": resolved.turn_wall_seconds,
        # RG06: t2 = the pin also carries the speaker and the expression-example
        # selection made at T0.  t1 (profile only) is kept for turns created
        # without a persona snapshot.
        "profile_pin": "t2" if persona is not None else "t1",
    }
    if persona is not None:
        strategy["persona"] = dict(persona)
    if expression_plan is not None:
        strategy["expression_plan"] = dict(expression_plan)
    if expression_targets:
        strategy["expression_targets"] = [dict(item) for item in expression_targets]
    return strategy


def _ensure_synthetic_key(config: HostConfig) -> str | None:
    """Give the Lab scripted transport a process-only placeholder credential.

    A production configuration must supply the real credential through the
    environment named by its manifest; this helper never touches that profile.
    """

    if not config.is_lab or config.model.transport_kind != "scripted":
        return None
    name = config.model.manifest.api_key_env
    if os.environ.get(name):
        return None
    os.environ[name] = LAB_KEY_PREFIX + secrets.token_hex(24)
    return name


def _build_model(
    config: HostConfig,
    store: CognitionStore,
    *,
    review: bool = False,
    transport_timeout_cap: float | None = None,
    max_retries: int | None = None,
) -> Any:
    """Build the consult model or the legacy review model.

    Consult uses the configured ``collab.v1`` destination and C09 policy.
    Review keeps the C07 ModelPort shape: the same provider/model/manifest and
    model authorization, but the legacy ``pcs2-008`` request/renderer the
    review generator has always used.  No second destination or provider is
    invented, and the model authorization + pre-send Context gate stay in
    ``DirectHttpModel``.

    ``transport_timeout_cap`` is used only by the bounded maintenance model:
    it shortens one socket wait without changing the manifest's authorization
    window.  ``None`` leaves every existing caller byte-for-byte unchanged.
    """

    spec = config.model
    policy = ConsultSendPolicy(
        namespace=config.authority.namespace,
        host_instance=config.authority.host_instance,
        access_ref=config.authority.access_ref,
        privacy_classes=config.authority.privacy_classes,
        purposes=config.authority.purposes,
        disclose=config.authority.disclose,
    )
    if spec.transport_kind == "scripted":
        if not config.is_lab:
            raise RuntimeStartupError(
                "CONFIG_INVALID", "the scripted transport requires the lab profile"
            )
        return ScriptedHttpModel(
            store,
            spec,
            policy=policy,
            protocol=(
                DIRECT_HTTP_LEGACY_PROTOCOL if review else spec.model_protocol
            ),
            script=(
                (spec.review_script or spec.script) if review else None
            ),
            request_budget_units=config.request_budget_units,
            output_reserve_units=config.output_reserve_units,
            transport_timeout_cap=transport_timeout_cap,
            max_retries=max_retries,
        )
    return DirectHttpModel(
        store,
        spec.manifest,
        protocol=(
            DIRECT_HTTP_LEGACY_PROTOCOL if review else spec.model_protocol
        ),
        consult_policy=policy,
        request_budget_units=config.request_budget_units,
        output_reserve_units=config.output_reserve_units,
        transport_timeout_cap=transport_timeout_cap,
        max_retries=max_retries,
    )


class ScriptedHttpModel(DirectHttpModel):
    """``DirectHttpModel`` with a deterministic in-process transport.

    Only ``_send`` is replaced.  Everything that makes an attempt auditable --
    the send gate, the reservation, the request hash, the response validation,
    settlement and ``last_attempt_evidence`` -- stays in the real adapter.  The
    script is host configuration, not model output.
    """

    def __init__(
        self,
        store: CognitionStore,
        spec: ModelSpec,
        *,
        policy: ConsultSendPolicy,
        protocol: str | None = None,
        script: Sequence[Mapping[str, Any]] | None = None,
        request_budget_units: int | None = None,
        output_reserve_units: int = 0,
        transport_timeout_cap: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        super().__init__(
            store,
            spec.manifest,
            protocol=spec.model_protocol if protocol is None else protocol,
            consult_policy=policy,
            request_budget_units=request_budget_units,
            output_reserve_units=output_reserve_units,
            transport_timeout_cap=transport_timeout_cap,
            max_retries=max_retries,
        )
        self._spec = spec
        self._script = [
            dict(item) for item in (spec.script if script is None else script)
        ]
        self._script_lock = threading.Lock()
        self._script_index = 0
        self.transport_calls = 0
        self.last_request_sha256: str | None = None

    def _send_blocking(
        self, body_text: str, *, timeout: float, api_key: str
    ) -> str:
        del timeout
        del api_key
        if self._spec.delay_ms:
            time.sleep(self._spec.delay_ms / 1000.0)
        with self._script_lock:
            if self._script_index >= len(self._script):
                raise RuntimeError("the configured model script is exhausted")
            output = self._script[self._script_index]
            self._script_index += 1
            self.transport_calls += 1
            self.last_request_sha256 = hashlib.sha256(
                body_text.encode("utf-8")
            ).hexdigest()
        content = json.dumps(
            output, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        envelope = {
            "id": "chatcmpl-desktop-lab",
            "object": "chat.completion",
            "created": 0,
            "model": self._spec.model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {
                "prompt_tokens": 128,
                "completion_tokens": 32,
                "total_tokens": 160,
            },
        }
        return json.dumps(
            envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )


def _generic_profile_body(review_ref: dict[str, Any]) -> dict[str, Any]:
    """A neutral profile for a fresh public install (no private identity)."""

    return {
        "profile_id": "quintessence.desktop.general",
        "profile_version": "1",
        "identity_summary": (
            "QUINTESSENCE 通用桌面助手。只基于当前可披露材料作答，"
            "不声称未发生的工具调用或未取得的证据。"
        ),
        "principles": [
            "State what is unknown instead of inventing an answer.",
            "Do not claim a tool ran without evidence.",
            "Keep private data out of the model input unless the host authorizes it.",
        ],
        "style": {
            "language": "model_default",
            "tone": "model_default",
            "verbosity_by_context": {"casual": "model_default", "technical": "model_default"},
        },
        "attention_preferences": [],
        "examples": [],
        "asset_hash": "0" * 64,
        "review_ref": review_ref,
        "renderer_compatibility": "pcs.renderer.generic-v1",
    }


def _profile_body(seed: Any) -> dict[str, Any]:
    return {
        "profile_id": seed.profile_id,
        "profile_version": seed.profile_version,
        "identity_summary": (
            "Synthetic desktop Lab profile. No real persona text is present."
        ),
        "principles": [
            "State what is unknown instead of inventing an answer.",
            "Do not claim a tool ran without evidence.",
        ],
        "style": {
            "language": "zh-CN",
            "tone": "plain",
            "verbosity_by_context": {"casual": "short", "technical": "detailed"},
        },
        "attention_preferences": [
            {
                "topic": "synthetic desktop Consult verification",
                "reason": "Lab fixture",
            }
        ],
        "examples": [
            {
                "example_id": "desktop-lab-example-1",
                "scene": "synthetic desktop consult",
                "user": "Synthetic user line.",
                "assistant": "Synthetic assistant line.",
                "synthetic": True,
            }
        ],
        "asset_hash": "0" * 64,
        "renderer_compatibility": "pcs.renderer.synthetic-v1",
    }


def _list_count(value: Any) -> int:
    """Number of items in a stored list, or zero for a non-list value."""

    return len(value) if isinstance(value, (list, tuple)) else 0


def _profile_style_summary(body: Mapping[str, Any]) -> dict[str, Any]:
    """The display-only style block; never the free-text persona body."""

    style = body.get("style") if isinstance(body.get("style"), Mapping) else {}
    verbosity = (
        style.get("verbosity_by_context")
        if isinstance(style.get("verbosity_by_context"), Mapping)
        else {}
    )
    return {
        "language": style.get("language"),
        "tone": style.get("tone"),
        "verbosity_by_context": {
            "casual": verbosity.get("casual"),
            "technical": verbosity.get("technical"),
        },
    }


def valid_persona_import_name(name: Any) -> bool:
    """A bare ``*.json`` file name; no separators, no dot-files, no traversal."""

    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 128
        or not name.endswith(".json")
    ):
        return False
    stem = name[: -len(".json")]
    if not stem or not stem[0].isalnum():
        return False
    return all(char.isalnum() or char in "._-" for char in stem)


def _import_modified_at(value: float) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _import_expires_at() -> str:
    return (
        datetime.fromtimestamp(
            time.time() + PERSONA_IMPORT_TTL_SECONDS, timezone.utc
        )
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _query_summary(query_body: str | None) -> dict[str, Any] | None:
    """Project only the public, displayable part of ``QueryResult.to_body``.

    The internal omission counts and the state revision never enter this
    projection.  Items are reduced to their exact refs plus the public
    current/expired flags so the source drawer can label a ref as current or
    historical without re-running a query.
    """

    if not query_body:
        return None
    try:
        document = json.loads(query_body)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    items: list[dict[str, Any]] = []
    raw_items = document.get("items")
    if isinstance(raw_items, list):
        for item in raw_items[:_MAX_PUBLIC_ITEMS]:
            if not isinstance(item, dict):
                continue
            ref = item.get("ref")
            if not isinstance(ref, dict) or ref.get("ref_type") != "record":
                continue
            items.append(
                {
                    "ref": {
                        "ref_type": "record",
                        "namespace": ref.get("namespace"),
                        "record_id": ref.get("record_id"),
                        "record_version": ref.get("record_version"),
                    },
                    "current": bool(item.get("current")) if "current" in item else None,
                    "expired": bool(item.get("expired")) if "expired" in item else None,
                }
            )
    return {
        "kind": document.get("query_kind"),
        "status": document.get("status"),
        "material_status": document.get("material_status"),
        "truncated": bool(document.get("truncated")),
        "policy_version": document.get("query_policy_version"),
        "items": items,
    }


# --------------------------------------------------------------------------
# review wire projections
# --------------------------------------------------------------------------

_REF_KEYS: dict[str, tuple[str, ...]] = {
    "event": ("ref_type", "namespace", "event_id"),
    "record": ("ref_type", "namespace", "record_id", "record_version"),
    "collab_input": ("ref_type", "namespace", "input_id", "content_version"),
    "collab_artifact": (
        "ref_type",
        "namespace",
        "artifact_id",
        "artifact_version",
    ),
}


def _decode_utf8(raw: bytes) -> str | None:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _strict_json(text: str) -> Any:
    try:
        return strict_json_loads(text)
    except ValueError:
        return None


def _proposal_claim(
    proposal: Mapping[str, Any],
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """The first claim-bearing change of one stored cognition proposal."""

    changes = proposal.get("changes")
    if not isinstance(changes, Sequence):
        return None, None, None
    for change in changes:
        if not isinstance(change, Mapping):
            continue
        target = change.get("target")
        target_value = dict(target) if isinstance(target, Mapping) else None
        body = change.get("body")
        if isinstance(body, Mapping):
            claim = body.get("claim")
            claim_type = body.get("claim_type")
            return (
                claim if isinstance(claim, str) else None,
                claim_type if isinstance(claim_type, str) else None,
                target_value,
            )
        if change.get("op") == "retract_belief":
            reason = change.get("reason")
            return (
                reason if isinstance(reason, str) else "撤回一条既有认识",
                None,
                target_value,
            )
    return None, None, None


def _approval_scope(
    proposal: Mapping[str, Any],
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Derive the approval scope from the *stored* proposal, never the UI."""

    operations: set[str] = set()
    claim_types: set[str] = set()
    scope: dict[str, Any] = {}
    changes = proposal.get("changes")
    if isinstance(changes, Sequence):
        for change in changes:
            if not isinstance(change, Mapping):
                continue
            operation = change.get("op")
            if isinstance(operation, str):
                operations.add(operation)
            body = change.get("body")
            if isinstance(body, Mapping) and isinstance(body.get("claim_type"), str):
                claim_types.add(body["claim_type"])
            candidate_scope = change.get("scope")
            if isinstance(candidate_scope, Mapping):
                scope = dict(candidate_scope)
    return sorted(operations), sorted(claim_types), scope


def _expires_in_minutes(now: str, minutes: int) -> str:
    parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    return (
        (parsed + timedelta(minutes=minutes))
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _job_view(job: Any, attempts: Sequence[Any] = ()) -> dict[str, Any]:
    """The observable half of one maintenance job.

    ``stop_reason`` comes from the most recent attempt: the job row records the
    state and the error, and the attempt records *why* the slice stopped, which
    is what AC-14 asks the host to be able to read.
    """

    last_reason = None
    for attempt in reversed(tuple(attempts)):
        if attempt.stop_reason is not None:
            last_reason = attempt.stop_reason
            break
    return {
        "task_id": job.task_id,
        "task_type": job.task_type,
        "state": job.state,
        "success_checkpoint": job.success_checkpoint,
        "scan_position": job.scan_position,
        "attempt": job.attempt,
        "next_retry_at": job.next_retry_at,
        "last_error": job.last_error,
        "last_stop_reason": last_reason,
        "units_spent": job.units_spent,
    }


def _public_ref(ref: Any) -> dict[str, Any] | None:
    """Return one exact, allow-listed reference object or ``None``."""

    if not isinstance(ref, Mapping):
        return None
    keys = _REF_KEYS.get(ref.get("ref_type"))
    if keys is None or any(key not in ref for key in keys):
        return None
    return {key: ref[key] for key in keys}


def _evidence_link(link: Any) -> dict[str, Any] | None:
    """Project an evidence link without internal grouping fields."""

    if not isinstance(link, Mapping):
        return None
    ref = _public_ref(link.get("ref"))
    if ref is None:
        return None
    relation = link.get("relation")
    return {
        "ref": ref,
        "relation": relation if isinstance(relation, str) else None,
    }


def _proposal_view(proposal: Any) -> dict[str, Any]:
    links = [
        projected
        for projected in (
            _evidence_link(link) for link in proposal.evidence_links
        )
        if projected is not None
    ]
    return {
        "operation": proposal.operation,
        "target_ref": (
            None if proposal.target_ref is None else proposal.target_ref.to_dict()
        ),
        "claim": proposal.claim,
        "claim_type": proposal.claim_type,
        "scope": None if proposal.scope is None else dict(proposal.scope),
        "evidence_links": links,
        "strength": proposal.strength,
        "strength_reason": proposal.strength_reason,
        "valid_until": proposal.valid_until,
    }


def _current_view(current: Any) -> dict[str, Any]:
    return {
        "ref": current.ref.to_dict(),
        "claim": current.claim,
        "claim_type": current.claim_type,
        "status": current.status,
        "strength": current.strength,
        "scope": dict(current.scope),
        "valid_until": current.valid_until,
        "applicability": current.applicability,
    }


def _diff_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field == "evidence_links":
        if not isinstance(value, (list, tuple)):
            return []
        return [
            projected
            for projected in (
                _evidence_link(link) for link in value
            )
            if projected is not None
        ]
    if field == "scope" and isinstance(value, Mapping):
        return dict(value)
    return value


def _diff_field(row: Any) -> dict[str, Any]:
    return {
        "field": row.field,
        "before": _diff_value(row.field, row.before),
        "after": _diff_value(row.field, row.after),
        "changed": bool(row.changed),
    }


def _source_view(source: Any) -> dict[str, Any]:
    return {
        "ref": _public_ref(source.ref),
        "kind": source.kind,
        "availability": source.availability,
        "reason": source.reason,
        "body": source.body,
        "ref_version": source.ref_version,
    }


def _candidate_summary(summary: Any) -> dict[str, Any]:
    ref = summary.candidate
    attempt = summary.attempt_ref if isinstance(summary.attempt_ref, dict) else {}
    attempt_id = attempt.get("attempt_id")
    attempt_no = attempt.get("attempt_no")
    return {
        "candidate_id": ref.candidate_id,
        "artifact_version": ref.artifact_version,
        "work_id": ref.work_id,
        "work_generation": ref.work_generation,
        "interaction_id": summary.interaction_id,
        "context_id": summary.context_id,
        "status": summary.status,
        "created_at": summary.created_at,
        "decided_at": summary.decided_at,
        "attempt_id": attempt_id if isinstance(attempt_id, str) else None,
        "attempt_no": attempt_no if type(attempt_no) is int else None,
    }


def _candidate_detail(detail: Any) -> dict[str, Any]:
    return {
        "candidate": _candidate_summary(detail.candidate),
        "proposal": (
            None if detail.proposal is None else _proposal_view(detail.proposal)
        ),
        "structural_validation": detail.structural_validation,
        "current": None if detail.current is None else _current_view(detail.current),
        "current_state": detail.current_state,
        "diff": [_diff_field(row) for row in detail.diff],
        "sources": [_source_view(source) for source in detail.sources],
        "can_approve": bool(detail.can_approve),
        "can_reject": bool(detail.can_reject),
        "eligibility_reason": detail.eligibility_reason,
        "redacted": bool(detail.redacted),
        "detail": detail.detail,
    }


def _receipt(receipt: Any) -> dict[str, Any]:
    """Project a receipt without the internal ``state_revision`` field."""

    return {
        "request_id": receipt.request_id,
        "context_id": receipt.context_id,
        "operation": receipt.operation,
        "outcome": receipt.outcome,
        "committed_at": receipt.committed_at,
        "record_refs": [
            {
                "record_id": ref.record_id,
                "record_version": ref.record_version,
            }
            for ref in receipt.record_refs
        ],
    }


def _review_view(view: Any) -> dict[str, Any]:
    return {
        "state": view.state,
        "interaction_id": view.interaction_id,
        "candidate_id": view.candidate_id,
        "receipt": None if view.receipt is None else _receipt(view.receipt),
        "redacted": bool(view.redacted),
        "detail": view.detail,
        "model_adapter": view.model_adapter,
    }


def _migrations_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "resources" / "migrations"
    import quintessence_pcs

    return (
        Path(quintessence_pcs.__file__).resolve().parent
        / "resources"
        / "migrations"
    )


def _uuid5(value: str) -> Any:
    import uuid

    return uuid.uuid5(uuid.NAMESPACE_URL, value)


__all__ = [
    "DesktopRuntime",
    "InputRegistrationError",
    "ReviewRequestError",
    "RuntimeStartupError",
    "ScriptedHttpModel",
]
