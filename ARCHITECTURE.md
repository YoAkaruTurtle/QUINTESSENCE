# QUINTESSENCE Architecture

**How persistent state carries experience, and how the model participates in belief revision.**

**English** · [简体中文](ARCHITECTURE.zh-CN.md)

> Status checked 2026-11-05. Local main includes the J-series and folder-instance A/B/C. This document distinguishes current code, scoped empirical evidence and future design. Windows installation has been exercised locally; genuinely clean-machine installation, signing and a formal release are not established.
>
> Status terms: **Implemented** = present in the current code and covered by tests; **Approved design** = design accepted, not implemented; **Planned** = not yet delivered; **Research direction** = long-term inquiry, not an engineering commitment.

[Project overview](README.md) · [Roadmap](ROADMAP.md)

## 1. Starting point: state across model calls

PCS (Persistent Cognitive System) is QUINTESSENCE's overall architectural identity. A model call can end, while the system's experiences, current beliefs, version history, and unfinished work continue to exist.

The model participates in inference based on explicitly supplied context and emits candidates or answers. Updates to persistent beliefs are determined by PCS domain rules and commit mechanisms. This makes it possible to study how system state accumulates and changes while model parameters remain fixed; whether state actually updates and whether behavior actually changes still require separate verification.

| Object | Ownership and responsibility |
|---|---|
| PCS | Authoritative cognitive state, provenance, versions, domain validation, and formal Context |
| LLM | Inference and candidate generation; cannot self-certify tool success, source trust, or authorization |
| Host (e.g., DSH; not yet integrated) | Session, runtime environment, real tool execution, and host-side authorization |
| Application | User input, result presentation, and interaction; consumes the unified interface |
| Private instance | Individual configuration and persistent experience; not bundled with general source code or model weights as one asset |

Model replaceability is an architectural principle; current work does not promise behavioral equivalence or lossless migration between arbitrary models.

## 2. Existing foundation

The current core has persistent storage, version updates, Context assembly, model ports, and Belief domain operations. The names below describe components in the current technical report; they are not stable API commitments for the public package.

| Component | Current responsibility |
|---|---|
| State Store / commit transactions | Stores state and history; performs controlled appends, version checks, and commit receipts |
| Domain services / BeliefService | Validates belief operations, evidence, and scope; applies legal changes |
| ContextService | Loads and assembles formal context; binds material, versions, and a state snapshot |
| ModelPort / DirectHttpModel | Model-call abstraction and the existing real HTTP call path |
| Proposal parsing and validation | Parses model output; rejects unsupported fields and illegal operations |
| CognitionRuntime.run_step | Orchestrates the existing bounded cognitive step, validation feedback, and limited retries |

### Experience, evidence, and belief

These concepts cannot be treated as interchangeable merely because their text looks similar:

| Concept | Meaning and boundary |
|---|---|
| Event | A record of an input or observation that occurred; trust depends on its source and cannot be granted by a model's self-report |
| Evidence | A source used by a judgment that satisfies the relevant rules; citing the same source again does not create independent evidence |
| Episode | An organizational form of experience; currently only a minimal foundation, not full memory organization or semantic retrieval |
| Belief | A belief with provenance, applicability scope, status, and version; it can be retracted or superseded |
| Knowledge | Deterministic belief queries/working sets exist; fuller Knowledge projection/research remains future work, not a separate fact store |

User statements, model inferences, and actual tool observations retain different source semantics. Storing a sentence does not automatically make it a fact or a Belief.

### Current core cognitive step

The diagram describes the responsibility relationships of the existing core step. The formal v4 experiment and the legacy Workbench have their own orchestration, described in the next section.

```mermaid
flowchart TD
    E["Trusted events and pending work"] --> C["ContextService assembles current material"]
    S["Persistent state and version history"] --> C
    C --> M["Model generates candidates"]
    M --> V["Parsing, domain validation, and commit checks"]
    V -->|legal update| S
    V --> O["Receipt, WAIT, or rejection"]
```

A typical revision path includes retracting an earlier judgment, reading the updated state and building a new Context, and proposing a replacement belief. The two operations are not a single whole that can be skipped by a verbal promise; if the second step fails or WAITs, the system must not report that the replacement belief has been established.

Existing validation and bounded feedback constrain model operations, but they cannot guarantee that the model triggers the correct operation every time, nor prove open-world conclusions true.

## 3. Current call paths

| Path | Actual situation | Boundary |
|---|---|---|
| PCS Core | Local closed loop of Context, model call, candidate parsing, and domain commit | Bounded cognitive operation; not a complete open-ended conversation protocol |
| EXP-01 v4 | Q1/Q2 use formal Context and some core mechanisms, orchestrated by the experiment adapter; Advice uses a separate call branch | Controlled research; not every step goes through run_step or the unified collaboration layer |
| Current desktop chat | ConversationOrchestrator / collab.v2: content, tools, SDRPE and governed cognitive proposals share formal Context and call records | Implemented; expression has no tool authority and chat-based state changes still require Cognition rules/receipts |
| P0 desktop collaboration | Windows Consult/Review client, trusted sidecar and the formal consult/review Facades | Implemented and merged to `main`; controlled real-model C13b evidence; unsigned development build, not a formal release |
| Workbench V0.2 | The prototype assembles some prompts, retrieval, and HTTP itself and reuses some PCS capabilities | Standalone demo; not unified into the core interface |

