"""RG03 conversation orchestration: turns, bounded tool loop, publishing.

The orchestrator is the only place that knows the order of a chat turn:

    T0 profile pin -> T1 accepted -> T2 running -> history -> Context ->
    interaction -> one bounded action -> tool? -> next Context -> ... ->
    final/wait message -> completed/failed/cancelled

It never writes SQL, never parses a provider body and never commits cognition.
Every model request goes through the shared ``generate_once`` proof.  The
structured tool loop is ``collab.v2`` only; a ``collab.v1`` turn keeps the
single-generation behaviour of RG02 byte for byte.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..collaboration.generation import (
    GenerationFailure,
    allowed_references,
    artifact_sources,
    generate_once,
    phase_label,
)
from ..domains.refs import RecordRef
from ..foundation.errors import (
    ContextStale,
    CorruptStore,
    DbBusy,
    InvalidInput,
    RecordNotFound,
    Redacted,
)
from ..foundation.jsonutil import canonical_json_text
from ..reply_bubbles import join_bubbles
from ..runtime.collab_protocol import (
    COLLAB_PROTOCOL_VERSION,
    COLLAB_V2_PROTOCOL_VERSION,
    CollabOutput,
    protocol_policy_version,
    protocol_renderer_version,
    repair_feedback,
)
from ..runtime.context import (
    ConsultQueryMaterial,
    ContextService,
    PublishedMessageMaterial,
    ReadSetEntry,
    ToolResultMaterial,
)
from ..runtime.tool_catalog import ToolOutcome
from ..store.conversation import ConversationRow, MessageRow, StepRow, TurnRow
from ..store.errors import ContextTooLarge, NeedsContext, TurnStateConflict
from .content_task import build_content_task
from .expression_check import check_expression
from .expression_stage import (
    build_content_handoff,
    build_expression_task,
    published_refs,
)
from .persona import build_persona_material
from .plain_text_final import classify_parse_rejection, plain_text_final
from .time_anchor import find_suspect_day_anchors
from .policy_bundle import PolicyBundle, resolve_bundle
from .sdrpe import (
    SKIP_CONTEXT_TOO_LARGE,
    SKIP_DRAFT_TOO_LARGE,
    SdrpePlan,
    content_request_cap,
    decide,
    skip,
)
from .tools import (
    ToolRuntime,
    collect_exact_refs,
    execute_tool,
    merge_tool_sources,
    tool_call_from_output,
)

LOGGER = logging.getLogger("quintessence.conversation")

DEFAULT_POLICY_REF = "policy:collab.v1"

#: Per-turn budgets.  Values are configuration, never model input; the tool
#: cap is 6 because the deletion owner chain adds one level on top of the
#: registration depth (RG03 §4.7).
DEFAULT_MAX_MODEL_REQUESTS = 8
DEFAULT_MAX_TOOL_CALLS = 6
DEFAULT_TURN_WALL_SECONDS = 180.0
DEFAULT_TOOL_WALL_SECONDS = 30.0
DEFAULT_COMMENTARY_MAX_PER_TURN = 2
DEFAULT_COMMENTARY_MAX_CHARS = 60
DEFAULT_TOOL_RESULT_EXCERPT_BYTES = 1536
DEFAULT_TOOL_RESULTS_TOTAL_BYTES = 6144
MAX_REPEATED_CALLS = 3

#: Host fallback commentary, one short line per real tool step.  The text says
#: what the host is about to do, never that a result already exists.  An unknown
#: (but catalog-validated) tool gets the neutral line.
TOOL_COMMENTARY_FALLBACKS = {
    "web.search": "我查一下",
    "web.fetch": "我打开看看",
    "cognition.belief.read": "我翻一下记录",
    "cognition.belief.history": "我翻一下版本记录",
    "cognition.partition": "我看一下现有记录",
    "cognition.propose": "我提交一条候选",
    "cognition.review": "我查一下这条候选",
    "cognition.result": "我看一下结果",
    "question.list": "我看一下问题列表",
    "question.get": "我打开看一下这个问题",
    "question.create": "我记下一个问题",
    "question.transition": "我更新一下问题状态",
    "attention.list": "我看一下注意力列表",
    "conversation.search": "我翻一下之前的对话",
}

#: A second host line when the model's text was a duplicate of the previous
#: commentary.  It still describes the action, never a result.
TOOL_COMMENTARY_FALLBACKS_ALT = {
    "web.search": "我再查一下",
    "web.fetch": "我再看一眼",
    "cognition.belief.read": "我再翻一下记录",
    "cognition.belief.history": "我再翻一下版本记录",
    "cognition.partition": "我再看一下现有记录",
    "cognition.propose": "我再提交一条候选",
    "cognition.review": "我再查一下这条候选",
    "cognition.result": "我再看一下结果",
    "question.list": "我再看一下问题列表",
    "question.get": "我再打开看一下这个问题",
    "question.create": "我再记下一个问题",
    "question.transition": "我再更新一下问题状态",
    "attention.list": "我再看一下注意力列表",
    "conversation.search": "我再翻一下之前的对话",
}

_TOOL_COMMENTARY_NEUTRAL = "我看一下"
_TOOL_COMMENTARY_NEUTRAL_ALT = "我再看一下"

#: The tool-shaped format-repair feedback.  It is the complete feedback for
#: this branch, not an addendum: the model is told that the rejected response's
#: tool markup was not executed and is given the two legal JSON shapes.  It
#: never echoes model output or a tool name and is added only after a parse
#: failure (never to a semantic refusal or after a tool already ran in this
#: same rejected response).
TOOL_FORMAT_REPAIR_FEEDBACK = (
    "上次响应使用了工具标记，未被接纳，工具没有执行。"
    "请将同一请求改写为一个合法 JSON 动作，不要改成声称查询完成的回答。"
    '若仍需调用工具，使用 {"action":"call_tool","tool_call":'
    '{"name":"当前合法工具名","arguments":{合法参数}}}；'
    '若无需工具，使用 {"action":"final","reply":"回答正文"}。'
    "不要输出 JSON 以外的文字。"
)

#: The same repair when earlier tool calls in this turn really did execute.
#: Saying "工具没有执行" globally would be false, and a re-issued call must not
#: duplicate a completed side effect.
TOOL_FORMAT_REPAIR_FEEDBACK_AFTER_EXECUTION = (
    "上次响应使用了工具标记，未被接纳，这次工具请求没有执行。"
    "此前已有工具调用发生，宿主保留其真实状态；不要重复执行已成功的调用。"
    "请将同一请求改写为一个合法 JSON 动作，不要改成声称查询完成的回答。"
    '若仍需调用工具，使用 {"action":"call_tool","tool_call":'
    '{"name":"当前合法工具名","arguments":{合法参数}}}；'
    '若无需工具，使用 {"action":"final","reply":"回答正文"}。'
    "不要输出 JSON 以外的文字。"
)


class PersonaNotReady(Exception):
    """T0 could not pin an active, non-deleted persona profile."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def resolve_turn_profile(profiles: Any, namespace: str) -> dict[str, Any]:
    """T0: read the active profile once, before any turn row exists."""

    try:
        active = profiles.get_active_profile(namespace)
    except Redacted as exc:
        raise PersonaNotReady("PERSONA_REDACTED") from exc
    if active.ref is None:
        raise PersonaNotReady("PERSONA_NOT_READY")
    return active.ref.to_dict()


@dataclass(frozen=True)
class TurnLimits:
    max_model_requests: int = DEFAULT_MAX_MODEL_REQUESTS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    turn_wall_seconds: float = DEFAULT_TURN_WALL_SECONDS
    tool_wall_seconds: float = DEFAULT_TOOL_WALL_SECONDS
    commentary_max_per_turn: int = DEFAULT_COMMENTARY_MAX_PER_TURN
    commentary_max_chars: int = DEFAULT_COMMENTARY_MAX_CHARS
    tool_result_excerpt_bytes: int = DEFAULT_TOOL_RESULT_EXCERPT_BYTES
    tool_results_total_bytes: int = DEFAULT_TOOL_RESULTS_TOTAL_BYTES
    tools_enabled: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.max_model_requests <= 16:
            raise ValueError("max_model_requests must be 1..16")
        if not 0 <= self.max_tool_calls <= 6:
            raise ValueError("max_tool_calls must be 0..6")
        if not 30 <= self.turn_wall_seconds <= 600:
            raise ValueError("turn_wall_seconds must be 30..600")
        if not 5 <= self.tool_wall_seconds <= 120:
            raise ValueError("tool_wall_seconds must be 5..120")
        if not 0 <= self.commentary_max_per_turn <= 4:
            raise ValueError("commentary_max_per_turn must be 0..4")
        if not 1 <= self.commentary_max_chars <= 200:
            raise ValueError("commentary_max_chars must be 1..200")
        if not 256 <= self.tool_result_excerpt_bytes <= 8192:
            raise ValueError("tool_result_excerpt_bytes must be 256..8192")
        if not 1024 <= self.tool_results_total_bytes <= 16384:
            raise ValueError("tool_results_total_bytes must be 1024..16384")
        if self.tool_result_excerpt_bytes > self.tool_results_total_bytes:
            raise ValueError("excerpt bytes must not exceed total bytes")
        if type(self.tools_enabled) is not bool:
            raise ValueError("tools_enabled must be a boolean")


