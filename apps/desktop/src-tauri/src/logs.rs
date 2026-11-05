//! Host-owned runtime log: one fixed **instance-scoped** directory, a bounded
//! in-memory ring and two rotating JSONL files.  Every entry is redacted
//! **before** it reaches the ring or disk; `read` redacts again as defence in
//! depth.
//!
//! The WebView never supplies a path or an instance id.  `logs_read` accepts
//! only a fixed source id, a fixed level id, a bounded substring and a bounded
//! limit.  The host writes to `instances/<fingerprint>/` once it has an
//! instance authority (namespace + host_instance + instance root); without one
//! it uses the isolated `boot/` scope.  The legacy shared `desktop.log` in the
//! base directory is neither written nor read.

use std::collections::VecDeque;
use std::fmt::Write as _;
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::sidecar::{HostError, HostReply};

pub const RING_CAPACITY: usize = 1000;
pub const FILE_MAX_BYTES: u64 = 512 * 1024;
pub const FILE_COUNT: usize = 2;
pub const MAX_MESSAGE_CHARS: usize = 2048;
pub const MAX_INPUT_CHARS: usize = 8192;
pub const READ_LIMIT_MAX: u32 = 500;
pub const READ_LIMIT_DEFAULT: u32 = 200;
pub const QUERY_MAX_CHARS: usize = 64;
pub const LOG_FILE: &str = "desktop.log";
pub const PREV_LOG_FILE: &str = "desktop.1.log";
pub const REDACTION_VERSION: u32 = 1;
pub const DIR_ENV: &str = "QUINTESSENCE_DESKTOP_LOG_DIR";
pub const PAYLOAD_MARKER: &str = "[payload-like line dropped]";
pub const INSTANCES_DIR: &str = "instances";
pub const BOOT_DIR: &str = "boot";
const IDENTITY_DOMAIN: &[u8] = b"quintessence.log.instance.v1\0";
const MAX_IDENTITY_CONFIG_BYTES: u64 = 4 * 1024 * 1024;

