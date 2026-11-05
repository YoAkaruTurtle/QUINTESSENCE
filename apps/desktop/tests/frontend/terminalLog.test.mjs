import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";
const require = createRequire(import.meta.url);
const logs = require(path.join(process.env.QUINTESSENCE_FRONTEND_BUILD, "terminalLog.js"));

const coverage = (overrides = {}) => ({
  scope: "instance",
  sources: [
    { id: "host", collected: true },
    { id: "sidecar", collected: true, detail: "stderr" },
    { id: "adapter", collected: false, reason: "stderr_not_forwarded" },
  ],
  window: { from_ms: 1, to_ms: 2, entries: 7, ring_capacity: 1000, dropped: 2 },
  history: { state: "available", entries: 5, skipped: 0 },
  persistence: {
    enabled: true,
    reason: null,
    file_label: "desktop.log",
    files: 2,
    max_bytes_per_file: 524288,
    rotation: "size",
  },
  redaction: { version: 1, applied_at: "write+read" },
  ...overrides,
});

const entry = (overrides = {}) => ({
  seq: 1,
  at_ms: 1761743053000,
  at_text: "2026-10-29 21:04:13,170",
  source: "host",
  level: "info",
  code: "host.start",
  message: "version=0.1.0 managed=false",
  truncated: false,
  ...overrides,
});

test("the host request is bounded and carries no path at all", () => {
  const params = logs.logParams({ source: "all", level: "all", query: "  bridge  " });
  assert.deepEqual(params, { limit: 500, query: "bridge" });
  assert.equal(JSON.stringify(params).includes("path"), false);
  assert.equal(JSON.stringify(params).includes("dir"), false);
  const filtered = logs.logParams({ source: "sidecar", level: "warn", query: "" });
  assert.deepEqual(filtered, { limit: 500, source: "sidecar", level: "warn" });
  assert.equal(logs.logParams({ source: "all", level: "all", query: "x".repeat(100) }).query.length, 64);
});

test("times prefer the source timestamp and never invent one", () => {
  assert.equal(logs.logTime(entry()), "21:04:13");
  assert.equal(logs.logTime(entry({ at_text: null, at_ms: 0 })), "08:00:00");
  assert.equal(logs.logTime(entry({ at_text: "garbage" })).length, 8);
});

test("a row keeps the real code, source and level; truncation is marked", () => {
  const row = logs.logRow(entry());
  assert.equal(row, "21:04:13 host info host.start version=0.1.0 managed=false");
  assert.equal(logs.logRow(entry({ truncated: true })).endsWith("…[truncated]"), true);
  assert.match(logs.logRow(entry({ code: null })), / host info version=0\.1\.0/);
});

test("copy text is only the received rows, never a placeholder", () => {
  assert.equal(logs.copyText([]), "");
  const text = logs.copyText([
    entry(),
    entry({ seq: 2, source: "sidecar", level: "warn", code: null, message: "bridge ready" }),
  ]);
  assert.equal(text.split("\n").length, 2);
  assert.ok(text.includes("host.start"));
  assert.ok(text.includes("sidecar warn bridge ready"));
});

test("coverage names host and sidecar, and marks adapter as not collected", () => {
  const lines = logs.coverageLines(coverage(), "zh");
  const text = lines.join("\n");
  assert.ok(text.includes("范围：当前实例"));
  assert.ok(text.includes("host"));
  assert.ok(text.includes("sidecar"));
  assert.ok(text.includes("adapter 未采集"));
  assert.ok(text.includes("最近 7 条"));
  assert.ok(text.includes("已滚出 2 条"));
  assert.ok(text.includes("2 × 512 KiB"));
  const english = logs.coverageLines(coverage(), "en").join("\n");
  assert.ok(english.includes("adapter not collected"));
  const boot = logs.coverageLines(coverage({ scope: "boot" }), "zh").join("\n");
  assert.ok(boot.includes("范围：引导日志"));
  assert.ok(boot.includes("不读取其他实例"));
});

