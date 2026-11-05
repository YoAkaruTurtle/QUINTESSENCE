# Repository development rules

- Use the adopted clean `main` as the only base for new features, fixes and experiments. Verify it agrees with `origin/main` before branching.
- Local `archive/` refs preserve old development history. Never push them, develop from them, or merge their commits into the public line.
- Extract useful old code only after review; replace private samples with synthetic data and verify the resulting change on clean main.
- Preserve unrelated worktree edits. Do not force checkout/reset, delete archives, rewrite public history, or push/tag/release without explicit authorization.
- Keep private instances, credentials, model outputs, audit evidence and build/test products outside the source tree. Do not copy real identity or persona text into negative tests.
- Preserve third-party notices and existing domain/safety boundaries. Run the relevant tests and repository gate without bypassing hooks.
- Follow [CONTRIBUTING.md](CONTRIBUTING.md), especially the development-mainline and local-archive policy.
