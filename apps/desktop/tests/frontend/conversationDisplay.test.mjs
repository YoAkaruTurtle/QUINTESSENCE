import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";
const require = createRequire(import.meta.url);
const build = process.env.QUINTESSENCE_FRONTEND_BUILD;
const {
  EMPTY_DISPLAY_PREFERENCES,
  DisplayPreferenceError,
  archiveLimitReached,
  conversationGroupKey,
  displayTitle,
  groupConversations,
  isArchived,
  loadDisplayPreferences,
  mergeDisplayTitles,
  parseDisplayPreferences,
  setDisplayPreference,
  sortedArchived,
  titleLimitReached,
  validateConversationTitle,
  visibleConversations,
} = require(path.join(build, "conversationDisplay.js"));

const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const now = new Date(2026, 9, 29, 12, 0, 0);
const at = (daysAgo, hour = 9) => new Date(2026, 9, 29 - daysAgo, hour, 0, 0).toISOString();
const conversation = (n, daysAgo) => ({
  conversation_id: id(n),
  title: null,
  created_at: at(daysAgo),
  updated_at: at(daysAgo),
  turn_count: 1,
  mode: "chat",
  last_turn_state: "completed",
});

test("a legacy or unscoped host view is still a usable empty preference set", () => {
  const parsed = parseDisplayPreferences({ scoped: false, archived: [], titles: {} });
  assert.equal(parsed.scoped, false);
  assert.deepEqual(parsed.archived, []);
  assert.deepEqual(parsed.titles, {});
  assert.equal(parsed.limits.title_chars, 40);
  assert.equal(parseDisplayPreferences(null), null);
  assert.equal(parseDisplayPreferences({ archived: [] }), null);
});

test("malformed ids, duplicate archives and invalid titles are dropped, not rendered", () => {
  const parsed = parseDisplayPreferences({
    scoped: true,
    archived: [
      { conversation_id: id(1).toUpperCase(), archived_at_ms: 5 },
      { conversation_id: id(1), archived_at_ms: 6 },
      { conversation_id: "not-a-uuid", archived_at_ms: 7 },
      { conversation_id: id(2), archived_at_ms: "9" },
    ],
    titles: {
      [id(1).toUpperCase()]: "  项目  ",
      bad: "x",
      [id(3)]: "bad\u0007name",
      [id(4)]: "x".repeat(41),
    },
  });
  assert.deepEqual(parsed.archived, [
    { conversation_id: id(1), archived_at_ms: 5 },
    { conversation_id: id(2), archived_at_ms: 0 },
  ]);
  assert.deepEqual(parsed.titles, { [id(1)]: "项目" });
});

test("title validation mirrors the host: trim, non-empty, 40 code points, no controls", () => {
  assert.deepEqual(validateConversationTitle("  项目笔记  "), { ok: true, value: "项目笔记" });
  assert.equal(validateConversationTitle("   ").reason, "empty");
  assert.equal(validateConversationTitle("").reason, "empty");
  assert.equal(validateConversationTitle("a".repeat(41)).reason, "tooLong");
  assert.equal(validateConversationTitle("会".repeat(40)).ok, true);
  assert.equal(validateConversationTitle("a".repeat(41)).ok, false);
  assert.equal(validateConversationTitle("bad\nname").reason, "control");
  assert.equal(validateConversationTitle("bad\u007fname").reason, "control");
});

test("the display title override is one layer for every conversation view", () => {
  const preferences = { ...EMPTY_DISPLAY_PREFERENCES, titles: { [id(1)]: "新名字" } };
  const items = [conversation(1, 0), conversation(2, 0)];
  const merged = mergeDisplayTitles(items, preferences);
  assert.equal(merged[0].title, "新名字");
  assert.equal(displayTitle(merged[0], preferences), "新名字");
  assert.equal(merged[1], items[1]);
  assert.equal(items[0].title, null, "the source item is never mutated");
});

test("archived conversations are hidden but keep their order and identity", () => {
  const preferences = {
    ...EMPTY_DISPLAY_PREFERENCES,
    archived: [{ conversation_id: id(2), archived_at_ms: 1 }],
  };
  const items = [conversation(1, 0), conversation(2, 1), conversation(3, 2)];
  const visible = visibleConversations(items, preferences);
  assert.deepEqual(visible.map((item) => item.conversation_id), [id(1), id(3)]);
  assert.equal(isArchived(id(2), preferences), true);
  assert.equal(isArchived(id(1), preferences), false);
  assert.equal(items.length, 3, "filtering never mutates the host list");
});

