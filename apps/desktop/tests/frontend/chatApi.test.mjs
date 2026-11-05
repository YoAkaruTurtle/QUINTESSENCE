import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";

const build = process.env.QUINTESSENCE_FRONTEND_BUILD;
if (!build) {
  throw new Error("QUINTESSENCE_FRONTEND_BUILD is not set");
}
const require = createRequire(import.meta.url);
const { createChatApi } = require(path.join(build, "chatApi.js"));

function spy() {
  const calls = [];
  const api = createChatApi(async (command, args) => {
    calls.push([command, args]);
    return { ok: true };
  });
  return { api, calls };
}

test("conversation_create sends sessionId + mode", async () => {
  const { api, calls } = spy();
  await api.conversationCreate("s-1", "chat");
  assert.deepEqual(calls, [
    ["conversation_create", { sessionId: "s-1", mode: "chat" }],
  ]);
});

test("conversation_list sends sessionId and omits absent options", async () => {
  const { api, calls } = spy();
  await api.conversationList("s-1");
  await api.conversationList("s-1", { limit: 5 });
  await api.conversationList("s-1", { limit: 5, cursor: "c-1" });
  assert.deepEqual(calls, [
    ["conversation_list", { sessionId: "s-1" }],
    ["conversation_list", { sessionId: "s-1", limit: 5 }],
    ["conversation_list", { sessionId: "s-1", limit: 5, cursor: "c-1" }],
  ]);
});

test("conversation_get sends sessionId + conversationId + options", async () => {
  const { api, calls } = spy();
  await api.conversationGet("s-1", "c-1");
  await api.conversationGet("s-1", "c-1", {
    beforeTurnNo: 2,
    beforeSeq: 1,
    limit: 10,
  });
  assert.deepEqual(calls, [
    ["conversation_get", { sessionId: "s-1", conversationId: "c-1" }],
    [
      "conversation_get",
      {
        sessionId: "s-1",
        conversationId: "c-1",
        beforeTurnNo: 2,
        beforeSeq: 1,
        limit: 10,
      },
    ],
  ]);
});

test("conversation_send sends exactly the Rust parameter set", async () => {
  const { api, calls } = spy();
  await api.conversationSend({
    sessionId: "s-1",
    conversationId: "c-1",
    clientRequestId: "r-1",
    text: "你好",
    confirmedDestinationId: null,
  });
  assert.deepEqual(calls, [
    [
      "conversation_send",
      {
        sessionId: "s-1",
        conversationId: "c-1",
        clientRequestId: "r-1",
        text: "你好",
        confirmedDestinationId: null,
      },
    ],
  ]);
  assert.deepEqual(Object.keys(calls[0][1]).sort(), [
    "clientRequestId",
    "confirmedDestinationId",
    "conversationId",
    "sessionId",
    "text",
  ]);
});

test("turn_result and bridge_ping send sessionId / empty args", async () => {
  const { api, calls } = spy();
  await api.turnResult("s-1", "t-1");
  await api.bridgePing();
  assert.deepEqual(calls, [
    ["turn_result", { sessionId: "s-1", turnId: "t-1" }],
    ["bridge_ping", {}],
  ]);
});

test("no wrapper sends snake_case authority-style keys", async () => {
  const { api, calls } = spy();
  await api.conversationCreate("s-1", "chat");
  await api.conversationGet("s-1", "c-1");
  for (const [, args] of calls) {
    for (const key of [
      "session_id",
      "conversation_id",
      "client_request_id",
      "namespace",
      "host_instance",
      "privacy_classes",
      "retention",
      "interaction_id",
      "context_id",
    ]) {
      assert.equal(Object.hasOwn(args, key), false, key);
    }
  }
});

test("turn_events sends afterSeq/limit and omits absent options", async () => {
  const { api, calls } = spy();
  await api.turnEvents("s-1", "t-1");
  await api.turnEvents("s-1", "t-1", { afterSeq: 7, limit: 50 });
  assert.deepEqual(calls, [
    ["turn_events", { sessionId: "s-1", turnId: "t-1" }],
    [
      "turn_events",
      { sessionId: "s-1", turnId: "t-1", afterSeq: 7, limit: 50 },
    ],
  ]);
});