@dataclass(frozen=True)
class TurnExecution:
    turn: TurnRow
    interaction_id: str | None
    context_id: str | None


@dataclass
class _ExecutionState:
    """The ids a failure path needs even when raised deep inside a runner."""

    stage_id: str | None = None
    interaction_id: str | None = None
    context_id: str | None = None


class _TurnFailure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Cancelled(Exception):
    """The worker saw the cancel flag at a step boundary."""


class ConversationOrchestrator:
    """One bounded conversation turn, over the real store and Context service."""

    def __init__(
        self,
        store: Any,
        *,
        context_service: ContextService,
        model: Any,
        max_history_turns: int = 8,
        max_history_bytes: int = 8192,
        max_generations: int = 2,
        protocol_version: str = COLLAB_PROTOCOL_VERSION,
        limits: TurnLimits | None = None,
        tool_runtime_factory: Callable[[str], ToolRuntime] | None = None,
        cognition_material_factory: (
            Callable[[], tuple[ConsultQueryMaterial | None, tuple[dict[str, Any], ...]]]
            | None
        ) = None,
        expression_material_factory: Callable[[Any], Any] | None = None,
        memory_recall_factory: Callable[[Any, Mapping[str, Any]], Any] | None = None,
        cognition_status_factory: Callable[[], int] | None = None,
        event_sink: Callable[[Any, Mapping[str, Any] | None], None] | None = None,
        cancel_requested: Callable[[str], bool] | None = None,
        host_is_lab: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._contexts = context_service
        self._model = model
        self._max_history_turns = max_history_turns
        self._max_history_bytes = max_history_bytes
        self._max_generations = max_generations
        self._protocol = protocol_version
        self._limits = limits or TurnLimits()
        self._tool_runtime_factory = tool_runtime_factory
        self._cognition_material_factory = cognition_material_factory
        self._expression_material_factory = expression_material_factory
        self._memory_recall_factory = memory_recall_factory
        self._cognition_status_factory = cognition_status_factory
        self._event_sink = event_sink
        self._cancel_requested = cancel_requested or (lambda _turn_id: False)
        # Trusted host identity, injected by the sidecar from HostConfig.
        # Never read from a request, a user message or a model output.
        self._host_is_lab = bool(host_is_lab)
        self._clock = clock

    # ------------------------------------------------------------------ turn

    def execute(
        self,
        *,
        conversation: ConversationRow,
        turn: TurnRow,
        input_ref: dict[str, Any],
        profile_ref: dict[str, Any],
        access: Any,
        retention: str,
        policy_ref: str = DEFAULT_POLICY_REF,
    ) -> TurnExecution:
        """Run one accepted turn to a persisted terminal state (or waiting)."""

        namespace = conversation.namespace
        state = _ExecutionState()
        try:
            strategy = getattr(turn, "strategy", None) or {}
            plan = SdrpePlan.from_strategy(
                strategy.get("sdrpe") if isinstance(strategy, Mapping) else None,
                protocol_version=self._protocol,
            )
            # One bundle per turn: resolved here, before any Context is built,
            # and handed down unchanged to both stages.  v1 and SDRPE off
            # never read the persona asset.
            bundle = resolve_bundle(
                self._store,
                namespace,
                RecordRef.from_value(profile_ref, field="profile_ref"),
                host_is_lab=self._host_is_lab,
                include_persona=(
                    self._protocol == COLLAB_V2_PROTOCOL_VERSION
                    and plan.effective_mode == "on"
                ),
            )
            persona_material = self._persona_material(namespace, turn, profile_ref)
            stage = self._store.begin_turn(
                namespace=namespace,
                turn_id=turn.turn_id,
                policy_version=protocol_policy_version(self._protocol),
                on_event=self._event_sink,
            )
            state.stage_id = stage.stage_id
            recall_session = self._new_memory_recall_session(turn, input_ref)
            runner = (
                self._run_tool_loop if self._tools_enabled() else self._run_single
            )
            return runner(
                namespace=namespace,
                conversation=conversation,
                turn=turn,
                input_ref=input_ref,
                profile_ref=profile_ref,
                access=access,
                retention=retention,
                policy_ref=policy_ref,
                stage=stage,
                state=state,
                persona=persona_material,
                sdrpe=plan,
                bundle=bundle,
                memory_recall_session=recall_session,
            )
        except _Cancelled:
            return TurnExecution(
                self._store.turn_result(
                    namespace=namespace,
                    host_instance=conversation.host_instance,
                    turn_id=turn.turn_id,
                    access=access,
                ).turn,
                state.interaction_id,
                state.context_id,
            )
        except TurnStateConflict:
            return TurnExecution(
                self._store.turn_result(
                    namespace=namespace,
                    host_instance=conversation.host_instance,
                    turn_id=turn.turn_id,
                    access=access,
                ).turn,
                state.interaction_id,
                state.context_id,
            )
        except _TurnFailure as failure:
            return self._fail(namespace, turn, failure.reason, state)
        except ContextTooLarge as exc:
            return self._fail(
                namespace, turn, f"context_too_large:{phase_label(exc)}", state
            )
        except NeedsContext as exc:
            return self._fail(
                namespace, turn, f"persona_not_ready:{phase_label(exc)}", state
            )
        except GenerationFailure as exc:
            return self._fail(
                namespace, turn, f"{exc.phase}:{exc.detail}", state
            )
        except ContextStale as exc:
            return self._fail(
                namespace, turn, f"context:{phase_label(exc)}", state
            )
        except (CorruptStore, DbBusy):
            raise
        except RecordNotFound:
            raise
        except Exception as exc:  # noqa: BLE001 - a turn failure is a result
            LOGGER.warning("conversation turn failed class=%s", type(exc).__name__)
            return self._fail(
                namespace, turn, f"turn:{phase_label(exc)}", state
            )

    def _content_task_for(self, plan: SdrpePlan, bundle: PolicyBundle) -> dict[str, Any] | None:
        """The content-stage task, or None on the collab.v1 path."""

        if self._protocol != COLLAB_V2_PROTOCOL_VERSION or bundle is None:
            return None
        return build_content_task(sdrpe_mode=plan.effective_mode, bundle=bundle)

    @staticmethod
    def _policy_record_ref(bundle: PolicyBundle) -> RecordRef | None:
        """The exact persona policy record this turn rendered, if any."""

        persona = getattr(bundle, "persona", None)
        if persona is None or persona.state != "present" or not persona.ref:
            return None
        return RecordRef.from_value(persona.ref, field="policy_record_ref")

    def _persona_material(
        self,
        namespace: str,
        turn: TurnRow,
        profile_ref: dict[str, Any],
    ) -> dict[str, Any] | None:
        """RG06: rebuild the T0 persona snapshot from the pinned profile version.

        ``collab.v1`` turns never carry persona material, so their Context is
        byte-identical to the RG02/RG03 behaviour.  A missing example or a hash
        mismatch is a corrupt store, never a silently reduced persona.
        """

        if self._protocol != COLLAB_V2_PROTOCOL_VERSION:
            return None
        strategy = getattr(turn, "strategy", None) or {}
        plan = strategy.get("persona") if isinstance(strategy, Mapping) else None
        if not isinstance(plan, Mapping):
            return None
        try:
            version = self._store.get_version(
                namespace,
                profile_ref["record_id"],
                int(profile_ref["record_version"]),
            )
        except (RecordNotFound, Redacted) as exc:
            raise CorruptStore(
                "the pinned persona profile version is not readable"
            ) from exc
        if version.kind != "persona_profile":
            raise CorruptStore("the pinned persona ref names a non-profile record")
        return build_persona_material(dict(version.body), plan)

    def _expression_material(self, turn: Any) -> Any:
        """The host-built expression-preference material for this turn."""

        if self._expression_material_factory is None:
            return None
        return self._expression_material_factory(turn)

    def _cognition_materials(
        self,
    ) -> tuple[ConsultQueryMaterial | None, tuple[dict[str, Any], ...]]:
        """The host-built working set for the Context about to be assembled.

        Rebuilt on every step: a tool may have committed cognition, which moves
        the revision basis the Context save re-checks.
        """

        if self._cognition_material_factory is None:
            return None, ()
        material, refs = self._cognition_material_factory()
        return material, tuple(refs)

    def _tools_enabled(self) -> bool:
        return (
            self._protocol == COLLAB_V2_PROTOCOL_VERSION
            and self._limits.tools_enabled
            and self._tool_runtime_factory is not None
        )

    def _new_memory_recall_session(self, turn: Any, input_ref: Mapping[str, Any]) -> Any:
        """Create the one per-turn recall session, or ``None`` when disabled."""

        if self._memory_recall_factory is None:
            return None
        try:
            return self._memory_recall_factory(turn, input_ref)
        except Exception:  # noqa: BLE001 - recall must never break a turn
            LOGGER.warning("memory recall session construction failed")
            return None

    def _memory_recall_material(
        self, session: Any, *, tool_results: Any, history: Any
    ) -> Any:
        if session is None:
            return None
        try:
            return session.material(tool_results=tool_results, history=history)
        except Exception:  # noqa: BLE001 - recall must never break a turn
            LOGGER.warning("memory recall materialization failed")
            return None

    def _record_memory_recall_event(self, session: Any, namespace: str, turn: Any) -> None:
        """Persist one bounded status event when the session reports a change."""

        if session is None or not hasattr(session, "take_event"):
            return
        try:
            payload = session.take_event()
        except Exception:  # noqa: BLE001
            return
        if not isinstance(payload, Mapping):
            return
        try:
            self._store.record_turn_event(
                namespace=namespace,
                turn_id=turn.turn_id,
                kind="memory_recall",
                payload=dict(payload),
                on_event=self._event_sink,
            )
        except Exception:  # noqa: BLE001 - an audit event must not fail the turn
            LOGGER.warning("memory recall event could not be recorded")

    # -------------------------------------------------------------- single

    def _run_single(
        self,
        *,
        namespace: str,
        conversation: ConversationRow,
        turn: TurnRow,
        input_ref: dict[str, Any],
        profile_ref: dict[str, Any],
        access: Any,
        retention: str,
        policy_ref: str,
        stage: Any,
        state: _ExecutionState,
        persona: dict[str, Any] | None = None,
        sdrpe: SdrpePlan | None = None,
        bundle: PolicyBundle | None = None,
        memory_recall_session: Any = None,
    ) -> TurnExecution:
        """The RG02 single-generation path, unchanged for collab.v1 turns."""

        plan = sdrpe or SdrpePlan.resolve("off", protocol_version=self._protocol)
        started = self._clock()
        history = self._store.read_history(
            access=access,
            namespace=namespace,
            conversation_id=conversation.conversation_id,
            before_turn_no=turn.turn_no,
            max_turns=self._max_history_turns,
            max_bytes=self._max_history_bytes,
        )
        pinned = RecordRef.from_value(profile_ref, field="profile_ref")
        memory_recall = self._memory_recall_material(
            memory_recall_session, tool_results=(), history=history
        )
        context = self._contexts.build_consult_context(
            namespace=namespace,
            access=access,
            input_refs=[input_ref],
            adapter_kind=self._model.adapter_kind,
            profile_ref=pinned,
            history=history,
            renderer_version=protocol_renderer_version(self._protocol),
            persona=persona,
            content_task=self._content_task_for(plan, bundle),
            policy_record_ref=self._policy_record_ref(bundle),
            expression_material=self._expression_material(turn),
            memory_recall=memory_recall,
        )
        self._record_memory_recall_event(
            memory_recall_session, namespace, turn
        )
        interaction_id = self._store.register_interaction(
            namespace=namespace,
            mode="consult",
            activation_kind="user_input",
            host_instance=conversation.host_instance,
            access_ref=access.access_ref,
            policy_ref=policy_ref,
            input_refs=[input_ref],
            context_id=context.context_id,
        )
        state.interaction_id = interaction_id
        state.context_id = context.context_id
        self._store.bind_stage_context(
            namespace=namespace,
            stage_id=stage.stage_id,
            context_id=context.context_id,
            ordinal=1,
            purpose="content_initial",
        )
        output, evidence, _used = self._generate(
            context,
            min(self._max_generations, self._limits.max_model_requests),
            namespace=namespace,
            turn_id=turn.turn_id,
        )
        action = output.action
        if action in ("none", "final"):
            parts = output.bubbles()
            if not parts:
                raise _TurnFailure("no_reply")
            return self._finish_draft(
                bundle=bundle,
                namespace=namespace,
                conversation=conversation,
                turn=turn,
                input_ref=input_ref,
                access=access,
                retention=retention,
                stage=stage,
                state=state,
                persona=persona,
                profile_ref=profile_ref,
                history=history,
                published=(),
                draft=join_bubbles(parts),
                draft_parts=parts,
                evidence=evidence,
                context=context,
                interaction_id=interaction_id,
                sdrpe=plan,
                requests_used=_used,
                started=started,
            )
        if action == "wait":
            text = output.reply if output.reply is not None else output.wait_question
            if text is None:
                raise _TurnFailure("wait_without_reply")
            if plan.effective_mode == "on" or plan.requested_mode == "auto":
                decision = decide(
                    plan=plan,
                    turn_mode=turn.mode,
                    content_action="wait",
                    protocol_version=self._protocol,
                    max_model_requests=self._limits.max_model_requests,
                    requests_used=_used,
                    wall_left=self._limits.turn_wall_seconds
                    - (self._clock() - started),
                )
                self._store.skip_expression_stage(
                    namespace=namespace,
                    turn_id=turn.turn_id,
                    reason=decision.reason or "wait_question",
                    on_event=self._event_sink,
                )
            waiting = self._store.publish_assistant_message(
                namespace=namespace,
                turn_id=turn.turn_id,
                stage_id=stage.stage_id,
                interaction_id=interaction_id,
                context_id=context.context_id,
                text=text,
                retention=retention,
                sources=artifact_sources(context),
                attempt_ref=evidence.attempt_ref,
                kind="wait_question",
                on_event=self._event_sink,
            )
            return TurnExecution(waiting, interaction_id, context.context_id)
        if action == "query":
            raise _TurnFailure("query_unavailable")
        raise _TurnFailure("invalid_action")

    # ----------------------------------------------------------------- tools

    def _run_tool_loop(
        self,
        *,
        namespace: str,
        conversation: ConversationRow,
        turn: TurnRow,
        input_ref: dict[str, Any],
        profile_ref: dict[str, Any],
        access: Any,
        retention: str,
        policy_ref: str,
        stage: Any,
        state: _ExecutionState,
        persona: dict[str, Any] | None = None,
        sdrpe: SdrpePlan | None = None,
        bundle: PolicyBundle | None = None,
        memory_recall_session: Any = None,
    ) -> TurnExecution:
        limits = self._limits
        plan = sdrpe or SdrpePlan.resolve("off", protocol_version=self._protocol)
        content_cap = content_request_cap(plan, limits.max_model_requests)
        history = self._store.read_history(
            access=access,
            namespace=namespace,
            conversation_id=conversation.conversation_id,
            before_turn_no=turn.turn_no,
            max_turns=self._max_history_turns,
            max_bytes=self._max_history_bytes,
        )
        pinned = RecordRef.from_value(profile_ref, field="profile_ref")
        if self._tool_runtime_factory is None:  # pragma: no cover - _tools_enabled
            raise _TurnFailure("tools_unavailable")
        runtime = self._tool_runtime_factory("")
        definitions = tuple(runtime.catalog.definitions())
        published: list[PublishedMessageMaterial] = []
        results: list[ToolResultMaterial] = []
        commentary_seen: set[str] = set()
        commentary_count = 0
        tool_commentary_count = 0
        requests = 0
        tool_calls = 0
        executed_tools = 0
        step_no = 0
        ordinal = 1
        last_key: tuple[str, str] | None = None
        repeats = 0
        started = self._clock()
        renderer = protocol_renderer_version(self._protocol)

        cognition_material, cognition_refs = self._cognition_materials()
        memory_recall = self._memory_recall_material(
            memory_recall_session, tool_results=(), history=history
        )
        context = self._contexts.build_consult_context(
            namespace=namespace,
            access=access,
            input_refs=[input_ref],
            adapter_kind=self._model.adapter_kind,
            profile_ref=pinned,
            history=history,
            published_messages=published,
            tool_results=results,
            tool_definitions=definitions,
            renderer_version=renderer,
            persona=persona,
            content_task=self._content_task_for(plan, bundle),
            policy_record_ref=self._policy_record_ref(bundle),
            cognition_material=cognition_material,
            cognition_collab_refs=cognition_refs,
            expression_material=self._expression_material(turn),
            memory_recall=memory_recall,
        )
        self._record_memory_recall_event(
            memory_recall_session, namespace, turn
        )
        runtime = runtime.rebind(context.context_id, turn.turn_id)
        interaction_id = self._store.register_interaction(
            namespace=namespace,
            mode="consult",
            activation_kind="user_input",
            host_instance=conversation.host_instance,
            access_ref=access.access_ref,
            policy_ref=policy_ref,
            input_refs=[input_ref],
            context_id=context.context_id,
        )
        state.interaction_id = interaction_id
        state.context_id = context.context_id
        self._store.bind_stage_context(
            namespace=namespace,
            stage_id=stage.stage_id,
            context_id=context.context_id,
            ordinal=ordinal,
            purpose="content_initial",
        )

        while True:
            self._require_not_cancelled(
                turn.turn_id, namespace, stage, interaction_id
            )
            if requests >= content_cap:
                raise _TurnFailure("budget:model_requests")
            if self._clock() - started > limits.turn_wall_seconds:
                raise _TurnFailure("wall:turn_wall_seconds")
            remaining = content_cap - requests
            output, evidence, used = self._generate(
                context,
                remaining,
                namespace=namespace,
                turn_id=turn.turn_id,
                tools_executed=executed_tools,
            )
            requests += used
            self._require_not_cancelled(
                turn.turn_id,
                namespace,
                stage,
                interaction_id,
                attempt_ref=evidence.attempt_ref,
            )
            action = output.action
            if action == "final":
                parts = output.bubbles()
                if not parts:
                    raise _TurnFailure("no_reply")
                commentary, omitted = self._commentary_text(
                    output.commentary,
                    mode=turn.mode,
                    count=commentary_count,
                    seen=commentary_seen,
                )
                if commentary is not None:
                    commentary_ref = self._publish_commentary(
                        namespace=namespace,
                        turn=turn,
                        stage=stage,
                        interaction_id=interaction_id,
                        context=context,
                        text=commentary,
                        retention=retention,
                        attempt_ref=evidence.attempt_ref,
                        published=published,
                    )
                    commentary_count += 1
                else:
                    commentary_ref = None
                return self._finish_draft(
                    bundle=bundle,
                    namespace=namespace,
                    conversation=conversation,
                    turn=turn,
                    input_ref=input_ref,
                    access=access,
                    retention=retention,
                    stage=stage,
                    state=state,
                    persona=persona,
                    profile_ref=profile_ref,
                    history=history,
                    published=published,
                    draft=join_bubbles(parts),
                    draft_parts=parts,
                    evidence=evidence,
                    context=context,
                    interaction_id=interaction_id,
                    sdrpe=plan,
                    requests_used=requests,
                    started=started,
                    commentary_omitted=omitted,
                    commentary_ref=commentary_ref,
                )
            if action == "wait":
                if output.wait_question is None:
                    raise _TurnFailure("wait_without_reply")
                if (
                    plan.effective_mode == "on"
                    or plan.requested_mode == "auto"
                ):
                    decision = decide(
                        plan=plan,
                        turn_mode=turn.mode,
                        content_action="wait",
                        protocol_version=self._protocol,
                        max_model_requests=limits.max_model_requests,
                        requests_used=requests,
                        wall_left=limits.turn_wall_seconds
                        - (self._clock() - started),
                    )
                    self._store.skip_expression_stage(
                        namespace=namespace,
                        turn_id=turn.turn_id,
                        reason=decision.reason or "wait_question",
                        on_event=self._event_sink,
                    )
                commentary, omitted = self._commentary_text(
                    output.commentary,
                    mode=turn.mode,
                    count=commentary_count,
                    seen=commentary_seen,
                )
                sources = artifact_sources(context)
                if commentary is not None:
                    ref = self._publish_commentary(
                        namespace=namespace,
                        turn=turn,
                        stage=stage,
                        interaction_id=interaction_id,
                        context=context,
                        text=commentary,
                        retention=retention,
                        attempt_ref=evidence.attempt_ref,
                        published=published,
                    )
                    commentary_count += 1
                    sources = sources + [ref]
                waiting = self._store.publish_assistant_message(
                    namespace=namespace,
                    turn_id=turn.turn_id,
                    stage_id=stage.stage_id,
                    interaction_id=interaction_id,
                    context_id=context.context_id,
                    text=output.wait_question,
                    retention=retention,
                    sources=sources,
                    attempt_ref=evidence.attempt_ref,
                    kind="wait_question",
                    commentary_omitted=omitted,
                    on_event=self._event_sink,
                )
                return TurnExecution(waiting, interaction_id, context.context_id)
            if action != "call_tool" or output.tool_call is None:
                raise _TurnFailure("invalid_action")
            if tool_calls >= limits.max_tool_calls:
                raise _TurnFailure("budget:tool_calls")
            if self._clock() - started > limits.turn_wall_seconds:
                raise _TurnFailure("wall:turn_wall_seconds")

            call = tool_call_from_output(output.tool_call)
            arguments = call.arguments_mapping()
            step_no += 1
            refusal = runtime.catalog.validate(call)
            commentary: str | None = None
            omitted: str | None = None
            commentary_source: str | None = None
            if refusal is None:
                # Tool commentary is bounded by the tool budget, not by the
                # general chat-filler cap: every accepted tool step can carry
                # one short line.  A disabled commentary policy (0) still
                # disables both model and host lines.
                tool_limit = (
                    self._limits.max_tool_calls
                    if self._limits.commentary_max_per_turn > 0
                    else 0
                )
                commentary, omitted = self._commentary_text(
                    output.commentary,
                    mode=turn.mode,
                    count=tool_commentary_count,
                    seen=commentary_seen,
                    limit=tool_limit,
                )
                model_text = output.commentary
                model_text_missing = (
                    model_text is None or not model_text.strip()
                )
                model_text_duplicate = (
                    omitted == "duplicate" and not model_text_missing
                )
                if (
                    commentary is None
                    and turn.mode == "chat"
                    and tool_limit > 0
                    and (
                        (
                            omitted in (None, "empty")
                            and model_text_missing
                        )
                        or model_text_duplicate
                    )
                ):
                    # A missing model line and a duplicate model line both get a
                    # visible host line.  The duplicate model text itself stays
                    # suppressed; the replacement is a different host sentence
                    # with an explicit host_fallback source.
                    commentary = self._host_fallback_commentary(
                        call.name, avoid=commentary_seen
                    )
                    commentary_source = "host_fallback"
                    omitted = None
                elif commentary is not None:
                    commentary_source = "model"
                if commentary is not None:
                    # The public sentence is committed before the tool is even
                    # planned or started, so it can never describe a result the
                    # host has not received.
                    self._publish_commentary(
                        namespace=namespace,
                        turn=turn,
                        stage=stage,
                        interaction_id=interaction_id,
                        context=context,
                        text=commentary,
                        retention=retention,
                        attempt_ref=evidence.attempt_ref,
                        published=published,
                        commentary_source=commentary_source or "model",
                    )
                    if commentary_source == "host_fallback":
                        commentary_seen.add(" ".join(commentary.split()))
                    tool_commentary_count += 1
            step = self._store.plan_tool_step(
                namespace=namespace,
                turn_id=turn.turn_id,
                stage_id=stage.stage_id,
                step_no=step_no,
                tool_name=call.name,
                arguments_digest=_arguments_digest(call.arguments),
                operation_key=f"tool:{step_no}",
                commentary_omitted=omitted,
                # A rejected call must not publish its arguments even through
                # the transient live ring; an accepted call keeps RG03's live
                # argument display.
                live_payload=(
                    None if refusal is not None else {"arguments": dict(arguments)}
                ),
                on_event=self._event_sink,
            )
            if refusal is not None:
                body = canonical_json_text(
                    {
                        "tool": call.name,
                        "status": "failed",
                        "result": {
                            "ok": False,
                            "code": refusal.code,
                            "message": str(refusal.payload.get("message", "")),
                        },
                    }
                )
                step = self._store.finish_tool_step(
                    namespace=namespace,
                    turn_id=turn.turn_id,
                    step_id=step.step_id,
                    interaction_id=interaction_id,
                    context_id=context.context_id,
                    status="failed",
                    duration_ms=0,
                    body=body,
                    sources=artifact_sources(context),
                    retention=retention,
                    error_label=f"refused:{refusal.code}",
                    live_payload=_refusal_live(refusal),
                    on_event=self._event_sink,
                    allow_planned=True,
                    event_kind="tool_rejected",
                    public_status="rejected",
                    reason=_tool_public_reason(refusal.code),
                )
                results.append(_tool_material(step, call.name, "failed", body))
            else:
                step = self._store.start_tool_step(
                    namespace=namespace,
                    turn_id=turn.turn_id,
                    step_id=step.step_id,
                    on_event=self._event_sink,
                )
                began = self._clock()
                _, outcome = execute_tool(runtime, output.tool_call)
                executed_tools += 1
                duration_ms = int((self._clock() - began) * 1000)
                status = "succeeded" if outcome.ok else "failed"
                body_document: dict[str, Any] = {
                    "tool": call.name,
                    "status": status,
                    # A failed tool keeps its stable code in the body the
                    # model and the process panel read: "web_timeout" is a
                    # refusal, not a result the model may paper over.
                    **({"error_code": outcome.code} if not outcome.ok else {}),
                    "result": dict(outcome.payload),
                }
                if outcome.ok:
                    # Failed/rejected calls do not publish their arguments;
                    # the model already knows what it asked for.
                    body_document["arguments"] = dict(arguments)
                body = canonical_json_text(body_document)
                sources = merge_tool_sources(
                    artifact_sources(context),
                    collect_exact_refs(outcome.payload, namespace),
                )
                step = self._store.finish_tool_step(
                    namespace=namespace,
                    turn_id=turn.turn_id,
                    step_id=step.step_id,
                    interaction_id=interaction_id,
                    context_id=context.context_id,
                    status=status,
                    duration_ms=duration_ms,
                    body=body,
                    sources=sources,
                    retention=retention,
                    error_label=None if outcome.ok else outcome.code,
                    live_payload=_outcome_live(outcome),
                    on_event=self._event_sink,
                    reason=(
                        None if outcome.ok else _tool_public_reason(outcome.code)
                    ),
                )
                results.append(_tool_material(step, call.name, status, body))
            tool_calls += 1

            key = (call.name, _arguments_digest(call.arguments))
            if key == last_key:
                repeats += 1
            else:
                repeats = 1
                last_key = key
            if repeats > MAX_REPEATED_CALLS:
                raise _TurnFailure("tool:repeated_calls")

            selected = _select_tool_results(
                results,
                limits.tool_results_total_bytes,
                limits.tool_result_excerpt_bytes,
            )
            ordinal += 1
            cognition_material, cognition_refs = self._cognition_materials()
            memory_recall = self._memory_recall_material(
                memory_recall_session, tool_results=selected, history=history
            )
            context = self._contexts.build_consult_context(
                namespace=namespace,
                access=access,
                input_refs=[input_ref],
                adapter_kind=self._model.adapter_kind,
                profile_ref=pinned,
                history=history,
                published_messages=published,
                tool_results=selected,
                tool_definitions=definitions,
                renderer_version=renderer,
                persona=persona,
                content_task=self._content_task_for(plan, bundle),
                policy_record_ref=self._policy_record_ref(bundle),
                cognition_material=cognition_material,
                cognition_collab_refs=cognition_refs,
                expression_material=self._expression_material(turn),
                memory_recall=memory_recall,
            )
            self._record_memory_recall_event(
                memory_recall_session, namespace, turn
            )
            runtime = runtime.rebind(context.context_id, turn.turn_id)
            state.context_id = context.context_id
            self._store.bind_stage_context(
                namespace=namespace,
                stage_id=stage.stage_id,
                context_id=context.context_id,
                ordinal=ordinal,
                purpose="content_after_tool",
            )

    # ------------------------------------------------------------ generation

    # ----------------------------------------------------- expression stage

    def _finish_draft(
        self,
        *,
        namespace: str,
        conversation: ConversationRow,
        turn: TurnRow,
        input_ref: dict[str, Any],
        access: Any,
        retention: str,
        stage: Any,
        state: _ExecutionState,
        persona: dict[str, Any] | None,
        profile_ref: dict[str, Any],
        history: Any,
        published: Sequence[PublishedMessageMaterial],
        draft: str,
        evidence: Any,
        context: Any,
        interaction_id: str,
        sdrpe: SdrpePlan,
        bundle: PolicyBundle,
        requests_used: int,
        started: float,
        draft_parts: Sequence[str] | None = None,
        commentary_omitted: str | None = None,
        commentary_ref: Mapping[str, Any] | None = None,
    ) -> TurnExecution:
        """Turn one finished content draft into the turn's only public reply.

        ``off``/``auto`` publish exactly as before.  ``on`` either runs the
        expression stage or records a real skip; a failed expression stage falls
        back to the draft, and a turn without a draft never reaches here.
        """

        limits = self._limits
        wall_left = limits.turn_wall_seconds - (self._clock() - started)
        decision = decide(
            plan=sdrpe,
            turn_mode=turn.mode,
            content_action="final",
            protocol_version=self._protocol,
            max_model_requests=limits.max_model_requests,
            requests_used=requests_used,
            wall_left=wall_left,
        )
        handoff: dict[str, Any] | None = None
        if decision.run:
            try:
                handoff = build_content_handoff(
                    draft=draft,
                    already_public_refs=published_refs(
                        [item.ref for item in published]
                    ),
                )
            except InvalidInput:
                decision = skip(SKIP_DRAFT_TOO_LARGE)
        if not decision.run:
            # ``off`` never creates a stage row: the honest record of "this
            # build does not run a second stage" is the absence of the stage.
            return self._publish_content_final(
                namespace=namespace,
                turn=turn,
                stage=stage,
                interaction_id=interaction_id,
                context=context,
                text=draft,
                parts=draft_parts,
                retention=retention,
                attempt_ref=evidence.attempt_ref,
                skip_reason=(
                    decision.reason
                    if (
                        sdrpe.effective_mode == "on"
                        or sdrpe.requested_mode == "auto"
                    )
                    else None
                ),
                commentary_omitted=commentary_omitted,
                extra_sources=(commentary_ref,) if commentary_ref else (),
            )

        self._store.finish_stage(
            namespace=namespace,
            turn_id=turn.turn_id,
            stage_id=stage.stage_id,
            status="succeeded",
            on_event=self._event_sink,
        )
        pending = self._pending_cognition_changes()
        material = self._expression_material(turn)
        task = build_expression_task(
            pending_cognition_changes=pending, bundle=bundle
        )
        try:
            expression_context = self._contexts.build_consult_context(
                namespace=namespace,
                access=access,
                input_refs=[input_ref],
                adapter_kind=self._model.adapter_kind,
                profile_ref=RecordRef.from_value(profile_ref, field="profile_ref"),
                history=history,
                published_messages=published,
                tool_results=(),
                tool_definitions=(),
                renderer_version=protocol_renderer_version(self._protocol),
                persona=persona,
                expression_material=material,
                content_handoff=handoff,
                expression_task=task,
                policy_record_ref=self._policy_record_ref(bundle),
            )
        except ContextTooLarge:
            return self._publish_content_final(
                namespace=namespace,
                turn=turn,
                stage=stage,
                interaction_id=interaction_id,
                context=context,
                text=draft,
                parts=draft_parts,
                retention=retention,
                attempt_ref=evidence.attempt_ref,
                skip_reason=SKIP_CONTEXT_TOO_LARGE,
                finish_stage=False,
                commentary_omitted=commentary_omitted,
                extra_sources=(commentary_ref,) if commentary_ref else (),
            )
        expression_stage = self._store.begin_expression_stage(
            namespace=namespace,
            turn_id=turn.turn_id,
            on_event=self._event_sink,
        )
        state.stage_id = expression_stage.stage_id
        state.context_id = expression_context.context_id
        self._store.bind_stage_context(
            namespace=namespace,
            stage_id=expression_stage.stage_id,
            context_id=expression_context.context_id,
            ordinal=1,
            purpose="expression",
        )
        self._require_not_cancelled(
            turn.turn_id, namespace, expression_stage, interaction_id
        )
        try:
            output, expression_evidence, used, metrics = self._generate_expression(
                expression_context,
                limits.max_model_requests - requests_used,
                namespace=namespace,
                turn_id=turn.turn_id,
                draft=draft,
                material=material,
                pending=pending,
                published=published,
                persona=persona,
            )
        except GenerationFailure as failure:
            label = f"{failure.phase}:{failure.detail}"[:128]
            self._store.finish_stage(
                namespace=namespace,
                turn_id=turn.turn_id,
                stage_id=expression_stage.stage_id,
                status="failed",
                failure_reason=label,
                on_event=self._event_sink,
            )
            return self._publish_content_final(
                namespace=namespace,
                turn=turn,
                stage=stage,
                interaction_id=interaction_id,
                context=context,
                text=draft,
                parts=draft_parts,
                retention=retention,
                attempt_ref=evidence.attempt_ref,
                finish_stage=False,
                commentary_omitted=commentary_omitted,
                extra_sources=(commentary_ref,) if commentary_ref else (),
            )
        self._require_not_cancelled(
            turn.turn_id,
            namespace,
            expression_stage,
            interaction_id,
            attempt_ref=expression_evidence.attempt_ref,
        )
        final_parts = output.bubbles()
        final_text = join_bubbles(final_parts)
        time_anchor_flags: list[dict[str, Any]] = []
        try:
            document = json.loads(expression_context.rendered_input)
            user_text = "".join(
                str(item.get("content") or "") for item in (document.get("inputs") or ())
            )
            history_texts = [
                str(item.get("text") or "") for item in (document.get("history") or ())
            ]
            time_anchor_flags = find_suspect_day_anchors(
                user_text, history_texts, final_text
            )
        except Exception:  # noqa: BLE001 - observation must never break a turn
            time_anchor_flags = []
        self._store.record_turn_event(
            namespace=namespace,
            turn_id=turn.turn_id,
            kind="expression_checked",
            payload={
                "stage": "expression",
                "metrics": metrics,
                "requests_used": int(used),
                "policy_version": bundle.version,
                "policy_digest": bundle.digest,
                "shared_sha256": bundle.shared_sha256,
                "content_sha256": bundle.content_sha256,
                "host_expression_sha256": bundle.host_expression_sha256,
                "persona_policy_state": (
                    bundle.persona.state if bundle.persona else "absent"
                ),
                "persona_policy_review_state": (
                    bundle.persona.review_state if bundle.persona else None
                ),
                "persona_policy_ref": (
                    bundle.persona.ref
                    if bundle.persona and bundle.persona.state == "present"
                    else None
                ),
                "persona_policy_sha256": bundle.persona_sha256,
                "persona_profile_id": (
                    bundle.persona.profile_id if bundle.persona else None
                ),
                "persona_profile_version": (
                    bundle.persona.profile_version if bundle.persona else None
                ),
                "expression_sha256": bundle.expression_sha256,
                # Observation only: day references with no source in the input
                # or the recent history. Never a gate, never a rewrite.
                "time_anchor_flags": time_anchor_flags,
            },
            on_event=self._event_sink,
        )
        sources = _merge_sources(
            artifact_sources(expression_context), artifact_sources(context)
        )
        if commentary_ref:
            sources = _merge_sources(sources, (commentary_ref,))
        final = self._store.publish_assistant_message(
            namespace=namespace,
            turn_id=turn.turn_id,
            stage_id=expression_stage.stage_id,
            interaction_id=interaction_id,
            context_id=expression_context.context_id,
            text=final_text,
            parts=final_parts,
            retention=retention,
            sources=sources,
            attempt_ref=expression_evidence.attempt_ref,
            kind="final",
            commentary_omitted=commentary_omitted,
            on_event=self._event_sink,
        )
        return TurnExecution(
            final, interaction_id, expression_context.context_id
        )

    def _publish_content_final(
        self,
        *,
        namespace: str,
        turn: TurnRow,
        stage: Any,
        interaction_id: str,
        context: Any,
        text: str,
        retention: str,
        attempt_ref: Mapping[str, Any] | None,
        parts: Sequence[str] | None = None,
        skip_reason: str | None = None,
        finish_stage: bool = True,
        commentary_omitted: str | None = None,
        extra_sources: Sequence[Mapping[str, Any]] = (),
    ) -> TurnExecution:
        """Publish the content-stage draft (the off path and the fallback)."""

        if skip_reason is not None:
            self._store.skip_expression_stage(
                namespace=namespace,
                turn_id=turn.turn_id,
                reason=skip_reason,
                on_event=self._event_sink,
            )
        sources = _merge_sources(artifact_sources(context), extra_sources)
        final = self._store.publish_assistant_message(
            namespace=namespace,
            turn_id=turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            text=text,
            parts=parts,
            retention=retention,
            sources=sources,
            attempt_ref=attempt_ref,
            kind="final",
            commentary_omitted=commentary_omitted,
            finish_stage=finish_stage,
            on_event=self._event_sink,
        )
        return TurnExecution(final, interaction_id, context.context_id)

    def _pending_cognition_changes(self) -> int:
        """The host's pending-cognition count for the expression task.

        A read failure is reported as one pending item: the expression stage is
        then not allowed to claim something was remembered.  Never fails a turn.
        """

        if self._cognition_status_factory is None:
            return 0
        try:
            value = self._cognition_status_factory()
        except (CorruptStore, DbBusy):
            raise
        except Exception:  # noqa: BLE001 - a status read is not the turn
            return 1
        if type(value) is not int or value < 0:
            return 1
        return value

    def _generate_expression(
        self,
        context: Any,
        remaining: int,
        *,
        namespace: str,
        turn_id: str,
        draft: str,
        material: Any,
        pending: int,
        published: Sequence[PublishedMessageMaterial],
        persona: dict[str, Any] | None,
    ) -> tuple[Any, Any, int, dict[str, Any]]:
        """One expression generation plus at most one policy repair."""

        allowed = allowed_references(context)
        attempts = max(0, min(2, remaining))
        if attempts < 1:
            raise GenerationFailure("budget", "expression_retry_exhausted")
        material_document: dict[str, Any] | None = None
        if material is not None:
            try:
                material_document = json.loads(material.body)
            except (ValueError, TypeError):
                material_document = None
        examples = tuple((persona or {}).get("examples") or ())
        feedback: str | None = None
        reason: str | None = None
        labels: tuple[str, ...] = ()
        used = 0
        for _attempt in range(attempts):
            before = None if feedback is None else self._attempt_ids(context)
            try:
                result = generate_once(
                    self._store,
                    self._model,
                    context=context,
                    allowed_references=allowed,
                    feedback=feedback,
                    protocol_version=self._protocol,
                )
            except GenerationFailure:
                if before is not None:
                    self._record_repair(
                        namespace, turn_id, "expression", context, before, reason
                    )
                raise
            used += 1
            if before is not None:
                self._record_repair(
                    namespace,
                    turn_id,
                    "expression",
                    context,
                    before,
                    reason,
                    evidence_ref=result.evidence.attempt_ref,
                )
            if result.parse_error is not None:
                feedback = repair_feedback(
                    result.parse_error, protocol_version=self._protocol
                )
                reason = "format"
                continue
            output = result.output
            expression_parts = () if output is None else output.bubbles()
            if output is None or output.action != "final" or not expression_parts:
                feedback = (
                    "本阶段没有工具，也不能再提出澄清问题：只输出 "
                    '{"action":"final","reply":"..."} 或 '
                    '{"action":"final","messages":["...","..."]}。'
                )
                reason = "feedback_retry"
                continue
            verdict = check_expression(
                draft,
                join_bubbles(expression_parts),
                published_commentary=tuple(item.text for item in published),
                expression_material=material_document,
                pending_cognition_changes=pending,
                examples=examples,
            )
            if verdict.ok:
                return output, result.evidence, used, dict(verdict.metrics)
            labels = verdict.failures
            feedback = _expression_feedback(verdict.failures)
            reason = "feedback_retry"
        raise GenerationFailure("policy", "+".join(labels) or "expression_rejected")

    def _generate(
        self,
        context: Any,
        remaining: int,
        *,
        namespace: str,
        turn_id: str,
        tools_executed: int = 0,
    ) -> tuple[Any, Any, int]:
        allowed = allowed_references(context)
        feedback: str | None = None
        reason: str | None = None
        used = 0
        last_result: Any = None
        attempts = self._repair_attempts(remaining)
        for _attempt in range(attempts):
            before = None if feedback is None else self._attempt_ids(context)
            try:
                result = generate_once(
                    self._store,
                    self._model,
                    context=context,
                    allowed_references=allowed,
                    feedback=feedback,
                    protocol_version=self._protocol,
                )
            except GenerationFailure:
                if before is not None:
                    self._record_repair(
                        namespace, turn_id, "content", context, before, reason
                    )
                raise
            used += 1
            if before is not None:
                # The request carried repair feedback: mark the attempt that
                # settled this logical request, not every HTTP try inside it.
                self._record_repair(
                    namespace,
                    turn_id,
                    "content",
                    context,
                    before,
                    reason,
                    evidence_ref=result.evidence.attempt_ref,
                )
            if result.parse_error is None:
                assert result.output is not None
                return result.output, result.evidence, used
            last_result = result
            classification = classify_parse_rejection(result.raw)
            if self._protocol == COLLAB_V2_PROTOCOL_VERSION:
                # A normal reply that missed the envelope is not a tool call.
                # Publish the honest class: only a visibly tool-shaped output
                # is ``tool_rejected``; ordinary prose is a reply-format
                # rejection.  Neither creates a step or a ``tool_started``.
                self._record_parse_rejection(
                    namespace=namespace,
                    turn_id=turn_id,
                    classification=classification,
                )
            if _declared_action(result.raw) == "propose":
                raise GenerationFailure("format", "propose")
            # The classifier is diagnostic only: unless the controlled plain
            # text fallback below admits the last answer, the turn label stays
            # ``format:format_exhausted`` and no model text is persisted.
            LOGGER.info(
                "collab format repair stage=content reason=%s class=%s",
                getattr(result.parse_error, "reason", "unknown"),
                classification,
            )
            if self._tools_enabled() and classification == "tool":
                # The tool-shaped branch gets its own complete, fixed feedback:
                # the rejected response's markup was not executed.  If earlier
                # calls in this turn really ran, say so instead of claiming
                # globally that no tool ran, and forbid duplicate execution.
                feedback = (
                    TOOL_FORMAT_REPAIR_FEEDBACK
                    if tools_executed == 0
                    else TOOL_FORMAT_REPAIR_FEEDBACK_AFTER_EXECUTION
                )
            else:
                feedback = repair_feedback(
                    result.parse_error, protocol_version=self._protocol
                )
            reason = "format"
        if last_result is not None:
            fallback = plain_text_final(
                last_result.raw,
                finish_reason=last_result.finish_reason,
                protocol_version=self._protocol,
            )
            if fallback is not None:
                self._record_plain_text_fallback(
                    namespace=namespace,
                    turn_id=turn_id,
                    result=last_result,
                )
                LOGGER.info(
                    "collab plain-text final fallback stage=content chars=%d",
                    len(fallback.text),
                )
                return (
                    CollabOutput(
                        action="final",
                        reply=fallback.text,
                        references=(),
                        brief_reason=None,
                    ),
                    last_result.evidence,
                    used,
                )
        raise GenerationFailure("format", "format_exhausted")

    def _repair_attempts(self, remaining: int) -> int:
        """Requests this generation may spend inside the remaining budget.

        The default ``max_generations=2`` is one initial request plus at most
        one format repair.  A repair is requested only when the budget has room
        for both the original request and the feedback request; the caller
        checks ``requests < content_cap`` before calling here.
        """

        if remaining < 1:
            return 0
        if remaining < 2:
            return 1
        return min(self._max_generations, remaining)

    def _attempt_ids(self, context: Any) -> set[str]:
        """The attempts this Context already owns, for repair bookkeeping."""

        return {
            attempt.attempt_id
            for attempt in self._store.model_attempts_for_run(context.run_id)
            if attempt.context_id == context.context_id
        }

    def _record_parse_rejection(
        self,
        *,
        namespace: str,
        turn_id: str,
        classification: str,
    ) -> None:
        """Publish one parse rejection without inventing a tool step.

        A visibly tool-shaped output is the only one that becomes
        ``tool_rejected``; ordinary prose that missed the action envelope is a
        reply-format failure and is published as ``reply_format_rejected``.
        Only the stable class and the stage are public: no provider text, tool
        name (none was parsed), arguments, body or credential can reach the
        event.
        """

        if classification == "tool":
            kind = "tool_rejected"
            reason = "parse"
        else:
            kind = "reply_format_rejected"
            reason = "format"
        self._store.record_turn_event(
            namespace=namespace,
            turn_id=turn_id,
            kind=kind,
            payload={"stage": "content", "reason": reason},
            on_event=self._event_sink,
        )

    def _record_plain_text_fallback(
        self,
        *,
        namespace: str,
        turn_id: str,
        result: Any,
    ) -> None:
        """Persist the admitted plain-text fallback without claiming protocol success.

        The raw provider response and its finish reason stay in the settled
        model attempt; this event only links that attempt and names the
        controlled fallback.  It never copies the reply text into the event.
        """

        self._store.record_turn_event(
            namespace=namespace,
            turn_id=turn_id,
            kind="plain_text_final_fallback",
            payload={
                "stage": "content",
                "reason": "plain_text_final",
                "finish_reason": result.finish_reason,
                "attempt_ref": dict(result.evidence.attempt_ref),
            },
            on_event=self._event_sink,
        )

    def _record_repair(
        self,
        namespace: str,
        turn_id: str,
        stage_kind: str,
        context: Any,
        before: set[str],
        reason: str | None,
        *,
        evidence_ref: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist exactly one marker for one logical repair request.

        One logical repair may contain several HTTP attempts when the adapter
        retries the same body after a transport failure.  Only the attempt that
        carried the feedback *and settled this logical request* is a repair:
        the evidence ref when the call returned, otherwise the last new
        attempt.  A transport retry inside the repair therefore stays a
        transport retry, and an attempt-less marker (send refused before any
        reservation) is recorded as unbound instead of guessing.

        The payload carries no request body, no header, no authorization id and
        no request hash: only the stage, the fixed reason and the attempt
        identity of the settled repair.
        """

        if reason not in ("format", "feedback_retry"):
            return
        fresh = [
            attempt
            for attempt in self._store.model_attempts_for_run(context.run_id)
            if attempt.context_id == context.context_id
            and attempt.attempt_id not in before
        ]
        fresh.sort(key=lambda attempt: (attempt.attempt_no, attempt.attempt_id))
        target = None
        if isinstance(evidence_ref, Mapping):
            wanted = evidence_ref.get("attempt_id")
            target = next(
                (attempt for attempt in fresh if attempt.attempt_id == wanted),
                None,
            )
        if target is None and fresh:
            target = fresh[-1]
        payload: dict[str, Any] = {"stage": stage_kind, "reason": reason}
        if target is not None:
            payload["attempt_id"] = target.attempt_id
            payload["attempt_no"] = target.attempt_no
        self._store.record_turn_event(
            namespace=namespace,
            turn_id=turn_id,
            kind="repair_attempt",
            payload=payload,
            on_event=self._event_sink,
        )

    def _publish_commentary(
        self,
        *,
        namespace: str,
        turn: TurnRow,
        stage: Any,
        interaction_id: str,
        context: Any,
        text: str,
        retention: str,
        attempt_ref: Mapping[str, Any] | None,
        published: list[PublishedMessageMaterial],
        commentary_source: str = "model",
    ) -> dict[str, Any]:
        """Publish one public short sentence and remember its artifact ref."""

        message = self._store.publish_commentary(
            namespace=namespace,
            turn_id=turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            text=text,
            retention=retention,
            sources=artifact_sources(context),
            attempt_ref=attempt_ref,
            live_text=text,
            commentary_source=commentary_source,
            on_event=self._event_sink,
        )
        ref = _message_artifact_ref(namespace, message)
        published.append(
            PublishedMessageMaterial(ref=ref, kind="commentary", text=text)
        )
        return ref

    def _commentary_text(
        self,
        raw: str | None,
        *,
        mode: str,
        count: int,
        seen: set[str],
        limit: int | None = None,
    ) -> tuple[str | None, str | None]:
        """Apply the host commentary policy; an omission is recorded, not retried."""

        if raw is None:
            return None, None
        if mode != "chat":
            return None, "work_mode"
        effective_limit = (
            self._limits.commentary_max_per_turn if limit is None else limit
        )
        if count >= effective_limit:
            return None, "count_limit"
        text = raw.strip()
        if not text:
            return None, "empty"
        if len(text) > self._limits.commentary_max_chars:
            return None, "too_long"
        normalized = " ".join(text.split())
        if normalized in seen:
            return None, "duplicate"
        seen.add(normalized)
        return text, None

    def _host_fallback_commentary(
        self, tool_name: str, *, avoid: set[str] | None = None
    ) -> str:
        """A deterministic short line spoken immediately before a real tool step.

        It is derived from the tool's purpose, never from a result and never from
        a provider response.  The caller records ``commentary_source`` so this
        line is not presented as model output.  When ``avoid`` already contains
        the first line (the model repeated identical text and the duplicate
        policy suppressed the second copy), a different host line is used so the
        second tool step still has a visible short sentence.
        """

        text = TOOL_COMMENTARY_FALLBACKS.get(
            tool_name, _TOOL_COMMENTARY_NEUTRAL
        )
        if avoid and text in avoid:
            text = TOOL_COMMENTARY_FALLBACKS_ALT.get(
                tool_name, _TOOL_COMMENTARY_NEUTRAL_ALT
            )
        if avoid and text in avoid:
            text = _TOOL_COMMENTARY_NEUTRAL_ALT
        if avoid and text in avoid:
            text = "我继续处理一下"
        limit = self._limits.commentary_max_chars
        if len(text) > limit:
            text = text[:limit]
        return text

    def _require_not_cancelled(
        self,
        turn_id: str,
        namespace: str,
        stage: Any | None,
        interaction_id: str,
        *,
        attempt_ref: Mapping[str, Any] | None = None,
    ) -> None:
        if not self._cancel_requested(turn_id):
            return
        try:
            self._store.record_turn_event(
                namespace=namespace,
                turn_id=turn_id,
                kind="cancel_requested",
                payload={},
            )
        except (RecordNotFound, TurnStateConflict):
            pass
        if attempt_ref is not None:
            try:
                self._store.discard_late_result(
                    namespace=namespace,
                    turn_id=turn_id,
                    attempt_ref=attempt_ref,
                    on_event=self._event_sink,
                )
            except (RecordNotFound, TurnStateConflict):
                pass
        try:
            self._store.cancel_turn(
                namespace=namespace,
                turn_id=turn_id,
                stage_id=None if stage is None else stage.stage_id,
                interaction_id=interaction_id,
                on_event=self._event_sink,
            )
        except (RecordNotFound, TurnStateConflict):
            pass
        raise _Cancelled()

    # ---------------------------------------------------------------- failure

    def _fail(
        self,
        namespace: str,
        turn: TurnRow,
        reason: str,
        state: _ExecutionState,
    ) -> TurnExecution:
        return TurnExecution(
            self._store.fail_turn(
                namespace=namespace,
                turn_id=turn.turn_id,
                reason=reason,
                stage_id=state.stage_id,
                interaction_id=state.interaction_id,
                on_event=self._event_sink,
            ),
            state.interaction_id,
            state.context_id,
        )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _expression_feedback(failures: Sequence[str]) -> str:
    """Bounded repair text for a rejected expression output (never echoes it)."""

    labels = ", ".join(failures) if failures else "expression_rejected"
    return (
        "上一版表达没有通过宿主检查（"
        + labels
        + "）。这不是措辞问题：请重新判断这段回复该怎么说，"
        "可以整句、整段、整条删除，可以改变消息条数，也可以重写整篇。"
        "必须保留草稿中的事实、数字、实体、否定、条件、不确定程度、承诺、拒绝边界与真实操作状态；"
        "不要新增事实、数字、承诺、工具结果或操作状态，也不要提高确定程度。"
        "只输出 {\"action\":\"final\",\"reply\":\"…\"} 或 "
        "{\"action\":\"final\",\"messages\":[\"…\",\"…\"]}。"
    )


def _merge_sources(
    left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Union two source lists by canonical value, keeping the first order."""

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for reference in list(left) + list(right):
        value = dict(reference)
        key = canonical_json_text(value)
        if key in seen:
            continue
        seen.add(key)
        merged.append(value)
    return merged


def _declared_action(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    action = document.get("action")
    return action if isinstance(action, str) else None


def _arguments_digest(arguments: str) -> str:
    return hashlib.sha256((arguments or "").encode("utf-8")).hexdigest()


#: Stable public rejection classes.  ``parse`` is emitted without a step;
#: ``unknown_tool``/``arguments_invalid`` are pre-execution refusals;
#: ``not_authorized``/``execution_failed`` settle a started step and are carried
#: on ``tool_finished.reason``.  The mapping never includes a message or args.
_TOOL_PUBLIC_REASONS = {
    "tool_unknown": "unknown_tool",
    "tool_arguments_invalid": "arguments_invalid",
    "tool_not_authorized": "not_authorized",
}


def _tool_public_reason(code: str) -> str:
    return _TOOL_PUBLIC_REASONS.get(code, "execution_failed")


def _message_artifact_ref(namespace: str, message: MessageRow) -> dict[str, Any]:
    if message.artifact_id is None or message.artifact_version is None:
        raise CorruptStore("commentary message has no artifact")
    return {
        "ref_type": "collab_artifact",
        "namespace": namespace,
        "artifact_id": message.artifact_id,
        "artifact_version": message.artifact_version,
    }


def _tool_material(
    step: StepRow,
    tool: str,
    status: str,
    body: str,
) -> ToolResultMaterial:
    if step.result_artifact_id is None or step.result_artifact_version is None:
        raise CorruptStore("a finished step with a body has no artifact")
    return ToolResultMaterial(
        ref={
            "ref_type": "collab_artifact",
            "namespace": step.namespace,
            "artifact_id": step.result_artifact_id,
            "artifact_version": step.result_artifact_version,
        },
        step_no=step.step_no,
        tool=tool,
        status=status,
        body=body,
        excerpt=_excerpt(body, 1024),
        truncated=len(body) > 1024,
    )


def _excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _select_tool_results(
    results: Sequence[ToolResultMaterial],
    total_bytes: int,
    excerpt_bytes: int,
) -> tuple[ToolResultMaterial, ...]:
    """Keep the newest excerpts inside the budget; older ones keep their ref.

    Every ref stays linked (the Context source closure must not lose a result),
    but an over-budget older result is rendered as a small identity JSON that
    says it was omitted for size.
    """

    selected: list[ToolResultMaterial] = []
    used = 0
    for material in reversed(results):
        text = _excerpt(material.body, excerpt_bytes)
        if used + len(text.encode("utf-8")) <= total_bytes:
            selected.append(
                ToolResultMaterial(
                    ref=material.ref,
                    step_no=material.step_no,
                    tool=material.tool,
                    status=material.status,
                    body=material.body,
                    excerpt=text,
                    truncated=len(text) < len(material.body),
                )
            )
            used += len(text.encode("utf-8"))
        else:
            identity = canonical_json_text(
                {
                    "omitted": "tool_result_byte_limit",
                    "step_no": material.step_no,
                    "tool": material.tool,
                    "status": material.status,
                }
            )
            selected.append(
                ToolResultMaterial(
                    ref=material.ref,
                    step_no=material.step_no,
                    tool=material.tool,
                    status=material.status,
                    body=material.body,
                    excerpt=identity,
                    truncated=True,
                )
            )
    selected.reverse()
    return tuple(selected)


def _refusal_live(outcome: ToolOutcome) -> dict[str, Any]:
    return {
        "result_excerpt": _excerpt(
            canonical_json_text(
                {
                    "ok": False,
                    "code": outcome.code,
                    "message": str(outcome.payload.get("message", "")),
                }
            ),
            1024,
        )
    }


def _outcome_live(outcome: ToolOutcome) -> dict[str, Any]:
    payload: dict[str, Any] = dict(outcome.payload)
    if not outcome.ok:
        payload = {"code": outcome.code, **payload}
    return {
        "result_excerpt": _excerpt(canonical_json_text(payload), 1024)
    }


__all__ = [
    "ConversationOrchestrator",
    "PersonaNotReady",
    "TurnExecution",
    "TurnLimits",
    "resolve_turn_profile",
]
