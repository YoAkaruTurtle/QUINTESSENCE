# Desktop Dreaming calendar journals plan

This record preserves the approved scope for calendar journals on the desktop
product branch. The [acceptance record](acceptance.md) identifies the reviewed
baseline. The [original Dreaming plan](../desktop-dreaming-continuity/plan.md)
describes the resident scheduler, idle reflection, and recent continuity.

## Calendar and ownership

Schedule work for closed periods in the instance's configured time zone. Each
period has an inclusive local start date and an exclusive local end date. A
local day can therefore cross a daylight-saving transition without changing
its identity.

| Journal | Eligible after | Source material |
| --- | --- | --- |
| Day | The next local midnight | Eligible PCS experiences during the day and source-backed idle reflections |
| Week | The next Monday at local midnight | Daily journals for the preceding Monday through Sunday |
| Month | The first local day of the next month | Whole weekly journals inside the month and daily journals for the portions of weeks that cross a month boundary |
| Year | January 1 of the next local year | Monthly journals for the preceding calendar year |

Record a running day only when the host observes the instance running. A day
without that evidence creates no daily work. A witnessed day with no chat can
still produce a journal. The writer can draw on earlier journals, recent
experiences, and idle reflections while distinguishing them from events in the
period it is writing about. Parent periods depend on the completed material for
the witnessed days they cover.

Store running-day markers, period identities, authorization, checkpoints, and
published revisions in a new SQLite file under the instance's Dreaming
directory. Startup validates ownership before using that file and preserves
existing PCS and wake storage. It uses the resident bridge and the existing
foreground admission boundary. [Calendar store](../../src/quintessence_pcs/runtime/dreaming_journal.py) ·
[Dreaming host](../../apps/desktop/sidecar/dreaming_host.py) ·
[Resident bridge](../../apps/desktop/sidecar/pcs_bridge.py)

## Writing and authorization

Calendar synthesis replaces the older six-record batch used for model
reflection on historical windows. OpenQuestion review and independent idle
reflection keep their original roles and authorization. The journal grant is
explicit, local to the instance, and bound to the live model route. Foreground
work can interrupt a journal before publication. Unfinished work remains
resumable, and a failed period can wait while other eligible periods advance.
[Scheduling and grant](../../apps/desktop/sidecar/dreaming_host.py) ·
[Desktop controls](../../apps/desktop/src/dreamingSettings.ts) ·
[Calendar store](../../src/quintessence_pcs/runtime/dreaming_journal.py)

All four levels use the complete active persona and the same read-only research
surface. The model can request earlier journals, exact experiences by date,
memory search, and existing PCS cognition and question reads. The host checks
current policy and source eligibility. For an instance without an active or
expected persona, the writer can use the desktop's generic persona in memory
without provisioning the existing PCS store.
[Journal writer](../../apps/desktop/sidecar/dreaming_calendar.py)

Reuse the existing Scope Recall projection and retrieval to find research
material. Its fact and episode consolidation produces structured evidence
proposals under a separate contract. Journal synthesis needs calendar
dependencies, persona-led prose, and publication under PCS authority, so it has
its own contract. [Journal research](../../apps/desktop/sidecar/dreaming_calendar.py) ·
[Scope Recall projection](../../apps/desktop/sidecar/dreaming_semantic.py)

Read the eligible input period in full. When a provider cannot accept a
request, split the material into ordered fragments and persist progress. When
a provider stops at its output limit, continue the response. Publish a complete
journal with its content hash, tags, date range, precise source and persona
references, and exact child revisions. The existing cancellable transport and
the provider's physical limits still apply.
[Journal writer](../../apps/desktop/sidecar/dreaming_calendar.py) ·
[Journal transport](../../src/quintessence_pcs/runtime/dreaming_journal_model.py) ·
[Publication](../../src/quintessence_pcs/runtime/dreaming_journal.py)

## Recall and acceptance

Ordinary Consult resolves a closed calendar period in the instance's time zone
and can receive the matching journal alongside other context. The recall
section includes the period, journal kind, tags, and text. The host rechecks
current authority and exact transitive dependencies before rendering and
sending. If a source is deleted, dependent journals are withheld. A diary or
summary gives historical context and has no authority to
change current facts, permissions, or formal cognition.
[Calendar query](../../src/quintessence_pcs/domains/memory/time_window.py) ·
[Context assembly](../../src/quintessence_pcs/runtime/context.py) ·
[Conversation send](../../src/quintessence_pcs/conversation/orchestrator.py)

Acceptance covers witnessed and skipped days, boundary weeks, due order,
restart, foreground interruption, preserved instance files, independent
authorization, complete source coverage after a provider limit, related reads
across all four levels, and the effect of source revocation on a year journal.
Exercise exact-period and long-journal recall through the ordinary provider
request. Change an existing Context limit only after reproducing a collision.
The final request budget must still apply.
