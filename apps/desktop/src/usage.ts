/** Read-only conversation usage / turn trace types and pure formatters.
 *
 * A missing number is `null`, never 0: the dashboard has to keep "unknown"
 * distinct from "no requests".  These helpers are DOM-free so the frontend
 * tests can prove that rule without a browser.
 */

export interface UsageTokens {
  prompt: number | null;
  completion: number | null;
  total: number | null;
  complete: boolean;
}

export interface ContextBytes {
  used_bytes: number;
  limit_bytes: number;
}

/** The persisted attempt kinds.  ``unmarked`` means "cannot prove". */
export type AttemptKind = "primary" | "repair" | "transport_retry" | "unmarked";

export interface UsageAttempt {
  attempt_id: string;
  attempt_no: number;
  stage: string;
  purpose: string;
  ordinal: number;
  status: string;
  outcome: string | null;
  finish_reason: string | null;
  error_label: string | null;
  usage_known: boolean;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  tokens_per_second: number | null;
  repair: boolean | null;
  repair_reason: string | null;
  resend_same_body: boolean;
  kind: AttemptKind;
}

export interface UsageContext {
  context_id: string;
  ordinal: number;
  purpose: string;
  created_at: string;
  bytes: ContextBytes | null;
  attempts: UsageAttempt[];
}

export interface UsageStage {
  stage_id: string;
  kind: string;
  status: string;
  skip_reason: string | null;
  policy_version: string;
  created_at: string;
  completed_at: string | null;
  tool_steps: number;
  tool_steps_by_status: Record<string, number>;
  contexts: UsageContext[];
}

export interface TraceFinal {
  message_id: string;
  seq: number;
  published_at: string | null;
  attempt_id: string | null;
  stage_kind: string | null;
  source: string;
  fallback: boolean;
  fallback_reason: string | null;
}

export interface TraceCounts {
  repairs: number;
  repairs_unbound: number;
  repair_unknown: number;
  transport_retries: number;
  primary: number;
  tool_steps: number;
}

export interface TurnTrace {
  turn: {
    turn_id: string;
    conversation_id: string;
    turn_no: number;
    state: string;
    created_at: string;
    started_at: string | null;
    completed_at: string | null;
    failure_reason: string | null;
  };
  stages: UsageStage[];
  final: TraceFinal | null;
  attempts_total: number;
  attempts_known: number;
  attempts_unknown: number;
  attempts_open: number;
  attempts_unreadable: number;
  counts: TraceCounts;
  tokens: UsageTokens;
  repairs: { stage: string; attempt_id: string; attempt_no: number; reason: string | null }[];
  latest_request: UsageAttempt | null;
  latest_context: { context_id: string; purpose: string; stage: string; bytes: ContextBytes | null } | null;
  truncated: boolean;
}

export interface UsageTurn {
  turn_id: string;
  turn_no: number;
  state: string;
  attempts_total: number;
  attempts_known: number;
  attempts_unknown: number;
  attempts_open: number;
  attempts_unreadable: number;
  tokens: UsageTokens;
  counts: TraceCounts;
  latest_request: UsageAttempt | null;
  latest_context: { context_id: string; purpose: string; stage: string; bytes: ContextBytes | null } | null;
}

export interface UsageSummary {
  conversation_id: string;
  turns_total: number;
  turns_with_attempts: number;
  attempts_total: number;
  attempts_known: number;
  attempts_unknown: number;
  attempts_open: number;
  attempts_unreadable: number;
  repairs: number;
  tokens: UsageTokens;
  latest_request: UsageAttempt | null;
  latest_context: { context_id: string; purpose: string; stage: string; bytes: ContextBytes | null } | null;
  turn: UsageTurn | null;
}

/** The bridge payload normalised for the dashboard. */
export interface UsageLoad {
  available: boolean;
  loading: boolean;
  error: boolean;
  /** A read was refused because the turn is running; never a zero. */
  busy: boolean;
  data: UsageSummary | null;
}

export const NO_USAGE: UsageLoad = Object.freeze({ available: false, loading: false, error: false, busy: false, data: null });

/** The exact token total, or null when any request in scope is unsettled. */
export function exactTokens(tokens: UsageTokens | null | undefined): number | null {
  if (!tokens || !tokens.complete || tokens.total === null) return null;
  return tokens.total;
}

/** "3/5" — settled requests over all requests; never a percentage. */
export function coverageText(known: number, total: number): string {
  return `${known}/${total}`;
}

/** "18200 / 65536 B" or null; this is a PCS Context byte cap, not a window. */
export function bytesLabel(bytes: ContextBytes | null | undefined): string | null {
  if (!bytes) return null;
  if (!Number.isFinite(bytes.used_bytes) || !Number.isFinite(bytes.limit_bytes)) return null;
  if (bytes.limit_bytes <= 0) return null;
  return `${bytes.used_bytes} / ${bytes.limit_bytes} B`;
}

/** The attempt's own measured speed, or null.  Never averaged across requests. */
export function exactSpeed(attempt: UsageAttempt | null | undefined): number | null {
  if (!attempt || !attempt.usage_known) return null;
  if (attempt.tokens_per_second === null) return null;
  if (!Number.isFinite(attempt.tokens_per_second)) return null;
  return attempt.tokens_per_second;
}

/** The latest request's speed only when it belongs to this scope's newest row. */
export function latestSpeed(summary: UsageSummary | null): number | null {
  return summary === null ? null : exactSpeed(summary.latest_request);
}

/** Proof of repair comes from the persisted marker, never from the order. */
export function repairedAttempt(attempt: UsageAttempt): boolean {
  return attempt.repair === true;
}

export function unmarkedAttempt(attempt: UsageAttempt): boolean {
  return attempt.repair === null;
}


/** Normalise one `conversation.usage` bridge payload for the dashboard. */
export function usageSummary(payload: unknown): UsageSummary | null {
  if (!payload || typeof payload !== "object") return null;
  const value = payload as Record<string, any>;
  const counts = (value.counts ?? {}) as Record<string, any>;
  return {
    conversation_id: String(value.conversation_id ?? ""),
    turns_total: Number(value.turns_total ?? 0),
    turns_with_attempts: Number(value.turns_with_attempts ?? 0),
    attempts_total: Number(value.attempts_total ?? 0),
    attempts_known: Number(value.attempts_known ?? 0),
    attempts_unknown: Number(value.attempts_unknown ?? 0),
    attempts_open: Number(value.attempts_open ?? 0),
    attempts_unreadable: Number(value.attempts_unreadable ?? 0),
    repairs: Number(counts.repairs ?? 0),
    tokens: value.tokens as UsageTokens,
    latest_request: (value.latest_request ?? null) as UsageAttempt | null,
    latest_context: (value.latest_context ?? null) as UsageSummary["latest_context"],
    turn: (value.turn ?? null) as UsageSummary["turn"],
  };
}