test("groups follow local calendar days and only the host updated_at", () => {
  assert.equal(conversationGroupKey(at(0), now), "today");
  assert.equal(conversationGroupKey(at(1), now), "yesterday");
  assert.equal(conversationGroupKey(at(2), now), "week");
  assert.equal(conversationGroupKey(at(6), now), "week");
  assert.equal(conversationGroupKey(at(7), now), "older");
  assert.equal(conversationGroupKey("not-a-date", now), "unknown");
  assert.equal(conversationGroupKey(new Date(2026, 9, 30, 8).toISOString(), now), "today");
});

test("grouping preserves host order, omits empty groups and never invents rows", () => {
  const items = [conversation(1, 0), conversation(2, 1), conversation(3, 3), conversation(4, 30), conversation(5, 0)];
  const groups = groupConversations(items, now);
  assert.deepEqual(groups.map((group) => group.key), ["today", "yesterday", "week", "older"]);
  assert.deepEqual(groups[0].items.map((item) => item.conversation_id), [id(1), id(5)]);
  assert.deepEqual(groups[3].items.map((item) => item.conversation_id), [id(4)]);
  assert.equal(groups.reduce((sum, group) => sum + group.items.length, 0), items.length);
});

test("the restore list is newest archived first", () => {
  const preferences = {
    ...EMPTY_DISPLAY_PREFERENCES,
    archived: [
      { conversation_id: id(1), archived_at_ms: 10 },
      { conversation_id: id(2), archived_at_ms: 30 },
      { conversation_id: id(3), archived_at_ms: 20 },
    ],
  };
  assert.deepEqual(sortedArchived(preferences).map((entry) => entry.conversation_id), [id(2), id(3), id(1)]);
});

test("host limits are respected without hiding existing entries", () => {
  const archived = Array.from({ length: 2 }, (_, index) => ({ conversation_id: id(index + 1), archived_at_ms: 1 }));
  const preferences = { ...EMPTY_DISPLAY_PREFERENCES, archived, limits: { archived: 2, titles: 1, title_chars: 40 } };
  assert.equal(archiveLimitReached(preferences, id(3)), true);
  assert.equal(archiveLimitReached(preferences, id(1)), false, "an existing archive is idempotent");
  const titles = { [id(1)]: "x" };
  assert.equal(titleLimitReached({ ...preferences, titles }, id(2)), true);
  assert.equal(titleLimitReached({ ...preferences, titles }, id(1)), false);
});

test("a successful read returns the host's current-instance slice", async () => {
  const calls = [];
  const invoke = async (command) => {
    calls.push(command);
    return {
      ok: true,
      result: { scoped: true, archived: [{ conversation_id: id(1), archived_at_ms: 9 }], titles: { [id(2)]: "名称" }, limits: { archived: 200, titles: 500, title_chars: 40 } },
    };
  };
  const preferences = await loadDisplayPreferences(invoke);
  assert.deepEqual(calls, ["conversation_display_preferences"]);
  assert.equal(preferences.archived[0].conversation_id, id(1));
  assert.equal(preferences.titles[id(2)], "名称");
});

test("a refused or malformed read is a real error, never an empty success", async () => {
  await assert.rejects(
    () => loadDisplayPreferences(async () => ({ ok: false, error: { code: "HOST_NOT_READY", message: "no identity" } })),
    (error) => error instanceof DisplayPreferenceError && error.code === "HOST_NOT_READY" && error.message === "no identity",
  );
  await assert.rejects(
    () => loadDisplayPreferences(async () => ({ ok: true, result: null })),
    (error) => error instanceof DisplayPreferenceError && error.code === "INVALID_PAYLOAD",
  );
});

test("writes use the closed action set and the camelCase IPC arguments", async () => {
  const calls = [];
  const invoke = async (command, args) => {
    calls.push([command, args]);
    return { ok: true, result: { scoped: true, archived: [], titles: {}, limits: { archived: 200, titles: 500, title_chars: 40 } } };
  };
  await setDisplayPreference(invoke, "rename", id(1), "名字");
  await setDisplayPreference(invoke, "archive", id(1));
  await setDisplayPreference(invoke, "restore_all");
  assert.deepEqual(calls, [
    ["set_conversation_display_preference", { action: "rename", conversationId: id(1), title: "名字" }],
    ["set_conversation_display_preference", { action: "archive", conversationId: id(1) }],
    ["set_conversation_display_preference", { action: "restore_all" }],
  ]);
});

test("a failed write is thrown and leaves the caller's display state untouched", async () => {
  const before = { ...EMPTY_DISPLAY_PREFERENCES };
  await assert.rejects(
    () => setDisplayPreference(async () => ({ ok: false, error: { code: "INVALID_PARAMS", message: "标题不能为空" } }), "rename", id(1), " "),
    (error) => error instanceof DisplayPreferenceError && error.code === "INVALID_PARAMS",
  );
  assert.deepEqual(before, EMPTY_DISPLAY_PREFERENCES);
});
