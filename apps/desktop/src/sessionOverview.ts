import type { AppInfo } from "./types";
import type { ChatState, MemoryState } from "./chat";
import { NO_USAGE, type UsageLoad, type UsageSummary } from "./usage";

export interface SessionOverview {
  id: string | null; title: string | null; createdAt: string | null; ready: boolean; noRequests: boolean;
  tasks: { state: MemoryState; count: number }[] | null; loadedTurns: number; taskCoverage: number;
  model: string | null; hostReady: boolean;
  /** True only when this host implements the read-only usage method. */
  usageAvailable: boolean;
  /** Real request usage for the selected conversation; never a synthetic value. */
  usage: UsageSummary | null;
  usageLoading: boolean;
  usageError: boolean;
  /** The usage read was refused because a turn is running. */
  usageBusy: boolean;
}
/** Keep scope and unknown distinct from zero. Never aggregate another conversation. */
export function sessionOverview(
  info: AppInfo | null,
  chat: ChatState,
  usage: UsageLoad = NO_USAGE,
): SessionOverview {
  const selected = chat.conversations.find(item => item.conversation_id === chat.activeId);
  const ids = new Set(chat.turns.map(turn => turn.turn_id));
  const loaded = Object.values(chat.postprocess).filter(turn => ids.has(turn.turn_id));
  const counts = new Map<MemoryState, number>();
  for (const turn of loaded) counts.set(turn.memory.state, (counts.get(turn.memory.state) ?? 0) + 1);
  return {
    id: chat.activeId, title: selected?.title ?? null, createdAt: selected?.created_at ?? null, ready: chat.status === "ready",
    noRequests: chat.status === "ready" && selected?.turn_count === 0 && chat.turns.length === 0,
    tasks: loaded.length ? [...counts].map(([state, count]) => ({ state, count })) : null,
    loadedTurns: chat.turns.length, taskCoverage: loaded.length,
    model: info?.destination?.model ?? null, hostReady: Boolean(info?.host.ready),
    usageAvailable: Boolean(info?.methods?.includes("conversation.usage")),
    usage: usage.data, usageLoading: usage.loading, usageError: usage.error, usageBusy: usage.busy,
  };
}
