//! Trusted folder selection and preparation; private state stays in the folder.
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use crate::host_settings::{self, HostSettings};
use crate::sidecar::{HostError, LaunchSpec, SidecarPaths};

pub const CONFIG: &str = "instance-host.json";
pub const SELECTED_ENV: &str = "QUINTESSENCE_SELECTED_INSTANCE";

pub fn config_at(root: &Path) -> (PathBuf, bool) {
    let managed = std::fs::read_to_string(root.join("instance.json")).ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|doc| doc.get("managed").and_then(Value::as_bool)).unwrap_or(false);
    (root.join(CONFIG), managed)
}

pub fn is_folder_config(path: &Path) -> bool {
    path.file_name().and_then(|s| s.to_str()) == Some(CONFIG)
}

pub fn preferences_path(config: &Path, fallback: PathBuf) -> PathBuf {
    if is_folder_config(config) { config.parent().unwrap().join("ui-preferences.json") } else { fallback }
}

pub fn scope(config: &Path) -> Option<String> {
    if !is_folder_config(config) { return None; }
    let doc: Value = serde_json::from_str(&std::fs::read_to_string(config.parent()?.join("instance.json")).ok()?).ok()?;
    if doc.get("schema")?.as_str()? != "quintessence.instance.v1" { return None; }
    let id = doc.get("instance_id")?.as_str()?;
    let digest = Sha256::digest(format!("quintessence.instance.ui.v1:{id}").as_bytes());
    Some(format!("{digest:x}"))
}

pub fn prepare_folder(paths: &SidecarPaths, root: &Path, settings: &HostSettings) -> Result<Value, HostError> {
    prepare_folder_with_adoption(paths, root, settings, None, None)
}

pub fn prepare_folder_with_adoption(paths: &SidecarPaths, root: &Path, settings: &HostSettings, adoption: Option<(&str, &str)>, declaration: Option<(&str, &str, &[String])>) -> Result<Value, HostError> {
    crate::paths::validate_instance_root(root)?;
    let mut command = match &paths.launch {
        LaunchSpec::Frozen { executable, working_dir } => {
            let mut cmd = Command::new(executable);
            cmd.current_dir(working_dir);
            for name in ["PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE"] { cmd.env_remove(name); }
            cmd
        },
        LaunchSpec::Development { python, script, working_dir, source_root } => {
            let mut cmd = Command::new(python);
            cmd.arg("-B").arg(script).current_dir(working_dir);
            let sidecar = script.parent().unwrap();
            let mut python_paths = vec![source_root.to_path_buf(), sidecar.to_path_buf()];
            if let Some(existing) = std::env::var_os("PYTHONPATH") { python_paths.extend(std::env::split_paths(&existing)); }
            cmd.env("PYTHONPATH", std::env::join_paths(python_paths).map_err(|_| HostError::new("INSTANCE_PREPARE_FAILED", "运行依赖路径无效"))?);
            cmd
        }
    };
    if let Some((by, reason)) = adoption {
        command.arg("--adopt-legacy-by").arg(by).arg("--adopt-legacy-reason").arg(reason);
    }
    if let Some((by, reason, components)) = declaration {
        command.arg("--declare-component-by").arg(by).arg("--declare-component-reason").arg(reason);
        for component in components { command.arg("--never-created-component").arg(component); }
    }
    command.arg("--prepare-instance").arg(root).env("PYTHONUTF8", "1")
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    #[cfg(windows)] { use std::os::windows::process::CommandExt; command.creation_flags(0x0800_0000); }
    let mut template = host_settings::managed_config(settings, root, &root.join("pcs.sqlite"),
        settings.request_budget_units.unwrap_or(2_000_000), settings.output_reserve_units.unwrap_or(4096));
    // New instances receive distinct identities; existing ones recover theirs.
    if let Some(host) = template.get_mut("host").and_then(Value::as_object_mut) {
        host.remove("namespace"); host.remove("host_instance");
    }
    let mut child = command.spawn().map_err(|_| HostError::new("INSTANCE_PREPARE_FAILED", "无法启动实例准备程序"))?;
    let mut input = child.stdin.take().ok_or_else(|| HostError::new("INSTANCE_PREPARE_FAILED", "实例准备通道不可用"))?;
    let payload = serde_json::to_vec(&template).unwrap();
    if input.write_all(&payload).is_err() { let _ = child.kill(); let _ = child.wait(); return Err(HostError::new("INSTANCE_PREPARE_FAILED", "无法发送实例配置")); }
    drop(input);
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    let output_reader = std::thread::spawn(move || { let mut bytes = Vec::new(); let _ = stdout.take(2 * 1024 * 1024).read_to_end(&mut bytes); bytes });
    let error_reader = std::thread::spawn(move || { let mut bytes = Vec::new(); let _ = stderr.take(64 * 1024).read_to_end(&mut bytes); });
    let deadline = Instant::now() + Duration::from_secs(120);
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(50)),
            _ => { let _ = child.kill(); let _ = child.wait(); let _ = output_reader.join(); let _ = error_reader.join();
                   return Err(HostError::new("INSTANCE_PREPARE_TIMEOUT", "实例准备超时；未执行重置，请重新检查原有数据")); }
        }
    };
    let bytes = output_reader.join().unwrap_or_default(); let _ = error_reader.join();
    let answer: Value = serde_json::from_slice(&bytes).map_err(|_| HostError::new("INSTANCE_PREPARE_FAILED", "实例准备未返回有效结果；原数据未被重置"))?;
    if !status.success() || answer["ok"] != true {
        return Err(HostError::new(answer.pointer("/error/code").and_then(Value::as_str).unwrap_or("INSTANCE_PREPARE_FAILED"),
            answer.pointer("/error/message").and_then(Value::as_str).unwrap_or("无法连接此实例")));
    }
    Ok(answer["result"].clone())
}