The legacy Workbench is frozen and will not be reused or migrated into the new application. Its operating experience clarified the need for a unified collaboration layer; the P0 implementation is separate from that demo.

Specific request formats required by the old experiments remain within the original protocol scope. The P0 collaboration layer uses explicit, versioned multi-message formats without silently changing historical experiment contracts.

## 4. Implemented P0: unified LLM–PCS collaboration layer

**Status: implemented and merged to `main`; release and signing are separate decisions.** The implementation uses a thin coordination entry point (Facade) inside the PCS library and extracts existing generation and commit steps for application reuse. It does not create a second authoritative cognitive state and does not add a resident cognitive server.

The target relationships are:

```mermaid
flowchart TD
    H["Application and trusted host"] --> F["Collaboration entry: consult / review"]
    F --> C["Single ContextService"]
    S["PCS domain state and sources"] --> C
    C --> M["Shared model adapter and call records"]
    M --> F
    F --> V["Candidate validation, permission, and domain commit"]
    V --> S
    F --> R["Reply, candidates, and authoritative receipt"]
```

### Two interactions, separate update responsibilities

| Mode | Purpose | State impact |
|---|---|---|
| consult | Consultation, query, and answer over currently accessible material | May store audit records; does not create cognitive work, write Belief, or refresh persistent SelfView |
| review | Review beliefs against events and pending work; propose candidates | After generation, commit according to permissions and domain rules; may also wait, reject, or fail |

consult does not automatically become review just because the model says “this should be remembered.” In review, candidate generation, structural validity, candidate approval, and successful commit are distinct stages. Natural-language UI text cannot override actual system results.

### Generation and commit are separate

A candidate is bound to its original Context, work, content, and a stable request identifier. Confirmation re-checks whether the candidate is still valid; an old candidate cannot be silently rebound to a new state.

Rejection is terminal for that candidate and blocks automatic reruns of the corresponding work. Restart, a new session, or a new request identifier cannot lift the rejection; reconsideration requires explicit authorization and produces a new candidate. If a commit has already succeeded, a later rejection cannot pretend to undo it; retracting the belief requires a separate domain operation.

Commit facts are determined by the authoritative receipt. The goal is to recover results when a commit succeeds but the response is lost, avoiding duplicate appends; this is a bounded engineering mechanism, not an exactly-once guarantee across arbitrary external systems.

### Queries must return to formal Context

The collaboration layer's deterministic query path reads permitted beliefs, versions, and applicability information. Query results are assembled through the same ContextService before being given to the model; applications must not assemble their own hidden cognitive material.

First-phase interactions have explicit call limits, and queries and repair share a budget; they do not recursively expand into an arbitrary Agent Loop. No result, WAIT, rejection, invalidation, and call failure are each reported separately, with no hidden background retry.

## 5. Persona, Memory, Cognition and collaboration experience

