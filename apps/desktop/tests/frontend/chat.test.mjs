import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";

const build = process.env.QUINTESSENCE_FRONTEND_BUILD;
if (!build) {
  throw new Error("QUINTESSENCE_FRONTEND_BUILD is not set");
}
const require = createRequire(import.meta.url);
const chat = require(path.join(build, "chat.js"));

const {
  INITIAL_CHAT_STATE,
  canReadTurnResult,
  canSwitchConversation,
  chatReduce,
  newestTurnId,
  newRequestId,
  pendingUserMessageVisible,
  requestIdFor,
  reuseRequestId,
} = chat;

function accepted(state, turnId = "turn-1") {
  let next = chatReduce(state, {
    type: "send-start",
    requestId: "req-1",
    text: "同一个问题",
  });
  next = chatReduce(next, {
    type: "send-accepted",
    turnId,
    state: "accepted",
  });
  return next;
}

test("a terminal turn clears the pending turn and merges the final", () => {
  let state = accepted(INITIAL_CHAT_STATE);
  assert.equal(state.pendingTurnId, "turn-1");
  state = chatReduce(state, {
    type: "turn-result",
    result: {
      turn_id: "turn-1",
      conversation_id: "conv-1",
      turn_no: 1,
      state: "completed",
      terminal: true,
      final: {
        message_id: "m-final",
        turn_id: "turn-1",
        seq: 2,
        kind: "final",
        text: "回答",
        redacted: false,
        created_at: "2026-10-28T00:00:00Z",
      },
      wait_question: null,
      failure_reason: null,
    },
  });
  assert.equal(state.pendingTurnId, null);
  assert.equal(state.messages.length, 1);
  assert.equal(state.messages[0].text, "回答");
  assert.equal(state.turns[0].state, "completed");
});

test("a BUSY read is not a terminal state", () => {
  let state = accepted(INITIAL_CHAT_STATE, "turn-2");
  state = chatReduce(state, { type: "turn-busy" });
  assert.equal(state.pendingTurnId, "turn-2");
  assert.equal(state.busy, true);
  state = chatReduce(state, {
    type: "turn-result",
    result: {
      turn_id: "turn-2",
      conversation_id: "conv-1",
      turn_no: 1,
      state: "running",
      terminal: false,
      final: null,
      wait_question: null,
      failure_reason: null,
    },
  });
  assert.equal(state.pendingTurnId, "turn-2");
});

test("a failed send keeps the draft and the same request id", () => {
  let state = chatReduce(INITIAL_CHAT_STATE, {
    type: "send-start",
    requestId: "req-keep",
    text: "忙时的问题",
  });
  state = chatReduce(state, {
    type: "send-failed",
    code: "BUSY",
    message: "另一个会话正在处理",
  });
  assert.equal(state.sending, false);
  assert.equal(state.pendingRequestId, "req-keep");
  assert.equal(state.pendingText, "忙时的问题");
  assert.equal(reuseRequestId(state, "忙时的问题"), true);
  assert.equal(reuseRequestId(state, "改过的问题"), false);
  assert.equal(requestIdFor(state, "忙时的问题"), "req-keep");
  assert.notEqual(requestIdFor(state, "改过的问题"), "req-keep");
});

test("editing the draft clears the request id only when idle", () => {
  let state = chatReduce(INITIAL_CHAT_STATE, {
    type: "send-start",
    requestId: "req-edit",
    text: "原文",
  });
  state = chatReduce(state, {
    type: "send-failed",
    code: "BUSY",
    message: "busy",
  });
  const edited = chatReduce(state, { type: "draft-changed" });
  assert.equal(edited.pendingRequestId, null);
  assert.equal(edited.pendingText, null);
  // While a send is in flight the draft action must not change the key.
  const inFlight = chatReduce(INITIAL_CHAT_STATE, {
    type: "send-start",
    requestId: "req-flight",
    text: "发送中",
  });
  const untouched = chatReduce(inFlight, { type: "draft-changed" });
  assert.equal(untouched.pendingRequestId, "req-flight");
});