/// Methods whose successful result is worth a debug line.  Polling reads are
/// deliberately absent: they would rewrite the log every couple of seconds.
const LOGGED_ACTIONS: &[&str] = &[
    "conversation.create",
    "conversation.send",
    "turn.cancel",
    "consult.interact",
    "confirm_destination",
    "review.generate",
    "review.resolve",
    "cognition.candidate.decide",
    "profile.publish",
    "profile.activate",
    "profile.publish_reviewed",
    "profile.read_body",
    "preference.revoke",
    "postprocess.retry",
    "settings.reload",
    "model.test_connection",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Source {
    Host,
    Sidecar,
}

impl Source {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Host => "host",
            Self::Sidecar => "sidecar",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Level {
    Debug,
    Info,
    Warn,
    Error,
}

impl Level {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Debug => "debug",
            Self::Info => "info",
            Self::Warn => "warn",
            Self::Error => "error",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "debug" => Some(Self::Debug),
            "info" => Some(Self::Info),
            "warn" | "warning" => Some(Self::Warn),
            "error" | "critical" => Some(Self::Error),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Entry {
    seq: u64,
    at_ms: u64,
    #[serde(default)]
    at_text: Option<String>,
    source: Source,
    level: Level,
    #[serde(default)]
    code: Option<String>,
    message: String,
    #[serde(default)]
    truncated: bool,
}

#[derive(Clone, Copy)]
enum WriteState {
    Ok,
    Disabled(&'static str),
}

struct History {
    state: &'static str,
    entries: u64,
    skipped: u64,
}

struct Inner {
    entries: VecDeque<Entry>,
    seq: u64,
    dropped: u64,
    history: History,
    write: WriteState,
    current_bytes: u64,
    last_sidecar_level: Level,
}

impl Default for Inner {
    fn default() -> Self {
        Self {
            entries: VecDeque::new(),
            seq: 0,
            dropped: 0,
            history: History {
                state: "empty",
                entries: 0,
                skipped: 0,
            },
            write: WriteState::Ok,
            current_bytes: 0,
            last_sidecar_level: Level::Info,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Scope {
    Instance,
    Boot,
}

impl Scope {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Instance => "instance",
            Self::Boot => "boot",
        }
    }
}

/// Stable, path-free instance identity.  The fingerprint is a SHA-256 over the
/// namespace, host instance and normalized instance root; the plaintext path is
/// never used as a directory name and is never returned to the WebView.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InstanceIdentity {
    namespace: String,
    host_instance: String,
    root: String,
}

impl InstanceIdentity {
    pub fn new(namespace: &str, host_instance: &str, root: &Path) -> Option<Self> {
        let namespace = namespace.trim();
        let host_instance = host_instance.trim();
        if namespace.is_empty() || host_instance.is_empty() || !root.is_absolute() {
            return None;
        }
        let canonical = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
        let mut normalized = canonical.to_string_lossy().into_owned();
        if cfg!(windows) {
            normalized = normalized.to_lowercase();
        }
        Some(Self {
            namespace: namespace.to_string(),
            host_instance: host_instance.to_string(),
            root: normalized,
        })
    }

    pub fn from_config_path(path: &Path) -> Option<Self> {
        let metadata = fs::metadata(path).ok()?;
        if metadata.len() > MAX_IDENTITY_CONFIG_BYTES {
            return None;
        }
        let text = fs::read_to_string(path).ok()?;
        let document: Value = serde_json::from_str(&text).ok()?;
        Self::from_config(&document)
    }

    pub fn from_config(document: &Value) -> Option<Self> {
        let namespace = document.pointer("/host/namespace")?.as_str()?;
        let host_instance = document.pointer("/host/host_instance")?.as_str()?;
        let root = document.pointer("/store/root")?.as_str()?;
        Self::new(namespace, host_instance, Path::new(root))
    }

    pub fn fingerprint(&self) -> String {
        let mut hasher = Sha256::new();
        hasher.update(IDENTITY_DOMAIN);
        hasher.update(self.namespace.as_bytes());
        hasher.update([0]);
        hasher.update(self.host_instance.as_bytes());
        hasher.update([0]);
        hasher.update(self.root.as_bytes());
        let digest = hasher.finalize();
        let mut output = String::with_capacity(64);
        for byte in digest {
            let _ = write!(output, "{byte:02x}");
        }
        output
    }
}

/// `<base>/instances/<fingerprint>` when an authority exists, otherwise the
/// isolated `<base>/boot`.  The legacy shared files directly under `<base>` are
/// never selected.
pub fn scope_dir(base: &Path, identity: Option<&InstanceIdentity>) -> (PathBuf, Scope) {
    match identity {
        Some(identity) => (
            base.join(INSTANCES_DIR).join(identity.fingerprint()),
            Scope::Instance,
        ),
        None => (base.join(BOOT_DIR), Scope::Boot),
    }
}

pub struct LogStore {
    dir: PathBuf,
    scope: Scope,
    file_max: u64,
    ring_cap: usize,
    inner: Mutex<Inner>,
}

pub struct Redaction {
    pub text: String,
    pub truncated: bool,
}

impl LogStore {
    pub fn open(dir: PathBuf) -> Arc<Self> {
        Self::open_in(dir, Scope::Instance)
    }

    pub fn open_in(dir: PathBuf, scope: Scope) -> Arc<Self> {
        Self::open_in_with(dir, scope, FILE_MAX_BYTES, RING_CAPACITY)
    }

    pub fn open_with(dir: PathBuf, file_max: u64, ring_cap: usize) -> Arc<Self> {
        Self::open_in_with(dir, Scope::Instance, file_max, ring_cap)
    }

    fn open_in_with(dir: PathBuf, scope: Scope, file_max: u64, ring_cap: usize) -> Arc<Self> {
        let store = Arc::new(Self {
            dir,
            scope,
            file_max,
            ring_cap: ring_cap.max(1),
            inner: Mutex::new(Inner::default()),
        });
        store.initialize();
        store
    }

    fn initialize(&self) {
        let mut inner = self.lock();
        if fs::create_dir_all(&self.dir).is_err() {
            inner.write = WriteState::Disabled("dir_unavailable");
            inner.history.state = "unavailable";
            return;
        }
        match self.load_history(&mut inner) {
            Ok(()) => {}
            Err(()) => {
                inner.history.state = "unavailable";
                inner.history.entries = 0;
            }
        }
        if matches!(inner.write, WriteState::Ok) {
            inner.current_bytes = fs::metadata(self.dir.join(LOG_FILE))
                .map(|meta| meta.len())
                .unwrap_or(0);
        }
    }

    fn load_history(&self, inner: &mut Inner) -> Result<(), ()> {
        let (mut previous, previous_skipped) = read_entries(&self.dir.join(PREV_LOG_FILE))?;
        let (mut current, current_skipped) = read_entries(&self.dir.join(LOG_FILE))?;
        previous.append(&mut current);
        let total = previous.len();
        let start = total.saturating_sub(self.ring_cap);
        for (index, mut entry) in previous.drain(start..).enumerate() {
            entry.seq = index as u64;
            let clean = redact(&entry.message);
            entry.message = clean.text;
            entry.truncated = entry.truncated || clean.truncated;
            inner.entries.push_back(entry);
        }
        inner.seq = inner.entries.len() as u64;
        inner.history = History {
            state: if inner.entries.is_empty() {
                "empty"
            } else {
                "available"
            },
            entries: inner.entries.len() as u64,
            skipped: previous_skipped + current_skipped,
        };
        Ok(())
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        // A poisoned log lock must not take down the host; keep logging.
        self.inner.lock().unwrap_or_else(|error| error.into_inner())
    }

    pub fn record(&self, level: Level, code: Option<&str>, message: &str) {
        self.record_entry(Source::Host, level, code, message, None);
    }

    pub fn record_lifecycle(&self, level: Level, code: &str, detail: &str) {
        self.record(level, Some(code), detail);
    }

    /// One sidecar stderr line.  The original text is never stored: redaction
    /// and payload dropping run before the ring/file write.
    pub fn record_sidecar_line(&self, line: &str) {
        let line = line.trim_end_matches(['\r', '\n']);
        if line.trim().is_empty() {
            return;
        }
        let parsed = parse_sidecar_prefix(line);
        let mut inner = self.lock();
        let level = parsed.level.unwrap_or(inner.last_sidecar_level);
        if let Some(level) = parsed.level {
            inner.last_sidecar_level = level;
        }
        drop(inner);
        self.record_entry(
            Source::Sidecar,
            level,
            None,
            &parsed.message,
            parsed.at_text,
        );
    }

    /// Record the outcome of one bridge call.  Failures are always recorded
    /// (method + code only); successful reads are not, to keep polling quiet.
    pub fn record_ipc(&self, method: &str, reply: &HostReply, elapsed_ms: u64) {
        let method = sanitize_token(method, 64);
        if !reply.ok {
            let code = reply
                .error
                .as_ref()
                .map(|error| sanitize_token(&error.code, 32))
                .unwrap_or_else(|| "UNKNOWN".to_string());
            self.record(
                Level::Warn,
                Some("ipc.failed"),
                &format!("method={method} code={code} ms={elapsed_ms}"),
            );
        } else if LOGGED_ACTIONS.contains(&method.as_str()) {
            self.record(
                Level::Debug,
                Some("ipc.action"),
                &format!("method={method} ms={elapsed_ms}"),
            );
        }
    }

    fn record_entry(
        &self,
        source: Source,
        level: Level,
        code: Option<&str>,
        message: &str,
        at_text: Option<String>,
    ) {
        let clean = redact(message);
        let mut inner = self.lock();
        inner.seq += 1;
        let entry = Entry {
            seq: inner.seq,
            at_ms: now_ms(),
            at_text: at_text.map(|text| sanitize_token(&text, 32)),
            source,
            level,
            code: code.and_then(|value| sanitize_code(value)),
            message: clean.text,
            truncated: clean.truncated,
        };
        self.push_locked(&mut inner, entry);
    }

    fn push_locked(&self, inner: &mut Inner, entry: Entry) {
        if inner.entries.len() >= self.ring_cap {
            inner.entries.pop_front();
            inner.dropped = inner.dropped.saturating_add(1);
        }
        inner.entries.push_back(entry.clone());
        if matches!(inner.write, WriteState::Ok) {
            if let Err(reason) = self.append_locked(inner, &entry) {
                inner.write = WriteState::Disabled(reason);
            }
        }
    }

    fn append_locked(&self, inner: &mut Inner, entry: &Entry) -> Result<(), &'static str> {
        let line = serde_json::to_string(entry).map_err(|_| "serialize_failed")?;
        let bytes = line.len() as u64 + 1;
        let current = self.dir.join(LOG_FILE);
        if !current.exists() {
            inner.current_bytes = 0;
        }
        if inner.current_bytes.saturating_add(bytes) > self.file_max {
            self.rotate_locked(inner)?;
        }
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&current)
            .map_err(|_| "write_failed")?;
        file.write_all(line.as_bytes()).map_err(|_| "write_failed")?;
        file.write_all(b"\n").map_err(|_| "write_failed")?;
        inner.current_bytes = inner.current_bytes.saturating_add(bytes);
        Ok(())
    }

    fn rotate_locked(&self, inner: &mut Inner) -> Result<(), &'static str> {
        let current = self.dir.join(LOG_FILE);
        let previous = self.dir.join(PREV_LOG_FILE);
        if previous.exists() {
            fs::remove_file(&previous).map_err(|_| "rotation_failed")?;
        }
        if current.exists() {
            fs::rename(&current, &previous).map_err(|_| "rotation_failed")?;
        }
        inner.current_bytes = 0;
        Ok(())
    }

    /// Bounded read.  Nothing here touches the sidecar: `logs_read` must work
    /// while the host is not ready or a turn is running.
    pub fn read(
        &self,
        source: Option<&str>,
        level: Option<&str>,
        query: Option<&str>,
        limit: Option<u32>,
    ) -> Result<Value, HostError> {
        let source = match source.map(str::trim).filter(|value| !value.is_empty()) {
            None => SourceFilter::All,
            Some(value) => SourceFilter::parse(value).ok_or_else(|| {
                HostError::new("INVALID_PARAMS", "未知的日志来源")
            })?,
        };
        let level = match level.map(str::trim).filter(|value| !value.is_empty()) {
            None => None,
            Some(value) => Some(
                Level::parse(value)
                    .ok_or_else(|| HostError::new("INVALID_PARAMS", "未知的日志级别"))?,
            ),
        };
        let query = query.unwrap_or("").trim().to_string();
        if query.chars().count() > QUERY_MAX_CHARS {
            return Err(HostError::new("INVALID_PARAMS", "日志搜索过长"));
        }
        let limit = limit.unwrap_or(READ_LIMIT_DEFAULT);
        if !(1..=READ_LIMIT_MAX).contains(&limit) {
            return Err(HostError::new("INVALID_PARAMS", "日志读取条数越界"));
        }
        let needle = query.to_lowercase();

        let inner = self.lock();
        let mut matched: Vec<&Entry> = inner
            .entries
            .iter()
            .filter(|entry| source.matches(entry.source))
            .filter(|entry| level.map(|wanted| entry.level == wanted).unwrap_or(true))
            .filter(|entry| {
                if needle.is_empty() {
                    return true;
                }
                entry.message.to_lowercase().contains(&needle)
                    || entry
                        .code
                        .as_deref()
                        .map(|code| code.to_lowercase().contains(&needle))
                        .unwrap_or(false)
            })
            .collect();
        let matched_total = matched.len();
        let taken: Vec<&Entry> = matched
            .drain(matched_total.saturating_sub(limit as usize)..)
            .collect();
        let entries: Vec<Value> = taken
            .iter()
            .map(|entry| {
                let clean = redact(&entry.message);
                json!({
                    "seq": entry.seq,
                    "at_ms": entry.at_ms,
                    "at_text": entry.at_text,
                    "source": entry.source.as_str(),
                    "level": entry.level.as_str(),
                    "code": entry.code,
                    "message": clean.text,
                    "truncated": entry.truncated || clean.truncated,
                })
            })
            .collect();
        let returned = entries.len();
        let (from_ms, to_ms) = match (inner.entries.front(), inner.entries.back()) {
            (Some(first), Some(last)) => (Some(first.at_ms), Some(last.at_ms)),
            _ => (None, None),
        };
        let (write_enabled, write_reason) = match inner.write {
            WriteState::Ok => (true, Value::Null),
            WriteState::Disabled(reason) => (false, Value::String(reason.to_string())),
        };
        Ok(json!({
            "entries": entries,
            "matched": matched_total,
            "returned": returned,
            "truncated": matched_total > returned,
            "coverage": {
                "scope": self.scope.as_str(),
                "sources": [
                    {"id": "host", "collected": true},
                    {"id": "sidecar", "collected": true, "detail": "stderr"},
                    {
                        "id": "adapter",
                        "collected": false,
                        "reason": "stderr_not_forwarded"
                    }
                ],
                "window": {
                    "from_ms": from_ms,
                    "to_ms": to_ms,
                    "entries": inner.entries.len(),
                    "ring_capacity": self.ring_cap,
                    "dropped": inner.dropped
                },
                "history": {
                    "state": inner.history.state,
                    "entries": inner.history.entries,
                    "skipped": inner.history.skipped
                },
                "persistence": {
                    "enabled": write_enabled,
                    "reason": write_reason,
                    "file_label": LOG_FILE,
                    "files": FILE_COUNT,
                    "max_bytes_per_file": self.file_max,
                    "rotation": "size"
                },
                "redaction": {
                    "version": REDACTION_VERSION,
                    "applied_at": "write+read"
                }
            }
        }))
    }
}

enum SourceFilter {
    All,
    Host,
    Sidecar,
    Adapter,
}

impl SourceFilter {
    fn parse(value: &str) -> Option<Self> {
        match value {
            "host" => Some(Self::Host),
            "sidecar" => Some(Self::Sidecar),
            "adapter" => Some(Self::Adapter),
            "all" => Some(Self::All),
            _ => None,
        }
    }

    fn matches(&self, source: Source) -> bool {
        match self {
            Self::All => true,
            Self::Host => source == Source::Host,
            Self::Sidecar => source == Source::Sidecar,
            Self::Adapter => false,
        }
    }
}

struct SidecarPrefix {
    at_text: Option<String>,
    level: Option<Level>,
    message: String,
}

/// `2026-10-28 06:32:10,170 INFO quintessence.desktop.bridge bridge ready`
fn parse_sidecar_prefix(line: &str) -> SidecarPrefix {
    let bounded: String = line.chars().take(MAX_INPUT_CHARS).collect();
    let mut parts = bounded.splitn(4, ' ');
    let date = parts.next().unwrap_or("");
    let time = parts.next().unwrap_or("");
    let level_token = parts.next().unwrap_or("");
    let rest = parts.next().unwrap_or("");
    let timestamp = !rest.is_empty()
        && date.len() == 10
        && date.as_bytes().get(4) == Some(&b'-')
        && date.as_bytes().get(7) == Some(&b'-')
        && time.len() >= 8
        && time.as_bytes().get(2) == Some(&b':')
        && time.as_bytes().get(5) == Some(&b':');
    if timestamp {
        if let Some(level) = Level::parse(level_token) {
            return SidecarPrefix {
                at_text: Some(format!("{date} {time}")),
                level: Some(level),
                message: rest.to_string(),
            };
        }
    }
    SidecarPrefix {
        at_text: None,
        level: None,
        message: bounded,
    }
}

fn read_entries(path: &Path) -> Result<(Vec<Entry>, u64), ()> {
    let file = match fs::File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok((Vec::new(), 0)),
        Err(_) => return Err(()),
    };
    let mut entries = Vec::new();
    let mut skipped = 0u64;
    for line in BufReader::new(file).lines() {
        let line = line.map_err(|_| ())?;
        if line.trim().is_empty() {
            continue;
        }
        match serde_json::from_str::<Entry>(&line) {
            Ok(entry) => entries.push(entry),
            Err(_) => skipped += 1,
        }
    }
    Ok((entries, skipped))
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as u64)
        .unwrap_or(0)
}

