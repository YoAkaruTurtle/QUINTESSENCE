import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const build = process.env.QUINTESSENCE_FRONTEND_BUILD;
if (!build) {
  throw new Error("QUINTESSENCE_FRONTEND_BUILD is not set");
}
const require = createRequire(import.meta.url);
const reviewModule = require(path.join(build, "review.js"));

const {
  INITIAL_REVIEW_STATE,
  approveEnabled,
  buildGenerateParams,
  buildResolveParams,
  candidateStatusLabel,
  eligibilityLabel,
  generateBlockReason,
  generateEnabled,
  receiptVisible,
  rejectEnabled,
  reviewAvailable,
  reviewCanRecover,
  reviewCaps,
  reviewErrorText,
  reviewReduce,
  reviewViewLabel,
  reviewWorkState,
  resolveBlockReason,
  sourceAvailabilityLabel,
  splitDiff,
} = reviewModule;

const NS = "example.test.desktop.lab";

function capabilities(overrides = {}) {
  return {
    read: true,
    generate: true,
    approve: true,
    reject: true,
    result: true,
    ...overrides,
  };
}

function host(overrides = {}) {
  return {
    app: "quintessence-desktop",
    version: "0.1.0",
    protocol: "desktop.v1",
    mode: "lab",
    label: "QUINTESSENCE Lab",
    ai_display_name: null,
    ui: { ai_display_name: "助手", source: "default" },
    session_id: "11111111-1111-4111-8111-111111111111",
    host: {
      namespace: NS,
      host_instance: "host:desktop-lab",
      operator: "operator:desktop-lab",
      retention: "standard",
      input_privacy_class: "private",
      policy_ref: "policy:collab.v1",
      ready: true,
      reason_code: null,
      reason_detail: null,
    },
    destination: {
      destination_id: "sha256:destination",
      provider: "synthetic",
      model: "scripted-collab-v1",
      external: true,
      kind: "external",
      confirmation_required: true,
      manifest_hash: "sha256:manifest",
      policy_ref: "policy:collab.v1",
    },
    workspace: { consult: true, review: true, review_reason: null },
    capabilities: {
      consult: true,
      confirm_destination: true,
      input_registration: true,
      review: capabilities(
        overrides.capabilities ? overrides.capabilities : {},
      ),
    },
    limits: {
      frame_bytes: 1048576,
      request_timeout_seconds: 120,
      max_question_chars: 8000,
    },
    methods: [
      "review.work.list",
      "review.candidates.list",
      "review.candidate.detail",
      "review.generate",
      "review.resolve",
      "review.result",
    ],
    reserved: ["review.commit"],
    pcs: {
      package: "quintessence_pcs",
      version: "pcs.v2",
      schema_migrations: 10,
      facade: "ConsultFacade + ReviewFacade",
    },
    ...overrides.info,
  };
}

function work(overrides = {}) {
  return {
    work_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    event_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    generation: 0,
    blocked: false,
    rerun_registered: false,
    ...overrides,
  };
}

function candidate(overrides = {}) {
  return {
    candidate_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    artifact_version: 1,
    work_id: work().work_id,
    work_generation: 0,
    interaction_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    context_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    status: "pending",
    created_at: "2026-10-24T00:00:00Z",
    decided_at: null,
    attempt_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
    attempt_no: 1,
    ...overrides,
  };
}

function detail(overrides = {}) {
  return {
    candidate: candidate(),
    proposal: {
      operation: "propose_belief",
      target_ref: null,
      claim: "capability:lab-file-read",
      claim_type: "capability",
      scope: { scene: "lab.synthetic" },
      evidence_links: [
        {
          ref: {
            ref_type: "event",
            namespace: NS,
            event_id: work().event_id,
          },
          relation: "supports",
        },
      ],
      strength: "moderate",
      strength_reason: "synthetic",
      valid_until: null,
    },
    structural_validation: "passed",
    current: null,
    current_state: "not_applicable",
    diff: [
      { field: "claim", before: null, after: "capability:lab-file-read", changed: true },
      { field: "claim_type", before: null, after: "capability", changed: true },
      { field: "scope", before: null, after: { scene: "lab.synthetic" }, changed: true },
      { field: "evidence_links", before: null, after: [{ ref: {} }], changed: true },
      { field: "strength", before: null, after: "moderate", changed: true },
      { field: "valid_until", before: null, after: null, changed: true },
    ],
    sources: [
      {
        ref: { ref_type: "event", namespace: NS, event_id: work().event_id },
        kind: "event",
        availability: "available",
        reason: null,
        body: null,
        ref_version: null,
      },
      {
        ref: {
          ref_type: "record",
          namespace: NS,
          record_id: "deadbeef-dead-4ead-8ead-deadbeefdead",
          record_version: 1,
        },
        kind: "record",
        availability: "redacted",
        reason: "privacy",
        body: null,
        ref_version: 1,
      },
    ],
    can_approve: true,
    can_reject: true,
    eligibility_reason: null,
    redacted: false,
    detail: null,
    ...overrides,
  };
}

