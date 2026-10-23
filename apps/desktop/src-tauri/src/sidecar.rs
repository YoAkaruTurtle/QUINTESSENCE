//! Trusted-host sidecar manager: spawn, frame, route, time out and fail closed.
//!
//! The WebView never receives a shell.  Rust owns the child process, the
//! 4-byte big-endian/UTF-8 frame codec, the pending-request map and the
//! 120-second request deadline.  A timeout, crash, truncated frame or protocol
//! violation terminates the child and fails every pending request with
//! ``result_unknown = true``; the caller must not retry a non-idempotent
//! consult automatically.

use serde::Serialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, VecDeque};
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

pub const PROTOCOL_VERSION: &str = "desktop.v1";
pub const MAX_FRAME_BYTES: usize = 1024 * 1024;
pub const DEFAULT_REQUEST_TIMEOUT: Duration = Duration::from_secs(120);

#[derive(Clone, Debug, Serialize)]
pub struct HostError {
    pub code: String,
    pub message: String,
    pub result_unknown: bool,
}

impl HostError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            result_unknown: false,
        }
    }

    pub fn unknown(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            result_unknown: true,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct HostReply {
    pub ok: bool,
    pub result: Option<Value>,
    pub error: Option<HostError>,
}

impl HostReply {
    pub fn ok(result: Value) -> Self {
        Self {
            ok: true,
            result: Some(result),
            error: None,
        }
    }

    pub fn error(error: HostError) -> Self {
        Self {
            ok: false,
            result: None,
            error: Some(error),
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct SidecarInfo {
    pub resource_dir: String,
    pub install_dir: String,
    pub sidecar_dir: String,
    pub executable: String,
    pub current_dir: String,
    pub dev_fallback: bool,
    pub executable_sha256: String,
    pub executable_bytes: u64,
    pub dist_files: u64,
    pub internal_files: u64,
    pub dll_and_pyd_files: u64,
    pub migrations: u64,
}

impl SidecarInfo {
    pub fn from_paths(paths: &SidecarPaths) -> Result<Self, HostError> {
        match &paths.launch {
            LaunchSpec::Frozen {
                executable,
                working_dir,
            } => {
                let executable_sha256 = sha256_file(executable)?;
                let executable_bytes = fs::metadata(executable)
                    .map_err(|_| {
                        HostError::new("SIDECAR_UNAVAILABLE", "sidecar 文件不可读")
                    })?
                    .len();
                let counts = count_tree(working_dir)?;
                Ok(Self {
                    resource_dir: paths.resource_dir.display().to_string(),
                    install_dir: paths.install_dir.display().to_string(),
                    sidecar_dir: working_dir.display().to_string(),
                    executable: executable.display().to_string(),
                    current_dir: working_dir.display().to_string(),
                    dev_fallback: paths.dev_fallback,
                    executable_sha256,
                    executable_bytes,
                    dist_files: counts.files,
                    internal_files: counts.internal_files,
                    dll_and_pyd_files: counts.dlls,
                    migrations: counts.migrations,
                })
            }
            LaunchSpec::Development {
                python,
                script,
                working_dir,
                ..
            } => Ok(Self {
                resource_dir: paths.resource_dir.display().to_string(),
                install_dir: paths.install_dir.display().to_string(),
                sidecar_dir: working_dir.display().to_string(),
                executable: python.display().to_string(),
                current_dir: working_dir.display().to_string(),
                dev_fallback: true,
                executable_sha256: String::new(),
                executable_bytes: 0,
                dist_files: 0,
                internal_files: 0,
                dll_and_pyd_files: 0,
                migrations: 0,
            })
            .map(|mut info| {
                info.executable = format!("{} {}", python.display(), script.display());
                info
            }),
        }
    }

    pub fn dummy() -> Self {
        Self {
            resource_dir: String::new(),
            install_dir: String::new(),
            sidecar_dir: String::new(),
            executable: String::new(),
            current_dir: String::new(),
            dev_fallback: false,
            executable_sha256: String::new(),
            executable_bytes: 0,
            dist_files: 0,
            internal_files: 0,
            dll_and_pyd_files: 0,
            migrations: 0,
        }
    }
}

#[derive(Clone, Debug)]
pub enum LaunchSpec {
    Frozen {
        executable: PathBuf,
        working_dir: PathBuf,
    },
    Development {
        python: PathBuf,
        script: PathBuf,
        working_dir: PathBuf,
        source_root: PathBuf,
    },
}

#[derive(Clone, Debug)]
pub struct SidecarPaths {
    pub resource_dir: PathBuf,
    pub install_dir: PathBuf,
    pub launch: LaunchSpec,
    pub dev_fallback: bool,
}

#[derive(Default)]
struct TreeCounts {
    files: u64,
    internal_files: u64,
    dlls: u64,
    migrations: u64,
}

pub struct SidecarManager {
    child: Arc<Mutex<Child>>,
    stdin: Mutex<ChildStdin>,
    pending: Arc<Mutex<HashMap<String, Sender<HostReply>>>>,
    next_id: AtomicU64,
    alive: Arc<AtomicBool>,
    stderr_tail: Arc<Mutex<VecDeque<String>>>,
    reader: Mutex<Option<thread::JoinHandle<()>>>,
    stderr_thread: Mutex<Option<thread::JoinHandle<()>>>,
    info: SidecarInfo,
    timeout: Duration,
}

impl SidecarManager {
    pub fn spawn(paths: &SidecarPaths, config_path: &Path) -> Result<Self, HostError> {
        let info = SidecarInfo::from_paths(paths)?;
        let mut command = Command::new(match &paths.launch {
            LaunchSpec::Frozen { executable, .. } => executable,
            LaunchSpec::Development { python, .. } => python,
        });
        match &paths.launch {
            LaunchSpec::Frozen { working_dir, .. } => {
                command.current_dir(working_dir);
                for key in [
                    "PYTHONPATH",
                    "PYTHONHOME",
                    "PYTHONSTARTUP",
                    "PYTHONUSERBASE",
                    "PYTHONEXECUTABLE",
                    "PYTHONNOUSERSITE",
                ] {
                    command.env_remove(key);
                }
            }
            LaunchSpec::Development {
                script,
                working_dir,
                source_root,
                ..
            } => {
                command.arg(script);
                command.current_dir(working_dir);
                command.env("PYTHONUTF8", "1");
                command.env("PYTHONIOENCODING", "utf-8");
                let existing = std::env::var_os("PYTHONPATH").unwrap_or_default();
                let mut paths_env = std::ffi::OsString::from(source_root);
                if !existing.is_empty() {
                    paths_env.push(";");
                    paths_env.push(existing);
                }
                command.env("PYTHONPATH", paths_env);
            }
        }
        command.arg("--config").arg(config_path);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
        }
        Self::spawn_command(command, info)
    }

    /// Test seam: spawn an arbitrary child with the production framing/state.
    pub fn spawn_test(mut command: Command, info: SidecarInfo) -> Result<Self, HostError> {
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        Self::spawn_command(command, info)
    }

    fn spawn_command(mut command: Command, info: SidecarInfo) -> Result<Self, HostError> {
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = command
            .spawn()
            .map_err(|_| HostError::new("SIDECAR_UNAVAILABLE", "sidecar 进程无法启动"))?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| HostError::new("SIDECAR_UNAVAILABLE", "sidecar stdin 不可用"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| HostError::new("SIDECAR_UNAVAILABLE", "sidecar stdout 不可用"))?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| HostError::new("SIDECAR_UNAVAILABLE", "sidecar stderr 不可用"))?;

        let child = Arc::new(Mutex::new(child));
        let pending: Arc<Mutex<HashMap<String, Sender<HostReply>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let alive = Arc::new(AtomicBool::new(true));
        let stderr_tail = Arc::new(Mutex::new(VecDeque::new()));

        let reader_child = Arc::clone(&child);
        let reader_pending = Arc::clone(&pending);
        let reader_alive = Arc::clone(&alive);
        let reader = thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            loop {
                match read_frame(&mut reader) {
                    Ok(Some(body)) => match decode_response(&body) {
                        Ok((id, reply)) => {
                            let sender = reader_pending
                                .lock()
                                .ok()
                                .and_then(|mut map| map.remove(&id));
                            if let Some(sender) = sender {
                                let _ = sender.send(reply);
                            }
                        }
                        Err(error) => {
                            reader_alive.store(false, Ordering::SeqCst);
                            fail_all(&reader_pending, error);
                            kill_child(&reader_child);
                            return;
                        }
                    },
                    Ok(None) => {
                        reader_alive.store(false, Ordering::SeqCst);
                        fail_all(
                            &reader_pending,
                            HostError::unknown(
                                "SIDECAR_EXIT",
                                "sidecar 已退出；未完成的请求结果未知",
                            ),
                        );
                        return;
                    }
                    Err(error) => {
                        reader_alive.store(false, Ordering::SeqCst);
                        fail_all(&reader_pending, error);
                        kill_child(&reader_child);
                        return;
                    }
                }
            }
        });

        let stderr_tail_thread = Arc::clone(&stderr_tail);
        let stderr_thread = thread::spawn(move || {
            let mut reader = BufReader::new(stderr);
            let mut line = String::new();
            loop {
                line.clear();
                match reader.read_line(&mut line) {
                    Ok(0) => return,
                    Ok(_) => {
                        let trimmed = line.trim_end().to_string();
                        if let Ok(mut tail) = stderr_tail_thread.lock() {
                            tail.push_back(trimmed);
                            while tail.len() > 200 {
                                tail.pop_front();
                            }
                        }
                    }
                    Err(_) => return,
                }
            }
        });

        Ok(Self {
            child,
            stdin: Mutex::new(stdin),
            pending,
            next_id: AtomicU64::new(1),
            alive,
            stderr_tail,
            reader: Mutex::new(Some(reader)),
            stderr_thread: Mutex::new(Some(stderr_thread)),
            info,
            timeout: DEFAULT_REQUEST_TIMEOUT,
        })
    }

    pub fn info(&self) -> &SidecarInfo {
        &self.info
    }

    pub fn timeout(&self) -> Duration {
        self.timeout
    }

    pub fn is_alive(&self) -> bool {
        self.alive.load(Ordering::SeqCst)
    }

    pub fn pending_count(&self) -> usize {
        self.pending.lock().map(|map| map.len()).unwrap_or(0)
    }

    pub fn stderr_tail(&self) -> Vec<String> {
        self.stderr_tail
            .lock()
            .map(|tail| tail.iter().cloned().collect())
            .unwrap_or_default()
    }

    pub fn request(&self, method: &str, params: Value) -> HostReply {
        self.request_with_timeout(method, params, self.timeout)
    }

    pub fn request_with_timeout(
        &self,
        method: &str,
        params: Value,
        timeout: Duration,
    ) -> HostReply {
        if !self.is_alive() {
            return HostReply::error(HostError::new(
                "SIDECAR_UNAVAILABLE",
                "sidecar 不可用；结果未知",
            ));
        }
        let request_id = format!("req-{}", self.next_id.fetch_add(1, Ordering::SeqCst));
        let (sender, receiver) = mpsc::channel();
        if let Ok(mut pending) = self.pending.lock() {
            pending.insert(request_id.clone(), sender);
        } else {
            return HostReply::error(HostError::new("HOST_INTERNAL", "pending 请求表不可用"));
        }
        let frame = match encode_frame(&json!({
            "v": PROTOCOL_VERSION,
            "id": request_id.clone(),
            "method": method,
            "params": params,
        })) {
            Ok(frame) => frame,
            Err(error) => {
                remove_pending(&self.pending, &request_id);
                return HostReply::error(error);
            }
        };
        if self.write_frame(&frame).is_err() {
            remove_pending(&self.pending, &request_id);
            self.terminate(HostError::unknown(
                "SIDECAR_IO",
                "无法写入 sidecar；请求结果未知",
            ));
            return HostReply::error(HostError::unknown(
                "SIDECAR_IO",
                "无法写入 sidecar；请求结果未知",
            ));
        }
        match receiver.recv_timeout(timeout) {
            Ok(reply) => reply,
            Err(RecvTimeoutError::Timeout) => {
                remove_pending(&self.pending, &request_id);
                self.terminate(HostError::unknown(
                    "TIMEOUT",
                    "sidecar 请求超时；sidecar 已终止，结果未知",
                ));
                HostReply::error(HostError::unknown(
                    "TIMEOUT",
                    "sidecar 请求超时；sidecar 已终止，结果未知",
                ))
            }
            Err(RecvTimeoutError::Disconnected) => {
                remove_pending(&self.pending, &request_id);
                HostReply::error(HostError::unknown(
                    "SIDECAR_EXIT",
                    "sidecar 已退出；请求结果未知",
                ))
            }
        }
    }

    pub fn shutdown(&self) {
        self.terminate(HostError::unknown(
            "SIDECAR_TERMINATED",
            "sidecar 已关闭；未完成的请求结果未知",
        ));
        if let Ok(mut guard) = self.reader.lock() {
            if let Some(handle) = guard.take() {
                let _ = handle.join();
            }
        }
        if let Ok(mut guard) = self.stderr_thread.lock() {
            if let Some(handle) = guard.take() {
                let _ = handle.join();
            }
        }
    }

    fn terminate(&self, error: HostError) {
        self.alive.store(false, Ordering::SeqCst);
        kill_child(&self.child);
        fail_all(&self.pending, error);
    }

    fn write_frame(&self, frame: &[u8]) -> std::io::Result<()> {
        let mut stdin = self
            .stdin
            .lock()
            .map_err(|_| std::io::Error::new(std::io::ErrorKind::Other, "stdin lock"))?;
        stdin.write_all(frame)?;
        stdin.flush()
    }
}

