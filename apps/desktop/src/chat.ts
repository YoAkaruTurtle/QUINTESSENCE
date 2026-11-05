/**
 * RG02 conversation state machine (DOM-free).
 *
 * `state.ts` remains the host/destination/error reducer and is not modified.
 * This module owns everything that is specific to a persistent conversation:
 * the conversation list, the active transcript, the pending turn, the
 * idempotency request id and the BUSY/terminal transitions.
 *
 * The WebView never decides authority, retention or a turn's terminal state;
 * the last word always comes from `turn.result`.
 */

export type TurnState =
  | "accepted"
  | "running"
  | "waiting"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export interface ChatMessage {
  message_id: string;
  turn_id: string;
  seq: number;
  kind: "user" | "commentary" | "final" | "wait_question";
  text: string | null;
  redacted: boolean;
  created_at: string;
  /** RG08: ordered explicit assistant bubbles; absent for a legacy single reply. */
  messages?: string[];
}

export interface ConversationSummary {
  conversation_id: string;
  mode: "chat" | "work";
  title: string | null;
  created_at: string;
  updated_at: string;
  turn_count: number;
  last_turn_state: TurnState | null;
}

export interface ConversationTurn {
  turn_id: string;
  turn_no: number;
  state: TurnState;
  failure_reason: string | null;
}

export interface TurnResult {
  turn_id: string;
  conversation_id: string;
  turn_no: number;
  state: TurnState;
  terminal: boolean;
  final: ChatMessage | null;
  wait_question: ChatMessage | null;
  failure_reason: string | null;
}

export type MemoryState =
  | "not_configured"
  | "not_eligible"
  | "pending"
  | "outcome_unknown"
  | "delivered"
  | "needs_review"
  | "blocked"
  | "obsolete";

export interface PostprocessRef {
  ref: Record<string, unknown>;
  role: "user" | "assistant";
  kind: string;
  state: string;
  detail: string | null;
  next_retry_at: string | null;
  delivery_id: string | null;
}

export interface PostprocessTurnStatus {
  turn_id: string;
  turn_state: TurnState;
  completed_at: string | null;
  transcript_saved: boolean;
  memory: {
    state: MemoryState;
    detail: string | null;
    pending: number;
    delivered: number;
    refs: PostprocessRef[];
  };
}

export interface TranscriptSearchRef {
  ref_type: "collab_input" | "collab_artifact";
  namespace?: string;
  input_id?: string;
  content_version?: number;
  artifact_id?: string;
  artifact_version?: number;
}

export interface TranscriptSearchItem {
  source: "turn" | "legacy";
  created_at: string;
  conversation_id: string | null;
  turn_no: number | null;
  message_id: string | null;
  legacy_interaction_id: string | null;
  role: "user" | "assistant";
  kind: string;
  text: string;
  truncated: boolean;
  ref: TranscriptSearchRef;
}

export interface TranscriptSearchPage {
  items: TranscriptSearchItem[];
  next_cursor: string | null;
  scanned: number;
  scan_window: number;
  filtered: number;
}

const MEMORY_LABELS: Record<MemoryState, string> = {
  not_configured: "记忆未配置",
  not_eligible: "记忆不适用",
  pending: "记忆待交付",
  outcome_unknown: "记忆结果不明，待核对",
  delivered: "记忆已交付",
  needs_review: "记忆待人工核对",
  blocked: "记忆已阻断",
  obsolete: "记忆已失效",
};

export function memoryStateLabel(state: MemoryState): string {
  return MEMORY_LABELS[state] ?? "记忆状态未知";
}

/**
 * RG05-F01: should the UI keep refreshing `postprocess.status` for a moment?
 *
 * Only a still-pending delivery can change on its own; `needs_review`,
 * `blocked` and `obsolete` are terminal human states and must not poll.
 */
export function hasPendingMemory(turns: PostprocessTurnStatus[]): boolean {
  return turns.some(
    (turn) =>
      turn.memory.state === "pending" ||
      turn.memory.state === "outcome_unknown",
  );
}

