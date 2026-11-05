/**
 * UI-F03: the host-managed runtime log (DOM-free rules).
 *
 * The host owns the directory, the filters and the redaction.  This module only
 * shapes a bounded request, formats the rows it really received, and states the
 * real coverage — it never invents a row and never claims the adapter stderr is
 * included when the transport drops it.
 */

export type LogLang = "zh" | "en";
export type LogSourceId = "all" | "host" | "sidecar" | "adapter";
export type LogLevelId = "all" | "debug" | "info" | "warn" | "error";
export type LogStatus = "idle" | "loading" | "live" | "paused" | "error";

export interface LogEntry {
  seq: number;
  at_ms: number;
  at_text?: string | null;
  source: string;
  level: string;
  code?: string | null;
  message: string;
  truncated?: boolean;
}

export interface LogSourceCoverage {
  id: string;
  collected: boolean;
  detail?: string | null;
  reason?: string | null;
}

export interface LogCoverage {
  scope: string;
  sources: LogSourceCoverage[];
  window: {
    from_ms: number | null;
    to_ms: number | null;
    entries: number;
    ring_capacity: number;
    dropped: number;
  };
  history: { state: string; entries: number; skipped: number };
  persistence: {
    enabled: boolean;
    reason?: string | null;
    file_label: string;
    files: number;
    max_bytes_per_file: number;
    rotation: string;
  };
  redaction: { version: number; applied_at: string };
}

export interface LogReadResult {
  entries: LogEntry[];
  matched: number;
  returned: number;
  truncated: boolean;
  coverage: LogCoverage;
}

export interface LogSelection {
  source: LogSourceId;
  level: LogLevelId;
  query: string;
}

export const LOG_PAGE_LIMIT = 500;
export const LOG_SEARCH_MAX = 64;
export const LOG_POLL_MS = 2000;

export const LOG_SOURCE_ORDER: LogSourceId[] = ["all", "host", "sidecar", "adapter"];
export const LOG_LEVEL_ORDER: LogLevelId[] = ["all", "debug", "info", "warn", "error"];

export class LogReadError extends Error {
  constructor(readonly code: string) {
    super(code);
    this.name = "LogReadError";
  }
}

function tr(lang: LogLang, zh: string, en: string): string {
  return lang === "en" ? en : zh;
}

export function logLang(): LogLang {
  return document.documentElement.lang === "en" ? "en" : "zh";
}

/** The only inputs the host accepts; there is no path/glob in this object. */
export function logParams(selection: LogSelection): Record<string, unknown> {
  const params: Record<string, unknown> = { limit: LOG_PAGE_LIMIT };
  if (selection.source !== "all") params.source = selection.source;
  if (selection.level !== "all") params.level = selection.level;
  const query = selection.query.trim();
  if (query) params.query = query.slice(0, LOG_SEARCH_MAX);
  return params;
}

export async function readLogs(
  invoke: (command: string, args: Record<string, unknown>) => Promise<unknown>,
  selection: LogSelection,
): Promise<LogReadResult> {
  let reply: unknown;
  try {
    reply = await invoke("logs_read", logParams(selection));
  } catch {
    throw new LogReadError("LOG_UNAVAILABLE");
  }
  const record = reply as
    | { ok?: unknown; result?: unknown; error?: { code?: unknown } }
    | null;
  if (!record || record.ok !== true) {
    const code = typeof record?.error?.code === "string" ? record.error.code : "LOG_UNAVAILABLE";
    throw new LogReadError(code);
  }
  return asLogResult(record.result);
}

export function asLogResult(value: unknown): LogReadResult {
  const record = value as
    | { entries?: unknown; coverage?: unknown; matched?: unknown }
    | null;
  if (
    !record ||
    typeof record !== "object" ||
    !Array.isArray(record.entries) ||
    !record.coverage ||
    typeof record.coverage !== "object"
  ) {
    throw new LogReadError("LOG_INVALID");
  }
  return value as LogReadResult;
}

/** `HH:MM:SS`; the sidecar's own timestamp wins when it reports one. */
export function logTime(entry: LogEntry): string {
  const text = entry.at_text ?? "";
  const match = /^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})[,.]\d+$/.exec(text);
  if (match) return match[1];
  const date = new Date(entry.at_ms);
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return [date.getHours(), date.getMinutes(), date.getSeconds()]
    .map((value) => String(value).padStart(2, "0"))
    .join(":");
}

export function logMessage(entry: LogEntry): string {
  const code = entry.code ? `${entry.code} ` : "";
  return `${code}${entry.message}${entry.truncated ? " …[truncated]" : ""}`;
}

export function logRow(entry: LogEntry): string {
  return `${logTime(entry)} ${entry.source} ${entry.level} ${logMessage(entry)}`;
}

export function copyText(entries: LogEntry[]): string {
  return entries.map(logRow).join("\n");
}

export function sourceLabel(source: LogSourceId, lang: LogLang): string {
  switch (source) {
    case "all":
      return tr(lang, "全部来源", "All sources");
    case "host":
      return tr(lang, "宿主 (host)", "Host (host)");
    case "sidecar":
      return tr(lang, "sidecar stderr", "Sidecar stderr");
    case "adapter":
      return tr(lang, "adapter（未采集）", "Adapter (not collected)");
  }
}

export function levelLabel(level: LogLevelId, lang: LogLang): string {
  switch (level) {
    case "all":
      return tr(lang, "全部级别", "All levels");
    case "debug":
      return tr(lang, "调试", "Debug");
    case "info":
      return tr(lang, "信息", "Info");
    case "warn":
      return tr(lang, "警告", "Warn");
    case "error":
      return tr(lang, "错误", "Error");
  }
}