| Domain | Actual ownership and responsibility |
|---|---|
| Persona | Reviewed profiles, expression policy, examples and controlled activation; expression does not rewrite evidence or beliefs |
| Memory | The external MIT project [Scope Recall](https://github.com/410979729/scope-recall-hermes) supplies the memory engine, event persistence and retrieval; these are upstream contributions |
| Cognition | QUINTESSENCE's source-linked beliefs, revision, support/impact governance, review and domain commits; retrieved text is not automatically a fact |
| Experience / conversations | PCS inputs, answers, tool results, turns and processing events retain exact sources; transcript search and Memory search are separate readers |

ExperienceReadBridge / MemoryProjectionService deliver eligible recorded interactions to Scope Recall under retention and authorization rules, with delivery state, stable source keys and receipts. The adapter uses upstream public capture/search/recall/time-window APIs; the host owns binding, scope, relocation, budgets and timeouts. Scope Recall's storage, Episode and retrieval implementation remain upstream work. QUINTESSENCE implements the collaboration, projection/delivery and management adaptation, not that external engine; the upstream repository is not vendored here.

Delivery creates searchable raw events. Enqueued semantic consolidation/embedding is not proof of completed processing: the desktop adapter does not automatically configure and drive the full upstream worker stack. Chat-based Cognition proposals/review are a separate mechanism. Provenance, version, role and derived status remain distinct; rule-based Review is not a proof of open-world semantic truth.

### SDRPE: two-stage expression

When enabled, ConversationOrchestrator first runs the content stage with formal Context, queries/permitted tools and governed cognitive proposals, then a separate expression stage using the turn's bound Persona and expression policy. Expression has no tool authority and cannot turn a wording change into a state commit or execution receipt. Turns, stages, messages and results are persisted separately; failure, skip and fallback use their actual states. The existing single-stage path remains when disabled. A missing custom Persona uses the model's default style.

### Cognition governance and bounded maintenance

The model proposes; the host/domain layer checks versions, scope, authorization, source/support and impact. Higher-impact candidates or those outside automatic-commit conditions require human review; every successful update still requires a formal receipt. Decisions are append-only history: current status uses the candidate and latest applicable decision, not a count of historical awaiting-human rows. Existing bounded maintenance slices, repair feedback and review linkage are foundations for Dreaming, not complete unattended growth.

## 6. Authorization, deletion, and persistence boundaries

Existing core checks for evidence, versions, watermarks, and deletion remain in place; P0 extends source registration, deletion propagation, and permission enforcement points for inputs and outputs. Those mechanisms are implemented on `main`; release status is separate.

The first-phase trust model is a **single-user trusted host**. The host establishes call identity and valid authorization; namespace is used for logical isolation, scope expresses applicability, and authorization references support audit. None of these is authentication by itself.

- Reading an instance, sending private content outward, automatic commit, memory integration, and rerun after rejection are authorized separately.
- Permission to send content to a model does not imply permission to modify beliefs; permission to commit does not imply permission to send private content outward.
- Current permission is checked before every model send and retry; the model cannot create authorization in its output.
- Deletion first establishes an unreadable barrier, then cleans registered derived content; old Contexts and dependent material should become invalid.
- Raw request records support review but are constrained by privacy and retention policies; after deletion, full text is not guaranteed to be recoverable.

This design does not defend against malicious same-process code operating on the underlying storage directly, and it is not a multi-tenant network-service security design.

### Instance-owned state and lifecycle

`instance.json` holds stable identity, component state and registration basis; `instance-host.json` holds non-secret host configuration. The PCS database owns Persona versions, conversations, Cognition, sources, decisions and delivery state. The Scope Recall database, deletion ledger, Anchor, private UI preferences and registration audit also belong to the instance folder. Cognition already lives in PCS, so no empty cosmetic component is created. Model services, generic runtime dependencies and authorization credentials may remain external; they are not irreplaceable private instance state.

A initializes real components from an empty folder under durable pending/initializing/ready states. B cold-copies a complete folder, preserves identity and assets, and audits path rebinding. C preserves existing content and completes only known-never-created components or noncritical README files. Unknown legacy history fails closed until a separate operator declaration establishes never-created status. Ready/lost, interrupted, damaged, remnant-bearing or identity/config-loss cases cannot be replaced with blank state. Existing path confinement, version checks, single-writer locks, idempotency and recovery checks remain. GUI selection is persisted and the host restarted only after preparation succeeds; active work blocks switching.

Recovery assets travel with the folder. Ledger integrity, watermarks, deletion replay and surviving-witness checks remain; joint rollback of database, ledger and Anchor cannot be reliably detected. A legacy folder lacking safety records needs a separately audited baseline adoption, recording pre-baseline deletions as unprovable; this does not replace component-history confirmation. PCS deletion does not yet physically erase content already delivered to Scope Recall. See [instance operations](docs/desktop/INSTANCE-FOLDERS.md).

## 7. Long-term position of the five capabilities

| Capability | Research responsibility | Current implementation scope |
|---|---|---|
| Persona | Continuity of identity and dispositions; contextual feedback | Profile publication/activation, policy, examples and SDRPE; deeper contextual evolution pending |
| Memory | Retention, organization, selection, and reuse of experience | Scope Recall delivery/browse/recall integrated; full semantic workers and cross-module deletion pending |
| Cognition | Form and revise beliefs; carry questions and focus forward | Belief / Context, source governance and persistent OpenQuestion basics implemented; fuller Focus/growth pending |
| Affect | Source-bearing evaluation of objects and experiences | Research direction |
| Load & Recovery | Activity load, stopping, and recovery rhythm | Long-term mechanism pending; current call budgets are not a complete load system |

The five are conceptual responsibility divisions; whether they become separate modules depends on implementation needs. Shared state and provenance do not imply that every domain uses the same update rules.

Long-term triggers for cognitive activity may include accumulated experience, state changes, unresolved problems, and persistent focus. Current chat-triggered and configuration-controlled maintenance entry points are bounded; they do not constitute complete Dreaming or autonomous continuous operation.

## 8. How to validate the architecture

Engineering acceptance checks channels and constraints: candidates are not committed early, repeated confirmation does not duplicate updates, rejection does not trigger automatic rerun, restart is recoverable, and deletion and permission changes can block reuse of old material.

Real-model research separately checks content and behavior: whether the right material is selected, whether the actual request contains the correct version, whether the output uses the applicable scope correctly, and whether judgments in new situations are reasonably affected. Citing a record or claiming to “remember” is not a substitute for behavioral evidence.

The P0 acceptance sequence uses post-revision restart and chat-cache clearing to separate the effect of persistent state from that of chat context, with controls for no revision, no relevant history, and a temporary equivalent summary. Performance similar to an equivalent summary does not negate the value of maintaining and revising long-term state.

These are P0 acceptance conditions; they do not establish PCS2-010 research results or human evaluation. Current experiment boundaries and next steps are in [ROADMAP.md](ROADMAP.md).