impl Drop for SidecarManager {
    fn drop(&mut self) {
        self.shutdown();
    }
}

fn remove_pending(
    pending: &Arc<Mutex<HashMap<String, Sender<HostReply>>>>,
    request_id: &str,
) {
    if let Ok(mut map) = pending.lock() {
        map.remove(request_id);
    }
}

fn fail_all(pending: &Arc<Mutex<HashMap<String, Sender<HostReply>>>>, error: HostError) {
    if let Ok(mut map) = pending.lock() {
        for (_, sender) in map.drain() {
            let _ = sender.send(HostReply::error(error.clone()));
        }
    }
}

fn kill_child(child: &Arc<Mutex<Child>>) {
    if let Ok(mut child) = child.lock() {
        let _ = child.kill();
        let _ = child.wait();
    }
}

pub fn sha256_file(path: &Path) -> Result<String, HostError> {
    let mut file = File::open(path)
        .map_err(|_| HostError::new("SIDECAR_UNAVAILABLE", "sidecar 文件不可读"))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|_| HostError::new("SIDECAR_UNAVAILABLE", "sidecar 文件读取失败"))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn count_tree(root: &Path) -> Result<TreeCounts, HostError> {
    let mut counts = TreeCounts::default();
    let internal_root = root.join("_internal");
    walk_counts(root, &internal_root, &mut counts)?;
    Ok(counts)
}

