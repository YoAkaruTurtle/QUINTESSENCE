# 06 — Deletion (three layers, order, acknowledgement, receipt proposal)

**Direction:** PCS deletion decision → local barrier → upstream deletion plan →
apply → completion tracking. PCS owns authorization; the upstream maintenance
component only executes.

## 1. Completion is three layers

| Layer | Meaning | Observable | Does not imply |
|---|---|---|---|
| L1 operation committed | the deletion operation exists and the epoch advanced | success response, operation record | content is unreadable |
| L2 reads and re-delivery blocked | target invisible; re-delivery refused; retrieval does not return it | `SOURCE_MISSING`, `ACCESS_DENIED: source_unavailable`, recall without the target | storage is cleared |
| L3 contracted storage layers cleared | every reported layer is `removed` | the receipt `layers` map | backups and physical media |

Receiving a receipt does **not** mean L3. `pending`, `inventory_pending` and
`unknown` keep their meaning and are never rewritten as success. Clearing a
"pending verification" state never removes the deletion barrier.

`active_content_removed` is true only when the active SQLite layer, active
vectors and attachments are all done. History layers may still be pending.

Layer keys observed at the pin: `sqlite_active`, `sqlite_history`,
`vector_active`, `vector_history`, `attachments`, `backups`, `host_sources`,
`physical_media`. Values observed include `pending`, `inventory_pending`,
`removed`, `retained`, `unknown`, `unverifiable`, `outside_plugin_scope`.

## 2. Order and local persistence

```text
1. PCS persists the deletion intent and raises the local barrier
   (at this point no upstream preview exists)
2. preview (read only)
3. compare the complete affected set with the PCS authorization range
   - out of range: stop, mark needs_review, send no apply
4. persist the approved preview, bound to the upstream instance
5. apply
6. query the receipt and track layer progress
```

Persist before apply:

| Field | Why |
|---|---|
| PCS task id | separate from the upstream `preview_id`; never merged |
| `preview_id`, `mode`, `targets` | bound to one upstream instance |
| complete `affected` identities and versions | a count is not a range check; do not invent versions the upstream does not report |
| upstream `memory_epoch` and dependencies | detect staleness |
| PCS authorization reference | the source of authority |
| operation ids and receipts | written **after** a success response |

An acknowledgement loss recovers from the pre-persisted instance identity,
`preview_id` and complete preview. The lost response is never a recovery
prerequisite.

Re-planning (target set or version changed) requires a new preview and a new
range comparison. Authorization is never expanded automatically.

## 3. Acknowledgement loss and retry by operation

Retry behaviour is per operation. The "retry with the same identity" rule in
`05-delivery-receipt.md` applies to **capture only**; it must not be copied to a
deletion or to a read.

| Operation | Unknown outcome possible | Allowed behaviour |
|---|---|---|
| capture (`record_event`) | yes (`durability="unknown"`, transport failure) | retry the same delivery and source identity; idempotent |
| deletion `apply` | yes (ack lost, process crash, transport failure) | **no automatic replay**; no re-preview or re-apply may stand in for verifying the original operation; keep the barrier and mark `needs_review`; verify only through a public receipt interface |
| read query (recall, inspect, history) | failure is a failure | bounded retry; a failure never becomes success, an empty result, or "no record" |
| maintenance `drain_worker` | partial progress | re-run with the same bounds; purge is idempotent per operation id |

**Deletion `apply` is not retried.** A retry cannot distinguish "the first
attempt committed" from "it never ran", it may act on a preview the data has
outgrown, and it can widen the affected set if the store changed. The measured
replay returns `VERSION_CONFLICT` in both the pre-purge and post-purge states.

**Two readings of `VERSION_CONFLICT` must not be mixed:**

1. **Preview clearly stale.** The target set or a target version changed before
   this apply, under this preview. The operation did not commit under it.
   Re-plan only after a fresh preview and a fresh comparison of the complete
   affected set with the PCS authorization. Authorization is never expanded.
2. **Original deletion outcome unknown.** An earlier apply may or may not have
   committed. A later `VERSION_CONFLICT` says nothing about that earlier
   attempt: it is neither success nor failure evidence. Resolution requires the
   public receipt interface; until then the barrier stays and the task is
   `needs_review`.

Forbidden as proof of success: a replay result, `VERSION_CONFLICT`,
`SOURCE_MISSING`, internal reads of `management_actions`, an internal
`deletions.receipt`, or recomputing an internal operation id hash.

Measured at the pin: replaying `apply` after a successful delete returns
`VERSION_CONFLICT: invalid preview`, both before and after the purge step. It
is neither success nor `SOURCE_MISSING`.

**At the pin**, without a public receipt interface the only honest state is
`needs_review` with the barrier kept, recorded as "deletion recovery blocked".
That is the state before the local patch in §4; the patch does not change the
pin's public surface.

