# Implementation status

One entry point for the LLM–PCS collaboration-layer work. It records approved
scope, the baseline, card states and evidence. The design plan and the
contracts remain the authority; this file never widens either. Update it when a
card is dispatched or accepted.

## Authorized scope

- Design authority:
  `QUINTESSENCE-LLM-PCS-COLLABORATION-IMPLEMENTATION-PLAN-v1.0.md`
  (2026-10-21, `DESIGN_REVIEW_READY`).
- The project owner approved implementation on 2026-10-22 in the plan's card
  order (C01 → C13). That approval does not cover real-model calls, sending
  private content, publishing changes, or any scope the plan does not name.
- Frozen and out of scope: the original Workbench V0.2 demo (kept outside this
  repository), the 010 closure material and release packaging.

## Baseline

- First card base commit: `fda2dc4` (2026-10-22). The plan cites `1b7e8ab`;
  between the two only documentation, README assets, ignore rules and
  repository tools changed. No file under `src/` or `tests/` differs.
- Test baseline at `fda2dc4`: `python tools/run_tests.py` → 509 tests, OK,
  0 failures, 0 errors, 105.9 s.

## Cards

| Card | State | Evidence |
|---|---|---|
| C01 — SelfView refresh switch | AWAITING_REVIEW | commit `9d11d10`; 512 tests, OK (3 new) |
| C02 — shared parse, validation and repair | NOT_STARTED | — |
| C03 — candidate generation and commit separation | NOT_STARTED | — |
| C04 — read-only command receipt lookup | NOT_STARTED | — |
| C05a — joint source/deletion/Context contract | NOT_STARTED | — |
| C05b–C05d, C06a–C06c, C07–C13 | NOT_STARTED | — |

`AWAITING_REVIEW` is a local extension of the plan's card states: the
implementation and its tests are complete, and the owner has neither accepted
nor rejected the card. It is not `ACCEPTED`.

## Evidence

- `tools/run_tests.py` writes its log to an OS temporary directory, outside the
  checkout; logs are not committed. Each card records its command, counts and
  result commit here and in the commit message.
- C01 evidence: `tests/domain/test_self_view.py` Context section (default
  refresh, skipped refresh, boolean guard, deleted-trigger/missing-profile
  failures under the flag) plus the full run above (512 tests, OK).
- Design documents live outside this repository. Only code, contracts and
  synthetic tests are kept here.

## Blockers

None.

## Next

C02, after C01 acceptance.