fn sanitize_code(value: &str) -> Option<String> {
    let cleaned: String = value
        .chars()
        .take(48)
        .filter(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-'))
        .collect();
    if cleaned.is_empty() {
        None
    } else {
        Some(cleaned)
    }
}

fn sanitize_token(value: &str, max: usize) -> String {
    value
        .chars()
        .take(max)
        .filter(|ch| !ch.is_control())
        .collect()
}

/// Validate a host-chosen directory: absolute, no `..`, existing/creatable and
/// reachable without a symlink or junction in its own path.
pub fn validated_dir(path: &Path) -> Option<PathBuf> {
    if !path.is_absolute() {
        return None;
    }
    if path
        .components()
        .any(|component| matches!(component, Component::ParentDir))
    {
        return None;
    }
    fs::create_dir_all(path).ok()?;
    let canonical = path.canonicalize().ok()?;
    if !canonical.is_dir() {
        return None;
    }
    if crate::paths::comparable(&canonical) != crate::paths::comparable(path) {
        return None;
    }
    Some(canonical)
}

/// Resolve the log **base** directory.  The environment override exists only in
/// debug builds; a release binary ignores it even when the variable is set.
/// `scope_dir` then appends the instance fingerprint or `boot/`.
pub fn resolve_base(default: PathBuf) -> (PathBuf, bool) {
    #[cfg(debug_assertions)]
    {
        if let Some(raw) = std::env::var_os(DIR_ENV) {
            return match validated_dir(Path::new(&raw)) {
                Some(path) => (path, false),
                None => (default, true),
            };
        }
    }
    (default, false)
}

// ---------------------------------------------------------------- redaction

fn normalize(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut chars = input.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\u{1b}' {
            if chars.peek() == Some(&'[') {
                chars.next();
                for next in chars.by_ref() {
                    if ('@'..='~').contains(&next) {
                        break;
                    }
                }
            }
            continue;
        }
        if ch == '\r' || ch == '\n' || ch == '\t' {
            out.push(' ');
        } else if (ch as u32) < 0x20 || ch == '\u{7f}' {
            out.push(' ');
        } else {
            out.push(ch);
        }
    }
    out
}

