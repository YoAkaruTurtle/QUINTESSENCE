# Maintenance boundary

This repository is the designated authoritative development mainline for the
general PCS. It is not an approved Public release; Public publication remains a
separate acceptance step. Public documentation (README, ARCHITECTURE, ROADMAP)
is present; licensing is recorded in `LICENSE` and `NOTICE`. The local `main`
branch tracks the private GitHub remote `origin`.

Canonical checkout for new general-PCS work: `E:\QUINTESSENCE`. Canonical
remote: `origin` (`https://github.com/YoAkaruTurtle/QUINTESSENCE`). New task
cards should cite the commit they start from.

The legacy research repository, its experiment worktrees and sealed experiment
materials are preserved separately as historical and research sources. They are
not the authoritative source for general PCS code, and this repository does not
absorb their private data, databases or model assets. Future P0 and PCS2-011
work targets this checkout.

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
all reachable history. Its `--pre-upload` mode additionally requires that no
remote is configured; the default mode also works after the private remote
exists. Update `tools/source-files.json` only after reviewing new code paths.
Ignore rules do not authorize adding private data and do not remove data from
prior commits. Human privacy and license review were completed before the
private upload; repeat them before any public release. If data enters history,
stop and report it.

SQL migrations are packaged once under `resources/migrations` and remain
byte-identical to the source baseline. The Path API supports unpacked wheel
installations, not zipimport. Tests retain Chinese and long synthetic paths.
Run Windows tests with UTF-8 enabled for parent and child interpreters.

Engineering closure is distinct from human research evaluation; no migration
test establishes a research result or resolves outstanding review governance.
