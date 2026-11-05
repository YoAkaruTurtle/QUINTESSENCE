/**
 * RG02/RG03 conversation IPC wrappers (DOM-free, injectable `invoke`).
 *
 * Tauri v2 maps the camelCase argument names used here onto the Rust command
 * parameters (`session_id`, `conversation_id`, `client_request_id`, ...).
 * Keeping the mapping in one module makes the exact parameter shape testable
 * without a WebView: a missing `sessionId` is a real IPC bug, not a style
 * issue.
 */

export type InvokeFn = (
  command: string,
  args: Record<string, unknown>,
) => Promise<unknown>;

export interface ConversationListOptions {
  limit?: number | null;
  cursor?: string | null;
}

export interface ConversationGetOptions {
  beforeTurnNo?: number | null;
  beforeSeq?: number | null;
  limit?: number | null;
}

export interface TurnEventsOptions {
  afterSeq?: number | null;
  limit?: number | null;
}

export interface TurnStepsOptions {
  afterStepNo?: number | null;
  limit?: number | null;
}

export interface ConversationSendParams {
  sessionId: string;
  conversationId: string;
  clientRequestId: string;
  text: string;
  confirmedDestinationId: string | null;
}

export interface ConversationSearchOptions {
  limit?: number | null;
  conversationId?: string | null;
  before?: string | null;
  includeFull?: boolean | null;
}

export interface MemorySearchOptions {
  limit?: number | null;
  cursor?: string | null;
}

function optional<T>(
  key: string,
  value: T | null | undefined,
): Record<string, unknown> {
  return value === null || value === undefined ? {} : { [key]: value };
}

export interface ChatApi {
  conversationCreate(sessionId: string, mode: string): Promise<unknown>;
  conversationList(
    sessionId: string,
    options?: ConversationListOptions,
  ): Promise<unknown>;
  conversationGet(
    sessionId: string,
    conversationId: string,
    options?: ConversationGetOptions,
  ): Promise<unknown>;
  conversationSend(params: ConversationSendParams): Promise<unknown>;
  conversationSearch(
    sessionId: string,
    query: string,
    options?: ConversationSearchOptions,
  ): Promise<unknown>;
  memorySearch(
    sessionId: string,
    query: string,
    options?: MemorySearchOptions,
  ): Promise<unknown>;
  postprocessStatus(
    sessionId: string,
    conversationId: string,
    turnIds?: string[] | null,
  ): Promise<unknown>;
  postprocessRetry(sessionId: string, turnId: string): Promise<unknown>;
  cognitionCandidates(sessionId: string, limit?: number): Promise<unknown>;
  cognitionCandidateDecide(
    sessionId: string,
    candidateId: string,
    artifactVersion: number,
    decision: "approve" | "reject",
  ): Promise<unknown>;
  turnResult(sessionId: string, turnId: string): Promise<unknown>;
  turnEvents(
    sessionId: string,
    turnId: string,
    options?: TurnEventsOptions,
  ): Promise<unknown>;
  turnSteps(
    sessionId: string,
    turnId: string,
    options?: TurnStepsOptions,
  ): Promise<unknown>;
  turnTrace(sessionId: string, turnId: string): Promise<unknown>;
  conversationUsage(
    sessionId: string,
    conversationId: string,
    turnId?: string | null,
  ): Promise<unknown>;
  turnCancel(sessionId: string, turnId: string): Promise<unknown>;
  bridgePing(): Promise<unknown>;
  profileList(sessionId: string): Promise<unknown>;
  profileActivate(
    sessionId: string,
    profileId: string,
    recordVersion: number,
  ): Promise<unknown>;
  profileReadBody(
    sessionId: string,
    recordId: string,
    recordVersion: number,
  ): Promise<unknown>;
  profileImportList(sessionId: string): Promise<unknown>;
  profileImportPreview(sessionId: string, name: string): Promise<unknown>;
  profilePublishReviewed(
    sessionId: string,
    importId: string,
    profileId: string | null,
    confirm: boolean,
  ): Promise<unknown>;
  preferenceList(
    sessionId: string,
    conversationId?: string | null,
  ): Promise<unknown>;
  preferenceRevoke(
    sessionId: string,
    conversationId: string,
    dimension: string,
    reason?: string | null,
  ): Promise<unknown>;
}

