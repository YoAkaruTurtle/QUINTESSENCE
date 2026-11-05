# 05 — Delivery and receipt

**Direction:** PCS outbox → upstream capture → receipt. Capture delivery is at
least once with idempotent source identity; it is never exactly once across two
stores.

## Delivery record

| Field | Meaning |
|---|---|
| `delivery_id` | PCS-owned stable identity of one delivery |
| `request_hash` | hash of the exact request payload; detects a changed retry |
| `attempt` | transport counter; never a content or source version |
| `lease_until` | bounded execution window |
| `next_retry_at` | back-off schedule |
| `receipt` | the upstream capture receipt once received |

## Capture receipt

The upstream receipt reports, at least:

| Field | Values at the pin | Basis |
|---|---|---|
| `disposition` | `inserted`, `duplicate`, `conflict`, `rejected`, `unavailable`, `cancelled` | `inserted`/`duplicate`/`conflict` measured; the others source-declared |
| `durability` | `persisted`, `not_persisted`, `unknown` | `persisted` measured; `not_persisted` and `unknown` source-declared (`STORAGE_UNAVAILABLE`) |
| `mutation` | `none`, `revised` | both measured |
| `lexical_state` | `ready`, `not_ready`, `not_indexed`, `unknown`, `unchanged` | `ready` measured; the rest source-declared |
| `semantic_state` | `ready`, `partial`, `not_indexed`, `not_scheduled`, `unknown`, `unchanged` | `ready`/`partial` measured; the rest source-declared |
| `error_code` | present for a refused or failed capture | measured for `ACCESS_DENIED`, `VERSION_CONFLICT`, `STORAGE_UNAVAILABLE` |

`enums.json` records these values with their source class. Every set is
**open**: a new value at the same pin is a contract change, and a new value
after an upstream upgrade requires re-assessment. A value being listed does not
mean it was measured.

**`durability` meanings.** `persisted` means the capture committed.
`not_persisted` means it was refused or cancelled; nothing was written.
`unknown` means the outcome is not confirmed, for example a storage failure
after the point where the commit may already have happened. `unknown` is never
success and never a definite failure.

## Retry rules — capture only

These rules apply to capture delivery. They do not apply to deletion `apply`
(see `06-deletion.md` §3) or to read queries (see `09-error-mapping.md`).

1. A retry keeps the same `delivery_id`, the same source identity and the same
   payload hash.
2. `duplicate` is success **for capture**: the upstream already holds this exact
   source. It carries no information about any other operation.
3. `conflict` is terminal. Do not rewrite the upstream source; keep the PCS
   record and surface the integrity fault.
4. A changed payload under the same `delivery_id` is a PCS bug; refuse it
   before sending.
5. `durability = "unknown"` is retried with the same identity, because a
   capture is idempotent under its source identity. It is never reported as
   success and never creates a second source identity.
6. A delivery whose source was deleted before sending, or whose delete barrier
   was raised while it was queued, is marked obsolete and is not retried.

## Invariants

- At most one effective upstream source per `delivery_id`, however many
  attempts occur.
- The receipt is stored as a delivery outcome, not as a belief update.
- Deleting a PCS source precedes and overrides any pending delivery of it.
- A retry never changes the source identity, the payload, or the target scope.

## Deletion receipt is a separate read

A capture receipt answers "was this source captured". It is not a deletion
receipt and never substitutes for one. After a deletion `apply` whose response
was lost, the only permitted confirmation is the public receipt read defined in
`06-deletion.md` §4, using the pre-persisted instance identity and
`preview_id`. The apply is not replayed, and a capture receipt must not be
reinterpreted as proof that a deletion committed.

On the local patch `629f4294`, `MemoryManagement.receipt` returns a `found`
view or one of four distinct failures; on the pinned `2b64026` the read does
not exist.

## Failure semantics

| Situation | Behaviour |
|---|---|
| upstream unreachable | retry the capture with the same identity; PCS keeps the local record |
| receipt lost after a committed capture | retry; expect `duplicate` |
| payload changed between attempts | refuse locally; do not send |
| source deleted while queued | cancel delivery; never resurrect |
| `durability = "unknown"` | retry with the same identity; keep the delivery open until confirmed |
| `conflict` | stop; integrity fault; no automatic retry |
