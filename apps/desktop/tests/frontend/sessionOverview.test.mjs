import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";
const require = createRequire(import.meta.url);
const { sessionOverview } = require(path.join(process.env.QUINTESSENCE_FRONTEND_BUILD, "sessionOverview.js"));
const { INITIAL_CHAT_STATE } = require(path.join(process.env.QUINTESSENCE_FRONTEND_BUILD, "chat.js"));

test("no selection or unread usage never becomes a fabricated zero", () => {
  assert.equal(sessionOverview(null, INITIAL_CHAT_STATE).noRequests, false);
  assert.equal(sessionOverview(null, INITIAL_CHAT_STATE).tasks, null);
  assert.equal(sessionOverview(null, INITIAL_CHAT_STATE).id, null);
});
test("only an authoritative empty selected conversation has no requests", () => {
  const selected = { ...INITIAL_CHAT_STATE, activeId: "a", status: "ready", conversations: [{ conversation_id: "a", turn_count: 0 }] };
  assert.equal(sessionOverview(null, selected).noRequests, true);
  assert.equal(sessionOverview(null, { ...selected, status: "loading" }).noRequests, false);
  assert.equal(sessionOverview(null, { ...selected, conversations: [{ conversation_id: "a", turn_count: 2 }] }).noRequests, false);
});
test("processing graph excludes stale data from another conversation", () => {
  const model = sessionOverview(null, { ...INITIAL_CHAT_STATE, activeId: "a", status: "ready",
    turns: [{ turn_id: "a1" }, { turn_id: "a2" }],
    postprocess: { a1: { turn_id: "a1", memory: { state: "pending" } }, b1: { turn_id: "b1", memory: { state: "delivered" } } } });
  assert.deepEqual(model.tasks, [{ state: "pending", count: 1 }]);
  assert.equal(model.taskCoverage, 1);
  assert.equal(model.loadedTurns, 2);
});

test("usage is only advertised when the host implements the read-only method", () => {
  const base = { ...INITIAL_CHAT_STATE, activeId: "a", status: "ready", conversations: [{ conversation_id: "a", turn_count: 2 }] };
  assert.equal(sessionOverview(null, base).usageAvailable, false);
  assert.equal(sessionOverview(null, base).usage, null);
  const info = { host: { ready: true }, destination: null, methods: ["conversation.usage"] };
  assert.equal(sessionOverview(info, base).usageAvailable, true);
  const load = { available: true, loading: false, error: false, data: { attempts_total: 2, tokens: { total: 320, complete: true }, latest_request: null, latest_context: null } };
  assert.equal(sessionOverview(info, base, load).usage.tokens.total, 320);
  const unknown = { available: true, loading: false, error: false, data: { attempts_total: 2, tokens: { total: null, complete: false }, latest_request: null, latest_context: null } };
  assert.equal(sessionOverview(info, base, unknown).usage.tokens.total, null);
});