/** One whitelisted public process event (already filtered by the host). */
export interface ProcessEvent {
  seq: number;
  event_id: string;
  type: string;
  at: string;
  kind?: string;
  text?: string;
  step_id?: string;
  step_no?: number;
  tool?: string;
  arguments_digest?: string;
  status?: string;
  duration_ms?: number;
  result_artifact_id?: string;
  result_artifact_version?: number;
  sizes?: Record<string, number>;
  stage?: string;
  message_seq?: number;
  message_id?: string;
  reason?: string;
  commentary_omitted?: string;
  commentary_source?: string;
  state?: string;
  attempt_ref?: unknown;
  arguments?: Record<string, unknown>;
  result_excerpt?: string;
}

export interface ChatState {
  status: "idle" | "loading" | "ready" | "error";
  conversations: ConversationSummary[];
  activeId: string | null;
  turns: ConversationTurn[];
  messages: ChatMessage[];
  /** Public process events of the active turn, ordered by `seq`. */
  events: ProcessEvent[];
  eventCursor: number;
  eventsTurnId: string | null;
  pendingTurnId: string | null;
  /** Per-turn postprocess status; keyed by turn id. */
  postprocess: Record<string, PostprocessTurnStatus>;
  /** The idempotency key bound to `pendingText`; kept across a failed send. */
  pendingRequestId: string | null;
  pendingText: string | null;
  /**
   * RG08 card C: the conversation that owns the local outgoing projection.
   * A projection is never shown in another conversation.
   */
  pendingConversationId: string | null;
  /**
   * The accepted turn this projection belongs to.  It survives a terminal
   * `turn.result` so the bubble keeps its slot until the host's official user
   * message is loaded by `conversation`.
   */
  pendingUserTurnId: string | null;
  /**
   * The host refused (or the bridge failed) before a turn/message existed.
   * Text and request id stay for an idempotent retry, but the local bubble is
   * hidden; the composer shows the restored draft instead.
   */
  pendingFailed: boolean;
  sending: boolean;
  busy: boolean;
  cancelRequested: boolean;
  error: { code: string; message: string } | null;
  nextBefore: { turn_no: number; seq: number } | null;
  /**
   * Card C2: at least one older page was merged for the active conversation.
   * The cursor then belongs to the oldest loaded page, not to the initial
   * latest-50 window.
   */
  historyLoaded: boolean;
  /** An older-page read is in flight; the entry must not fire twice. */
  loadingOlder: boolean;
  /** The last older-page read failed; `nextBefore` stays for an explicit retry. */
  olderError: { code: string; message: string } | null;
  /** No messages older than the loaded window remain. */
  olderExhausted: boolean;
}

export const INITIAL_CHAT_STATE: ChatState = {
  status: "idle",
  conversations: [],
  activeId: null,
  turns: [],
  messages: [],
  events: [],
  eventCursor: 0,
  eventsTurnId: null,
  pendingTurnId: null,
  postprocess: {},
  pendingRequestId: null,
  pendingText: null,
  pendingConversationId: null,
  pendingUserTurnId: null,
  pendingFailed: false,
  sending: false,
  busy: false,
  cancelRequested: false,
  error: null,
  nextBefore: null,
  historyLoaded: false,
  loadingOlder: false,
  olderError: null,
  olderExhausted: false,
};

export type ChatAction =
  | { type: "loading" }
  | { type: "error"; code: string; message: string }
  | { type: "conversations"; conversations: ConversationSummary[] }
  | {
      type: "conversation";
      conversationId: string;
      turns: ConversationTurn[];
      messages: ChatMessage[];
      nextBefore: { turn_no: number; seq: number } | null;
    }
  | { type: "select"; conversationId: string }
  | { type: "new-conversation"; conversationId: string }
  | { type: "draft-changed" }
  | { type: "send-start"; requestId: string; text: string }
  | {
      type: "send-accepted";
      turnId: string;
      turnNo?: number;
      state: TurnState;
    }
  | {
      type: "send-reused";
      turnId: string;
      turnNo?: number;
      state: TurnState;
    }
  | { type: "send-failed"; code: string; message: string }
  | { type: "busy"; busy: boolean }
  | { type: "turn-result"; result: TurnResult }
  | { type: "turn-not-terminal" }
  | { type: "turn-busy" }
  | {
      type: "events";
      turnId: string;
      events: ProcessEvent[];
      nextSeq: number;
      terminal: boolean;
    }
  | { type: "postprocess"; turns: PostprocessTurnStatus[] }
  | { type: "cancel-requested" }
  | { type: "cancel-refused"; code: string; message: string }
  | { type: "older-start"; conversationId: string }
  | {
      type: "older-page";
      conversationId: string;
      turns: ConversationTurn[];
      messages: ChatMessage[];
      nextBefore: { turn_no: number; seq: number } | null;
    }
  | {
      type: "older-failed";
      conversationId: string;
      code: string;
      message: string;
    }
  | { type: "older-cancelled"; conversationId: string }
  | { type: "clear-pending" };

