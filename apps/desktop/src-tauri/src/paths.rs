//! Resolve the installed one-folder sidecar without a developer-relative path.
//!
//! Release builds accept only a complete bundle below the install directory.
//! Debug builds also accept an explicitly selected Lab build directory and a
//! source-checkout Python fallback so `tauri dev` and the Rust tests do not
//! depend on a packaged installer.

use std::path::{Path, PathBuf};

use tauri::{AppHandle, Manager};

use crate::sidecar::{HostError, LaunchSpec, SidecarPaths};

fn is_bundle(path: &Path) -> bool {
    path.join("pcs-bridge.exe").is_file() && path.join("_internal").is_dir()
}

pub fn resolve(app: &AppHandle) -> Result<SidecarPaths, HostError> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|_| HostError::new("RESOURCE_DIR_UNAVAILABLE", "Tauri resource_dir 不可用"))?;
    let install_dir = std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(Path::to_path_buf))
        .ok_or_else(|| HostError::new("INSTALL_DIR_UNAVAILABLE", "安装目录不可用"))?;
    let resource_dir = resource_dir
        .canonicalize()
        .unwrap_or_else(|_| resource_dir.clone());
    let install_dir = install_dir
        .canonicalize()
        .unwrap_or_else(|_| install_dir.clone());

    // Tauri's Windows resource_dir differs between versions/layouts: it can be
    // the install root or an explicit resources subdirectory.
    let mut candidates = vec![
        resource_dir
            .join("resources")
            .join("sidecar")
            .join("pcs-bridge"),
        resource_dir.join("sidecar").join("pcs-bridge"),
        install_dir
            .join("resources")
            .join("sidecar")
            .join("pcs-bridge"),
    ];

    #[cfg(debug_assertions)]
    {
        if let Some(selected) = std::env::var_os("QUINTESSENCE_DESKTOP_SIDECAR_DIR") {
            candidates.insert(0, PathBuf::from(selected));
        }
        let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        candidates.push(
            manifest
                .join("..")
                .join("sidecar")
                .join("dist")
                .join("pcs-bridge"),
        );
    }

    for candidate in candidates {
        let candidate = candidate
            .canonicalize()
            .unwrap_or_else(|_| candidate.clone());
        if is_bundle(&candidate) {
            #[cfg(not(debug_assertions))]
            if !candidate.starts_with(&install_dir) {
                return Err(HostError::new(
                    "SIDECAR_PATH_UNSAFE",
                    "解析到的 sidecar 目录不在安装目录下",
                ));
            }
            let dev_fallback = !candidate.starts_with(&install_dir);
            return Ok(SidecarPaths {
                resource_dir,
                install_dir,
                launch: LaunchSpec::Frozen {
                    executable: candidate.join("pcs-bridge.exe"),
                    working_dir: candidate,
                },
                dev_fallback,
            });
        }
    }

    #[cfg(debug_assertions)]
    {
        let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let desktop = manifest
            .join("..")
            .canonicalize()
            .unwrap_or_else(|_| manifest.join(".."));
        let script = desktop.join("sidecar").join("pcs_bridge.py");
        let source_root = desktop.join("..").join("..").join("src");
        if script.is_file() {
            let python = std::env::var_os("QUINTESSENCE_DESKTOP_PYTHON")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("python"));
            return Ok(SidecarPaths {
                resource_dir,
                install_dir,
                launch: LaunchSpec::Development {
                    python,
                    script,
                    working_dir: desktop,
                    source_root: source_root
                        .canonicalize()
                        .unwrap_or(source_root),
                },
                dev_fallback: true,
            });
        }
    }

    Err(HostError::new(
        "SIDECAR_NOT_FOUND",
        "安装资源中找不到完整的 one-folder sidecar（需要 pcs-bridge.exe 与 _internal）",
    ))
}