## 4. Receipt interface (implemented in a local patch, not upstream)

The pinned commit `2b64026` has **no** public receipt interface. It is now
implemented in a **local patch** on branch `pcs/receipt-v1`: first commit
`629f4294c8b48e42e079b8778ad76fc60eed7a53`, current head
`446bfef886df14bcb25d0e4f73b264e90cf47097`, base `2b64026`; the upstream project has
not accepted or released it. Three states stay separate:

| State | Public receipt interface |
|---|---|
| original `3.14.1` at `2b64026` | no |
| local patch head `446bfef8` | yes, as tested below |
| upstream accepted / released | not yet |

```python
MemoryManagement.receipt(
    preview_id: str,
    *,
    expected_instance: str | None = None,
) -> DeletionActionView
```

R1 as implemented: `apply()` writes the related operation ids on the management
action row in the **same write transaction** as the deletion. One management
action may own several operations (grouped by project and branch). Historical
rows are not backfilled. The implementation carries a schema migration
(1112 → 1113: one nullable column), and an old row without the link answers
`incomplete_link` rather than a guessed identifier.

- Lookup is bounded by maintenance authority and the instance boundary.
  `preview_id` is a lookup key, not a credential.
- Returns the action (`targets`, `recorded_at`, `memory_epoch`) plus, for each
  operation, the layer progress **as of the query time**, in the apply order.
- Five outcomes stay distinct: `found` returns the view; `no_record` is a
  missing action (`SOURCE_MISSING`, field `receipt`); `incomplete_link` is a
  missing, empty, duplicated or inconsistent link (`INCOMPLETE_LINK`);
  `authority_mismatch` is an instance or scope mismatch (`ACCESS_DENIED`, field
  `receipt`); `query_failed` is a storage failure (`STORAGE_UNAVAILABLE`, field
  `receipt`). None of the last four is success.
- **Authorization is decided before the link is read.** The entry authority,
  the declared instance and every target scope are checked first; then each
  operation's scope is checked before its progress is parsed. An action outside
  the reading scopes answers `authority_mismatch` even when its link or layer
  data is damaged, and the message carries no scope, target, count or cleaning
  detail. Data that cannot be parsed cannot establish authority, so it fails
  closed with `authority_mismatch`.
- **Link completeness is bound to the action.** A delete or suppress action
  must name at least one operation; an empty link, a duplicate identifier, an
  identifier with no operation row, or operation groups that do not equal the
  action's own target groups are all `incomplete_link`. A truncated
  multi-operation link therefore fails even when every listed identifier
  resolves. Non-deletion actions (revise, unmute, restore) legitimately carry
  no deletion operation and are `found` with an empty operation list.
- A broken link is never repaired by guessing: identifiers are not derived from
  targets or request hashes, and the read never re-runs a deletion.
- The read is read-only: it never deletes, purges, replans or writes. "Cleaning
  not finished" is progress inside a `found` view, not a sixth outcome.
- Known limit: a coordinated rewrite of both the action's target snapshot and
  its link sits inside the local management DB trust boundary and is not
  detectable by this read. No extra ledger is added to cover it.
- D1 does not depend on this interface; D2 does. **D2 is declared on the local
  patch baseline `446bfef8`** because AC-16 and the AC-07 recovery branch pass
  there. The patch is not an upstream capability; see §6.

## 5. PurgePort semantics

`drain_worker(..., purge=<PurgePort>)` is the only maintenance path; the
`purge` and `rebuild_projection` work types do not need a model.

- "No vector backend configured" is not "no historical external content".
- A storage layer may report "nothing to clear" only when the managed instance
  proves it was never created or is verifiably empty.
- A layer that was used but is now off, unreachable or unknown must not report
  success.
- Active vectors, historical vectors, attachments and backups are reported
  separately. One layer's success never covers another layer's unknown.
- A test double proves scheduling and interface behaviour only. It never proves
  a real backend was cleared.

Without a purge port, purge keeps retrying `storage_unavailable`. That is
measured behaviour, not a queue failure, and it is never a silent success.

## 6. Status of this contract

- L1/L2/L3 wording, order, ack rules and purge semantics: **D1 complete**.
- Receipt interface: **implemented in local patch head `446bfef8`**, tested there
  (found / no_record / incomplete_link / authority_mismatch / query_failed,
  read-only, transaction link, migration 1112→1113). **Not upstream, not
  released.**
- **D2: declared on the local patch baseline `446bfef8`.** AC-16 (public receipt
  interface) passes there and the AC-07 recovery branch (lost ack → restart →
  confirm through the public interface, never replaying `apply`) passes there.
  The original `3.14.1` at `2b64026` still has no interface; its BLOCKED record
  stays as history.
- D2 on a local baseline is **not** an upstream capability, does **not** start
  an adapter, and does **not** verify real purge backends.
- Real vector/attachment/backup backends: not verified by this card.
