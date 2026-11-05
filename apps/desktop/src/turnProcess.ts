/**
 * RG08 card D: DOM-free state for replaying an old turn's process.
 *
 * The bridge already exposes the persisted public events and the filtered tool
 * steps.  This module only decides *what* the panel can honestly say: a read is
 * still running, was refused because another turn owns the worker, failed, is
 * unavailable, or is genuinely empty.  It never invents a tool name and never
 * treats a failed read as "no tools".
 */

import type { ConversationTurn, ProcessEvent } from "./chat";

/** One bounded cache holds at most this many turns. */
export const MAX_TURN_PROCESS_CACHE = 64;

/** `turn.events` returns at most 100 rows per page. */
export const MAX_EVENT_PAGES = 10;

/** `turn.steps` returns at most 50 rows per page. */
export const MAX_STEP_PAGES = 10;

export type LoadState =
  | "idle"
  | "loading"
  | "ready"
  | "busy"
  | "error"
  | "unavailable";

/** The public `turn.steps` row shape (fields owned by conversation.dto). */
export interface StepView {
  step_id: string;
  step_no: number;
  tool_name: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  error_label: string | null;
  arguments: Record<string, unknown> | null;
  result: {
    available: boolean;
    excerpt?: string | null;
    truncated?: boolean;
  };
  result_artifact_ref?: Record<string, unknown> | null;
}

export interface TurnStepPage {
  steps: StepView[];
  next_after_step_no: number | null;
}

/**
 * The empty states card D/A2 must distinguish.
 *
 * `rejected` is only produced from a persisted `tool_rejected` event; a plain
 * format repair with no such event stays the conservative `format-blocked`.
 */
export type ProcessGapKind =
  | "not-called"
  | "inconsistent"
  | "format-blocked"
  | "rejected";

export interface ProcessView {
  /** Executed steps when they are known, otherwise planned tool calls. */
  toolCount: number;
  eventsGap: LoadState | null;
  stepsGap: LoadState | null;
  gap: ProcessGapKind | null;
  toolPlanned: number;
  /** Public `tool_rejected` events, including a parse failure with no step. */
  toolRejected: number;
}

export interface ProcessInput {
  turn: ConversationTurn | null;
  events: ProcessEvent[];
  eventsState: LoadState;
  steps: StepView[];
  stepsState: LoadState;
  processEnabled: boolean;
  terminal: boolean;
}

function displayLoad(state: LoadState): LoadState | null {
  if (state === "ready") {
    return null;
  }
  if (state === "idle") {
    return "loading";
  }
  return state;
}

/**
 * One honest projection of the two independent reads.
 *
 * The section gaps are independent: a failed `turn.steps` read never hides an
 * already loaded event timeline, and vice versa.  The whole-turn `gap` is only
 * produced when *both* reads are ready, because an empty step list after a
 * failed read is not evidence that nothing ran.
 */
export function classifyProcess(input: ProcessInput): ProcessView {
  const toolPlanned = input.events.filter(
    (event) => event.type === "tool_planned",
  ).length;
  const toolRejected = input.events.filter(
    (event) => event.type === "tool_rejected",
  ).length;
  const stepsReady = input.stepsState === "ready";
  const toolCount = stepsReady ? input.steps.length : toolPlanned;

  if (!input.processEnabled) {
    return {
      toolCount,
      eventsGap: "unavailable",
      stepsGap: "unavailable",
      gap: null,
      toolPlanned,
      toolRejected,
    };
  }

  const eventsGap = displayLoad(input.eventsState);
  const stepsGap = input.terminal ? displayLoad(input.stepsState) : null;

  if (
    input.terminal &&
    eventsGap === null &&
    stepsGap === null &&
    input.steps.length === 0
  ) {
    // A persisted rejection is evidence of a blocked call even when the step
    // read has no row (a parse failure never creates one).  Only a planned
    // call without either a step or a rejection is inconsistent.
    if (toolRejected > 0) {
      return {
        toolCount,
        eventsGap,
        stepsGap,
        gap: "rejected",
        toolPlanned,
        toolRejected,
      };
    }
    if (toolPlanned > 0) {
      return {
        toolCount,
        eventsGap,
        stepsGap,
        gap: "inconsistent",
        toolPlanned,
        toolRejected,
      };
    }
    const formatFailure =
      input.turn?.failure_reason?.startsWith("format:") === true;
    const formatRepair = input.events.some(
      (event) => event.type === "repair_attempt" && event.reason === "format",
    );
    return {
      toolCount,
      eventsGap,
      stepsGap,
      gap: formatFailure || formatRepair ? "format-blocked" : "not-called",
      toolPlanned,
      toolRejected,
    };
  }

  return { toolCount, eventsGap, stepsGap, gap: null, toolPlanned, toolRejected };
}

/**
 * The public commentary text of one `message_published` event.
 *
 * Live events carry a transient `text`; a persisted event only carries the
 * message id.  The already loaded transcript is therefore the first source for
 * a replay, then the live text, then an explicit non-public marker.
 */
export function commentaryText(
  event: ProcessEvent,
  messages: ReadonlyArray<{ message_id: string; text: string | null }>,
): string | null {
  if (event.type !== "message_published" || event.kind !== "commentary") {
    return null;
  }
  if (event.message_id) {
    const message = messages.find((item) => item.message_id === event.message_id);
    if (message !== undefined) {
      return message.text ?? event.text ?? "";
    }
  }
  return event.text ?? null;
}

/** Arguments are public only through `turn.steps`; a live event may carry them. */
export function toolArguments(
  step: StepView | undefined,
  event: ProcessEvent | undefined,
): Record<string, unknown> | null {
  if (step && step.arguments !== null) {
    return step.arguments;
  }
  if (event && event.arguments !== undefined) {
    return event.arguments;
  }
  return null;
}

/** A step's duration is derived from its persisted timestamps, never guessed. */
export function stepDurationMs(step: StepView): number | null {
  if (!step.started_at || !step.finished_at) {
    return null;
  }
  const started = Date.parse(step.started_at);
  const finished = Date.parse(step.finished_at);
  if (!Number.isFinite(started) || !Number.isFinite(finished)) {
    return null;
  }
  const duration = finished - started;
  return duration >= 0 ? duration : null;
}
