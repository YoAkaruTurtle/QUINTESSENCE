/**
 * RG08: shared presentation rules for the integrated memory + transcript
 * search.  This module is DOM-free so the Node tests can load it; the side
 * panel view lives in `knowledgeSearchView.ts`.
 *
 * The two result kinds are never merged into one list: memory items come from
 * the instance's pinned Scope Recall store through the host, transcript items
 * come from `conversation.search`.  A transcript hit is not memory and a
 * memory hit is not a PCS belief; every label below keeps that distinction.
 */

import type { AppInfo } from "./types";

export interface MemorySourceMeta {
  ref: string;
  revision: number;
  entry_id: string;
  agent: string;
  session: string;
  occurred_at: string | null;
  recorded_at: string | null;
}

export interface MemorySearchItem {
  kind: string;
  ref: string;
  revision: number;
  scope_id: string;
  state: string;
  content: string;
  content_truncated: boolean;
  sources: MemorySourceMeta[];
}

export interface MemorySearchPage {
  items: MemorySearchItem[];
  examined: number;
  scan_complete: boolean;
  stop_reason: string;
  next_cursor: string | null;
  memory_epoch: number;
}

export type MemoryUnavailableReason =
  | "host_not_ready"
  | "method_unavailable"
  | "not_configured";

export type MemoryAvailability =
  | { available: true }
  | { available: false; reason: MemoryUnavailableReason };

const KINDS: Record<string, [string, string]> = {
  event: ["记录", "record"],
  claim: ["主张", "claim"],
  episode: ["片段", "episode"],
  artifact: ["产物", "artifact"],
  reference: ["引用", "reference"],
};

const STATES: Record<string, [string, string]> = {
  current: ["当前", "current"],
  historical: ["历史", "historical"],
  disputed: ["有争议", "disputed"],
  unknown: ["未知", "unknown"],
};

function pair(
  table: Record<string, [string, string]>,
  value: unknown,
  lang: string,
): string {
  const entry = typeof value === "string" ? table[value] : undefined;
  if (!entry) return typeof value === "string" && value ? value : lang === "en" ? "unknown" : "未知";
  return lang === "en" ? entry[1] : entry[0];
}

export function memoryKindLabel(kind: unknown, lang: string): string {
  return pair(KINDS, kind, lang);
}

export function memoryStateLabel(state: unknown, lang: string): string {
  return pair(STATES, state, lang);
}

export function memorySearchAvailability(info: AppInfo | null): MemoryAvailability {
  if (!info) return { available: false, reason: "host_not_ready" };
  if (!Array.isArray(info.methods) || !info.methods.includes("memory.search")) {
    return { available: false, reason: "method_unavailable" };
  }
  if (info.capabilities?.memory?.search !== true) {
    return { available: false, reason: "not_configured" };
  }
  return { available: true };
}

export function memoryUnavailableText(
  reason: MemoryUnavailableReason,
  lang: string,
): string {
  if (lang === "en") {
    if (reason === "not_configured") {
      return "Memory search is unavailable: this instance has no configured memory bridge. Conversation search still works.";
    }
    if (reason === "method_unavailable") {
      return "Memory search is unavailable in this host build. Conversation search still works.";
    }
    return "Memory search is unavailable until the host is ready. Conversation search still works.";
  }
  if (reason === "not_configured") {
    return "记忆检索不可用：此实例未配置记忆桥接。旧对话检索仍可用。";
  }
  if (reason === "method_unavailable") {
    return "记忆检索不可用：当前宿主版本未提供该接口。旧对话检索仍可用。";
  }
  return "记忆检索不可用：宿主尚未就绪。旧对话检索仍可用。";
}

/** A memory browse may run with an empty query; a transcript search may not. */
export function searchQueries(query: string): {
  memory: string;
  transcript: string | null;
} {
  const trimmed = query.trim();
  return { memory: trimmed, transcript: trimmed.length > 0 ? trimmed : null };
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function integer(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) ? value : 0;
}