export function chatReduce(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "loading":
      return { ...state, status: "loading", error: null };
    case "error":
      return {
        ...state,
        status: state.status === "ready" ? "ready" : "error",
        error: { code: action.code, message: action.message },
      };
    case "conversations":
      return { ...state, status: "ready", conversations: action.conversations };
    case "conversation": {
      const pageMessages = sortMessages(action.messages, action.turns);
      const pendingConfirmed = confirmedPendingMessage(
        state,
        action.conversationId,
        pageMessages,
      );
      // Card C2: a same-conversation refresh (terminal poll, explicit reload)
      // must not throw away pages the reader already loaded.  A real switch
      // arrives here only after `select`, which resets the history window.
      const preserving =
        state.historyLoaded && state.activeId === action.conversationId;
      const turns = preserving
        ? mergeTurns(state.turns, action.turns)
        : action.turns;
      const messages = preserving
        ? sortMessages(mergeMessages(state.messages, action.messages), turns)
        : pageMessages;
      return {
        ...state,
        status: "ready",
        activeId: action.conversationId,
        turns,
        messages,
        events: [],
        eventCursor: 0,
        eventsTurnId: null,
        cancelRequested: false,
        nextBefore: preserving ? state.nextBefore : action.nextBefore,
        error: null,
        historyLoaded: preserving ? state.historyLoaded : false,
        loadingOlder: preserving ? state.loadingOlder : false,
        olderError: preserving ? state.olderError : null,
        olderExhausted: preserving ? state.olderExhausted : false,
        // A reload may happen while a turn is still running; never drop the
        // pending turn id here or the poll would stop tracking it.
        pendingTurnId: state.pendingTurnId,
        pendingRequestId: pendingConfirmed ? null : state.pendingRequestId,
        pendingText: pendingConfirmed ? null : state.pendingText,
        pendingConversationId: pendingConfirmed
          ? null
          : state.pendingConversationId,
        pendingUserTurnId: pendingConfirmed ? null : state.pendingUserTurnId,
        pendingFailed: pendingConfirmed ? false : state.pendingFailed,
      };
    }
    case "older-start":
      if (action.conversationId !== state.activeId) {
        return state;
      }
      return {
        ...state,
        loadingOlder: true,
        olderError: null,
      };
    case "older-page": {
      if (action.conversationId !== state.activeId) {
        return state;
      }
      const turns = mergeTurns(state.turns, action.turns);
      const messages = sortMessages(
        mergeMessages(state.messages, action.messages),
        turns,
      );
      return {
        ...state,
        turns,
        messages,
        nextBefore: action.nextBefore,
        loadingOlder: false,
        olderError: null,
        historyLoaded: true,
        olderExhausted: action.nextBefore === null,
      };
    }
    case "older-failed":
      if (action.conversationId !== state.activeId) {
        return state;
      }
      return {
        ...state,
        loadingOlder: false,
        olderError: { code: action.code, message: action.message },
      };
    case "older-cancelled":
      if (action.conversationId !== state.activeId) {
        return state;
      }
      return { ...state, loadingOlder: false };
    case "select":
      return {
        ...state,
        activeId: action.conversationId,
        turns: [],
        messages: [],
        events: [],
        eventCursor: 0,
        eventsTurnId: null,
        cancelRequested: false,
        nextBefore: null,
        historyLoaded: false,
        loadingOlder: false,
        olderError: null,
        olderExhausted: false,
        error: null,
      };
    case "new-conversation":
      return {
        ...state,
        activeId: action.conversationId,
        turns: [],
        messages: [],
        events: [],
        eventCursor: 0,
        eventsTurnId: null,
        cancelRequested: false,
        nextBefore: null,
        historyLoaded: false,
        loadingOlder: false,
        olderError: null,
        olderExhausted: false,
        error: null,
      };
    case "draft-changed":
      if (state.sending) {
        return state;
      }
      // Editing a draft only invalidates the retry key of a failed send in
      // this conversation.  An accepted projection stays until its official
      // message is loaded, and another conversation's key is left alone.
      if (!state.pendingFailed) {
        return state;
      }
      if (
        state.pendingConversationId !== null &&
        state.pendingConversationId !== state.activeId
      ) {
        return state;
      }
      return {
        ...state,
        pendingRequestId: null,
        pendingText: null,
        pendingConversationId: null,
        pendingUserTurnId: null,
        pendingFailed: false,
      };
    case "send-start":
      return {
        ...state,
        sending: true,
        pendingRequestId: action.requestId,
        pendingText: action.text,
        pendingConversationId: state.activeId,
        pendingUserTurnId: null,
        pendingFailed: false,
        error: null,
      };
    case "send-accepted":
    case "send-reused":
      return {
        ...state,
        sending: false,
        pendingTurnId: action.turnId,
        pendingUserTurnId: action.turnId,
        pendingFailed: false,
        turns: upsertAcceptedTurn(
          state.turns,
          action.turnId,
          action.turnNo,
          action.state,
        ),
        events: [],
        eventCursor: 0,
        eventsTurnId: action.turnId,
        cancelRequested: false,
        error: null,
      };
    case "send-failed":
      return {
        ...state,
        sending: false,
        // Text and request id stay for an idempotent retry; only the local
        // bubble is retired so it cannot look like a committed message.
        pendingFailed: true,
        error: { code: action.code, message: action.message },
      };
    case "busy":
      return { ...state, busy: action.busy };
    case "turn-result": {
      const turns = mergeTurn(state.turns, action.result);
      return {
        ...state,
        pendingTurnId: action.result.terminal ? null : state.pendingTurnId,
        turns,
        messages: mergeFinal(state.messages, action.result, turns),
        cancelRequested: action.result.terminal ? false : state.cancelRequested,
      };
    }
    case "events":
      return {
        ...state,
        events:
          state.eventsTurnId === action.turnId
            ? mergeEvents(state.events, action.events)
            : mergeEvents([], action.events),
        eventCursor:
          state.eventsTurnId === action.turnId
            ? Math.max(state.eventCursor, action.nextSeq)
            : action.nextSeq,
        eventsTurnId: action.turnId,
      };
    case "postprocess": {
      const next = { ...state.postprocess };
      for (const turn of action.turns) {
        next[turn.turn_id] = turn;
      }
      return { ...state, postprocess: next };
    }
    case "cancel-requested":
      return { ...state, cancelRequested: true };
    case "cancel-refused":
      return {
        ...state,
        cancelRequested: false,
        error: { code: action.code, message: action.message },
      };
    case "turn-busy":
      // R2: a read was refused because a turn is running.  Not terminal.
      return { ...state, busy: true };
    case "turn-not-terminal":
      return state;
    case "clear-pending":
      return {
        ...state,
        pendingTurnId: null,
        pendingRequestId: null,
        pendingText: null,
        pendingConversationId: null,
        pendingUserTurnId: null,
        pendingFailed: false,
        cancelRequested: false,
      };
    default:
      return state;
  }
}

