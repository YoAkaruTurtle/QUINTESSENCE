# Candidate maintenance boundary

This local candidate is not yet the designated development mainline or an
approved release. Public documentation, mainline switch and publication remain
separate acceptance steps. Licensing is recorded in `LICENSE` and `NOTICE`.
No remote is configured.

The import namespace is `quintessence_pcs`; no legacy import alias is supplied.
Protocol `pcs.v2`, baseline identity `PCS-2026-10-17-r3`, SQL migration names,
ledger checksums, stored identifiers and error codes retain their meaning.
The only runtime presentation change beyond packaging is the generic scope
example and HTTP User-Agent branding; no new cognitive mechanism is introduced.

Keep reusable code and original synthetic test inputs in this checkout.
Private instance data belongs in a separately managed external directory.
Experiment outputs belong in an external workspace. Neither external platform
is created or managed by this library. Never use real conversations, private
profiles or model responses as test fixtures.

`python tools/run_tests.py` uses an external temporary output directory and
enables a Python accidental-access guard: only loopback sockets, explicit
external writes, and code/output/interpreter reads. This is not an OS security
sandbox. The public Store APIs still accept explicit paths; application owners
must choose an external database location. No implicit database path is added.

`python tools/check_repository.py` checks approved file paths, the index and
all reachable history. Update `tools/source-files.json` only after reviewing
new code paths. Ignore rules do not authorize adding private data and do not
remove data from prior commits. Human privacy and license review are still
required before any upload. If data enters history, stop upload and report it.

SQL migrations are packaged once under `resources/migrations` and remain
byte-identical to the source baseline. The Path API supports unpacked wheel
installations, not zipimport. Tests retain Chinese and long synthetic paths.
Run Windows tests with UTF-8 enabled for parent and child interpreters.

Engineering closure is distinct from human research evaluation; no migration
test establishes a research result or resolves outstanding review governance.
