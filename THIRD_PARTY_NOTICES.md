# Third-party notices

QUINTESSENCE first-party code is licensed under **Apache-2.0**, as recorded in
[LICENSE](LICENSE), [NOTICE](NOTICE), Python project metadata and the desktop
Rust/npm manifests. This does not relicense any third-party work. Upstream code
is obtained through package managers/lockfiles; this repository retains license
texts, not a vendored copy of the Scope Recall implementation.

## Scope Recall: external memory runtime

- Project: [Scope Recall](https://github.com/410979729/scope-recall-hermes).
- Repository owner/maintainer: **410979729**; author attribution: **scope-recall contributors**.
- Validated distribution: `hermes-scope-recall` **3.14.1**, source revision
  [`2b640260134565ac7fef3a76644e352a50866533`](https://github.com/410979729/scope-recall-hermes/tree/2b640260134565ac7fef3a76644e352a50866533).
- License: MIT. Original notice: **Copyright (c) 2026 scope-recall contributors**.
  The complete unmodified permission/disclaimer text is retained in
  [licenses/Scope-Recall-MIT.txt](licenses/Scope-Recall-MIT.txt).

Scope Recall provides the memory engine, storage and retrieval. QUINTESSENCE
provides host collaboration, Experience projection/delivery, binding, management
and UI adaptation, and its own Cognition governance. These are separate works;
Scope Recall's internal design or source is not claimed as QUINTESSENCE original
work. No private instance, Memory database or personalized assets are distributed.

## Windows sidecar and native runtime

The validated build uses CPython 3.12.10 and freezes Scope Recall with its existing
dependencies. `collect_licenses.py` reads the installed distribution metadata,
follows non-extra runtime requirements and copies the original notices. It fails
on missing notices or unreviewed version/native-component drift.

| Component in the validated environment | Version | License / retained boundary |
|---|---|---|
| CPython | 3.12.10 | PSF-2.0 and original bundled notices, including bzip2, libffi and Microsoft Distributable Code restrictions |
| PyYAML | 6.0.3 | MIT |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| referencing | 0.37.0 | MIT |
| attrs | 26.1.0 | MIT |
| rpds-py | 0.30.0 | MIT; embedded Rust graph also inventoried from the wheel SBOM |
| typing_extensions | 4.16.0 | PSF-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| tzdata | 2026.6 | Apache-2.0 wrapper and original timezone notices |
| OpenSSL | 3.0.16 | Apache-2.0, original upstream license retained |
| Expat | 2.7.1 | MIT, original copyright/permission text retained |
| zlib | 1.3.1 | Zlib |
| XZ liblzma | 5.2.5 | Public-domain library, with original component-boundary notice; no claim to redistribute GPL command-line utilities |

The locally rebuilt rpds-py wheel uses compiler path remapping; it is not an
upstream binary wheel. Its version and original source license are unchanged.
Its installed SBOM identifies the additional exact-version Rust source archives.
The collector requires those source-notice files to be available in the Cargo
cache; it never substitutes an unrelated generic license for an unknown package.

CPython's Microsoft runtime terms apply to those components alone. The
first-party Apache license does not remove those terms. SQLite's upstream code
is [public domain](https://www.sqlite.org/copyright.html); its use does not make
private databases distributable project assets.

## Desktop Rust, web frontend and WebView2

The exact dependency graph is recorded by `apps/desktop/src-tauri/Cargo.lock`
and `apps/desktop/pnpm-lock.yaml`. Direct Rust dependencies include Tauri 2,
serde, serde_json, sha2, windows and windows-sys; their original MIT/Apache terms
remain intact. The frontend uses `@tauri-apps/api` 2.12.4 (Apache-2.0 OR MIT).
Vite 7.3.7 (MIT), TypeScript 5.9.3 (Apache-2.0), esbuild and Rollup are build
tooling; original build/target dependency notices are conservatively included
alongside runtime notices rather than claiming every listed package is linked.

Some locked transitive crates are MPL-2.0: cssparser 0.37.0,
cssparser-macros 0.7.1, dtoa-short 0.3.5, option-ext 0.2.0 and selectors 0.38.0.
They are not relicensed as Apache first-party code. The shipped inventory links
each **exact-version, unmodified source archive** at
`https://crates.io/api/v1/crates/<name>/<version>/download`, so recipients can
obtain the covered source under MPL-2.0. Original licenses/notices accompany the
binary. See the [MPL-2.0 text](licenses/MPL-2.0.txt) and its source/executable
and larger-work conditions in sections 3.1–3.4.

The `webview2-com*` Rust wrappers are MIT work of their upstream authors; their
original workspace notice is retained. **Microsoft's native loader is separate**:
this build's `WebView2LoaderStatic.lib` was byte-matched to the official
Microsoft.Web.WebView2 **1.0.3800.47** NuGet package. Its original
[SDK license](licenses/WebView2-SDK-LICENSE.txt) and
[SDK notices](licenses/WebView2-SDK-NOTICE.txt) are retained. The installed
Evergreen WebView2 browser runtime is an external Microsoft runtime, not
QUINTESSENCE Apache-licensed code or a bundled browser distribution.

## Build tools and generated distribution notices

PyInstaller **6.16.0** is a build tool, but its compiled bootloader/bootstrap
code is included in the executable. Its original GPL-2.0-or-later text and
**Bootloader Distribution Exception** are included: the exception permits
linking/embedding the compiled bootloader in combined programs without imposing
GPL restrictions from those files on the combined program. Other portions retain
their own terms; the runtime hooks are Apache-2.0. See the
[upstream exception](https://github.com/pyinstaller/pyinstaller/blob/v6.16.0/COPYING.txt).
PyInstaller hooks-contrib's original notice is also collected. Pillow is used
only for generating first-party icons; its implementation is not the application
runtime. NSIS 3.13 builds the installer. Its original [COPYING](licenses/NSIS-COPYING.txt)
identifies zlib/libpng, bzip2 and CPL-1.0 compressor terms; those remain separate
from QUINTESSENCE's license. Unmodified source, including compressor source, is
available in the [official exact-version source release](https://sourceforge.net/projects/nsis/files/NSIS%203/3.13/).
The notices and source-availability link are shipped with the package. No NSIS
source is modified or vendored here.

The build script puts original texts, per-file SHA256 hashes, exact versions,
source URLs and MPL source-availability information in
`resources/sidecar/pcs-bridge/_internal/licenses/inventory.json`, with the texts
beside it in the installed app. The standard-library notice set is retained from
the actual Rust toolchain. A missing or changed license requires review; no
privacy-scan exemption or license inference silently clears it.

`licenses/upstream-sources.json` records origins of the small original texts
retained in this source repository. The source allowlist covers these notice
files, not an imported third-party source tree. Before any later public binary
release, regenerate and verify the inventory for that exact artifact. Older
local packages with an incomplete notice payload are not the distribution
candidate, even though their runtime tests passed.

## Assets and private data

The public banner is first-party project artwork. Private original icon files
are not required in source; a developer can build neutral fallback icons.
No third-party imagery, model weights, real profiles, conversations, Memory
stores, logs, local backups or API credentials are part of the open-source asset
set. API/model service access remains subject to the selected provider's terms.
