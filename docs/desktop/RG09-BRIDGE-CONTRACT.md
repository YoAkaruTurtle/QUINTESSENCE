# RG09 P2/P4/P8 bridge contract

Status: implemented on `rg09/p2p4p8` from base `9536187`.  This note records the
three behaviours the implementation now guarantees and the parts that stay
deliberately outside the change.

## P2 — maintenance state errors are not transport failures

`desktop.v1` `maintenance.run` used to answer **every** runtime exception with
`SIDECAR_UNAVAILABLE`.  A paused, live-leased or terminal job is not a dead
sidecar; the bridge now separates the two:

| Store outcome | Bridge error | Meaning for the host |
|---|---|---|
| `InvalidInput` / `MaintenanceConflict` (paused, live lease, terminal) | `MAINTENANCE_STATE_INVALID` | The job's business state forbids this call. Read the job, then resume/cancel it. Do not retry the transport. |
| `RuntimeStartupError` (`MAINTENANCE_FORBIDDEN`, startup/auth refusals) | the error's own code | The entry or its permission is unavailable. |
| Any other exception | `SIDECAR_UNAVAILABLE` | Unexpected bridge failure. This is not a business-state answer; do not replay the call blindly. |

The mapping lives in `pcs_bridge.Bridge._run_maintenance`; the store's messages
are fixed phrases such as `cannot lease a job in state paused`, `maintenance job
is leased by another attempt` and `maintenance job is cancelled`.

### Does a paused job block normal recovery?

- The automatic review path does **not** go through `maintenance.run`; it uses
  `maintenance.after_turn`/its own scheduler and is unaffected by this error
  mapping.
- `maintenance.run` with no `task_id` deliberately auto-resumes one paused
  `cognition.review` job (the software-restart path).  It does **not**
  auto-resume a paused `memory_delivery` job registered under
  `host:maintenance.run`; a second no-argument run hits the paused state and
  returns `MAINTENANCE_STATE_INVALID`.
- Recovery for that explicit entry is: read the job (`maintenance.status` with
  its `task_id`), then `maintenance.control` `resume` (or `cancel`), then
  `maintenance.run` with the `task_id`.  This is intentional: a paused slice
  means "stop and let the host decide", not "retry silently".

## P4 — one writer per data instance is the design

Actual implementation (verified by reading the code, not inferred):

- The Tauri host is a single application process.  A per-user named mutex
  (`single_instance`) runs before setup; a second GUI only focuses the first
  window and exits.  The host has no SQLite dependency and never opens the PCS
  database.  The WebView has no filesystem/database capability; every read and
  write goes through typed IPC to the sidecar.
- The sidecar takes an OS file lock on `<database>.lock` **before** opening
  SQLite (`DataInstanceLock`).  The lock is bound to one resolved database
  path, is released by the OS when the process exits (including abnormal exit),
  and the lock file itself is never deleted, so a stale file is harmless.
- A second sidecar on the same path never opens a write connection and reports
  `DATA_LOCKED` in `app.info` (`host.reason_code`).  Store methods it serves
  answer `DATA_LOCKED` rather than falling back to a partial host; `app.info`
  and `bridge.ping` still answer so the caller can diagnose the lock.
- A sidecar configured for a different database path gets a different lock and
  starts normally.

Conclusion: this is **design, not a defect**.  It is a desktop-instance write
coordination contract, not a PCS-global permanent exclusion and not a database
engine write lock.  No new Sidecar is added.

### Acceptance recipe

Authoritative validation that needs writes (migrations, seeded fixtures,
store assertions that mutate) **must stop the GUI** (or at least its sidecar)
first.  There is no supported second writer and the bridge has no write-audit
mode.

For a quick read-only diagnostic while the writer runs, SQLite's rollback
journal allows a short reader when no writer is mid-commit.  Use an explicit
read-only URI, `query_only`, a short timeout and close immediately:

```python
import sqlite3
connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2.0)
try:
    connection.execute("PRAGMA query_only = ON")
    print(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone())
finally:
    connection.close()
```

Do not use `immutable=1` while the writer is alive, do not hold a read
transaction, and do not treat a `database is locked` result as an empty store.
Prefer the bridge's own read methods (`app.info`, `maintenance.status`, read
review/profile methods) when the question is about host state.  The test suite
uses exactly this read-only diagnostic; it never writes beside the sidecar.

## P8 — bridge entry action-set compatibility

The Memory bridge entry is a pinned Python shim configured by
`memory_bridge.entry`.  Two known generations exist:

- older `main`-line entry: `_ACTIONS = ("capture", "search")`;
- current base / `rg08-repair` entry: adds `recall` and `time_window`.

Previously `app.info.capabilities.memory.recall.configured` only checked the
config switch and the adapter object.  A host pointed at the older entry looked
recall-capable and failed only when a turn actually tried the missing action.

The host now performs a **read-only, non-executing** check in
`entry_compat.py`: it parses the entry source with `ast`, reads the top-level
literal `_ACTIONS`, and compares it with the actions the configured surfaces
require (`capture`, `search`, plus `recall` when recall is enabled, plus
`time_window` when time retrieval is enabled).  It never imports, executes or
opens the Memory store, PCS database or a model.

`app.info.capabilities.memory.recall.entry` reports:

| Field | Meaning |
|---|---|
| `checked` | A literal `_ACTIONS` declaration was found and parsed. |
| `actions` | Declared actions (empty when unchecked). |
| `required` | Actions this configuration needs. |
| `missing` | Required actions absent from the declaration. |
| `compatible` | No required action is missing (unchecked is `true`, not a rejection). |
| `reason` | `ok`, `actions_missing`, `actions_not_declared`, `entry_unreadable`, `entry_unparsed`, `entry_too_large`, `entry_not_utf8`, `actions_not_literal`. |

`recall.configured` is now false when a declared entry is incompatible, and
`missing` contains `entry.recall` / `entry.time_window`.  Automatic recall is
not scheduled for such an entry (`memory_recall_configured` and the recall
factory check the same facts).  The ordinary chat continues; this is a degraded
recall surface, never a startup failure.

A wrapper, packaged binary or test double that declares no `_ACTIONS` is
reported as `checked=false` and is **not** called incompatible: the host cannot
prove a negative without executing it.  That keeps the check honest for the
known PCS adapter and does not break alternative entries.

### Deployment check

Before repointing a Lab or installed host configuration, run:

```text
python apps/desktop/scripts/check_memory_bridge_entry.py --config <host.json>
```

It prints the same fields (no entry path, no memory content) and exits `0`
when compatible, `2` on a configuration error, or `3` when a declared entry is
missing a required action.  Restart or `settings.reload` after changing the
entry path; `app.info` reads the compatibility at runtime startup and on the
memory-settings reload boundary.

## Verification

| Test | Coverage |
|---|---|
| `tests/desktop/test_j09_host_entry.py::HostEntrySidecarTests::test_a_paused_job_is_a_state_error_not_an_unavailable_sidecar` | paused → `MAINTENANCE_STATE_INVALID` |
| `...::test_a_cancelled_job_is_a_state_error_not_an_unavailable_sidecar` | terminal → `MAINTENANCE_STATE_INVALID` |
| `...::test_a_live_leased_job_is_a_state_error_not_an_unavailable_sidecar` | live lease → `MAINTENANCE_STATE_INVALID` |
| `tests/desktop/test_lock.py` | second sidecar `DATA_LOCKED`, release on exit, read-only acceptance query |
| `tests/desktop/test_bridge_entry_compat.py` | action-set parser, old/current entries, `app.info` gating, unknown wrapper tolerance |

Limits: the action check proves only the *declared* set in the entry source; it
cannot prove the upstream `scope_recall` package version or a dynamic wrapper.
The data-lock contract is enforced by this desktop host, not by SQLite against
arbitrary other processes.
