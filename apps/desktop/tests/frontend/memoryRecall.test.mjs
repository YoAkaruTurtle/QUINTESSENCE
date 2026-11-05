import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";

const build = process.env.QUINTESSENCE_FRONTEND_BUILD;
if (!build) {
  throw new Error("QUINTESSENCE_FRONTEND_BUILD is not set");
}
const require = createRequire(import.meta.url);
const { recallPanelModel } = require(path.join(build, "memoryRecallView.js"));

function status(overrides = {}) {
  return {
    enabled: false,
    configured: false,
    declared: true,
    bridge_configured: true,
    max_items: 4,
    budget_units: 2048,
    timeout_seconds: 2,
    timezone: "Asia/Shanghai",
    source: "lab",
    missing: ["memory.recall.enabled"],
    apply: "next_turn",
    managed: false,
    editable: true,
    sidecar_available: true,
    host_ready: true,
    ...overrides,
  };
}

test("a configured recall surface exposes bounded values and a real toggle", () => {
  const model = recallPanelModel(status({ enabled: true, configured: true }), "zh-CN");
  assert.equal(model.headline, "已开启");
  assert.equal(model.editable, true);
  assert.equal(model.values.max_items, 4);
  assert.equal(model.values.budget_units, 2048);
  assert.equal(model.values.timeout_ms, 2000);
  assert.equal(model.values.timezone, "Asia/Shanghai");
  assert.ok(model.rows.some((row) => row.value === "可用"));
  assert.ok(model.notes.some((note) => note.includes("下一轮")));
});

test("a missing bridge is unavailable, not disabled by the user", () => {
  const model = recallPanelModel(
    status({ enabled: true, bridge_configured: false, editable: false }),
    "zh-CN",
  );
  assert.equal(model.headline, "不可用");
  assert.equal(model.editable, false);
  assert.ok(model.notes.some((note) => note.includes("记忆桥未配置")));
});

test("a disabled surface still shows the explicit off note", () => {
  const model = recallPanelModel(status({ enabled: false }), "en");
  assert.equal(model.headline, "Off");
  assert.ok(model.notes.some((note) => note.includes("explicit memory.search")));
});

test("unreadable status is not guessed as available", () => {
  const model = recallPanelModel(null, "en");
  assert.equal(model.headline, "Unavailable");
  assert.equal(model.editable, false);
  assert.equal(model.rows.length, 0);
});