function detailState(overrides = {}) {
  return reviewReduce(
    reviewReduce(
      reviewReduce(
        reviewReduce(INITIAL_REVIEW_STATE, {
          type: "works-loaded",
          managed: [work()],
          legacy: [],
        }),
        { type: "work-selected", work: work() },
      ),
      { type: "candidates-loaded", candidates: [candidate()] },
    ),
    { type: "candidate-selected", candidate: candidate() },
  );
}

test("review availability follows the host capability, not a local grant", () => {
  assert.equal(reviewAvailable(null), false);
  assert.equal(reviewAvailable(host()), true);
  assert.equal(
    reviewAvailable(
      host({
        info: {
          workspace: { consult: true, review: false, review_reason: "REVIEW_NOT_CONFIGURED" },
        },
      }),
    ),
    false,
  );
  assert.equal(
    reviewAvailable(
      host({ capabilities: { read: false, generate: true } }),
    ),
    false,
  );
  assert.equal(reviewCaps(host()).approve, true);
});

test("can_approve/can_reject never replace a host permission", () => {
  const state = { ...detailState(), detail: detail() };
  assert.equal(approveEnabled(state, host()), true);
  assert.equal(
    approveEnabled(state, host({ capabilities: { approve: false } })),
    false,
  );
  assert.match(
    resolveBlockReason(state, host({ capabilities: { approve: false } }), "approve"),
    /缺少审批权限/,
  );
  assert.equal(
    rejectEnabled(state, host({ capabilities: { reject: false } })),
    false,
  );
  // The content hint is still true; the permission is what disables it.
  assert.equal(state.detail.can_approve, true);
});

test("v1 and exact version/work-generation rules gate both actions", () => {
  const nonV1 = { ...detailState(), detail: detail({ candidate: candidate({ artifact_version: 2 }) }) };
  assert.equal(approveEnabled(nonV1, host()), false);
  assert.equal(rejectEnabled(nonV1, host()), false);
  assert.match(
    resolveBlockReason(nonV1, host(), "approve"),
    /不在 C12c 支持范围/,
  );

  const terminal = {
    ...detailState(),
    detail: detail({ candidate: candidate({ status: "committed" }) }),
  };
  assert.equal(approveEnabled(terminal, host()), false);
  assert.equal(rejectEnabled(terminal, host()), false);
  assert.match(
    resolveBlockReason(terminal, host(), "approve"),
    /不是待决定状态/,
  );

  const staleGeneration = {
    ...detailState(),
    detail: detail({ eligibility_reason: "stale_generation", can_reject: false }),
  };
  assert.equal(approveEnabled(staleGeneration, host()), false);
  assert.equal(rejectEnabled(staleGeneration, host()), false);

  const generationMismatch = {
    ...detailState(),
    selectedWork: work({ generation: 2 }),
    detail: detail(),
  };
  assert.equal(approveEnabled(generationMismatch, host()), false);
  assert.equal(rejectEnabled(generationMismatch, host()), false);
});

test("current moved blocks only approve; current redacted still allows reject", () => {
  const moved = {
    ...detailState(),
    detail: detail({
      current_state: "available",
      current: {
        ref: { ref_type: "record", namespace: NS, record_id: "r", record_version: 1 },
        claim: "old",
        claim_type: "capability",
        status: "supported",
        strength: "moderate",
        scope: {},
        valid_until: null,
        applicability: "general",
      },
      can_approve: false,
      can_reject: true,
      eligibility_reason: "target_moved",
    }),
  };
  assert.equal(approveEnabled(moved, host()), false);
  assert.equal(rejectEnabled(moved, host()), true);
  assert.match(resolveBlockReason(moved, host(), "approve"), /目标版本已变化/);

  const redactedCurrent = {
    ...detailState(),
    detail: detail({
      current_state: "redacted",
      current: null,
      can_approve: false,
      can_reject: true,
      eligibility_reason: "current_redacted",
    }),
  };
  assert.equal(approveEnabled(redactedCurrent, host()), false);
  assert.equal(rejectEnabled(redactedCurrent, host()), true);

  const redactedCandidate = {
    ...detailState(),
    detail: detail({
      redacted: true,
      proposal: null,
      can_approve: false,
      can_reject: false,
      eligibility_reason: "candidate_redacted",
    }),
  };
  assert.equal(approveEnabled(redactedCandidate, host()), false);
  assert.equal(rejectEnabled(redactedCandidate, host()), false);
});