function mergeTurn(
  turns: ConversationTurn[],
  result: TurnResult,
): ConversationTurn[] {
  const next = turns.filter((item) => item.turn_id !== result.turn_id);
  next.push({
    turn_id: result.turn_id,
    turn_no: result.turn_no,
    state: result.state,
    failure_reason: result.failure_reason,
  });
  next.sort((left, right) => left.turn_no - right.turn_no);
  return next;
}

/**
 * Card C2: union of turn metadata for the initial message window and loaded
 * old pages.  An already-known turn wins: live polling owns its state, while a
 * page only contributes the older turns that were not loaded yet.
 */
function mergeTurns(
  existing: ConversationTurn[],
  incoming: ConversationTurn[],
): ConversationTurn[] {
  const byId = new Map(existing.map((turn) => [turn.turn_id, turn]));
  for (const turn of incoming) {
    if (!byId.has(turn.turn_id)) {
      byId.set(turn.turn_id, turn);
    }
  }
  return [...byId.values()].sort(
    (left, right) => left.turn_no - right.turn_no,
  );
}

/**
 * Card C2: merge one older page by message_id.  The first loaded copy wins, so
 * a page can never replace a live final with a stale projected body.
 */
function mergeMessages(
  existing: ChatMessage[],
  incoming: ChatMessage[],
): ChatMessage[] {
  const byId = new Map(
    existing.map((message) => [message.message_id, message]),
  );
  for (const message of incoming) {
    if (!byId.has(message.message_id)) {
      byId.set(message.message_id, message);
    }
  }
  return [...byId.values()];
}