test("conversation switching and result reads wait for an idle turn", () => {
  let state = accepted(INITIAL_CHAT_STATE);
  assert.equal(canSwitchConversation(state), false);
  state = chatReduce(state, { type: "turn-result", result: {
    turn_id: "turn-1", conversation_id: "conv-1", turn_no: 1,
    state: "completed", terminal: true, final: null,
    wait_question: null, failure_reason: null,
  }});
  assert.equal(canSwitchConversation(state), true);
  state = chatReduce(state, { type: "busy", busy: true });
  assert.equal(canReadTurnResult(state), false);
  state = chatReduce(state, { type: "busy", busy: false });
  assert.equal(canReadTurnResult(state), false);
  state = chatReduce(state, {
    type: "send-accepted", turnId: "turn-3", state: "accepted",
  });
  assert.equal(canReadTurnResult(state), true);
});

test("a redacted final keeps its identity but has no text", () => {
  let state = accepted(INITIAL_CHAT_STATE);
  state = chatReduce(state, { type: "turn-result", result: {
    turn_id: "turn-1", conversation_id: "conv-1", turn_no: 1,
    state: "completed", terminal: true,
    final: {
      message_id: "m-redacted", turn_id: "turn-1", seq: 2,
      kind: "final", text: null, redacted: true,
      created_at: "2026-10-28T00:00:00Z",
    },
    wait_question: null, failure_reason: null,
  }});
  assert.equal(state.messages[0].redacted, true);
  assert.equal(state.messages[0].text, null);
});

test("request ids are fresh and the conversation list is replaceable", () => {
  assert.notEqual(newRequestId(), newRequestId());
  let state = chatReduce(INITIAL_CHAT_STATE, {
    type: "conversations",
    conversations: [
      {
        conversation_id: "c-1",
        mode: "chat",
        title: null,
        created_at: "2026-10-28T00:00:00Z",
        updated_at: "2026-10-28T00:00:00Z",
        turn_count: 2,
        last_turn_state: "completed",
      },
    ],
  });
  assert.equal(state.status, "ready");
  assert.equal(state.conversations.length, 1);
  state = chatReduce(state, { type: "new-conversation", conversationId: "c-2" });
  assert.equal(state.activeId, "c-2");
  assert.deepEqual(state.messages, []);
});

test("process events merge by event_id and order by seq", () => {
  let state = accepted(INITIAL_CHAT_STATE, "turn-9");
  const events = [
    { seq: 2, event_id: "e2", type: "tools", at: "t2" },
    { seq: 1, event_id: "e1", type: "turn_accepted", at: "t1" },
  ];
  state = chatReduce(state, {
    type: "events",
    turnId: "turn-9",
    events,
    nextSeq: 2,
    terminal: false,
  });
  assert.deepEqual(
    state.events.map((event) => event.event_id),
    ["e1", "e2"],
  );
  assert.equal(state.eventCursor, 2);
  state = chatReduce(state, {
    type: "events",
    turnId: "turn-9",
    events: [{ seq: 2, event_id: "e2", type: "tools", at: "t2" }],
    nextSeq: 2,
    terminal: true,
  });
  assert.equal(state.events.length, 2);
  assert.equal(state.eventsTurnId, "turn-9");
});

test("a late final cannot jump ahead of a newer turn", () => {
  const messages = [
    { message_id: "m3", turn_id: "turn-2", seq: 1, kind: "user", text: "二", redacted: false, created_at: "t" },
    { message_id: "m1", turn_id: "turn-1", seq: 2, kind: "final", text: "一", redacted: false, created_at: "t" },
  ];
  const turns = [
    { turn_id: "turn-1", turn_no: 1, state: "completed", failure_reason: null },
    { turn_id: "turn-2", turn_no: 2, state: "running", failure_reason: null },
  ];
  assert.deepEqual(
    chat.sortMessages(messages, turns).map((item) => item.message_id),
    ["m1", "m3"],
  );
});

test("cancel is only offered for a live pending turn", () => {
  let state = accepted(INITIAL_CHAT_STATE, "turn-3");
  assert.equal(chat.canCancel(state), true);
  state = chatReduce(state, { type: "cancel-requested" });
  assert.equal(chat.canCancel(state), false);
  state = chatReduce(state, { type: "cancel-refused", code: "BUSY", message: "busy" });
  assert.equal(state.cancelRequested, false);
  assert.equal(chat.canCancel(state), true);
});

