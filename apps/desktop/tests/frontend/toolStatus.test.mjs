import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";

const build = process.env.QUINTESSENCE_FRONTEND_BUILD;
if (!build) {
  throw new Error("QUINTESSENCE_FRONTEND_BUILD is not set");
}
const require = createRequire(import.meta.url);
const { toolStatusLabel } = require(path.join(build, "toolStatus.js"));

test("planned and unknown statuses never claim the tool is running", () => {
  assert.equal(toolStatusLabel(undefined), "已计划");
  assert.equal(toolStatusLabel("planned"), "已计划");
  assert.notEqual(toolStatusLabel(undefined), "运行中");
  assert.notEqual(toolStatusLabel("planned"), "运行中");
});

test("only a real running/started status says running", () => {
  assert.equal(toolStatusLabel("running"), "运行中");
  assert.equal(toolStatusLabel("started"), "运行中");
});

test("terminal and refusal statuses keep their own labels", () => {
  assert.equal(toolStatusLabel("succeeded"), "成功");
  assert.equal(toolStatusLabel("failed"), "失败");
  assert.equal(toolStatusLabel("rejected"), "已拒绝");
  assert.equal(toolStatusLabel("cancelled"), "已取消");
});
