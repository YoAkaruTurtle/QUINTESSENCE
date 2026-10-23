import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const app = path.resolve(here, "..", "..");
const build = process.env.QUINTESSENCE_FRONTEND_BUILD;
if (!build) {
  throw new Error("QUINTESSENCE_FRONTEND_BUILD is not set");
}
const require = createRequire(import.meta.url);
const stateModule = require(path.join(build, "state.js"));
const formatModule = require(path.join(build, "format.js"));

const {
  INITIAL_STATE,
  actionsFor,
  buildConsultParams,
  isTerminal,
  needsConfirmation,
  reduce,
  sendEnabled,
  validateQuestion,
} = stateModule;
const {
  destinationLabel,
  detailLabel,
  materialNotice,
  queryStatusLabel,
  reasonLabel,
  shortId,
  sourceCountLabel,
  sourceRows,
  aiDisplayName,
  avatarLetter,
} = formatModule;

const NS = "example.test.desktop.lab";

function destination(external = true) {
  return {
    destination_id: external ? "sha256:external-destination" : "sha256:local-destination",
    provider: "synthetic",
    model: "scripted-collab-v1",
    external,
    kind: external ? "external" : "local",
    confirmation_required: external,
    manifest_hash: "sha256:manifest",
    policy_ref: "policy:collab.v1",
  };
}

function host(options = {}) {
  const external = options.external ?? true;
  const ready = options.ready ?? true;
  return {
    app: "quintessence-desktop",
    version: "0.1.0",
    protocol: "desktop.v1",
    mode: "lab",
    label: "QUINTESSENCE Lab",
    ai_display_name: options.aiName ?? "小助手",
    ui: {
      ai_display_name: options.aiName ?? "小助手",
      source: options.aiSource ?? "default",
    },
    session_id: "11111111-1111-4111-8111-111111111111",
    host: {
      namespace: NS,
      host_instance: "host:desktop-lab",
      operator: "operator:desktop-lab",
      retention: "standard",
      input_privacy_class: "private",
      policy_ref: "policy:collab.v1",
      ready,
      reason_code: ready ? null : "CONFIG_MISSING",
      reason_detail: ready ? null : "no private configuration",
    },
    destination: destination(external),
    workspace: { consult: ready, review: false, review_reason: "REVIEW_NOT_CONFIGURED" },
    capabilities: {
      consult: ready,
      confirm_destination: ready,
      input_registration: ready,
      review: {
        read: false,
        generate: false,
        approve: false,
        reject: false,
        result: false,
      },
    },
    limits: {
      frame_bytes: 1048576,
      request_timeout_seconds: 120,
      max_question_chars: 8000,
    },
    methods: ["app.confirm_destination", "app.info", "bridge.ping", "consult.interact"],
    reserved: ["consult.result", "consult.history", "consult.resume", "review.generate"],
    pcs: {
      package: "quintessence_pcs",
      version: "pcs.v2",
      schema_migrations: 10,
      facade: "ConsultFacade",
    },
  };
}

function result(state = "answered", extra = {}) {
  return {
    interaction_id: "22222222-2222-4222-8222-222222222222",
    context_id: "33333333-3333-4333-8333-333333333333",
    state,
    reply: state === "answered" ? "合成回答：三条待办。" : state === "waiting" ? null : null,
    detail: state === "failed" ? "model:Timeout" : null,
    query: {
      kind: "belief",
      status: "ok",
      material_status: "partial",
      truncated: false,
      policy_version: "collab.v1.query.v1",
      items: [
        {
          ref: { ref_type: "record", namespace: NS, record_id: "record-1", record_version: 1 },
          current: true,
          expired: false,
        },
        {
          ref: { ref_type: "record", namespace: NS, record_id: "record-2", record_version: 2 },
          current: false,
          expired: false,
        },
        {
          ref: { ref_type: "record", namespace: NS, record_id: "record-3", record_version: 1 },
          current: false,
          expired: true,
        },
      ],
    },
    query_source_refs: [
      { ref_type: "record", namespace: NS, record_id: "record-1", record_version: 1 },
      { ref_type: "record", namespace: NS, record_id: "record-2", record_version: 2 },
      { ref_type: "record", namespace: NS, record_id: "record-3", record_version: 1 },
      { ref_type: "event", namespace: NS, event_id: "event-1" },
    ],
    query_result_ref: {
      ref_type: "collab_artifact",
      namespace: NS,
      artifact_id: "artifact-query",
      artifact_version: 1,
    },
    reply_artifact_ref: {
      ref_type: "collab_artifact",
      namespace: NS,
      artifact_id: "artifact-reply",
      artifact_version: 1,
    },
    input: {
      input_ref: {
        ref_type: "collab_input",
        namespace: NS,
        input_id: "input-1",
        content_version: 1,
      },
      origin_kind: "user_statement",
      privacy_class: "private",
      retention: "standard",
    },
    destination: destination(true),
    ...extra,
  };
}

