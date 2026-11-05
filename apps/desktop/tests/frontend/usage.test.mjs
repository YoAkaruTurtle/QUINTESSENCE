import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";
const require = createRequire(import.meta.url);
const usage = require(path.join(process.env.QUINTESSENCE_FRONTEND_BUILD, "usage.js"));

const tokens = (total, complete) => ({ prompt: complete ? 1 : null, completion: complete ? 2 : null, total, complete });

test("an incomplete token scope stays unknown and is never zero", () => {
  assert.equal(usage.exactTokens(tokens(null, false)), null);
  assert.equal(usage.exactTokens(tokens(160, false)), null);
  assert.equal(usage.exactTokens(tokens(160, true)), 160);
  assert.equal(usage.exactTokens(null), null);
});

test("coverage is a count, not a percentage", () => {
  assert.equal(usage.coverageText(3, 5), "3/5");
  assert.equal(usage.coverageText(0, 0), "0/0");
  assert.ok(!usage.coverageText(3, 5).includes("%"));
});

test("context bytes are shown as used/limit and never as a window percentage", () => {
  assert.equal(usage.bytesLabel({ used_bytes: 900, limit_bytes: 24576 }), "900 / 24576 B");
  assert.equal(usage.bytesLabel(null), null);
  assert.equal(usage.bytesLabel({ used_bytes: 1, limit_bytes: 0 }), null);
  assert.ok(!String(usage.bytesLabel({ used_bytes: 1, limit_bytes: 2 })).includes("%"));
});

test("speed comes from one measured attempt, never an average", () => {
  assert.equal(usage.exactSpeed(null), null);
  assert.equal(usage.exactSpeed({ usage_known: false, tokens_per_second: 99 }), null);
  assert.equal(usage.exactSpeed({ usage_known: true, tokens_per_second: null }), null);
  assert.equal(usage.exactSpeed({ usage_known: true, tokens_per_second: 12.5 }), 12.5);
  assert.equal(usage.latestSpeed(null), null);
  assert.equal(usage.latestSpeed({ latest_request: null }), null);
  assert.equal(usage.latestSpeed({ latest_request: { usage_known: true, tokens_per_second: 7 } }), 7);
});

test("repair needs the persisted marker; unmarked stays unknown", () => {
  assert.equal(usage.repairedAttempt({ repair: true }), true);
  assert.equal(usage.repairedAttempt({ repair: false }), false);
  assert.equal(usage.repairedAttempt({ repair: null }), false);
  assert.equal(usage.unmarkedAttempt({ repair: null }), true);
  assert.equal(usage.unmarkedAttempt({ repair: false }), false);
});

test("the bridge payload keeps unknown nulls and counts repairs only from markers", () => {
  const summary = usage.usageSummary({
    conversation_id: "c1",
    turns_total: 3,
    turns_with_attempts: 2,
    attempts_total: 5,
    attempts_known: 4,
    attempts_unknown: 1,
    attempts_open: 0,
    attempts_unreadable: 0,
    counts: { repairs: 1 },
    tokens: tokens(null, false),
    latest_request: null,
    latest_context: { context_id: "x", purpose: "content_initial", stage: "content", bytes: { used_bytes: 10, limit_bytes: 20 } },
    turn: null,
  });
  assert.equal(summary.repairs, 1);
  assert.equal(summary.tokens.total, null);
  assert.equal(summary.latest_request, null);
  assert.equal(usage.usageSummary(null), null);
});
