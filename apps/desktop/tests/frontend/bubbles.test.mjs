import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";

const build = process.env.QUINTESSENCE_FRONTEND_BUILD;
if (!build) {
  throw new Error("QUINTESSENCE_FRONTEND_BUILD is not set");
}
const require = createRequire(import.meta.url);
const bubbles = require(path.join(build, "bubbles.js"));

const {
  BUBBLE_STAGGER_MS,
  BubbleRevealTracker,
  MAX_REPLY_BUBBLES,
  bubbleDelayMs,
  bubbleRevealKey,
  replyBubbles,
  shouldRevealTurn,
} = bubbles;

test("explicit messages keep their order and are never split again", () => {
  assert.deepEqual(
    replyBubbles({ text: "一。二。", messages: ["第一段。", "第二段。"] }),
    ["第一段。", "第二段。"],
  );
  assert.deepEqual(
    replyBubbles({ text: "单条。", messages: ["唯一一条。"] }),
    ["唯一一条。"],
  );
});

test("a legacy single reply with punctuation or newline stays one bubble", () => {
  assert.deepEqual(replyBubbles({ text: "第一句。第二句。\n\n第三句。" }), [
    "第一句。第二句。\n\n第三句。",
  ]);
  assert.deepEqual(replyBubbles({ text: "a\nb\nc" }), ["a\nb\nc"]);
});

test("invalid or absent explicit messages fall back to the legacy text", () => {
  assert.deepEqual(replyBubbles({ text: "旧回答。", messages: [] }), ["旧回答。"]);
  assert.deepEqual(
    replyBubbles({ text: "旧回答。", messages: ["a", "b", "c", "d", "e"] }),
    ["旧回答。"],
  );
  assert.deepEqual(
    replyBubbles({ text: "旧回答。", messages: ["ok", "   "] }),
    ["旧回答。"],
  );
  assert.deepEqual(replyBubbles({ text: null, messages: undefined }), []);
  assert.deepEqual(replyBubbles({ text: "已删除。", redacted: true }), []);
});

test("the reveal stagger is short, ordered and bounded", () => {
  assert.equal(bubbleDelayMs(0), 0);
  assert.equal(bubbleDelayMs(1), BUBBLE_STAGGER_MS);
  assert.equal(bubbleDelayMs(3), BUBBLE_STAGGER_MS * 3);
  assert.equal(bubbleDelayMs(99), BUBBLE_STAGGER_MS * (MAX_REPLY_BUBBLES - 1));
});

test("each bubble is claimed once and a switch resets the tracker", () => {
  const tracker = new BubbleRevealTracker();
  const key = bubbleRevealKey("m-final", 0);
  assert.equal(tracker.claim(key), true);
  assert.equal(tracker.claim(key), false);
  assert.equal(tracker.claim(bubbleRevealKey("m-final", 1)), true);
  assert.equal(tracker.size, 2);
  tracker.reset();
  assert.equal(tracker.claim(key), true);
});

test("only the newest turn animates", () => {
  assert.equal(shouldRevealTurn("turn-2", "turn-2"), true);
  assert.equal(shouldRevealTurn("turn-1", "turn-2"), false);
  assert.equal(shouldRevealTurn("turn-1", null), false);
});
