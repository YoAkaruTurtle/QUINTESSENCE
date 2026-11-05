# 08 — Permissions and the maintenance boundary

**Direction:** trusted host → integration layer. The maintenance capability is
an execution credential; it is not a model capability.

## Two surfaces

| Surface | Caller | Authorization carrier |
|---|---|---|
| normal memory surface | PCS runtime and review code | `TrustedContext` (binding, session, scopes, actor origin) |
| maintenance surface | isolated host maintenance component | `InstallationMaintenance` held next to the maintenance object |

`MemoryManagement` is constructed with `MemoryManagement(core, authority)` and
the authority must be an `InstallationMaintenance`. Maintenance context
requires `allowed_scope_ids == binding.scope_ids` and an actor origin of
`human_direct` or `host_generated`.

## Boundary rules

1. Maintenance capability is constructed only by the host maintenance
   component. It is never reachable from a model request, a model output, a
   candidate DTO, or a prompt.
2. Normal reads and writes use `TrustedContext` only. There is no side channel
   that widens a caller's scopes or origins.
3. The maintenance authorization is on **scope**, not on project. A maintenance
   context with the same scopes can plan objects of another project. The
   integration layer must compare the complete affected set with its own
   authorization; upstream will not do it.
4. Disabling a model or adapter entry point is not the same as removing the
   authority check. The check stays.
5. A maintenance call is never issued on behalf of a model. The model may
   propose; the host decides; the maintenance component executes.

## Failure semantics

| Situation | Behaviour |
|---|---|
| maintenance authority absent or wrong type | construction fails; no maintenance call |
| scope set mismatch | `ACCESS_DENIED`; nothing written |
| affected set crosses the authorized range | stop; `needs_review`; no apply |
| model DTO attempts to carry maintenance access | refuse at the boundary |

## Evidence

`g0-deletion-recovery/evidence/s6-edges.json` records that a maintenance
context using another project still plans the original project's object when
the scope matches. That is the measured reason for rule 3.