#[cfg(windows)]
pub fn pick_folder() -> Result<Option<String>, HostError> {
    // Each dialog owns an STA thread, independent of Tauri's async runtime.
    std::thread::spawn(|| unsafe {
        use windows::Win32::System::Com::{CoCreateInstance, CoInitializeEx, CoTaskMemFree, CoUninitialize, CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED};
        use windows::Win32::UI::Shell::{IFileOpenDialog, FileOpenDialog, FOS_PICKFOLDERS, FOS_FORCEFILESYSTEM, FOS_NOCHANGEDIR, SIGDN_FILESYSPATH};
        use windows::core::w;
        CoInitializeEx(None, COINIT_APARTMENTTHREADED).ok().map_err(|_| HostError::new("FOLDER_DIALOG_FAILED", "无法打开文件夹选择窗口"))?;
        struct ComGuard; impl Drop for ComGuard { fn drop(&mut self) { unsafe { CoUninitialize(); } } }
        let _guard = ComGuard;
        let dialog: IFileOpenDialog = CoCreateInstance(&FileOpenDialog, None, CLSCTX_INPROC_SERVER)
            .map_err(|_| HostError::new("FOLDER_DIALOG_FAILED", "无法创建文件夹选择窗口"))?;
        let options = dialog.GetOptions().map_err(|_| HostError::new("FOLDER_DIALOG_FAILED", "无法读取选择窗口状态"))?;
        dialog.SetOptions(options | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_NOCHANGEDIR)
            .map_err(|_| HostError::new("FOLDER_DIALOG_FAILED", "无法设置文件夹选择窗口"))?;
        let _ = dialog.SetTitle(w!("选择 AI 实例文件夹"));
        if let Err(error) = dialog.Show(None) {
            if error.code().0 as u32 == 0x800704C7 { return Ok(None); }
            return Err(HostError::new("FOLDER_DIALOG_FAILED", "文件夹选择未完成"));
        }
        let item = dialog.GetResult().map_err(|_| HostError::new("FOLDER_DIALOG_FAILED", "无法取得所选文件夹"))?;
        let value = item.GetDisplayName(SIGDN_FILESYSPATH).map_err(|_| HostError::new("FOLDER_DIALOG_FAILED", "所选位置不是文件夹"))?;
        let text = value.to_string(); CoTaskMemFree(Some(value.0.cast()));
        text.map(Some).map_err(|_| HostError::new("FOLDER_DIALOG_FAILED", "文件夹路径无法读取"))
    }).join().map_err(|_| HostError::new("FOLDER_DIALOG_FAILED", "文件夹选择窗口异常关闭"))?
}