test("postprocess status keeps saved and memory as separate dimensions", () => {
  const state = chatReduce(INITIAL_CHAT_STATE, {
    type: "postprocess",
    turns: [
      {
        turn_id: "turn-1",
        turn_state: "completed",
        completed_at: "2026-10-29T00:00:00Z",
        transcript_saved: true,
        memory: {
          state: "outcome_unknown",
          detail: "capture_result_unknown",
          pending: 1,
          delivered: 0,
          refs: [],
        },
      },
    ],
  });
  assert.equal(state.postprocess["turn-1"].transcript_saved, true);
  assert.equal(chat.memoryStateLabel("outcome_unknown"), "记忆结果不明，待核对");
  assert.equal(chat.memoryStateLabel("delivered"), "记忆已交付");
  assert.notEqual(chat.memoryStateLabel("delivered"), "对话已保存");
});

test("only pending memory states request a bounded status refresh", () => {
  const turn = (state) => ({
    turn_id: "turn-1",
    turn_state: "completed",
    completed_at: "2026-10-29T00:00:00Z",
    transcript_saved: true,
    memory: { state, detail: null, pending: 0, delivered: 0, refs: [] },
  });
  assert.equal(chat.hasPendingMemory([]), false);
  assert.equal(chat.hasPendingMemory([turn("pending")]), true);
  assert.equal(chat.hasPendingMemory([turn("outcome_unknown")]), true);
  assert.equal(chat.hasPendingMemory([turn("delivered")]), false);
  assert.equal(chat.hasPendingMemory([turn("needs_review")]), false);
  assert.equal(chat.hasPendingMemory([turn("blocked")]), false);
  assert.equal(chat.hasPendingMemory([turn("obsolete")]), false);
  assert.equal(chat.hasPendingMemory([turn("not_eligible")]), false);
  assert.equal(chat.hasPendingMemory([turn("not_configured")]), false);
});

test("the local pending user message never duplicates the formal one", () => {
  let state = chatReduce(INITIAL_CHAT_STATE, {
    type: "send-start",
    requestId: "req-pending",
    text: "同一个问题",
  });
  assert.equal(pendingUserMessageVisible(state), true);
  state = chatReduce(state, {
    type: "send-accepted",
    turnId: "turn-1",
    state: "accepted",
  });
  assert.equal(pendingUserMessageVisible(state), true);
  // The host reload includes the official user message for that same turn.
  state = chatReduce(state, {
    type: "conversation",
    conversationId: "conv-1",
    turns: [
      {
        turn_id: "turn-1",
        turn_no: 1,
        state: "running",
        failure_reason: null,
      },
    ],
    messages: [
      {
        message_id: "m-user",
        turn_id: "turn-1",
        seq: 1,
        kind: "user",
        text: "同一个问题",
        redacted: false,
        created_at: "2026-10-29T00:00:00Z",
      },
    ],
    nextBefore: null,
  });
  assert.equal(state.pendingText, null);
  assert.equal(pendingUserMessageVisible(state), false);
  assert.equal(
    state.messages.filter(
      (message) => message.kind === "user" && message.text === "同一个问题",
    ).length,
    1,
  );
});

test("a split final keeps the host order and exposes the newest turn", () => {
  let state = accepted(INITIAL_CHAT_STATE);
  state = chatReduce(state, {
    type: "turn-result",
    result: {
      turn_id: "turn-1",
      conversation_id: "conv-1",
      turn_no: 1,
      state: "completed",
      terminal: true,
      final: {
        message_id: "m-final",
        turn_id: "turn-1",
        seq: 2,
        kind: "final",
        text: "第一段。\n\n第二段。",
        messages: ["第一段。", "第二段。"],
        redacted: false,
        created_at: "2026-10-29T00:00:00Z",
      },
      wait_question: null,
      failure_reason: null,
    },
  });
  assert.equal(state.messages.length, 1);
  assert.deepEqual(state.messages[0].messages, ["第一段。", "第二段。"]);
  assert.equal(state.messages[0].text, "第一段。\n\n第二段。");
  assert.equal(newestTurnId(state), "turn-1");
  assert.equal(newestTurnId(INITIAL_CHAT_STATE), null);
});

// ---------------------------------------------------------------------------
// Card C: stable order for the local projection and reload consistency.
// ---------------------------------------------------------------------------