export function createChatApi(invoke: InvokeFn): ChatApi {
  return {
    conversationCreate(sessionId, mode) {
      return invoke("conversation_create", { sessionId, mode });
    },
    conversationList(sessionId, options = {}) {
      return invoke("conversation_list", {
        sessionId,
        ...optional("limit", options.limit),
        ...optional("cursor", options.cursor),
      });
    },
    conversationGet(sessionId, conversationId, options = {}) {
      return invoke("conversation_get", {
        sessionId,
        conversationId,
        ...optional("beforeTurnNo", options.beforeTurnNo),
        ...optional("beforeSeq", options.beforeSeq),
        ...optional("limit", options.limit),
      });
    },
    conversationSend(params) {
      return invoke("conversation_send", {
        sessionId: params.sessionId,
        conversationId: params.conversationId,
        clientRequestId: params.clientRequestId,
        text: params.text,
        confirmedDestinationId: params.confirmedDestinationId,
      });
    },
    conversationSearch(sessionId, query, options = {}) {
      return invoke("conversation_search", {
        sessionId,
        query,
        ...optional("limit", options.limit),
        ...optional("conversationId", options.conversationId),
        ...optional("before", options.before),
        ...optional("includeFull", options.includeFull),
      });
    },
    memorySearch(sessionId, query, options = {}) {
      return invoke("memory_search", {
        sessionId,
        query,
        ...optional("limit", options.limit),
        ...optional("cursor", options.cursor),
      });
    },
    postprocessStatus(sessionId, conversationId, turnIds = null) {
      return invoke("postprocess_status", {
        sessionId,
        conversationId,
        ...optional("turnIds", turnIds),
      });
    },
    postprocessRetry(sessionId, turnId) {
      return invoke("postprocess_retry", { sessionId, turnId });
    },
    cognitionCandidates(sessionId, limit) {
      return invoke("cognition_candidates", {
        sessionId,
        ...optional("limit", limit),
      });
    },
    cognitionCandidateDecide(sessionId, candidateId, artifactVersion, decision) {
      return invoke("cognition_candidate_decide", {
        sessionId,
        candidateId,
        artifactVersion,
        decision,
      });
    },
    turnResult(sessionId, turnId) {
      return invoke("turn_result", { sessionId, turnId });
    },
    turnEvents(sessionId, turnId, options = {}) {
      return invoke("turn_events", {
        sessionId,
        turnId,
        ...optional("afterSeq", options.afterSeq),
        ...optional("limit", options.limit),
      });
    },
    turnSteps(sessionId, turnId, options = {}) {
      return invoke("turn_steps", {
        sessionId,
        turnId,
        ...optional("afterStepNo", options.afterStepNo),
        ...optional("limit", options.limit),
      });
    },
    turnTrace(sessionId, turnId) {
      return invoke("turn_trace", { sessionId, turnId });
    },
    conversationUsage(sessionId, conversationId, turnId = null) {
      return invoke("conversation_usage", {
        sessionId,
        conversationId,
        ...optional("turnId", turnId),
      });
    },
    turnCancel(sessionId, turnId) {
      return invoke("turn_cancel", { sessionId, turnId });
    },
    bridgePing() {
      return invoke("bridge_ping", {});
    },
    profileList(sessionId) {
      return invoke("profile_list", { sessionId });
    },
    profileActivate(sessionId, profileId, recordVersion) {
      return invoke("profile_activate", {
        sessionId,
        profileId,
        recordVersion,
      });
    },
    profileReadBody(sessionId, recordId, recordVersion) {
      return invoke("profile_read_body", {
        sessionId,
        recordId,
        recordVersion,
      });
    },
    profileImportList(sessionId) {
      return invoke("profile_import_list", { sessionId });
    },
    profileImportPreview(sessionId, name) {
      return invoke("profile_import_preview", { sessionId, name });
    },
    profilePublishReviewed(sessionId, importId, profileId, confirm) {
      return invoke("profile_publish_reviewed", {
        sessionId,
        importId,
        profileId,
        confirm,
      });
    },
    preferenceList(sessionId, conversationId = null) {
      return invoke("preference_list", {
        sessionId,
        ...optional("conversationId", conversationId),
      });
    },
    preferenceRevoke(sessionId, conversationId, dimension, reason = null) {
      return invoke("preference_revoke", {
        sessionId,
        conversationId,
        dimension,
        ...optional("reason", reason),
      });
    },
  };
}
