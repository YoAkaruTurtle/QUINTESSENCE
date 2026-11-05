# 04 — Root source, versions and de-duplication

**Direction:** memory reasoning → PCS evidence. A projection version and an
evidence root are two different identities; only the root may count as a
support unit.

## Two identities

| Identity | What it names | Who owns it | Example |
|---|---|---|---|
| **Projection version identity** | one immutable PCS `(ref, version)` → one upstream `(source_event_key, source_revision)` | PCS mapping (see `02-source-mapping.md`) | `K1` for `P1@1`, `K2` for `P1@2` |
| **Evidence root identity** | the origin a source ultimately comes from | PCS root mapping | root `R1` covers `K1`, `K2`, and their summaries |

A root can own several projection versions, summaries, segments, and delivery
attempts. Each of those is a distinct projection identity; none of them is a
distinct root.

## What does not add a support vote

1. A second version of the same source (`P1@2` alongside `P1@1`).
2. A summary or consolidation derived from the same root.
3. Several segments of one document.
4. The same root delivered again under another delivery identity.
5. A multi-source Episode whose roots cannot be verified: it counts as one
   unverifiable group, never as several independent votes.

Independent support requires distinct roots with distinct verified source
instances. Two rows that share a root are one vote, whatever their version
numbers say.

## Multi-source derivation

A derived statement that used several roots must keep the **complete root set**
it depended on. It may not:

- keep only the largest or newest root;
- drop a root because it is "probably included" in another;
- treat an unverifiable root as if it were verified.

If the root set cannot be established, the derived statement is not usable as
evidence and must not claim independence.

## Deletion scope

Deleting one projection version and deleting a whole root are different
operations with different coverage.

| Operation | Covers | Does not cover |
|---|---|---|
| delete one projection version | that one mapping `(key, revision)` and its derived copies | other versions of the same root; other roots |
| delete the whole root | every mapped projection version and derived copy of that root, enumerated from the root mapping | another root; anything outside the authorized set |

Rules:

1. Deleting a version does not imply deleting the root, and does not authorize
   widening the deletion to sibling versions.
2. Deleting a root must enumerate all mapped versions first. If enumeration is
   incomplete or unverifiable, stop and mark `needs_review`; never report the
   unenumerated part as cleaned.
3. A derived copy whose root was deleted stops being delivered even when the
   copy's own row still exists.
4. The affected set must be compared with the PCS authorization range before
   any upstream deletion is applied.

## Worked examples (contract level)

1. **One root, two versions.** `P1@1` → `K1`, `P1@2` → `K2`, both under root
   `R1`. A judgement supported by both has one support vote, not two.
2. **Summary of a root.** A summary of `R1` is a derived copy. It adds no vote
   and inherits `R1` for provenance.
3. **Repeated delivery.** `R1` delivered twice (two deliveries) still counts
   once. The second delivery is `duplicate`, not a second source.
4. **Single-version deletion.** Deleting `P1@1` covers `K1`; `K2` stays unless
   separately authorized.
5. **Root deletion.** Deleting `R1` must enumerate `K1` and `K2` (and derived
   copies) as one plan. A plan that covers only `K1` must not be reported as a
   root deletion.

## Invariants

- Every derived statement carries a path back to its root.
- Root identity is stable across restarts and re-projections.
- Root counting is independent of version numbers and delivery attempts.

## Failure semantics

| Situation | Behaviour |
|---|---|
| closure contains objects outside the authorized set | stop before apply; mark `needs_review` |
| Episode body and source mapping disagree | corruption error; do not deliver |
| root enumeration incomplete | do not delete; mark `needs_review` |
| derived copy without a root | do not use it as evidence |
