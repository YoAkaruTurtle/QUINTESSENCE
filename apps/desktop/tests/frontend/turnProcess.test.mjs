import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";

const build = process.env.QUINTESSENCE_FRONTEND_BUILD;
if (!build) {
  throw new Error("QUINTESSENCE_FRONTEND_BUILD is not set");
}
const require = createRequire(import.meta.url);
const processModule = require(path.join(build, "turnProcess.js"));

const {
  classifyProcess,
  commentaryText,
  stepDurationMs,
  toolArguments,
} = processModule;

function turn(state = "completed", failureReason = null) {
  return { turn_id: "t1", turn_no: 1, state, failure_reason: failureReason };
}

function step(no, extra = {}) {
  return {
    step_id: `s${no}`,
    step_no: no,
    tool_name: "memory.search",
    status: "succeeded",
    started_at: "2026-10-29T18:25:30.885794Z",
    finished_at: "2026-10-29T18:25:31.193392Z",
    error_label: null,
    arguments: { query: "实验室代号" },
    result: { available: true, excerpt: '{"ok":true}' },
    ...extra,
  };
}

test("a completed turn with both reads empty is genuinely not-called", () => {
  const view = classifyProcess({
    turn: turn(),
    events: [{ type: "turn_completed" }],
    eventsState: "ready",
    steps: [],
    stepsState: "ready",
    processEnabled: true,
    terminal: true,
  });
  assert.equal(view.eventsGap, null);
  assert.equal(view.stepsGap, null);
  assert.equal(view.gap, "not-called");
  assert.equal(view.toolCount, 0);
});

test("a format repair with no steps is only format-blocked, never a tool name", () => {
  const view = classifyProcess({
    turn: turn("failed", "format:format_exhausted"),
    events: [
      { type: "repair_attempt", reason: "format", stage: "content" },
      { type: "turn_failed", reason: "format:format_exhausted" },
    ],
    eventsState: "ready",
    steps: [],
    stepsState: "ready",
    processEnabled: true,
    terminal: true,
  });
  assert.equal(view.gap, "format-blocked");
  assert.equal(view.toolCount, 0);
  assert.equal(view.toolRejected, 0);
});

test("a persisted parse rejection is a rejected gap, not format-blocked", () => {
  const view = classifyProcess({
    turn: turn("failed", "format:format_exhausted"),
    events: [
      { type: "tool_rejected", reason: "parse", stage: "content" },
      { type: "repair_attempt", reason: "format", stage: "content" },
      { type: "turn_failed", reason: "format:format_exhausted" },
    ],
    eventsState: "ready",
    steps: [],
    stepsState: "ready",
    processEnabled: true,
    terminal: true,
  });
  assert.equal(view.gap, "rejected");
  assert.equal(view.toolRejected, 1);
});

test("a reply-format rejection is format-blocked, not a tool rejection", () => {
  const view = classifyProcess({
    turn: turn("failed", "format:format_exhausted"),
    events: [
      { type: "reply_format_rejected", reason: "format", stage: "content" },
      { type: "repair_attempt", reason: "format", stage: "content" },
      { type: "turn_failed", reason: "format:format_exhausted" },
    ],
    eventsState: "ready",
    steps: [],
    stepsState: "ready",
    processEnabled: true,
    terminal: true,
  });
  assert.equal(view.gap, "format-blocked");
  assert.equal(view.toolRejected, 0);
});

test("a rejected tool step is visible, not an empty state", () => {
  const view = classifyProcess({
    turn: turn(),
    events: [
      { type: "tool_planned", step_no: 1, tool: "memory.search" },
      { type: "tool_rejected", step_no: 1, tool: "memory.search", reason: "unknown_tool" },
      { type: "turn_completed" },
    ],
    eventsState: "ready",
    steps: [
      step(1, {
        status: "failed",
        error_label: "refused:tool_unknown",
        started_at: null,
        finished_at: "2026-10-29T18:25:31.193392Z",
      }),
    ],
    stepsState: "ready",
    processEnabled: true,
    terminal: true,
  });
  assert.equal(view.gap, null);
  assert.equal(view.toolCount, 1);
  assert.equal(view.toolRejected, 1);
});

test("a planned tool with no step row is inconsistent, not not-called", () => {
  const view = classifyProcess({
    turn: turn(),
    events: [{ type: "tool_planned", step_no: 1, tool: "memory.search" }],
    eventsState: "ready",
    steps: [],
    stepsState: "ready",
    processEnabled: true,
    terminal: true,
  });
  assert.equal(view.gap, "inconsistent");
  assert.equal(view.toolPlanned, 1);
  assert.equal(view.toolCount, 0);
});

