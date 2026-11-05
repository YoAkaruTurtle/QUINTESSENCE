# 09 — Error mapping

**Direction:** upstream `ContractError` → PCS error classification. Errors are
mapped, never swallowed, and never turned into a success shape.

## Upstream shape

`ContractError(code, field, hint)` carries a stable `code`, the offending
`field`, and an optional hint. The message text is not a contract; the code and
field are.

Codes observed at the pin: `ACCESS_DENIED`, `SOURCE_MISSING`,
`VERSION_CONFLICT`, `IDENTITY_UNBOUND`, `INPUT_INVALID`. The set is open; a new
code is a contract change that must be added here with its PCS classification.

## Mapping

| Upstream code | PCS classification | Retryable | Notes |
|---|---|---|---|
| `ACCESS_DENIED` | authorization / scope violation | no | includes `origin`, `source_unavailable`, `forget_not_authorized`, `target_unavailable` fields |
| `SOURCE_MISSING` | missing source | no | also the read signal after a successful delete |
| `VERSION_CONFLICT` | stale preview or unknown earlier outcome | no | stale preview → re-preview and re-compare before a new apply; unknown earlier deletion outcome → never replay, never re-preview/re-apply to verify, keep the barrier; never proof of deletion |
| `IDENTITY_UNBOUND` | configuration / identity error | no | stop the instance, do not retry |
| `INPUT_INVALID` | malformed request | no | a PCS-side bug; do not retry unchanged |
| `INCOMPLETE_LINK` | receipt link cannot be confirmed | no | local patch `629f4294`; missing/damaged operation link; never treated as "not found" or "deleted" |
| `STORAGE_UNAVAILABLE` | storage failure / unknown outcome | depends | for capture, retry the same identity; for the receipt read, map to `query_failed`; for a deletion apply, no retry |

## Receipt results (local patch head `446bfef8`)

The public receipt read maps one return value and four error fields. The PCS
side keeps them distinct and never folds a failure into `found`.

| Receipt result | Upstream shape | PCS action |
|---|---|---|
| `found` | returns `DeletionActionView` | confirm the action; progress is read, never assumed complete |
| `no_record` | `SOURCE_MISSING`, field `receipt` | unknown action; do not infer deletion |
| `incomplete_link` | `INCOMPLETE_LINK` | cannot confirm; keep the barrier, needs review |
| `authority_mismatch` | `ACCESS_DENIED`, field `receipt` | stop; do not probe further |
| `query_failed` | `STORAGE_UNAVAILABLE`, field `receipt` | retry the read later; never turn into `no_record` |

Two rules order the read. Authority is checked before the operation link is
parsed, so a damaged link never turns an unauthorized action into a more
specific internal answer. Once authorized, an empty link on a delete/suppress
action, a duplicate identifier, a missing operation row, or a link whose
operation groups differ from the action's target groups is `incomplete_link`;
a non-deletion action may legitimately have none. Target data that cannot be
parsed cannot establish authority and fails closed as `authority_mismatch`.

The read never writes, so a retry is safe. That is different from replaying a
deletion `apply`, which is never allowed.

## Rules

1. A code is mapped to exactly one PCS classification. The field is preserved
   for diagnosis; only a documented public subset may reach a model.
2. `ACCESS_DENIED` is not "not found". Collapsing the two would let a caller
   probe for hidden objects.
3. `SOURCE_MISSING` means the object is not readable. It is not "never
   existed", and after a delete it is not proof of which operation removed it.
4. `VERSION_CONFLICT` on `apply` means the plan changed. It is never evidence
   that a previous apply succeeded.
5. Unknown codes are mapped to an opaque integration failure. They are logged
   with code and field and are not retried automatically.
6. Errors are never converted into an empty successful result. A degraded path
   must be visible as degraded.

## Retry is per operation

A generic "retry with the same identity" is wrong: it fits capture, not a
deletion and not a read. The rules below are the only allowed readings.

| Operation | Condition for retry | Forbidden |
|---|---|---|
| capture | source not deleted, identity and payload unchanged, delivery still authorized by the upstream contract | retrying with a changed payload or after the source was deleted |
| deletion `apply` | **never automatic.** An unknown outcome (ack lost, crash, transport failure) keeps the barrier and becomes `needs_review`; resolution needs a public receipt interface | replaying `apply`, or re-preview/re-apply to "confirm" the earlier operation; treating `VERSION_CONFLICT` or `SOURCE_MISSING` as success |
| read query | bounded retry with the same request | turning a failure into success, an empty result, or `no_record` |
| maintenance `drain_worker` | re-run with the same bounds | unbounded loop; claiming success for unprocessed items |

`VERSION_CONFLICT` has two distinct readings and they are never merged:

1. **stale preview** — the plan no longer matches the store; re-preview and
   re-compare the complete affected set before any new apply. Do not expand
   authorization.
2. **unknown earlier outcome** — an earlier delete may have committed. The
   conflict is not evidence either way; keep the barrier, mark `needs_review`,
   and wait for a public receipt interface.

## Failure semantics

| Situation | Behaviour |
|---|---|
| unknown code | opaque failure; alert; no auto-retry |
| repeated `ACCESS_DENIED` | stop the operation; surface the denial |
| `VERSION_CONFLICT` on a preview | stale preview: re-preview, re-compare, no automatic authorization growth |
| `VERSION_CONFLICT` on a deletion | unknown earlier outcome: barrier stays, `needs_review`; never replay |
| transport error on capture | unknown outcome; retry the same source identity |
| transport error (no code) on deletion | unknown outcome; **no retry**; barrier stays, `needs_review` |
| transport error on a read | bounded retry; on failure report failure, never an empty success |