function ready(external = true) {
  return reduce(INITIAL_STATE, { type: "host-info", info: host({ external }) });
}

test("boot transitions to input only after a ready host", () => {
  assert.equal(INITIAL_STATE.phase, "booting");
  const input = ready();
  assert.equal(input.phase, "input");
  assert.equal(input.host.host.ready, true);
});

test("AI display name is separate from the software brand", () => {
  const info = host({ aiName: "小助手" });
  assert.equal(aiDisplayName(info), "小助手");
  assert.equal(avatarLetter("小助手"), "小");
  assert.notEqual(aiDisplayName(info), "QUINTESSENCE");
  const defaulted = { ...info, ui: undefined, ai_display_name: null };
  assert.equal(aiDisplayName(defaulted), "助手");
  assert.equal(avatarLetter(""), "AI");
});

test("a sent question becomes the user message and a new question clears it", () => {
  let state = ready(false);
  state = reduce(state, { type: "draft", question: "这是第一轮问题" });
  state = reduce(state, { type: "send-request" });
  assert.equal(state.lastQuestion, "这是第一轮问题");
  assert.equal(state.phase, "sending");
  assert.equal(state.question, "这是第一轮问题");
  state = reduce(state, { type: "result", result: result("answered") });
  assert.equal(state.question, "");
  assert.equal(state.lastQuestion, "这是第一轮问题");
  state = reduce(state, { type: "new-question" });
  assert.equal(state.question, "");
  assert.equal(state.lastQuestion, "");
});

test("typing after a result can start a new independent turn", () => {
  let state = ready(false);
  state = reduce(state, { type: "draft", question: "第一轮" });
  state = reduce(state, { type: "send-request" });
  state = reduce(state, {
    type: "result",
    result: result("answered", { destination: destination(false) }),
  });
  assert.equal(sendEnabled(state), false);
  state = reduce(state, { type: "draft", question: "第二轮" });
  assert.equal(sendEnabled(state), true);
  state = reduce(state, { type: "send-request" });
  assert.equal(state.phase, "sending");
  assert.equal(state.result, null);
  assert.equal(state.lastQuestion, "第二轮");
});

test("a not-ready host disables sending and shows not-ready", () => {
  const state = reduce(INITIAL_STATE, {
    type: "host-info",
    info: host({ ready: false }),
  });
  assert.equal(state.phase, "not-ready");
  assert.equal(sendEnabled(state), false);
});

test("send requires a non-empty question and a ready host", () => {
  let state = ready();
  assert.equal(sendEnabled(state), false);
  state = reduce(state, { type: "draft", question: "   " });
  assert.equal(sendEnabled(state), false);
  state = reduce(state, { type: "draft", question: "合成问题" });
  assert.equal(sendEnabled(state), true);
  const notReady = reduce(
    reduce(INITIAL_STATE, { type: "host-info", info: host({ ready: false }) }),
    { type: "draft", question: "合成问题" },
  );
  assert.equal(sendEnabled(notReady), false);
});

test("external destination asks for confirmation before sending", () => {
  let state = ready(true);
  state = reduce(state, { type: "draft", question: "合成问题" });
  assert.equal(needsConfirmation(state), true);
  state = reduce(state, { type: "send-request" });
  assert.equal(state.phase, "input");
  assert.equal(state.confirmPending, true);
  assert.equal(state.confirmedDestinationId, null);
  state = reduce(state, { type: "confirm-cancel" });
  assert.equal(state.confirmPending, false);
  state = reduce(state, { type: "send-request" });
  state = reduce(state, { type: "confirm-accepted" });
  assert.equal(state.phase, "sending");
  assert.equal(state.confirmedDestinationId, "sha256:external-destination");
});

test("local destination sends without a confirmation step", () => {
  let state = ready(false);
  state = reduce(state, { type: "draft", question: "合成问题" });
  assert.equal(needsConfirmation(state), false);
  state = reduce(state, { type: "send-request" });
  assert.equal(state.phase, "sending");
  assert.equal(state.confirmPending, false);
});

