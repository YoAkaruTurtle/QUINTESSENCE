# Implementation status

One entry point for the P0 LLM–PCS collaboration-layer implementation. It
records the approved scope, baseline, implementation state and evidence. Design
plans and contracts remain the authority; this file does not widen either.

## Authorized scope

- Design authority: the approved LLM–PCS collaboration implementation plan
  (2026-10-21).
- The project owner approved implementation in card order C01 → C13 on
  2026-10-22. That approval does not itself authorize publishing, release
  distribution, private content disclosure, or work outside the plan.
- Frozen and out of scope: the legacy Workbench V0.2 demo, PCS2-010 closure
  material, and release packaging/signing.

## Current P0 review line

- Branch: `codex/c13b-exp-01`
- Base: `fda2dc4` (`main` at the start of the P0 card sequence)
- Current HEAD: the review branch tip; code evidence commit `3037cf2`, followed by docs/status commits.
- `main` is an ancestor; the branch is 36 commits ahead and 0 behind.
- The working tree is clean at the time of this status entry.
- The branch has not been merged to `main` and nothing has been pushed by this
  implementation line.

C01–C13b are implemented on this line. C13b includes the Windows Consult/Review
client and a controlled synthetic real-model experiment. Merge review, release
packaging/signing and human research evaluation remain separate decisions.

## Evidence

- The P0 review suite currently records **972 Python tests passing with 3
  skipped**, plus frontend, Rust, packaged-sidecar smoke and repository-gate
  checks. Test logs are written outside the checkout and are not committed.
- The C13b experiment records a formal cyan Belief, formal retraction, a new
  red replacement record, deterministic C10 retrieval, and a plain-language
  real-model query using the replacement after the owner's client restart.
- The durable experiment database is Lab data outside this repository; it is not
  a source asset and is not committed.
- The C07 claim-freeze preflight and C09 query policy are covered by regression
  tests and the C13b records.

## Card state

| Card group | State | Evidence |
|---|---|---|
| C01–C07 foundation/review | Implemented | contracts, domain/store tests, Review Facade tests |
| C08–C11 consult/query | Implemented | Context/query/C11 tests and C13b client records |
| C12a–C12c design and desktop | Implemented | Windows client, bridge and frontend tests |
| C13a bridge lifecycle | Implemented | C13a lifecycle tests and packaged checks |
| C13b real integration | Implemented; owner experiment recorded | C13b fixes, 972-test run, synthetic real-model records |

Card implementation does not equal final owner acceptance or a public release.

## Blockers

No known code-merge blocker remains after the P0 review fixes. External release
distribution is still blocked separately by the absence of release signing, a
clean-machine installation acceptance, and formal release packaging. Human
research evaluation is still pending.

## Next

Merge review, then publication preparation. PCS2-011 Knowledge and later
capabilities remain subsequent work.