function msg(id, turnId, seq, kind, text) {
  return {
    message_id: id,
    turn_id: turnId,
    seq,
    kind,
    text,
    redacted: false,
    created_at: "2026-10-30T00:00:00Z",
  };
}

function turn(id, no, state = "completed") {
  return { turn_id: id, turn_no: no, state, failure_reason: null };
}

function page(conversationId, turns, messages) {
  return {
    type: "conversation",
    conversationId,
    turns,
    messages,
    nextBefore: null,
  };
}

function turnResult(turnId, turnNo, final, waitQuestion = null) {
  return {
    type: "turn-result",
    result: {
      turn_id: turnId,
      conversation_id: "conv-1",
      turn_no: turnNo,
      state: "completed",
      terminal: true,
      final,
      wait_question: waitQuestion,
      failure_reason: null,
    },
  };
}

test("orderedTurnIds keeps a turn without loaded messages at its turn_no", () => {
  // The latest page is capped: turn 1/2 exist but their messages are not in
  // this page.  The old messages-first union rendered their groups last.
  let state = chatReduce(
    INITIAL_CHAT_STATE,
    page(
      "conv-1",
      [turn("turn-1", 1), turn("turn-2", 2), turn("turn-3", 3)],
      [msg("m3u", "turn-3", 1, "user", "三"), msg("m3f", "turn-3", 2, "final", "答")],
    ),
  );
  assert.deepEqual(chat.orderedTurnIds(state), ["turn-1", "turn-2", "turn-3"]);
  // An unknown historical turn is still rendered, but last.
  state = chatReduce(
    state,
    page(
      "conv-1",
      [turn("turn-1", 1)],
      [msg("mxu", "turn-x", 1, "user", "旧页")],
    ),
  );
  assert.deepEqual(chat.orderedTurnIds(state), ["turn-1", "turn-x"]);
});

test("an accepted turn_no places the projection in its own last turn", () => {
  let state = chatReduce(
    INITIAL_CHAT_STATE,
    page(
      "conv-1",
      [turn("turn-1", 1), turn("turn-2", 2)],
      [
        msg("m1u", "turn-1", 1, "user", "一"),
        msg("m1f", "turn-1", 2, "final", "答一"),
        msg("m2u", "turn-2", 1, "user", "二"),
        msg("m2f", "turn-2", 2, "final", "答二"),
      ],
    ),
  );
  state = chatReduce(state, {
    type: "send-start",
    requestId: "req-c",
    text: "三",
  });
  state = chatReduce(state, {
    type: "send-accepted",
    turnId: "turn-3",
    turnNo: 3,
    state: "accepted",
  });
  assert.deepEqual(state.turns.map((item) => item.turn_id), [
    "turn-1",
    "turn-2",
    "turn-3",
  ]);
  assert.equal(state.pendingUserTurnId, "turn-3");
  assert.equal(pendingUserMessageVisible(state), true);
  assert.deepEqual(chat.orderedTurnIds(state), ["turn-1", "turn-2", "turn-3"]);
});

test("the projection keeps its turn until the official user message loads", () => {
  let state = chatReduce(
    INITIAL_CHAT_STATE,
    page(
      "conv-1",
      [turn("turn-1", 1)],
      [msg("m1u", "turn-1", 1, "user", "旧"), msg("m1f", "turn-1", 2, "final", "旧答")],
    ),
  );
  state = chatReduce(state, {
    type: "send-start",
    requestId: "req-c",
    text: "新问题",
  });
  state = chatReduce(state, {
    type: "send-accepted",
    turnId: "turn-2",
    turnNo: 2,
    state: "accepted",
  });
  state = chatReduce(
    state,
    turnResult("turn-2", 2, msg("m2f", "turn-2", 2, "final", "新答")),
  );
  assert.equal(state.pendingTurnId, null);
  assert.equal(state.pendingUserTurnId, "turn-2");
  assert.equal(state.pendingText, "新问题");
  assert.equal(pendingUserMessageVisible(state), true);
  assert.deepEqual(chat.orderedTurnIds(state), ["turn-1", "turn-2"]);
  state = chatReduce(
    state,
    page(
      "conv-1",
      [turn("turn-1", 1), turn("turn-2", 2)],
      [
        msg("m1u", "turn-1", 1, "user", "旧"),
        msg("m1f", "turn-1", 2, "final", "旧答"),
        msg("m2u", "turn-2", 1, "user", "新问题"),
        msg("m2f", "turn-2", 2, "final", "新答"),
      ],
    ),
  );
  assert.equal(state.pendingText, null);
  assert.equal(state.pendingUserTurnId, null);
  assert.equal(pendingUserMessageVisible(state), false);
  assert.deepEqual(
    state.messages.map((message) => message.message_id),
    ["m1u", "m1f", "m2u", "m2f"],
  );
  assert.equal(
    state.messages.filter((message) => message.text === "新问题").length,
    1,
  );
});

