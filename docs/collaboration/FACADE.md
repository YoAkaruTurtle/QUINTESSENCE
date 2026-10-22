# Review Facade (C07)

This document describes the review-only trusted-host Facade in
`src/quintessence_pcs/collaboration/facade.py`. The implementation plan and the
contracts remain the authority; this file explains how the Facade uses them and
what it deliberately does not do.

## 1. Scope

The Facade wires one loop:

```
work scan -> Context/generation -> candidate -> approve/reject
          -> belief commit or WAIT -> result recovery -> experience read
```

It is an adapter over the C06 trusted internal API (`CollaborationService`),
not a second implementation of it. The one commit path, the one approve/reject
path, the one result-recovery path and every store rule stay where C06a/C06b/
C06c put them.

Out of scope: consult flows, a new model protocol, query surfaces, full
Persona/Memory integration, a new application, ACL or identity services, and
any contract or migration change. C07 adds no error code.

## 2. HostAccess

```python
@dataclass(frozen=True)
class HostAccess:
    namespace: str
    host_instance: str
    operator: str
    permissions: frozenset[str]
    privacy_classes: frozenset[str]
    purposes: frozenset[str]
    access_ref: str
```

`FACADE_PERMISSIONS` is closed:

| Permission        | Allows |
|---|---|
| `review.generate` | `preview`, `review` |
| `review.approve`  | `resolve(decision="approve")` |
| `review.reject`   | `resolve(decision="reject")` |
| `result.read`     | `result`, and showing a commit receipt in any view |
| `experience.read` | `read_experience` |
| `work.scan`       | `legacy_work`, `managed_work` |

Rules:

- Every public method accepts only a `HostAccess` instance. The trusted host
  constructs it; model output, candidate bodies and request JSON never do.
- Construction enforces: `permissions` is a subset of `FACADE_PERMISSIONS`,
  `privacy_classes` is a subset of the frozen privacy classes and `purposes`
  is non-empty. A malformed object cannot be built.
- A missing permission is `AuthorizationRequired` (`SCOPE_VIOLATION`); no new
  error code is introduced.
- This is **not** an ACL or token system. There is no permission table, no
  revocation store and no identity service. It is a per-call capability object
  whose real trust boundary is architectural: only trusted host code can build
  one and call the Facade.

`review.generate` only allows starting a review. It does not grant reading
local files, a private database, or publishing anything to the network.

## 3. Review states and DTOs

`REVIEW_STATES`:

| State | Meaning |
|---|---|
| `structural_only` | parsed and domain-validated; nothing persisted |
| `pending` | a candidate row exists; no command receipt exists |
| `committed` | a command receipt exists |
| `waiting` | a WAIT receipt exists |
| `rejected` | the candidate was rejected |
| `stale` | the work generation or trigger moved before landing/commit |
| `denied` | the persisted review gate refused the attempt |
| `failed` | generation or a lifecycle step failed |
| `incomplete` | no authoritative conclusion is available |

`ReviewView` carries `state`, optional `interaction_id`/`candidate_id`, an
optional `receipt`, `redacted`, a label-only `detail` and `model_adapter`.
It never carries body text or a proposal. Body access is only
`read_experience`.

`ReviewWorkItem` exposes a managed work item:
`work_id`, `event_id`, `generation`, `blocked`, `rerun_authorization_ref`.

## 4. API

### 4.1 Common entry rule

Every public method first rejects a non-`HostAccess` argument and then refuses
to run while the store connection is inside an open transaction:

```
InvalidInput: facade methods must not run inside an open store transaction
```

The second rule protects C06c `get_result` recovery (the R1 fix): the Facade
must call it in autocommit mode, never inside a caller-owned read snapshot.

### 4.2 `preview(access, raw_output=...)`

Runs the generation parser and the frozen proposal validator, and writes
nothing. It creates no interaction, candidate, work, Belief or command, and it
calls no model. A WAIT is structurally legal without a domain body, so it is
not passed through the proposal validator.

- valid -> `structural_only`
- invalid -> `failed` with a code label (for example `INVALID_SCHEMA`,
  `UNSUPPORTED_OPERATION`)

### 4.3 `review(access, work_id=..., trigger_event_id=..., ...)`

1. Requires `review.generate`.
2. Proves the Work belongs to that Event and is still pending, using
   `unfinished_work_for_event`. A mismatch is `InvalidInput` **before** any
   Context or model call: zero model calls, zero rows.
3. Reads the persisted start gate (`review_start`). A closed gate returns
   `denied` without a model call.
