/**
 * UI-F02: the persona/preference settings view rules.
 *
 * These tests pin the honest distinctions: activation comes from the active
 * pointer (never from `record_status`), tentative stays "this conversation
 * only", inactive never reads as confirmed, and a missing capability is shown
 * as unavailable.
 */
import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";

const build = process.env.QUINTESSENCE_FRONTEND_BUILD;
if (!build) {
  throw new Error("QUINTESSENCE_FRONTEND_BUILD is not set");
}
const require = createRequire(import.meta.url);
const view = require(path.join(build, "adminView.js"));

const {
  activeSummary,
  bodyText,
  canActivate,
  canImport,
  canReadBody,
  errorLabel,
  formatBytes,
  importAvailability,
  inactiveStateLabel,
  preferenceSections,
  profileRows,
  profileSummaryLine,
  scopeLabel,
  t,
} = view;

function version(recordVersion, active) {
  return {
    record_version: recordVersion,
    profile_version: `${recordVersion}.0.0`,
    schema_version: "2",
    recorded_at: "2026-10-29T00:00:00Z",
    record_status: "active",
    is_active_version: active,
    review_event_id: "11111111-1111-4111-8111-111111111111",
  };
}

function profileList() {
  return {
    active: {
      record_id: "record-a",
      record_version: 1,
      profile_id: "example.persona.a",
      degraded: false,
    },
    profiles: [
      {
        profile_id: "example.persona.a",
        record_id: "record-a",
        is_active: true,
        current: {
          record_version: 2,
          profile_version: "2.0.0",
          schema_version: "2",
          record_status: "active",
          recorded_at: "2026-10-29T00:00:00Z",
          asset_hash: "a".repeat(64),
          renderer_compatibility: "pcs.renderer.synthetic-v1",
          style_summary: null,
          principles_count: 2,
          examples_count: 1,
        },
        versions: [version(2, false), version(1, true)],
        versions_total: 2,
        versions_truncated: false,
      },
      {
        profile_id: "example.persona.b",
        record_id: "record-b",
        is_active: false,
        current: {
          record_version: 1,
          profile_version: "1.0.0",
          schema_version: "2",
          record_status: "active",
          recorded_at: "2026-10-29T00:00:00Z",
          asset_hash: "b".repeat(64),
          renderer_compatibility: "pcs.renderer.synthetic-v1",
          style_summary: null,
          principles_count: 0,
          examples_count: 0,
        },
        versions: [version(1, false)],
        versions_total: 1,
        versions_truncated: false,
      },
    ],
    truncated: false,
  };
}

test("activation follows the pointer, not the stored record_status", () => {
  const rows = profileRows(profileList(), "zh-CN");
  assert.equal(rows[0].isActive, true);
  assert.equal(rows[1].isActive, false);
  // Both profiles store record_status=active; only pointer-derived flags count.
  assert.equal(rows[0].versions[0].isActiveVersion, false);
  assert.equal(rows[0].versions[1].isActiveVersion, true);
  assert.equal(rows[1].versions[0].isActiveVersion, false);
  assert.match(profileSummaryLine(rows[0], "zh-CN"), /2 原则 · 1 示例/);
  assert.equal(activeSummary(profileList().active, "zh-CN").recordVersion, 1);
  assert.equal(
    activeSummary({ record_id: "r", record_version: 1, profile_id: null, degraded: true }, "zh-CN").label,
    "激活指针不可读",
  );
});

test("capabilities decide which controls exist", () => {
  assert.equal(canReadBody({ read: true, publish: false, activate: true, read_body: true }), true);
  assert.equal(canReadBody({ read: true, publish: false, activate: false }), false);
  assert.equal(canImport({ read: true, publish: false, activate: true, import: false }), false);
  assert.equal(canActivate(undefined), false);
});

test("import availability is a real empty state, never a fake picker", () => {
  const missing = importAvailability(
    { available: false, reason: "IMPORT_UNAVAILABLE", dir_label: "persona-import", files: [] },
    "zh-CN",
  );
  assert.equal(missing.available, false);
  assert.match(missing.message, /persona-import/);
  assert.deepEqual(missing.rows, []);
  const empty = importAvailability(
    { available: true, reason: null, dir_label: "persona-import", files: [] },
    "zh-CN",
  );
  assert.equal(empty.available, true);
  assert.match(empty.message, /没有可导入/);
  const listed = importAvailability(
    {
      available: true,
      reason: null,
      dir_label: "persona-import",
      files: [{ name: "alpha.json", size_bytes: 2048, modified_at: "2026-10-29T00:00:00Z" }],
    },
    "zh-CN",
  );
  assert.deepEqual(listed.rows.map((row) => row.name), ["alpha.json"]);
  assert.equal(listed.rows[0].size, "2.0 KiB");
});

