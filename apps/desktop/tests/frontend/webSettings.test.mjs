import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";

const build = process.env.QUINTESSENCE_FRONTEND_BUILD;
if (!build) {
  throw new Error("QUINTESSENCE_FRONTEND_BUILD is not set");
}
const require = createRequire(import.meta.url);
const {
  webState,
  webHeadline,
  webPanelModel,
  webSourceLabel,
} = require(path.join(build, "webSettingsView.js"));

function status(overrides = {}) {
  return {
    enabled: false,
    configured: false,
    declared: true,
    search: true,
    fetch: true,
    credential_present: false,
    credential_env: "DEEPSEEK_API_KEY",
    allowed_hosts: 0,
    host_policy: "any_public_https",
    source: "external",
    missing: ["web.enabled"],
    managed: false,
    editable: true,
    sidecar_available: true,
    credential_restart_required: false,
    host_ready: true,
    reason_code: null,
    ...overrides,
  };
}

test("a configured surface is available and offers a real off switch", () => {
  const value = status({ enabled: true, configured: true, credential_present: true });
  assert.equal(webState(value), "available");
  assert.equal(webHeadline("available", "zh-CN"), "可用");
  const model = webPanelModel(value, "zh-CN");
  assert.equal(model.toggleLabel, "关闭联网");
  assert.equal(model.editable, true);
  assert.ok(model.rows.some((row) => row.value.includes("未验证")));
  assert.ok(!model.notes.some((note) => note.includes("未配置")));
});

test("an enabled switch without a credential is never called available", () => {
  const value = status({ enabled: true, missing: ["web.api_key_env"] });
  assert.equal(webState(value), "enabled_no_credential");
  assert.notEqual(webHeadline("enabled_no_credential", "zh-CN"), "可用");
  const model = webPanelModel(value, "zh-CN");
  assert.equal(model.headline, "已开启 · 缺少凭据");
  assert.ok(model.notes.some((note) => note.includes("DEEPSEEK_API_KEY")));
  assert.ok(model.notes.some((note) => note.includes("不会伪造结果")));
  // The toggle stays real: the user can still turn the surface off.
  assert.equal(model.toggleLabel, "关闭联网");
});

test("a missing web block is 'not configured', not 'disabled'", () => {
  const value = status({ declared: false, search: true, fetch: true, missing: ["web"] });
  assert.equal(webState(value), "unconfigured");
  assert.equal(webPanelModel(value, "zh-CN").headline, "未配置");
  const model = webPanelModel(value, "zh-CN");
  assert.ok(model.notes.some((note) => note.includes("没有 web 块")));
  assert.equal(model.toggleLabel, "启用联网");
});

test("an explicitly disabled block reads as disabled", () => {
  const value = status({ enabled: false });
  assert.equal(webState(value), "disabled");
  assert.equal(webPanelModel(value, "en").headline, "Disabled");
  assert.equal(webPanelModel(value, "en").toggleLabel, "Turn web access on");
});

test("host status that cannot be read renders no guessed web state", () => {
  assert.equal(webState(null), "unavailable");
  assert.equal(webState(status({ sidecar_available: false })), "unavailable");
  assert.equal(webState(status({ source: null })), "unavailable");
  const model = webPanelModel(null, "zh-CN");
  assert.equal(model.toggleLabel, null);
  assert.equal(model.editable, false);
  assert.equal(model.rows.length, 0);
  assert.ok(model.notes.some((note) => note.includes("不显示推测")));
});

test("a read-only config gets an explicit edit instruction, not a fake switch", () => {
  const model = webPanelModel(status({ editable: false }), "zh-CN");
  assert.equal(model.toggleLabel, null);
  assert.ok(model.editableNote.includes("不可写"));
  assert.ok(model.editableNote.includes("web 块"));
});

test("the writer target is named per source without leaking a path or key", () => {
  const managed = webPanelModel(
    status({ source: "managed", managed: true, enabled: true, configured: true, credential_present: true }),
    "zh-CN",
  );
  assert.ok(managed.editableNote.includes("应用设置"));
  const external = webPanelModel(status({ source: "external" }), "zh-CN");
  assert.ok(external.editableNote.includes("原子写回"));
  const lab = webPanelModel(status({ source: "lab" }), "zh-CN");
  assert.ok(lab.editableNote.includes("Lab"));
  assert.equal(webSourceLabel("lab", "en"), "Lab configuration");

  const hostile = status({
    enabled: true,
    configured: true,
    credential_present: true,
    api_key: "synthetic-unverified-key-material",
    config_path: "/private/host.json",
  });
  const rendered = JSON.stringify(webPanelModel(hostile, "zh-CN"));
  assert.ok(!rendered.includes("synthetic-unverified-key-material"));
  assert.ok(!rendered.includes("private"));
});

test("a saved-but-not-loaded credential is explained", () => {
  const model = webPanelModel(
    status({ enabled: true, credential_restart_required: true }),
    "zh-CN",
  );
  assert.ok(model.notes.some((note) => note.includes("重启应用")));
});