export function historyText(coverage: LogCoverage, lang: LogLang): string {
  const history = coverage.history;
  const tail = history.skipped > 0
    ? tr(lang, `，${history.skipped} 行不可解析已跳过`, `; ${history.skipped} unreadable lines skipped`)
    : "";
  switch (history.state) {
    case "available":
      return tr(lang, `历史：已加载 ${history.entries} 条${tail}`, `History: ${history.entries} loaded${tail}`);
    case "empty":
      return tr(lang, `历史：本次开始采集，更早未采集${tail}`, `History: collection starts now; earlier entries were not collected${tail}`);
    default:
      return tr(lang, `历史：不可读或已轮转，未采集${tail}`, `History: unreadable or rotated away; not collected${tail}`);
  }
}

function persistenceReason(reason: string | null | undefined, lang: LogLang): string {
  switch (reason) {
    case "dir_unavailable":
      return tr(lang, "目录不可用", "directory unavailable");
    case "write_failed":
      return tr(lang, "写入失败", "write failed");
    case "rotation_failed":
      return tr(lang, "轮转失败", "rotation failed");
    case "serialize_failed":
      return tr(lang, "序列化失败", "serialization failed");
    default:
      return reason || tr(lang, "不可用", "unavailable");
  }
}

export function coverageLines(coverage: LogCoverage, lang: LogLang): string[] {
  const scope = coverage.scope === "boot"
    ? tr(
        lang,
        "范围：引导日志（实例身份未建立），不读取其他实例",
        "Scope: boot logs (no instance identity); other instances are not read",
      )
    : tr(lang, "范围：当前实例", "Scope: current instance");
  const collected = coverage.sources.filter((source) => source.collected).map((source) => source.id);
  const missing = coverage.sources.filter((source) => !source.collected);
  const sources = [
    tr(lang, "覆盖：", "Coverage: "),
    collected.join(" + ") || tr(lang, "无", "none"),
    ...(missing.length
      ? [
          tr(lang, `；${missing.map((source) => source.id).join("/")} 未采集`, `; ${missing.map((source) => source.id).join("/")} not collected`),
        ]
      : []),
  ].join("");
  const window = coverage.window;
  const windowText = tr(
    lang,
    `窗口：最近 ${window.entries} 条 / 上限 ${window.ring_capacity}，已滚出 ${window.dropped} 条`,
    `Window: latest ${window.entries} / cap ${window.ring_capacity}, ${window.dropped} rolled out`,
  );
  const files = coverage.persistence.enabled
    ? tr(
        lang,
        `文件：${coverage.persistence.file_label}（${coverage.persistence.files} × ${Math.round(coverage.persistence.max_bytes_per_file / 1024)} KiB，按大小轮转）`,
        `File: ${coverage.persistence.file_label} (${coverage.persistence.files} × ${Math.round(coverage.persistence.max_bytes_per_file / 1024)} KiB, size rotation)`,
      )
    : tr(
        lang,
        `文件：不可用（${persistenceReason(coverage.persistence.reason, lang)}）；本次运行仅内存，不再落盘`,
        `File: unavailable (${persistenceReason(coverage.persistence.reason, lang)}); memory only for this run, no further disk writes`,
      );
  const redaction = tr(
    lang,
    `脱敏：写入+读取（v${coverage.redaction.version}）`,
    `Redaction: write+read (v${coverage.redaction.version})`,
  );
  return [scope, sources, windowText, files, historyText(coverage, lang), redaction];
}

export function sourceCollected(coverage: LogCoverage | null, source: LogSourceId): boolean | null {
  if (!coverage || source === "all") return true;
  const found = coverage.sources.find((item) => item.id === source);
  return found ? found.collected : null;
}

/** Shown instead of an empty list when the selected source was never captured. */
export function adapterNotice(coverage: LogCoverage | null, lang: LogLang): string | null {
  if (!coverage) return null;
  const adapter = coverage.sources.find((source) => source.id === "adapter");
  if (!adapter || adapter.collected) return null;
  return tr(
    lang,
    "adapter/transport stderr 未采集：sidecar 子进程的 stderr 当前未被读取（capture_output 丢弃）。这里没有条目，也不代表已覆盖全部。",
    "Adapter/transport stderr is not collected: the sidecar subprocess stderr is currently discarded (capture_output). No rows exist here, and this is not full coverage.",
  );
}

export function emptyNotice(lang: LogLang, filtered: boolean): string {
  return filtered
    ? tr(lang, "当前筛选没有匹配条目。", "No entries match the current filter.")
    : tr(lang, "当前窗口没有日志条目。", "No log entries in the current window.");
}

export function errorNotice(lang: LogLang, code: string): string {
  return tr(lang, `日志读取失败：${code}`, `Log read failed: ${code}`);
}

export function statusText(lang: LogLang, status: LogStatus, count: number): string {
  switch (status) {
    case "loading":
      return tr(lang, "读取中…", "Reading…");
    case "live":
      return tr(lang, `实时 · ${count} 条`, `Live · ${count}`);
    case "paused":
      return tr(lang, `已暂停 · ${count} 条`, `Paused · ${count}`);
    case "error":
      return tr(lang, "读取失败", "Read failed");
    default:
      return tr(lang, "尚未读取", "Not loaded");
  }
}

export function isFiltered(selection: LogSelection): boolean {
  return selection.source !== "all" || selection.level !== "all" || selection.query.trim() !== "";
}