test("result states map to distinct phases", () => {
  for (const value of ["answered", "waiting", "failed", "incomplete", "stale"]) {
    let state = ready();
    state = reduce(state, { type: "draft", question: "合成问题" });
    state = reduce(state, { type: "confirm-accepted" });
    state = reduce(state, { type: "result", result: result(value) });
    assert.equal(state.phase, value);
    assert.equal(isTerminal(value), true);
  }
});

test("unknown and busy are not collapsed into failure", () => {
  let state = ready();
  state = reduce(state, {
    type: "error",
    error: { code: "TIMEOUT", message: "timeout", result_unknown: true },
  });
  assert.equal(state.phase, "unknown");
  state = reduce(state, {
    type: "error",
    error: { code: "BUSY", message: "busy", result_unknown: false },
  });
  assert.equal(state.phase, "busy");
});

test("host-not-ready and registration failures stay actionable", () => {
  let state = ready();
  state = reduce(state, {
    type: "error",
    error: { code: "DATA_LOCKED", message: "locked", result_unknown: false },
  });
  assert.equal(state.phase, "not-ready");
  state = ready();
  state = reduce(state, { type: "draft", question: "合成问题" });
  state = reduce(state, {
    type: "error",
    error: {
      code: "INPUT_REGISTRATION_FAILED",
      message: "registration failed",
      result_unknown: false,
    },
  });
  assert.equal(state.phase, "input");
  assert.equal(state.question, "合成问题");
  assert.equal(state.error.code, "INPUT_REGISTRATION_FAILED");
});

test("confirmation-required clears the local confirmation flag", () => {
  let state = ready();
  state = reduce(state, { type: "draft", question: "合成问题" });
  state = reduce(state, { type: "confirm-accepted" });
  state = reduce(state, {
    type: "error",
    error: {
      code: "CONFIRMATION_REQUIRED",
      message: "missing",
      result_unknown: false,
    },
  });
  assert.equal(state.phase, "input");
  assert.equal(state.confirmedDestinationId, null);
  assert.equal(state.confirmPending, true);
});

test("new question keeps a same-destination confirmation", () => {
  let state = ready();
  state = reduce(state, { type: "draft", question: "合成问题" });
  state = reduce(state, { type: "confirm-accepted" });
  state = reduce(state, { type: "result", result: result("answered") });
  state = reduce(state, { type: "new-question" });
  assert.equal(state.phase, "input");
  assert.equal(state.result, null);
  assert.equal(state.confirmedDestinationId, "sha256:external-destination");
});

test("a changed destination invalidates the confirmation", () => {
  let state = ready();
  state = reduce(state, { type: "draft", question: "合成问题" });
  state = reduce(state, { type: "confirm-accepted" });
  const changed = host();
  changed.destination = {
    ...destination(true),
    destination_id: "sha256:new-destination",
  };
  changed.destination.model = "scripted-collab-v2";
  changed.destination.manifest_hash = "sha256:manifest-2";
  state = reduce(state, { type: "host-info", info: changed });
  assert.equal(state.confirmedDestinationId, null);
  assert.equal(state.destination.destination_id, "sha256:new-destination");
  assert.equal(state.destinationChanged, true);
});

test("the frontend request body names no authority field", () => {
  let state = ready();
  state = reduce(state, { type: "draft", question: "合成问题" });
  const params = buildConsultParams(state);
  assert.deepEqual(Object.keys(params).sort(), [
    "confirmedDestinationId",
    "question",
    "sessionId",
  ]);
  assert.equal(params.confirmedDestinationId, null);
  const serialized = JSON.stringify(params);
  for (const forbidden of [
    "input_refs",
    "origin_kind",
    "privacy",
    "retention",
    "namespace",
    "host_instance",
    "policy_ref",
    "query_plan",
    "provider",
    "model",
    "endpoint",
  ]) {
    assert.equal(serialized.includes(forbidden), false, forbidden);
  }
  state = reduce(state, { type: "confirm-accepted" });
  assert.equal(
    buildConsultParams(state).confirmedDestinationId,
    "sha256:external-destination",
  );
});

test("answered offers copy and new question, never Review or candidate actions", () => {
  const actions = actionsFor({ ...ready(), phase: "answered", result: result() });
  assert.deepEqual(actions, ["copy", "new-question", "sources", "destination"]);
  assert.equal(actions.includes("review"), false);
  assert.equal(actions.includes("save-candidate"), false);
  const waiting = actionsFor({ ...ready(), phase: "waiting", result: result("waiting") });
  assert.equal(waiting.includes("copy"), false);
  assert.equal(waiting.includes("new-question"), true);
  const unknown = actionsFor({ ...ready(), phase: "unknown" });
  assert.equal(unknown.includes("retry"), false);
});

