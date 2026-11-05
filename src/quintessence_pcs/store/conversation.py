"""C13/RG02: persistent conversations, turns, messages and Context history.

The store owns the invariants; the orchestrator owns the order of operations.
Nothing here calls a model, writes a Belief or fabricates a reply.

The tables are defined by ``0017_conversation_turns.sql``:

* ``collab_conversations`` / ``collab_turns`` — durable identity and state;
* ``collab_turn_stages`` / ``collab_stage_contexts`` — stage to Context chain;
* ``collab_turn_messages`` — the public transcript only (no tool rows);
* ``collab_turn_events`` — append-only process events with a per-turn ``seq``;
* ``collab_context_history`` — the exact historical messages a Context
  rendered.  These rows are **not** material sources: they never enter
  ``context_sources``/``collab_source_links`` and a reply artifact is not
  required to cite them.  They exist so a deleted historical message makes the
  Context stale and so deletion cleanup can find the rendered copy.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..foundation import identifiers
from ..foundation.errors import (
    ContextStale,
    CorruptStore,
    InvalidInput,
    RecordNotFound,
    Redacted,
)
from ..foundation.jsonutil import (
    canonical_json_text,
    sha256_hex,
    strict_json_loads,
)
from ..foundation.timeutil import utc_now_rfc3339
from ..reply_bubbles import (
    decode_reply_body,
    encode_reply_body,
    join_bubbles,
    normalise_bubble_parts,
)
from .collaboration import COLLAB_ARTIFACT, COLLAB_INPUT
from .errors import ClientRequestConflict, TurnStateConflict
from .experience import TERMINAL_TURN_STATES

CONVERSATION_MODES = ("chat", "work")
TURN_STATES = (
    "accepted",
    "running",
    "waiting",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
)
TURN_TERMINAL_STATES = ("completed", "failed", "cancelled", "interrupted")
MESSAGE_KINDS = ("user", "commentary", "final", "wait_question")
PUBLISHED = "published"

DEFAULT_CONVERSATION_LIMIT = 20
MAX_CONVERSATION_LIMIT = 50
DEFAULT_MESSAGE_LIMIT = 50
MAX_MESSAGE_LIMIT = 200
DEFAULT_TURN_LIMIT = 50
MAX_TURN_LIMIT = 50

MAX_CLIENT_REQUEST_ID = 128
MAX_EVENT_PAYLOAD = 16384
MAX_HISTORY_TURNS = 64
MAX_HISTORY_BYTES = 1 << 20
HISTORY_OVERHEAD_BYTES = 32
MAX_STEP_DETAIL_CHARS = 2048

_USER_STATEMENT = "user_statement"


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ConversationRow:
    conversation_id: str
    namespace: str
    host_instance: str
    mode: str
    title: str | None
    next_turn_no: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TurnRow:
    turn_id: str
    namespace: str
    conversation_id: str
    turn_no: int
    client_request_id: str
    client_request_digest: str
    input_ref: dict[str, Any]
    state: str
    profile_ref: dict[str, Any]
    mode: str
    strategy: dict[str, Any]
    last_event_seq: int
    final_message_ref: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class TurnCreation:
    reused: bool
    turn: TurnRow
    input_ref: dict[str, Any]
    message_id: str


@dataclass(frozen=True)
class StageRow:
    stage_id: str
    namespace: str
    turn_id: str
    kind: str
    status: str
    policy_version: str
    skip_reason: str | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class MessageRow:
    message_id: str
    namespace: str
    turn_id: str
    seq: int
    kind: str
    input_ref: dict[str, Any] | None
    artifact_id: str | None
    artifact_version: int | None
    attempt_ref: dict[str, Any] | None
    status: str
    published_at: str | None
    created_at: str


@dataclass(frozen=True)
class MessageView:
    message_id: str
    turn_id: str
    seq: int
    kind: str
    text: str | None
    redacted: bool
    created_at: str
    #: RG08: the ordered explicit bubbles of an assistant final.  Empty for a
    #: user message, a redacted view or a legacy single reply; ``text`` is the
    #: joined compatibility view of the same parts.
    messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class TurnDetail:
    turn: TurnRow
    failure_reason: str | None


@dataclass(frozen=True)
class MessagePage:
    messages: tuple[MessageView, ...]
    next_before: tuple[int, int] | None


@dataclass(frozen=True)
class ConversationSummary:
    conversation: ConversationRow
    turn_count: int
    last_turn_state: str | None


@dataclass(frozen=True)
class ConversationPage:
    conversations: tuple[ConversationSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class TurnResultView:
    turn: TurnRow
    final: MessageView | None
    wait_question: MessageView | None
    failure_reason: str | None


@dataclass(frozen=True)
class HistoryMessage:
    role: str
    text: str
    ref_kind: str
    ref_id: str
    ref_version: int
    turn_no: int
    seq: int
    #: The stored message timestamp (UTC ISO), or ``None`` when the row has no
    #: usable value.  RG08 time-anchor: never inferred, never back-filled.
    created_at: str | None = None


@dataclass(frozen=True)
class HistoryBundle:
    messages: tuple[HistoryMessage, ...]
    omissions: tuple[dict[str, Any], ...]
    stats: dict[str, Any]


@dataclass(frozen=True)
class TurnEventRow:
    namespace: str
    turn_id: str
    seq: int
    event_id: str
    kind: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class TurnEventPage:
    events: tuple[TurnEventRow, ...]
    next_seq: int
    terminal: bool


@dataclass(frozen=True)
class StepRow:
    step_id: str
    namespace: str
    turn_id: str
    stage_id: str
    step_no: int
    tool_name: str
    arguments_digest: str | None
    result_artifact_id: str | None
    result_artifact_version: int | None
    result_digest: str | None
    operation_key: str
    status: str
    started_at: str | None
    finished_at: str | None
    error_label: str | None


@dataclass(frozen=True)
class StepView:
    step: StepRow
    arguments: dict[str, Any] | None
    result_available: bool
    result_excerpt: str | None
    result_truncated: bool


@dataclass(frozen=True)
class StepPage:
    steps: tuple[StepView, ...]
    next_after_step_no: int | None


# --------------------------------------------------------------------------
# mixin
# --------------------------------------------------------------------------


class ConversationMixin:
    """Persistent conversation/turn storage on the single worker connection."""

    # ------------------------------------------------------------ conversations

    def create_conversation(
        self,
        *,
        namespace: str,
        host_instance: str,
        mode: str = "chat",
        conversation_id: str | None = None,
        title: str | None = None,
        created_at: str | None = None,
    ) -> ConversationRow:
        ns = identifiers.require_namespace(namespace)
        host = _require_text(host_instance, "host_instance", 256)
        if mode not in CONVERSATION_MODES:
            raise InvalidInput("mode must be one of " + ", ".join(CONVERSATION_MODES))
        cid = str(uuid.uuid4()) if conversation_id is None else _require_uuid(
            conversation_id, "conversation_id"
        )
        label = None if title is None else _require_text(title, "title", 200)
        created = utc_now_rfc3339() if created_at is None else _require_timestamp(
            created_at, "created_at"
        )
        with self.transaction():
            try:
                self._require_connection().execute(
                    "INSERT INTO collab_conversations (conversation_id, namespace, "
                    "host_instance, mode, title, next_turn_no, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (cid, ns, host, mode, label, created, created),
                )
            except Exception as exc:  # sqlite3.IntegrityError
                raise InvalidInput(f"conversation {cid} already exists") from exc
            return self._conversation_locked(ns, host, cid)

    def get_conversation(
        self, *, namespace: str, host_instance: str, conversation_id: str
    ) -> ConversationRow:
        ns = identifiers.require_namespace(namespace)
        host = _require_text(host_instance, "host_instance", 256)
        cid = _require_uuid(conversation_id, "conversation_id")
        return self._conversation_locked(ns, host, cid)

    def list_conversations(
        self,
        *,
        namespace: str,
        host_instance: str,
        limit: int = DEFAULT_CONVERSATION_LIMIT,
        cursor: str | None = None,
    ) -> ConversationPage:
        ns = identifiers.require_namespace(namespace)
        host = _require_text(host_instance, "host_instance", 256)
        if type(limit) is not int or not 1 <= limit <= MAX_CONVERSATION_LIMIT:
            raise InvalidInput(
                f"limit must be an integer between 1 and {MAX_CONVERSATION_LIMIT}"
            )
        parameters: list[Any] = [ns, host]
        where = "WHERE namespace = ? AND host_instance = ?"
        if cursor is not None:
            updated_at, last_id = _decode_cursor(cursor)
            where += (
                " AND (updated_at < ? OR (updated_at = ? AND conversation_id < ?))"
            )
            parameters.extend([updated_at, updated_at, last_id])
        rows = self._require_connection().execute(
            "SELECT conversation_id, namespace, host_instance, mode, title, "
            "next_turn_no, created_at, updated_at FROM collab_conversations "
            + where
            + " ORDER BY updated_at DESC, conversation_id DESC LIMIT ?",
            (*parameters, limit + 1),
        ).fetchall()
        summaries: list[ConversationSummary] = []
        next_cursor: str | None = None
        for index, row in enumerate(rows):
            if index == limit:
                previous = summaries[-1].conversation
                next_cursor = _encode_cursor(previous.updated_at, previous.conversation_id)
                break
            conversation = _conversation_from_row(row)
            turn_count = int(
                self._require_connection().execute(
                    "SELECT COUNT(*) FROM collab_turns WHERE namespace = ? "
                    "AND conversation_id = ?",
                    (ns, conversation.conversation_id),
                ).fetchone()[0]
            )
            last = self._require_connection().execute(
                "SELECT state FROM collab_turns WHERE namespace = ? "
                "AND conversation_id = ? ORDER BY turn_no DESC LIMIT 1",
                (ns, conversation.conversation_id),
            ).fetchone()
            summaries.append(
                ConversationSummary(
                    conversation=conversation,
                    turn_count=turn_count,
                    last_turn_state=None if last is None else str(last["state"]),
                )
            )
        return ConversationPage(
            conversations=tuple(summaries), next_cursor=next_cursor
        )

    # ------------------------------------------------------------------ turns

    def register_turn(
        self,
        *,
        namespace: str,
        host_instance: str,
        conversation_id: str,
        client_request_id: str,
        text: str,
        privacy_class: str,
        retention: str,
        profile_ref: Mapping[str, Any],
        strategy: Mapping[str, Any],
        turn_id: str | None = None,
        on_event: Callable[[TurnEventRow, Mapping[str, Any] | None], None] | None = None,
    ) -> TurnCreation:
        """Create one accepted turn and its user message, or replay an existing one.

        The ``(conversation, client_request_id)`` unique key is the durable
        idempotency proof.  ``profile_ref`` is the fixed T0 value; this method
        never reads the active-profile pointer.
        """

        ns = identifiers.require_namespace(namespace)
        host = _require_text(host_instance, "host_instance", 256)
        cid = _require_uuid(conversation_id, "conversation_id")
        request_id = _require_text(
            client_request_id, "client_request_id", MAX_CLIENT_REQUEST_ID
        )
        if any(char in request_id for char in "\x00\r\n\t"):
            raise InvalidInput("client_request_id must not contain control characters")
        body = _require_text(text, "text", 262144)
        privacy = identifiers.require_privacy_class(privacy_class)
        policy = _require_retention(retention)
        pinned = _require_profile_ref(profile_ref, ns)
        snapshot = _require_mapping(strategy, "strategy")
        strategy_text = canonical_json_text(snapshot)
        if len(strategy_text) > MAX_EVENT_PAYLOAD:
            raise InvalidInput("strategy snapshot is too large")
        tid = str(uuid.uuid4()) if turn_id is None else _require_uuid(
            turn_id, "turn_id"
        )
        pending: list[tuple[TurnEventRow, Mapping[str, Any] | None]] = []

        with self.transaction():
            conversation = self._conversation_locked(ns, host, cid)
            digest = _turn_digest(conversation.conversation_id, body, conversation.mode)
            existing = self._require_connection().execute(
                "SELECT * FROM collab_turns WHERE namespace = ? "
                "AND conversation_id = ? AND client_request_id = ?",
                (ns, cid, request_id),
            ).fetchone()
            if existing is not None:
                if existing["client_request_digest"] != digest:
                    raise ClientRequestConflict(
                        "client_request_id was already used with another payload"
                    )
                message = self._require_connection().execute(
                    "SELECT message_id FROM collab_turn_messages "
                    "WHERE namespace = ? AND turn_id = ? AND kind = 'user' "
                    "ORDER BY seq LIMIT 1",
                    (ns, existing["turn_id"]),
                ).fetchone()
                if message is None:
                    raise CorruptStore(
                        f"turn {existing['turn_id']} has no user message"
                    )
                turn = _turn_from_row(existing)
                return TurnCreation(
                    reused=True,
                    turn=turn,
                    input_ref=dict(turn.input_ref),
                    message_id=str(message["message_id"]),
                )

            input_now = utc_now_rfc3339()
            input_row = self.register_input(
                namespace=ns,
                origin_kind=_USER_STATEMENT,
                body=body,
                privacy_class=privacy,
                retention=policy,
                observed_at=input_now,
                recorded_at=input_now,
                content_version=1,
            )
            input_ref = {
                "ref_type": "collab_input",
                "namespace": ns,
                "input_id": input_row.input_id,
                "content_version": input_row.content_version,
            }
            turn_no = int(conversation.next_turn_no)
            cursor = self._require_connection().execute(
                "UPDATE collab_conversations SET next_turn_no = next_turn_no + 1, "
                "updated_at = ? WHERE namespace = ? AND conversation_id = ? "
                "AND host_instance = ? AND next_turn_no = ?",
                (utc_now_rfc3339(), ns, cid, host, turn_no),
            )
            if cursor.rowcount != 1:
                raise TurnStateConflict("the conversation turn counter moved")
            now = utc_now_rfc3339()
            self._require_connection().execute(
                "INSERT INTO collab_turns (turn_id, namespace, conversation_id, "
                "turn_no, client_request_id, client_request_digest, "
                "input_ref_json, state, profile_ref_json, mode, strategy_json, "
                "last_event_seq, final_message_ref, created_at, updated_at, "
                "started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, "
                "'accepted', ?, ?, ?, 0, NULL, ?, ?, NULL, NULL)",
                (
                    tid,
                    ns,
                    cid,
                    turn_no,
                    request_id,
                    digest,
                    canonical_json_text(input_ref),
                    canonical_json_text(pinned),
                    conversation.mode,
                    strategy_text,
                    now,
                    now,
                ),
            )
            message_id = str(uuid.uuid4())
            self._require_connection().execute(
                "INSERT INTO collab_turn_messages (message_id, namespace, "
                "turn_id, seq, kind, input_ref_json, artifact_id, "
                "artifact_version, attempt_ref_json, status, published_at, "
                "created_at) VALUES (?, ?, ?, 1, 'user', ?, NULL, NULL, NULL, "
                "'published', ?, ?)",
                (message_id, ns, tid, canonical_json_text(input_ref), now, now),
            )
            pending.append(
                (
                    self._append_turn_event_locked(
                        ns, tid, "turn_accepted", {"turn_no": turn_no}
                    ),
                    None,
                )
            )
            turn = self._turn_locked(ns, tid)
            creation = TurnCreation(
                reused=False,
                turn=turn,
                input_ref=input_ref,
                message_id=message_id,
            )
        _fire_events(on_event, pending)
        return creation

    def begin_turn(
        self,
        *,
        namespace: str,
        turn_id: str,
        policy_version: str = "collab.v1.direct-http.v2",
        on_event: Callable[[TurnEventRow, Mapping[str, Any] | None], None] | None = None,
    ) -> StageRow:
        ns = identifiers.require_namespace(namespace)
        tid = _require_uuid(turn_id, "turn_id")
        policy = _require_text(policy_version, "policy_version", 128)
        pending: list[tuple[TurnEventRow, Mapping[str, Any] | None]] = []
        with self.transaction():
            turn = self._turn_locked(ns, tid)
            if turn.state != "accepted":
                raise TurnStateConflict(
                    f"turn {tid} is {turn.state!r}, not accepted"
                )
            stage_id = str(uuid.uuid4())
            now = utc_now_rfc3339()
            self._require_connection().execute(
                "INSERT INTO collab_turn_stages (stage_id, namespace, turn_id, "
                "kind, status, policy_version, skip_reason, created_at, "
                "completed_at) VALUES (?, ?, ?, 'content', 'pending', ?, NULL, "
                "?, NULL)",
                (stage_id, ns, tid, policy, now),
            )
            cursor = self._require_connection().execute(
                "UPDATE collab_turns SET state = 'running', started_at = ?, "
                "updated_at = ? WHERE namespace = ? AND turn_id = ? "
                "AND state = 'accepted'",
                (now, now, ns, tid),
            )
            if cursor.rowcount != 1:
                raise TurnStateConflict(f"turn {tid} moved before it started")
            self._require_connection().execute(
                "UPDATE collab_turn_stages SET status = 'running' "
                "WHERE namespace = ? AND stage_id = ? AND status = 'pending'",
                (ns, stage_id),
            )
            pending.append(
                (
                    self._append_turn_event_locked(
                        ns, tid, "stage_started", {"stage": "content"}
                    ),
                    None,
                )
            )
            stage = self._stage_locked(ns, stage_id)
        _fire_events(on_event, pending)
        return stage

    def bind_stage_context(
        self,
        *,
        namespace: str,
        stage_id: str,
        context_id: str,
        ordinal: int = 1,
        purpose: str = "content_initial",
    ) -> None:
        """Link one persisted Context to its stage (stage -> Context chain)."""

        ns = identifiers.require_namespace(namespace)
        sid = _require_uuid(stage_id, "stage_id")
        cid = _require_uuid(context_id, "context_id")
        if type(ordinal) is not int or ordinal < 1:
            raise InvalidInput("ordinal must be a positive integer")
        if purpose not in (
            "content_initial",
            "content_after_commentary",
            "content_after_tool",
            "content_repair",
            "expression",
        ):
            raise InvalidInput("purpose is not a frozen stage purpose")
        with self.transaction():
            self._stage_locked(ns, sid)
            row = self._require_connection().execute(
                "SELECT namespace FROM context_snapshots WHERE context_id = ?",
                (cid,),
            ).fetchone()
            if row is None:
                raise RecordNotFound(f"no persisted context {cid!r}")
            if row["namespace"] != ns:
                raise CorruptStore("stage context names another namespace")
            try:
                self._require_connection().execute(
                    "INSERT INTO collab_stage_contexts (namespace, stage_id, "
                    "context_id, ordinal, purpose, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ns, sid, cid, ordinal, purpose, utc_now_rfc3339()),
                )
            except Exception as exc:  # sqlite3.IntegrityError
                raise InvalidInput("stage context link already exists") from exc

    # --------------------------------------------------------- stages (RG07)

    def begin_expression_stage(
        self,
        *,
        namespace: str,
        turn_id: str,
        policy_version: str = "sdrpe.v1",
        on_event: Callable[[TurnEventRow, Mapping[str, Any] | None], None] | None = None,
    ) -> StageRow:
        """Start the second (expression) stage of one running turn.

        ``0017`` freezes ``kind='expression'`` and one stage per kind, so this is
        the durable proof that the turn really attempted a second generation.
        It never publishes, never calls a model and never touches the content
        stage.
        """

        ns = identifiers.require_namespace(namespace)
        tid = _require_uuid(turn_id, "turn_id")
        policy = _require_text(policy_version, "policy_version", 128)
        pending: list[tuple[TurnEventRow, Mapping[str, Any] | None]] = []
        with self.transaction():
            turn = self._turn_locked(ns, tid)
            if turn.state != "running":
                raise TurnStateConflict(f"turn {tid} is {turn.state!r}, not running")
            stage_id = str(uuid.uuid4())
            now = utc_now_rfc3339()
            try:
                self._require_connection().execute(
                    "INSERT INTO collab_turn_stages (stage_id, namespace, turn_id, "
                    "kind, status, policy_version, skip_reason, created_at, "
                    "completed_at) VALUES (?, ?, ?, 'expression', 'running', ?, "
                    "NULL, ?, NULL)",
                    (stage_id, ns, tid, policy, now),
                )
            except Exception as exc:  # sqlite3.IntegrityError
                raise InvalidInput(
                    "this turn already has an expression stage"
                ) from exc
            pending.append(
                (
                    self._append_turn_event_locked(
                        ns, tid, "stage_started", {"stage": "expression"}
                    ),
                    None,
                )
            )
            stage = self._stage_locked(ns, stage_id)
        _fire_events(on_event, pending)
        return stage

    def skip_expression_stage(
        self,
        *,
        namespace: str,
        turn_id: str,
        reason: str,
        policy_version: str = "sdrpe.v1",
        on_event: Callable[[TurnEventRow, Mapping[str, Any] | None], None] | None = None,
    ) -> StageRow:
        """Record a skipped expression stage without ever starting it.

        One row and one ``stage_skipped`` event: a skip is a real outcome, not a
        stage that ran and then failed.
        """

        ns = identifiers.require_namespace(namespace)
        tid = _require_uuid(turn_id, "turn_id")
        label = _require_text(reason, "skip_reason", 128)
        policy = _require_text(policy_version, "policy_version", 128)
        pending: list[tuple[TurnEventRow, Mapping[str, Any] | None]] = []
        with self.transaction():
            turn = self._turn_locked(ns, tid)
            if turn.state != "running":
                raise TurnStateConflict(f"turn {tid} is {turn.state!r}, not running")
            stage_id = str(uuid.uuid4())
            now = utc_now_rfc3339()
            try:
                self._require_connection().execute(
                    "INSERT INTO collab_turn_stages (stage_id, namespace, turn_id, "
                    "kind, status, policy_version, skip_reason, created_at, "
                    "completed_at) VALUES (?, ?, ?, 'expression', 'skipped', ?, ?, "
                    "?, ?)",
                    (stage_id, ns, tid, policy, label, now, now),
                )
            except Exception as exc:  # sqlite3.IntegrityError
                raise InvalidInput(
                    "this turn already has an expression stage"
                ) from exc
            pending.append(
                (
                    self._append_turn_event_locked(
                        ns,
                        tid,
                        "stage_skipped",
                        {"stage": "expression", "reason": label},
                    ),
                    None,
                )
            )
            stage = self._stage_locked(ns, stage_id)
        _fire_events(on_event, pending)
        return stage

    def finish_stage(
        self,
        *,
        namespace: str,
        turn_id: str,
        stage_id: str,
        status: str,
        skip_reason: str | None = None,
        failure_reason: str | None = None,
        on_event: Callable[[TurnEventRow, Mapping[str, Any] | None], None] | None = None,
    ) -> StageRow:
        """Settle one open stage as succeeded, failed or skipped.

        ``skipped`` carries a bounded reason in ``skip_reason``; ``failed`` may
        carry a bounded ``failure_reason`` that is only written to the event
        payload (the column exists for skips, not for errors).  Exactly one
        transition is allowed: ``pending``/``running`` to the requested value.
        """

        if status not in ("succeeded", "failed", "skipped"):
            raise InvalidInput("status must be succeeded, failed or skipped")
        label: str | None
        if status == "skipped":
            if skip_reason is None:
                raise InvalidInput("a skipped stage requires a skip_reason")
            label = _require_text(skip_reason, "skip_reason", 128)
            if failure_reason is not None:
                raise InvalidInput("a skipped stage carries no failure_reason")
        elif status == "failed":
            if skip_reason is not None:
                raise InvalidInput("a failed stage does not carry a skip_reason")
            label = (
                None
                if failure_reason is None
                else _require_text(failure_reason, "failure_reason", 128)
            )
        else:
            if skip_reason is not None or failure_reason is not None:
                raise InvalidInput("only a skipped or failed stage carries a reason")
            label = None
        ns = identifiers.require_namespace(namespace)
        tid = _require_uuid(turn_id, "turn_id")
        sid = _require_uuid(stage_id, "stage_id")
        pending: list[tuple[TurnEventRow, Mapping[str, Any] | None]] = []
        with self.transaction():
            stage = self._stage_locked(ns, sid)
            if stage.turn_id != tid:
                raise CorruptStore("the stage belongs to another turn")
            now = utc_now_rfc3339()
            cursor = self._require_connection().execute(
                "UPDATE collab_turn_stages SET status = ?, skip_reason = ?, "
                "completed_at = ? WHERE namespace = ? AND stage_id = ? "
                "AND status IN ('pending', 'running')",
                (status, label, now, ns, sid),
            )
            if cursor.rowcount != 1:
                raise TurnStateConflict(f"stage {sid} is {stage.status!r}, not open")
            payload: dict[str, Any] = {"stage": stage.kind}
            if label is not None:
                payload["reason"] = label
            pending.append(
                (
                    self._append_turn_event_locked(
                        ns, tid, f"stage_{status}", payload
                    ),
                    None,
                )
            )
            settled = self._stage_locked(ns, sid)
        _fire_events(on_event, pending)
        return settled

    def stages(self, *, namespace: str, turn_id: str) -> tuple[StageRow, ...]:
        """The turn's stages in order (content first), for reads and evidence."""

        ns = identifiers.require_namespace(namespace)
        tid = _require_uuid(turn_id, "turn_id")
        self._turn_locked(ns, tid)
        rows = self._require_connection().execute(
            "SELECT * FROM collab_turn_stages WHERE namespace = ? AND turn_id = ? "
            "ORDER BY CASE kind WHEN 'content' THEN 0 ELSE 1 END, created_at",
            (ns, tid),
        ).fetchall()
        return tuple(_stage_from_row(row) for row in rows)

    def _settle_open_stages_locked(
        self, ns: str, turn_id: str, status: str
    ) -> None:
        """Terminalize any stage a failed/cancelled turn left behind."""

        self._require_connection().execute(
            "UPDATE collab_turn_stages SET status = ?, completed_at = ? "
            "WHERE namespace = ? AND turn_id = ? "
            "AND status IN ('pending', 'running')",
            (status, utc_now_rfc3339(), ns, turn_id),
        )

    def turn_id_for_context(self, namespace: str, context_id: str) -> str | None:
        """The turn whose stage rendered one Context, if it was a chat turn.

        A Context built outside a conversation (legacy consult) has no link and
        returns ``None``; the caller must not guess a turn.
        """

        ns = identifiers.require_namespace(namespace)
        cid = _require_uuid(context_id, "context_id")
        row = self._require_connection().execute(
            "SELECT s.turn_id FROM collab_stage_contexts sc "
            "JOIN collab_turn_stages s "
            "  ON s.namespace = sc.namespace AND s.stage_id = sc.stage_id "
            "WHERE sc.namespace = ? AND sc.context_id = ? LIMIT 1",
            (ns, cid),
        ).fetchone()
        return None if row is None else str(row["turn_id"])

    def latest_stage_context_id(
        self, namespace: str, turn_id: str, *, kind: str = "content"
    ) -> str | None:
        """The latest Context rendered by one stage of one turn.

        RG08 R-E uses this read-only link to see which bridge events R-B
        actually delivered into the turn's final content Context.  A turn with
        no stage/context (legacy) returns ``None``; no row is written.
        """

        ns = identifiers.require_namespace(namespace)
        tid = _require_uuid(turn_id, "turn_id")
        if kind not in ("content", "expression"):
            raise InvalidInput("stage kind must be content or expression")
        row = self._require_connection().execute(
            "SELECT sc.context_id FROM collab_stage_contexts sc "
            "JOIN collab_turn_stages s "
            "  ON s.namespace = sc.namespace AND s.stage_id = sc.stage_id "
            "WHERE sc.namespace = ? AND s.turn_id = ? AND s.kind = ? "
            "ORDER BY sc.ordinal DESC, sc.created_at DESC LIMIT 1",
            (ns, tid, kind),
        ).fetchone()
        return None if row is None else str(row["context_id"])

    def publish_assistant_message(
        self,
        *,
        namespace: str,
        turn_id: str,
        stage_id: str,
        interaction_id: str,
        context_id: str,
        text: str,
        retention: str,
        sources: Sequence[Mapping[str, Any]],
        attempt_ref: Mapping[str, Any] | None,
        kind: str = "final",
        commentary_omitted: str | None = None,
        finish_stage: bool = True,
        parts: Sequence[str] | None = None,
        on_event: Callable[[TurnEventRow, Mapping[str, Any] | None], None] | None = None,
    ) -> TurnRow:
        """Register the reply artifact, its public message and the terminal state.

        The artifact, message, Turn CAS, process event and interaction result
        commit (or roll back) together.  ``kind='wait_question'`` moves the
        turn to ``waiting`` and never sets ``final_message_ref``.

        ``finish_stage=False`` keeps an already settled stage untouched: RG07
        publishes the content stage's draft after the expression stage failed,
        and the content stage was marked ``succeeded`` before that request.

        ``parts`` is the RG08 ordered bubble list.  It is persisted once in the
        reply artifact body (plain text for one bubble, one schema-marked JSON
        envelope for several); ``text`` must be their joined compatibility view.
        """

        ns = identifiers.require_namespace(namespace)
        tid = _require_uuid(turn_id, "turn_id")
        sid = _require_uuid(stage_id, "stage_id")
        iid = _require_uuid(interaction_id, "interaction_id")
        cid = _require_uuid(context_id, "context_id")
        if kind not in ("final", "wait_question"):
            raise InvalidInput("kind must be final or wait_question")
        body = _require_text(text, "text", 262144)
        artifact_body = body
        if parts is not None:
            if kind != "final":
                raise InvalidInput("only a final message carries ordered parts")
            values = normalise_bubble_parts(parts, field="parts")
            if join_bubbles(values) != body:
                raise InvalidInput("parts must join to exactly the published text")
            artifact_body = encode_reply_body(values)
        policy = _require_retention(retention)
        pending: list[tuple[TurnEventRow, Mapping[str, Any] | None]] = []
        with self.transaction():
            turn = self._turn_locked(ns, tid)
            if turn.state != "running":
                raise TurnStateConflict(f"turn {tid} is not running")
            artifact = self.register_artifact(
                namespace=ns,
                kind="reply",
                interaction_id=iid,
                context_id=cid,
                origin_kind="model_output",
                body=artifact_body,
                attempt_ref=attempt_ref,
                retention=policy,
                sources=sources,
            )
            now = utc_now_rfc3339()
            seq = int(
                self._require_connection().execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM collab_turn_messages "
                    "WHERE namespace = ? AND turn_id = ?",
                    (ns, tid),
                ).fetchone()[0]
            )
            message_id = str(uuid.uuid4())
            self._require_connection().execute(
                "INSERT INTO collab_turn_messages (message_id, namespace, "
                "turn_id, seq, kind, input_ref_json, artifact_id, "
                "artifact_version, attempt_ref_json, status, published_at, "
                "created_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 'published', "
                "?, ?)",
                (
                    message_id,
                    ns,
                    tid,
                    seq,
                    kind,
                    artifact.artifact_id,
                    artifact.artifact_version,
                    None
                    if attempt_ref is None
                    else canonical_json_text(dict(attempt_ref)),
                    now,
                    now,
                ),
            )
            new_state = "completed" if kind == "final" else "waiting"
            cursor = self._require_connection().execute(
                "UPDATE collab_turns SET state = ?, final_message_ref = ?, "
                "completed_at = ?, updated_at = ? WHERE namespace = ? "
                "AND turn_id = ? AND state = 'running'",
                (
                    new_state,
                    message_id if kind == "final" else None,
                    now if kind == "final" else None,
                    now,
                    ns,
                    tid,
                ),
            )
            if cursor.rowcount != 1:
                raise TurnStateConflict(f"turn {tid} left running before publish")
            if finish_stage:
                self._require_connection().execute(
                    "UPDATE collab_turn_stages SET status = 'succeeded', "
                    "completed_at = ? WHERE namespace = ? AND stage_id = ?",
                    (now, ns, sid),
                )
            published_payload: dict[str, Any] = {
                "kind": kind,
                "message_seq": seq,
                "message_id": message_id,
            }
            if commentary_omitted is not None:
                published_payload["commentary_omitted"] = commentary_omitted
            pending.append(
                (
                    self._append_turn_event_locked(
                        ns, tid, "message_published", published_payload
                    ),
                    None,
                )
            )
            pending.append(
                (
                    self._append_turn_event_locked(
                        ns,
                        tid,
                        "turn_completed" if kind == "final" else "turn_waiting",
                        {"state": new_state},
                    ),
                    None,
                )
            )
            self.complete_interaction(
                namespace=ns,
                interaction_id=iid,
                result="answered" if kind == "final" else "waiting",
            )
            self._touch_conversation_locked(ns, turn.conversation_id)
            published = self._turn_locked(ns, tid)
        _fire_events(on_event, pending)
        return published

    def fail_turn(
        self,
        *,
        namespace: str,
        turn_id: str,
        reason: str,
        stage_id: str | None = None,
        interaction_id: str | None = None,
        on_event: Callable[[TurnEventRow, Mapping[str, Any] | None], None] | None = None,
    ) -> TurnRow:
        ns = identifiers.require_namespace(namespace)
        tid = _require_uuid(turn_id, "turn_id")
        pending: list[tuple[TurnEventRow, Mapping[str, Any] | None]] = []
        label = _require_text(reason, "reason", 256)
        sid = None if stage_id is None else _require_uuid(stage_id, "stage_id")
        iid = (
            None
            if interaction_id is None
            else _require_uuid(interaction_id, "interaction_id")
        )
        with self.transaction():
            turn = self._turn_locked(ns, tid)
            if turn.state in TURN_TERMINAL_STATES:
                return turn
            now = utc_now_rfc3339()
            cursor = self._require_connection().execute(
                "UPDATE collab_turns SET state = 'failed', completed_at = ?, "
                "updated_at = ? WHERE namespace = ? AND turn_id = ? "
                "AND state IN ('accepted', 'running')",
                (now, now, ns, tid),
            )
            if cursor.rowcount != 1:
                return self._turn_locked(ns, tid)
            if sid is not None:
                self._require_connection().execute(
                    "UPDATE collab_turn_stages SET status = 'failed', "
                    "completed_at = ? WHERE namespace = ? AND stage_id = ?",
                    (now, ns, sid),
                )
            # RG07: a failed turn never leaves a second stage running.
            self._settle_open_stages_locked(ns, tid, "failed")
            pending.append(
                (
                    self._append_turn_event_locked(
                        ns, tid, "turn_failed", {"reason": label, "state": "failed"}
                    ),
                    None,
                )
            )
            if iid is not None:
                try:
                    self.complete_interaction(
                        namespace=ns, interaction_id=iid, result="failed"
                    )
                except RecordNotFound:
                    pass
            self._touch_conversation_locked(ns, turn.conversation_id)
            failed = self._turn_locked(ns, tid)
        _fire_events(on_event, pending)
        return failed

    def interrupt_stale_turns(self, *, namespace: str) -> int:
        """Startup scan: accepted/running/waiting -> interrupted; running steps unknown."""

        ns = identifiers.require_namespace(namespace)
        interrupted = 0
        with self.transaction():
            rows = self._require_connection().execute(
                "SELECT turn_id, conversation_id FROM collab_turns "
                "WHERE namespace = ? AND state IN ('accepted', 'running', "
                "'waiting') ORDER BY created_at LIMIT 1000",
                (ns,),
            ).fetchall()
            now = utc_now_rfc3339()
            for row in rows:
                cursor = self._require_connection().execute(
                    "UPDATE collab_turns SET state = 'interrupted', "
                    "completed_at = ?, updated_at = ? WHERE namespace = ? "
                    "AND turn_id = ? AND state IN ('accepted', 'running', "
                    "'waiting')",
                    (now, now, ns, row["turn_id"]),
                )
                if cursor.rowcount != 1:
                    continue
                interrupted += 1
                self._require_connection().execute(
                    "UPDATE collab_turn_steps SET status = 'outcome_unknown', "
                    "finished_at = NULL WHERE namespace = ? AND turn_id = ? "
                    "AND status = 'running'",
                    (ns, row["turn_id"]),
                )
                # RG07: no stage may keep "running" across a restart.  The
                # stage CHECK has no 'interrupted' value, so 'cancelled' is the
                # honest terminal label; turn_interrupted below carries why.
                self._settle_open_stages_locked(ns, row["turn_id"], "cancelled")
                self._record_turn_event_locked(
                    ns,
                    row["turn_id"],
                    "turn_interrupted",
                    {"reason": "host_restart", "state": "interrupted"},
                )
                interaction = self._interaction_for_turn_locked(
                    ns, row["turn_id"]
                )
                if interaction is not None:
                    # A waiting turn already completed its interaction with the
                    # frozen label 'waiting'.  Restarting the host must not try
                    # to downgrade it to 'incomplete' (that is CORRUPT_STORE and
                    # used to make the whole host refuse to start).  Only an
                    # interaction that has no result yet may be marked here.
                    current = self._require_connection().execute(
                        "SELECT result FROM collab_interactions "
                        "WHERE namespace = ? AND interaction_id = ?",
                        (ns, interaction),
                    ).fetchone()
                    if current is None or current["result"] is None:
                        try:
                            self.complete_interaction(
                                namespace=ns,
                                interaction_id=interaction,
                                result="incomplete",
                            )
                        except RecordNotFound:
                            pass
                self._touch_conversation_locked(ns, row["conversation_id"])
        return interrupted

    def record_turn_event(
        self,
        *,
        namespace: str,
        turn_id: str,
        kind: str,
        payload: Mapping[str, Any],
        on_event: Callable[[TurnEventRow, Mapping[str, Any] | None], None] | None = None,
    ) -> int:
        ns = identifiers.require_namespace(namespace)
        tid = _require_uuid(turn_id, "turn_id")
        label = _require_text(kind, "kind", 64)
        with self.transaction():
            self._turn_locked(ns, tid)
            row = self._append_turn_event_locked(ns, tid, label, payload)
        if on_event is not None:
            # The row is already committed: the live ring only mirrors it.
            on_event(row, None)
        return row.seq

    # ------------------------------------------------------- tool steps/events

    def plan_tool_step(
        self,
        *,
        namespace: str,
        turn_id: str,
        stage_id: str,
        step_no: int,
        tool_name: str,
        arguments_digest: str | None,
        operation_key: str,
        commentary_omitted: str | None = None,
        live_payload: Mapping[str, Any] | None = None,
        on_event: Callable[[TurnEventRow, Mapping[str, Any] | None], None] | None = None,
    ) -> StepRow:
        """Insert one ``planned`` step and its public event in one transaction."""

        ns = identifiers.require_namespace(namespace)
        tid = _require_uuid(turn_id, "turn_id")
        sid = _require_uuid(stage_id, "stage_id")
        if type(step_no) is not int or not 1 <= step_no <= 1000:
            raise InvalidInput("step_no must be 1..1000")
        tool = _require_text(tool_name, "tool_name", 128)
        digest = (
            None
            if arguments_digest is None
            else _require_text(arguments_digest, "arguments_digest", 64)
        )
        key = _require_text(operation_key, "operation_key", 256)
        step_id = str(uuid.uuid4())
        pending: list[tuple[TurnEventRow, Mapping[str, Any] | None]] = []
        with self.transaction():
            turn = self._turn_locked(ns, tid)
            if turn.state != "running":
                raise TurnStateConflict(f"turn {tid} is {turn.state!r}, not running")
            self._stage_locked(ns, sid)
            self._require_connection().execute(
                "INSERT INTO collab_turn_steps (step_id, namespace, turn_id, "
                "stage_id, step_no, tool_name, arguments_payload_ref, "
                "arguments_digest, result_artifact_id, result_artifact_version, "
                "result_digest, operation_key, status, started_at, finished_at, "
                "error_label) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, "
                "NULL, ?, 'planned', NULL, NULL, NULL)",
                (step_id, ns, tid, sid, step_no, tool, digest, key),
            )
            planned: dict[str, Any] = {
                "step_id": step_id,
                "step_no": step_no,
                "tool": tool,
                "arguments_digest": digest,
            }
            if commentary_omitted is not None:
                planned["commentary_omitted"] = _require_text(
                    commentary_omitted, "commentary_omitted", 32
                )
            event = self._append_turn_event_locked(
                ns, tid, "tool_planned", planned
            )
            pending.append((event, live_payload))
        _fire_events(on_event, pending)
        return self._step_locked(ns, step_id)

    def start_tool_step(
        self,
        *,
        namespace: str,
        turn_id: str,
        step_id: str,
        on_event: Callable[[TurnEventRow, Mapping[str, Any] | None], None] | None = None,
    ) -> StepRow:
        """``planned -> running`` plus the public ``tool_started`` event."""

        ns = identifiers.require_namespace(namespace)
        tid = _require_uuid(turn_id, "turn_id")
        sid = _require_uuid(step_id, "step_id")
        pending: list[tuple[TurnEventRow, Mapping[str, Any] | None]] = []
        with self.transaction():
            step = self._step_locked(ns, sid)
            if step.turn_id != tid:
                raise CorruptStore("step does not belong to the named turn")
            now = utc_now_rfc3339()
            cursor = self._require_connection().execute(
                "UPDATE collab_turn_steps SET status = 'running', started_at = ? "
                "WHERE namespace = ? AND step_id = ? AND status = 'planned'",
                (now, ns, sid),
            )
            if cursor.rowcount != 1:
                raise TurnStateConflict(f"step {sid} is not planned")
            event = self._append_turn_event_locked(
                ns,
                tid,
                "tool_started",
                {"step_id": sid, "step_no": step.step_no, "tool": step.tool_name},
            )
            pending.append((event, None))
        _fire_events(on_event, pending)
        return self._step_locked(ns, sid)

    def finish_tool_step(
        self,
        *,
        namespace: str,
        turn_id: str,
        step_id: str,
        interaction_id: str,
        context_id: str,
        status: str,
        duration_ms: int,
        body: str | None = None,
        sources: Sequence[Mapping[str, Any]] = (),
        retention: str | None = None,
        error_label: str | None = None,
        live_payload: Mapping[str, Any] | None = None,
        on_event: Callable[[TurnEventRow, Mapping[str, Any] | None], None] | None = None,
        allow_planned: bool = False,
        event_kind: str = "tool_finished",
        public_status: str | None = None,
        reason: str | None = None,
    ) -> StepRow:
        """Finish one step and (when a body exists) register its result artifact.

        ``allow_planned`` is used by one path only: a call the catalog refused
        before execution moves ``planned -> failed`` and emits ``tool_rejected``
        instead of ``tool_started``/``tool_finished``.  The optional ``reason``
        is a stable public classification (never a message or arguments).
        """

        ns = identifiers.require_namespace(namespace)
        tid = _require_uuid(turn_id, "turn_id")
        sid = _require_uuid(step_id, "step_id")
        iid = _require_uuid(interaction_id, "interaction_id")
        cid = _require_uuid(context_id, "context_id")
        if status not in ("succeeded", "failed", "cancelled", "outcome_unknown"):
            raise InvalidInput(
                "status must be succeeded, failed, cancelled or outcome_unknown"
            )
        if event_kind not in ("tool_finished", "tool_rejected"):
            raise InvalidInput(
                "event_kind must be tool_finished or tool_rejected"
            )
        if allow_planned and (event_kind != "tool_rejected" or status != "failed"):
            raise InvalidInput(
                "a planned step can only become a failed tool_rejected event"
            )
        if type(duration_ms) is not int or not 0 <= duration_ms <= 86_400_000:
            raise InvalidInput("duration_ms must be a bounded integer")
        label = (
            None
            if error_label is None
            else _require_text(error_label, "error_label", 128)
        )
        public = (
            None
            if public_status is None
            else _require_text(public_status, "public_status", 32)
        )
        public_reason = (
            None if reason is None else _require_text(reason, "reason", 32)
        )
        expected_status = "planned" if allow_planned else "running"
        artifact_id = None
        artifact_version = None
        result_digest = None
        body_text = None
        policy = None
        if body is not None:
            body_text = _require_text(body, "body", 262144)
            policy = _require_retention(retention or "standard")
        pending: list[tuple[TurnEventRow, Mapping[str, Any] | None]] = []
        with self.transaction():
            step = self._step_locked(ns, sid)
            if step.turn_id != tid:
                raise CorruptStore("step does not belong to the named turn")
            if step.status != expected_status:
                raise TurnStateConflict(
                    f"step {sid} is {step.status!r}, not {expected_status!r}"
                )
            if body_text is not None:
                artifact = self.register_artifact(
                    namespace=ns,
                    kind="query_result",
                    interaction_id=iid,
                    context_id=cid,
                    origin_kind="deterministic_query",
                    body=body_text,
                    attempt_ref=None,
                    retention=policy,
                    sources=sources,
                )
                artifact_id = artifact.artifact_id
                artifact_version = artifact.artifact_version
                result_digest = "sha256:" + sha256_hex(body_text)
            now = utc_now_rfc3339()
            cursor = self._require_connection().execute(
                "UPDATE collab_turn_steps SET status = ?, finished_at = ?, "
                "result_artifact_id = ?, result_artifact_version = ?, "
                "result_digest = ?, error_label = ? WHERE namespace = ? AND "
                "step_id = ? AND status = ?",
                (
                    status,
                    now,
                    artifact_id,
                    artifact_version,
                    result_digest,
                    label,
                    ns,
                    sid,
                    expected_status,
                ),
            )
            if cursor.rowcount != 1:
                raise TurnStateConflict(f"step {sid} left {expected_status}")
            payload: dict[str, Any] = {
                "step_id": sid,
                "step_no": step.step_no,
                "tool": step.tool_name,
                "status": public or status,
                "duration_ms": duration_ms,
                "result_artifact_id": artifact_id,
                "result_artifact_version": artifact_version,
                "sizes": {
                    "result_chars": 0 if body_text is None else len(body_text)
                },
            }
            if public_reason is not None:
                payload["reason"] = public_reason
            event = self._append_turn_event_locked(
                ns, tid, event_kind, payload
            )
            pending.append((event, live_payload))
        _fire_events(on_event, pending)
        return self._step_locked(ns, sid)

    def publish_commentary(
        self,
        *,
        namespace: str,
        turn_id: str,
        stage_id: str,
        interaction_id: str,
        context_id: str,
        text: str,
        retention: str,
        sources: Sequence[Mapping[str, Any]],
        attempt_ref: Mapping[str, Any] | None = None,
        live_text: str | None = None,
        commentary_source: str = "model",
        on_event: Callable[[TurnEventRow, Mapping[str, Any] | None], None] | None = None,
    ) -> MessageRow:
        """Publish one already-validated commentary message without ending the turn.

        ``commentary_source`` is ``model`` for a sentence the provider wrote or
        ``host_fallback`` for the deterministic short line the host supplies
        immediately before an accepted tool step.  The event carries it so a
        host line is never presented as model output.  The reply artifact keeps
        ``origin_kind='model_output'`` because the frozen artifact schema only
        permits ``model_output``/``deterministic_query``; the event field is the
        authoritative source and is always present.
        """

        ns = identifiers.require_namespace(namespace)
        tid = _require_uuid(turn_id, "turn_id")
        sid = _require_uuid(stage_id, "stage_id")
        iid = _require_uuid(interaction_id, "interaction_id")
        cid = _require_uuid(context_id, "context_id")
        if commentary_source not in ("model", "host_fallback"):
            raise InvalidInput("commentary_source must be model or host_fallback")
        body = _require_text(text, "text", 4096)
        policy = _require_retention(retention)
        if live_text is not None and live_text != body:
            raise InvalidInput("live_text must equal the published commentary")
        pending: list[tuple[TurnEventRow, Mapping[str, Any] | None]] = []
        with self.transaction():
            turn = self._turn_locked(ns, tid)
            if turn.state != "running":
                raise TurnStateConflict(f"turn {tid} is {turn.state!r}, not running")
            self._stage_locked(ns, sid)
            artifact = self.register_artifact(
                namespace=ns,
                kind="reply",
                interaction_id=iid,
                context_id=cid,
                origin_kind="model_output",
                body=body,
                attempt_ref=attempt_ref,
                retention=policy,
                sources=sources,
            )
            now = utc_now_rfc3339()
            seq = int(
                self._require_connection().execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM collab_turn_messages "
                    "WHERE namespace = ? AND turn_id = ?",
                    (ns, tid),
                ).fetchone()[0]
            )
            message_id = str(uuid.uuid4())
            self._require_connection().execute(
                "INSERT INTO collab_turn_messages (message_id, namespace, "
                "turn_id, seq, kind, input_ref_json, artifact_id, "
                "artifact_version, attempt_ref_json, status, published_at, "
                "created_at) VALUES (?, ?, ?, ?, 'commentary', NULL, ?, ?, ?, "
                "'published', ?, ?)",
                (
                    message_id,
                    ns,
                    tid,
                    seq,
                    artifact.artifact_id,
                    artifact.artifact_version,
                    None
                    if attempt_ref is None
                    else canonical_json_text(dict(attempt_ref)),
                    now,
                    now,
                ),
            )
            event = self._append_turn_event_locked(
                ns,
                tid,
                "message_published",
                {
                    "kind": "commentary",
                    "message_seq": seq,
                    "message_id": message_id,
                    "commentary_source": commentary_source,
                },
            )
            pending.append(
                (event, None if live_text is None else {"text": live_text})
            )
            row = self._message_row_locked(ns, message_id)
        _fire_events(on_event, pending)
        return row

    def cancel_turn(
        self,
        *,
        namespace: str,
        turn_id: str,
        stage_id: str | None = None,
        interaction_id: str | None = None,
        on_event: Callable[[TurnEventRow, Mapping[str, Any] | None], None] | None = None,
    ) -> TurnRow:
        """Cooperative cancel: CAS to ``cancelled``; never writes a final."""

        ns = identifiers.require_namespace(namespace)
        tid = _require_uuid(turn_id, "turn_id")
        sid = None if stage_id is None else _require_uuid(stage_id, "stage_id")
        iid = (
            None
            if interaction_id is None
            else _require_uuid(interaction_id, "interaction_id")
        )
        pending: list[tuple[TurnEventRow, Mapping[str, Any] | None]] = []
        with self.transaction():
            turn = self._turn_locked(ns, tid)
            if turn.state in TURN_TERMINAL_STATES:
                return turn
            now = utc_now_rfc3339()
            cursor = self._require_connection().execute(
                "UPDATE collab_turns SET state = 'cancelled', completed_at = ?, "
                "updated_at = ? WHERE namespace = ? AND turn_id = ? "
                "AND state IN ('accepted', 'running')",
                (now, now, ns, tid),
            )
            if cursor.rowcount != 1:
                return self._turn_locked(ns, tid)
            if sid is not None:
                self._require_connection().execute(
                    "UPDATE collab_turn_stages SET status = 'cancelled', "
                    "completed_at = ? WHERE namespace = ? AND stage_id = ?",
                    (now, ns, sid),
                )
            # RG07: the expression stage may be open when a cancel lands.
            self._settle_open_stages_locked(ns, tid, "cancelled")
            event = self._append_turn_event_locked(
                ns, tid, "turn_cancelled", {"state": "cancelled"}
            )
            pending.append((event, None))
            if iid is not None:
                try:
                    self.complete_interaction(
                        namespace=ns, interaction_id=iid, result="incomplete"
                    )
                except RecordNotFound:
                    pass
            self._touch_conversation_locked(ns, turn.conversation_id)
            result = self._turn_locked(ns, tid)
        _fire_events(on_event, pending)
        return result

    def discard_late_result(
        self,
        *,
        namespace: str,
        turn_id: str,
        attempt_ref: Mapping[str, Any],
        on_event: Callable[[TurnEventRow, Mapping[str, Any] | None], None] | None = None,
    ) -> None:
        """Record a post-cancel/post-terminal response without publishing it."""

        ns = identifiers.require_namespace(namespace)
        tid = _require_uuid(turn_id, "turn_id")
        ref = _require_mapping(attempt_ref, "attempt_ref")
        pending: list[tuple[TurnEventRow, Mapping[str, Any] | None]] = []
        with self.transaction():
            self._turn_locked(ns, tid)
            event = self._append_turn_event_locked(
                ns, tid, "late_result_discarded", {"attempt_ref": dict(ref)}
            )
            pending.append((event, None))
        _fire_events(on_event, pending)

    def read_turn_events(
        self,
        *,
        namespace: str,
        host_instance: str,
        turn_id: str,
        after_seq: int = 0,
        limit: int = 100,
    ) -> TurnEventPage:
        """The persisted event tail of one turn, in ``seq`` order."""

        ns = identifiers.require_namespace(namespace)
        host = _require_text(host_instance, "host_instance", 256)
        tid = _require_uuid(turn_id, "turn_id")
        if type(after_seq) is not int or not 0 <= after_seq <= 1_000_000:
            raise InvalidInput("after_seq must be 0..1000000")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidInput("limit must be 1..200")
        turn = self._turn_for_host_locked(ns, host, tid)
        rows = self._require_connection().execute(
            "SELECT * FROM collab_turn_events WHERE namespace = ? AND "
            "turn_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (ns, tid, after_seq, limit),
        ).fetchall()
        events = tuple(_event_from_row(row) for row in rows)
        next_seq = events[-1].seq if events else after_seq
        return TurnEventPage(
            events=events,
            next_seq=next_seq,
            terminal=turn.state in TURN_TERMINAL_STATES,
        )

    def read_turn_steps(
        self,
        *,
        namespace: str,
        host_instance: str,
        turn_id: str,
        access: Any,
        after_step_no: int = 0,
        limit: int = 20,
    ) -> StepPage:
        """The filtered tool-step detail rows of one turn."""

        ns = identifiers.require_namespace(namespace)
        host = _require_text(host_instance, "host_instance", 256)
        tid = _require_uuid(turn_id, "turn_id")
        if type(after_step_no) is not int or not 0 <= after_step_no <= 1000:
            raise InvalidInput("after_step_no must be 0..1000")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise InvalidInput("limit must be 1..50")
        self._turn_for_host_locked(ns, host, tid)
        rows = self._require_connection().execute(
            "SELECT * FROM collab_turn_steps WHERE namespace = ? AND "
            "turn_id = ? AND step_no > ? ORDER BY step_no LIMIT ?",
            (ns, tid, after_step_no, limit),
        ).fetchall()
        views = tuple(
            self._step_view_locked(ns, _step_from_row(row), access) for row in rows
        )
        next_after = views[-1].step.step_no if len(views) == limit else None
        return StepPage(steps=views, next_after_step_no=next_after)

    # ------------------------------------------------------- usage / trace

    #: A trace lists at most this many Contexts per stage and attempts per
    #: Context; counts and token sums still cover every stored row.
    MAX_TRACE_CONTEXTS = 20
    MAX_TRACE_ATTEMPTS = 20

    def conversation_usage(
        self,
        *,
        namespace: str,
        host_instance: str,
        conversation_id: str,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        """Read-only token/speed/Context-byte totals for one conversation.

        One ``model_attempts`` row is one real HTTP request.  A request whose
        supplier usage is unavailable counts as unknown and is never estimated;
        a repair is counted only when a persisted ``repair_attempt`` marker
        names that attempt.
        """

        from ..conversation import usage as usage_view

        ns = identifiers.require_namespace(namespace)
        host = _require_text(host_instance, "host_instance", 256)
        cid = _require_uuid(conversation_id, "conversation_id")
        self._conversation_locked(ns, host, cid)
        turn = self._usage_turn_locked(ns, cid, turn_id)

        aggregate = self._conversation_attempt_aggregate_locked(ns, cid)
        attempts_total = int(aggregate["attempts_total"])
        attempts_open = int(aggregate["attempts_open"])
        attempts_known = int(aggregate["attempts_known"])
        attempts_unreadable = int(aggregate["attempts_unreadable"])
        latest = self._latest_attempt_record_locked(ns, cid)
        latest_block: dict[str, Any] | None = None
        latest_context: dict[str, Any] | None = None
        if latest is not None:
            latest_markers, _latest_unbound = self._repair_markers_locked(
                ns, turn_id=latest.turn_id
            )
            latest_block = usage_view.summarize(
                latest,
                latest_markers,
                previous=self._previous_attempt_record_locked(ns, latest),
            ).to_dict()
            latest_context = self._budget_block_locked(ns, latest)
        conversation_markers, conversation_unbound = self._repair_markers_locked(
            ns, conversation_id=cid
        )
        turns_total, turns_with_attempts = self._turn_coverage_locked(ns, cid)
        result: dict[str, Any] = {
            "conversation_id": cid,
            "turns_total": turns_total,
            "turns_with_attempts": turns_with_attempts,
            "attempts_total": attempts_total,
            "attempts_known": attempts_known,
            "attempts_unknown": (
                attempts_total - attempts_open - attempts_known - attempts_unreadable
            ),
            "attempts_open": attempts_open,
            "attempts_unreadable": attempts_unreadable,
            "counts": {
                "repairs": len(conversation_markers),
                "repairs_unbound": conversation_unbound,
            },
            "tokens": usage_view.complete_from_aggregate(aggregate),
            "latest_request": latest_block,
            "latest_context": latest_context,
            "turn": None,
        }
        if turn is not None:
            records = self._attempt_records_locked(ns, turn.turn_id)
            turn_markers, turn_unbound = self._repair_markers_locked(
                ns, turn_id=turn.turn_id
            )
            scope = usage_view.summarize_scope(records, turn_markers)
            turn_counts = dict(scope["counts"])
            turn_counts["repairs_unbound"] = turn_unbound
            result["turn"] = {
                "turn_id": turn.turn_id,
                "turn_no": turn.turn_no,
                "state": turn.state,
                "attempts_total": scope["attempts_total"],
                "attempts_known": scope["attempts_known"],
                "attempts_unknown": scope["attempts_unknown"],
                "attempts_open": scope["attempts_open"],
                "attempts_unreadable": scope["attempts_unreadable"],
                "tokens": scope["tokens"],
                "counts": turn_counts,
                "latest_request": scope["latest_request"],
                "latest_context": self._scope_latest_context_locked(ns, records),
            }
        return result

    def turn_trace(
        self, *, namespace: str, host_instance: str, turn_id: str
    ) -> dict[str, Any]:
        """Stages, stage-to-Context links, attempts and the final's origin.

        Everything is read from persisted rows, so the same answer survives a
        host restart.  An attempt is ``repair`` only when a persisted marker
        names it; a same-body resend is a ``transport_retry`` and an unmarked
        attempt stays ``unknown``.
        """

        from ..conversation import usage as usage_view

        ns = identifiers.require_namespace(namespace)
        host = _require_text(host_instance, "host_instance", 256)
        tid = _require_uuid(turn_id, "turn_id")
        turn = self._turn_for_host_locked(ns, host, tid)
        records = self._attempt_records_locked(ns, tid)
        markers, unbound_repairs = self._repair_markers_locked(ns, turn_id=tid)
        scope = usage_view.summarize_scope(records, markers)
        summaries = {item["attempt_id"]: item for item in scope["attempts"]}
        contexts = self._stage_contexts_locked(ns, tid)
        budgets = self._budget_map_locked(
            ns, [item["context_id"] for item in contexts]
        )
        tool_counts = self._stage_tool_counts_locked(ns, tid)
        truncated = False
        rendered_stages: list[dict[str, Any]] = []
        for stage in self.stages(namespace=ns, turn_id=tid):
            owned = [item for item in contexts if item["stage_id"] == stage.stage_id]
            if len(owned) > self.MAX_TRACE_CONTEXTS:
                truncated = True
            rendered: list[dict[str, Any]] = []
            for context in owned[: self.MAX_TRACE_CONTEXTS]:
                attempts = [
                    summaries[record.attempt_id]
                    for record in records
                    if record.context_id == context["context_id"]
                ]
                if len(attempts) > self.MAX_TRACE_ATTEMPTS:
                    truncated = True
                rendered.append(
                    {
                        "context_id": context["context_id"],
                        "ordinal": int(context["ordinal"]),
                        "purpose": context["purpose"],
                        "created_at": context["created_at"],
                        "bytes": usage_view.context_bytes(
                            budgets.get(context["context_id"])
                        ),
                        "attempts": attempts[: self.MAX_TRACE_ATTEMPTS],
                    }
                )
            counts = tool_counts.get(stage.stage_id, {})
            rendered_stages.append(
                {
                    "stage_id": stage.stage_id,
                    "kind": stage.kind,
                    "status": stage.status,
                    "skip_reason": stage.skip_reason,
                    "policy_version": stage.policy_version,
                    "created_at": stage.created_at,
                    "completed_at": stage.completed_at,
                    "tool_steps": int(counts.get("total", 0)),
                    "tool_steps_by_status": dict(counts.get("by_status", {})),
                    "contexts": rendered,
                }
            )
        counts = dict(scope["counts"])
        counts["repairs_unbound"] = unbound_repairs
        counts["tool_steps"] = sum(
            int(item.get("total", 0)) for item in tool_counts.values()
        )
        return {
            "turn": {
                "turn_id": turn.turn_id,
                "conversation_id": turn.conversation_id,
                "turn_no": turn.turn_no,
                "state": turn.state,
                "created_at": turn.created_at,
                "started_at": turn.started_at,
                "completed_at": turn.completed_at,
                "failure_reason": self._failure_reasons_locked(ns, [tid]).get(tid),
            },
            "stages": rendered_stages,
            "final": self._final_block_locked(ns, tid, records, rendered_stages),
            "attempts_total": scope["attempts_total"],
            "attempts_known": scope["attempts_known"],
            "attempts_unknown": scope["attempts_unknown"],
            "attempts_open": scope["attempts_open"],
            "attempts_unreadable": scope["attempts_unreadable"],
            "counts": counts,
            "tokens": scope["tokens"],
            "repairs": scope["repairs"],
            "latest_request": scope["latest_request"],
            "latest_context": self._scope_latest_context_locked(ns, records),
            "truncated": truncated,
        }

    # ------------------------------------------------- usage/trace internals

    _ATTEMPT_JOIN = (
        "FROM collab_turns t "
        "JOIN collab_turn_stages s ON s.namespace = t.namespace "
        "AND s.turn_id = t.turn_id "
        "JOIN collab_stage_contexts sc ON sc.namespace = s.namespace "
        "AND sc.stage_id = s.stage_id "
        "JOIN model_attempts m ON m.namespace = t.namespace "
        "AND m.context_id = sc.context_id "
    )

    _ATTEMPT_SELECT = (
        "SELECT m.attempt_id, m.attempt_no, m.context_id, m.status, m.outcome, "
        "m.usage_json, m.usage_known, m.started_at, m.finished_at, "
        "m.request_hash, m.finish_reason, m.error_label, "
        "s.kind AS stage_kind, sc.purpose, sc.ordinal, t.turn_id "
    )

    def _attempt_records_locked(self, ns: str, turn_id: str) -> list[Any]:
        rows = self._require_connection().execute(
            self._ATTEMPT_SELECT
            + self._ATTEMPT_JOIN
            + "WHERE t.namespace = ? AND t.turn_id = ? "
            "ORDER BY CASE s.kind WHEN 'content' THEN 0 ELSE 1 END, "
            "sc.ordinal, m.attempt_no, m.attempt_id",
            (ns, turn_id),
        ).fetchall()
        return [self._attempt_record_locked(row) for row in rows]

    def _attempt_record_locked(self, row: Any) -> Any:
        from ..conversation import usage as usage_view

        usage_document: Any = None
        if row["usage_json"] is not None:
            try:
                usage_document = strict_json_loads(row["usage_json"])
            except ValueError as exc:
                raise CorruptStore("stored model usage is not strict JSON") from exc
            if not isinstance(usage_document, dict):
                raise CorruptStore("stored model usage is not a JSON object")
        return usage_view.AttemptRecord(
            attempt_id=str(row["attempt_id"]),
            attempt_no=int(row["attempt_no"]),
            turn_id=str(row["turn_id"]),
            context_id=str(row["context_id"]),
            stage_kind=str(row["stage_kind"]),
            purpose=str(row["purpose"]),
            ordinal=int(row["ordinal"]),
            status=str(row["status"]),
            outcome=None if row["outcome"] is None else str(row["outcome"]),
            usage=usage_document,
            usage_known=bool(int(row["usage_known"])),
            started_at=str(row["started_at"]),
            finished_at=(
                None if row["finished_at"] is None else str(row["finished_at"])
            ),
            request_hash=str(row["request_hash"]),
            finish_reason=(
                None if row["finish_reason"] is None else str(row["finish_reason"])
            ),
            error_label=(
                None if row["error_label"] is None else str(row["error_label"])
            ),
        )

    def _repair_markers_locked(
        self,
        ns: str,
        *,
        turn_id: str | None = None,
        conversation_id: str | None = None,
    ) -> tuple[dict[str, str], int]:
        """Persisted repair markers: attempt-bound markers plus unbound ones.

        ``attempt_id`` is the only identity a marker may carry; a marker with
        no attempt (the send was refused before any reservation) is counted as
        unbound and attributed to nothing.  Legacy markers that nested an
        ``attempt_ref`` are read for their attempt id only, and their other
        fields are never re-exposed.
        """

        if (turn_id is None) == (conversation_id is None):
            raise InvalidInput("exactly one repair marker scope is required")
        if turn_id is not None:
            rows = self._require_connection().execute(
                "SELECT payload_json FROM collab_turn_events "
                "WHERE namespace = ? AND turn_id = ? AND kind = 'repair_attempt' "
                "ORDER BY seq",
                (ns, turn_id),
            ).fetchall()
        else:
            rows = self._require_connection().execute(
                "SELECT e.payload_json FROM collab_turn_events e "
                "JOIN collab_turns t ON t.namespace = e.namespace "
                "AND t.turn_id = e.turn_id "
                "WHERE e.namespace = ? AND t.conversation_id = ? "
                "AND e.kind = 'repair_attempt' ORDER BY t.turn_no, e.seq",
                (ns, conversation_id),
            ).fetchall()
        markers: dict[str, str] = {}
        unbound = 0
        for row in rows:
            try:
                payload = strict_json_loads(row["payload_json"])
            except ValueError as exc:
                raise CorruptStore("stored repair marker is not strict JSON") from exc
            if not isinstance(payload, dict):
                raise CorruptStore("stored repair marker is not a JSON object")
            reason = payload.get("reason")
            if not isinstance(reason, str):
                raise CorruptStore("stored repair marker has no reason")
            attempt_id = payload.get("attempt_id")
            if not isinstance(attempt_id, str):
                reference = payload.get("attempt_ref")
                attempt_id = (
                    reference.get("attempt_id")
                    if isinstance(reference, dict)
                    else None
                )
            if isinstance(attempt_id, str) and attempt_id:
                markers[attempt_id] = reason
            else:
                unbound += 1
        return markers, unbound

    def _budget_map_locked(
        self, ns: str, context_ids: Sequence[str]
    ) -> dict[str, Any]:
        if not context_ids:
            return {}
        placeholders = ", ".join("?" for _ in context_ids)
        rows = self._require_connection().execute(
            "SELECT context_id, budget_view_json FROM context_snapshots "
            "WHERE namespace = ? AND context_id IN (" + placeholders + ")",
            (ns, *context_ids),
        ).fetchall()
        budgets: dict[str, Any] = {}
        for row in rows:
            text = row["budget_view_json"]
            if text is None:
                budgets[str(row["context_id"])] = None
                continue
            try:
                document = strict_json_loads(text)
            except ValueError as exc:
                raise CorruptStore("stored budget view is not strict JSON") from exc
            budgets[str(row["context_id"])] = (
                dict(document) if isinstance(document, dict) else None
            )
        return budgets

    def _budget_block_locked(self, ns: str, record: Any) -> dict[str, Any] | None:
        from ..conversation import usage as usage_view

        budgets = self._budget_map_locked(ns, [record.context_id])
        return {
            "context_id": record.context_id,
            "purpose": record.purpose,
            "stage": record.stage_kind,
            "bytes": usage_view.context_bytes(budgets.get(record.context_id)),
        }

    def _scope_latest_context_locked(
        self, ns: str, records: Sequence[Any]
    ) -> dict[str, Any] | None:
        from ..conversation import usage as usage_view

        latest = usage_view.latest_record(records)
        if latest is None:
            return None
        return self._budget_block_locked(ns, latest)

    def _conversation_attempt_aggregate_locked(
        self, ns: str, conversation_id: str
    ) -> dict[str, int]:
        known = (
            "m.status = 'finished' AND m.usage_known = 1 "
            "AND json_type(m.usage_json, '$.prompt_tokens') = 'integer' "
            "AND json_type(m.usage_json, '$.completion_tokens') = 'integer' "
            "AND json_type(m.usage_json, '$.total_tokens') = 'integer' "
            "AND json_extract(m.usage_json, '$.prompt_tokens') >= 0 "
            "AND json_extract(m.usage_json, '$.completion_tokens') >= 0 "
            "AND json_extract(m.usage_json, '$.total_tokens') >= 0"
        )
        unlimited = (
            "m.status = 'finished' AND m.usage_known = 1 "
            "AND json_type(m.usage_json, '$.prompt_tokens') = 'integer' "
            "AND json_type(m.usage_json, '$.completion_tokens') = 'integer' "
            "AND json_type(m.usage_json, '$.total_tokens') = 'integer'"
        )
        sql = (
            "SELECT COUNT(*) AS attempts_total, "
            "SUM(CASE WHEN m.status = 'reserved' THEN 1 ELSE 0 END) "
            "AS attempts_open, "
            f"SUM(CASE WHEN {known} THEN 1 ELSE 0 END) AS attempts_known, "
            "SUM(CASE WHEN m.status = 'finished' AND m.usage_known = 1 "
            f"AND NOT ({unlimited}) THEN 1 ELSE 0 END) AS attempts_unreadable, "
            f"SUM(CASE WHEN {unlimited} "
            "THEN json_extract(m.usage_json, '$.prompt_tokens') ELSE 0 END) "
            "AS prompt_tokens, "
            f"SUM(CASE WHEN {unlimited} "
            "THEN json_extract(m.usage_json, '$.completion_tokens') ELSE 0 END) "
            "AS completion_tokens, "
            f"SUM(CASE WHEN {unlimited} "
            "THEN json_extract(m.usage_json, '$.total_tokens') ELSE 0 END) "
            "AS total_tokens "
            + self._ATTEMPT_JOIN
            + "WHERE t.namespace = ? AND t.conversation_id = ?"
        )
        row = self._require_connection().execute(
            sql, (ns, conversation_id)
        ).fetchone()
        return {
            "attempts_total": int(row["attempts_total"] or 0),
            "attempts_open": int(row["attempts_open"] or 0),
            "attempts_known": int(row["attempts_known"] or 0),
            "attempts_unreadable": int(row["attempts_unreadable"] or 0),
            "prompt_tokens": int(row["prompt_tokens"] or 0),
            "completion_tokens": int(row["completion_tokens"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
        }

    def _turn_coverage_locked(
        self, ns: str, conversation_id: str
    ) -> tuple[int, int]:
        row = self._require_connection().execute(
            "SELECT COUNT(DISTINCT t.turn_id) AS turns_total, "
            "COUNT(DISTINCT CASE WHEN m.attempt_id IS NOT NULL THEN t.turn_id END) "
            "AS turns_with_attempts "
            "FROM collab_turns t "
            "LEFT JOIN collab_turn_stages s ON s.namespace = t.namespace "
            "AND s.turn_id = t.turn_id "
            "LEFT JOIN collab_stage_contexts sc ON sc.namespace = s.namespace "
            "AND sc.stage_id = s.stage_id "
            "LEFT JOIN model_attempts m ON m.namespace = t.namespace "
            "AND m.context_id = sc.context_id "
            "WHERE t.namespace = ? AND t.conversation_id = ?",
            (ns, conversation_id),
        ).fetchone()
        return int(row["turns_total"] or 0), int(row["turns_with_attempts"] or 0)

    def _usage_turn_locked(
        self, ns: str, conversation_id: str, turn_id: str | None
    ) -> TurnRow | None:
        if turn_id is None:
            row = self._require_connection().execute(
                "SELECT * FROM collab_turns WHERE namespace = ? "
                "AND conversation_id = ? ORDER BY turn_no DESC LIMIT 1",
                (ns, conversation_id),
            ).fetchone()
            return None if row is None else _turn_from_row(row)
        tid = _require_uuid(turn_id, "turn_id")
        turn = self._turn_locked(ns, tid)
        if turn.conversation_id != conversation_id:
            raise RecordNotFound(f"no turn {tid} in this conversation")
        return turn

    def _latest_attempt_record_locked(
        self, ns: str, conversation_id: str
    ) -> Any | None:
        row = self._require_connection().execute(
            self._ATTEMPT_SELECT
            + self._ATTEMPT_JOIN
            + "WHERE t.namespace = ? AND t.conversation_id = ? "
            "ORDER BY m.started_at DESC, m.attempt_no DESC, m.attempt_id DESC "
            "LIMIT 1",
            (ns, conversation_id),
        ).fetchone()
        return None if row is None else self._attempt_record_locked(row)

    def _previous_attempt_record_locked(self, ns: str, record: Any) -> Any | None:
        row = self._require_connection().execute(
            "SELECT m.*, s.kind AS stage_kind, sc.purpose, sc.ordinal, t.turn_id "
            "FROM model_attempts m "
            "JOIN collab_stage_contexts sc ON sc.namespace = m.namespace "
            "AND sc.context_id = m.context_id "
            "JOIN collab_turn_stages s ON s.namespace = sc.namespace "
            "AND s.stage_id = sc.stage_id "
            "JOIN collab_turns t ON t.namespace = s.namespace "
            "AND t.turn_id = s.turn_id "
            "WHERE m.namespace = ? AND m.context_id = ? AND m.attempt_no < ? "
            "ORDER BY m.attempt_no DESC LIMIT 1",
            (ns, record.context_id, record.attempt_no),
        ).fetchone()
        return None if row is None else self._attempt_record_locked(row)

    def _stage_contexts_locked(
        self, ns: str, turn_id: str
    ) -> list[dict[str, Any]]:
        rows = self._require_connection().execute(
            "SELECT sc.stage_id, sc.context_id, sc.ordinal, sc.purpose, "
            "sc.created_at FROM collab_stage_contexts sc "
            "JOIN collab_turn_stages s ON s.namespace = sc.namespace "
            "AND s.stage_id = sc.stage_id "
            "WHERE s.namespace = ? AND s.turn_id = ? "
            "ORDER BY CASE s.kind WHEN 'content' THEN 0 ELSE 1 END, sc.ordinal",
            (ns, turn_id),
        ).fetchall()
        return [
            {
                "stage_id": str(row["stage_id"]),
                "context_id": str(row["context_id"]),
                "ordinal": int(row["ordinal"]),
                "purpose": str(row["purpose"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def _stage_tool_counts_locked(
        self, ns: str, turn_id: str
    ) -> dict[str, dict[str, Any]]:
        rows = self._require_connection().execute(
            "SELECT stage_id, status, COUNT(*) AS count FROM collab_turn_steps "
            "WHERE namespace = ? AND turn_id = ? GROUP BY stage_id, status",
            (ns, turn_id),
        ).fetchall()
        counts: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = counts.setdefault(
                str(row["stage_id"]), {"total": 0, "by_status": {}}
            )
            value = int(row["count"] or 0)
            entry["total"] += value
            entry["by_status"][str(row["status"])] = value
        return counts

    def _final_block_locked(
        self,
        ns: str,
        turn_id: str,
        records: Sequence[Any],
        stages: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any] | None:
        row = self._require_connection().execute(
            "SELECT * FROM collab_turn_messages WHERE namespace = ? "
            "AND turn_id = ? AND kind = 'final' AND status = 'published'",
            (ns, turn_id),
        ).fetchone()
        if row is None:
            return None
        attempt_id: str | None = None
        if row["attempt_ref_json"] is not None:
            try:
                reference = strict_json_loads(row["attempt_ref_json"])
            except ValueError as exc:
                raise CorruptStore(
                    "stored message attempt reference is not strict JSON"
                ) from exc
            if isinstance(reference, dict) and isinstance(
                reference.get("attempt_id"), str
            ):
                attempt_id = reference["attempt_id"]
        if attempt_id is None:
            return {
                "message_id": str(row["message_id"]),
                "seq": int(row["seq"]),
                "published_at": row["published_at"],
                "attempt_id": None,
                "stage_kind": None,
                "source": "unknown",
                "fallback": False,
                "fallback_reason": None,
            }
        record = next(
            (item for item in records if item.attempt_id == attempt_id), None
        )
        stage_kind = None if record is None else record.stage_kind
        source = "unknown" if stage_kind is None else stage_kind
        expression = next(
            (item for item in stages if item["kind"] == "expression"), None
        )
        fallback = (
            source == "content"
            and expression is not None
            and expression["status"] in ("failed", "skipped")
        )
        reason: str | None = None
        if fallback:
            if expression["status"] == "skipped":
                reason = expression["skip_reason"]
            else:
                reason = self._stage_failed_reason_locked(ns, turn_id, "expression")
        return {
            "message_id": str(row["message_id"]),
            "seq": int(row["seq"]),
            "published_at": row["published_at"],
            "attempt_id": attempt_id,
            "stage_kind": stage_kind,
            "source": source,
            "fallback": fallback,
            "fallback_reason": reason,
        }

    def _stage_failed_reason_locked(
        self, ns: str, turn_id: str, stage_kind: str
    ) -> str | None:
        rows = self._require_connection().execute(
            "SELECT payload_json FROM collab_turn_events WHERE namespace = ? "
            "AND turn_id = ? AND kind = 'stage_failed' ORDER BY seq DESC",
            (ns, turn_id),
        ).fetchall()
        for row in rows:
            try:
                payload = strict_json_loads(row["payload_json"])
            except ValueError as exc:
                raise CorruptStore("stored stage event is not strict JSON") from exc
            if not isinstance(payload, dict):
                raise CorruptStore("stored stage event is not a JSON object")
            if payload.get("stage") != stage_kind:
                continue
            reason = payload.get("reason")
            return reason if isinstance(reason, str) else None
        return None

    def _turn_for_host_locked(self, ns: str, host: str, turn_id: str) -> TurnRow:
        row = self._require_connection().execute(
            "SELECT t.* FROM collab_turns t JOIN collab_conversations c "
            "ON c.namespace = t.namespace AND c.conversation_id = "
            "t.conversation_id WHERE t.namespace = ? AND t.turn_id = ? "
            "AND c.host_instance = ?",
            (ns, turn_id, host),
        ).fetchone()
        if row is None:
            raise RecordNotFound(f"no turn {turn_id} in this instance")
        return _turn_from_row(row)

    def _step_locked(self, ns: str, step_id: str) -> StepRow:
        row = self._require_connection().execute(
            "SELECT * FROM collab_turn_steps WHERE namespace = ? AND "
            "step_id = ?",
            (ns, step_id),
        ).fetchone()
        if row is None:
            raise RecordNotFound(f"no step {step_id} in {ns!r}")
        return _step_from_row(row)

    def _step_view_locked(self, ns: str, step: StepRow, access: Any) -> StepView:
        arguments: dict[str, Any] | None = None
        result_available = False
        result_excerpt: str | None = None
        result_truncated = False
        if (
            step.result_artifact_id is not None
            and step.result_artifact_version is not None
        ):
            try:
                loaded = self.load_artifact(
                    namespace=ns,
                    artifact_id=step.result_artifact_id,
                    artifact_version=step.result_artifact_version,
                )
                privacy = self._artifact_privacy_classes(
                    namespace=ns,
                    artifact_id=step.result_artifact_id,
                    artifact_version=step.result_artifact_version,
                    depth=0,
                    seen=set(),
                )
                allowed = privacy is not None and privacy.issubset(
                    set(access.privacy_classes)
                )
                if allowed and loaded.body:
                    document = strict_json_loads(loaded.body)
                    if isinstance(document, dict):
                        raw_arguments = document.get("arguments")
                        if isinstance(raw_arguments, dict):
                            arguments = dict(raw_arguments)
                        text = canonical_json_text(document.get("result"))
                        result_available = True
                        if len(text) > MAX_STEP_DETAIL_CHARS:
                            result_excerpt = text[:MAX_STEP_DETAIL_CHARS]
                            result_truncated = True
                        else:
                            result_excerpt = text
            except (Redacted, RecordNotFound, ValueError):
                result_available = False
        return StepView(
            step=step,
            arguments=arguments,
            result_available=result_available,
            result_excerpt=result_excerpt,
            result_truncated=result_truncated,
        )

    # ------------------------------------------------------------------ reads

    def read_turns(
        self,
        *,
        namespace: str,
        host_instance: str,
        conversation_id: str,
        limit: int = DEFAULT_TURN_LIMIT,
    ) -> tuple[TurnDetail, ...]:
        ns = identifiers.require_namespace(namespace)
        host = _require_text(host_instance, "host_instance", 256)
        cid = _require_uuid(conversation_id, "conversation_id")
        if type(limit) is not int or not 1 <= limit <= MAX_TURN_LIMIT:
            raise InvalidInput(f"limit must be 1..{MAX_TURN_LIMIT}")
        self._conversation_locked(ns, host, cid)
        rows = self._require_connection().execute(
            "SELECT * FROM collab_turns WHERE namespace = ? AND "
            "conversation_id = ? ORDER BY turn_no DESC LIMIT ?",
            (ns, cid, limit),
        ).fetchall()
        turns = [_turn_from_row(row) for row in reversed(rows)]
        reasons = self._failure_reasons_locked(ns, [turn.turn_id for turn in turns])
        return tuple(
            TurnDetail(turn=turn, failure_reason=reasons.get(turn.turn_id))
            for turn in turns
        )

    def read_turns_by_ids(
        self,
        *,
        namespace: str,
        host_instance: str,
        conversation_id: str,
        turn_ids: Iterable[str],
    ) -> tuple[TurnDetail, ...]:
        """Read specific turns of one conversation, oldest first.

        RG08 card C2: a page of messages may reach turns older than the latest
        50-turn window.  The page reader needs those exact turn_no/state rows to
        group and order the messages it already returned.  The ids are scoped to
        the conversation and bounded by the message-page limit.
        """

        ns = identifiers.require_namespace(namespace)
        host = _require_text(host_instance, "host_instance", 256)
        cid = _require_uuid(conversation_id, "conversation_id")
        unique: list[str] = []
        seen: set[str] = set()
        for value in turn_ids:
            turn_id = _require_uuid(value, "turn_id")
            if turn_id not in seen:
                seen.add(turn_id)
                unique.append(turn_id)
        if not unique:
            return ()
        if len(unique) > MAX_MESSAGE_LIMIT:
            raise InvalidInput(
                f"at most {MAX_MESSAGE_LIMIT} turn ids may be read at once"
            )
        self._conversation_locked(ns, host, cid)
        placeholders = ", ".join("?" for _ in unique)
        rows = self._require_connection().execute(
            "SELECT * FROM collab_turns WHERE namespace = ? "
            "AND conversation_id = ? "
            f"AND turn_id IN ({placeholders}) ORDER BY turn_no",
            (ns, cid, *unique),
        ).fetchall()
        turns = [_turn_from_row(row) for row in rows]
        reasons = self._failure_reasons_locked(
            ns, [turn.turn_id for turn in turns]
        )
        return tuple(
            TurnDetail(turn=turn, failure_reason=reasons.get(turn.turn_id))
            for turn in turns
        )

    def turn_for_host(
        self, *, namespace: str, host_instance: str, turn_id: str
    ) -> TurnRow:
        """RG04: one turn scoped to its owning conversation's host instance."""

        ns = identifiers.require_namespace(namespace)
        host = _require_text(host_instance, "host_instance", 256)
        return self._turn_for_host_locked(ns, host, _require_uuid(turn_id, "turn_id"))

    def list_terminal_turns(
        self,
        *,
        namespace: str,
        host_instance: str,
        limit: int = 50,
    ) -> tuple[TurnRow, ...]:
        """RG04: terminal turns of this host, oldest completion first.

        Oldest-first lets a bounded sweep catch up after a crash without
        starving newly completed turns.  This is a read only: registration is
        the terminal turn row itself.
        """

        ns = identifiers.require_namespace(namespace)
        host = _require_text(host_instance, "host_instance", 256)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidInput("limit must be an integer between 1 and 200")
        placeholders = ", ".join("?" for _ in TERMINAL_TURN_STATES)
        rows = self._require_connection().execute(
            "SELECT t.* FROM collab_turns AS t "
            "JOIN collab_conversations AS c ON c.namespace = t.namespace "
            "AND c.conversation_id = t.conversation_id "
            f"WHERE t.namespace = ? AND c.host_instance = ? "
            f"AND t.state IN ({placeholders}) "
            "ORDER BY COALESCE(t.completed_at, t.updated_at), t.turn_id LIMIT ?",
            (ns, host, *TERMINAL_TURN_STATES, limit),
        ).fetchall()
        return tuple(_turn_from_row(row) for row in rows)

    def read_messages(
        self,
        *,
        namespace: str,
        host_instance: str,
        conversation_id: str,
        access: Any,
        before: tuple[int, int] | None = None,
        limit: int = DEFAULT_MESSAGE_LIMIT,
    ) -> MessagePage:
        ns = identifiers.require_namespace(namespace)
        host = _require_text(host_instance, "host_instance", 256)
        cid = _require_uuid(conversation_id, "conversation_id")
        if type(limit) is not int or not 1 <= limit <= MAX_MESSAGE_LIMIT:
            raise InvalidInput(f"limit must be 1..{MAX_MESSAGE_LIMIT}")
        self._conversation_locked(ns, host, cid)
        parameters: list[Any] = [ns, cid, ns, host]
        where = (
            "WHERE m.namespace = ? AND t.conversation_id = ? "
            "AND c.namespace = ? AND c.host_instance = ? "
            "AND m.status = 'published'"
        )
        if before is not None:
            before_turn, before_seq = _require_position(before)
            where += (
                " AND (t.turn_no < ? OR (t.turn_no = ? AND m.seq < ?))"
            )
            parameters.extend([before_turn, before_turn, before_seq])
        rows = self._require_connection().execute(
            "SELECT m.*, t.turn_no FROM collab_turn_messages m "
            "JOIN collab_turns t ON t.namespace = m.namespace "
            "AND t.turn_id = m.turn_id "
            "JOIN collab_conversations c ON c.namespace = t.namespace "
            "AND c.conversation_id = t.conversation_id "
            + where
            + " ORDER BY t.turn_no DESC, m.seq DESC LIMIT ?",
            (*parameters, limit + 1),
        ).fetchall()
        next_before: tuple[int, int] | None = None
        selected = list(rows)
        if len(selected) > limit:
            selected = selected[:limit]
            oldest = selected[-1]
            next_before = (int(oldest["turn_no"]), int(oldest["seq"]))
        views = [self._message_view_locked(ns, row, access) for row in reversed(selected)]
        return MessagePage(messages=tuple(views), next_before=next_before)

    def turn_result(
        self, *, namespace: str, host_instance: str, turn_id: str, access: Any
    ) -> TurnResultView:
        ns = identifiers.require_namespace(namespace)
        host = _require_text(host_instance, "host_instance", 256)
        tid = _require_uuid(turn_id, "turn_id")
        row = self._require_connection().execute(
            "SELECT t.* FROM collab_turns t JOIN collab_conversations c "
            "ON c.namespace = t.namespace AND c.conversation_id = "
            "t.conversation_id WHERE t.namespace = ? AND t.turn_id = ? "
            "AND c.host_instance = ?",
            (ns, tid, host),
        ).fetchone()
        if row is None:
            raise RecordNotFound(f"no turn {tid} in this instance")
        turn = _turn_from_row(row)
        final = None
        wait_question = None
        messages = self._require_connection().execute(
            "SELECT m.*, t.turn_no FROM collab_turn_messages m "
            "JOIN collab_turns t ON t.namespace = m.namespace "
            "AND t.turn_id = m.turn_id WHERE m.namespace = ? AND "
            "m.turn_id = ? AND m.status = 'published' ORDER BY m.seq",
            (ns, tid),
        ).fetchall()
        for message in messages:
            view = self._message_view_locked(ns, message, access)
            if view.kind == "final":
                final = view
            elif view.kind == "wait_question":
                wait_question = view
        reasons = self._failure_reasons_locked(ns, [tid])
        return TurnResultView(
            turn=turn,
            final=final,
            wait_question=wait_question,
            failure_reason=reasons.get(tid),
        )

    # ------------------------------------------------------------- history

    def read_history(
        self,
        *,
        access: Any,
        namespace: str,
        conversation_id: str,
        before_turn_no: int,
        max_turns: int = 8,
        max_bytes: int = 8192,
    ) -> HistoryBundle:
        """The bounded, exact historical messages a new Context may render."""

        ns = identifiers.require_namespace(namespace)
        cid = _require_uuid(conversation_id, "conversation_id")
        if type(before_turn_no) is not int or before_turn_no < 1:
            raise InvalidInput("before_turn_no must be a positive integer")
        if type(max_turns) is not int or not 1 <= max_turns <= MAX_HISTORY_TURNS:
            raise InvalidInput(f"max_turns must be 1..{MAX_HISTORY_TURNS}")
        if type(max_bytes) is not int or not 1024 <= max_bytes <= MAX_HISTORY_BYTES:
            raise InvalidInput(f"max_bytes must be 1024..{MAX_HISTORY_BYTES}")
        conversation = self._conversation_locked(ns, access.host_instance, cid)
        del conversation
        total_prior = int(
            self._require_connection().execute(
                "SELECT COUNT(*) FROM collab_turns WHERE namespace = ? AND "
                "conversation_id = ? AND turn_no < ?",
                (ns, cid, before_turn_no),
            ).fetchone()[0]
        )
        turn_rows = self._require_connection().execute(
            "SELECT * FROM collab_turns WHERE namespace = ? AND "
            "conversation_id = ? AND turn_no < ? ORDER BY turn_no DESC LIMIT ?",
            (ns, cid, before_turn_no, max_turns),
        ).fetchall()
        groups: list[tuple[TurnRow, list[HistoryMessage], list[dict[str, Any]]]] = []
        for row in reversed(turn_rows):
            turn = _turn_from_row(row)
            messages: list[HistoryMessage] = []
            omissions: list[dict[str, Any]] = []
            for message in self._require_connection().execute(
                "SELECT * FROM collab_turn_messages WHERE namespace = ? "
                "AND turn_id = ? AND status = 'published' ORDER BY seq",
                (ns, turn.turn_id),
            ).fetchall():
                view = self._history_message_locked(ns, message, turn.turn_no, access)
                if view is None:
                    omissions.append(
                        {
                            "reason": "history_unavailable",
                            "turn_no": turn.turn_no,
                            "seq": int(message["seq"]),
                        }
                    )
                    continue
                messages.append(view)
            if turn.state in ("failed", "cancelled", "interrupted") and messages:
                omissions.append(
                    {
                        "reason": "turn_unfinished",
                        "turn_no": turn.turn_no,
                        "state": turn.state,
                    }
                )
            if messages:
                groups.append((turn, messages, omissions))

        selected: list[tuple[TurnRow, list[HistoryMessage], list[dict[str, Any]]]] = []
        used = 0
        for index, group in enumerate(reversed(groups)):
            turn, messages, _ = group
            size = sum(len(item.text.encode("utf-8")) for item in messages) + (
                HISTORY_OVERHEAD_BYTES * len(messages)
            )
            is_newest = index == 0 and not selected
            if not is_newest and used + size > max_bytes:
                continue
            selected.insert(0, group)
            used += size
        dropped = groups[: len(groups) - len(selected)]
        omissions: list[dict[str, Any]] = []
        dropped_messages = 0
        for _turn, messages, group_omissions in dropped:
            dropped_messages += len(messages)
            omissions.extend(group_omissions)
        if dropped:
            omissions.append(
                {
                    "reason": "history_byte_limit",
                    "dropped_turns": len(dropped),
                    "dropped_messages": dropped_messages,
                }
            )
        turn_dropped = max(0, total_prior - len(turn_rows))
        if turn_dropped:
            oldest_kept = (
                int(turn_rows[-1]["turn_no"]) if turn_rows else before_turn_no
            )
            limit_messages = int(
                self._require_connection().execute(
                    "SELECT COUNT(*) FROM collab_turn_messages m "
                    "JOIN collab_turns t ON t.namespace = m.namespace "
                    "AND t.turn_id = m.turn_id WHERE t.namespace = ? "
                    "AND t.conversation_id = ? AND t.turn_no < ? "
                    "AND m.status = 'published'",
                    (ns, cid, oldest_kept),
                ).fetchone()[0]
            )
            omissions.append(
                {
                    "reason": "history_turn_limit",
                    "dropped_turns": turn_dropped,
                    "dropped_messages": limit_messages,
                }
            )
        flat: list[HistoryMessage] = []
        for _turn, messages, group_omissions in selected:
            flat.extend(messages)
            omissions.extend(group_omissions)
        if selected:
            newest = selected[-1][0]
            newest_size = sum(
                len(item.text.encode("utf-8")) for item in selected[-1][1]
            )
            if newest_size > max_bytes:
                omissions.append(
                    {"reason": "history_over_budget", "turn_no": newest.turn_no}
                )
        stats = {
            "limit_turns": max_turns,
            "limit_bytes": max_bytes,
            "history_bytes": used,
            "history_turns": len(selected),
            "history_messages": len(flat),
            "dropped_turns": len(dropped) + turn_dropped,
            "dropped_messages": dropped_messages
            + (limit_messages if turn_dropped else 0),
        }
        return HistoryBundle(
            messages=tuple(flat),
            omissions=tuple(_dedupe_omissions(omissions)),
            stats=stats,
        )

    def register_context_history(
        self,
        *,
        namespace: str,
        context_id: str,
        messages: Sequence[HistoryMessage],
    ) -> int:
        """Persist the exact history rows inside the Context insert transaction."""

        if self._tx_depth == 0:
            raise RuntimeError(
                "register_context_history must run inside the Context insert transaction"
            )
        ns = identifiers.require_namespace(namespace)
        cid = _require_uuid(context_id, "context_id")
        row = self._require_connection().execute(
            "SELECT namespace FROM context_snapshots WHERE context_id = ?",
            (cid,),
        ).fetchone()
        if row is None:
            raise RecordNotFound(f"no persisted context {cid!r}")
        if row["namespace"] != ns:
            raise CorruptStore("context history names another namespace")
        now = utc_now_rfc3339()
        count = 0
        for ordinal, message in enumerate(messages, start=1):
            if not isinstance(message, HistoryMessage):
                raise InvalidInput("history entries must be HistoryMessage values")
            if message.ref_kind not in (COLLAB_INPUT, COLLAB_ARTIFACT):
                raise InvalidInput("history ref_kind must be a collaboration ref")
            self._require_history_ref_live(
                ns, message.ref_kind, message.ref_id, message.ref_version
            )
            self._require_connection().execute(
                "INSERT INTO collab_context_history (namespace, context_id, "
                "ordinal, ref_kind, ref_id, ref_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ns,
                    cid,
                    ordinal,
                    message.ref_kind,
                    _require_uuid(message.ref_id, "ref_id"),
                    _require_version(message.ref_version, "ref_version"),
                    now,
                ),
            )
            count += 1
        return count

    def context_history_rows(
        self, *, namespace: str, context_id: str
    ) -> tuple[dict[str, Any], ...]:
        ns = identifiers.require_namespace(namespace)
        cid = _require_uuid(context_id, "context_id")
        rows = self._require_connection().execute(
            "SELECT ordinal, ref_kind, ref_id, ref_version FROM "
            "collab_context_history WHERE namespace = ? AND context_id = ? "
            "ORDER BY ordinal",
            (ns, cid),
        ).fetchall()
        return tuple(
            {
                "ordinal": int(row["ordinal"]),
                "ref_kind": row["ref_kind"],
                "ref_id": row["ref_id"],
                "ref_version": int(row["ref_version"]),
            }
            for row in rows
        )

    def require_context_history_live(
        self, *, namespace: str, context_id: str, access: Any
    ) -> None:
        """The C09 re-check for the history rows of one Context.

        Only metadata is read (existence, barrier, privacy, non-empty payload);
        the rendered body is never decoded here.
        """

        ns = identifiers.require_namespace(namespace)
        cid = _require_uuid(context_id, "context_id")
        if getattr(access, "namespace", None) != ns:
            raise ContextStale("history access belongs to another namespace")
        rows = self._require_connection().execute(
            "SELECT ref_kind, ref_id, ref_version FROM collab_context_history "
            "WHERE namespace = ? AND context_id = ? ORDER BY ordinal",
            (ns, cid),
        ).fetchall()
        for row in rows:
            kind = row["ref_kind"]
            ref_id = str(row["ref_id"])
            version = int(row["ref_version"])
            try:
                self._require_history_ref_live(ns, kind, ref_id, version)
            except (RecordNotFound, Redacted) as exc:
                raise ContextStale(
                    f"history source {kind} {ref_id} is no longer readable"
                ) from exc
            if kind == COLLAB_INPUT:
                found = self._require_connection().execute(
                    "SELECT privacy_class FROM collab_inputs WHERE namespace = ? "
                    "AND input_id = ? AND content_version = ?",
                    (ns, ref_id, version),
                ).fetchone()
                if found is None:
                    raise ContextStale("history input disappeared")
                if found["privacy_class"] not in set(access.privacy_classes):
                    raise ContextStale("history input is outside the access policy")
                continue
            privacy = self._artifact_privacy_classes(
                namespace=ns,
                artifact_id=ref_id,
                artifact_version=version,
                depth=0,
                seen=set(),
            )
            if privacy is None or not privacy.issubset(set(access.privacy_classes)):
                raise ContextStale("history artifact is outside the access policy")

    # ------------------------------------------------------------- internals

    def _conversation_locked(
        self, ns: str, host: str, conversation_id: str
    ) -> ConversationRow:
        row = self._require_connection().execute(
            "SELECT * FROM collab_conversations WHERE namespace = ? "
            "AND host_instance = ? AND conversation_id = ?",
            (ns, host, conversation_id),
        ).fetchone()
        if row is None:
            raise RecordNotFound(f"no conversation {conversation_id} in this instance")
        return _conversation_from_row(row)

    def _turn_locked(self, ns: str, turn_id: str) -> TurnRow:
        row = self._require_connection().execute(
            "SELECT * FROM collab_turns WHERE namespace = ? AND turn_id = ?",
            (ns, turn_id),
        ).fetchone()
        if row is None:
            raise RecordNotFound(f"no turn {turn_id} in {ns!r}")
        return _turn_from_row(row)

    def _stage_locked(self, ns: str, stage_id: str) -> StageRow:
        row = self._require_connection().execute(
            "SELECT * FROM collab_turn_stages WHERE namespace = ? AND stage_id = ?",
            (ns, stage_id),
        ).fetchone()
        if row is None:
            raise RecordNotFound(f"no stage {stage_id} in {ns!r}")
        return _stage_from_row(row)

    def _touch_conversation_locked(self, ns: str, conversation_id: str) -> None:
        self._require_connection().execute(
            "UPDATE collab_conversations SET updated_at = ? WHERE namespace = ? "
            "AND conversation_id = ?",
            (utc_now_rfc3339(), ns, conversation_id),
        )

    def _interaction_for_turn_locked(self, ns: str, turn_id: str) -> str | None:
        row = self._require_connection().execute(
            "SELECT i.interaction_id FROM collab_interactions i "
            "JOIN collab_stage_contexts sc ON sc.namespace = i.namespace "
            "AND sc.context_id = i.context_id "
            "JOIN collab_turn_stages st ON st.namespace = sc.namespace "
            "AND st.stage_id = sc.stage_id WHERE st.turn_id = ? "
            "AND i.namespace = ? AND i.mode = 'consult' LIMIT 1",
            (turn_id, ns),
        ).fetchone()
        return None if row is None else str(row["interaction_id"])

    def _append_turn_event_locked(
        self, ns: str, turn_id: str, kind: str, payload: Mapping[str, Any]
    ) -> TurnEventRow:
        """Append one event and return its row (same transaction as the state)."""

        label = _require_text(kind, "kind", 64)
        body = _require_mapping(payload, "payload")
        text = canonical_json_text(body)
        if len(text) > MAX_EVENT_PAYLOAD:
            raise InvalidInput("turn event payload is too large")
        connection = self._require_connection()
        cursor = connection.execute(
            "UPDATE collab_turns SET last_event_seq = last_event_seq + 1 "
            "WHERE namespace = ? AND turn_id = ?",
            (ns, turn_id),
        )
        if cursor.rowcount != 1:
            raise RecordNotFound(f"no turn {turn_id} in {ns!r}")
        seq = int(
            connection.execute(
                "SELECT last_event_seq FROM collab_turns WHERE namespace = ? "
                "AND turn_id = ?",
                (ns, turn_id),
            ).fetchone()["last_event_seq"]
        )
        event_id = str(uuid.uuid4())
        created_at = utc_now_rfc3339()
        connection.execute(
            "INSERT INTO collab_turn_events (namespace, turn_id, seq, event_id, "
            "kind, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ns, turn_id, seq, event_id, label, text, created_at),
        )
        return TurnEventRow(
            namespace=ns,
            turn_id=turn_id,
            seq=seq,
            event_id=event_id,
            kind=label,
            payload=dict(body),
            created_at=created_at,
        )

    def _record_turn_event_locked(
        self, ns: str, turn_id: str, kind: str, payload: Mapping[str, Any]
    ) -> int:
        return self._append_turn_event_locked(ns, turn_id, kind, payload).seq

    def _failure_reasons_locked(
        self, ns: str, turn_ids: Sequence[str]
    ) -> dict[str, str]:
        if not turn_ids:
            return {}
        placeholders = ", ".join("?" for _ in turn_ids)
        rows = self._require_connection().execute(
            "SELECT turn_id, payload_json FROM collab_turn_events "
            "WHERE namespace = ? AND kind IN ('turn_failed', 'turn_interrupted') "
            f"AND turn_id IN ({placeholders}) ORDER BY turn_id, seq",
            (ns, *turn_ids),
        ).fetchall()
        reasons: dict[str, str] = {}
        for row in rows:
            payload = strict_json_loads(row["payload_json"])
            if isinstance(payload, dict) and isinstance(payload.get("reason"), str):
                reasons[str(row["turn_id"])] = payload["reason"]
        return reasons

    def _message_row_locked(self, ns: str, message_id: str) -> MessageRow:
        row = self._require_connection().execute(
            "SELECT * FROM collab_turn_messages WHERE namespace = ? "
            "AND message_id = ?",
            (ns, message_id),
        ).fetchone()
        if row is None:
            raise RecordNotFound(f"no message {message_id} in {ns!r}")
        return _message_from_row(row)

    def _message_view_locked(
        self, ns: str, row: Any, access: Any
    ) -> MessageView:
        message = _message_from_row(row)
        text: str | None = None
        parts: tuple[str, ...] = ()
        redacted = False
        try:
            if message.kind == "user":
                ref = message.input_ref or {}
                loaded = self.load_input(
                    namespace=ns,
                    input_id=str(ref.get("input_id")),
                    content_version=int(ref.get("content_version") or 0),
                )
                if loaded.privacy_class not in set(access.privacy_classes):
                    redacted = True
                else:
                    text = loaded.body
            else:
                if message.artifact_id is None or message.artifact_version is None:
                    raise CorruptStore("assistant message has no artifact")
                loaded = self.load_artifact(
                    namespace=ns,
                    artifact_id=message.artifact_id,
                    artifact_version=message.artifact_version,
                )
                privacy = self._artifact_privacy_classes(
                    namespace=ns,
                    artifact_id=message.artifact_id,
                    artifact_version=message.artifact_version,
                    depth=0,
                    seen=set(),
                )
                if privacy is None or not privacy.issubset(
                    set(access.privacy_classes)
                ):
                    redacted = True
                else:
                    parts, text = decode_reply_body(loaded.body)
        except (Redacted, RecordNotFound):
            redacted = True
        return MessageView(
            message_id=message.message_id,
            turn_id=message.turn_id,
            seq=message.seq,
            kind=message.kind,
            text=text,
            redacted=redacted,
            created_at=message.created_at,
            messages=() if len(parts) < 2 else parts,
        )

    def _history_message_locked(
        self, ns: str, row: Any, turn_no: int, access: Any
    ) -> HistoryMessage | None:
        message = _message_from_row(row)
        try:
            if message.kind == "user":
                ref = message.input_ref or {}
                loaded = self.load_input(
                    namespace=ns,
                    input_id=str(ref.get("input_id")),
                    content_version=int(ref.get("content_version") or 0),
                )
                if loaded.privacy_class not in set(access.privacy_classes):
                    return None
                return HistoryMessage(
                    role="user",
                    text=loaded.body,
                    ref_kind=COLLAB_INPUT,
                    ref_id=loaded.input_id,
                    ref_version=loaded.content_version,
                    turn_no=turn_no,
                    seq=message.seq,
                    created_at=_message_created_at(row),
                )
            if message.artifact_id is None or message.artifact_version is None:
                raise CorruptStore("assistant message has no artifact")
            loaded = self.load_artifact(
                namespace=ns,
                artifact_id=message.artifact_id,
                artifact_version=message.artifact_version,
            )
            privacy = self._artifact_privacy_classes(
                namespace=ns,
                artifact_id=message.artifact_id,
                artifact_version=message.artifact_version,
                depth=0,
                seen=set(),
            )
            if privacy is None or not privacy.issubset(set(access.privacy_classes)):
                return None
            if not loaded.body:
                return None
            _, joined = decode_reply_body(loaded.body)
            return HistoryMessage(
                role="assistant",
                text=joined,
                ref_kind=COLLAB_ARTIFACT,
                ref_id=loaded.artifact_id,
                ref_version=loaded.artifact_version,
                created_at=_message_created_at(row),
                turn_no=turn_no,
                seq=message.seq,
            )
        except (Redacted, RecordNotFound):
            return None

    def _require_history_ref_live(
        self, ns: str, kind: str, ref_id: str, ref_version: int
    ) -> None:
        connection = self._require_connection()
        identifier = _require_uuid(ref_id, "ref_id")
        version = _require_version(ref_version, "ref_version")
        if kind == COLLAB_INPUT:
            row = connection.execute(
                "SELECT i.body_payload_ref, p.body_json FROM collab_inputs i "
                "JOIN private_payloads p ON p.payload_ref = i.body_payload_ref "
                "WHERE i.namespace = ? AND i.input_id = ? "
                "AND i.content_version = ?",
                (ns, identifier, version),
            ).fetchone()
        elif kind == COLLAB_ARTIFACT:
            row = connection.execute(
                "SELECT a.body_payload_ref, p.body_json FROM collab_artifacts a "
                "JOIN private_payloads p ON p.payload_ref = a.body_payload_ref "
                "WHERE a.namespace = ? AND a.artifact_id = ? "
                "AND a.artifact_version = ?",
                (ns, identifier, version),
            ).fetchone()
        else:
            raise InvalidInput("history ref must be a collab input or artifact")
        if row is None:
            raise RecordNotFound(
                f"history source {kind} {identifier} version {version} is missing"
            )
        if not row["body_json"]:
            raise Redacted(f"history source {kind} {identifier} was cleared")
        if self._source_barred_locked(
            namespace=ns, kind=kind, ref_id=identifier, version=version
        ):
            raise Redacted(f"history source {kind} {identifier} is inside a barrier")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _conversation_from_row(row: Any) -> ConversationRow:
    return ConversationRow(
        conversation_id=row["conversation_id"],
        namespace=row["namespace"],
        host_instance=row["host_instance"],
        mode=row["mode"],
        title=row["title"],
        next_turn_no=int(row["next_turn_no"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _turn_from_row(row: Any) -> TurnRow:
    return TurnRow(
        turn_id=row["turn_id"],
        namespace=row["namespace"],
        conversation_id=row["conversation_id"],
        turn_no=int(row["turn_no"]),
        client_request_id=row["client_request_id"],
        client_request_digest=row["client_request_digest"],
        input_ref=_decode_object(row["input_ref_json"], "input_ref_json"),
        state=row["state"],
        profile_ref=_decode_object(row["profile_ref_json"], "profile_ref_json"),
        mode=row["mode"],
        strategy=_decode_object(row["strategy_json"], "strategy_json"),
        last_event_seq=int(row["last_event_seq"]),
        final_message_ref=row["final_message_ref"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def _stage_from_row(row: Any) -> StageRow:
    return StageRow(
        stage_id=row["stage_id"],
        namespace=row["namespace"],
        turn_id=row["turn_id"],
        kind=row["kind"],
        status=row["status"],
        policy_version=row["policy_version"],
        skip_reason=row["skip_reason"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _message_created_at(row: Any) -> str | None:
    """The stored message time, or None.  Never derived from another column."""

    try:
        value = row["created_at"]
    except (KeyError, IndexError, TypeError):
        return None
    return value if isinstance(value, str) and value else None


def _message_from_row(row: Any) -> MessageRow:
    return MessageRow(
        message_id=row["message_id"],
        namespace=row["namespace"],
        turn_id=row["turn_id"],
        seq=int(row["seq"]),
        kind=row["kind"],
        input_ref=(
            None
            if row["input_ref_json"] is None
            else _decode_object(row["input_ref_json"], "input_ref_json")
        ),
        artifact_id=row["artifact_id"],
        artifact_version=(
            None
            if row["artifact_version"] is None
            else int(row["artifact_version"])
        ),
        attempt_ref=(
            None
            if row["attempt_ref_json"] is None
            else _decode_object(row["attempt_ref_json"], "attempt_ref_json")
        ),
        status=row["status"],
        published_at=row["published_at"],
        created_at=row["created_at"],
    )


def _decode_object(text: str | None, field: str) -> dict[str, Any]:
    if text is None:
        raise CorruptStore(f"stored {field} is NULL")
    try:
        value = strict_json_loads(text)
    except ValueError as exc:
        raise CorruptStore(f"stored {field} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise CorruptStore(f"stored {field} is not a JSON object")
    return value


def _turn_digest(conversation_id: str, text: str, mode: str) -> str:
    return sha256_hex(
        canonical_json_text(
            {"conversation_id": conversation_id, "text": text, "mode": mode}
        )
    )


def _require_profile_ref(value: Any, namespace: str) -> dict[str, Any]:
    ref = _require_mapping(value, "profile_ref")
    if ref.get("ref_type") != "record":
        raise InvalidInput("profile_ref must be a record reference")
    if ref.get("namespace") != namespace:
        raise InvalidInput("profile_ref belongs to another namespace")
    _require_uuid(ref.get("record_id"), "profile_ref.record_id")
    _require_version(ref.get("record_version"), "profile_ref.record_version")
    return dict(ref)


def _fire_events(
    on_event: Callable[[TurnEventRow, Mapping[str, Any] | None], None] | None,
    pending: Sequence[tuple[TurnEventRow, Mapping[str, Any] | None]],
) -> None:
    """Fire committed-event callbacks; called strictly after the transaction."""

    if on_event is None:
        return
    for row, live in pending:
        on_event(row, live)


def _event_from_row(row: Any) -> TurnEventRow:
    payload = strict_json_loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise CorruptStore("a stored turn event payload is not an object")
    return TurnEventRow(
        namespace=row["namespace"],
        turn_id=row["turn_id"],
        seq=int(row["seq"]),
        event_id=row["event_id"],
        kind=row["kind"],
        payload=payload,
        created_at=row["created_at"],
    )


def _step_from_row(row: Any) -> StepRow:
    return StepRow(
        step_id=row["step_id"],
        namespace=row["namespace"],
        turn_id=row["turn_id"],
        stage_id=row["stage_id"],
        step_no=int(row["step_no"]),
        tool_name=row["tool_name"],
        arguments_digest=row["arguments_digest"],
        result_artifact_id=row["result_artifact_id"],
        result_artifact_version=(
            None
            if row["result_artifact_version"] is None
            else int(row["result_artifact_version"])
        ),
        result_digest=row["result_digest"],
        operation_key=row["operation_key"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error_label=row["error_label"],
    )

def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidInput(f"{field} must be an object")
    return dict(value)


def _require_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str) or identifiers.UUID_PATTERN.fullmatch(value) is None:
        raise InvalidInput(f"{field} must be a canonical lower-case UUID")
    return value


def _require_version(value: Any, field: str) -> int:
    if type(value) is not int or value < 1:
        raise InvalidInput(f"{field} must be a positive integer")
    return value


def _require_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise InvalidInput(f"{field} must be at most {maximum} characters")
    return value


def _require_timestamp(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or identifiers.TIMESTAMP_PATTERN.fullmatch(value) is None
    ):
        raise InvalidInput(f"{field} must be a UTC RFC 3339 timestamp ending in Z")
    return value


def _require_retention(value: Any) -> str:
    if value not in ("session", "standard", "durable"):
        raise InvalidInput("retention must be session, standard or durable")
    return value


def _require_position(value: Any) -> tuple[int, int]:
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 2
        or type(value[0]) is not int
        or type(value[1]) is not int
        or value[0] < 1
        or value[1] < 1
    ):
        raise InvalidInput("before must be a (turn_no, seq) pair")
    return int(value[0]), int(value[1])


def _encode_cursor(updated_at: str, conversation_id: str) -> str:
    return f"{updated_at}|{conversation_id}"


def _decode_cursor(cursor: str) -> tuple[str, str]:
    if not isinstance(cursor, str) or "|" not in cursor:
        raise InvalidInput("cursor is not a conversation cursor")
    updated_at, conversation_id = cursor.split("|", 1)
    _require_timestamp(updated_at, "cursor.updated_at")
    _require_uuid(conversation_id, "cursor.conversation_id")
    return updated_at, conversation_id


def _dedupe_omissions(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = canonical_json_text(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
