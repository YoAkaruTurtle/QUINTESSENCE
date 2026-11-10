import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";
const require = createRequire(import.meta.url);

const { chatDayKey, chatDayLabel } = require(path.join(process.env.QUINTESSENCE_FRONTEND_BUILD, "chatDay.js"));

const at = (day, hour = 12) => new Date(2026, 10, day, hour, 0, 0);

test("chat day keys use local calendar dates", () => {
  assert.equal(chatDayKey(at(11).toISOString()), "2026-11-11");
  assert.equal(chatDayKey("not-a-date"), null);
  assert.equal(chatDayKey(null), null);
});

test("chat day labels distinguish today, yesterday and older dates", () => {
  const now = at(11, 18);
  assert.equal(chatDayLabel(at(11, 8).toISOString(), now, "zh-CN"), "今天");
  assert.equal(chatDayLabel(at(10).toISOString(), now, "zh-CN"), "昨天");
  assert.equal(chatDayLabel(at(9).toISOString(), now, "en-US"), "Nov 9");
  assert.equal(chatDayLabel("not-a-date", now, "zh-CN"), "");
});
