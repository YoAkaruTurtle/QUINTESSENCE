/**
 * The Consult UI state machine.
 *
 * This module is deliberately free of DOM and Tauri imports so the transition
 * rules and the authority-facing request shape can be tested in Node.  It
 * never stores a question, answer or reference outside memory.
 */

import type {
  AppInfo,
  BridgeError,
  ConsultResult,
  Destination,
} from "./types";

export type Phase =
  | "booting"
  | "not-ready"
  | "input"
  | "sending"
  | "answered"
  | "waiting"
  | "failed"
  | "incomplete"
  | "stale"
  | "unknown"
  | "busy";

export type DrawerTab = "sources" | "audit" | "destination" | "settings";

export type ActionId =
  | "send"
  | "clear"
  | "copy"
  | "new-question"
  | "sources"
  | "destination"
  | "retry-host";

export interface DrawerState {
  open: boolean;
  tab: DrawerTab;
}

export interface UiState {
  phase: Phase;
  host: AppInfo | null;
  hostError: BridgeError | null;
  question: string;
  lastQuestion: string;
  result: ConsultResult | null;
  error: BridgeError | null;
  destination: Destination | null;
  confirmedDestinationId: string | null;
  confirmPending: boolean;
  drawer: DrawerState;
  notice: string | null;
  destinationChanged: boolean;
}

export const INITIAL_STATE: UiState = {
  phase: "booting",
  host: null,
  hostError: null,
  question: "",
  lastQuestion: "",
  result: null,
  error: null,
  destination: null,
  confirmedDestinationId: null,
  confirmPending: false,
  drawer: { open: false, tab: "sources" },
  notice: null,
  destinationChanged: false,
};

export type UiAction =
  | { type: "host-info"; info: AppInfo }
  | { type: "host-error"; error: BridgeError }
  | { type: "draft"; question: string }
  | { type: "send-request" }
  | { type: "confirm-open" }
  | { type: "confirm-cancel" }
  | { type: "confirm-accepted" }
  | { type: "destination-confirmed"; destinationId: string }
  | { type: "result"; result: ConsultResult }
  | { type: "error"; error: BridgeError }
  | { type: "new-question" }
  | { type: "drawer-open"; tab?: DrawerTab }
  | { type: "drawer-tab"; tab: DrawerTab }
  | { type: "drawer-close" }
  | { type: "destination-changed" }
  | { type: "retry-host" }
  | { type: "clear" };

const TERMINAL_PHASES = new Set<Phase>([
  "answered",
  "waiting",
  "failed",
  "incomplete",
  "stale",
  "unknown",
  "busy",
]);

export function isTerminal(phase: Phase): boolean {
  return TERMINAL_PHASES.has(phase);
}

export function reduce(state: UiState, action: UiAction): UiState {
  switch (action.type) {
    case "host-info": {
      const info = action.info;
      const next = info.destination;
      const previousId = state.destination?.destination_id ?? null;
      const changed =
        previousId !== null &&
        next !== null &&
        previousId !== next.destination_id;
      let confirmed = state.confirmedDestinationId;
      if (changed || !next || confirmed !== next.destination_id) {
        confirmed = null;
      }
      let phase = state.phase;
      if (!info.host.ready && !state.result) {
        phase = "not-ready";
      } else if (
        info.host.ready &&
        (phase === "booting" || phase === "not-ready")
      ) {
        phase = "input";
      }
      return {
        ...state,
        host: info,
        hostError: null,
        phase,
        destination: next,
        confirmedDestinationId: confirmed,
        destinationChanged: state.destinationChanged || changed,
        notice: changed
          ? "目的地已变化；请重新确认当前外部目的地。"
          : state.notice,
      };
    }
    case "host-error": {
      return {
        ...state,
        hostError: action.error,
        phase: state.result ? state.phase : "not-ready",
        error: state.result ? state.error : action.error,
      };
    }
    case "draft": {
      if (
        state.phase === "booting" ||
        state.phase === "sending" ||
        state.phase === "not-ready"
      ) {
        return state;
      }
      return { ...state, question: action.question };
    }
    case "send-request": {
      if (!sendEnabled(state)) {
        return state;
      }
      if (needsConfirmation(state)) {
        return { ...state, confirmPending: true, error: null };
      }
      return {
        ...state,
        phase: "sending",
        lastQuestion: state.question,
        result: null,
        error: null,
        confirmPending: false,
        notice: null,
      };
    }
    case "confirm-open": {
      return { ...state, confirmPending: needsConfirmation(state) };
    }
    case "confirm-cancel": {
      return { ...state, confirmPending: false };
    }
    case "confirm-accepted": {
      if (!state.destination) {
        return state;
      }
      return {
        ...state,
        confirmedDestinationId: state.destination.destination_id,
        confirmPending: false,
        phase: "sending",
        lastQuestion: state.question,
        result: null,
        error: null,
        notice: null,
      };
    }
    case "destination-confirmed": {
      return {
        ...state,
        confirmedDestinationId: action.destinationId,
        confirmPending: false,
        error: null,
      };
    }
    case "result": {
      return {
        ...state,
        phase: action.result.state,
        result: action.result,
        lastQuestion: state.lastQuestion || state.question,
        question: "",
        destination: action.result.destination ?? state.destination,
        error: null,
        confirmPending: false,
        notice: null,
      };
    }
    case "error": {
      return errorState(state, action.error);
    }
    case "new-question": {
      const keepConfirmation =
        state.destination?.confirmation_required === true &&
        state.confirmedDestinationId === state.destination?.destination_id;
      return {
        ...state,
        phase: state.host?.host.ready ? "input" : "not-ready",
        question: "",
        lastQuestion: "",
        result: null,
        error: null,
        confirmPending: false,
        notice: null,
        destinationChanged: false,
        confirmedDestinationId: keepConfirmation
          ? state.confirmedDestinationId
          : null,
        drawer: { ...state.drawer, open: false },
      };
    }
    case "drawer-open": {
      return {
        ...state,
        drawer: { open: true, tab: action.tab ?? state.drawer.tab },
      };
    }
    case "drawer-tab": {
      return { ...state, drawer: { open: true, tab: action.tab } };
    }
    case "drawer-close": {
      return { ...state, drawer: { ...state.drawer, open: false } };
    }
    case "destination-changed": {
      return {
        ...state,
        confirmedDestinationId: null,
        destinationChanged: true,
        notice: "目的地已变化；请重新确认当前外部目的地。",
      };
    }
    case "retry-host": {
      return {
        ...state,
        phase: "booting",
        hostError: null,
        error: null,
      };
    }
    case "clear": {
      return { ...state, question: "" };
    }
    default: {
      return state;
    }
  }
}