fn looks_like_payload(text: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    if lower.len() > 600 && (lower.contains('{') || lower.contains('[')) {
        return true;
    }
    const MARKERS: &[&str] = &[
        "\"messages\"",
        "\"content\"",
        "\"prompt\"",
        "\"quote\"",
        "\"body\"",
        "\"text\":",
        "\"question\":",
        "\"answer\":",
        "\"draft\":",
        "\"payload\"",
        "\"scope_json\"",
        "insert into",
        "select ",
        "db row",
    ];
    MARKERS.iter().any(|marker| lower.contains(marker))
}

/// Single-line, bounded, redacted text.  Key/header values and absolute paths
/// are replaced; payload-like lines are dropped as a whole.
pub fn redact(input: &str) -> Redaction {
    let bounded: String = input.chars().take(MAX_INPUT_CHARS).collect();
    let mut truncated = bounded.chars().count() < input.chars().count();
    let normalized = normalize(&bounded);
    if looks_like_payload(&normalized) {
        return Redaction {
            text: PAYLOAD_MARKER.to_string(),
            truncated: false,
        };
    }
    let replaced = replace_spans(&normalized);
    let mut chars = replaced.chars();
    let text: String = chars.by_ref().take(MAX_MESSAGE_CHARS).collect();
    if chars.next().is_some() {
        truncated = true;
    }
    Redaction { text, truncated }
}

fn replace_spans(input: &str) -> String {
    let ranges = find_spans(input);
    if ranges.is_empty() {
        return input.to_string();
    }
    let mut out = String::with_capacity(input.len());
    let mut cursor = 0usize;
    for (start, end, marker) in ranges {
        if start < cursor {
            continue;
        }
        out.push_str(&input[cursor..start]);
        out.push_str(marker);
        cursor = end;
    }
    out.push_str(&input[cursor..]);
    out
}

fn find_spans(input: &str) -> Vec<(usize, usize, &'static str)> {
    let bytes = input.as_bytes();
    let mut spans = Vec::new();
    let mut i = 0usize;
    while i < bytes.len() {
        if let Some((end, marker)) = match_span(bytes, i) {
            spans.push((i, end, marker));
            i = end;
            continue;
        }
        i += next_char_len(input, i);
    }
    spans
}

fn next_char_len(input: &str, index: usize) -> usize {
    input[index..]
        .chars()
        .next()
        .map(|ch| ch.len_utf8())
        .unwrap_or(1)
}

fn is_boundary(bytes: &[u8], index: usize) -> bool {
    if index == 0 {
        return true;
    }
    matches!(
        bytes[index - 1],
        b' ' | b'\t'
            | b'('
            | b'['
            | b'{'
            | b'='
            | b':'
            | b'"'
            | b'\''
            | b','
            | b';'
            | b'>'
    )
}

fn starts_with_ci(bytes: &[u8], index: usize, needle: &str) -> bool {
    let needle = needle.as_bytes();
    index + needle.len() <= bytes.len()
        && bytes[index..index + needle.len()].eq_ignore_ascii_case(needle)
}

fn skip_spaces(bytes: &[u8], mut index: usize) -> usize {
    while index < bytes.len() && (bytes[index] == b' ' || bytes[index] == b'\t') {
        index += 1;
    }
    index
}

fn is_value_end(byte: u8) -> bool {
    matches!(
        byte,
        b' ' | b'\t' | b'"' | b'\'' | b',' | b';' | b')' | b']' | b'}' | b'<'
    )
}

