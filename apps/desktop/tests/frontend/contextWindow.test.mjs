import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";
const require = createRequire(import.meta.url);
const { modelWindow, contextMeasurement } = require(path.join(process.env.QUINTESSENCE_FRONTEND_BUILD, "contextWindow.js"));
test("capacity follows supported model metadata, not arbitrary names", () => {
  assert.equal(modelWindow("deepseek", "deepseek-flash"), 1048576);
  assert.equal(modelWindow("deepseek", "deepseek-v4-pro"), 1048576);
  assert.equal(modelWindow("other", "deepseek-flash"), null);
  assert.equal(modelWindow("deepseek", "unknown"), null);
});
test("context uses content input, not expression or cumulative billed usage", () => {
  const summary = { tokens: { total: 900000 }, latest_request: { prompt_tokens: 100 }, latest_content_request: { usage_known: true, prompt_tokens: 262144 } };
  assert.equal(contextMeasurement(summary, 1048576).ratio, .25);
  assert.equal(contextMeasurement(summary, 131072).ratio, 2);
  assert.equal(contextMeasurement(summary, null).ratio, null);
  assert.equal(contextMeasurement({ ...summary, latest_content_request: { usage_known: false, prompt_tokens: 262144 } }, 1048576).used, null);
  assert.equal(contextMeasurement(null, 1048576).used, null);
});
