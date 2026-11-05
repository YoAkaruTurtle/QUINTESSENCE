# 07 — Budget and limits

**Direction:** PCS runtime → upstream recall and maintenance. Budgets bound
work; they are not model token accounting.

## Units

| Upstream quantity | Meaning | Not |
|---|---|---|
| `budget_tokens` | an estimated unit used by the recall planner | a model token count |
| `estimate_tokens` | the planner's estimate for a payload | a measured usage |
| `max_items` | the maximum number of items claimed or processed | a content limit |
| `lease_seconds` | the bounded execution window | a timeout budget for the caller |
| `remaining_seconds` | the caller's deadline for this call | a retry allowance |

Estimates are labelled as estimates. Actual model usage, where available, is
recorded separately and never back-filled into a pre-call estimate.

## Rules

1. The PCS request budget covers the whole request: instructions, tool
   schemas, retained history, tool results, the current input, the injected
   memory material and output reserve. It is checked every turn, not once.
2. The upstream recall budget is a sub-budget. It is capped by the PCS budget
   and never raises it.
3. A call that cannot fit returns a bounded refusal. It does not silently
   truncate conditions, negations or source envelopes.
4. Work is bounded per call: `max_items`, `lease_seconds` and
   `remaining_seconds` are always supplied. Unbounded drains are not part of
   this contract.
5. Exhausting a budget is not an error about content. It is reported as a
   budget outcome, and the unprocessed items stay queued.

## Failure semantics

| Situation | Behaviour |
|---|---|
| estimate unavailable | use a conservative estimate and mark it estimated |
| deadline exceeded | bounded failure; no partial success claim |
| cap reached | stop cleanly; keep the backlog and its age visible |
| caller asks for more than the PCS budget | refuse; do not exceed the cap |

## Evidence

`SCOPE-RECALL-INVESTIGATION.md` records that `budget_tokens` is an independent
estimation unit counted per ASCII/CJK/symbol class. The J02 runs use explicit
`max_items` and `remaining_seconds` on every maintenance call.
