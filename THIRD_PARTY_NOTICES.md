# Third-party notices

QUINTESSENCE first-party code is licensed under the Apache License, Version 2.0.
See `LICENSE` and `NOTICE`.

This repository does **not** vendor third-party source code. It uses package
managers and lockfiles to obtain dependencies at build/test time. The direct
dependencies currently declared by the project are listed below; their own
license texts and notices remain with their packages.

## Desktop application

| Component | Version | License | Source |
|---|---|---|---|
| Tauri | 2.x | MIT OR Apache-2.0 | https://github.com/tauri-apps/tauri |
| @tauri-apps/api | 2.12.4 | Apache-2.0 OR MIT | https://github.com/tauri-apps/tauri |
| @tauri-apps/cli | 2.12.4 | Apache-2.0 OR MIT | https://github.com/tauri-apps/tauri |
| TypeScript | 5.9.3 | Apache-2.0 | https://github.com/microsoft/TypeScript |
| Vite | 7.3.x | MIT | https://github.com/vitejs/vite |

Rust direct dependencies from `apps/desktop/src-tauri/Cargo.toml`:

| Component | License |
|---|---|
| tauri-build | MIT OR Apache-2.0 |
| tauri | MIT OR Apache-2.0 |
| serde | MIT OR Apache-2.0 |
| serde_json | MIT OR Apache-2.0 |
| sha2 | MIT OR Apache-2.0 |
| windows-sys | MIT OR Apache-2.0 |

The full transitive set is recorded in `apps/desktop/pnpm-lock.yaml` and
`apps/desktop/src-tauri/Cargo.lock`; it is fetched by pnpm/Cargo and is not
included as source in this repository.

## Build-only tooling

PyInstaller 6.16 is used only to build the Windows sidecar executable in the
external Lab build environment. It is not vendored or imported by QUINTESSENCE
source code. PyInstaller is distributed under GPL-2.0 with its bootloader
exception; any future public binary distribution must re-check the applicable
exception and include the required notices.

## Assets

The repository contains the first-party `assets/quintessence-banner.png` and the
desktop brand assets generated from the project's own mark. No third-party
imagery or model weights are included.

## Boundary

This file is a dependency inventory, not a legal opinion. Before a formal public
binary release, regenerate the dependency inventory from the lockfiles and
verify the shipped notices for the exact build.
