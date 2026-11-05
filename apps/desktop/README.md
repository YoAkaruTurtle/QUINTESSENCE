# QUINTESSENCE — Consult and Review (P0)

The Windows desktop Consult and Review workspace. It is a dark, conversational
TypeScript UI over a Tauri 2 Rust trusted host and a fixed Python PCS sidecar.
The P0 review line covers Consult, deterministic query, Review candidate
inspection/approval/rejection, and result recovery. It is a development build,
not a signed public release.

The software brand is **QUINTESSENCE** (`QUINTESSENCE · Consult`).  The AI
conversation display name is separate: it defaults to `助手`, can come from the
private host config, and can be changed in Settings.  Changing it does not touch
PCS identity, namespace, sources, authorization or Persona semantics.

## Layout

```text
apps/desktop/
  src/                 TypeScript UI, state machine and Node tests
  src-tauri/           Rust host, framing, single instance, preferences and commands
  sidecar/             Python host adapter, config, lock and bridge
  scripts/             build/install/measure/L2/scan/evidence helpers
  tests/frontend/      Node test files for the DOM-free UI logic
  public/              generated top-bar brand image (ignored)
```

## Trust boundary

- The WebView can call only `app_info`, `confirm_destination`,
  `consult_interact`, the RG02 `conversation.create/list/get/send` methods, the
  RG03 `turn.result/events/steps/cancel` methods, the RG04
  `conversation.search` and `postprocess.status/retry` methods, the RG08
  `memory.search` read-only method, the RG06
  `profile.status/read/publish/activate` methods, `bridge_ping`, the six
  `review.*` operations, the host-only `logs_read` bounded runtime-log reader,
  the host-only display-only `conversation_display_preferences` /
  `set_conversation_display_preference` pair, the host-only
  `generation_settings` / `save_generation_settings` and `web_settings` /
  `save_web_settings` pairs, and the local UI/model settings commands listed in
  `src-tauri/src/lib.rs`.
  `web_settings` is a read of `app.info.capabilities.web` plus the host's own
  writability flags; `save_web_settings` accepts one boolean and no path, key,
  allowlist or configuration document.  Neither command triggers a network
  call.
  `logs_read` takes only a fixed source id, level, substring and page size: it
  reads the host's fixed application log base and only the current instance
  subdirectory (`instances/<fingerprint>/`, or the isolated `boot/` scope when
  the host has no instance authority). It accepts no path, glob, file name or
  instance id; the WebView never reads arbitrary files.
- `conversation_display_preferences` and `set_conversation_display_preference`
  are UI-F04 display preferences, not conversation business methods.  The host
  reads and atomically rewrites only `ui-preferences.json`, scoped by the same
  F03 instance fingerprint; the WebView supplies a closed `action`
  (`rename`/`clear_title`/`archive`/`unarchive`/`restore_all`), a conversation
  id and a bounded title, never a path, namespace or fingerprint.  Archive and
  rename never call the sidecar, the store or SQL: no conversation, turn,
  message, artifact, memory or cognition record is created, updated, deleted or
  retained differently, and an archived conversation stays readable and
  sendable through the ordinary conversation methods.
- `conversation.send` returns after the host commits the accepted turn and then
  runs the model in the sidecar's single worker; the WebView polls
  `bridge.ping.busy` and reads the terminal state with `turn.result`.  A
  `client_request_id` is the durable idempotency key: the same id with the same
  text replays the existing turn, a different text is `CONFLICT`.
- `turn.events` returns only the committed, whitelisted public event DTOs.  Each
  event is committed to SQLite before it is cached in the in-memory ring, and
  the ring only adds transient live extras (commentary text, tool arguments and
  a bounded result excerpt) for an already-committed event.  `after_seq` makes a
  reconnect a pure re-read: it never reruns a tool or a model request.  When the
  ring does not hold the turn, a terminal read comes from the database.
- `turn.steps` is the on-demand tool-detail read.  It is not polled: it answers
  its bounded page only after the turn is terminal, and `BUSY` immediately while
  a turn is running, like every other database read.
