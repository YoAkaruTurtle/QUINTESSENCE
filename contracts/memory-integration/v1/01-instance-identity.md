# 01 — Instance and identity mapping

**Direction:** PCS instance → upstream `InstanceBinding`. One PCS instance talks
to exactly one upstream installation; the binding is configuration owned by the
trusted host, never by a model.

## Fields

| Upstream field | Meaning | PCS obligation |
|---|---|---|
| `agent_id` | product-level agent label | constant per PCS deployment profile |
| `installation_id` | unique installation identity | generated once per instance root; never reused after a rebuild |
| `data_directory` | absolute path of the instance data root | must be the host-approved instance root; validated before start |
| `scope_ids` | the scopes this installation owns | the complete set; maintenance requires exact equality |
| `installation_kind` | deployment kind | declared by the host, not inferred |

## Mapping

- One PCS instance root maps to one `data_directory` and one PCS `namespace`.
  Two instances must never share a directory, an `installation_id`, or a
  namespace.
- The PCS `namespace` is the only identity axis exposed to domain code; the
  upstream `installation_id` is configuration and stays in the integration
  layer.
- The binding is immutable for the lifetime of an instance. Changing it is a
  new instance, not an edit.

## Invariants

1. `data_directory` is an absolute, host-verified path inside the approved
   instance root. It is never the repository, a temporary directory, or a
   fallback location.
2. `scope_ids` is a set with no duplicates, and every scope belongs to the same
   installation.
3. A missing, unreadable, or unverifiable instance root stops the instance from
   starting. There is no silent fallback.
4. Maintenance execution requires `allowed_scope_ids == binding.scope_ids`.
   This is an equality check on the whole set, not a subset check.

## Failure semantics

| Situation | Behaviour |
|---|---|
| binding does not match the stored installation | refuse to open the instance |
| unreadable or missing `data_directory` | refuse to start; no fallback |
| scope set mismatch during maintenance | `ACCESS_DENIED`; nothing is written |
| two instances share a directory | configuration error; stop before any write |

## Evidence

`SCOPE-RECALL-INVESTIGATION.md` §2 and the G0 isolation runs use one binding per
instance and demonstrate that cross-scope access is refused. Measured values are
recorded in `g0-deletion-recovery/evidence/`.