fn walk_counts(
    dir: &Path,
    internal_root: &Path,
    counts: &mut TreeCounts,
) -> Result<(), HostError> {
    let entries = fs::read_dir(dir)
        .map_err(|_| HostError::new("SIDECAR_UNAVAILABLE", "sidecar 目录不可读"))?;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            walk_counts(&path, internal_root, counts)?;
            continue;
        }
        counts.files += 1;
        if path.starts_with(internal_root) {
            counts.internal_files += 1;
        }
        let extension = path
            .extension()
            .and_then(|value| value.to_str())
            .map(|value| value.to_ascii_lowercase())
            .unwrap_or_default();
        if extension == "dll" || extension == "pyd" {
            counts.dlls += 1;
        }
        if extension == "sql" && path.to_string_lossy().contains("migrations") {
            counts.migrations += 1;
        }
    }
    Ok(())
}

pub fn encode_frame(value: &Value) -> Result<Vec<u8>, HostError> {
    let body =
        serde_json::to_vec(value).map_err(|_| HostError::new("HOST_INTERNAL", "请求无法序列化"))?;
    if body.is_empty() || body.len() > MAX_FRAME_BYTES {
        return Err(HostError::new("FRAME_TOO_LARGE", "请求帧超过 1 MiB"));
    }
    let mut frame = Vec::with_capacity(4 + body.len());
    frame.extend_from_slice(&(body.len() as u32).to_be_bytes());
    frame.extend_from_slice(&body);
    Ok(frame)
}

