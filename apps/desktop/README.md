# QUINTESSENCE — Consult (C12b)

The Windows desktop Consult workspace. It is a dark, conversational TypeScript
UI over a Tauri 2 Rust trusted host and a fixed Python PCS sidecar. C12b covers
Consult only; Review is a C12c placeholder.

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
  `consult_interact`, `bridge_ping` and the local UI preference command
  `set_ai_display_name`.
- `set_ai_display_name` writes only `ui-preferences.json` in the app config
  directory. It is not a `desktop.v1` method and cannot change authority,
  namespace, Persona or destination.
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

## Private host configuration

The host configuration is private and lives outside the checkout. Start the app
with `QUINTESSENCE_DESKTOP_CONFIG` pointing at it (or pass
`quintessence-desktop --config <path>`). The default path is
`desktop-host.json` in the app-local data directory; without a configuration the
app starts not-ready and sending stays disabled.

Schema (`desktop.host.v1`, strict; unknown keys are refused):

```json
{
  "schema": "desktop.host.v1",
  "app": {
    "profile": "production",
    "label": "QUINTESSENCE",
    "ai_display_name": "助手"
  },
  "store": {"database": "${QUINTESSENCE_DESKTOP_DATA}/pcs.sqlite"},
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

## AI display name

Settings (gear icon) exposes the AI conversation display name. The value is
stored in `ui-preferences.json` in the application config directory, outside
the repository and outside the installed package. The UI shows the preference
when set, otherwise `app.ai_display_name` from the private host config, otherwise
the neutral default `助手`.

The display name is plain text and only affects UI labels. It does not alter
PCS internal identity, namespace, source identity, authorization, Persona
semantics or the model destination. It is not written to the PCS store.

## Development

Requirements: Node 24, pnpm 11, Rust 1.77+, Python 3.12 (dev only), Tauri CLI
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

## Lab verification

```text
scripts/build_sidecar.ps1
scripts/build_app.ps1
scripts/verify_install.ps1          # install, screenshots, store, single instance, lock, uninstall
scripts/measure.ps1                 # 5 runs, per-process working set/private
scripts/measure_idle_nocdp.ps1      # idle without CDP instrumentation
scripts/l2_probe.ps1
scripts/detect_webview2.ps1
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
`DESTINATION_MISMATCH`, `INPUT_REGISTRATION_FAILED`. Host-side failures add
`TIMEOUT`, `SIDECAR_EXIT`, `PROTOCOL_ERROR`, `SIDECAR_IO`,
`SIDECAR_UNAVAILABLE`.

Timeout/crash/truncation is `result_unknown=true`: the UI shows “结果未知”,
never auto-retries and never reads a local cache as an authoritative result.
There is no `consult.result`/history/resume in C11; refresh/restart cannot
restore a previous Consult.

## Known gaps

- Review (C12c) is a placeholder: no candidate enumeration, diff, approve or
  reject.
- Attempt details and the complete Context source list have no public read
  surface; the drawer marks them unavailable rather than querying the DB.
- L2, WebView2-missing and offline-install verification remain UNVERIFIED.
- The development installer is unsigned.
- The app is not PCS-global-exclusive: the single-instance and data locks
  coordinate one desktop instance with one data instance, and leave room for a
  future explicit lease path.