test("a reload during the send cannot retire the in-flight projection", () => {
  let state = chatReduce(
    INITIAL_CHAT_STATE,
    page("conv-1", [turn("turn-1", 1)], [msg("m1u", "turn-1", 1, "user", "同一句")]),
  );
  state = chatReduce(state, {
    type: "send-start",
    requestId: "req-c",
    text: "同一句",
  });
  state = chatReduce(
    state,
    page("conv-1", [turn("turn-1", 1)], [msg("m1u", "turn-1", 1, "user", "同一句")]),
  );
  assert.equal(state.sending, true);
  assert.equal(state.pendingText, "同一句");
  assert.equal(pendingUserMessageVisible(state), true);
});

test("a mid-turn reload confirms the projection without dropping the poll", () => {
  let state = chatReduce(
    INITIAL_CHAT_STATE,
    page("conv-1", [turn("turn-1", 1)], [msg("m1u", "turn-1", 1, "user", "旧")]),
  );
  state = chatReduce(state, {
    type: "send-start",
    requestId: "req-c",
    text: "新问题",
  });
  state = chatReduce(state, {
    type: "send-accepted",
    turnId: "turn-2",
    turnNo: 2,
    state: "accepted",
  });
  state = chatReduce(
    state,
    page(
      "conv-1",
      [turn("turn-1", 1), turn("turn-2", 2, "running")],
      [msg("m1u", "turn-1", 1, "user", "旧"), msg("m2u", "turn-2", 1, "user", "新问题")],
    ),
  );
  assert.equal(state.pendingTurnId, "turn-2");
  assert.equal(state.pendingUserTurnId, null);
  assert.equal(state.pendingText, null);
  assert.equal(chat.canCancel(state), true);
  assert.equal(chat.canReadTurnResult(state), true);
});

test("wait_question is merged by message_id exactly like a final", () => {
  let state = chatReduce(INITIAL_CHAT_STATE, {
    type: "send-start",
    requestId: "req-c",
    text: "问题",
  });
  state = chatReduce(state, {
    type: "send-accepted",
    turnId: "turn-1",
    turnNo: 1,
    state: "accepted",
  });
  state = chatReduce(
    state,
    turnResult("turn-1", 1, null, msg("m-wait", "turn-1", 2, "wait_question", "需要确认")),
  );
  assert.equal(state.messages.length, 1);
  assert.equal(state.messages[0].message_id, "m-wait");
  // A second authoritative result cannot duplicate it.
  state = chatReduce(
    state,
    turnResult("turn-1", 1, null, msg("m-wait", "turn-1", 2, "wait_question", "需要确认")),
  );
  assert.equal(state.messages.length, 1);
});

test("a failed send hides the projection but keeps the retry key", () => {
  let state = chatReduce(
    INITIAL_CHAT_STATE,
    page("conv-1", [turn("turn-1", 1)], [msg("m1u", "turn-1", 1, "user", "旧")]),
  );
  state = chatReduce(state, {
    type: "send-start",
    requestId: "req-retry",
    text: "重试问题",
  });
  assert.equal(pendingUserMessageVisible(state), true);
  state = chatReduce(state, {
    type: "send-failed",
    code: "BUSY",
    message: "另一个会话正在处理",
  });
  assert.equal(state.sending, false);
  assert.equal(pendingUserMessageVisible(state), false);
  assert.equal(state.pendingText, "重试问题");
  assert.equal(reuseRequestId(state, "重试问题", "conv-1"), true);
  assert.equal(reuseRequestId(state, "重试问题", "conv-2"), false);
  assert.equal(requestIdFor(state, "重试问题", "conv-1"), "req-retry");
  assert.notEqual(requestIdFor(state, "重试问题", "conv-2"), "req-retry");
});