test("history says what really happened instead of pretending", () => {
  assert.ok(logs.historyText(coverage(), "zh").includes("已加载 5 条"));
  const empty = coverage({ history: { state: "empty", entries: 0, skipped: 0 } });
  assert.ok(logs.historyText(empty, "zh").includes("更早未采集"));
  const unavailable = coverage({ history: { state: "unavailable", entries: 0, skipped: 3 } });
  const text = logs.historyText(unavailable, "zh");
  assert.ok(text.includes("未采集"));
  assert.ok(text.includes("3 行不可解析"));
});

test("a failed file store is shown as unavailable, not as persisted history", () => {
  const failed = coverage({
    persistence: {
      enabled: false,
      reason: "dir_unavailable",
      file_label: "desktop.log",
      files: 2,
      max_bytes_per_file: 524288,
      rotation: "size",
    },
    history: { state: "unavailable", entries: 0, skipped: 0 },
  });
  const text = logs.coverageLines(failed, "zh").join("\n");
  assert.ok(text.includes("文件：不可用"));
  assert.ok(text.includes("本次运行仅内存"));
  assert.ok(text.includes("历史：不可读或已轮转，未采集"));
});

test("selecting adapter shows an explicit not-collected notice, not an empty list", () => {
  assert.ok(logs.adapterNotice(coverage(), "zh").includes("未采集"));
  assert.ok(logs.adapterNotice(coverage(), "zh").includes("capture_output"));
  const collected = coverage({
    sources: [
      { id: "host", collected: true },
      { id: "sidecar", collected: true },
      { id: "adapter", collected: true },
    ],
  });
  assert.equal(logs.adapterNotice(collected, "zh"), null);
  assert.equal(logs.sourceCollected(coverage(), "adapter"), false);
  assert.equal(logs.sourceCollected(coverage(), "host"), true);
  assert.equal(logs.sourceCollected(null, "host"), true);
});

test("labels and status are localized without changing row ids", () => {
  assert.equal(logs.sourceLabel("adapter", "zh"), "adapter（未采集）");
  assert.equal(logs.levelLabel("warn", "en"), "Warn");
  assert.ok(logs.statusText("zh", "paused", 3).includes("已暂停"));
  assert.ok(logs.statusText("en", "live", 3).includes("Live"));
  assert.equal(logs.isFiltered({ source: "all", level: "all", query: "  " }), false);
  assert.equal(logs.isFiltered({ source: "host", level: "all", query: "" }), true);
});

test("a malformed host payload is rejected instead of rendered", () => {
  assert.throws(() => logs.asLogResult(null), /LOG_INVALID/);
  assert.throws(() => logs.asLogResult({ entries: [], coverage: null }), /LOG_INVALID/);
  assert.throws(
    () => logs.asLogResult({ entries: "nope", coverage: {} }),
    /LOG_INVALID/,
  );
  assert.equal(logs.asLogResult({ entries: [], coverage: {} }).entries.length, 0);
});

test("readLogs sends exactly one bounded command and surfaces the host code", async () => {
  const calls = [];
  const reply = await logs.readLogs(
    async (command, args) => {
      calls.push([command, args]);
      return { ok: true, result: { entries: [entry()], matched: 1, returned: 1, truncated: false, coverage: coverage() } };
    },
    { source: "host", level: "info", query: "start" },
  );
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "logs_read");
  assert.deepEqual(calls[0][1], { limit: 500, source: "host", level: "info", query: "start" });
  assert.equal(reply.entries.length, 1);
  await assert.rejects(
    logs.readLogs(async () => ({ ok: false, error: { code: "INVALID_PARAMS" } }), {
      source: "all",
      level: "all",
      query: "",
    }),
    (error) => error instanceof Error && error.code === "INVALID_PARAMS",
  );
  await assert.rejects(
    logs.readLogs(async () => {
      throw new Error("command missing");
    }, { source: "all", level: "all", query: "" }),
    (error) => error.code === "LOG_UNAVAILABLE",
  );
});