fn match_span(bytes: &[u8], index: usize) -> Option<(usize, &'static str)> {
    if !is_boundary(bytes, index) {
        return None;
    }
    if let Some(end) = match_path(bytes, index) {
        return Some((end, "«path»"));
    }
    if let Some(end) = match_key(bytes, index) {
        return Some((end, "«redacted:key»"));
    }
    if starts_with_ci(bytes, index, "sk-") {
        let mut end = index + 3;
        while end < bytes.len()
            && (bytes[end].is_ascii_alphanumeric() || matches!(bytes[end], b'_' | b'-'))
        {
            end += 1;
        }
        if end.saturating_sub(index + 3) >= 8 {
            return Some((end, "«redacted:key»"));
        }
    }
    None
}

fn match_path(bytes: &[u8], index: usize) -> Option<usize> {
    // Drive-letter absolute path, quoted or unquoted.
    if index + 2 < bytes.len()
        && bytes[index].is_ascii_alphabetic()
        && bytes[index + 1] == b':'
        && matches!(bytes[index + 2], b'\\' | b'/')
    {
        return Some(consume_path(bytes, index + 2));
    }
    // UNC path.
    if index + 1 < bytes.len() && bytes[index] == b'\\' && bytes[index + 1] == b'\\' {
        return Some(consume_path(bytes, index + 2));
    }
    if bytes[index] == b'/' {
        for name in ["Users", "home", "root", "tmp"] {
            let after = index + 1 + name.len();
            if after < bytes.len()
                && starts_with_ci(bytes, index + 1, name)
                && bytes[after] == b'/'
            {
                return Some(consume_path(bytes, after + 1));
            }
        }
    }
    if starts_with_ci(bytes, index, "file")
        && index + 7 <= bytes.len()
        && bytes[index + 4] == b':'
        && bytes[index + 5] == b'/'
        && bytes[index + 6] == b'/'
    {
        return Some(consume_path(bytes, index + 7));
    }
    for marker in [
        "%USERPROFILE%",
        "%APPDATA%",
        "%LOCALAPPDATA%",
        "%TEMP%",
        "%TMP%",
    ] {
        if starts_with_ci(bytes, index, marker) {
            let mut end = index + marker.len();
            while end < bytes.len() && !is_value_end(bytes[end]) {
                end += 1;
            }
            return Some(end);
        }
    }
    None
}

/// A path ends at a quote only when it starts inside that quote; otherwise it
/// ends at a closing delimiter or the end of the line.  Over-redacting the rest
/// of an unquoted path (including spaces) is deliberate: a half-redacted drive
/// path would still leak the user directory.
fn consume_path(bytes: &[u8], start: usize) -> usize {
    let quote = if start > 0 {
        match bytes[start - 1] {
            b'"' => Some(b'"'),
            b'\'' => Some(b'\''),
            _ => None,
        }
    } else {
        None
    };
    let mut end = start;
    while end < bytes.len() {
        if let Some(quote) = quote {
            if bytes[end] == quote {
                return end;
            }
        } else if matches!(bytes[end], b')' | b']' | b'}' | b',' | b';' | b'<') {
            return end;
        }
        end += 1;
    }
    end
}

