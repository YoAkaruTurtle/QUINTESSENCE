# UI v0.2 implementation and verification

This delivery continues the settings supplement and applies the supplied v0.2
design to the real desktop client. The source baseline is `fc55b62`; the aligned
working tree is committed on `rg/persona-chat` (not pushed). It is not a release
or a claim of complete backend capability.

## Implemented experience

- A 244px sidebar with the original brand asset, real recent conversations,
  new conversation, chat, dashboard, terminal, Review and settings. Desktop
  collapse and bottom-panel height persist in host-owned UI preferences.
- Neutral white/yellow and charcoal/red themes, quiet industrial lines, corner
  markers, original icon container, a 76px header and a centered reading column.
- A full settings page. Finite language/theme/model/SDRPE choices use keyboard
  accessible radio segments, never a native operating-system popup. Existing
  save, validation, capability and next-turn semantics remain authoritative.
- An uncluttered composer with “今天想聊些什么？” and an upward send icon.
  During a live turn the same location offers a stop icon. The count, shortcut
  text and source button are removed; sources/details live in the dashboard.
  Enter sends; Shift+Enter inserts a line; composition Enter never sends.
- Conversation drafts and reading positions survive page/conversation switches.
  New conversation creation is serialized and preserves the old draft on failure.
  Recent-conversation metadata refreshes after creation and completion.
- Public progress, tools and published answers are grouped by real turn identity.
  Explicit process/tool expansion survives polling. Reading old content does not
  force scrolling; new activity has a separate jump control.
- Dashboard cards use typed, scoped selectors: instance readiness, loaded
  conversation-processing states and loaded cognition candidates. Ineligible
  processing states do not inflate the delivery denominator. Unknown is never
  presented as zero; only an authoritative empty conversation shows zero usage.
- A resizable bottom panel with selected-conversation summary and an explicit
  unavailable state for runtime logs. No arbitrary local file reader, shell,
  fabricated log entries or paid connection probe is introduced.
- Review retains its existing actions and authorization rules. Its old viewport
  spacer and dark sticky gradient are removed. The empty state and heading use
  the new page system rather than exposing implementation metadata by default.

## Reported defects and their causes

The old transcript search overlay was absolutely positioned inside the chat
column. It therefore covered only that column and appeared shifted to the right.
Its panel/input/result colors were fixed cream colors, while text inherited the
new dark-theme foreground. Changing global variables could not repair those
hardcoded colors.

Search now mounts in a root-level modal host, covers the viewport and centers a
720px responsive panel. Every surface and text color uses theme tokens. Background
navigation is inert while open, focus stays in the dialog, Escape closes it, and
focus returns to the invoking control. Result updates preserve input focus and
selection. Close animation is visual only: interaction is disabled immediately.

Review used `min-height: calc(100vh - ...)`, `margin-top: auto` and an old dark
gradient action bar. Those rules produced the oversized blank area and bottom
stripe. Its action bar now appears only when useful actions exist, uses the
surface token, and no longer stretches the page around an empty state.

## Motion

Page changes have a 230ms eased entrance; sidebar width changes take 230ms;
selection pills slide for 180ms; modal entry takes 230ms and closure 140ms;
details expand in 180ms; real ring/bar data enters in 450–500ms. Dashboard cards
have short staggered entrances and buttons have restrained press/hover feedback.
There are no fake progress loops, delayed answers or animation-driven business
state transitions. Reduced-motion disables CSS effects and bypasses page/modal
Web Animations API effects.

## Verification

- `npm test`: 65 passed, including unknown/zero distinctions and rejection of
  processing data from another conversation.
- `npm run build`: TypeScript and Vite passed.
- Repository gate: 431 files, 177 commits and 1143 historical blobs passed.
- Rust library: 21 passed; host integration: 10 passed. A workspace-local test
  temporary directory avoids the existing low-integrity system-temp restriction.
- Python full suite: 1942 tests, 9 skipped, no failures, 676.097 seconds. Run once;
  later changes were frontend/style-only. The shared checkout also contains
  independently edited Thinking-manifest tests; they were preserved.
- After that full run the managed Thinking default was changed to enabled
  (`THINKING-DEFAULT-01`). That change was verified by targeted manifest/collab
  tests (33), Rust library/host tests and one real `deepseek-flash` on-mode
  content+expression smoke; the 1942-test full run was not repeated.
- `ui_v02_probe.mjs` exercises a real Tauri WebView2 through its test-only CDP
  endpoint against an explicitly checked synthetic Lab destination. It covers
  four language/theme combinations, actual framed synthetic sending, draft
  retention, IME, radio keyboard behavior, Review spacing, viewport modal bounds,
  light/dark modal backgrounds, Escape/inert behavior, mobile navigation, 1024px
  and 390px overflow, 200% equivalent viewport and reduced motion.
- Evidence is generated under the ignored desktop `src-tauri/target/ui-v02-evidence`
  directory: PNGs and `checks.json`. No test hook is included in the production
  binary and no paid model request is required for these visual checks.

The native capture helper returned `foreground window did not report a process
id` twice. Verification therefore used the repository's existing real-WebView2
CDP approach, rather than presenting browser mocks as desktop evidence.

## Explicit limits

The host still has no complete request-level usage/window/throughput endpoint,
safe bounded runtime-log reader, complete persona list/import/version API or
preference-management API. The UI reports those gaps and preserves existing
capability gates. No token estimates, false connection health percentages or
sample values from the design HTML are shipped. Some legacy technical detail
and error wording still needs full English editorial coverage; conversation
content, proper names and technical originals are never translated.

No migration, collaboration-v1 contract change, permission widening, push or
release is part of this UI delivery. Revert the UI changes and rebuild to roll
back; preserve application preferences and the selected instance configuration.