#[cfg(not(windows))]
pub fn pick_folder() -> Result<Option<String>, HostError> {
    Err(HostError::new("FOLDER_DIALOG_UNAVAILABLE", "请直接输入实例文件夹路径"))
}

pub fn status(config: &Path) -> Value {
    let root = if is_folder_config(config) { config.parent().map(Path::to_path_buf) } else {
        std::fs::read_to_string(config).ok().and_then(|s| serde_json::from_str::<Value>(&s).ok())
            .and_then(|v| v.pointer("/store/root").and_then(Value::as_str).map(PathBuf::from))
    };
    let manifest = root.as_ref().and_then(|p| std::fs::read_to_string(p.join("instance.json")).ok())
        .and_then(|s| serde_json::from_str::<Value>(&s).ok()).unwrap_or(Value::Null);
    json!({"root":root.map(|p| p.to_string_lossy().to_string()), "instance_id":manifest.get("instance_id"),
           "namespace":manifest.get("namespace"), "portable":is_folder_config(config),
           "rollback_protection":"数据库、账本和 Anchor 随整个实例共同回滚时，无法可靠检测。"})
}

/// Tauri launches the replacement before exiting. Wait for the old mutex owner
/// instead of letting the replacement mistake this handoff for a second app.
pub fn wait_for_restart_parent() {
    let parent = std::env::var("QUINTESSENCE_RESTART_PARENT").ok().and_then(|s| s.parse::<u32>().ok());
    std::env::remove_var("QUINTESSENCE_RESTART_PARENT");
    #[cfg(windows)]
    if let Some(parent) = parent.filter(|pid| *pid != std::process::id()) {
        unsafe {
            use windows_sys::Win32::System::Threading::{OpenProcess, WaitForSingleObject};
            use windows_sys::Win32::Foundation::CloseHandle;
            let process = OpenProcess(0x0010_0000, 0, parent);
            if !process.is_null() { WaitForSingleObject(process, 15000); CloseHandle(process); }
        }
    }
    #[cfg(not(windows))] let _ = parent;
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn moving_an_instance_preserves_the_ui_scope_and_preferences_location() {
        let base = std::env::temp_dir().join(format!("instance-ui-test-{}", std::process::id()));
        let first = base.join("first"); let second = base.join("second");
        std::fs::create_dir_all(&first).unwrap(); std::fs::create_dir_all(&second).unwrap();
        let manifest = json!({"schema":"quintessence.instance.v1","instance_id":"11111111-1111-4111-8111-111111111111","managed":true});
        for path in [&first,&second] { std::fs::write(path.join("instance.json"),manifest.to_string()).unwrap(); }
        assert_eq!(scope(&first.join(CONFIG)),scope(&second.join(CONFIG)));
        assert_eq!(preferences_path(&second.join(CONFIG),base.join("global.json")), second.join("ui-preferences.json"));
        assert!(config_at(&first).1);
        std::fs::remove_file(first.join("instance.json")).unwrap(); std::fs::remove_file(second.join("instance.json")).unwrap();
        std::fs::remove_dir(first).unwrap(); std::fs::remove_dir(second).unwrap(); let _ = std::fs::remove_dir(base);
    }
}
