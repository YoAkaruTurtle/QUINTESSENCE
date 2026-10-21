# QUINTESSENCE

Local migration candidate, version 0.0.0. Not a release or the designated
development mainline. Public README, architecture, roadmap and licensing
review are pending separate approval.

Python 3.12+ library: storage, Persona/Memory/Cognition services and runtime.
Import `quintessence_pcs`. Protocol `pcs.v2` and migration ledger identities
are unchanged. No CLI is provided. Installed wheels include all SQL migrations.

Offline checks: `python tools/run_tests.py`. Only synthetic data and loopback
fake providers are used. Test outputs go to an OS temporary directory outside
the checkout; `--output` accepts an explicit external directory.

The code checkout, private instance data and experiment outputs are separate
areas. Store APIs require the caller to choose a database path: always use an
external instance or experiment directory. No private instance manager or
experiment platform is supplied by this migration.

Engineering completion does not establish research evaluation results.