test("a format repair with an executed step is not an empty state", () => {
  const view = classifyProcess({
    turn: turn("failed", "format:format_exhausted"),
    events: [
      { type: "repair_attempt", reason: "format", stage: "content" },
      { type: "tool_planned", step_no: 1, tool: "cognition.partition" },
      { type: "turn_failed", reason: "format:format_exhausted" },
    ],
    eventsState: "ready",
    steps: [step(1, { tool_name: "cognition.partition" })],
    stepsState: "ready",
    processEnabled: true,
    terminal: true,
  });
  assert.equal(view.gap, null);
  assert.equal(view.toolCount, 1);
});

test("executed steps are authoritative once read", () => {
  const view = classifyProcess({
    turn: turn(),
    events: [{ type: "tool_planned", step_no: 1, tool: "memory.search" }],
    eventsState: "ready",
    steps: [step(1), step(2)],
    stepsState: "ready",
    processEnabled: true,
    terminal: true,
  });
  assert.equal(view.toolCount, 2);
  assert.equal(view.gap, null);
});

test("event failure keeps steps visible and never claims not-called", () => {
  const view = classifyProcess({
    turn: turn(),
    events: [],
    eventsState: "error",
    steps: [step(1), step(2)],
    stepsState: "ready",
    processEnabled: true,
    terminal: true,
  });
  assert.equal(view.eventsGap, "error");
  assert.equal(view.stepsGap, null);
  assert.equal(view.gap, null);
  assert.equal(view.toolCount, 2);
});

test("step failure keeps the timeline and never claims not-called", () => {
  const view = classifyProcess({
    turn: turn(),
    events: [{ type: "turn_completed" }],
    eventsState: "ready",
    steps: [],
    stepsState: "error",
    processEnabled: true,
    terminal: true,
  });
  assert.equal(view.eventsGap, null);
  assert.equal(view.stepsGap, "error");
  assert.equal(view.gap, null);
});

test("busy and unavailable are distinct from an empty result", () => {
  const busy = classifyProcess({
    turn: turn("running"),
    events: [],
    eventsState: "busy",
    steps: [],
    stepsState: "busy",
    processEnabled: true,
    terminal: true,
  });
  assert.equal(busy.eventsGap, "busy");
  assert.equal(busy.stepsGap, "busy");
  assert.equal(busy.gap, null);

  const unavailable = classifyProcess({
    turn: turn(),
    events: [],
    eventsState: "ready",
    steps: [],
    stepsState: "ready",
    processEnabled: false,
    terminal: true,
  });
  assert.equal(unavailable.eventsGap, "unavailable");
  assert.equal(unavailable.stepsGap, "unavailable");
  assert.equal(unavailable.gap, null);
});

test("while a turn is running the steps read is not reported as loading", () => {
  const view = classifyProcess({
    turn: turn("running"),
    events: [],
    eventsState: "ready",
    steps: [],
    stepsState: "idle",
    processEnabled: true,
    terminal: false,
  });
  assert.equal(view.eventsGap, null);
  assert.equal(view.stepsGap, null);
  assert.equal(view.gap, null);
});

test("an unread terminal process still reports loading for both reads", () => {
  const view = classifyProcess({
    turn: turn(),
    events: [],
    eventsState: "idle",
    steps: [],
    stepsState: "idle",
    processEnabled: true,
    terminal: true,
  });
  assert.equal(view.eventsGap, "loading");
  assert.equal(view.stepsGap, "loading");
  assert.equal(view.gap, null);
});

test("commentary prefers the loaded transcript, then the live event", () => {
  const event = {
    type: "message_published",
    kind: "commentary",
    message_id: "m1",
    text: "live",
  };
  assert.equal(
    commentaryText(event, [{ message_id: "m1", text: "transcript" }]),
    "transcript",
  );
  assert.equal(commentaryText(event, []), "live");
  assert.equal(
    commentaryText({ ...event, text: undefined }, []),
    null,
  );
  assert.equal(
    commentaryText({ type: "message_published", kind: "final" }, []),
    null,
  );
});

test("arguments come from steps before a transient live event", () => {
  assert.deepEqual(toolArguments(step(1), undefined), { query: "实验室代号" });
  assert.deepEqual(
    toolArguments(undefined, { type: "tool_planned", arguments: { a: 1 } }),
    { a: 1 },
  );
  assert.deepEqual(toolArguments(step(1, { arguments: {} }), undefined), {});
  assert.equal(toolArguments(undefined, undefined), null);
});

test("step duration is derived only from real timestamps", () => {
  assert.equal(stepDurationMs(step(1)), 308);
  assert.equal(stepDurationMs(step(1, { finished_at: null })), null);
  assert.equal(
    stepDurationMs(
      step(1, { started_at: "not-a-time", finished_at: "2026-10-29T18:25:31Z" }),
    ),
    null,
  );
  assert.equal(
    stepDurationMs(
      step(1, {
        started_at: "2026-10-29T18:25:31Z",
        finished_at: "2026-10-29T18:25:30Z",
      }),
    ),
    null,
  );
});
