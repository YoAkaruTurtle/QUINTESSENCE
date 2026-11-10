import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";
const require = createRequire(import.meta.url);
const { cognitionAvailable, runCognitionDecision } = require(path.join(process.env.QUINTESSENCE_FRONTEND_BUILD, "cognitionDecision.js"));
const fresh = () => ({ busy: null, decisionError: null, notice: null, error: null });

test("chat cognition remains reachable without granting managed-work permissions", () => {
  const info = { host: { ready: true }, capabilities: { cognition: { enabled: true }, review: { read: false } } };
  assert.equal(cognitionAvailable(info), true);
  assert.equal(info.capabilities.review.read, false);
  assert.equal(cognitionAvailable({ ...info, host: { ready: false } }), false);
  assert.equal(cognitionAvailable({ ...info, capabilities: {} }), false);
});

test("failed decision survives a successful list refresh and renders pending immediately", async () => {
  const state = fresh();
  const frames = [];
  await runCognitionDecision(state, "synthetic-candidate", "approve",
    async () => ({ ok: false, error: { message: "Candidate version changed" } }),
    async () => { state.error = null; },
    () => frames.push({ ...state }), String);
  assert.equal(frames[0].busy, "synthetic-candidate");
  assert.equal(frames.at(-1).busy, null);
  assert.equal(state.decisionError, "Candidate version changed");
  assert.equal(state.notice, null);
});

test("duplicate clicks are blocked until refresh completes", async () => {
  const state = fresh();
  let release;
  let calls = 0;
  const submit = async () => { calls++; return { ok: true }; };
  const refresh = () => new Promise(resolve => { release = resolve; });
  const first = runCognitionDecision(state, "synthetic-candidate", "reject", submit, refresh, () => {}, String);
  await Promise.resolve();
  await runCognitionDecision(state, "synthetic-candidate", "approve", submit, refresh, () => {}, String);
  assert.equal(calls, 1);
  release();
  await first;
  assert.match(state.notice, /拒绝/);
  assert.equal(state.busy, null);
});

test("transport failure remains visible alongside refresh failure", async () => {
  const state = fresh();
  await runCognitionDecision(state, "synthetic-candidate", "approve",
    async () => { throw new Error("Disconnected"); },
    async () => { state.error = "Refresh unavailable"; }, () => {}, e => e.message);
  assert.equal(state.decisionError, "Disconnected");
  assert.equal(state.error, "Refresh unavailable");
  assert.equal(state.busy, null);
});
