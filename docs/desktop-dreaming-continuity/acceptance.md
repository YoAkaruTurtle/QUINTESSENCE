# Desktop Dreaming and continuity acceptance

Independent behavioral acceptance is **PASS** for code tip `acd6d9c` over
clean-main base `e9dccaa`. The [approved plan](plan.md) defines the scope.
This report records synthetic-instance verification and the measured limits
of that baseline.

## Verified behavior

| Criterion | Verified outcome | Source and regression coverage |
|---|---|---|
| Additive startup | Registered, ready instances use validated startup. Two native launches preserved a 19-file original set. A promoted instance preserved 15 originals and frozen-sidecar startup preserved 19. A native switch sequence preserved 18 originals in each of three synthetic instances. | [Instance-folder tests](../../tests/desktop/test_instance_folders.py) and [resident host tests](../../tests/desktop/test_dreaming_host.py). |
| Resident idle admission | The actual bridge and a loopback final transport exercised idle reflection with an explicit per-destination private grant. Foreground work admitted before the physical-send check prevents the request, and late responses are discarded. | [Resident host tests](../../tests/desktop/test_dreaming_host.py) and [model boundary tests](../../tests/domain/test_dreaming_model.py). |
| Timing and budgets | Reflection requires 15 minutes of true idle, a 30-minute cooldown, and at most four attempts per rolling 24 hours. The explicit grant lasts 30 days, permits at most 120 calls, and caps output at 512 tokens per call. Restart retains attempt accounting. | [Wake tests](../../tests/test_dreaming.py) and [host grant implementation](../../apps/desktop/sidecar/dreaming_host.py). |
| Bounded deep work | History pages retain source references. Exact manual review takes priority over source changes, which take priority over due work. Wake settlement and a handled batch's checkpoint publish atomically. | [History tests](../../tests/test_dreaming_history.py) and [wake transaction tests](../../tests/test_dreaming.py). |
| Recent continuity | Context admits standard or durable material from the rolling 24-hour source-occurrence window. Exact source, time, privacy, retention, and authority are checked again before use. Deletion and revocation exclude stale derivatives. | [History tests](../../tests/test_dreaming_history.py) and [Context assembly](../../src/quintessence_pcs/runtime/context.py). |
| Optional semantic recall | Explicitly configured, metered Scope Recall 3.14.1 uses its real worker and vector path. A paraphrase with no lexical overlap resolves after restart. Keyword fallback remains available. Live PCS rechecks enforce instance, namespace, scope, revision, and source eligibility. | [Scope Recall tests](../../tests/domain/test_dreaming_scope_recall.py) and [semantic host tests](../../tests/desktop/test_dreaming_semantic.py). |
| Local proposals | Reflections persist bounded, source-linked proposals. The future trusted-host handoff is explicit and has no connected executor. | [Proposal tests](../../tests/test_dreaming.py) and [model tests](../../tests/domain/test_dreaming_model.py). |
| Native status window | The actual Tauri frontend rendered foreground, disabled, and disconnected states. Native checks covered hide and reopen, instance switching, and close. A separate resident test covered thinking state. | [Tauri window](../../apps/desktop/src-tauri/src/dreaming_orb.rs), [status projection](../../apps/desktop/src/dreamingStatus.ts), and [resident host tests](../../tests/desktop/test_dreaming_host.py). |

## Test evidence

The independent offline full discovery ran 2,682 tests on the acceptance
baseline before the final narrow fixes. It recorded 2,673 passes and nine skips.
At `acd6d9c`, the six Dreaming modules passed all 41 tests. Three packaged
cases that were skipped in discovery passed separately with the frozen sidecar.
The remaining skipped categories were two real-model cases, three live-web
cases, and one host symlink-capability case.

The following commands reproduce the feature and standard Python checks from
the repository root with the pinned Scope Recall environment. Keep output in
an external directory and use the repository's offline guard for direct
unittest runs.

```text
python tools/run_tests.py --output ../dreaming-test-output
python -B -m unittest tests.test_dreaming tests.test_dreaming_history tests.domain.test_dreaming_model tests.domain.test_dreaming_scope_recall tests.desktop.test_dreaming_host tests.desktop.test_dreaming_semantic -v
python -B -m unittest tests.desktop.test_instance_folders -v
python -B -m unittest tests.desktop.test_packaged -v
python tools/check_repository.py
```

Select the compatible frozen sidecar with
`QUINTESSENCE_DESKTOP_SIDECAR_EXE` before running the packaged cases. Their
[tests](../../tests/desktop/test_packaged.py) cover Consult, Review approval,
and data-lock handling. The standard
[test runner](../../tools/run_tests.py) configures offline execution, external
output, and bytecode suppression.

The code-tip repository gate passed for 587 approved paths. The local workflow
documentation gate also passed. This product baseline has no
`docs/README.md`. These feature records use `plan.md` as their entry point,
relative links, and a separate docs-only commit. Closeout verification reviews
the prepared records and the committed range, then repeats the applicable
final-tip gates.

## Scope and remaining boundaries

- Immutable startup covers registered, ready instances. Explicit create,
  repair, and move operations retain their existing workflows.
- Exact manual OpenQuestion review is available through the host interface.
  A UI selector remains deferred.
- Deleted and revoked material is filtered immediately from recall, including
  when indexing is disabled or unavailable. Physical purge integration remains
  deferred.
- Acceptance exercised the frozen bridge and native frontend with synthetic
  data and controlled transport. Installer delivery and paid-provider model
  acceptance remain unverified.
- Autonomous proposals remain local until a future explicitly authorized
  executor is connected.
