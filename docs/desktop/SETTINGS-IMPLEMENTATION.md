# Desktop settings supplement

Baseline: `rg/persona-chat`, `fc55b622eac206c9a495961f1d4fadd15965a2e0`.
Scope: the settings supplement, integrated into the existing settings drawer.
This is not acceptance of the complete v0.2 visual redesign, dashboard or logs.

## Inventory and behavior

| Setting | Existing authority | Persistence | Effect |
| --- | --- | --- | --- |
| Interface language | New fixed UI-preference commands | Existing application `ui-preferences.json` | Immediate after successful save |
| Light/dark theme | Same application preferences | Same file; preserves display-name preferences | Immediate |
| AI display name | Existing `set_ai_display_name` | Application preferences | Immediate |
| SDRPE | Existing `conversation.sdrpe.mode` parser; new host save/reload | Selected host config; managed settings mirror for generated config | Next turn |
| Model | Existing managed model allowlist | Existing `desktop-settings.json` and generated host config | Next turn |
| Maximum output tokens | Existing manifest field | Managed settings and generated config | Next turn; budget and authorization caps still apply |
| Persona | Existing `profile_activate` and exact published record version | Existing instance profile activation | Next turn; current turn keeps its pinned profile |
| API key | Existing Windows Credential Manager entry | Credential Manager only | Application restart required |
| Web access | Existing `web` block parser; new host read/write pair | Managed settings + generated config, or the external/Lab config atomically | Next turn; a running turn keeps its configuration |

Temperature is fixed at zero by the supported non-thinking manifest contract.
Top-p is not implemented. Neither is offered as an editable control. Existing
experimental Thinking protocol configuration is not promoted to a general option.
Internal tool limits, budgets, namespace, permissions, policies, paths and protocol
switches are not exposed as settings. There is no auto option. A legacy auto
configuration explicitly reports unsupported/effective off and requires an
explicit off/on choice. A v1 host also explains its effective off behavior.

External model manifests retain their existing host-managed model and output
limits; the UI does not grant new external-model authorization. Their SDRPE mode
can be saved through the selected host-config path without editing files by hand.
Persona activation appears only with the host capability enabled. There is no
invented profile list: the supported operation accepts an existing published ID
and exact record version, with confirmation and the original host validation.

## Cause and implementation decision

The previous `set_model_choice` saved configuration and immediately shut down
the sidecar. Key save/delete did the same. This could interrupt a live turn and
trigger startup interruption recovery, contradicting next-turn semantics.

Generation changes now use `save_generation_settings` in the trusted Rust host.
It validates a closed field set, serializes concurrent saves, and writes complete
JSON through a same-directory temporary file and rename. For managed instances,
the settings file is authoritative across launches; failure to update its generated
config attempts rollback and reports an unknown persistence state if rollback fails.

The fixed host-only sidecar operation `settings.reload` reads only its startup
config path and accepts no renderer payload, path or credential. It queues on the
existing single DB worker after the current job. While pending, new business
actions receive BUSY; public status/events and cancellation remain available.
The reload rejects changes to instance, authority, budgets, tools or persona
contracts. It does not reopen the store or run startup recovery. An SDRPE-only
change keeps model adapters; a changed manifest rebuilds the existing model,
Consult, Review and conversation adapters. Only existing managed provisioning
policy can authorize a changed managed manifest. External authorization is not
created. Destination identity changes still invalidate the previous confirmation.

Saved and applied are separate results. The UI reports an unconfirmed application
instead of treating an IPC failure as success. It shows persisted form values and
the actual model/effective SDRPE separately. Chat polling preserves the mounted
settings form and pending save feedback. Language and theme do not touch records,
conversation text, persona or generation settings. Keys never enter JSON settings
or a read-back DTO; a credential change blocks testing until restart so an old
in-process credential cannot falsely validate a newly saved one.

## Verification

- Real framed sidecar tests in `tests/desktop/test_settings_bridge.py`: stable
  session, persisted mode, refusal of auto/authority/path changes, invalid JSON
  preserving the runtime, output-limit destination changes, and a running request
  completing before reload.