test("claim-freeze preflight blocks approval and asks for retract then replacement", () => {
  const frozen = {
    ...detailState(),
    detail: detail({
      can_approve: false,
      can_reject: true,
      eligibility_reason: "claim_text_frozen",
    }),
  };
  assert.equal(approveEnabled(frozen, host()), false);
  assert.equal(rejectEnabled(frozen, host()), true);
  const reason = resolveBlockReason(frozen, host(), "approve");
  assert.match(reason, /冻结的认识正文/);
  assert.match(reason, /撤回旧认识/);
  assert.match(reason, /替代认识/);
  assert.equal(
    eligibilityLabel("claim_text_frozen", "fallback"),
    "此候选试图改变已冻结的认识正文；请先撤回旧认识，再依据新证据建立替代认识",
  );
  assert.match(
    eligibilityLabel("claim_type_frozen", "fallback"),
    /冻结的认识类型/,
  );
});

test("generate is refused for legacy, pending, committed and paused work", () => {
  const base = {
    ...INITIAL_REVIEW_STATE,
    loaded: true,
    managed: [work()],
    legacy: [],
    selectedWork: work(),
    selectedWorkId: work().work_id,
    screen: "candidates",
  };
  assert.equal(generateEnabled(base, host()), true);

  const pending = { ...base, candidates: [candidate()] };
  assert.equal(generateEnabled(pending, host()), false);
  assert.match(generateBlockReason(pending, host()), /已有待决定候选/);

  const committed = {
    ...base,
    candidates: [candidate({ status: "committed" })],
  };
  assert.equal(generateEnabled(committed, host()), false);
  assert.match(generateBlockReason(committed, host()), /已提交候选/);

  const blocked = { ...base, selectedWork: work({ blocked: true }) };
  assert.equal(generateEnabled(blocked, host()), false);
  assert.match(generateBlockReason(blocked, host()), /需要宿主发放新的复查许可/);
  const rerun = {
    ...base,
    selectedWork: work({ blocked: true, rerun_registered: true }),
  };
  assert.equal(generateEnabled(rerun, host()), true);

  const legacy = {
    ...base,
    selectedWork: null,
    selectedLegacy: {
      work_id: "legacy",
      event_id: "event",
      kind: "synthetic",
      status: "pending",
      created_at: "now",
    },
  };
  assert.equal(generateEnabled(legacy, host()), false);
  assert.match(generateBlockReason(legacy, host()), /旧工作项只读/);

  assert.equal(
    generateEnabled(base, host({ capabilities: { generate: false } })),
    false,
  );
});

test("request builders name only the host-approved fields", () => {
  const state = {
    ...detailState(),
    selectedWork: work(),
    selectedWorkId: work().work_id,
    detail: detail(),
  };
  const generated = buildGenerateParams(host().session_id, state, "sha256:destination");
  assert.deepEqual(Object.keys(generated).sort(), [
    "confirmedDestinationId",
    "eventId",
    "sessionId",
    "workId",
  ]);
  assert.equal("feedback" in generated, false);
  assert.equal("provider" in generated, false);
  assert.equal("model" in generated, false);
  assert.equal("approval" in generated, false);
  assert.equal("grant" in generated, false);

  const resolved = buildResolveParams(host().session_id, detail(), "approve");
  assert.deepEqual(Object.keys(resolved).sort(), [
    "artifactVersion",
    "candidateId",
    "decision",
    "sessionId",
  ]);
  assert.equal("approval" in resolved, false);
  assert.equal("grant" in resolved, false);
  assert.equal("candidateBody" in resolved, false);
  assert.equal("provider" in resolved, false);
  assert.equal("model" in resolved, false);
});

test("double submit is blocked and a conflict refreshes without retrying", () => {
  const state = { ...detailState(), detail: detail() };
  const started = reviewReduce(state, { type: "resolve-start", decision: "approve" });
  assert.equal(started.busy, "resolve");
  assert.equal(reviewReduce(started, { type: "resolve-start", decision: "approve" }).busy, "resolve");
  assert.equal(approveEnabled(started, host()), false);

  const conflict = reviewReduce(started, {
    type: "action-error",
    error: { code: "REVISION_CONFLICT", message: "moved", result_unknown: false },
    kind: "resolve",
  });
  assert.equal(conflict.busy, null);
  assert.equal(conflict.refreshRequested, true);
  assert.match(conflict.conflict, /目标版本已变化/);
  assert.equal(conflict.screen, "detail");
  assert.equal("retry" in conflict, false);
});