fn read_frame<R: Read>(reader: &mut R) -> Result<Option<Vec<u8>>, HostError> {
    let mut header = [0u8; 4];
    if !read_exact_or_eof(reader, &mut header)? {
        return Ok(None);
    }
    let length = u32::from_be_bytes(header) as usize;
    if length == 0 || length > MAX_FRAME_BYTES {
        return Err(HostError::unknown(
            "PROTOCOL_ERROR",
            "sidecar 返回了非法帧长度；连接已关闭，结果未知",
        ));
    }
    let mut body = vec![0u8; length];
    if !read_exact_or_eof(reader, &mut body)? {
        return Err(HostError::unknown(
            "PROTOCOL_ERROR",
            "sidecar 在帧体结束前退出；结果未知",
        ));
    }
    Ok(Some(body))
}

fn read_exact_or_eof<R: Read>(reader: &mut R, buffer: &mut [u8]) -> Result<bool, HostError> {
    let mut offset = 0;
    while offset < buffer.len() {
        match reader.read(&mut buffer[offset..]) {
            Ok(0) => {
                if offset == 0 {
                    return Ok(false);
                }
                return Err(HostError::unknown(
                    "PROTOCOL_ERROR",
                    "sidecar 在帧结束前退出；结果未知",
                ));
            }
            Ok(read) => offset += read,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => {
                return Err(HostError::unknown(
                    "PROTOCOL_ERROR",
                    "sidecar 流读取失败；结果未知",
                ))
            }
        }
    }
    Ok(true)
}

