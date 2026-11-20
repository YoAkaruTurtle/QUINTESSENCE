# Desktop Dreaming and Scope Recall integration

This record covers the candidate that brings Desktop Dreaming and calendar
journals forward from clean `main` with the Scope Recall runtime in the same
checkout. The [journal plan](plan.md), [journal acceptance](acceptance.md),
and [original Dreaming acceptance](../desktop-dreaming-continuity/acceptance.md)
describe their respective feature baselines.

## Scope Recall source and authority

The [vendored source](../../third_party/scope-recall/) is Scope Recall
`3.14.1` at upstream revision `2b640260`. It contains the exact 287 files
in the upstream wheel's runtime allowlist and eight upstream build or notice
files. The [provenance manifest](../../third_party/scope-recall-upstream.json)
records the full revision and a digest for each imported file. The original
[MIT license](../../third_party/scope-recall/LICENSE) and
[third-party notices](../../THIRD_PARTY_NOTICES.md) accompany the source.

Scope Recall supplies indexing and retrieval. PCS controls instance
authorization, validates exact source revisions and scope, and decides
which results may be delivered. The vendored directory contains source and
package metadata. Instance stores, indexes, and credentials belong in
instance storage.

## Development and sidecar packaging

`python tools/setup_scope_recall.py` prepares the checkout's `.venv`
from the vendored source. The installer stages the upstream build outside
the checkout. `--check` verifies the installed distribution's version, runtime
file set, and digests against the checkout. Ordinary Python dependencies
can be installed through pip or supplied in a local wheelhouse.

Debug desktop startup uses the prepared `.venv` by default when it is
available. `python tools/run_tests.py --scope-recall` selects the Scope Recall
tests and checks the environment before running them. Use repeatable
`--test` options for a different targeted selection. The existing
`QUINTESSENCE_DESKTOP_PYTHON` override can select an explicit interpreter.

`python tools/setup_scope_recall.py --build` prepares the sidecar build tools.
The desktop build selects the checkout environment, verifies the Scope Recall
installation, and packages it into the frozen sidecar. Build outputs are
external to the checkout. A prepared checkout can run these paths without a
Lab checkout. [Setup helper](../../tools/setup_scope_recall.py) ·
[Test runner](../../tools/run_tests.py) ·
[Desktop development guide](../../apps/desktop/README.md#development)

## Candidate verification

Four cancellation regressions found during merge preparation were fixed. The
scripted transport now honors cancellation without consuming a scripted
response. Test doubles use the same send interface and continue to exercise a
draining transport. The production HTTP adapter retains the cancellation check
immediately before sending. [Scripted transport](../../apps/desktop/sidecar/runtime.py) ·
[Foreground regression tests](../../tests/desktop/test_rg08_delivery_foreground.py) ·
[Transport cancellation tests](../../tests/domain/test_rg08_delivery_cancel.py)

The focused integrated offline Python run passed 89 tests covering Scope Recall,
Dreaming, calendar journals, and cancellation. A separate Scope Recall run
passed all 12 tests. The offline Rust run passed all seven tests. The upstream
wheel was built from the vendored source and installed in an isolated
environment. The checkout’s `.venv` passed the installed-runtime digest check.

The standard [sidecar build](../../apps/desktop/scripts/build_sidecar.ps1)
completed with default paths and no Lab flags. It collected notices for 306
components. All three packaged smoke tests passed, and the bundle’s
private-data scan reported no findings.

A separate frozen-sidecar probe captured and retrieved synthetic memory with
the ambient `PYTHONPATH` cleared.

Independent full-suite acceptance passed with exit code 0. The run recorded
2,708 tests, with 2,702 passes, six skips, and zero failures. All three
packaged checks ran in the full suite.