test("every phase action set is free of Review and candidate actions", () => {
  for (const phase of [
    "booting",
    "not-ready",
    "input",
    "sending",
    "answered",
    "waiting",
    "failed",
    "incomplete",
    "stale",
    "unknown",
    "busy",
  ]) {
    const actions = actionsFor({ ...ready(), phase });
    for (const action of actions) {
      assert.notEqual(action, "review", phase);
      assert.notEqual(action, "save-candidate", phase);
      assert.notEqual(action, "approve", phase);
      assert.notEqual(action, "reject", phase);
    }
  }
});

test("material notice follows the public status/material_status table", () => {
  const make = (status, material) => ({
    kind: "belief",
    status,
    material_status: material,
    truncated: false,
    policy_version: "v1",
    items: [],
  });
  assert.equal(materialNotice(make("ok", "complete")), null);
  assert.match(materialNotice(make("ok", "partial")), /可能不完整/);
  assert.match(materialNotice(make("empty", "complete")), /没有可披露的结果/);
  assert.match(materialNotice(make("empty", "partial")), /可能不完整/);
  assert.match(materialNotice(make("unavailable", "complete")), /不可用/);
  assert.equal(materialNotice(make("unknown", "partial")), null);
  assert.equal(materialNotice(null), null);
});

test("source rows distinguish current, historical and expired refs", () => {
  const rows = sourceRows(result());
  const statuses = rows.map((row) => row.status);
  assert.equal(statuses.includes("当前"), true);
  assert.equal(statuses.includes("历史"), true);
  assert.equal(statuses.includes("已过期"), true);
  assert.equal(rows[0].kind, "输入");
  assert.equal(rows.some((row) => row.kind === "查询产物"), true);
  assert.equal(rows.some((row) => row.kind === "回复产物"), true);
  assert.equal(sourceRows(null).length, 0);
  assert.equal(sourceCountLabel(result()), "来源：输入 1 · 查询 4");
});

test("formatting helpers are stable and do not leak full ids", () => {
  assert.equal(shortId("sha256:abcdef0123456789"), "abcdef01…");
  assert.equal(shortId(null), "—");
  assert.equal(
    destinationLabel(destination(true)),
    "外部 · synthetic / scripted-collab-v1",
  );
  assert.equal(
    destinationLabel(destination(false)),
    "本地 · synthetic / scripted-collab-v1",
  );
  assert.equal(reasonLabel("DATA_LOCKED", null), "数据实例正在被另一个进程使用");
  assert.equal(detailLabel("model:Timeout"), "模型调用未完成");
  assert.equal(detailLabel(null), "未分类的未完成原因");
  assert.equal(
    queryStatusLabel({
      kind: "belief",
      status: "empty",
      material_status: "partial",
      truncated: false,
      policy_version: "v1",
      items: [],
    }),
    "查询完成；没有可披露结果",
  );
});

test("question validation bounds empty, long and NUL input", () => {
  assert.match(validateQuestion("", 8000), /写下问题/);
  assert.match(validateQuestion("   ", 8000), /写下问题/);
  assert.equal(validateQuestion("合成问题", 8000), null);
  assert.match(validateQuestion("x".repeat(8001), 8000), /8000/);
  assert.match(validateQuestion("bad\u0000text", 8000), /不可用字符/);
});

test("a result survives a host refresh that becomes not-ready", () => {
  let state = ready();
  state = reduce(state, { type: "draft", question: "合成问题" });
  state = reduce(state, { type: "confirm-accepted" });
  state = reduce(state, { type: "result", result: result("answered") });
  state = reduce(state, {
    type: "host-info",
    info: host({ ready: false }),
  });
  assert.equal(state.phase, "answered");
  assert.equal(state.result.state, "answered");
});

test("the source tree contains no browser persistence or HTML injection", () => {
  const files = fs
    .readdirSync(path.join(app, "src"))
    .filter((name) => /\.(ts|css|html)$/.test(name));
  for (const name of files) {
    const text = fs.readFileSync(path.join(app, "src", name), "utf8");
    assert.equal(/localStorage|indexedDB|sessionStorage/.test(text), false, name);
    assert.equal(/\.innerHTML\s*=/.test(text), false, name);
  }
});
