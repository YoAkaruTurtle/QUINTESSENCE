# 03 — Role, origin and principal authority

**Direction:** trusted host → upstream capture. A role label and an `origin`
value are different facts; neither may be invented by a model.

## Terms

| Term | Meaning | May be set by |
|---|---|---|
| `origin` | who produced the content | trusted host only |
| `role` | how the content was carried (`user`, `assistant`, `tool`, `system`, `document`, `unknown`) | host, from the transport |
| `source_principal` | the identified principal behind a source | host only |
| `human_direct` | the content is the user's own direct statement | host only, from a registered input |

`role` is a transport label. It says nothing about truth, authority, or the
principal. `origin` is an authority claim and is checked against the trusted
context.

Origin values observed at the pin: `human_direct`, `assistant_visible`,
`tool_observation`, `external_document`, `host_generated`,
`memory_reinjection`, `imported`, `origin_unknown`.

## Invariants

1. The host sets `origin` from the trusted context. The upstream capture refuses
   an event whose `origin` differs from `context.actor_origin`.
2. A model output is at most `assistant_visible`. Assistant text is never
   promoted to `human_direct`, and an assistant summary never becomes an
   independent source.
3. `source_principal` is injected by the host from a verified identity. A role
   label, a payload field, or a self-declared principal is not identity.
4. A user preference has one reporting person. Support from different source
   instances is refused for that claim type.
5. Imported content keeps its import provenance and is marked as imported, not
   as a direct observation.

## Failure semantics

| Situation | Behaviour |
|---|---|
| event `origin` differs from the context | `ACCESS_DENIED`, field `origin`; nothing is stored |
| unknown or missing principal | store conservatively as unverified; never as `human_direct` |
| role says `user` but no trusted input proves it | treat as imported/unknown |
| import provenance missing | refuse the import; do not fabricate an origin |

## Evidence

The measured probe (J02 `j02-evidence/ac05_ac08_ac10.json`) sends a capture
whose `origin` is `host_generated` under a context whose `actor_origin` is
`human_direct`: the result is `ACCESS_DENIED` with field `origin`, while the
matching control capture succeeds. The upstream allowed origin values observed
at the pin are listed in `enums.json`.
