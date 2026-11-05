# Cognition contracts v1

This directory is a **local, additive** contract for J05.  It does not modify
or extend `contracts/v2`, and it is not a replacement for any J02
Memory-Integration contract.

`open-question.schema.json` describes the immutable Record body for
`kind="cognition.open_question"`, `schema_version="1"`.  The Record envelope
(namespace, record id/version, status, privacy class, scope) is the existing
PCS Record envelope and is intentionally not repeated in the body.

Boundary:

- only typed `EventRef` / `RecordRef` objects are accepted;
- one `privacy_class` must cover every source; multiple classes fail closed;
- Attention is a derived view and has no persisted table;
- J05 does not implement Goal/Commitment, Review/J06, Context/J07 or Dreaming.