fn decode_response(body: &[u8]) -> Result<(String, HostReply), HostError> {
    let value: Value = serde_json::from_slice(body).map_err(|_| {
        HostError::unknown("PROTOCOL_ERROR", "sidecar 响应不是 JSON；结果未知")
    })?;
    let object = value.as_object().ok_or_else(|| {
        HostError::unknown("PROTOCOL_ERROR", "sidecar 响应不是对象；结果未知")
    })?;
    if object.get("v").and_then(Value::as_str) != Some(PROTOCOL_VERSION) {
        return Err(HostError::unknown(
            "PROTOCOL_ERROR",
            "sidecar 响应协议版本不匹配；结果未知",
        ));
    }
    let id = object
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| HostError::unknown("PROTOCOL_ERROR", "sidecar 响应缺少 id"))?
        .to_string();
    let ok = object
        .get("ok")
        .and_then(Value::as_bool)
        .ok_or_else(|| HostError::unknown("PROTOCOL_ERROR", "sidecar 响应缺少 ok"))?;
    if ok {
        return Ok((
            id,
            HostReply::ok(object.get("result").cloned().unwrap_or(Value::Null)),
        ));
    }
    let error = object
        .get("error")
        .and_then(Value::as_object)
        .ok_or_else(|| HostError::unknown("PROTOCOL_ERROR", "sidecar 错误响应缺少 error"))?;
    let code = error
        .get("code")
        .and_then(Value::as_str)
        .unwrap_or("SIDECAR_ERROR")
        .to_string();
    let message = error
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("sidecar 返回错误")
        .to_string();
    let result_unknown = error
        .get("result_unknown")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    Ok((
        id,
        HostReply::error(HostError {
            code,
            message,
            result_unknown,
        }),
    ))
}

/// Wait for the first successful ping without assuming a startup duration.
pub fn wait_ready(manager: &SidecarManager, timeout: Duration) -> HostReply {
    let deadline = Instant::now() + timeout;
    let mut last = HostReply::error(HostError::new(
        "SIDECAR_UNAVAILABLE",
        "sidecar 未就绪",
    ));
    while Instant::now() < deadline {
        last = manager.request_with_timeout("bridge.ping", json!({}), Duration::from_secs(5));
        if last.ok {
            return last;
        }
        if !manager.is_alive() {
            return last;
        }
        thread::sleep(Duration::from_millis(120));
    }
    last
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn framing_round_trip_rejects_invalid_lengths() {
        let frame = encode_frame(&json!({"ok": true})).unwrap();
        let mut cursor = std::io::Cursor::new(frame);
        let body = read_frame(&mut cursor).unwrap().unwrap();
        assert_eq!(body, br#"{"ok":true}"#);

        for length in [0u32, (MAX_FRAME_BYTES + 1) as u32] {
            let mut cursor = std::io::Cursor::new(length.to_be_bytes().to_vec());
            assert!(read_frame(&mut cursor).is_err());
        }
    }

    #[test]
    fn truncated_frame_is_an_error_and_clean_eof_is_none() {
        let mut cursor = std::io::Cursor::new(vec![0u8, 0, 0, 9, 1, 2]);
        assert!(read_frame(&mut cursor).is_err());

        let mut cursor = std::io::Cursor::new(Vec::<u8>::new());
        assert!(read_frame(&mut cursor).unwrap().is_none());
    }
}