test("turn_steps and turn_cancel send exactly their Rust parameters", async () => {
  const { api, calls } = spy();
  await api.turnSteps("s-1", "t-1", { afterStepNo: 2, limit: 20 });
  await api.turnCancel("s-1", "t-1");
  assert.deepEqual(calls, [
    [
      "turn_steps",
      { sessionId: "s-1", turnId: "t-1", afterStepNo: 2, limit: 20 },
    ],
    ["turn_cancel", { sessionId: "s-1", turnId: "t-1" }],
  ]);
});

test("the process wrappers never send authority fields", async () => {
  const { api, calls } = spy();
  await api.turnEvents("s-1", "t-1", { afterSeq: 0, limit: 100 });
  await api.turnSteps("s-1", "t-1", { limit: 20 });
  await api.turnCancel("s-1", "t-1");
  for (const [, args] of calls) {
    for (const key of [
      "namespace",
      "host_instance",
      "access_ref",
      "privacy_classes",
      "retention",
      "interaction_id",
      "context_id",
    ]) {
      assert.equal(Object.hasOwn(args, key), false, key);
    }
  }
});

test("transcript search and postprocess wrappers send exact keys", async () => {
  const { api, calls } = spy();
  await api.conversationSearch("s-1", "读书会");
  await api.conversationSearch("s-1", "读书会", {
    limit: 3,
    conversationId: "c-1",
    before: "cursor-1",
    includeFull: true,
  });
  await api.postprocessStatus("s-1", "c-1");
  await api.postprocessStatus("s-1", "c-1", ["t-1"]);
  await api.postprocessRetry("s-1", "t-1");
  assert.deepEqual(calls, [
    ["conversation_search", { sessionId: "s-1", query: "读书会" }],
    [
      "conversation_search",
      {
        sessionId: "s-1",
        query: "读书会",
        limit: 3,
        conversationId: "c-1",
        before: "cursor-1",
        includeFull: true,
      },
    ],
    ["postprocess_status", { sessionId: "s-1", conversationId: "c-1" }],
    [
      "postprocess_status",
      { sessionId: "s-1", conversationId: "c-1", turnIds: ["t-1"] },
    ],
    ["postprocess_retry", { sessionId: "s-1", turnId: "t-1" }],
  ]);
  for (const [, args] of calls) {
    for (const key of [
      "namespace",
      "host_instance",
      "access_ref",
      "privacy_classes",
      "retention",
    ]) {
      assert.equal(Object.hasOwn(args, key), false, key);
    }
  }
});

test("persona management wrappers send only their fixed keys", async () => {
  const { api, calls } = spy();
  await api.profileList("s-1");
  await api.profileActivate("s-1", "example.persona", 3);
  await api.profileReadBody("s-1", "record-1", 2);
  await api.profileImportList("s-1");
  await api.profileImportPreview("s-1", "alpha.json");
  await api.profilePublishReviewed("s-1", "import-1", null, true);
  await api.preferenceList("s-1");
  await api.preferenceList("s-1", "c-1");
  await api.preferenceRevoke("s-1", "c-1", "length", "设置里撤销");
  assert.deepEqual(calls, [
    ["profile_list", { sessionId: "s-1" }],
    ["profile_activate", { sessionId: "s-1", profileId: "example.persona", recordVersion: 3 }],
    ["profile_read_body", { sessionId: "s-1", recordId: "record-1", recordVersion: 2 }],
    ["profile_import_list", { sessionId: "s-1" }],
    ["profile_import_preview", { sessionId: "s-1", name: "alpha.json" }],
    [
      "profile_publish_reviewed",
      { sessionId: "s-1", importId: "import-1", profileId: null, confirm: true },
    ],
    ["preference_list", { sessionId: "s-1" }],
    ["preference_list", { sessionId: "s-1", conversationId: "c-1" }],
    [
      "preference_revoke",
      {
        sessionId: "s-1",
        conversationId: "c-1",
        dimension: "length",
        reason: "设置里撤销",
      },
    ],
  ]);
  for (const [, args] of calls) {
    for (const key of [
      "namespace",
      "host_instance",
      "access_ref",
      "authorization_ref",
      "privacy_classes",
      "retention",
      "review_event_id",
      "confirmation",
    ]) {
      assert.equal(Object.hasOwn(args, key), false, key);
    }
  }
});
