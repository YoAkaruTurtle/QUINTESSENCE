# Memory integration v1 contracts (J02 D1)

Status: **D1 complete; D2 blocked**. The nine contracts are frozen under
[`contracts/memory-integration/v1/`](../../contracts/memory-integration/v1/README.md);
this page records the version evidence, the acceptance outcome and what remains
blocked.

## Revision after second-level review (2026-10-25)

Three contract issues were corrected without changing the acceptance outcome:

1. **Source identity versus version.** `02-source-mapping.md` now derives one
   `source_event_key` per immutable PCS `(ref, version)`, so a version update
   yields a different key. The four version axes (PCS ref version, PCS
   `record_version`, upstream `source_revision`, delivery `attempt`) are named
   and never equated. `04-root-source-dedup.md` separates the projection
   version identity from the evidence root identity, states which copies add no
   support vote, and distinguishes deleting one version from deleting the whole
   root.
2. **Retry by operation.** The capture retry rule no longer generalises to
   other calls: deletion `apply` is never automatically replayed (unknown
   outcome keeps the barrier and becomes `needs_review`), read queries may only
   retry bounded, and `VERSION_CONFLICT` has two explicit readings (stale
   preview versus unknown earlier outcome).
3. **Enum evidence classes.** `enums.json` now separates measured values,
   source-declared values, PCS contract values and proposed values. `conflict`
   is recorded as measured, `durability = "unknown"` as source-declared, and
   `receipt_result` as a proposal only.

D1 is unchanged; AC-07 and AC-16 remain blocked on the missing public receipt
interface, and D2 is not declared.

## J02b — receipt interface in a local patch (2026-10-25, revised)

The public deletion receipt now exists in a **local patch**, not upstream:

| State | Receipt interface |
|---|---|
| original `3.14.1` at `2b64026` | absent (unchanged fact) |
| local patch `629f4294` (first), head `446bfef8`, branch `pcs/receipt-v1` | present and tested |
| upstream accepted / released | not yet |

`MemoryManagement.receipt(preview_id, *, expected_instance=None)` reads one
committed action and its operations. `apply()` records the action-to-operation
link in the same write transaction. Five outcomes stay distinct: `found`
(returns the view), `no_record`, `incomplete_link`, `authority_mismatch`,
`query_failed`. The read never deletes, purges, replans, repairs or writes;
cleaning progress is data inside `found`, not a sixth outcome. The schema step
is 1112 → 1113 with one nullable column; historical rows are not backfilled and
answer `incomplete_link`.

The revision `446bfef8` (second-level review) changed three things:

1. **Link completeness is bound to the action.** A delete/suppress action with
   an empty link, duplicate ids, a missing operation row, or operation groups
   that do not match the action's own target groups is `incomplete_link`; a
   truncated multi-operation link fails even when every listed id resolves.
   Non-deletion actions (revise, unmute, restore) may legitimately have none.
2. **Authorization before link parsing.** Targets first, then each operation's
   scope before its progress; an unauthorized action answers `authority_mismatch`
   even when its link or layers are damaged, and unparseable targets fail closed
   rather than guessing authority.
3. **The migration failure test injects after the writes.** It runs the real
   `upgrade_1112` and then raises, so the column and both version markers are
   already written; the rollback is verified and the same store upgrades once
   the fault is removed.

Verification on the patch baseline (raw output in the J02b Lab folder):

- 19 receipt tests pass: transaction rollback, restart recovery, empty /
  duplicate / truncated / missing links, non-deletion action without
  operations, authorization ordering, migration, read-only and progress.
- Upstream regression: deletion, audit, migration and the management/admin,
  CLI and export suites pass (176); the `tests/contract` subset that fails also
  fails on the unpatched baseline (native vector backend and group-publish
  fixtures), so there is **no new regression**.
- Old code on an upgraded store: the original 3.14.1 opens a 1113 store and is
  **rejected** with `SCHEMA_UNSUPPORTED`; the store is unchanged. Not
  forward-compatible, recorded as a limitation.

## J02 D2 — closed on the local patch baseline

D2's rule is the one already in the J02 card (§1.4): **AC-16 PASS and the AC-07
recovery branch PASS**. Nothing was added for this closure.

| Condition | Evidence | Result |
|---|---|---|
| D1 reached | see the D1 record below | PASS |
| AC-16 public receipt interface | patch head `446bfef8`: `MemoryManagement.receipt(preview_id, *, expected_instance=None)`; 19 receipt tests; five outcomes; transaction link; migration 1112→1113 | PASS **on the local patch** |
| AC-07 recovery branch | `test_receipt_after_response_loss_and_restart`: response lost, restart, confirm with pre-stored instance identity + `preview_id`, never replaying `apply` | PASS **on the local patch** |

**J02 D2 PASS — applicable baseline: local patch `446bfef8`** (series
`2b64026` → `629f4294` → `446bfef8`). This is not upstream acceptance or
release, it does not verify real purge backends, and it does not start an
adapter. The original `3.14.1` pin still lacks the interface; its BLOCKED record
below is retained as history.

