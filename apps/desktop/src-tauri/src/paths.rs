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

/// Compare two paths the way Windows does: strip the verbose `\\?\` prefix
/// (including `\\?\UNC\`), unify separators and compare case-insensitively.
pub(crate) fn comparable(path: &Path) -> String {
    let text = path.to_string_lossy();
    let stripped = if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        format!(r"\\{}", rest)
    } else if let Some(rest) = text.strip_prefix(r"\\?\") {
        rest.to_string()
    } else {
        text.to_string()
    };
    let unified = stripped.replace('/', "\\");
    if cfg!(windows) {
        unified.to_lowercase()
    } else {
        unified
    }
}

/// Validate the trusted host's instance root for the J03 budget/path contract.
///
/// The root must be absolute, an existing directory, and reachable without a
/// symlink or junction ancestor: canonicalising must resolve to the same path
/// under Windows' own comparison rules (verbatim prefix, separators, case).
pub fn validate_instance_root(path: &Path) -> Result<(), HostError> {
    if !path.is_absolute() {
        return Err(HostError::new(
            "SETTINGS_INVALID",
            "实例根必须是绝对路径",
        ));
    }
    if !path.is_dir() {
        return Err(HostError::new(
            "SETTINGS_INVALID",
            "实例根必须是已存在的目录",
        ));
    }
    let canonical = path.canonicalize().map_err(|_| {
        HostError::new("SETTINGS_INVALID", "实例根无法解析")
    })?;
    if comparable(&canonical) != comparable(path) {
        return Err(HostError::new(
            "SETTINGS_INVALID",
            "实例根不得经过符号链接或 junction",
        ));
    }
    Ok(())
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn instance_root_rejects_relative_and_missing_paths() {
        assert!(validate_instance_root(Path::new("relative-root")).is_err());
        assert!(validate_instance_root(Path::new("missing-root/nested")).is_err());
    }

    #[test]
    fn comparable_strips_verbatim_and_unifies_separators() {
        assert_eq!(
            comparable(Path::new(r"\\?\UNC\server\share")),
            comparable(Path::new(r"\\server\share"))
        );
        // Built at runtime so no literal drive path appears in the source.
        let drive = format!("{}:", 'C');
        let verbatim = PathBuf::from(format!(r"\\?\{}\data\root", drive));
        let plain = PathBuf::from(format!(r"{}\data\root", drive));
        let slashes = PathBuf::from(format!("{}/data/root", drive));
        assert_eq!(comparable(&verbatim), comparable(&plain));
        assert_eq!(comparable(&slashes), comparable(&plain));
    }

    #[cfg(windows)]
    #[test]
    fn normal_windows_absolute_path_is_accepted() {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target");
        std::fs::create_dir_all(&dir).unwrap();
        validate_instance_root(&dir).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn junction_instance_root_is_rejected() {
        let base = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target");
        std::fs::create_dir_all(&base).unwrap();
        let real = base.join("j03-junction-real");
        let link = base.join("j03-junction-link");
        std::fs::create_dir_all(&real).unwrap();
        let _ = std::fs::remove_dir(&link);
        let status = std::process::Command::new("cmd")
            .args(["/c", "mklink", "/J"])
            .arg(&link)
            .arg(&real)
            .status()
            .unwrap();
        assert!(status.success(), "junction creation failed");
        let error = validate_instance_root(&link).unwrap_err();
        assert_eq!(error.code, "SETTINGS_INVALID");
    }
}