function source(value: unknown): MemorySourceMeta | null {
  const row = record(value);
  if (!row) return null;
  return {
    ref: text(row.ref),
    revision: integer(row.revision),
    entry_id: text(row.entry_id),
    agent: text(row.agent),
    session: text(row.session),
    occurred_at: typeof row.occurred_at === "string" ? row.occurred_at : null,
    recorded_at: typeof row.recorded_at === "string" ? row.recorded_at : null,
  };
}

function item(value: unknown): MemorySearchItem | null {
  const row = record(value);
  if (!row) return null;
  const kind = text(row.kind);
  if (!kind) return null;
  const rawSources = Array.isArray(row.sources) ? row.sources : [];
  return {
    kind,
    ref: text(row.ref),
    revision: integer(row.revision),
    scope_id: text(row.scope_id),
    state: text(row.state),
    content: text(row.content),
    content_truncated: row.content_truncated === true,
    sources: rawSources
      .map(source)
      .filter((value): value is MemorySourceMeta => value !== null),
  };
}

/** Validate one host answer; an unexpected shape is an error, never an empty page. */
export function normalizeMemoryPage(value: unknown): MemorySearchPage {
  const row = record(value);
  if (!row || !Array.isArray(row.items)) {
    throw new Error("invalid memory search page");
  }
  const cursor = row.next_cursor;
  if (cursor !== null && typeof cursor !== "string") {
    throw new Error("invalid memory search cursor");
  }
  return {
    items: row.items
      .map(item)
      .filter((entry): entry is MemorySearchItem => entry !== null),
    examined: integer(row.examined),
    scan_complete: row.scan_complete === true,
    stop_reason: text(row.stop_reason),
    next_cursor: typeof cursor === "string" ? cursor : null,
    memory_epoch: integer(row.memory_epoch),
  };
}

export function mergeMemoryPages(
  previous: MemorySearchPage | null,
  next: MemorySearchPage,
): MemorySearchPage {
  if (!previous) return next;
  const seen = new Set(previous.items.map((entry) => `${entry.kind}:${entry.ref}:${entry.revision}`));
  const items = [...previous.items];
  for (const entry of next.items) {
    const key = `${entry.kind}:${entry.ref}:${entry.revision}`;
    if (seen.has(key)) continue;
    seen.add(key);
    items.push(entry);
  }
  return { ...next, items };
}

/** Prefer the first declared source's event time, then its recording time. */
export function memoryPrimaryTime(value: MemorySearchItem): string | null {
  for (const entry of value.sources) {
    if (entry.occurred_at) return entry.occurred_at;
    if (entry.recorded_at) return entry.recorded_at;
  }
  return null;
}

export function formatMemoryTime(value: string | null, lang: string): string {
  if (!value) return lang === "en" ? "time unknown" : "时间未知";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
}

export function memoryCitation(value: MemorySearchItem): string {
  const ref = value.ref || (value.kind ? `${value.kind}:unknown` : "unknown");
  return `${ref}@${value.revision || 0}`;
}

export function memorySourceLine(value: MemorySearchItem, lang: string): string {
  const first = value.sources[0];
  if (!first) return lang === "en" ? "source not reported" : "未报告来源";
  const parts = [first.entry_id || (lang === "en" ? "unknown entry" : "未知条目")];
  if (first.agent && first.agent !== first.entry_id) parts.push(first.agent);
  if (first.session) parts.push(first.session);
  parts.push(formatMemoryTime(memoryPrimaryTime(value), lang));
  return parts.join(" · ");
}

export function memoryScanText(page: MemorySearchPage, lang: string): string {
  if (lang === "en") {
    const suffix =
      page.stop_reason === "time_budget"
        ? " · scan stopped at the time budget"
        : page.stop_reason === "limit"
          ? " · more may match"
          : page.scan_complete
            ? " · scan complete"
            : "";
    return `Examined ${page.examined} memory objects${suffix}`;
  }
  const suffix =
    page.stop_reason === "time_budget"
      ? " · 扫描达到时间上限"
      : page.stop_reason === "limit"
        ? " · 可能还有更多"
        : page.scan_complete
          ? " · 已扫描完"
          : "";
  return `已扫描 ${page.examined} 条记忆${suffix}`;
}
