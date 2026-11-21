# Implementation status

One entry point for the mainline implementation state. It records the approved
scope, baseline, implementation state and evidence. The approved design plans
and contracts define the implementation scope.

## Desktop Dreaming and Scope Recall integration (2026-11-21)

The Dreaming and Scope Recall integration is on `main` at `16aaa1a`,
following the J-series and folder-instance A/B/C implementation.

- Registered, ready folder instances initialize Dreaming additively. The
  original instance files are preserved and new runtime state belongs to the
  selected private instance. The floating status window can be shown or hidden
  independently of Dreaming. Foreground work takes priority over Dreaming model
  requests. Rolling 24-hour continuity remains available independently of
  permission for idle reflection. [Dreaming and continuity acceptance](../desktop-dreaming-continuity/acceptance.md)
- Calendar journals cover the previous local day, Monday-to-Sunday week,
  month, and year. A running day can produce a diary without chat. A day
  without an observed run is skipped. The calendar writer uses the active
  persona, period evidence, and existing PCS read tools to follow relevant
  earlier experiences. The journal model grant is separate from untasked
  reflection, and automatic maintenance retains its own controls. Ordinary
  Consult can recall journals by time and checks their exact source lineage
  before delivery. Rebuilding a journal after a source invalidates it is
  pending. [Calendar journal acceptance](../desktop-dreaming-calendar-journals/acceptance.md)
- Scope Recall 3.14.1 at upstream revision `2b640260` is vendored in the checkout.
  The development setup installs the pinned runtime and the standard Windows
  sidecar build packages it. The desktop uses lexical retrieval and can use
  semantic retrieval with an explicitly configured, metered embedding route.
  PCS, which checks instance, scope, source revision, and availability, controls
  delivery to the model. [Scope Recall source, setup, and integration evidence](../desktop-dreaming-calendar-journals/main-integration.md)
- Independent full-suite acceptance recorded 2,708 tests, with 2,702 passes,
  six skips, and zero failures. The default Windows sidecar build completed,
  all three packaged smoke tests passed, and the frozen sidecar captured and
  searched synthetic memory with the ambient `PYTHONPATH` cleared.
  Clean-machine installation, signing, and a formal binary release remain
  pending. [Integrated verification record](../desktop-dreaming-calendar-journals/main-integration.md#candidate-verification)

The entries below preserve earlier implementation and verification snapshots.

## Publication snapshot (2026-11-05)

The publication snapshot includes the complete J-series and folder-instance
A/B/C implementation. The pre-consolidation engineering baseline executed
2614 offline tests with 9 skips;
frontend 166, Rust 70 and exact installed-sidecar checks were separately run.
These are scoped engineering records, not a human research conclusion.

Windows packaging/local installation, folder GUI creation/restore/safe completion,
real-model persistence/recall and Cognition source/review paths have been exercised.
Genuinely clean-machine installation, signing and a formal binary release remain
pending. Scope Recall 3.14.1 is the external Memory engine; the host adapter does
not configure/drive its complete semantic/embedding workers. PCS deletion does
not yet remove delivered Memory content. Folder-local PS06 cannot reliably detect
joint rollback of all safety assets.

The clean publication candidate is rooted at the existing public main without
old development commits as ancestors. After approved adoption, public main,
local main and every new branch baseline use that clean history. Old worktrees
and branch names below are historical context only, never development bases.
No push, tag or Release is implied; current-candidate verification is separate
from the historical counts recorded here.

## Historical scope authorization

- Design authority: the approved LLM–PCS collaboration implementation plan
  (2026-10-21).
- The project owner approved implementation in card order C01 → C13 on
  2026-10-22. That approval does not itself authorize publishing, release
  distribution, private content disclosure, or work outside the plan.
- Frozen and out of scope: the legacy Workbench V0.2 demo, PCS2-010 closure
  material, and release packaging/signing.

## Historical P0 publication baseline

- Branch: `main`; HEAD `13c3589` (`docs: align P0 status and add third-party
  notices`).
- `origin/main` points at the same commit; the repository has no release tag.
- The working tree is clean at the time of this status entry.
- P0 is merged into `main`. The former review branch `codex/c13b-exp-01` points
  at the same commit.

C01–C13b are implemented on `main`. C13b includes the Windows Consult/Review
client and a controlled synthetic real-model experiment. Release
packaging/signing and human research evaluation remain separate decisions.

## Evidence

- The full offline Python suite was run on 2026-10-25 for the J01 delivery:
  991 tests, 988 passed, 3 skipped, 0 failed, 0 errors. The count is 19 above
  the 972-test baseline because J01 adds the availability contract tests. Test
  logs are written outside the checkout and are not committed.
- The C13b experiment records a formal cyan Belief, formal retraction, a new
  red replacement record, deterministic C10 retrieval, and a plain-language
  real-model query using the replacement after the owner's client restart.
- The durable experiment database is Lab data outside this repository; it is not
  a source asset and is not committed.
- The C07 claim-freeze preflight and C09 query policy are covered by regression
  tests and the C13b records.
- J01 adds one shared availability rule for Belief delivery, its outlet
  coverage, and a read-only re-evaluation helper. See
  [J01-AVAILABILITY.md](J01-AVAILABILITY.md).

## Card state

| Card group | State | Evidence |
|---|---|---|
| C01–C07 foundation/review | Implemented | contracts, domain/store tests, Review Facade tests |
| C08–C11 consult/query | Implemented | Context/query/C11 tests and C13b client records |
| C12a–C12c design and desktop | Implemented | Windows client, bridge and frontend tests |
| C13a bridge lifecycle | Implemented | C13a lifecycle tests and packaged checks |
| C13b real integration | Implemented; owner experiment recorded | C13b fixes, full offline suite run, synthetic real-model records |
| J01 delivery availability | Implemented | availability contract tests, query policy v2, outlet coverage |

Card implementation does not equal final owner acceptance or a public release.

## P0 release blockers

No known code-merge blocker remains after the P0 merge. External release
distribution is still blocked separately by the absence of release signing, a
clean-machine installation acceptance, and formal release packaging. Human
research evaluation is still pending.

## Work planned after P0

The Memory–Cognition minimum loop (J01–J11). PCS2-011 Knowledge and later
capabilities remain subsequent work.