- `turn.cancel` is a cooperative request handled on the reader thread: it sets
  an in-memory flag for that turn and answers at once.  The worker checks the
  flag only at step boundaries, publishes no partial assistant message, discards
  a late model result (`late_result_discarded`) and marks the open Interaction
  `incomplete`.  An unknown/terminal turn is `cancel_requested=false,
  terminal=null, reason=NOT_RUNNING`; the reader never guesses a terminal state.
- `conversation.search` is a bounded **transcript archive** read, not a memory or
  cognition query: it returns at most ten published snippets plus
  time/conversation/role/source/truncation fields from the already-stored
  transcript (v2 turns and legacy single-turn consult records).  Deleted,
  unauthorized or shorter-retention rows are filtered before return; an
  unavailable Scope Recall index cannot hide a just-made correction.
- `memory.search` is the read-only Scope Recall browse/search.  The host owns
  the pinned binding, the one configured scope, the adapter executable and the
  timeout; the WebView sends only `session_id`, `query` (an empty query
  browses), `limit` (1..20) and an opaque `cursor`.  The adapter reuses the
  upstream local-owner `MemoryManagement.list` management API and never opens
  SQLite directly, calls a model or writes.  Items carry their upstream
  `kind`/`ref`/`revision`, scope, temporal state, clipped content and declared
  sources.  The same service is exposed to the content stage as the
  `memory.search` tool, so the side panel and the model read the same data;
  the expression stage has no tools.  A disabled bridge publishes no tool and
  reports `capabilities.memory.search=false` instead of a fabricated empty page.
  Upstream memory refs are not PCS record/event citations, and a memory hit is
  never a committed PCS Belief.
- `postprocess.status` reports two independent dimensions per turn:
  `transcript_saved` and the memory delivery state (`not_configured`,
  `not_eligible`, `pending`, `outcome_unknown`, `delivered`, `needs_review`,
  `blocked`, `obsolete`).  It never returns body text.  `postprocess.retry`
  runs one bounded projection/delivery slice for a turn.  The automatic sweep
  is small (two turns/one delivery, 1.5 s capture cap) and yields when a
  foreground `conversation.send` is queued or running.
- While a turn is running, `conversation.get/list`, `turn.result`, `turn.steps`
  and `conversation.create` answer `BUSY` immediately instead of queueing a
  database read behind the turn; `turn.events` (ring hit) and `turn.cancel` stay
  available.
- `set_ai_display_name` writes only `ui-preferences.json` in the app config
  directory. It is not a `desktop.v1` business method and cannot change
  authority, namespace, Persona or destination.
- `save_model_api_key`, `delete_model_api_key`, `model_status`,
  `set_model_choice` and `test_model_connection` are trusted-host settings
  commands. The API key is stored in Windows Credential Manager, never returned
  to the WebView, and is injected only into the sidecar child environment.
- `model.test_connection` is a side-effect-free, user-triggered probe against
  the configured provider; it creates no PCS Interaction, Context, Attempt,
  Belief or Artifact.
- `profile.read` returns a **summary** only (id/version/source/asset hash/style
  summary/counts/speaker); it never returns the profile body or example text.
  `profile.publish` and `profile.activate` are host-controlled: both are
  refused unless `app.persona_control` enables them, publishing still needs a
  real `persona.review.approve` event, and a failed call never moves the active
  profile pointer.
- It cannot name authority fields (`input_refs`, `origin_kind`, privacy,
  purposes, authorization, provider, model, endpoint, namespace, policy,
  query plan, interaction/context ids).
- Namespace, host, access, privacy/purpose, retention, policy, query plan and
  destination come from a private host configuration outside this repository.
- The Python sidecar registers the user's question through C05b
  (`origin_kind="user_statement"`), builds `ConsultRequest`, and calls the
  formal `ConsultFacade`.
- C09 send gate remains the final model authorization check.
- No shell, filesystem, HTTP or database capability is granted to the WebView.

## Folder-owned instances

Settings → Instance folder → Choose folder creates an empty instance, restores a
complete instance, or safely completes known-never-created components. Existing
content is preserved; unknown history or damaged/lost initialized components stop
connection. Persona is optional. Model service, credentials and authorization
must still be configured. See [instance operations](../../docs/desktop/INSTANCE-FOLDERS.md).

## Private host configuration