4. Generates a candidate (preflight, Context, bounded model loop). A failure
   returns `failed` with `phase:code` and no candidate row.
5. Verifies the generated Context belongs to the same namespace and trigger
   event; a mismatch is `CORRUPT_STORE`.
6. Registers the review interaction with the exact trigger, work id, work
   generation and context id.
7. For a WAIT proposal: commits one `cognition.wait` through Tx B, completes
   the work in the same transaction, then marks the interaction `waiting`.
8. For every other proposal: writes the candidate body as canonical proposal
   JSON (M-A; raw-output capture is C09). The final Work liveness read and
   `register_review_candidate` run in **one** `BEGIN IMMEDIATE` transaction, so
   a concurrent completion cannot leave a candidate for a finished Work
   behind. Model generation is never inside a database write transaction.
9. `CandidateTransitionDenied` (the gate closed or the generation moved) marks
   the interaction `stale` and returns `stale`; it never fabricates a
   candidate.

The WAIT path has an in-transaction guard (`_ReviewWaitGuard`): it re-reads the
interaction binding, the trigger, the Work's pending state and the gate
generation after the three watermark checks and before `apply`. A failure rolls
the whole WAIT transaction back (no `work_progress`, no WAIT receipt) and the
Facade marks the interaction `stale`. This guard is independent of C06b's
`_NoManagedWorkGuard`.

### 4.4 `resolve(access, candidate_id=..., decision=..., decision_id=..., grant_id=...)`

- `approve` requires `review.approve`; `reject` requires `review.reject`.
- The candidate's interaction must belong to `access.host_instance`, otherwise
  `AuthorizationRequired`.
- `approve` builds the exact `Approval(candidate_id, proposal_digest,
  context_id, request_id)` from the stored candidate. A host may supply a
  commit `grant_id` instead; it can never supply its own `Approval`.
- `reject` presents no commit authority; `decision_id` is an audit label, not
  an authorization token.
- Status mapping: `committed`/`already_committed` -> `committed`;
  `rejected` -> `rejected`; `stale` -> `stale`; `failed` -> `failed`.
- The receipt goes through the disclosure rule in section 6.

### 4.5 `result(access, interaction_id=...)`

- Requires `result.read`; the interaction must belong to
  `access.host_instance`.
- Calls `get_result` (autocommit) and maps `candidate_pending` -> `pending`;
  `committed` -> `committed`; `waiting`/`rejected`/`stale`/`denied`/`failed`/
  `incomplete` keep their name.
- The receipt goes through the same disclosure rule. Hiding it never changes
  the state: a historical commit stays `committed`, a WAIT stays `waiting`.

### 4.6 `read_experience(access, ref=..., purpose=...)`

Maps `HostAccess` to `ExperienceAccess` and calls the C05c read surface
unchanged. Widening `read_experience` never widens the Facade: a suppressed
receipt for a barred or invisible candidate remains suppressed.

### 4.7 Work scans

- `legacy_work` returns pending work with **no** collaboration control. It is
  the old scan set only.
- `managed_work` returns pending work **with** a control, mapped to
  `ReviewWorkItem`.
- The two sets are disjoint and together cover `unfinished_work(namespace)`.
- `run_step` is unchanged; C06b's managed-work isolation remains in force.

## 5. Receipt disclosure rule

`_disclose_receipt` is the only place a receipt can leave the Facade.
`review()` (immediate WAIT return), `resolve()` and `result()` all call it.
It returns `(receipt, redacted)` and never changes the lifecycle state.

A receipt is shown only when **all** of these hold:

1. the call holds `result.read`;
2. the interaction names a Context;
3. the receipt Context equals the interaction Context;
4. the Context is not behind a deletion barrier (and the row exists);
5. the trigger Event is not deleted;
6. for a candidate: the candidate artifact is not barred, and the C05c read
   surface returns a readable view under the current `privacy_classes` and
   `purposes` (so every source in the recursive closure is covered);
7. for a WAIT: the receipt is exactly `cognition.wait` with no record refs,
   `review` is an allowed purpose, and every exact Context source is visible
   under the current privacy classes (events and records by their
   `privacy_class`, collaboration sources through the C05c read surface).

Any failure means `receipt=None, redacted=True`. The state still reports the
real lifecycle conclusion: access rights do not deny history, and a hidden
receipt is not a downgrade to `pending` or `failed`.

## 6. Information-protection principles

These are long-term principles; C07 records them and does not implement a full
classification engine.