test("a projection is scoped to its conversation and survives a switch", () => {
  let state = chatReduce(
    INITIAL_CHAT_STATE,
    page(
      "conv-1",
      [turn("turn-1", 1)],
      [msg("m1u", "turn-1", 1, "user", "旧"), msg("m1f", "turn-1", 2, "final", "旧答")],
    ),
  );
  state = chatReduce(state, {
    type: "send-start",
    requestId: "req-c",
    text: "新问题",
  });
  state = chatReduce(state, {
    type: "send-accepted",
    turnId: "turn-2",
    turnNo: 2,
    state: "accepted",
  });
  state = chatReduce(
    state,
    turnResult("turn-2", 2, msg("m2f", "turn-2", 2, "final", "新答")),
  );
  assert.equal(pendingUserMessageVisible(state), true);
  state = chatReduce(state, { type: "select", conversationId: "conv-2" });
  assert.equal(state.pendingText, "新问题");
  assert.equal(pendingUserMessageVisible(state), false);
  state = chatReduce(
    state,
    page("conv-2", [turn("turn-9", 1)], [msg("m9u", "turn-9", 1, "user", "别的会话")]),
  );
  assert.equal(state.pendingText, "新问题");
  state = chatReduce(state, { type: "select", conversationId: "conv-1" });
  state = chatReduce(
    state,
    page(
      "conv-1",
      [turn("turn-1", 1), turn("turn-2", 2)],
      [
        msg("m1u", "turn-1", 1, "user", "旧"),
        msg("m1f", "turn-1", 2, "final", "旧答"),
        msg("m2u", "turn-2", 1, "user", "新问题"),
        msg("m2f", "turn-2", 2, "final", "新答"),
      ],
    ),
  );
  assert.equal(state.pendingText, null);
  assert.equal(pendingUserMessageVisible(state), false);
});

test("an older identical message cannot confirm the newest projection", () => {
  let state = chatReduce(
    INITIAL_CHAT_STATE,
    page("conv-1", [turn("turn-1", 1)], [msg("m1u", "turn-1", 1, "user", "同一句")]),
  );
  state = chatReduce(state, { type: "send-start", requestId: "req-c", text: "同一句" });
  state = chatReduce(state, {
    type: "send-accepted",
    turnId: "turn-2",
    turnNo: 2,
    state: "accepted",
  });
  // The old turn-1 copy must not hide the projection of turn-2.
  assert.equal(pendingUserMessageVisible(state), true);
  state = chatReduce(
    state,
    page(
      "conv-1",
      [turn("turn-1", 1), turn("turn-2", 2)],
      [msg("m1u", "turn-1", 1, "user", "同一句"), msg("m2u", "turn-2", 1, "user", "同一句")],
    ),
  );
  assert.equal(pendingUserMessageVisible(state), false);
  assert.equal(state.messages.filter((message) => message.kind === "user").length, 2);
});

test("the status line covers sending and an active turn", () => {
  let state = chatReduce(INITIAL_CHAT_STATE, {
    type: "send-start",
    requestId: "req-c",
    text: "问题",
  });
  assert.equal(chat.turnStatusLabel(state), "发送中…");
  state = chatReduce(state, {
    type: "send-accepted",
    turnId: "turn-1",
    turnNo: 1,
    state: "accepted",
  });
  assert.equal(chat.turnStatusLabel(state), "正在处理…");
  state = chatReduce(state, turnResult("turn-1", 1, msg("m1f", "turn-1", 2, "final", "答")));
  assert.equal(chat.turnStatusLabel(state), null);
});

// ---------------------------------------------------------------------------
// Card C2: older history pages.
// ---------------------------------------------------------------------------

function initialPage(turns, messages, nextBefore = null) {
  return {
    type: "conversation",
    conversationId: "conv-1",
    turns,
    messages,
    nextBefore,
  };
}

function olderPage(conversationId, turns, messages, nextBefore) {
  return { type: "older-page", conversationId, turns, messages, nextBefore };
}