The host configuration is private and lives outside the checkout. Start the app
with `QUINTESSENCE_DESKTOP_CONFIG` pointing at it (or pass
`quintessence-desktop --config <path>`). A user-supplied `desktop-host.json` in
the app-local data directory is treated as an advanced external configuration
and is never overwritten.

On a fresh public install with neither file, the Rust host creates
`desktop-managed-host.json` and `desktop-settings.json` under the app-local data
directory. It exposes a neutral production profile, a single DeepSeek
destination, and the C07/C09 paths; the sidecar provisions only a generic
profile and the matching model authorization. No private name or Persona asset
is embedded.

Settings exposes DeepSeek model choice (`deepseek-flash` or
`deepseek-v4-pro`) and an API key stored in Windows Credential Manager. Saving,
changing or deleting the key restarts the sidecar so `app.info` and the
destination identity reflect the new state. “API Key saved” is intentionally
separate from “real connection tested”. The test-connection button makes one
explicit real API request and may incur a small charge.

Schema (`desktop.host.v1`, strict; unknown keys are refused):

```json
{
  "schema": "desktop.host.v1",
  "app": {
    "profile": "production",
    "label": "QUINTESSENCE",
    "ai_display_name": "助手"
  },  "store": {"database": "${QUINTESSENCE_DESKTOP_DATA}/pcs.sqlite"},
  "host": {
    "namespace": "example.instance",
    "host_instance": "host:desktop",
    "operator": "operator:owner",
    "access_ref": "authorization:host:desktop",
    "privacy_classes": ["private"],
    "purposes": ["consult", "reasoning"],
    "disclose": true,
    "retention": "standard",
    "policy_ref": "policy:collab.v1",
    "input_privacy_class": "private"
  },
  "consult": {
    "query_plan": {
      "kind": "belief_current",
      "claim_types": ["capability"],
      "scope": {"scene": "example.scene"},
      "limit": 5
    },
    "max_question_chars": 8000
  },
  "destination": {
    "provider": "example-provider",
    "model": "example-model",
    "model_protocol": "collab.v1",
    "external": true,
    "manifest": {
      "provider": "example-provider",
      "base_url": "https://example.invalid/v1",
      "model": "example-model",
      "api_key_env": "EXAMPLE_MODEL_KEY",
      "max_calls": 64,
      "max_output_tokens_per_call": 512,
      "max_wall_seconds": 120,
      "thinking": "disabled",
      "temperature": 0
    }
  }
}
```

- `store.database` and `destination.manifest_path` support `${VAR}` expansion;
  they never contain secrets.
- `external=true` derives `confirmation_required=true`. Local destinations do
  not ask for confirmation.
- The `lab` section and the `scripted` transport are Lab-only and refused for
  the `production` profile.
- A production config must reference an existing store with an active model
  authorization matching the manifest; the desktop app never grants one.
- The model credential is read from the environment variable named by the
  manifest. It is never written to the config, DB, logs or package.

### Optional persona keys (RG06)

All three are optional and additive; an existing config keeps working
unchanged.

```json
{
  "app": {
    "profile": "lab",
    "persona_expected": {"profile_id": "example.persona", "profile_version": "1"},
    "speaker": {"identity": "owner", "display_name": "Synthetic Owner"},
    "persona_control": {"publish": false, "activate": false}
  },
  "conversation": {
    "persona_examples_max_items": 2,
    "persona_examples_max_bytes": 2048
  }
}
```

- `app.persona_expected` is a **startup/restart contract**, not a per-turn pin.
  If it names a profile that is not active (or a different id/version, or an
  approval event that no longer exists) the host starts `HOST_NOT_READY` with
  `EXPECTED_PERSONA_UNAVAILABLE` naming that profile and makes **zero model
  calls**; it never creates or falls back to a neutral persona. A later
  `profile.activate` is a controlled management action: the next turn uses the
  new active profile, earlier turns keep their T0 snapshot.
- `app.speaker` is the host-supplied current speaker (owner / known_other /
  unknown). It is rendered as a separate Context `speaker` section and is never
  inferred from message text; `identity` absent means the model must treat the
  speaker as unknown. It is required whenever `persona_expected` is set.
  `display_name` is refused for `unknown`.
- `app.persona_control` defaults to `false/false`; `profile.publish` and
  `profile.activate` answer `PERSONA_CONTROL_DISABLED` without it.
