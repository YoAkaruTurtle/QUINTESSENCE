<h1 align="center">QUINTESSENCE</h1>

<p align="center"><strong>Persistent Cognitive System (PCS)</strong></p>

<p align="center"><em>Preserve · Participate · Change</em></p>

<p align="center">
  <img alt="PCS: Research" src="https://img.shields.io/badge/PCS-Research-8B1E24?style=flat-square&amp;labelColor=181818">
  <img alt="Status: Early Research" src="https://img.shields.io/badge/Status-Early%20Research-555555?style=flat-square&amp;labelColor=181818">
  <img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache--2.0-8B1E24?style=flat-square&amp;labelColor=181818">
  <img alt="Python 3.12+" src="https://img.shields.io/badge/Python-3.12%2B-555555?style=flat-square&amp;labelColor=181818">
  <img alt="Windows Tested" src="https://img.shields.io/badge/Windows-Tested-555555?style=flat-square&amp;labelColor=181818">
</p>

<p align="center">
  <strong>English</strong> · <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="ARCHITECTURE.md">Architecture</a> ·
  <a href="ROADMAP.md">Roadmap</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

QUINTESSENCE is an open-source research and engineering project exploring how AI systems can preserve, revise, and develop cognitive state across interactions — beyond the lifespan of any single language-model call. It keeps persistent state, evidence provenance, and belief revision outside the model, while a replaceable language model participates in inference.

A Persistent Cognitive System keeps experiences, current beliefs, provenance, versions, and unfinished work across calls, and treats model output as a candidate subject to domain rules rather than as directly authoritative state. This differs from a conventional LLM memory layer, which mainly stores and retrieves text for prompts: PCS adds explicit evidence relationships, scoped beliefs, versioned retraction and replacement, formal Context assembly, and commit and audit boundaries. Retrieval is one part; the system also governs whether a proposed belief may become persistent state.

> **Early research stage. The GitHub repository is public.** Status checked 2026-10-21. The M1–M6 phases (migration, licensing, formal mainline, and Public repository) are complete. The cognitive substrate and the controlled revision chain have implementations and real-model run records. The unified LLM–PCS collaboration layer is not implemented. Design approved; implementation pending. Human evaluation of the formal experiment is still pending.

## Why this matters

A single conversation can look intelligent, but what happens after it ends? Can what happened become evidence for the next understanding? When new evidence overturns an earlier judgment, can the system retain the reason for the revision and later use the appropriate version? Further, can experience gradually form persistent questions, unresolved problems, and stable but evolving individual characteristics?

QUINTESSENCE studies continuity through three stages:

- **Preserve**: experiences and beliefs survive the end of a call, with provenance, time, and version.
- **Participate**: state that is relevant, still valid, and permitted enters the inference context.
- **Change**: new evidence can revise existing beliefs. Whether those revisions later change behavior in new situations must be tested separately.

The long-term goal is a system with identity continuity, memory, cognition, evaluation, and operational rhythm. Current work starts from verifiable belief revision; full autonomous persistent cognition is not yet implemented.

## Where the project stands

| Capability | Current status | Scope |
|---|---|---|
| Persistent state and provenance | Implemented foundation | Events, record versions, evidence links, and controlled state updates |
| Belief and formal Context | Implemented foundation | Scoped beliefs, retraction and supersession, and canonical model-facing context |
| Model participation in belief revision | Real-run records exist | Candidate, validation feedback, and persistent revision in controlled cases; success rate and behavioral effect remain limited |
| Unified consult and cognitive review | Design approved, not implemented | Organize existing capabilities into a reusable LLM–PCS collaboration layer |
| Cognitive Workbench V0.2 | Experimental prototype | Standalone demo with bespoke orchestration; not the delivered standard chat interface of the core package |
| Knowledge, Question / Focus, contextual persona feedback | Planned | To be integrated with unified state and the collaboration path |
| Affect, Load & Recovery, long-term development | Research direction | No complete engineering capability yet |

### A concrete example: revising “this tool does not work”

In a synthetic scenario, a tool fails on a class of paths. The system stores a judgment with evidence and scope. Later evidence appears: the failure was permission-related, and a retest under specified conditions succeeded.

During revision, the model proposes a candidate; PCS checks whether the operation and evidence satisfy the rules. After the old judgment is retracted, the system assembles a new Context and forms a replacement belief with a narrower scope. Version history retains why the change happened. If the second step fails or waits, the system does not pretend that the replacement belief has been established.