test("an older page merges old turns and messages without changing the C order", () => {
  let state = chatReduce(
    INITIAL_CHAT_STATE,
    initialPage(
      [turn("t3", 3), turn("t4", 4), turn("t5", 5)],
      [
        msg("m3u", "t3", 1, "user", "问3"),
        msg("m3f", "t3", 2, "final", "答3"),
        msg("m4u", "t4", 1, "user", "问4"),
        msg("m4f", "t4", 2, "final", "答4"),
      ],
      { turn_no: 3, seq: 1 },
    ),
  );
  state = chatReduce(state, { type: "older-start", conversationId: "conv-1" });
  assert.equal(state.loadingOlder, true);
  state = chatReduce(
    state,
    olderPage(
      "conv-1",
      [turn("t1", 1), turn("t2", 2), turn("t3", 3, "failed")],
      [
        msg("m1u", "t1", 1, "user", "问1"),
        msg("m1f", "t1", 2, "final", "答1"),
        msg("m2u", "t2", 1, "user", "问2"),
        msg("m2f", "t2", 2, "final", "答2"),
        msg("m3u", "t3", 1, "user", "已加载的重复"),
      ],
      { turn_no: 1, seq: 1 },
    ),
  );
  assert.equal(state.loadingOlder, false);
  assert.equal(state.historyLoaded, true);
  assert.equal(state.olderExhausted, false);
  assert.deepEqual(state.nextBefore, { turn_no: 1, seq: 1 });
  assert.deepEqual(
    state.turns.map((item) => item.turn_id),
    ["t1", "t2", "t3", "t4", "t5"],
  );
  assert.deepEqual(
    state.messages.map((item) => item.message_id),
    ["m1u", "m1f", "m2u", "m2f", "m3u", "m3f", "m4u", "m4f"],
  );
  // The live copy wins over the page copy.
  assert.equal(state.messages.find((item) => item.message_id === "m3u").text, "问3");
  assert.equal(state.turns.find((item) => item.turn_id === "t3").state, "completed");
  assert.deepEqual(chat.orderedTurnIds(state), ["t1", "t2", "t3", "t4", "t5"]);
});

test("an empty older page marks the history as exhausted", () => {
  let state = chatReduce(
    INITIAL_CHAT_STATE,
    initialPage(
      [turn("t2", 2)],
      [msg("m2u", "t2", 1, "user", "问2"), msg("m2f", "t2", 2, "final", "答2")],
      { turn_no: 2, seq: 1 },
    ),
  );
  state = chatReduce(state, { type: "older-start", conversationId: "conv-1" });
  state = chatReduce(state, olderPage("conv-1", [], [], null));
  assert.equal(state.loadingOlder, false);
  assert.equal(state.historyLoaded, true);
  assert.equal(state.olderExhausted, true);
  assert.equal(state.nextBefore, null);
  assert.equal(state.messages.length, 2);
});

test("a failed older page keeps the cursor and a retry can succeed", () => {
  let state = chatReduce(
    INITIAL_CHAT_STATE,
    initialPage(
      [turn("t2", 2)],
      [msg("m2u", "t2", 1, "user", "问2")],
      { turn_no: 2, seq: 1 },
    ),
  );
  state = chatReduce(state, { type: "older-start", conversationId: "conv-1" });
  state = chatReduce(state, {
    type: "older-failed",
    conversationId: "conv-1",
    code: "BUSY",
    message: "另一轮正在处理",
  });
  assert.equal(state.loadingOlder, false);
  assert.equal(state.olderError.code, "BUSY");
  assert.deepEqual(state.nextBefore, { turn_no: 2, seq: 1 });
  state = chatReduce(state, { type: "older-start", conversationId: "conv-1" });
  assert.equal(state.olderError, null);
  state = chatReduce(
    state,
    olderPage(
      "conv-1",
      [turn("t1", 1)],
      [msg("m1u", "t1", 1, "user", "问1"), msg("m1f", "t1", 2, "final", "答1")],
      null,
    ),
  );
  assert.equal(state.olderError, null);
  assert.equal(state.olderExhausted, true);
  assert.deepEqual(
    state.messages.map((item) => item.message_id),
    ["m1u", "m1f", "m2u"],
  );
});