- Confidentiality and usage rights do not depend on whether a model runs
  locally or in the cloud. A smaller local model is not automatically allowed
  to read more, and a frontier cloud model is not a licence to send more.
- Three user-controlled dimensions must stay separable:
  - **Read** — may PCS read the information at all;
  - **Use** — may it be used for internal inference, memory and cognitive
    work;
  - **Disclose** — may it be sent to an external model, tool, website or any
    other target.
- Ordinary information may be used for inference under ordinary
  authorization. Information a user explicitly marks as protected, and
  high-risk material such as keys and credentials, must obey stricter rules.
- `review.generate` allows starting a review; it does not allow reading
  arbitrary local files, private databases, or publishing anything.
- C07 implements no graded classification, no outbound approval flow and no
  leak detection. The three-way split is reserved for a future private
  instance; the Facade's present controls are the capability set, the privacy
  classes, the purpose label and the deletion barrier.
- `ScriptedModel` is a test stand-in, not a production adapter. A real local
  or cloud model may be configured by the trusted host, but a Lab run only
  uses synthetic data or data the user explicitly allows.

## 7. Review retry and recovery

- `review()` creates a new interaction and a fresh request id on every call.
  It does not promise end-to-end exactly-once: if a response is lost and the
  call is retried, a second model call and a second collaboration record may
  exist. This is deliberate; C07 adds no request-deduplication platform.
- Historical recovery promises one thing only: given a known
  `interaction_id`, `result()` reads and (through C06c) repairs that
  interaction's labels.
- Model text can never decide whether a candidate is committed. A body that
  says "remembered" is still `pending` until a real receipt exists.

## 8. C06 constraints restated

- The Facade never calls `get_result` inside an external open transaction.
- `decision_ref`/`decision_id` is an audit reference, not Reject authority.
- Approve/Reject always checks a `HostAccess` permission and the interaction's
  host instance.
- Model output never decides that a candidate is committed.
- Managed work never returns to the old `run_step` direct-commit path.
- Host scans skip managed/blocked work; the legacy scan and the managed scan
  cannot livelock each other.

## 9. Lab data isolation

Three directory trees are kept separate:

- **source checkout** — public code, contracts and synthetic tests only;
- **Lab** — development, synthetic experiments and trial runs;
- **future private instance** — never part of this repository or a Lab run.

On the current development machine the source checkout is the drive-root
directory `QUINTESSENCE` on drive `E`, and Lab runs live in the sibling
drive-root directory `QUINTESSENCE-Lab` on the same drive. A run therefore
uses:

```
<lab-root>/<run-id>/data/pcs.sqlite      # e.g. the E-drive QUINTESSENCE-Lab tree
```

No run artefact, temporary database, SQLite sibling file (`-journal`, `-wal`,
`-shm`) or log is written under the source checkout. `open_store` enforces the
root rule:

```python
import os
from pathlib import Path

from quintessence_pcs.collaboration import open_store

lab_root = Path(os.environ["QUINTESSENCE_LAB_DATA"]).expanduser()
store = open_store(lab_root / "run-<run-id>" / "data" / "pcs.sqlite")
```

- `QUINTESSENCE_LAB_DATA` must be an absolute path to the Lab data root; it is
  never inside the checkout.
- `open_store` resolves the target, refuses equality with or containment in the
  source checkout **before creating any directory or file**, creates the parent
  outside the checkout, and then opens/migrates the database.
- Python bytecode is a run artefact too. A Lab command that imports the
  checkout must run with `PYTHONDONTWRITEBYTECODE=1` (or `python -B`), so no
  `__pycache__` tree appears under the source checkout; `tools/run_tests.py`
  already sets this for the test suite.
- Only synthetic data enters Lab. A private-instance Persona/Memory/PCS
  database must not be copied into Lab, the repository or a commit.
- The repository itself is written by Git only. After tests or a Lab run,
  `git status --porcelain --ignored` must be empty; test logs and temporary
  output belong in the OS temporary directory or Lab.
- `tools/run_tests.py` already writes its log outside the checkout.

Absolute host paths are not committed in this repository (the repository gate
flags them), which is why this document names the drive and the two root
directory names instead of one machine path.

## 10. Known limits

- No exactly-once review; see section 7.
- No ACL, identity provider, token store or permission table.
- The Facade is review-only: a consult Context is not a committable basis and
  C07 exposes no consult entry point.
- WAIT receipt visibility is derived from the current public privacy facts of
  the exact Context sources; it is not a full content classification system.
- The in-transaction guard closes the determined race, not every possible
  concurrency schedule; C06b's gate and C06c's recovery remain the backstop.
