# QUINTESSENCE Roadmap

**From revisable persistent beliefs toward a cognitive system that carries experience, questions, and development forward.**

**English** · [简体中文](ROADMAP.zh-CN.md)

> Status checked 2026-10-21. The roadmap describes current priorities and completion conditions; it does not promise delivery dates. The P0 collaboration layer is not implemented. Design approved; implementation pending.

[Project overview](README.md) · [Architecture](ARCHITECTURE.md)

## Where the project is now

QUINTESSENCE has an early cognitive substrate and a real-model revision chain. The next focus is to bring these capabilities into everyday applications through a unified interface, and to test how revised persistent beliefs participate in later consultation.

| Work | Status | Existing results or remaining work |
|---|---|---|
| PCS2-001–009 foundation | Implemented foundation | Events, versions, state, minimal Profile / Episode, Belief / Context, and model port |
| PCS2-010 engineering | Closed | Specified engineering and real-model run work completed; successes, failures, and limits archived |
| EXP-01 v4 research evaluation | Awaiting human review | Runs complete; blind review, lock, unblinding, and final research conclusion pending |
| Migration, licensing, and formal mainline | Completed | First-party migration, Apache-2.0, tests and build acceptance; formal mainline switched to this repository |
| M6 GitHub Public repository | Completed | Public repository is live; no formal release package or version tag yet |
| P0 unified collaboration layer | Design approved, not implemented | Next foundation; builds on existing Cognition |
| 011 and later | Planned / research direction | Progress by capability dependency |

M1–M6 (migration, licensing, formal mainline, and Public repository preparation) are complete; P0 remains unimplemented.

“Implemented foundation” covers only the delivered scope; “engineering passed” and “real-model behavior verified” are recorded separately.

## What PCS2-010 left behind: results and open questions

PCS2-010 established and ran a controlled belief-update path: events and evidence enter Context, the model proposes operations, PCS validates and stores legal updates, and revised state can re-enter context.

The earlier five exploratory rounds and formal v4 are different experimental batches. In formal v4, six runs took place: one completed belief formation, revision, and the later Advice stage; three did not trigger a valid initial operation; two produced invalid operations during revision. The complete run shows the path is feasible; the other results show that model triggering and operation-following remain unstable. No claim that the overall study passed is made before human evaluation.

Direct correction and same-source duplication restraint in the old Q1–Q4 design also cannot directly prove transfer to new situations. The next plan will specifically test consultation behavior after applicability conditions change, without retroactively changing old data or mixing experimental denominators.

PCS2-010's engineering closure remains valid. Later research questions will not be endlessly added as hidden completion gates for it.

## Next: P0 unified LLM–PCS collaboration layer

P0 is a new priority label; its formal task number is still to be determined. The former PCS2-011 remains Knowledge.

The goal is to let applications reuse the formal Context, model-call, belief-candidate, and commit mechanisms so that state changes persist across interactions. The design uses a thin coordination entry point inside the library and preserves existing domain rules; the first phase does not build a background scheduler or a general Agent platform.

### Milestone A: unified cognitive review

Corresponds to internal implementation plan C01–C07.

- Extract shared generation steps; separate candidate generation from commit.
- Provide exact confirmation, rejection, result receipts, and restart recovery.
- Block automatic reruns of the same work after rejection; reconsideration requires explicit permission.
- Establish provenance and a read bridge for collaboration experience, connecting to future memory use.
- Jointly define provenance, Context, deletion, and authorization contracts before persistence.

**Completion conditions:** a synthetic tool-revision case runs through the formal review entry point; key negative cases such as duplicate operations, invalidated context, rejection, and recovery are verified. This stage does not require an interface first, and real-model effects are not a substitute for engineering acceptance.

### Milestone B: consult using current beliefs

Corresponds to internal implementation plan C08–C11.

- consult accepts natural-language input and queries and answers within permissions.
- Query material passes through the single ContextService; applications may not assemble cognitive context privately.
- Consultation does not create cognitive work or modify Belief; audit persistence is recorded explicitly.
- Revised beliefs remain available to new consultation after restart and after clearing the host's chat cache.

