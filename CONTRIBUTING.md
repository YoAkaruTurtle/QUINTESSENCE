# Contributing to QUINTESSENCE

This is the minimum contribution policy for a repository licensed under the
Apache License, Version 2.0. It is intentionally short; it is not a substitute
for legal advice.

## 1. Inbound = outbound

By submitting a contribution (pull request, patch, issue attachment, or any
other form), you agree that your contribution is licensed under the Apache
License, Version 2.0. No separate CLA is required.

Submit only code you wrote or are authorized to submit.

## 2. Third-party code and assets

- Do not add code copied from another project unless its license is compatible
  with Apache 2.0 and its attribution is recorded.
- Licenses that may be accepted after review: Apache-2.0, MIT, BSD-2-Clause,
  BSD-3-Clause, ISC, CC0-1.0, Unlicense.
- Copyleft licenses (GPL, AGPL, LGPL, MPL-2.0) and proprietary code require an
  explicit maintainer decision and must not be merged by default.
- Record every third-party component in `THIRD_PARTY_NOTICES.md` (create it
  when the first component is added) or in `NOTICE` with: component name,
  source URL, version or commit, license, and the files or directories used.
- Do not add model weights, datasets, or other assets unless their license is
  documented and compatible with Apache 2.0. `.gitignore` excludes common
  weight formats; that is not a license review.

## 3. AI-assisted contributions

- Disclose material AI assistance in the pull request description.
- A human contributor must review, understand, and take responsibility for the
  whole change, including AI-generated parts.
- Do not paste code from an AI tool if you cannot identify its provenance.
  Treat unexplained, project-specific code as third-party until reviewed.

## 4. Before opening a pull request

- Run `python tools/run_tests.py`.
- Run `python tools/check_repository.py` and update `tools/source-files.json`
  for any new path.
- Keep test data synthetic. Never commit real conversations, private profiles,
  credentials, instance data, or experiment outputs.
- Preserve existing copyright and license notices. If you modify a file,
  consider a short notice stating that you changed it (Apache 2.0, section
  4(b)).

## 5. Review

Maintainers check provenance, license compatibility, and test evidence before
merging. First-party contributions must follow the project Apache-2.0 terms. Separately
reviewed third-party components retain their original licenses and notices;
they are not automatically relicensed as Apache-2.0.


## 6. One development mainline; local archives are reference only

After the approved clean publication is adopted, GitHub `main`, local `main`
and every new feature/fix/experiment baseline must share that clean history.
Before starting work, fetch `origin/main`, verify local `main` is aligned, and
create the new branch from it. Do not start work from an old worktree merely
because it is already open.

Old J/RG/development tips are preserved as clearly named **local-only archives**
under `archive/`. Never push archive refs (`--all`/`--mirror` are forbidden), use
them as new development bases, or merge/cherry-pick old unreviewed history into
the public line. When old code is useful, review and extract only the necessary
implementation or synthetic examples, then test it on a clean-main branch.
Keep the archive history intact; do not delete it as part of routine cleanup.

Initial adoption uses a reviewed snapshot rooted at the already-public
`origin/main`; old development commits are not parents of that snapshot.
The maintainer's explicit approval is required before pushing it. The remote
update is a normal fast-forward; local `main` is aligned only after the old tip
has an archive ref and the approved remote update succeeds. Do not force-push or
rewrite already-public history. Before switching any worktree, inspect its
status and preserve unrelated edits; never force checkout to make it clean.

Build/test/automation entry points must resolve the intended clean checkout,
not a hardcoded legacy branch or old Lab worktree. Check branch/base before
committing or building a release candidate. Scripts must not silently switch
back to archived development history. Recheck the complete incoming reachable
history and artifacts for personal data before every publication.