**The persistent revision chain is implemented and has actually run.** It is not yet proof that a revised belief will reliably produce better advice in arbitrary new situations.

### How progress is judged

Engineering tests check state updates, versions, validation, and recovery mechanisms. Real-model experiments check whether the model can trigger and correctly use those mechanisms. The two kinds of evidence are reported separately.

The PCS2-010 engineering work is closed. The formal EXP-01 v4 study ran six times: one run completed belief formation, revision, and the later Advice stage; three did not trigger a valid initial operation; two produced invalid operations during revision. Human blind review and final research conclusions remain pending. The earlier five exploratory rounds are a different batch and are not pooled with v4.

The current mainline's engineering acceptance record includes **509 regression tests passing and successful wheel / sdist builds**. This shows that the mainline passes the corresponding engineering checks; it does not prove stable cognitive transfer.

## How the system is organized

**PCS holds cognitive state; the model participates in inference; the host performs execution.**

| Part | Responsibility |
|---|---|
| QUINTESSENCE / PCS | Preserve and revise beliefs; manage provenance, versions, and formal context |
| Language model | Answer or propose candidates from the supplied material; model output must pass rule processing before becoming persistent belief |
| Host and application | Provide interaction, authorization, and tool-execution environments; consume PCS through an integration interface |
| Private instance | Store an individual's persona configuration, memories, and persistent state; kept separate from the general source code |

Chitose (千岁) is a private instance in this research. The public project provides general mechanisms and does not include her private experiences or persona assets. Hosts such as DSH are future integration targets; they are not yet integrated and are neither the whole of QUINTESSENCE nor a core runtime dependency.

The five conceptual capabilities in the architecture are Persona, Memory, Cognition, Affect, and Load & Recovery. They describe long-term responsibilities; they do not imply five mature services. See [ARCHITECTURE.md](ARCHITECTURE.md).

## Getting started

The GitHub repository is public. There is no confirmed public release package or installation entry point yet. This document does not provide unverified installation commands, and the standalone Workbench demo is not the core package's default feature.

### Source-tree checks (Windows / Python 3.12)

Python 3.12+ is required. The current verification environment is Windows; Linux has not been verified. From the repository root:

```powershell
python tools/run_tests.py
python tools/check_repository.py
```

`run_tests.py` uses synthetic data and a loopback fake provider, and writes test output to a temporary directory outside the checkout. `check_repository.py` checks the path allowlist, sensitive patterns, and reachable Git history. There is no confirmed installation or command-line entry point; these commands are only for source-tree development checks.

Release packaging will add versioned installation instructions, dependencies, and synthetic examples. Linux verification remains future work; current engineering acceptance does not extend to a cross-platform support claim.

## Next step

The next foundation is the **unified LLM–PCS collaboration layer**: connect consultation, query, belief review, candidate confirmation, and result recovery to one formal mechanism. Knowledge, Question / Focus, persona-context feedback, and host integration follow.

One key validation: after revising a belief, close the process, clear the chat cache, and ask a related question. Can the system still retrieve the correct version from persistent state and handle new applicability conditions appropriately? See [ROADMAP.md](ROADMAP.md).

## Development and contributions

The project is independently maintained and uses **AI-assisted code, tests, and documentation**; the maintainer owns direction, trade-offs, and delivery, and AI output is reviewed and verified. AI-assisted contributions should disclose their use and follow the project's contribution rules.

Future development of the general PCS uses this repository as the sole authoritative source mainline. The legacy research repository, experiment worktrees, and sealed materials are preserved as historical and research sources only; they are not the code authority for the general PCS.

Contributions are welcome around concrete problems, minimal reproductions, synthetic tests, and well-defined capability gaps. See [CONTRIBUTING.md](CONTRIBUTING.md). Do not submit private conversations, instance databases, persona profiles, keys, or other sensitive data. See [MIGRATION-BOUNDARY.md](docs/development/MIGRATION-BOUNDARY.md) for the maintenance boundary.

## License and data

General first-party QUINTESSENCE code is licensed under the **Apache License 2.0**; see [LICENSE](LICENSE) and [NOTICE](NOTICE). Any future host-adapter code will be handled according to its source and license; the full DSH upstream repository is not published as first-party code here.

Copyright 2026 Linguist.

Private memories, cognitive traces, persona configuration, personalized training data, and weights are not part of the default open-source scope. The project opens general mechanisms so that each individual's accumulated state remains under its holder's control.