/**
 * Card C: record the host-accepted turn before any message exists.
 *
 * The accepted reply carries the authoritative `turn_no`, so the projection can
 * be placed at its final position immediately instead of being prepended to the
 * thread.  An already-known turn is never rewritten (the poll owns its state).
 */
function upsertAcceptedTurn(
  turns: ConversationTurn[],
  turnId: string,
  turnNo: number | undefined,
  state: TurnState,
): ConversationTurn[] {
  if (turnNo === undefined) {
    return turns;
  }
  if (turns.some((turn) => turn.turn_id === turnId)) {
    return turns;
  }
  const next: ConversationTurn[] = [
    ...turns,
    {
      turn_id: turnId,
      turn_no: turnNo,
      state,
      failure_reason: null,
    },
  ];
  next.sort((left, right) => left.turn_no - right.turn_no);
  return next;
}

/**
 * Card C: did this conversation page contain the host's official copy of the
 * local projection?  Scoped by conversation so loading another thread cannot
 * retire the projection of the one that is still waiting.
 */
function confirmedPendingMessage(
  state: ChatState,
  conversationId: string,
  messages: ChatMessage[],
): boolean {
  if (state.pendingText === null) {
    return false;
  }
  // While the send is in flight no loaded page can prove that this projection
  // is the host's official message (it may only match an older identical text).
  if (state.sending) {
    return false;
  }
  if (
    state.pendingConversationId !== null &&
    state.pendingConversationId !== conversationId
  ) {
    return false;
  }
  const turnId = state.pendingUserTurnId ?? state.pendingTurnId;
  if (turnId !== null) {
    return messages.some(
      (message) => message.turn_id === turnId && message.kind === "user",
    );
  }
  return messages.some(
    (message) => message.kind === "user" && message.text === state.pendingText,
  );
}

function mergeFinal(
  messages: ChatMessage[],
  result: TurnResult,
  turns: ConversationTurn[],
): ChatMessage[] {
  const incoming = [result.final, result.wait_question].filter(
    (message): message is ChatMessage => message !== null,
  );
  if (incoming.length === 0) {
    return sortMessages(messages, turns);
  }
  const incomingIds = new Set(incoming.map((message) => message.message_id));
  const next = messages.filter(
    (item) => !incomingIds.has(item.message_id),
  );
  next.push(...incoming);
  return sortMessages(next, turns);
}

/**
 * RG08 card C: one deterministic order for the rendered turn groups.
 *
 * The host already orders messages by (turn_no, seq); this helper only makes
 * the grouping independent of whether the latest page happens to contain a
 * message for an older turn.  A projection whose turn is not in `turns` yet is
 * kept last (it has no authoritative number); the accepted projection is
 * inserted into `turns` and therefore sorts by its real `turn_no`.
 */
export function orderedTurnIds(state: ChatState): string[] {
  const ids: string[] = [];
  const seen = new Set<string>();
  const add = (turnId: string | null): void => {
    if (turnId !== null && !seen.has(turnId)) {
      seen.add(turnId);
      ids.push(turnId);
    }
  };
  for (const message of state.messages) {
    add(message.turn_id);
  }
  for (const turn of state.turns) {
    add(turn.turn_id);
  }
  add(state.pendingUserTurnId);
  add(state.eventsTurnId);
  const turnNo = new Map(state.turns.map((turn) => [turn.turn_id, turn.turn_no]));
  const index = new Map(ids.map((turnId, position) => [turnId, position]));
  const rank = (turnId: string): number => {
    const number = turnNo.get(turnId);
    if (number !== undefined) {
      return number;
    }
    if (turnId === state.pendingUserTurnId) {
      return Number.MAX_SAFE_INTEGER - 1;
    }
    return Number.MAX_SAFE_INTEGER;
  };
  return [...ids].sort(
    (left, right) =>
      rank(left) - rank(right) ||
      (index.get(left) as number) - (index.get(right) as number),
  );
}

