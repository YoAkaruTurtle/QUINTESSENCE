# 02 — Source mapping

**Direction:** PCS experience (an `EventRef` or an Experience projection) →
upstream `SourceEvent`. The mapping is created by PCS, verified by the host, and
never guessed from content.

## Identity

One immutable PCS `(ref, version)` maps to exactly one upstream source
identity:

```text
source_event_key = deterministic(instance_id, namespace, ref_kind, ref_id, version)
source_identity  = (source_event_key, source_revision)
```

- `source_event_key` already names the immutable version. Two different
  versions of one object therefore have **different** keys.
- `source_revision` is declared by the projection and stays constant for that
  key; retries reuse the same pair. Upstream treats `(key, revision)` as the
  unique source identity and does not derive the number from PCS versions.
- The mapping is stored on the PCS side. The upstream does not know about PCS
  records or their version chains.

## Four version axes (never equated)

| Axis | Owner | Meaning |
|---|---|---|
| PCS ref version | PCS | version of the immutable PCS object that was projected |
| PCS `record_version` | PCS | version of a PCS record such as a Belief; not a source version |
| upstream `source_revision` | projection | declared revision of this projection; constant for one key |
| delivery `attempt` | transport | retry counter; never a content or source version |

The four values are independent. No rule in these contracts assumes they are
numerically equal, comparable, or synchronised.

## Dispositions

| Case | Upstream result | PCS action |
|---|---|---|
| same `(key, revision)`, same content | `duplicate` | success; no new source, no new mapping |
| same `(key, revision)`, different content | `conflict` | terminal; no overwrite; flag a PCS-side integrity bug |
| a new PCS version | new key | new source; the old key and mapping stay intact |
| source deleted before delivery | not sent | delivery becomes obsolete; never resurrected |

A `conflict` is not a retry condition. It means the same immutable identity was
projected with a different payload, which must not happen and must be
investigated rather than overwritten.

PCS-side delivery state values: `pending`, `applied`, `verified`,
`needs_review`, `blocked`, `obsolete`. These are PCS contract values; the
upstream has no equivalent state machine for a projection.

## Worked examples (contract level, no implementation)

1. **Same version retried.** PCS object `P1` version 1 → key `K1`, revision 1,
   content A. A retry of the same projection → `duplicate`.
2. **Version update.** `P1` version 2 → key `K2` (a different key), revision 1,
   content B → `inserted`. `K1` is untouched; no conflict is involved.
3. **Same key, different text.** `K1` revision 1 is sent again with content C →
   `conflict`, no overwrite, and PCS records an integrity fault for `K1`.
4. **Cross-version de-duplication.** `K1` and `K2` are two versions of one
   evidence root. Both may exist upstream; the root still contributes at most
   one support vote to a PCS judgement (see `04-root-source-dedup.md`).
5. **Deletion scope.** Deleting `P1` version 1 covers `K1` only. Deleting the
   whole root covers every mapped key of that root; the adapter must enumerate
   them and must not claim the rest was cleaned.

## Invariants

1. Every upstream source row is traceable to exactly one PCS ref and version
   through PCS-side mapping records, not through text matching.
2. Deleted PCS sources are never re-projected. The projection checks the PCS
   deletion barrier before and after the upstream write.
3. `origin` cannot be derived from `role` or from the payload. See
   `03-role-and-origin.md`.
4. A mapping stores the external revision alongside the PCS version. A later
   read compares both; matching one is not enough.

## Failure semantics

| Situation | Behaviour |
|---|---|
| same key and revision, same content | `duplicate`; treat as success |
| same key and revision, different content | `conflict`; no overwrite; integrity fault |
| a new version projected under an old key | PCS bug: versions must get distinct keys |
| source deleted before delivery | do not deliver; mark the delivery obsolete |
| upstream unreachable | retry the same delivery identity; PCS content stays local |

## Evidence

- `s6-edges.json` records `duplicate` for an identical replay and `conflict`
  (with `mutation = "none"`) for the same key with different text.
- The pinned schema declares `UNIQUE(source_event_key, source_revision)` and
  `PRIMARY KEY(event_id, source_revision)`: identity is the key plus revision,
  and a revision is addressable (`core.source(ctx, ref, revision)`).
- The projection's version-to-key rule is a **PCS contract decision**. It uses
  the upstream uniqueness semantics; it does not claim the upstream validates
  PCS versions.