test("unknown result keeps an unknown banner and recovers only by id", () => {
  const state = {
    ...detailState(),
    detail: detail(),
    lastInteractionId: candidate().interaction_id,
  };
  const unknown = reviewReduce(state, {
    type: "action-error",
    error: { code: "INTERNAL_ERROR", message: "lost", result_unknown: true },
    kind: "resolve",
  });
  assert.equal(unknown.busy, null);
  assert.equal(unknown.unknown.interactionId, candidate().interaction_id);
  assert.match(unknown.unknown.message, /结果未知/);
  assert.equal(unknown.outcome, null);
  assert.equal(reviewCanRecover(unknown, host()), true);
  assert.equal(reviewCanRecover(unknown, host({ capabilities: { result: false } })), false);

  const recovered = reviewReduce(unknown, {
    type: "result-result",
    view: {
      state: "committed",
      interaction_id: candidate().interaction_id,
      candidate_id: candidate().candidate_id,
      receipt: {
        request_id: "r",
        context_id: "c",
        operation: "propose_belief",
        outcome: "committed",
        committed_at: "now",
        record_refs: [],
      },
      redacted: false,
      detail: null,
      model_adapter: "direct-http",
    },
  });
  assert.equal(recovered.unknown, null);
  assert.equal(recovered.screen, "result");
  assert.equal(recovered.outcome.state, "committed");
  assert.equal(receiptVisible(recovered.outcome), true);
});

test("receipt display is honest when redacted or absent", () => {
  assert.equal(receiptVisible(null), false);
  assert.equal(
    receiptVisible({
      state: "committed",
      interaction_id: null,
      candidate_id: null,
      receipt: null,
      redacted: true,
      detail: null,
      model_adapter: null,
    }),
    false,
  );
  assert.equal(
    receiptVisible({
      state: "committed",
      interaction_id: null,
      candidate_id: null,
      receipt: {
        request_id: "r",
        context_id: "c",
        operation: "propose_belief",
        outcome: "committed",
        committed_at: "now",
        record_refs: [],
      },
      redacted: false,
      detail: null,
      model_adapter: null,
    }),
    true,
  );
});

test("waiting, rejected and reauth keep the correct screens", () => {
  const waiting = reviewReduce(
    { ...INITIAL_REVIEW_STATE, busy: "generate" },
    {
      type: "generate-result",
      view: {
        state: "waiting",
        interaction_id: "i",
        candidate_id: null,
        receipt: null,
        redacted: false,
        detail: null,
        model_adapter: "direct-http",
      },
    },
  );
  assert.equal(waiting.screen, "result");
  assert.equal(waiting.outcome.state, "waiting");

  const rejected = reviewReduce(
    { ...detailState(), detail: detail() },
    {
      type: "resolve-result",
      view: {
        state: "rejected",
        interaction_id: "i",
        candidate_id: candidate().candidate_id,
        receipt: null,
        redacted: false,
        detail: null,
        model_adapter: "direct-http",
      },
    },
  );
  assert.equal(rejected.screen, "result");
  assert.equal(rejected.outcome.state, "rejected");
  assert.equal(reviewViewLabel("rejected"), "已拒绝 · 已暂停");

  const reauth = reviewReduce(rejected, { type: "reauth-open" });
  assert.equal(reauth.screen, "reauth");
  assert.equal(reviewReduce(reauth, { type: "reauth-close" }).screen, "result");
});

test("diff and source labels mark changed fields and three-state sources", () => {
  const { changed, unchanged } = splitDiff(detail());
  assert.equal(changed.length, 6);
  assert.equal(unchanged.length, 0);
  assert.equal(sourceAvailabilityLabel("available"), "可读");
  assert.equal(sourceAvailabilityLabel("redacted"), "已隐藏");
  assert.equal(sourceAvailabilityLabel("unavailable"), "不可用");
  assert.equal(candidateStatusLabel("pending"), "待决定");
});

test("work rows show candidate lifecycle instead of a stale pending label", () => {
  assert.deepEqual(reviewWorkState("committed", false), {
    label: "已提交",
    tone: "ok",
  });
  assert.deepEqual(reviewWorkState("pending", false), {
    label: "待决定",
    tone: "pending",
  });
  assert.deepEqual(reviewWorkState("rejected", true), {
    label: "已拒绝",
    tone: "warn",
  });
  assert.deepEqual(reviewWorkState(null, true), {
    label: "已暂停",
    tone: "warn",
  });
  assert.deepEqual(reviewWorkState(null, false), {
    label: "待处理",
    tone: "pending",
  });
});

test("error text never claims success or failure for an unknown result", () => {
  assert.match(
    reviewErrorText({ code: "INTERNAL_ERROR", message: "", result_unknown: true }),
    /结果未知/,
  );
  assert.match(
    reviewErrorText({ code: "REVISION_CONFLICT", message: "", result_unknown: false }),
    /目标版本已变化/,
  );
  assert.match(
    reviewErrorText({ code: "AUTHORIZATION_REQUIRED", message: "", result_unknown: false }),
    /缺少审批权限/,
  );
});