/**
 * Order messages by (turn_no, seq).  The bridge already returns this order,
 * but a late commentary/final merge must never jump ahead of a newer turn.
 */
export function sortMessages(
  messages: ChatMessage[],
  turns: ConversationTurn[],
): ChatMessage[] {
  const turnNo = new Map(turns.map((turn) => [turn.turn_id, turn.turn_no]));
  const rank = (message: ChatMessage): number =>
    turnNo.get(message.turn_id) ?? Number.MAX_SAFE_INTEGER;
  return [...messages].sort(
    (left, right) => rank(left) - rank(right) || left.seq - right.seq,
  );
}

export function mergeEvents(
  existing: ProcessEvent[],
  incoming: ProcessEvent[],
): ProcessEvent[] {
  const byId = new Map(existing.map((event) => [event.event_id, event]));
  for (const event of incoming) {
    byId.set(event.event_id, event);
  }
  const merged = [...byId.values()].sort((left, right) => left.seq - right.seq);
  return merged.length > 300 ? merged.slice(merged.length - 300) : merged;
}

/** Cancel is offered only for the pending turn that is still in flight. */
export function canCancel(state: ChatState): boolean {
  return state.pendingTurnId !== null && !state.cancelRequested;
}

/** A fresh idempotency key.  `crypto.randomUUID` exists in the Tauri WebView. */
export function newRequestId(): string {
  const cryptoObject = globalThis.crypto as
    | { randomUUID?: () => string }
    | undefined;
  if (cryptoObject?.randomUUID) {
    return cryptoObject.randomUUID();
  }
  return "req-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
}

/** True when a retry of the same draft may reuse the stored request id. */
export function reuseRequestId(
  state: ChatState,
  text: string,
  conversationId: string | null = state.activeId,
): boolean {
  return (
    state.pendingRequestId !== null &&
    state.pendingText !== null &&
    state.pendingConversationId === conversationId &&
    state.pendingText === text
  );
}

/** The request id a send should use for this draft. */
export function requestIdFor(
  state: ChatState,
  text: string,
  conversationId: string | null = state.activeId,
): string {
  return reuseRequestId(state, text, conversationId)
    ? (state.pendingRequestId as string)
    : newRequestId();
}

/** A conversation switch/create is only offered while no turn is running. */
export function canSwitchConversation(state: ChatState): boolean {
  return !state.busy && !state.sending && state.pendingTurnId === null;
}

/** `turn.result` may only be requested after `ping.busy` is false. */
export function canReadTurnResult(state: ChatState): boolean {
  return !state.busy && state.pendingTurnId !== null;
}

/** The status line for the active turn, in user-facing Chinese. */
export function turnStatusLabel(state: ChatState): string | null {
  if (state.sending) {
    return "发送中…";
  }
  if (state.pendingTurnId !== null) {
    return "正在处理…";
  }
  return null;
}

/**
 * RG08: the newest turn whose messages are on screen.
 *
 * `null` before the first turn loads; used only for the reveal gate, never for
 * host state or ordering.
 */
export function newestTurnId(state: ChatState): string | null {
  if (state.turns.length === 0) {
    return null;
  }
  return state.turns[state.turns.length - 1].turn_id;
}

/**
 * RG08: should the local pending user bubble be shown?
 *
 * The pending bubble is a local projection, not a second message.  It stays
 * visible while the send is in flight, keeps its slot through a terminal
 * `turn.result`, and disappears as soon as the host's official user message is
 * loaded.  A failed send hides it (the draft is restored instead) but keeps the
 * text/request id for an idempotent retry.  It is scoped to its conversation and
 * is never shown beside its own formal message.
 */
export function pendingUserMessageVisible(state: ChatState): boolean {
  if (state.pendingText === null) {
    return false;
  }
  if (state.pendingFailed) {
    return false;
  }
  if (
    state.pendingConversationId !== null &&
    state.pendingConversationId !== state.activeId
  ) {
    return false;
  }
  if (state.sending) {
    return true;
  }
  if (state.pendingUserTurnId !== null) {
    return !state.messages.some(
      (message) =>
        message.turn_id === state.pendingUserTurnId && message.kind === "user",
    );
  }
  // Defensive: an accepted projection always carries a turn id; keep the
  // bubble until a conversation reload confirms it rather than guessing by text.
  return true;
}
