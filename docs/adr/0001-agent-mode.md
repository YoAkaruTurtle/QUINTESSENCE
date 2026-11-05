# ADR 0001 — Controlled agent mode (relaxing the `tool-free` promise)

**Status:** accepted (J07, M1)
**Date:** 2026-10-27
**Supersedes:** the blanket "PCS2-009 is tool-free" rule, narrowly and only for the new agent mode.

## Context

PCS2-009 deliberately forbade tool machinery everywhere. The rule is not
incidental; it is enforced in three independent places:

| Place | Behaviour |
|---|---|
| `runtime/manifest.py:253-264` | `build_request_body` walks the body and refuses `tools` / `functions` / `tool_calls` / `function_call` |
| `runtime/collab_protocol.py:561-563` | the collaboration request builder applies the same refusal |
| `runtime/http_model.py:1211-1218` | a response that carries `tool_calls` (or `function_call`, at message **or** choice level) is treated as `TOOL_CALLS_PRESENT` and unusable |

The provider itself supports tool calling; the restriction is self-imposed.
That distinction matters: without it, a later reader would conclude the model
cannot call tools, when in fact PCS chose not to ask it to.

J07 needs the model to choose a tool, see a real result, and continue. That is
unavailable under the blanket rule, so the rule must change — narrowly.

## Decision

Introduce a second request-building mode:

- `ModelManifest.tool_mode` is `"tool_free"` (default) or `"agent"`.
- `build_agent_request_body` accepts `tools` **only** when
  `manifest.tool_mode == "agent"`; every other mode keeps the original refusal.
- `build_request_body` (the proposal path) is untouched: it still rejects tool
  keys unconditionally, whatever the mode. A manifest in agent mode does not
  silently gain tools on the proposal path.
- `DirectHttpModel.step()` validates responses through a mode-aware validator:
  in agent mode `tool_calls` is accepted; everywhere else the original
  `TOOL_CALLS_PRESENT` failure stands.

## What is preserved

1. **No hidden prompt.** The agent path sends exactly the messages the caller
   supplies; there is no system prompt, no few-shot example, and no injected
   instruction. The first message is still the caller's user message.
2. **The model cannot certify itself.** Authority, identity and permission
   fields (`authority_kind`, `approval_id`, `auto_authorization_id`, `scope`,
   `actor`, `namespace`) are never taken from the model. The tool catalog
   strips them from arguments and the host injects the real values.
3. **No second submission authority.** No tool exposes `commit`; a change can
   only be proposed, and the J06 governance path decides.
4. **Reasoning content is not user-visible.** `reasoning_content` is echoed
   back to the API on later rounds (the protocol requires it when `tools` is
   present) but is never rendered to the user.

## Consequences

- Manifest hashes change for any manifest that adds `tool_mode`, and old
  manifests that lack the field hash differently. Configuration written for
  PCS2-009 still parses (`tool_mode` defaults to `"tool_free"`), but its hash is
  not identical to a re-serialised copy — treat hashes as opaque, not portable.
- The proposal path keeps its guarantee, so `TOOL_FREE` behaviour is unchanged
  for every existing caller and every existing test.
- Rollback: set `tool_mode` back to `"tool_free"`; the agent builder then
  refuses `tools` again and `step()` cannot be used. No migration is involved.

## Alternatives rejected

- **Delete the restriction outright.** Rewrites a deliberate safety decision and
  removes the guarantee for paths that never asked for tools.
- **Simulate tools by parsing natural language** ("please look up X"). It hides
  the real problem behind a parser and cannot be audited as a tool call.
