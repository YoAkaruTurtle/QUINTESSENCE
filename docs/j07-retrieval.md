# Bounded belief contexts and on-demand pages

The review Context builder now selects a deterministic working set instead of
requiring every current Belief to fit. The default is 64 KiB (raised 2026-11-04;
J07 shipped with 24 KiB). Host JSON
may set `budget.pcs_context_max_bytes` to another positive integer; the desktop
host passes this to `ContextService`. The constructor exposes the same keyword.
`pcs_belief_coverage=false` refuses startup. There is no independent receipt-off
mode. Host privacy authority is applied before the visible collection is built.

P1 explicit typed references and P3 scope overlap precede the recent P4 group
and status-ranked P5 remainder; P2 is reserved for future explicit retention.
The count limit is 200, with a target of 20 P5 slots limited by capacity after
P1--P3. High-priority records are never evicted to make this target. Byte limits
can prevent P5 selection altogether; this is logged. Oversized records are
skipped whole, and scanning continues. Small libraries keep their existing
order when everything fits. Actual rendered Beliefs retain their stored status,
strength, provenance references and scope. SelfView references follow the
selected working set, not the entire library.

`belief_coverage` exists only in the rendered review document. Its visible
total equals rendered plus omitted-by-budget plus omitted-by-size. The receipt
is included in byte accounting and recomputed after final serialization drops.
Clipped Beliefs are absent from the exact dependency read set; its meaning and
the frozen envelope schema remain unchanged. This changes dependencies to match
actual supplied content, rather than treating omitted records as supplied.
Deletion audit for unavailable Belief records stays in the envelope and is
excluded from the model document, so it cannot indirectly consume working-set
bytes or change the coverage receipt.

`cognition.belief.read` accepts `cursor`, optional `status`, and `limit` 1--20
(default 5). Enumeration returns `items`, `total`, `truncated`, `next_cursor`
inside `result`. It is a separate state-preserving projection; the existing
C10 current-belief query remains unchanged. The default page includes all live
supported, proposed, contested, contradicted, stale and legacy insufficient
records allowed by host privacy, purpose and applicability scope. Exact reads
retain their existing authority checks and also return pagination metadata.
`ref` with `cursor` is rejected. Query result bodies are decoded from their
canonical JSON text before being returned as structured tool results.

Pages sort by `(recorded_at DESC, record_id DESC)` and compare continuation keys,
so deletion of the anchor does not invalidate a valid cursor. Invalid syntax,
unknown cursor versions and changed filters produce `cursor_invalid`,
`cursor_version_mismatch` and `cursor_filters_mismatch`, respectively. Cursors
bind host authority and scope as well as status, and cannot expand read access.
Each page has its own read snapshot. Concurrent changes across pages have no
snapshot consistency guarantee. Clients must follow `truncated` and
`next_cursor`, not infer exhaustion from an intermediate scan batch length.

J03 still owns `U()`, injection units, whole-request estimates and the HTTP
pre-send gate. Provider usage remains separate evidence. Agent budget refusal
returns a visible explanation and recovery options, preserving history. An
unconfigured request budget retains J03's prior behavior: no whole-request
check, and an injection check only when injection units are known.

The separate frozen collab.v1 consult path still consumes explicit inputs and
registered C10 query materials; this change does not replace that query plan
with the review working-set algorithm or implement semantic retrieval. Its
storage byte cap accepts the host-configured limit. Work-set coverage does not
certify that the model used or understood supplied content.

Run `tools/benchmark_j07.py --output <external-directory>` for the 100/1,000
record by 24/32/48/64 KiB offline matrix. `tools/experiment_j07.py` has a separate
`--preflight-only` mode and requires an explicit secret file for a real turn.
The real runner never retries failed transport calls and refuses to reuse an
output directory containing a previous experiment. All generated files must
stay outside the checkout. No database migration is required. Rollback is a
revert of the complete J07 change, restoring full assembly without the receipt;
turning off only the receipt is deliberately unsupported.