**Engineering completion conditions:** verify with a controllable model that actual input contains the correct, permitted version and can drive distinguishable output; negative cases such as deletion, permission revocation, and expiry must not leak old material.

**Real-model goal:** in a limited set of cases, a new scoped belief can reasonably change or narrow later advice. A legitimate WAIT may be the correct behavior; the model is not forced from waiting into action. When no effect is observed, record “not demonstrated” rather than substituting engineering tests for behavioral conclusions.

### Application and limited real research

Corresponds to internal implementation plan C12–C13.

A new application will be designed after the protocol stabilizes, providing consultation, provenance and scope display, candidate differences, confirmation or rejection, commit results, and a reauthorization entry point. Legacy Workbench V0.2 is frozen as an early demonstration demo; its bespoke calling protocol will not be migrated.

Small-scale real research uses synthetic tool capabilities and synthetic persona-context preference cases. The latter tests scope understanding and does not mean Persona P1 is implemented. Raw outputs, actual requests, provenance, failures, and costs are retained and evaluated against criteria defined in advance; results belong to the behavioral acceptance of Milestone B.

The new interface is not a prerequisite for the underlying collaboration capability, and real research does not automatically expand into a large-scale evaluation platform.

## Next capability evolution

The following is the current priority order; implementation details will follow prior evidence and design refinement, without fixed dates.

| Stage | Problem to solve | Observable completion direction |
|---|---|---|
| **011 · Knowledge** | How can currently applicable knowledge be obtained from existing beliefs? | Traceable projection and retrieval that correctly filters old versions, invalid states, and out-of-scope material; results enter formal Context |
| **012 · Question / Focus** | How can experience leave persistent questions and focus? | Questions and focus persist across calls; compare related experience and run EXP-02; internal triggers validated separately by scope |
| **013 · Persona P1** | How does contextual feedback influence the expression of stable dispositions? | Source-bearing, constrained contextual-feedback updates and EXP-03; avoid expanding local preferences into a global persona |
| **014 · Host collaboration** | How can PCS participate in real host environments? | DSH Observe / Advisory connects to the formal collaboration protocol and the single Context; the host provides real execution receipts |

Memory's experience selection, Episode integration, and retrieval will be designed incrementally with these needs. P0 first provides the provenance bridge and does not prematurely claim delivery of a complete memory system.

## Farther research

The five conceptual capabilities describe the project's long-term direction, not five modules that must all be finished before use.

- **Continuous cognitive activity**: limited review triggered by state changes, unresolved problems, and persistent focus, with explicit stopping and recovery conditions.
- **Affect**: how evaluation of objects and experiences forms, is revised, and participates in selection, while preserving the semantic distinction between fact and evaluation.
- **Load & Recovery**: study load, recovery, and operational rhythm starting from concrete activity budgets and stopping conditions.
- **Long-term development**: study experience organization, disposition change, and individual continuity across time, with traceable provenance and controllable private data.
- **More unified runtime orchestration**: evaluate further integration of session, model, task, and tool lifecycles only after real host-coordination needs appear; there is no commitment to rewrite DSH now.

Models, retrieval components, or external memory projects are evaluated per concrete need; no mandatory technical dependency is assumed.

## Publication and research proceed separately

Local migration, licensing, build acceptance, and the formal mainline switch are complete; the GitHub Public repository is live. A formal release package, installation examples, and versioned release still need to be arranged. Linux verification has been deferred and is not listed as a supported environment.

Publication preparation and capability development are recorded separately. GitHub upload is not a technical dependency of the cognitive mechanisms; execution order is set by the maintainer, and publication status is not rewritten as engineering capability or experimental results.

This roadmap does not publish sealed experiment material, private instance content, or complete internal run records. Any future research report should use approved results and synthetic examples.

## How to judge whether the next stage should change

Each stage prioritizes one observable problem and records implemented, offline-tested, real-model-tested, not-demonstrated, and known-limitation status separately. Use the minimum necessary tests and reviewable evidence; do not create a new governance process for every small change.

Experiments help determine whether a mechanism holds and where the gaps are; the project's pursuit of persistent individuals and long-term development does not depend on a higher score on any short-term task. If results do not support expectations, revise the conclusion and the next design while preserving the real history.
