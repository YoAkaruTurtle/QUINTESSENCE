import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";

const build = process.env.QUINTESSENCE_FRONTEND_BUILD;
if (!build) {
  throw new Error("QUINTESSENCE_FRONTEND_BUILD is not set");
}
const require = createRequire(import.meta.url);
const format = require(path.join(build, "format.js"));

const { personaLabel, personaSourceLabel, reasonLabel, speakerLabel } = format;

function info(persona) {
  return {
    app: "quintessence-desktop",
    version: "0.1.0",
    protocol: "desktop.v1",
    mode: "lab",
    label: "Lab",
    ai_display_name: null,
    persona,
    ui: { ai_display_name: "助手", source: "default" },
    session_id: "s",
    host: { ready: true },
  };
}

test("the persona chip shows the active profile id and version", () => {
  const ready = info({
    state: "ready",
    source: "expected",
    profile_id: "example.persona.alpha",
    profile_version: "1",
    expected: { profile_id: "example.persona.alpha", profile_version: "1" },
    matches_expected: true,
    speaker: { identity: "owner", display_name: "小明" },
    reason_code: null,
  });
  assert.equal(personaLabel(ready), "人格：example.persona.alpha v1");
  assert.equal(personaSourceLabel(ready), "启动契约指定（app.persona_expected）");
  assert.equal(speakerLabel(ready), "所有者 · 小明");
});

test("an unready persona is shown without claiming an identity", () => {
  const missing = info({
    state: "not_ready",
    source: null,
    profile_id: null,
    profile_version: null,
    expected: { profile_id: "example.absent" },
    matches_expected: false,
    speaker: null,
    reason_code: "EXPECTED_PERSONA_UNAVAILABLE",
  });
  assert.equal(personaLabel(missing), "人格未就绪");
  assert.equal(speakerLabel(missing), "未声明（按身份未知处理）");
  assert.equal(
    reasonLabel("EXPECTED_PERSONA_UNAVAILABLE", null),
    "指定的新人格不可用，宿主已停止初始化（没有回退默认人格）",
  );
  assert.equal(reasonLabel("NEUTRAL_PROVISION_FAILED", null), "默认人格初始化未完成");
  assert.equal(reasonLabel("PROFILE_CORRUPT", null), "已保存的人格记录不完整或已被删除");
});

test("a known-other speaker never renders the owner label", () => {
  const other = info({
    state: "ready",
    source: "neutral_provisioned",
    profile_id: "quintessence.desktop.general",
    profile_version: "1",
    expected: null,
    matches_expected: null,
    speaker: { identity: "known_other" },
    reason_code: null,
  });
  assert.equal(speakerLabel(other), "已知他人");
  assert.equal(personaLabel(other), "人格：quintessence.desktop.general v1");
  assert.equal(personaSourceLabel(other), "中性默认（宿主初始化）");
});