test("preference groups keep tentative and inactive out of confirmed", () => {
  const list = {
    confirmed: [
      {
        dimension: "length",
        value: "concise",
        label: "简短",
        claim: "用户在聊天中希望回答保持简短。",
        strength: "limited",
        scope: { scene: "lab" },
        source: { kind: "user_statement", input_id: "i-1" },
        record: { record_id: "r-1", record_version: 1, recorded_at: "2026-10-29T00:00:00Z" },
      },
    ],
    tentative: [
      {
        dimension: "length",
        value: "detailed",
        label: "详细",
        status: "tentative",
        scope: { session_id: "c-1" },
        source: { kind: "user_statement", input_id: "i-2" },
        record: { record_id: "r-2", record_version: 1, recorded_at: "2026-10-29T00:00:00Z" },
      },
    ],
    inactive: [
      {
        kind: "tentative",
        state: "revoked",
        status: "revoked",
        dimension: "length",
        value: "concise",
        label: "简短",
        reason: "用户在设置中撤销",
        scope: { session_id: "c-1" },
        record: { record_id: "r-3", record_version: 2, recorded_at: "2026-10-29T00:00:00Z" },
        superseded_by: null,
      },
      {
        kind: "confirmed",
        state: "evidence_deleted",
        status: "supported",
        dimension: "tone",
        value: "warm",
        label: "温柔",
        reason: null,
        scope: {},
        record: { record_id: "r-4", record_version: 1, recorded_at: "2026-10-29T00:00:00Z" },
        superseded_by: null,
      },
    ],
    conversation: { conversation_id: "c-1", title: "Synthetic" },
    expression_recording: { enabled: true, reason: null },
    confirmed_recording: { enabled: false, reason: "COGNITION_PROPOSE_DISABLED" },
    truncated: false,
  };
  const sections = preferenceSections(list, "zh-CN");
  assert.equal(sections.confirmed.length, 1);
  assert.equal(sections.confirmed[0].stateLabel, "已确认 · 实例范围");
  assert.equal(sections.confirmed[0].canRevoke, false);
  assert.equal(sections.tentative[0].stateLabel, "暂定 · 仅本会话，未长期记住");
  assert.equal(sections.tentative[0].canRevoke, true);
  assert.equal(sections.tentative[0].scope, "仅本会话");
  assert.equal(sections.inactive[0].stateLabel, "已撤销");
  assert.equal(sections.inactive[1].stateLabel, "证据已删除，未生效");
  assert.ok(
    sections.inactive.every(
      (row) => !row.stateLabel.includes("已确认") && row.canRevoke === false,
    ),
  );
  assert.ok(sections.notes.some((note) => note.includes("cognition.propose=false")));
});

test("a missing conversation explains legacy records and disabled cognition", () => {
  const sections = preferenceSections(
    {
      confirmed: [],
      tentative: [],
      inactive: [],
      conversation: null,
      expression_recording: { enabled: false, reason: "COGNITION_DISABLED" },
      confirmed_recording: { enabled: false, reason: "COGNITION_DISABLED" },
      truncated: false,
    },
    "en",
  );
  assert.ok(sections.notes.some((note) => note.includes("Cognition is disabled")));
  assert.ok(sections.notes.some((note) => note.includes("Legacy tentative records are per conversation")));
  assert.ok(sections.notes.some((note) => note.includes("mechanism is retired")));
  assert.ok(sections.notes.every((note) => !note.includes("tentative records still work")));
});

test("scopes, inactive states, errors and bytes are explicit", () => {
  assert.equal(scopeLabel({ session_id: "c-1" }, "zh-CN"), "仅本会话");
  assert.equal(scopeLabel({ scene: "lab" }, "zh-CN"), "scene=lab");
  assert.equal(scopeLabel({}, "zh-CN"), "实例范围");
  assert.equal(inactiveStateLabel("superseded", "zh-CN"), "已被确认值取代");
  assert.equal(inactiveStateLabel("retracted", "zh-CN"), "已撤销（确认记录）");
  assert.equal(inactiveStateLabel("source_deleted", "zh-CN"), "来源已删除，未生效");
  assert.equal(inactiveStateLabel("unsupported", "zh-CN"), "未生效");
  assert.equal(errorLabel("IMPORT_INVALID", "zh-CN"), t("zh-CN", "errorIMPORTINVALID"));
  assert.equal(errorLabel("BUSY", "zh-CN"), "本轮运行中，请结束后再试。");
  assert.equal(errorLabel("NOT_A_REAL_CODE", "zh-CN"), null);
  assert.equal(formatBytes(2048), "2.0 KiB");
  assert.equal(formatBytes(512), "512 B");
  assert.equal(bodyText({ body: { profile_id: "x" } }), '{\n  "profile_id": "x"\n}');
});
