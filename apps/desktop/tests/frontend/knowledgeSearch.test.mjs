import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";
const require = createRequire(import.meta.url);
const {
  memorySearchAvailability,
  memoryUnavailableText,
  normalizeMemoryPage,
  mergeMemoryPages,
  searchQueries,
  memoryKindLabel,
  memoryStateLabel,
  memoryCitation,
  memoryPrimaryTime,
  memoryScanText,
} = require(path.join(process.env.QUINTESSENCE_FRONTEND_BUILD, "knowledgeSearch.js"));

const page = (overrides = {}) => ({
  items: [
    {
      kind: "event",
      ref: "event-abc",
      revision: 3,
      scope_id: "scope:rg08",
      state: "current",
      content: "合成记忆：代号蓝鲸",
      content_truncated: false,
      sources: [
        {
          ref: "event-abc",
          revision: 3,
          entry_id: "local",
          agent: "agent:rg08",
          session: "session:rg08",
          occurred_at: "2026-10-29T17:00:00+00:00",
          recorded_at: null,
        },
      ],
    },
  ],
  examined: 2,
  scan_complete: true,
  stop_reason: "complete",
  next_cursor: null,
  memory_epoch: 1,
  ...overrides,
});

test("memory availability comes from the host, never assumed", () => {
  assert.deepEqual(memorySearchAvailability(null), { available: false, reason: "host_not_ready" });
  assert.deepEqual(memorySearchAvailability({ methods: [] }), { available: false, reason: "method_unavailable" });
  assert.deepEqual(
    memorySearchAvailability({ methods: ["memory.search"], capabilities: { memory: { configured: true } } }),
    { available: false, reason: "not_configured" },
  );
  assert.deepEqual(
    memorySearchAvailability({ methods: ["memory.search"], capabilities: { memory: { configured: true, search: true } } }),
    { available: true },
  );
});

test("unavailable text names the real state and keeps transcript usable", () => {
  assert.match(memoryUnavailableText("not_configured", "zh"), /未配置记忆桥接/);
  assert.match(memoryUnavailableText("not_configured", "zh"), /旧对话检索仍可用/);
  assert.match(memoryUnavailableText("method_unavailable", "en"), /host build/);
});

test("empty input browses memory but never searches transcripts", () => {
  assert.deepEqual(searchQueries("   "), { memory: "", transcript: null });
  assert.deepEqual(searchQueries("  蓝鲸 "), { memory: "蓝鲸", transcript: "蓝鲸" });
});

test("a malformed memory page is an error, not a fabricated empty page", () => {
  assert.throws(() => normalizeMemoryPage(null));
  assert.throws(() => normalizeMemoryPage({ items: "no" }));
  assert.throws(() => normalizeMemoryPage({ items: [], next_cursor: 12 }));
  const normalized = normalizeMemoryPage({
    items: [page().items[0], { kind: "" }, 7],
    examined: 3,
    scan_complete: true,
    stop_reason: "complete",
    next_cursor: null,
    memory_epoch: 1,
  });
  assert.equal(normalized.items.length, 1);
  assert.equal(normalized.items[0].sources[0].entry_id, "local");
  assert.equal(normalized.examined, 3);
});

test("a page from another encoding is still bounded and typed", () => {
  const normalized = normalizeMemoryPage({
    items: [
      {
        kind: "claim",
        ref: 7,
        revision: "9",
        scope_id: null,
        state: "disputed",
        content: 5,
        content_truncated: undefined,
        sources: [{ entry_id: 4, occurred_at: 5 }],
      },
    ],
    next_cursor: null,
  });
  assert.equal(normalized.items[0].ref, "");
  assert.equal(normalized.items[0].revision, 0);
  assert.equal(normalized.items[0].content, "");
  assert.equal(normalized.items[0].sources[0].entry_id, "");
  assert.equal(normalized.next_cursor, null);
});

test("merge keeps prior items and deduplicates by kind/ref/version", () => {
  const first = normalizeMemoryPage(page({ next_cursor: "cursor-1" }));
  const second = normalizeMemoryPage(
    page({
      items: [
        page().items[0],
        { ...page().items[0], ref: "event-def", content: "second page" },
      ],
      next_cursor: null,
    }),
  );
  const merged = mergeMemoryPages(first, second);
  assert.deepEqual(merged.items.map((entry) => entry.ref), ["event-abc", "event-def"]);
  assert.equal(merged.next_cursor, null);
});

test("labels and citations stay explicit about source and state", () => {
  assert.equal(memoryKindLabel("claim", "zh"), "主张");
  assert.equal(memoryKindLabel("claim", "en"), "claim");
  assert.equal(memoryKindLabel("unknown-kind", "zh"), "unknown-kind");
  assert.equal(memoryStateLabel("disputed", "zh"), "有争议");
  assert.equal(memoryStateLabel("unknown", "en"), "unknown");
  const item = normalizeMemoryPage(page()).items[0];
  assert.equal(memoryCitation(item), "event-abc@3");
  assert.equal(memoryPrimaryTime(item), "2026-10-29T17:00:00+00:00");
  assert.match(memoryScanText(page(), "zh"), /已扫描 2 条记忆/);
  assert.match(memoryScanText(page(), "zh"), /已扫描完/);
  assert.match(memoryScanText(page({ scan_complete: false, stop_reason: "limit" }), "zh"), /可能还有更多/);
});
