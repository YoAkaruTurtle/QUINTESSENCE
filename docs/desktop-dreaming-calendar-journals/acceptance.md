# Desktop Dreaming calendar journals acceptance

This record describes synthetic-instance verification of the
[approved calendar journal scope](plan.md).

Independent acceptance for code baseline `5c19a4f` is **PASS WITH CONDITIONS**.
All calendar journal tests passed. The full-suite conditions are recorded
below.

## Verified behavior

| Criterion | Verified outcome | Evidence |
| --- | --- | --- |
| Calendar periods | The store accepts witnessed running days, skips unwitnessed dates, and finalizes compatible dependencies in calendar order. The fixtures exercise local midnight, daylight-saving transitions, weeks crossing a month boundary, and a year boundary. | [Calendar journal tests](../../tests/test_dreaming_journal.py) |
| Startup and consent | A synthetic resident host records and writes a witnessed day without chat after restart. The journal grant is independent of untasked idle reflection. The resident comparison checks that journal work preserves the existing instance files before foreground chat. Unknown and linked calendar files are preserved. | [Resident host tests](../../tests/desktop/test_dreaming_host.py) · [Calendar journal tests](../../tests/test_dreaming_journal.py) |
| Research and input coverage | The writer can read an earlier exact source in a no-chat day and pass the result to the next model round. The provider-overflow fixtures cover more than six original records, long source text, and later research results. | [Writer tests](../../tests/desktop/test_dreaming_calendar_writer.py) |
| Transport and recovery | The loopback transport recognizes provider context overflow and leaves output length to the provider. The writer continues a length-limited result and preserves completed fragments across restart. An unsuccessful period can wait without holding later work. | [Writer tests](../../tests/desktop/test_dreaming_calendar_writer.py) · [Calendar journal tests](../../tests/test_dreaming_journal.py) |
| Exact dependencies | A revoked leaf source invalidates the derived annual lineage. The Consult regression exercises exact dependency registration and deletion handling. | [Writer tests](../../tests/desktop/test_dreaming_calendar_writer.py) · [Context tests](../../tests/domain/test_consult_context.py) |
| Ordinary Consult | A calendar query can recall a journal in the ordinary chat path. An approximately 81 KB synthetic journal reaches the loopback model intact through the real HTTP adapter. | [Calendar query tests](../../tests/domain/test_time_window.py) · [Resident host tests](../../tests/desktop/test_dreaming_host.py) · [Provider request test](../../tests/conversation/test_orchestrator.py) |

## Measured Context limit

An exact-period recall fixture rendered an 82,682-byte synthetic Context and
reproduced rejection at the existing 65,536-byte ceiling. Journal material now
has a separate Context section. The ordinary section keeps its existing
assembly ceiling and the combined payload still goes through the existing
provider request budget. A separate loopback regression validates the entire
journal body, including the final sentence, in the normal model request.
[Context assembly](../../src/quintessence_pcs/runtime/context.py) ·
[Context fixture](../../tests/domain/test_consult_context.py) ·
[Provider request fixture](../../tests/conversation/test_orchestrator.py)

## Test evidence and limits

The independent targeted run passed 98 tests. An integrated targeted run
recorded 103 passes during implementation. The focused evidence includes
seven writer tests, nine calendar store tests, and three orb tests. The
desktop TypeScript check and frontend production build passed.

An independent synthetic desktop run wrote a day, week, month, and year
journal in order. The four levels received the same active persona and read
tools, carried the exact original source, and passed live dependency checks.

The full offline run took 1,366.103 seconds and recorded 2,708 tests, with
2,693 passes, nine skips, five failures, and one error. Four cancellation
cases reproduced identically at pre-change baseline `8dc6651`. They include
`test_a_valid_turn_creates_a_bounded_review_and_no_change`, where the scripted
transport rejects the `cancel` keyword. The other two failures came from a
fixture assertion that rejected the substring `sk-` in the temporary path's
`task-` segment. Both cases passed on the baseline and candidate with a
neutral temporary path. [Maintenance regression](../../tests/desktop/test_rg08_re_maintenance.py)

The standard [offline test runner](../../tools/run_tests.py) writes evidence
outside the source tree. Model verification used synthetic inputs and scripted
or loopback transports. Live paid-provider behavior and redistribution through
an installer were outside this verification.

Rebuilding a published journal after a source invalidates it is pending.