test("a same-conversation reload keeps loaded older pages and their cursor", () => {
  let state = chatReduce(
    INITIAL_CHAT_STATE,
    initialPage(
      [turn("t4", 4), turn("t5", 5)],
      [
        msg("m4u", "t4", 1, "user", "问4"),
        msg("m4f", "t4", 2, "final", "答4"),
        msg("m5u", "t5", 1, "user", "问5"),
        msg("m5f", "t5", 2, "final", "答5"),
      ],
      { turn_no: 4, seq: 1 },
    ),
  );
  state = chatReduce(state, { type: "older-start", conversationId: "conv-1" });
  state = chatReduce(
    state,
    olderPage(
      "conv-1",
      [turn("t1", 1), turn("t2", 2), turn("t3", 3)],
      [
        msg("m1u", "t1", 1, "user", "问1"),
        msg("m1f", "t1", 2, "final", "答1"),
        msg("m2u", "t2", 1, "user", "问2"),
        msg("m2f", "t2", 2, "final", "答2"),
      ],
      { turn_no: 1, seq: 1 },
    ),
  );
  // A terminal poll reloads the latest window; the loaded page must stay.
  state = chatReduce(
    state,
    initialPage(
      [turn("t4", 4), turn("t5", 5), turn("t6", 6)],
      [
        msg("m4u", "t4", 1, "user", "问4"),
        msg("m4f", "t4", 2, "final", "答4"),
        msg("m5u", "t5", 1, "user", "问5"),
        msg("m5f", "t5", 2, "final", "答5"),
        msg("m6u", "t6", 1, "user", "问6"),
        msg("m6f", "t6", 2, "final", "答6"),
      ],
      { turn_no: 4, seq: 1 },
    ),
  );
  assert.equal(state.historyLoaded, true);
  assert.deepEqual(state.nextBefore, { turn_no: 1, seq: 1 });
  assert.deepEqual(
    state.messages.map((item) => item.message_id),
    ["m1u", "m1f", "m2u", "m2f", "m4u", "m4f", "m5u", "m5f", "m6u", "m6f"],
  );
});

test("switching conversations resets the history cursor and error", () => {
  let state = chatReduce(
    INITIAL_CHAT_STATE,
    initialPage(
      [turn("t2", 2)],
      [msg("m2u", "t2", 1, "user", "问2")],
      { turn_no: 2, seq: 1 },
    ),
  );
  state = chatReduce(state, { type: "older-start", conversationId: "conv-1" });
  state = chatReduce(state, {
    type: "older-failed",
    conversationId: "conv-1",
    code: "BUSY",
    message: "busy",
  });
  state = chatReduce(state, { type: "select", conversationId: "conv-2" });
  assert.equal(state.historyLoaded, false);
  assert.equal(state.loadingOlder, false);
  assert.equal(state.olderError, null);
  assert.equal(state.olderExhausted, false);
  assert.equal(state.nextBefore, null);
});

test("a page for another conversation is ignored", () => {
  let state = chatReduce(
    INITIAL_CHAT_STATE,
    initialPage(
      [turn("t2", 2)],
      [msg("m2u", "t2", 1, "user", "问2")],
      { turn_no: 2, seq: 1 },
    ),
  );
  state = chatReduce(state, { type: "older-start", conversationId: "conv-1" });
  const before = state;
  state = chatReduce(
    state,
    olderPage(
      "conv-2",
      [turn("tx", 1)],
      [msg("mxu", "tx", 1, "user", "别的会话")],
      null,
    ),
  );
  assert.equal(state, before);
});

test("a loaded old page does not move the pending projection off the end", () => {
  let state = chatReduce(
    INITIAL_CHAT_STATE,
    initialPage(
      [turn("t1", 1), turn("t2", 2)],
      [
        msg("m1u", "t1", 1, "user", "问1"),
        msg("m1f", "t1", 2, "final", "答1"),
        msg("m2u", "t2", 1, "user", "问2"),
        msg("m2f", "t2", 2, "final", "答2"),
      ],
      { turn_no: 1, seq: 1 },
    ),
  );
  state = chatReduce(state, { type: "send-start", requestId: "req-c", text: "问3" });
  state = chatReduce(state, {
    type: "send-accepted",
    turnId: "t3",
    turnNo: 3,
    state: "accepted",
  });
  assert.deepEqual(chat.orderedTurnIds(state), ["t1", "t2", "t3"]);
  assert.equal(pendingUserMessageVisible(state), true);
});
