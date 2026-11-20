# Desktop Dreaming and continuity plan

This record preserves the approved product scope from clean main `e9dccaa`.
The [acceptance record](acceptance.md) describes the verified delivery at code
tip `acd6d9c`, including its limits. These records are evidence for that
baseline.

## Goal and authority

Enable an existing desktop instance to maintain recent continuity and perform
bounded reflection while idle. Keep the PCS authoritative for source identity,
scope, privacy, retention, authorization, and formal cognition changes.

The startup guarantee applies to registered, ready instances. Validate identity,
recovery state, compatible schema, and paths before publishing new instance-owned
Dreaming state. Preserve the original SQLite files and sidecars, manifests,
configuration, preferences, logs, recovery material, and existing lock-file
identity during startup and idle Dreaming. Refuse the affected path on collisions,
unsupported schema, or recovery conflicts. Explicit creation, repair, and move
operations retain their existing lifecycle.

The startup boundary is implemented through
[instance management](../../apps/desktop/sidecar/instance_manager.py) and the
[desktop runtime](../../apps/desktop/sidecar/runtime.py).

## Approved work

1. Reuse the resident desktop bridge, foreground admission, and bounded
   maintenance behavior. Persist automatic work in the new instance-owned
   component. Shallow work refreshes recent Experience. Deep work uses bounded
   history pages and eligible OpenQuestions. Preserve progress across restart
   and publish a handled batch's checkpoint together with its wake result.
2. Give the empty-queue path an independent due trigger. A due wake may make one
   separately authorized model request and produce one supported question,
   topic, optional next-step proposal, or no-op. The proposed defaults are
   15 minutes of true idle, 30 minutes between self-directed attempts, and
   four attempts per rolling 24 hours. Count committed attempts across restart
   and crashes.
3. Reuse the production model-policy and transport boundaries. Keep background
   purpose, destination, grant, and attempt accounting explicit. Recheck
   foreground work, live sources, and authorization before transport admission
   and before publishing results. Queued or running foreground work, including
   tool work, cancels background work. Instance or attempt-generation changes
   invalidate late results.
4. Keep autonomous action requests as bounded local proposals with stable
   identity, source references, scope, effect, and expiry. Expose a future
   trusted-host handoff for explicit authorization. Formal Belief and
   OpenQuestion changes use the existing user-triggered Review and Tx B flow.
5. Assemble a rolling 24-hour continuity view by source occurrence time and
   within the Context budget. Carry permitted chat, tool progress, unfinished
   work, and epistemic status across sessions. Resolve exact source references
   again at use time. Use eligible raw events as a bounded fallback when the
   recent projection is unavailable or stale.
6. Compose the pinned Scope Recall runtime with a rebuildable projection in
   the new instance-owned component. Exercise its actual worker and vector
   recall with authorized source IDs and revisions. Recheck source eligibility
   before embedding or provider sends and again before delivery. Retain
   keyword recall when semantic capability is unavailable.
7. Present actual idle, shallow, deep, thinking, foreground-yield, and degraded
   state in an instance-scoped Tauri floating window. The host owns scheduling
   and authorization. The window observes current status.

Scheduling, attempt persistence, and proposals are defined by
[Dreaming state](../../src/quintessence_pcs/runtime/dreaming.py),
[history](../../src/quintessence_pcs/runtime/dreaming_history.py), and the
[resident host](../../apps/desktop/sidecar/dreaming_host.py). The
[model boundary](../../src/quintessence_pcs/runtime/dreaming_model.py),
[Context assembly](../../src/quintessence_pcs/runtime/context.py), and
[Scope Recall adapter](../../src/quintessence_pcs/adapters/scope_recall/dreaming.py)
own their respective live checks. The
[Tauri orb](../../apps/desktop/src-tauri/src/dreaming_orb.rs) projects host state
through the [frontend](../../apps/desktop/src/dreamingOrb.ts).

## Acceptance criteria

- Cold and warm starts preserve the original bytes and file identities of a
  synthetic registered instance, including existing locks, rotation-threshold
  logs, SQLite sidecars, settings, and recovery files. Check restart,
  interruption, collisions, multiple instances, and ordinary authorized chat.
- Clock-driven idle work observes timing and quota limits, performs one
  policy-checked send, and persists a local result or no-op. Check concurrency,
  exhausted budgets, restart, expiry, revocation, foreground cancellation,
  instance switching, and late-result rejection.
- A new conversation receives eligible recent chat and task progress. Check
  exact time boundaries, source revisions, privacy, retention, and immediate
  exclusion of deleted or revoked sources.
- Run the actual Scope Recall vector path in a synthetic instance. Check a
  paraphrase without lexical overlap, disabled-vector fallback, source tracing,
  scope rejection, and revocation while queued or before embedding.
- Exercise the native floating window against live backend state, including
  disconnect, foreground work, instance switch, and close.
- Independently reproduce affected tests and the standard offline suite.
  Validate repository content and the final documentation boundary.

## Delivery decisions

The feasibility requirement is additive startup together with a genuine
policy-checked idle request using a deterministic final transport. Autonomous
output remains local. The future proposal interface has no connected executor.

Working plans and raw evidence remain external during implementation. At product
closeout, retain these reviewed feature records on the product branch. The
verified behavior, grant limits, test baselines, and deferred interfaces are
recorded in the acceptance report.