- Configuration/expression/BUSY suite: 69 passed.
- Settings/profile/authority suite: 32 passed (including the six new settings tests).
- Frontend: 62 existing tests passed; TypeScript/Vite build passed.
- Rust: 21 library and 10 host tests passed. Library tests used a workspace-local
  temporary directory because the sandboxed executable cannot access the normal
  system temporary directory. Preference round-trip assertions include language
  and theme; generation validation checks auto and out-of-range token limits.
- Final repository gate: 422 files, 177 commits and 1143 historical blobs passed.
- A real Tauri/WebView2 Lab window was opened using the existing normal-integrity
  launcher. The settings controls, capability limitations and v1 effective-off
  explanation were inspected. Saving light mode persisted across a WebView reload.
  Visual inspection found hardcoded dark drawer/composer backgrounds; those now
  use theme variables. No model connection test or real provider request was run.

## RG08 web settings supplement

`app.info.capabilities.web` reports the real surface: `enabled`, `configured`,
`declared`, `search`, `fetch`, `credential_present`, `credential_env` (a name,
never a key), `allowed_hosts` count, `host_policy`, `source`
(`managed`/`external`/`lab`) and `missing` (`web`, `web.enabled`,
`web.api_key_env` or `config`).  `configured` requires the switch, a non-empty
named environment variable and at least one tool; an enabled surface without a
credential is shown as “已开启 · 缺少凭据”.

`web_settings` adds only host-side facts (`managed`, `editable`,
`credential_restart_required`, `host_ready`, `apply: next_turn`).  It performs
no network call and returns no path, key or full configuration.
`save_web_settings(enabled)` is the real writer:

- managed: `web_enabled` is stored in `desktop-settings.json`, the managed host
  config is regenerated and the existing `web` block's other fields (allowlist,
  timeout, capture mode) are preserved; `api_key_env` points at the same
  environment variable the host injects the application credential into;
- external/Lab: only `web.enabled` is changed, through the existing atomic
  writer, and every other key in the file survives;
- both paths call the existing `settings.reload` on the single worker, so the
  next turn sees the change and a running turn keeps its runtime.  A read-only
  external file offers no toggle and names what its owner must edit.

No confirmation was added before web queries, and tool permissions, the fetch
allowlist, the destination and egress policy are unchanged.

Verification for this supplement: `tests/desktop/test_web_settings_bridge.py`
(real sidecar: unconfigured, enabled-without-key, present credential without
secret leakage, managed source, reload enabling the next turn's tool runtime);
Rust library tests (managed writer ownership/preservation, external atomic
writer, status composition); frontend tests for the pure status mapping; and a
real Tauri/WebView2 window driven over CDP.  The window showed “未配置”, then a
real toggle wrote `{"enabled": true}` to the Lab config and the panel changed to
“已开启 · 缺少凭据”; the managed window showed “可用” with the credential
environment name and “1 个” allowlisted host.  Enabling, disabling and a restart
round-trip were verified against both config shapes; the managed rewrite
preserved `allowed_hosts`/`timeout_seconds`, and the original application data
files were restored afterwards.  No real web or model request was made.

## Remaining work for full UI acceptance

The settings supplement does not implement the complete v0.2 AppShell, dashboard,
metrics, log viewer or unavailable persona-list/import/version APIs. The new
settings sections have matching Chinese/English dictionaries and primary existing
UI labels use an exact-label dictionary; legacy errors, details and some existing
UI copy still need complete English coverage. This is not full internationalization
acceptance. The full four-combination screenshot matrix, narrow-window/zoom and
all scripted desktop workflows remain part of the separate complete UI delivery.

No schema migration, v1 collaboration-contract change, push or release is included.
Rollback the source changes and rebuild; before downgrade, retain a copy of host
configuration and application preferences. Existing parsers tolerate the added
preference/settings fields, but old UI model/key commands regain restart behavior.