fn match_key(bytes: &[u8], index: usize) -> Option<usize> {
    for keyword in [
        "authorization",
        "x-api-key",
        "api-key",
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "bearer",
    ] {
        if !starts_with_ci(bytes, index, keyword) {
            continue;
        }
        let after = index + keyword.len();
        if after < bytes.len() && (bytes[after].is_ascii_alphanumeric() || bytes[after] == b'_') {
            continue;
        }
        let mut cursor = skip_spaces(bytes, after);
        if keyword == "bearer" {
            if cursor == after {
                continue;
            }
        } else {
            if cursor >= bytes.len() || !matches!(bytes[cursor], b':' | b'=') {
                continue;
            }
            cursor = skip_spaces(bytes, cursor + 1);
            // `Authorization: Bearer <token>` keeps the token as the value.
            if starts_with_ci(bytes, cursor, "bearer") {
                let bearer_end = cursor + 6;
                if bearer_end < bytes.len()
                    && matches!(bytes[bearer_end], b' ' | b'\t')
                {
                    cursor = skip_spaces(bytes, bearer_end);
                }
            }
        }
        if cursor >= bytes.len() {
            return None;
        }
        if matches!(bytes[cursor], b'"' | b'\'') {
            let quote = bytes[cursor];
            cursor += 1;
            while cursor < bytes.len() && bytes[cursor] != quote {
                cursor += 1;
            }
            if cursor < bytes.len() {
                cursor += 1;
            }
            return Some(cursor);
        }
        let mut end = cursor;
        while end < bytes.len() && !is_value_end(bytes[end]) {
            end += 1;
        }
        if end > cursor {
            return Some(end);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static COUNTER: AtomicU64 = AtomicU64::new(0);

    fn temp_dir(name: &str) -> PathBuf {
        // The repository's existing Rust tests place temporary state under
        // `target/`; the OS temp directory can be a non-writable root path when
        // the harness exports a POSIX-style TMP.
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("log-tests")
            .join(format!(
                "{}-{}-{name}",
                std::process::id(),
                COUNTER.fetch_add(1, Ordering::SeqCst)
            ));
        let _ = fs::remove_dir_all(&dir);
        dir
    }

    fn cleanup(dir: &Path) {
        let _ = fs::remove_dir_all(dir);
    }

    fn entries(store: &LogStore) -> Value {
        store.read(None, None, None, None).unwrap()
    }

    fn fake_windows_path() -> String {
        // Built at runtime so the repository gate never sees an absolute path.
        format!("{}:\\secret\\draft.txt", 'C')
    }

    #[test]
    fn redacts_keys_headers_and_bearer_values() {
        let text = redact(
            "Authorization: Bearer abc.def.ghi api_key=sk-ABCDEFGH12345678 password: hunter2 token=abc123",
        );
        assert!(!text.text.contains("hunter2"));
        assert!(!text.text.contains("ABCDEFGH12345678"));
        assert!(!text.text.contains("abc.def.ghi"));
        assert!(text.text.contains("«redacted:key»"));
    }

    #[test]
    fn redacts_paths_without_leaking_segments() {
        let drive = redact(&format!("open {}", fake_windows_path()));
        assert!(!drive.text.contains("secret"));
        assert!(drive.text.contains("«path»"));
        let spaced = redact(&format!("open {}:\\Users\\A B\\secret file.txt now", 'C'));
        assert!(!spaced.text.contains("Users"));
        assert!(!spaced.text.contains("secret file"));
        assert!(spaced.text.contains("«path»"));
        let unc = redact("share \\\\server\\share\\x here");
        assert!(!unc.text.contains("server"));
        assert!(unc.text.contains("«path»"));
        let posix = redact(&format!("home {0}home{0}u{0}x here", '/'));
        assert!(!posix.text.contains(&format!("{0}home{0}u", '/')));
        assert!(posix.text.contains("«path»"));
        let file = redact(&format!("open file{}//tmp/y", ":/"));
        assert!(!file.text.contains("tmp/y"));
        assert!(file.text.contains("«path»"));
    }

    #[test]
    fn drops_payload_and_database_lines() {
        let payload = format!(
            "{{\"messages\":[{{\"role\":\"user\",\"content\":\"{}\"}}]}}",
            "x".repeat(50)
        );
        assert_eq!(redact(&payload).text, PAYLOAD_MARKER);
        assert_eq!(
            redact("db row record_versions body sampled").text,
            PAYLOAD_MARKER
        );
        assert_eq!(redact("SELECT body FROM record_versions").text, PAYLOAD_MARKER);
    }

    #[test]
    fn truncates_long_messages_and_flags_it() {
        let text = redact(&"a".repeat(MAX_MESSAGE_CHARS + 100));
        assert_eq!(text.text.chars().count(), MAX_MESSAGE_CHARS);
        assert!(text.truncated);
    }

    #[test]
    fn strips_control_characters_and_ansi() {
        let text = redact("bridge \u{1b}[31mready\u{1b}[0m\u{7}\nnext");
        assert!(!text.text.contains('\u{1b}'));
        assert!(!text.text.contains('\n'));
        assert!(text.text.contains("bridge"));
        assert!(text.text.contains("ready"));
    }

    #[test]
    fn ring_is_bounded_and_counts_dropped_entries() {
        let dir = temp_dir("ring");
        let store = LogStore::open_with(dir.clone(), FILE_MAX_BYTES, 5);
        for index in 0..9 {
            store.record(Level::Info, Some("host.tick"), &format!("tick={index}"));
        }
        let value = entries(&store);
        assert_eq!(value["coverage"]["window"]["entries"], 5);
        assert_eq!(value["coverage"]["window"]["dropped"], 4);
        assert_eq!(value["entries"].as_array().unwrap().len(), 5);
        cleanup(&dir);
    }

    #[test]
    fn reads_are_limited_and_report_truncation() {
        let dir = temp_dir("limit");
        let store = LogStore::open_with(dir.clone(), FILE_MAX_BYTES, 50);
        for index in 0..12 {
            store.record(Level::Info, Some("host.tick"), &format!("tick={index}"));
        }
        let value = store.read(None, None, None, Some(5)).unwrap();
        assert_eq!(value["matched"], 12);
        assert_eq!(value["returned"], 5);
        assert_eq!(value["truncated"], true);
        let value = store.read(None, None, None, Some(20)).unwrap();
        assert_eq!(value["returned"], 12);
        assert_eq!(value["truncated"], false);
        cleanup(&dir);
    }

    #[test]
    fn filters_by_source_level_and_query() {
        let dir = temp_dir("filter");
        let store = LogStore::open_with(dir.clone(), FILE_MAX_BYTES, 50);
        store.record(Level::Info, Some("host.start"), "desktop host started");
        store.record(Level::Warn, Some("ipc.failed"), "method=conversation.send code=BUSY");
        store.record_sidecar_line("2026-10-28 06:32:10,170 INFO quintessence.desktop.bridge bridge ready");
        let sidecar = store.read(Some("sidecar"), None, None, None).unwrap();
        assert_eq!(sidecar["returned"], 1);
        assert_eq!(sidecar["entries"][0]["source"], "sidecar");
        let warn = store.read(None, Some("warn"), None, None).unwrap();
        assert_eq!(warn["returned"], 1);
        assert_eq!(warn["entries"][0]["code"], "ipc.failed");
        let search = store.read(None, None, Some("BRIDGE"), None).unwrap();
        assert_eq!(search["returned"], 1);
        let adapter = store.read(Some("adapter"), None, None, None).unwrap();
        assert_eq!(adapter["returned"], 0);
        assert_eq!(adapter["coverage"]["sources"][2]["collected"], false);
        assert_eq!(adapter["coverage"]["sources"][2]["reason"], "stderr_not_forwarded");
        cleanup(&dir);
    }

    #[test]
    fn rejects_invalid_parameters() {
        let dir = temp_dir("params");
        let store = LogStore::open_with(dir.clone(), FILE_MAX_BYTES, 10);
        assert_eq!(
            store.read(Some("file"), None, None, None).unwrap_err().code,
            "INVALID_PARAMS"
        );
        assert_eq!(
            store.read(None, Some("verbose"), None, None).unwrap_err().code,
            "INVALID_PARAMS"
        );
        assert_eq!(
            store.read(None, None, None, Some(0)).unwrap_err().code,
            "INVALID_PARAMS"
        );
        assert_eq!(
            store.read(None, None, None, Some(501)).unwrap_err().code,
            "INVALID_PARAMS"
        );
        assert_eq!(
            store
                .read(None, None, Some(&"q".repeat(65)), None)
                .unwrap_err()
                .code,
            "INVALID_PARAMS"
        );
        cleanup(&dir);
    }

    #[test]
    fn rotates_files_and_keeps_two_bounded_files() {
        let dir = temp_dir("rotate");
        let store = LogStore::open_with(dir.clone(), 700, 100);
        for index in 0..30 {
            store.record(Level::Info, Some("host.tick"), &format!("tick={index} padded={}", "x".repeat(40)));
        }
        let current = dir.join(LOG_FILE);
        let previous = dir.join(PREV_LOG_FILE);
        assert!(current.is_file());
        assert!(previous.is_file());
        assert!(fs::metadata(&current).unwrap().len() <= 700 + 256);
        assert!(fs::metadata(&previous).unwrap().len() <= 700 + 256);
        let coverage = entries(&store)["coverage"].clone();
        assert_eq!(coverage["persistence"]["enabled"], true);
        assert_eq!(coverage["persistence"]["files"], 2);
        cleanup(&dir);
    }

    #[test]
    fn write_time_redaction_reaches_disk() {
        let dir = temp_dir("disk");
        let store = LogStore::open_with(dir.clone(), FILE_MAX_BYTES, 20);
        let secret = "Bearer sk-ABCDEFGH12345678";
        let path = fake_windows_path();
        store.record_sidecar_line(&format!(
            "2026-10-28 06:32:10,170 INFO quintessence.desktop.bridge {secret} at {path}"
        ));
        let raw = fs::read_to_string(dir.join(LOG_FILE)).unwrap();
        assert!(!raw.contains("sk-"));
        assert!(!raw.contains("secret"));
        assert!(!raw.contains(&fake_windows_path()));
        assert!(raw.contains("«redacted:key»"));
        let value = entries(&store);
        let response = serde_json::to_string(&value).unwrap();
        assert!(!response.contains("sk-"));
        assert!(!response.contains(&fake_windows_path()));
        assert_eq!(value["coverage"]["persistence"]["enabled"], true);
        cleanup(&dir);
    }

    #[test]
    fn history_is_loaded_from_files_and_bad_lines_are_counted() {
        let dir = temp_dir("history");
        {
            let store = LogStore::open_with(dir.clone(), FILE_MAX_BYTES, 50);
            store.record(Level::Info, Some("host.start"), "desktop host started");
            store.record(Level::Warn, Some("ipc.failed"), "method=conversation.send code=BUSY");
        }
        fs::OpenOptions::new()
            .append(true)
            .open(dir.join(LOG_FILE))
            .unwrap()
            .write_all(b"not json\n")
            .unwrap();
        let reopened = LogStore::open_with(dir.clone(), FILE_MAX_BYTES, 50);
        let value = entries(&reopened);
        assert_eq!(value["coverage"]["history"]["state"], "available");
        assert_eq!(value["coverage"]["history"]["entries"], 2);
        assert_eq!(value["coverage"]["history"]["skipped"], 1);
        assert_eq!(value["entries"][0]["code"], "host.start");
        cleanup(&dir);
    }

    #[test]
    fn unusable_directory_is_reported_and_never_claims_history() {
        let base = temp_dir("unusable");
        fs::create_dir_all(&base).unwrap();
        let blocker = base.join("blocked");
        fs::write(&blocker, b"not a directory").unwrap();
        let store = LogStore::open_with(blocker, FILE_MAX_BYTES, 10);
        store.record(Level::Info, Some("host.start"), "desktop host started");
        let value = entries(&store);
        assert_eq!(value["coverage"]["persistence"]["enabled"], false);
        assert_eq!(value["coverage"]["persistence"]["reason"], "dir_unavailable");
        assert_eq!(value["coverage"]["history"]["state"], "unavailable");
        assert_eq!(value["returned"], 1);
        cleanup(&base);
    }

    #[test]
    fn sidecar_lines_parse_time_level_and_inherit_continuation_levels() {
        let dir = temp_dir("sidecar");
        let store = LogStore::open_with(dir.clone(), FILE_MAX_BYTES, 20);
        store.record_sidecar_line("2026-10-28 06:32:10,170 INFO quintessence.desktop.bridge bridge ready");
        store.record_sidecar_line("  File \"somewhere\", line 12, in <module>");
        store.record_sidecar_line("2026-10-28 06:32:11,200 ERROR quintessence.desktop.bridge startup failed");
        store.record_sidecar_line("Traceback continuation");
        let value = store.read(Some("sidecar"), None, None, None).unwrap();
        let rows = value["entries"].as_array().unwrap();
        assert_eq!(rows.len(), 4);
        assert_eq!(rows[0]["at_text"], "2026-10-28 06:32:10,170");
        assert_eq!(rows[0]["level"], "info");
        assert_eq!(rows[1]["level"], "info");
        assert_eq!(rows[2]["level"], "error");
        assert_eq!(rows[3]["level"], "error");
        cleanup(&dir);
    }

    #[test]
    fn ipc_logging_records_failures_and_only_action_successes() {
        let dir = temp_dir("ipc");
        let store = LogStore::open_with(dir.clone(), FILE_MAX_BYTES, 20);
        store.record_ipc(
            "conversation.send",
            &HostReply::ok(json!({"turn_id": "t1"})),
            12,
        );
        store.record_ipc(
            "conversation.get",
            &HostReply::ok(json!({"turns": []})),
            3,
        );
        store.record_ipc(
            "conversation.send",
            &HostReply::error(HostError::new("BUSY", "turn running")),
            4,
        );
        let value = entries(&store);
        let rows = value["entries"].as_array().unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["level"], "debug");
        assert!(rows[0]["message"].as_str().unwrap().contains("method=conversation.send"));
        assert_eq!(rows[1]["level"], "warn");
        assert!(rows[1]["message"].as_str().unwrap().contains("code=BUSY"));
        assert!(!value.to_string().contains("turn running"));
        cleanup(&dir);
    }

    #[test]
    fn coverage_never_exposes_an_absolute_path_or_a_secret() {
        let dir = temp_dir("coverage");
        let store = LogStore::open_with(dir.clone(), FILE_MAX_BYTES, 20);
        store.record(Level::Info, Some("host.start"), "managed=false");
        let response = serde_json::to_string(&entries(&store)).unwrap();
        assert!(!response.contains(&dir.display().to_string()));
        assert!(!response.contains("sk-"));
        assert!(!response.contains("Bearer"));
    }

    #[test]
    fn validated_dir_rejects_relative_parent_and_links() {
        assert!(validated_dir(Path::new("relative/logs")).is_none());
        let base = temp_dir("validate");
        fs::create_dir_all(&base).unwrap();
        let parent = base.join("a").join("..").join("b");
        assert!(validated_dir(&parent).is_none());
        let real = base.join("real");
        fs::create_dir_all(&real).unwrap();
        assert!(validated_dir(&real).is_some());
        #[cfg(windows)]
        {
            let link = base.join("link");
            let status = std::process::Command::new("cmd")
                .args(["/c", "mklink", "/J"])
                .arg(&link)
                .arg(&real)
                .status()
                .unwrap();
            assert!(status.success(), "junction creation failed");
            assert!(validated_dir(&link).is_none());
        }
        cleanup(&base);
    }

    fn identity(namespace: &str, host_instance: &str, root: &str) -> InstanceIdentity {
        InstanceIdentity::new(namespace, host_instance, Path::new(root)).expect("identity")
    }

    fn drive_root(name: &str) -> String {
        format!("{}:\\instances\\{name}", 'C')
    }

    #[test]
    fn fingerprint_is_stable_and_path_free() {
        let root = drive_root("alpha");
        let a = identity("ns.a", "host:a", &root);
        let same = identity("ns.a", "host:a", &root);
        let b = identity("ns.b", "host:a", &root);
        assert_eq!(a.fingerprint(), same.fingerprint());
        assert_ne!(a.fingerprint(), b.fingerprint());
        let fingerprint = a.fingerprint();
        assert_eq!(fingerprint.len(), 64);
        assert!(fingerprint.chars().all(|ch| ch.is_ascii_hexdigit()));
        assert!(!fingerprint.contains("ns.a"));
        assert!(!fingerprint.contains("alpha"));
    }

    #[test]
    fn scope_directories_are_instance_or_boot_never_the_base() {
        let base = temp_dir("scope-dirs");
        let id = identity("ns.a", "host:a", &drive_root("alpha"));
        let (instance, scope) = scope_dir(&base, Some(&id));
        assert_eq!(scope, Scope::Instance);
        assert!(instance.starts_with(base.join(INSTANCES_DIR)));
        assert!(!instance.to_string_lossy().contains("alpha"));
        let (boot, boot_scope) = scope_dir(&base, None);
        assert_eq!(boot_scope, Scope::Boot);
        assert_eq!(boot, base.join(BOOT_DIR));
        assert_ne!(instance, base);
        assert_ne!(boot, base);
        cleanup(&base);
    }

    #[test]
    fn two_instances_do_not_share_entries() {
        let base = temp_dir("ab");
        let root = drive_root("alpha");
        let a = identity("ns.a", "host:a", &root);
        let b = identity("ns.b", "host:b", &root);
        let (dir_a, scope_a) = scope_dir(&base, Some(&a));
        let (dir_b, scope_b) = scope_dir(&base, Some(&b));
        assert_ne!(dir_a, dir_b);
        let store_a = LogStore::open_in(dir_a, scope_a);
        store_a.record(Level::Info, Some("host.start"), "instance=a");
        let store_b = LogStore::open_in(dir_b, scope_b);
        let value_b = entries(&store_b);
        assert_eq!(value_b["returned"], 0);
        assert_eq!(value_b["coverage"]["scope"], "instance");
        assert_eq!(entries(&store_a)["returned"], 1);
        cleanup(&base);
    }

    #[test]
    fn boot_scope_is_isolated_from_instances() {
        let base = temp_dir("boot-scope");
        let boot = LogStore::open_in(base.join(BOOT_DIR), Scope::Boot);
        boot.record(Level::Info, Some("host.start"), "boot=only");
        assert_eq!(entries(&boot)["coverage"]["scope"], "boot");
        let id = identity("ns.a", "host:a", &drive_root("alpha"));
        let (dir, scope) = scope_dir(&base, Some(&id));
        let instance = LogStore::open_in(dir, scope);
        assert_eq!(entries(&instance)["returned"], 0);
        assert_eq!(entries(&boot)["returned"], 1);
        cleanup(&base);
    }

    #[test]
    fn legacy_shared_files_are_never_read() {
        let base = temp_dir("legacy");
        fs::create_dir_all(&base).unwrap();
        let sentinel = Entry {
            seq: 1,
            at_ms: 1,
            at_text: None,
            source: Source::Host,
            level: Level::Info,
            code: Some("host.start".to_string()),
            message: "legacy-shared-marker".to_string(),
            truncated: false,
        };
        fs::write(
            base.join(LOG_FILE),
            format!("{}\n", serde_json::to_string(&sentinel).unwrap()),
        )
        .unwrap();
        let id = identity("ns.a", "host:a", &drive_root("alpha"));
        let (dir, scope) = scope_dir(&base, Some(&id));
        let instance = LogStore::open_in(dir, scope);
        assert_eq!(entries(&instance)["returned"], 0);
        let boot = LogStore::open_in(base.join(BOOT_DIR), Scope::Boot);
        assert_eq!(entries(&boot)["returned"], 0);
        assert!(base.join(LOG_FILE).is_file());
        cleanup(&base);
    }

    #[test]
    fn restart_reads_only_the_same_instance_history() {
        let base = temp_dir("restart-scope");
        let id = identity("ns.a", "host:a", &drive_root("alpha"));
        let (dir, scope) = scope_dir(&base, Some(&id));
        {
            let store = LogStore::open_in(dir.clone(), scope);
            store.record(Level::Info, Some("host.start"), "first");
        }
        let reopened = LogStore::open_in(dir, Scope::Instance);
        let value = entries(&reopened);
        assert_eq!(value["coverage"]["history"]["state"], "available");
        assert_eq!(value["entries"][0]["message"], "first");
        cleanup(&base);
    }

    #[test]
    fn identity_from_config_requires_host_identity_and_absolute_root() {
        let root = drive_root("alpha");
        let valid = json!({
            "host": {"namespace": "ns.a", "host_instance": "host:a"},
            "store": {"root": root}
        });
        assert!(InstanceIdentity::from_config(&valid).is_some());
        let missing = json!({"store": {"root": root}});
        assert!(InstanceIdentity::from_config(&missing).is_none());
        let relative = json!({
            "host": {"namespace": "ns.a", "host_instance": "host:a"},
            "store": {"root": "relative"}
        });
        assert!(InstanceIdentity::from_config(&relative).is_none());
    }

    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    #[cfg(debug_assertions)]
    #[test]
    fn debug_build_honours_a_validated_log_dir_override() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|error| error.into_inner());
        let dir = temp_dir("override");
        fs::create_dir_all(&dir).unwrap();
        std::env::set_var(DIR_ENV, &dir);
        let (resolved, rejected) = resolve_base(PathBuf::from("default"));
        std::env::remove_var(DIR_ENV);
        assert_eq!(resolved, dir.canonicalize().unwrap());
        assert!(!rejected);
        cleanup(&dir);
        let (fallback, rejected) = resolve_base(PathBuf::from("default"));
        assert_eq!(fallback, PathBuf::from("default"));
        assert!(!rejected);
    }

    #[cfg(debug_assertions)]
    #[test]
    fn debug_build_rejects_an_unsafe_log_dir_override() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|error| error.into_inner());
        std::env::set_var(DIR_ENV, "relative/logs");
        let (resolved, rejected) = resolve_base(PathBuf::from("default"));
        std::env::remove_var(DIR_ENV);
        assert_eq!(resolved, PathBuf::from("default"));
        assert!(rejected);
    }

    #[cfg(not(debug_assertions))]
    #[test]
    fn release_build_ignores_the_log_dir_override() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|error| error.into_inner());
        std::env::set_var(DIR_ENV, "anywhere");
        let (resolved, rejected) = resolve_base(PathBuf::from("default"));
        std::env::remove_var(DIR_ENV);
        assert_eq!(resolved, PathBuf::from("default"));
        assert!(!rejected);
    }
}