## Upstream pin

| Item | Value | How verified |
|---|---|---|
| Distribution | `hermes-scope-recall` | `pyproject.toml` `[project] name` |
| Version | `3.14.1` | `_version.py`; `python -c "from scope_recall._version import __version__"` |
| Commit | `2b640260134565ac7fef3a76644e352a50866533` | `git rev-parse HEAD` in the fixed checkout, branch `main`, clean |
| Licence | MIT | `LICENSE`, package metadata |
| Python | `>=3.11,<3.15` | `pyproject.toml` |

The fixed checkout is a git working tree at that commit. It was not modified by
this card; no install, upgrade or patch was performed.

## Environment (reused, not rebuilt)

- An isolated virtual environment from the G0 work holds only the declared
  dependencies and their transitive closure (`pip freeze` recorded in the G0
  evidence set).
- The upstream package is imported from a read-only source junction; it is not
  installed as a distribution and does not touch repository dependencies.
- `tzdata` is installed only because the declared dependency is conditional on
  the Windows platform.

## Acceptance outcome (D1, at the upstream pin `2b64026`)

| AC | Result | Note |
|---|---|---|
| AC-01 version and install | PASS | version, commit, clean tree, dependency freeze recorded |
| AC-02 same key replay | PASS | `duplicate`, no new version |
| AC-03 same key different content | PASS | `conflict`, no overwrite |
| AC-04 replay under a deleted key | PASS | `ACCESS_DENIED: source_unavailable` |
| AC-05 forged identity | PASS | `ACCESS_DENIED`, field `origin`; matching control succeeds |
| AC-06 closure range | PASS | upstream authorizes by scope, not project; the adapter must compare the full affected set |
| AC-07 ack loss | BLOCKED | replay `apply` returns `VERSION_CONFLICT`; no public receipt interface |
| AC-08 deletion authorization | PASS | no statement: `forget_not_authorized`; with a human statement: operation recorded |
| AC-09 retrieval after deletion | PASS | recall returns no item; retracted-since epoch advances |
| AC-10 upstream claim mutation | PASS | capture reports `mutation="revised"`; PCS belief store untouched |
| AC-11 no model, no vector | PASS | lexical path works; purge retries `storage_unavailable` without a port |
| AC-12 contract structure and text-consistency self-check | PASS | `tests/contract/test_memory_integration_contract.py`: 24 assertions on files, sections, phrases, enum classes and cross-file consistency only |
| AC-13 repository gate | PASS | run after the artifacts were written |
| AC-14 adapter surface | N/A | no adapter skeleton was created; D2 closure does not start one |
| AC-15 maintenance separation | PASS | only `purge`/`rebuild_projection` claimed; `consolidate`/`embed` stay pending at `attempt=0` |
| AC-16 public receipt interface | BLOCKED **at the pin** | no public interface at `2b64026`; unblocked by the local patch (see the D2 section above) |

**D1: reached at the pin.** At that baseline AC-16 and the AC-07 recovery branch
were blocked by the absence of a public interface. Both were unblocked later by
the local patch, and **D2 is now declared on that patch baseline `446bfef8`**
(see the D2 section above). The pin itself is unchanged.

## What remains open

1. **Upstream receipt interface.** Implemented only in the local patch
   (`pcs/receipt-v1` @ `446bfef8`). Upstream acceptance and release have not
   happened; the patch must be maintained until they do.
2. **Deletion recovery at the original pin.** Without the patch an
   acknowledgement loss can still only be `needs_review` with the barrier kept.
   Replaying `apply` stays forbidden evidence.
3. **PurgePort.** The honest semantics are specified, but no real backend was
   validated. A test double proves scheduling only. Real vector, attachment and
   backup clearing is future work.
4. **Adapter skeleton.** `adapters/scope_recall/` is not created. D2 closure does
   not start it; that is a separate decision and card.

## Artifacts

- Nine contracts plus the observed enum inventory under
  `contracts/memory-integration/v1/`.
- Contract **structure and text-consistency** self-check:
  `tests/contract/test_memory_integration_contract.py`. Its 24 assertions cover
  required files, sections, keywords, phrases, enum classes and cross-file
  consistency. It does not prove runtime behaviour; behavioural claims cite the
  probe outputs above.
- Evidence (Lab, outside the repository): the G0 deletion/recovery report and
  evidence sets, plus the J02 D1 probe output for identity, deletion
  authorization and claim mutation.

## Verifying

```text
python -m unittest tests.contract.test_memory_integration_contract -v
python tools/check_repository.py
```

The contract test reads only the contract files; it does not import the
upstream package and does not require the Lab environment.

Gate numbers move with the commit graph: `files` is governed by the allowlist,
while `commits` and `history_blobs` grow with every commit. Cite a gate result
together with the HEAD it was taken at; two runs at different commits are both
valid and are not a discrepancy.
