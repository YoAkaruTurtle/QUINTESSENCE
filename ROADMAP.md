# QUINTESSENCE Roadmap

**From revisable persistent beliefs toward a cognitive system that carries experience, questions, and development forward.**

**English** · [简体中文](ROADMAP.zh-CN.md)

> Status checked 2026-11-05. J-series and folder-instance A/B/C engineering are complete on local main within their exercised scope. No delivery date is promised; formal release, signing and genuinely clean-machine installation remain pending.

[Project overview](README.md) · [Architecture](ARCHITECTURE.md)

## Where the project is now

QUINTESSENCE has an early cognitive substrate and a real-model revision chain. Unified collaboration, chat and folder-owned instances are available in the Windows application; next work concerns bounded incremental maintenance, research and installation maturity.

| Work | Status | Existing results or remaining work |
|---|---|---|
| PCS2-001–009 foundation | Implemented foundation | Events, versions, state, minimal Profile / Episode, Belief / Context, and model port |
| PCS2-010 engineering | Closed | Specified engineering and real-model run work completed; successes, failures, and limits archived |
| EXP-01 v4 research evaluation | Awaiting human review | Runs complete; blind review, lock, unblinding, and final research conclusion pending |
| Migration, licensing, and formal mainline | Completed | First-party migration, Apache-2.0, tests and build acceptance; formal mainline switched to this repository |
| M6 GitHub Public repository | Completed | Public repository is live; no formal release package or version tag yet |
| P0 unified collaboration layer | Implemented and merged to `main`; controlled real-model evidence | Consult, deterministic query, Review, approval, recovery, Windows desktop client; release packaging and signing pending |
| J-series engineering | Implemented, with behavioral limits | Persona/SDRPE, Memory delivery/recall, Cognition governance, transcripts, bounded maintenance and recovery |
| Folder-instance A/B/C | Implemented and exercised | Empty creation, complete relocation/restart, safe partial completion, GUI selection and explicit legacy registration |
| Windows packaging/local installation | Exercised locally | Complete frozen sidecar and NSIS development installer; genuine clean-machine test not run |

M1–M6, P0, the J-series and A/B/C are complete within their engineering scope. The Windows installer has been exercised locally; it remains an unsigned development build, not a formal public release.

“Implemented foundation” covers only the delivered scope; “engineering passed” and “real-model behavior verified” are recorded separately.

## What PCS2-010 left behind: results and open questions

PCS2-010 established and ran a controlled belief-update path: events and evidence enter Context, the model proposes operations, PCS validates and stores legal updates, and revised state can re-enter context.

The earlier five exploratory rounds and formal v4 are different experimental batches. In formal v4, six runs took place: one completed belief formation, revision, and the later Advice stage; three did not trigger a valid initial operation; two produced invalid operations during revision. The complete run shows the path is feasible; the other results show that model triggering and operation-following remain unstable. No claim that the overall study passed is made before human evaluation.

Direct correction and same-source duplication restraint in the old Q1–Q4 design also cannot directly prove transfer to new situations. The next plan will specifically test consultation behavior after applicability conditions change, without retroactively changing old data or mixing experimental denominators.

PCS2-010's engineering closure remains valid. Later research questions will not be endlessly added as hidden completion gates for it.

## P0 delivery: unified LLM–PCS collaboration layer

P0 is a new priority label; its formal task number is still to be determined. The former PCS2-011 remains Knowledge.

The goal is to let applications reuse the formal Context, model-call, belief-candidate, and commit mechanisms so that state changes persist across interactions. The implementation uses a thin coordination entry point inside the library and preserves existing domain rules; the first phase does not build a background scheduler or a general Agent platform.

Milestones A and B and the Windows desktop application are implemented on `main`. Engineering checks cover the review/query/commit chain, and a controlled C13b synthetic run provides real-model evidence for the formal retract/replacement and post-restart query path. This does not establish human research evaluation, release signing, or a fresh-machine distribution claim.

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