- `conversation.persona_examples_*` bound the per-turn expression-demo budget.
  Examples are rendered only into `collab.v2` chat Contexts, are marked
  `expression_demo` / `not_experience`, and are never memory, Episode or Belief
  sources. When the Context byte cap is tight the examples are dropped (and
  recorded as `examples_omitted`) before any core persona text.

## AI display name

Settings (gear icon) exposes the AI conversation display name. The value is
stored in `ui-preferences.json` in the application config directory, outside
the repository and outside the installed package. The UI shows the preference
when set, otherwise `app.ai_display_name` from the private host config, otherwise
the neutral default `助手`.

The display name is plain text and only affects UI labels. It does not alter
PCS internal identity, namespace, source identity, authorization, Persona
semantics or the model destination. It is not written to the PCS store.

The neutral public default is `助手`; valid saved instance names are preserved. “恢复默认”
sets an explicit neutral choice even when a private host config provides a
different default.

## Web access (RG08)

Settings exposes the real `web.search` / `web.fetch` surface.  The status comes
from the sidecar's `app.info.capabilities.web`: switch, search/fetch flags,
credential presence (the **environment variable name**, never the key),
allowlist count and policy, `configured`, `missing`, and the configuration
source (`managed`, `external` or `lab`).  `configured` requires the switch, a
present credential and at least one tool; an enabled surface without a
credential is shown as “已开启 · 缺少凭据”, never as available.

The toggle is a real writer, not a UI boolean.  A managed install stores
`web_enabled` in `desktop-settings.json` and regenerates the managed host
config; every other field of an existing `web` block (allowlist, timeout,
capture mode) survives, and the managed block points `api_key_env` at the same
environment variable the host injects the application credential into.  An
external or Lab config is atomically rewritten in place, touching only
`web.enabled`.  Both paths then call `settings.reload`: the change applies to
the next turn and never interrupts a running turn.  A read-only config offers
no switch and explains what its owner must edit.

No confirmation is added before a web query, and this surface does not change
tool permissions, the fetch allowlist, the destination or the egress policy.
The key is never rendered, logged or written to a host config.

## Development

Requirements: Node 24, pnpm 11, Rust 1.90+ for the locked dependency graph (validated with 1.99.1), Python 3.12 (dev only), Tauri CLI
2.12, WebView2 Runtime, Pillow (only for brand asset generation).

```text
pnpm --dir apps/desktop install
pnpm --dir apps/desktop build
pnpm --dir apps/desktop test
cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --features host-tests
python tools/run_tests.py
```

`pnpm dev` starts Vite; `pnpm tauri dev` starts the shell. In debug builds the
Rust host can run the sidecar from the source checkout or from
`QUINTESSENCE_DESKTOP_SIDECAR_DIR`; release builds accept only a complete
one-folder bundle under the install directory.

For the validated packaged-sidecar baseline, use CPython 3.12.10, PyInstaller
6.16.0 and Scope Recall 3.14.1 from its pinned upstream revision; the core's wider
Python requirement does not certify a different native distribution. Install the
upstream project outside this source tree, never vendor its repository here.
The build also needs locked pnpm dependencies and Cargo sources available for
original-license collection. `collect_licenses.py` produces a complete original
notice payload and fails on unreviewed version or notice changes.

Build products belong outside the checkout. The scripts default to
`<repo>/../QUINTESSENCE-Lab/build/c12b` and accept `-LabRoot`, `-RepoRoot`,
`-CargoTarget` and `-Python` overrides. The Tauri resource mapping is injected
from the Lab build path at bundle time, so `tauri.conf.json` contains no
machine-specific path.

Generated icons are written by `build.rs` into `src-tauri/icons/` (ignored)
and are not committed. A formal build first runs
`scripts/make_brand_assets.py` against the owner-supplied original image
(`QUINTESSENCE_BRAND_ICON` or `<lab>/build/c12b/brand/quintessence-icon.png`),
producing installation icons and `public/brand.png` by proportional downscale
only. The source artwork is not in the public repository; public distribution
rights must be confirmed by the owner. A clean checkout without the artwork
gets a neutral fallback icon from `build.rs`.

### Complete development package

From a PowerShell environment authorized to run local project scripts, choose
an external build directory and prepare its Python venv with the dependencies
above (Pillow is needed for custom artwork only). Then:

```powershell
$RepoRoot = (Get-Location).Path
$BuildRoot = Join-Path (Split-Path $RepoRoot -Parent) 'quintessence-build'
$BuildPython = Join-Path $BuildRoot 'build/c12b/venv/Scripts/python.exe'
pnpm --dir apps/desktop install --frozen-lockfile
./apps/desktop/scripts/build_sidecar.ps1 -RepoRoot $RepoRoot -LabRoot $BuildRoot -Python $BuildPython
./apps/desktop/scripts/build_app.ps1 -RepoRoot $RepoRoot -LabRoot $BuildRoot -AllowFallbackIcons
```

The scripts emit a complete sidecar and NSIS development installer outside the
checkout. `-AllowFallbackIcons` selects neutral generated icons when private
artwork is absent. A bare `cargo build --release` host does not include the
sidecar resources. This is a tested local packaging path, not a clean-machine,
signed release or cross-platform support claim. No policy-changing command is
required by these instructions; use your own authorized development environment.

## Lab verification

```text
scripts/build_sidecar.ps1
scripts/build_app.ps1
scripts/verify_install.ps1          # install, screenshots, store, single instance, lock, uninstall
scripts/measure.ps1                 # 5 runs, per-process working set/private
scripts/measure_idle_nocdp.ps1      # idle without CDP instrumentation
scripts/l2_probe.ps1
scripts/detect_webview2.ps1
scripts/check_memory_bridge_entry.py  # read-only action-set check for the configured Memory bridge entry
```

The verification harness drives the real installed UI over the WebView2
DevTools protocol. The remote-debugging port is enabled by the verification
environment only; it is not a packaged feature and no test hook is compiled
into the app.

`scripts/make_lab_host.py` generates a synthetic Lab config. It is never a
production default.

## Desktop protocol and errors

`desktop.v1` frames are a 4-byte big-endian length plus UTF-8 JSON (1 byte to
1 MiB). stdout is protocol-only; stderr is bounded diagnostics and never
contains a question, reply, prompt, database path or credential.

Bridge-local errors: `INVALID_REQUEST`, `INVALID_JSON`, `INVALID_PARAMS`,
`DUPLICATE_REQUEST_ID`, `AUTHORITY_FIELD_REJECTED`, `METHOD_NOT_FOUND`,
`RESERVED_NOT_IMPLEMENTED`, `BUSY`, `INTERNAL_ERROR`, `HOST_NOT_READY`,
`DATA_LOCKED`, `SESSION_MISMATCH`, `CONFIRMATION_REQUIRED`,
`DESTINATION_MISMATCH`, `INPUT_REGISTRATION_FAILED`,
`MAINTENANCE_STATE_INVALID`. Host-side failures add `TIMEOUT`, `SIDECAR_EXIT`,
`PROTOCOL_ERROR`, `SIDECAR_IO`, `SIDECAR_UNAVAILABLE`.

`maintenance.run` reports a paused/leased/terminal job as
`MAINTENANCE_STATE_INVALID`, not `SIDECAR_UNAVAILABLE`; resume or cancel the
job from `maintenance.status`, then rerun with its `task_id`.  The single-writer
data-lock contract and the Memory-bridge entry action-set check are documented
in [`docs/desktop/RG09-BRIDGE-CONTRACT.md`](../../docs/desktop/RG09-BRIDGE-CONTRACT.md).

Timeout/crash/truncation is `result_unknown=true`: the UI shows “结果未知”,
never auto-retries and never reads a local cache as an authoritative result.
There is no `consult.result`/history/resume in C11; refresh/restart cannot
restore a previous Consult.

## Known gaps

- Candidate detail and approval eligibility are exposed through C12c; C06
  remains the final commit authority. A candidate that violates the claim
  freeze is shown as not approvable, not silently mutated.
- Attempt details and the complete Context source list have no public read
  surface; the drawer marks them unavailable rather than querying the DB.
- L2, WebView2-missing, clean-machine/offline-install verification, and release
  signing remain UNVERIFIED or unfinished.
- The development installer is unsigned.
- The app is not PCS-global-exclusive: the single-instance and data locks
  coordinate one desktop instance with one data instance, and leave room for a
  future explicit lease path.