function errorState(state: UiState, error: BridgeError): UiState {
  const wasSending = state.phase === "sending";
  if (error.code === "BUSY") {
    return {
      ...state,
      phase: "busy",
      error,
      question: "",
      confirmPending: false,
    };
  }
  if (error.result_unknown) {
    return {
      ...state,
      phase: "unknown",
      error,
      question: "",
      confirmPending: false,
    };
  }
  if (
    error.code === "HOST_NOT_READY" ||
    error.code === "DATA_LOCKED" ||
    error.code === "SIDECAR_UNAVAILABLE"
  ) {
    return {
      ...state,
      phase: state.result ? state.phase : "not-ready",
      hostError: error,
      error,
      question: wasSending ? "" : state.question,
      confirmPending: false,
    };
  }
  if (error.code === "INPUT_REGISTRATION_FAILED") {
    return {
      ...state,
      phase: state.host?.host.ready ? "input" : "not-ready",
      lastQuestion: "",
      error,
      confirmPending: false,
    };
  }
  if (error.code === "CONFIRMATION_REQUIRED") {
    return {
      ...state,
      phase: "input",
      confirmedDestinationId: null,
      confirmPending: true,
      error,
    };
  }
  if (error.code === "DESTINATION_MISMATCH" || error.code === "SESSION_MISMATCH") {
    return {
      ...state,
      phase: "input",
      confirmedDestinationId: null,
      confirmPending: false,
      error,
      notice:
        error.code === "SESSION_MISMATCH"
          ? "宿主会话已重建；请重新确认目的地并重新提问。"
          : "确认的目的地与当前目的地不一致；请重新确认。",
    };
  }
  return {
    ...state,
    phase: state.host?.host.ready ? "input" : "not-ready",
    error,
    question: wasSending ? "" : state.question,
    confirmPending: false,
  };
}

export function maxQuestionChars(state: UiState): number {
  return state.host?.limits.max_question_chars ?? 8000;
}

export function sendEnabled(state: UiState): boolean {
  if (
    state.phase === "booting" ||
    state.phase === "sending" ||
    state.phase === "not-ready"
  ) {
    return false;
  }
  if (!state.host?.host.ready || !state.destination) {
    return false;
  }
  if (validateQuestion(state.question, maxQuestionChars(state)) !== null) {
    return false;
  }
  return true;
}

export function needsConfirmation(state: UiState): boolean {
  if (!state.destination?.confirmation_required) {
    return false;
  }
  return state.confirmedDestinationId !== state.destination.destination_id;
}

export function isConfirmed(state: UiState): boolean {
  return (
    !!state.destination &&
    state.confirmedDestinationId === state.destination.destination_id
  );
}

export function validateQuestion(
  question: string,
  maximum: number,
): string | null {
  if (!question.trim()) {
    return "请先写下问题。";
  }
  if (Array.from(question).length > maximum) {
    return `问题不能超过 ${maximum} 个字符。`;
  }
  if (question.includes("\u0000")) {
    return "问题包含不可用字符。";
  }
  return null;
}

/**
 * The only request body the frontend may build.  It names no authority field;
 * the trusted Rust host and Python host derive everything else.
 */
export function buildConsultParams(state: UiState): {
  sessionId: string;
  question: string;
  confirmedDestinationId: string | null;
} {
  const destinationId = state.destination?.destination_id ?? null;
  const confirmed =
    destinationId !== null && state.confirmedDestinationId === destinationId
      ? destinationId
      : null;
  return {
    sessionId: state.host?.session_id ?? "",
    question: state.question,
    confirmedDestinationId: confirmed,
  };
}

export function actionsFor(state: UiState): ActionId[] {
  switch (state.phase) {
    case "booting":
      return [];
    case "not-ready":
      return ["retry-host", "destination"];
    case "input":
      return state.question
        ? ["send", "clear", "sources", "destination"]
        : ["send", "sources", "destination"];
    case "sending":
      return ["destination"];
    case "answered":
      return ["copy", "new-question", "sources", "destination"];
    case "waiting":
    case "failed":
    case "incomplete":
    case "stale":
      return ["new-question", "sources", "destination"];
    case "unknown":
    case "busy":
      return ["new-question", "destination"];
    default:
      return ["destination"];
  }
}

export function hasSources(state: UiState): boolean {
  return state.result !== null;
}