A development Windows application is implemented on `main`, providing consultation, provenance and scope display, candidate differences, confirmation or rejection, commit results, and a reauthorization entry point. Legacy Workbench V0.2 is frozen as an early demonstration demo; its bespoke calling protocol is not migrated.

Small-scale real research uses synthetic tool capabilities and synthetic persona-context preference cases. The latter tests scope understanding and does not mean Persona P1 is implemented. Raw outputs, actual requests, provenance, failures, and costs are retained and evaluated against criteria defined in advance; results belong to the behavioral acceptance of Milestone B.

The new interface is not a prerequisite for the underlying collaboration capability, and real research does not automatically expand into a large-scale evaluation platform.

## Next capability evolution

| Direction | Status | Next observable work |
|---|---|---|
| Dreaming | Design and partial foundations; full system pending | Incremental Experience organization; coordinate Memory/Cognition workers, budgets, review and interruption recovery; validate processing rather than queue admission alone |
| Work and DSH tools | Planned | Connect host observations, authorization and real execution receipts to existing collaboration boundaries; DSH is not a current core dependency |
| Deeper Cognition and growth | Ongoing research; full growth not implemented | Source support, belief revision, Knowledge and Question/Focus, with separate behavioral evidence |
| Memory deletion/cross-module forgetting | Not complete | Invalidation, removal and recovery consistency across PCS, Scope Recall, derived artifacts and backups; PCS deletion currently leaves delivered Memory text |
| Persona evolution | Management/expression delivered; further evolution pending | Traceable, source-bearing and constrained contextual feedback, without turning a local preference into global identity |
| Installation/compatibility | Local Windows checks complete; further work | Genuine clean-machine installation, WebView/overlay compatibility, signing/versioned releases; Linux/macOS require separate verification |

Bounded maintenance, review linkage and raw Memory delivery do not constitute complete Dreaming, semantic memory or autonomous continual learning. A design document does not close an implementation item.

## Farther research

The five conceptual capabilities describe the project's long-term direction, not five modules that must all be finished before use.

- **Continuous cognitive activity**: limited review triggered by state changes, unresolved problems, and persistent focus, with explicit stopping and recovery conditions.
- **Affect**: how evaluation of objects and experiences forms, is revised, and participates in selection, while preserving the semantic distinction between fact and evaluation.
- **Load & Recovery**: study load, recovery, and operational rhythm starting from concrete activity budgets and stopping conditions.
- **Long-term development**: study experience organization, disposition change, and individual continuity across time, with traceable provenance and controllable private data.
- **More unified runtime orchestration**: evaluate further integration of session, model, task, and tool lifecycles only after real host-coordination needs appear; there is no commitment to rewrite DSH now.

Scope Recall is already integrated for Memory. Future model, retrieval and host choices remain driven by concrete needs rather than a new general platform.

## Publication and research proceed separately

Local migration, licensing, build acceptance, and the formal mainline switch are complete; the GitHub Public repository is live. P0, the J-series and A/B/C are on local main; complete Windows packaging and local installation have been exercised. Formal release, signing, genuinely clean-machine validation and versioned publication remain pending. Linux verification has been deferred and is not listed as a supported environment.

Publication preparation and capability development are recorded separately. GitHub upload is not a technical dependency of the cognitive mechanisms; execution order is set by the maintainer, and publication status is not rewritten as engineering capability or experimental results.

This roadmap does not publish sealed experiment material, private instance content, or complete internal run records. Any future research report should use approved results and synthetic examples.

## How to judge whether the next stage should change

Each stage prioritizes one observable problem and records implemented, offline-tested, real-model-tested, not-demonstrated, and known-limitation status separately. Use the minimum necessary tests and reviewable evidence; do not create a new governance process for every small change.

Experiments help determine whether a mechanism holds and where the gaps are; the project's pursuit of persistent individuals and long-term development does not depend on a higher score on any short-term task. If results do not support expectations, revise the conclusion and the next design while preserving the real history.
