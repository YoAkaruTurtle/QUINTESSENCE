# J01 — Availability coverage for Belief delivery

Status: implemented in J01 (task card v1.5). This file records the single
"currently usable" rule, where each outlet implements it, and which gaps are
deliberately left to later cards.

## 1. The rule

A Belief version may be delivered as current or readable material only while
all of the following hold:

1. identity and authorization: namespace, `privacy_class`, declared purpose
   (including `consult`) and scope all match;
2. object barrier: the Belief record itself is not behind a deletion barrier;
3. source liveness: every `evidence_links` and `premise_refs` target is still
   outside a deletion barrier (`belief_evidence_is_live`);
4. status and time: the version is the current pointer (or an explicitly
   requested exact version), its stored status is in the allowed set, and an
   expired `valid_until` is reported as `applicability="expired"` rather than
   silently dropped.

Gate 3 runs **only after** gate 1. If authorization, privacy or scope fails,
`belief_evidence_is_live` must not be called: otherwise the new gate would be
a probe for "does a hidden deleted record exist". This ordering is locked by
`tests/domain/test_availability_contract.py` with a call-counting stub.

A deleted source never rewrites the stored status. The version keeps its
stored `status` (for example `supported`) and simply stops being deliverable.

## 2. Exact-read failure shapes

`BeliefExactQuery` answers `unavailable` for exactly five cases, and the
`to_body()` bytes are identical for all five:

| case | how it arises |
|---|---|
| record deleted | `record_is_deleted` on the requested record |
| record missing | no such record id |
| not a Belief | the record kind is not `belief` |
| privacy class insufficient | the version's privacy class is outside the access set |
| evidence no longer live | gate 3 on the requested version |

Three failures are deliberately **not** `unavailable` and are unchanged:
missing `consult` purpose raises `AuthorizationRequired`; a ref in another
namespace raises `ScopeViolation`; a scope mismatch returns `empty` with the
public `scope_mismatch` reason.

## 3. Coverage

Legend: **gated** = the outlet applies gate 3; **ungated** = an existing outlet
that does not; **absent** = the outlet does not exist yet.

| outlet | location | status | owner |
|---|---|---|---|
| current Belief query | `domains/cognition/query.py` `_current_locked` | gated: omitted, internal `evidence_not_live` count | J01 |
| exact Belief query | `domains/cognition/query.py` `_exact_locked` | gated: `unavailable` | J01 |
| Episode query | `domains/cognition/query.py` `_episodes_locked` | gated: deleted EventRefs filtered (pre-existing) | J01 regression |
| Context assembly | `runtime/context.py` `_materials_locked` | gated: `belief_evidence_is_live` + `source_redacted` omission (pre-existing) | J01 regression, J03 boundary |
| SelfView | `domains/cognition/views.py` | gated: unavailable when a referenced Belief's sources are gone (pre-existing) | J01 regression |
| Consult query path | `collaboration/consult.py` | gated through C10 and `to_body()`; the artifact body is internal only | J01 |
| Review current-version view | `collaboration/facade.py` `_read_current` | gated: source liveness after the entry, visibility, namespace and record-barrier/privacy checks (J01b) | J01b |
| model pre-send check | `runtime/http_model.py` `_pre_send` | partial: barriers for referenced records/events, no Belief evidence closure | J03 / J07 |
| history view | exact query with `current=false` | gated per requested version | J01 |
| export | n/a | absent | future card |
| memory/external delivery | n/a | absent | J04 / J07 |
| final request-release barrier | n/a | absent | J03 / J07 |

## 4. Projection rules

- `to_body()` (model visible) never carries an internal omission reason. The
  internal reasons are `not_authorized`, `source_ref_not_authorized`,
  `source_ref_redacted`, `redacted`, `unavailable`, `evidence_not_live`.
  Publishing `evidence_not_live` counts would let a caller infer the number of
  hidden blocked records, so it stays internal.
- `to_artifact_body()` (artifact persistence) keeps only the public reasons
  plus `state_revision`. The internal counts exist only on the in-memory
  `QueryResult`, which is never persisted or rendered. This preserves the
  existing Consult contract that internal omission reasons never reach a stored
  artifact (see `tests/collaboration/test_consult_facade.py`).
- `QUERY_POLICY_VERSION` is `collab.v1.query.v2`. Consumers (J02–J07) must
  assume that a returned item is currently usable, but that a missing item may
  be missing, unauthorized, deleted or source-blocked; the result does not
  distinguish them.

## 5. Known gaps and handoffs

1. **Review current-version view (closed by J01b).** `_read_current` now
   applies the source-liveness predicate after the entry permission, candidate
   visibility, namespace and record-barrier/privacy checks, and reports the
   existing `current_redacted` state. No public state or reason was added.
2. **Final delivery barrier.** Nothing guarantees atomicity between the last
   check and the actual model request. J01 provides the predicate; the release
   protocol belongs to J03, the Context delivery point to J07.
3. **C11 query artifacts.** A previously persisted `query_result` artifact is
   not re-gated. If artifacts are ever consumed as a source, the predicate must
   be extended.
4. **Dangling doc reference.** `store/deletion.py` points at
   `docs/design/PCS2-006-TXB.md`, which does not exist. Recorded here; not
   repaired by J01.
5. **Chinese roadmap wording (aligned by J01b).** `ROADMAP.zh-CN.md` now states
   that P0 is merged to `main` at `13c3589`, matching `ROADMAP.md`.

## 6. Read-only re-evaluation

`reevaluate_belief_against_live_sources(store, namespace=..., body=...,
clock=...)` in `domains/cognition/beliefs.py` computes the verdict a Judgement
would have if only its surviving evidence counted. It:

- uses the same derivation rules as the commit path (`BeliefService._derive`);
- writes nothing: no record, version, pointer or receipt changes;
- reports `live_link_count`, `retired_refs` (role, ref type, id, version),
  `status`, `strength` and `degraded_to_proposed`;
- raises `CorruptStore` for a **missing** (not deleted) source, matching
  `belief_evidence_is_live`.

Writing the verdict back (a new version through Tx B, or a Review candidate) is
J08/J06 work. J08 must consume this function rather than re-deriving the
verdict.

## 7. Verification

```text
python -m unittest tests.domain.test_availability_contract -v
python -m unittest tests.domain.test_cognition_query tests.domain.test_context_step \
    tests.domain.test_self_view tests.domain.test_beliefs
python tools/run_tests.py --output <outside the checkout>
python tools/check_repository.py
```

The audit that produced §3 used:

```text
grep -rn "parse_belief_body" src/quintessence_pcs --include=*.py
grep -rn "to_body()\|to_artifact_body()\|query_body" src/quintessence_pcs --include=*.py
grep -rn "belief_refs\|stored_status\|applicability" src/quintessence_pcs/runtime \
    src/quintessence_pcs/collaboration --include=*.py
```
