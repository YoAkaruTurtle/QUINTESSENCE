//! Desktop-managed host settings and generated production configuration.
//!
//! The public client ships without a private host config.  On first run the
//! trusted Rust host creates a neutral production config under the application
//! data directory, and the sidecar provisions only the generic resources it
//! needs (an active neutral profile and a model authorization for the selected
//! manifest).  A user-supplied `desktop-host.json` or an explicit `--config`
//! remains an advanced/private configuration and is never overwritten.

use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, Manager};

use crate::paths::validate_instance_root;
use crate::sidecar::HostError;

pub const PROVIDER: &str = "deepseek";
pub const DEFAULT_MODEL: &str = "deepseek-flash";
pub const PRO_MODEL: &str = "deepseek-v4-pro";
pub const BASE_URL: &str = "https://api.deepseek.com";
pub const KEY_ENV: &str = "QUINTESSENCE_DESKTOP_MODEL_KEY";
pub const MANAGED_NAMESPACE: &str = "quintessence.desktop";
pub const MANAGED_HOST_INSTANCE: &str = "host:desktop";
pub const SETTINGS_SCHEMA: &str = "desktop.settings.v1";
pub const SETTINGS_FILE: &str = "desktop-settings.json";
pub const MANAGED_CONFIG_FILE: &str = "desktop-managed-host.json";
pub const LEGACY_CONFIG_FILE: &str = "desktop-host.json";

pub const MODEL_CHOICES: [&str; 2] = [DEFAULT_MODEL, PRO_MODEL];
/// RG08 R-B: the UI-bounded automatic-recall settings.
pub const MEMORY_TIMEZONES: [&str; 2] = ["Asia/Shanghai", "UTC"];
pub const MEMORY_RECALL_MAX_ITEMS: (u32, u32) = (1, 4);
pub const MEMORY_RECALL_BUDGET_UNITS: (u32, u32) = (64, 2048);
pub const MEMORY_RECALL_TIMEOUT_MS: (u32, u32) = (250, 2000);

/// The J03 safety margin, in PCS `U` units; duplicated here so the host can
/// refuse a budget that cannot fit the margin without loading the Python tree.
pub const SAFETY_MARGIN_UNITS: u32 = 256;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct HostSettings {
    #[serde(default = "off")]
    pub sdrpe_mode: String,
    #[serde(default = "output_tokens")]
    pub max_output_tokens: u32,
    #[serde(default = "schema")]
    pub schema: String,
    #[serde(default = "provider")]
    pub provider: String,
    #[serde(default = "default_model")]
    pub model: String,
    #[serde(default = "base_url")]
    pub base_url: String,
    /// The trusted instance root; absent until the host is configured.
    #[serde(default)]
    pub instance_root: Option<String>,
    /// The last folder selected through the instance connection UI.
    #[serde(default)]
    pub selected_instance: Option<String>,
    /// Whole-request budget in PCS `U` units.
    #[serde(default)]
    pub request_budget_units: Option<u32>,
    /// Output reserve in PCS `U` units.
    #[serde(default)]
    pub output_reserve_units: Option<u32>,
    /// RG08: the application-owned web switch for the generated config.
    ///
    /// `None` means the settings file predates this switch; a hand-edited
    /// `web` block in the generated config is then preserved verbatim.  Once
    /// the user has used the settings toggle, `enabled` is authoritative while
    /// the rest of the block (allowlist, timeout, capture mode) survives.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub web_enabled: Option<bool>,
    /// RG08 R-B: automatic literal recall.  `None` means the settings file
    /// predates the switch; the generated/external `memory` block is then
    /// preserved verbatim.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_recall_enabled: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_recall_max_items: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_recall_budget_units: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_recall_timeout_ms: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_timezone: Option<String>,
    /// RG08 DELIVERY: the application's auto-maintenance switch.  `None` means
    /// the settings file predates the switch; the external/managed maintenance
    /// block then stays exactly as configured.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub maintenance_enabled: Option<bool>,
}

fn schema() -> String { SETTINGS_SCHEMA.to_string() }
fn off() -> String { "off".to_string() }
fn output_tokens() -> u32 { 8192 }
fn provider() -> String { PROVIDER.to_string() }
fn default_model() -> String { DEFAULT_MODEL.to_string() }
fn base_url() -> String { BASE_URL.to_string() }

impl Default for HostSettings {
    fn default() -> Self {
        Self {
            sdrpe_mode: off(),
            max_output_tokens: output_tokens(),
            schema: schema(),
            provider: provider(),
            model: default_model(),
            base_url: base_url(),
            instance_root: None,
            selected_instance: None,
            request_budget_units: None,
            output_reserve_units: None,
            web_enabled: None,
            memory_recall_enabled: None,
            memory_recall_max_items: None,
            memory_recall_budget_units: None,
            memory_recall_timeout_ms: None,
            memory_timezone: None,
            maintenance_enabled: None,
        }
    }
}

pub fn data_dir(app: &AppHandle) -> PathBuf {
    // Explicit portable/test host state contains only the remembered selection
    // and model settings. The instance itself never depends on this directory.
    if let Some(path) = std::env::var_os("QUINTESSENCE_DESKTOP_STATE_DIR").map(PathBuf::from) {
        if path.is_absolute() && path.is_dir() && crate::paths::validate_instance_root(&path).is_ok() { return path; }
    }
    app.path()
        .app_local_data_dir()
        .unwrap_or_else(|_| std::env::temp_dir().join("quintessence-desktop"))
}

pub fn settings_path(app: &AppHandle) -> PathBuf {
    data_dir(app).join(SETTINGS_FILE)
}

pub fn load(path: &Path) -> HostSettings {
    let text = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(_) => return HostSettings::default(),
    };
    let parsed = serde_json::from_str::<HostSettings>(&text).unwrap_or_default();
    validate_settings(parsed).unwrap_or_default()
}

pub fn save(path: &Path, settings: &HostSettings) -> Result<(), HostError> {
    validate_settings(settings.clone())?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|_| {
            HostError::new("SETTINGS_UNAVAILABLE", "无法创建应用配置目录")
        })?;
    }
    let text = serde_json::to_string_pretty(settings).map_err(|_| {
        HostError::new("SETTINGS_UNAVAILABLE", "模型设置无法序列化")
    })?;
    atomic_write(path, &text)
}

pub fn validate_settings(settings: HostSettings) -> Result<HostSettings, HostError> {
    validate_generation(&settings.sdrpe_mode, settings.max_output_tokens)?;
    if settings.schema != SETTINGS_SCHEMA {
        return Err(HostError::new("SETTINGS_INVALID", "模型设置版本不受支持"));
    }
    if settings.provider != PROVIDER {
        return Err(HostError::new("SETTINGS_INVALID", "目前只支持 DeepSeek Provider"));
    }
    if !MODEL_CHOICES.contains(&settings.model.as_str()) {
        return Err(HostError::new("SETTINGS_INVALID", "不支持的 DeepSeek 模型"));
    }
    if settings.base_url != BASE_URL {
        return Err(HostError::new("SETTINGS_INVALID", "DeepSeek API 地址不可修改"));
    }
    if let Some(root) = settings.instance_root.as_deref() {
        validate_instance_root(Path::new(root))?;
    }
    if let (Some(request), Some(reserve)) = (
        settings.request_budget_units,
        settings.output_reserve_units,
    ) {
        if request <= reserve.saturating_add(SAFETY_MARGIN_UNITS) {
            return Err(HostError::new(
                "SETTINGS_INVALID",
                "请求预算必须大于输出预留与安全余量之和",
            ));
        }
    } else if settings.request_budget_units.is_some() || settings.output_reserve_units.is_some() {
        return Err(HostError::new(
            "SETTINGS_INVALID",
            "请求预算与输出预留必须同时配置",
        ));
    }
    validate_memory_settings(&settings)?;
    Ok(settings)
}

fn validate_memory_settings(settings: &HostSettings) -> Result<(), HostError> {
    if let Some(value) = settings.memory_recall_max_items {
        if !(MEMORY_RECALL_MAX_ITEMS.0..=MEMORY_RECALL_MAX_ITEMS.1).contains(&value) {
            return Err(HostError::new(
                "SETTINGS_INVALID",
                "自动召回条数必须在 1 到 4 之间",
            ));
        }
    }
    if let Some(value) = settings.memory_recall_budget_units {
        if !(MEMORY_RECALL_BUDGET_UNITS.0..=MEMORY_RECALL_BUDGET_UNITS.1).contains(&value) {
            return Err(HostError::new(
                "SETTINGS_INVALID",
                "自动召回预算必须在 64 到 2048 U 之间",
            ));
        }
    }
    if let Some(value) = settings.memory_recall_timeout_ms {
        if !(MEMORY_RECALL_TIMEOUT_MS.0..=MEMORY_RECALL_TIMEOUT_MS.1).contains(&value) {
            return Err(HostError::new(
                "SETTINGS_INVALID",
                "自动召回超时必须在 250 到 2000 毫秒之间",
            ));
        }
    }
    if let Some(ref value) = settings.memory_timezone {
        if !MEMORY_TIMEZONES.contains(&value.as_str()) {
            return Err(HostError::new(
                "SETTINGS_INVALID",
                "自动召回时区不受支持",
            ));
        }
    }
    Ok(())
}

pub fn validate_model(value: &str) -> Result<(), HostError> {
    if MODEL_CHOICES.contains(&value) {
        Ok(())
    } else {
        Err(HostError::new("INVALID_MODEL", "不支持的 DeepSeek 模型"))
    }
}

/// Resolve the config path and whether this host owns it.
///
/// Explicit CLI/environment configuration wins.  A legacy private
/// `desktop-host.json` is also treated as externally managed.  Only a fresh
/// install with neither file gets a generated managed config.
pub fn resolve_config_path(app: &AppHandle, requested: Option<PathBuf>) -> (PathBuf, bool) {
    if let Some(root) = std::env::var_os(crate::instance::SELECTED_ENV).filter(|s| !s.is_empty()) {
        return crate::instance::config_at(&PathBuf::from(root));
    }
    if let Some(path) = requested {
        return (path, false);
    }
    if let Some(value) = std::env::var_os("QUINTESSENCE_DESKTOP_CONFIG") {
        if !value.is_empty() {
            return (PathBuf::from(value), false);
        }
    }
    let settings = load(&settings_path(app));
    if let Some(root) = settings.selected_instance { return crate::instance::config_at(Path::new(&root)); }
    let base = data_dir(app);
    let legacy = base.join(LEGACY_CONFIG_FILE);
    if legacy.is_file() {
        return (legacy, false);
    }
    (base.join(MANAGED_CONFIG_FILE), true)
}

/// Create or refresh the generated production config.  No secret is written.
pub fn ensure_managed_config(
    path: &Path,
    settings: &HostSettings,
) -> Result<(), HostError> {
    validate_settings(settings.clone())?;
    if crate::instance::is_folder_config(path) && path.is_file() {
        let mut document: Value = serde_json::from_str(&fs::read_to_string(path).map_err(|_| HostError::new("CONFIG_UNAVAILABLE", "无法读取实例配置"))?)
            .map_err(|_| HostError::new("CONFIG_UNAVAILABLE", "实例配置损坏，未覆盖"))?;
        document["destination"]["model"] = json!(settings.model);
        document["destination"]["manifest"]["model"] = json!(settings.model);
        document["destination"]["manifest"]["max_output_tokens_per_call"] = json!(settings.max_output_tokens);
        if !document.get("conversation").map(Value::is_object).unwrap_or(false) { document["conversation"] = json!({}); }
        document["conversation"]["sdrpe"] = json!({"mode": settings.sdrpe_mode});
        let previous_web = document.get("web").cloned();
        merge_managed_web(&mut document, settings.web_enabled, previous_web);
        let previous_memory = document.get("memory").cloned();
        merge_managed_memory(&mut document, settings, previous_memory);
        if let Some(enabled) = settings.maintenance_enabled { document["maintenance"]["enabled"] = json!(enabled); }
        return atomic_write(path, &serde_json::to_string_pretty(&document).unwrap());
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|_| {
            HostError::new("CONFIG_UNAVAILABLE", "无法创建应用数据目录")
        })?;
    }
    let Some(root) = settings.instance_root.as_deref().map(PathBuf::from) else {
        return Err(HostError::new(
            "SETTINGS_INVALID",
            "尚未连接实例，请在设置中选择空文件夹或已有实例文件夹。",
        ));
    };
    let database = root.join("pcs.sqlite");
    let (Some(request), Some(reserve)) = (
        settings.request_budget_units,
        settings.output_reserve_units,
    ) else {
        return Err(HostError::new(
            "SETTINGS_INVALID",
            "托管配置必须同时提供请求预算与输出预留",
        ));
    };
    let previous = fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok());
    let previous_web = previous
        .as_ref()
        .and_then(|document| document.get("web").cloned());
    let previous_memory = previous
        .as_ref()
        .and_then(|document| document.get("memory").cloned());
    let mut document = managed_config(settings, &root, &database, request, reserve);
    // Keep an existing instance's manifest binding until an explicit renewal.
    if let Some(previous) = previous.as_ref().filter(|prior| {
        prior.pointer("/store/root") == document.pointer("/store/root")
    }) {
        if let Some(calls) = previous
            .pointer("/destination/manifest/max_calls")
            .and_then(Value::as_u64)
            .filter(|calls| (1..=1000).contains(calls))
        {
            document["destination"]["manifest"]["max_calls"] = json!(calls);
        }
    }
    // The managed document now owns the web switch, but never the whole block:
    // a user's allowlist, timeout or capture mode must survive every rewrite.
    // Without a settings switch (legacy settings file) the whole block is kept
    // exactly as it was, so an existing hand-edited opt-in is not lost.
    merge_managed_web(&mut document, settings.web_enabled, previous_web);
    merge_managed_memory(&mut document, settings, previous_memory);
    let text = serde_json::to_string_pretty(&document).map_err(|_| {
        HostError::new("CONFIG_UNAVAILABLE", "托管宿主配置无法序列化")
    })?;
    atomic_write(path, &text)
}

/// Keep the app-owned `web` switch and every user-owned field in the block.
///
/// The credential for the managed install is the one Windows Credential
/// Manager entry the host injects under `KEY_ENV`; pointing `web.api_key_env`
/// anywhere else would leave an enabled-but-unusable surface.  `enabled` is
/// the only field the settings toggle overwrites.
fn merge_managed_web(
    document: &mut Value,
    web_enabled: Option<bool>,
    previous_web: Option<Value>,
) {
    match web_enabled {
        Some(enabled) => {
            let mut block = match previous_web {
                Some(Value::Object(map)) => Value::Object(map),
                _ => json!({}),
            };
            block["enabled"] = json!(enabled);
            block["api_key_env"] = json!(KEY_ENV);
            document["web"] = block;
        }
        None => {
            if let Some(web) = previous_web {
                document["web"] = web;
            }
        }
    }
}

/// RG08 R-B: keep the app-owned recall/timezone fields and every user-owned
/// memory field (R-C1 may add more) across a managed rewrite.
fn merge_managed_memory(
    document: &mut Value,
    settings: &HostSettings,
    previous_memory: Option<Value>,
) {
    let any = settings.memory_recall_enabled.is_some()
        || settings.memory_recall_max_items.is_some()
        || settings.memory_recall_budget_units.is_some()
        || settings.memory_recall_timeout_ms.is_some()
        || settings.memory_timezone.is_some();
    if !any {
        if let Some(memory) = previous_memory {
            document["memory"] = memory;
        }
        return;
    }
    let mut block = match previous_memory {
        Some(Value::Object(map)) => Value::Object(map),
        _ => json!({}),
    };
    if !block.get("recall").map(Value::is_object).unwrap_or(false) {
        block["recall"] = json!({});
    }
    if let Some(enabled) = settings.memory_recall_enabled {
        block["recall"]["enabled"] = json!(enabled);
    }
    if let Some(value) = settings.memory_recall_max_items {
        block["recall"]["max_items"] = json!(value);
    }
    if let Some(value) = settings.memory_recall_budget_units {
        block["recall"]["budget_units"] = json!(value);
    }
    if let Some(value) = settings.memory_recall_timeout_ms {
        block["recall"]["timeout_seconds"] = json!(value as f64 / 1000.0);
    }
    if let Some(ref value) = settings.memory_timezone {
        block["timezone"] = json!(value);
    }
    document["memory"] = block;
}

/// RG08 R-B: write only the recall/timezone fields into an external config.
pub fn set_external_memory_settings(
    path: &Path,
    enabled: bool,
    max_items: u32,
    budget_units: u32,
    timeout_ms: u32,
    timezone: &str,
) -> Result<(), HostError> {
    if !(MEMORY_RECALL_MAX_ITEMS.0..=MEMORY_RECALL_MAX_ITEMS.1).contains(&max_items) {
        return Err(HostError::new("SETTINGS_INVALID", "自动召回条数必须在 1 到 4 之间"));
    }
    if !(MEMORY_RECALL_BUDGET_UNITS.0..=MEMORY_RECALL_BUDGET_UNITS.1).contains(&budget_units) {
        return Err(HostError::new(
            "SETTINGS_INVALID",
            "自动召回预算必须在 64 到 2048 U 之间",
        ));
    }
    if !(MEMORY_RECALL_TIMEOUT_MS.0..=MEMORY_RECALL_TIMEOUT_MS.1).contains(&timeout_ms) {
        return Err(HostError::new(
            "SETTINGS_INVALID",
            "自动召回超时必须在 250 到 2000 毫秒之间",
        ));
    }
    if !MEMORY_TIMEZONES.contains(&timezone) {
        return Err(HostError::new("SETTINGS_INVALID", "自动召回时区不受支持"));
    }
    let text = fs::read_to_string(path).map_err(|_| {
        HostError::new("CONFIG_UNAVAILABLE", "无法读取当前宿主配置")
    })?;
    let mut document: Value = serde_json::from_str(&text).map_err(|_| {
        HostError::new("CONFIG_INVALID", "宿主配置不是有效的 JSON")
    })?;
    if !document.is_object() {
        return Err(HostError::new("CONFIG_INVALID", "宿主配置必须是对象"));
    }
    if !document.get("memory").map(Value::is_object).unwrap_or(true) {
        return Err(HostError::new(
            "CONFIG_INVALID",
            "宿主 memory 配置不是对象，未作修改",
        ));
    }
    if document.get("memory").is_none() {
        document["memory"] = json!({});
    }
    let block = document["memory"]
        .as_object_mut()
        .expect("memory block was just ensured to be an object");
    if !block.get("recall").map(Value::is_object).unwrap_or(true) {
        return Err(HostError::new(
            "CONFIG_INVALID",
            "宿主 memory.recall 配置不是对象，未作修改",
        ));
    }
    if block.get("recall").is_none() {
        block.insert("recall".to_string(), json!({}));
    }
    let recall = block["recall"]
        .as_object_mut()
        .expect("recall block was just ensured to be an object");
    recall.insert("enabled".to_string(), json!(enabled));
    recall.insert("max_items".to_string(), json!(max_items));
    recall.insert("budget_units".to_string(), json!(budget_units));
    recall.insert(
        "timeout_seconds".to_string(),
        json!(timeout_ms as f64 / 1000.0),
    );
    block.insert("timezone".to_string(), json!(timezone));
    let text = serde_json::to_string_pretty(&document).map_err(|_| {
        HostError::new("CONFIG_UNAVAILABLE", "宿主配置无法序列化")
    })?;
    atomic_write(path, &text)
}

/// RG08: flip `web.enabled` in an externally managed host config.
///
/// The file is the user's (or the Lab harness's), so only the switch is
/// touched: every other key and every other `web` field survives the atomic
/// rewrite.  The block is created when absent; `api_key_env` is not invented
/// here because an external config owns its own credential contract.
pub fn set_external_web_enabled(path: &Path, enabled: bool) -> Result<(), HostError> {
    let text = fs::read_to_string(path).map_err(|_| {
        HostError::new("CONFIG_UNAVAILABLE", "无法读取当前宿主配置")
    })?;
    let mut document: Value = serde_json::from_str(&text).map_err(|_| {
        HostError::new("CONFIG_INVALID", "宿主配置不是有效的 JSON")
    })?;
    if !document.is_object() {
        return Err(HostError::new("CONFIG_INVALID", "宿主配置必须是对象"));
    }
    if !document.get("web").map(Value::is_object).unwrap_or(true) {
        return Err(HostError::new(
            "CONFIG_INVALID",
            "宿主 web 配置不是对象，未作修改",
        ));
    }
    if document.get("web").is_none() {
        document["web"] = json!({});
    }
    let block = document["web"]
        .as_object_mut()
        .expect("web block was just ensured to be an object");
    block.insert("enabled".to_string(), json!(enabled));
    let text = serde_json::to_string_pretty(&document).map_err(|_| {
        HostError::new("CONFIG_UNAVAILABLE", "宿主配置无法序列化")
    })?;
    atomic_write(path, &text)
}

pub(crate) fn managed_config(
    settings: &HostSettings,
    root: &Path,
    database: &Path,
    request_budget_units: u32,
    output_reserve_units: u32,
) -> Value {
    let budget = json!({
        "request_budget_units": request_budget_units,
        "output_reserve_units": output_reserve_units,
    });
    json!({
        "schema": "desktop.host.v1",
        "app": {
            "profile": "production",
            "label": "QUINTESSENCE"
        },
        "store": {
            "root": root.to_string_lossy(),
            "database": database.to_string_lossy()
        },
        "budget": budget,
        "host": {
            "namespace": MANAGED_NAMESPACE,
            "host_instance": MANAGED_HOST_INSTANCE,
            "operator": "operator:local",
            "access_ref": "authorization:host:desktop",
            "privacy_classes": ["private"],
            "purposes": ["consult", "reasoning", "review"],
            "disclose": true,
            "retention": "standard",
            "policy_ref": "policy:collab.v1",
            "input_privacy_class": "private"
        },
        "review": {
            "permissions": [
                "work.scan",
                "experience.read",
                "review.generate",
                "review.approve",
                "review.reject",
                "result.read"
            ]
        },
        "consult": {
            "query_plan": {
                "kind": "belief_current",
                "claim_types": ["capability"],
                "scope": {"scene": "quintessence.general"},
                "limit": 5
            },
            "max_question_chars": 8000
        },
        "destination": {
            "provider": settings.provider,
            "model": settings.model,
            "model_protocol": "collab.v1",
            "external": true,
            "manifest": {
                "provider": settings.provider,
                "base_url": settings.base_url,
                "model": settings.model,
                "api_key_env": KEY_ENV,
                "max_calls": 999,
                "max_output_tokens_per_call": settings.max_output_tokens,
                "max_wall_seconds": 120,
                "thinking": "enabled",
                "reasoning_effort": "high",
                "protocol_version": "EXP-01-THINKING-CANDIDATE-v1"
            }
        },
        "desktop": {
            "managed": true,
            "provision": true
        },
        "maintenance": {
            "enabled": settings.maintenance_enabled.unwrap_or(false),
            "permissions": ["maintenance"],
            "max_events": 1,
            "slice_seconds": 90.0,
            "model_timeout_seconds": 30.0
        },
        "conversation": {"sdrpe": {"mode": settings.sdrpe_mode}}
    })
}

/// The smallest per-call output budget that leaves room for an answer.
///
/// The manifest always enables `thinking`, and reasoning tokens are deducted
/// from this same cap: at 2048 the whole budget was consumed by reasoning and
/// a structured proposal came back as `finish_reason=length` with no content.
pub const MIN_OUTPUT_TOKENS: u32 = 8192;

pub fn validate_generation(mode: &str, tokens: u32) -> Result<(), HostError> {
    if !["off", "on"].contains(&mode) || !(MIN_OUTPUT_TOKENS..=393216).contains(&tokens) {
        return Err(HostError::new(
            "INVALID_PARAMS",
            "SDRPE 仅支持 off/on；输出上限必须为 8192–393216 的整数（推理 token 与回答共用此预算）",
        ));
    }
    Ok(())
}

/// Replace a complete file in its own directory; never expose partial JSON.
pub fn atomic_write(path: &Path, text: &str) -> Result<(), HostError> {
    let temporary = path.with_extension("json.pending");
    fs::write(&temporary, text).and_then(|_| fs::rename(&temporary, path))
        .map_err(|_| HostError::new("SETTINGS_UNAVAILABLE", "无法保存设置；原配置未被部分写入"))
}

/// Read the manifest's key environment name without exposing any secret.
pub fn api_key_env(config_path: &Path) -> String {
    let text = match fs::read_to_string(config_path) {
        Ok(text) => text,
        Err(_) => return KEY_ENV.to_string(),
    };
    let document = match serde_json::from_str::<Value>(&text) {
        Ok(value) => value,
        Err(_) => return KEY_ENV.to_string(),
    };
    if let Some(value) = document
        .get("destination")
        .and_then(|value| value.get("manifest"))
        .and_then(|value| value.get("api_key_env"))
        .and_then(Value::as_str)
    {
        return value.to_string();
    }
    KEY_ENV.to_string()
}

pub fn base_url_from_config(config_path: &Path, fallback: &str) -> String {
    let text = match fs::read_to_string(config_path) {
        Ok(text) => text,
        Err(_) => return fallback.to_string(),
    };
    let document = match serde_json::from_str::<Value>(&text) {
        Ok(value) => value,
        Err(_) => return fallback.to_string(),
    };
    document
        .get("destination")
        .and_then(|value| value.get("manifest"))
        .and_then(|value| value.get("base_url"))
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| fallback.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn managed_config_requires_an_explicit_instance_root() {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join(format!("j03-managed-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let root = dir.canonicalize().unwrap();
        let path = root.join("desktop-managed-host.json");

        let unconfigured = HostSettings::default();
        let error = ensure_managed_config(&path, &unconfigured).unwrap_err();
        assert_eq!(error.code, "SETTINGS_INVALID");
        assert!(!path.exists());

        let mut configured = HostSettings::default();
        configured.instance_root = Some(root.to_string_lossy().to_string());
        configured.request_budget_units = Some(200_000);
        configured.output_reserve_units = Some(4096);
        ensure_managed_config(&path, &configured).unwrap();

        let document: Value = serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(
            document["store"]["root"].as_str(),
            Some(root.to_string_lossy().as_ref())
        );
        assert_eq!(document["budget"]["request_budget_units"], 200_000);
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn managed_config_preserves_an_existing_web_section() {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join(format!("rg08-web-preserve-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let root = dir.canonicalize().unwrap();
        let path = root.join("desktop-managed-host.json");
        let mut settings = HostSettings::default();
        settings.instance_root = Some(root.to_string_lossy().to_string());
        settings.request_budget_units = Some(200_000);
        settings.output_reserve_units = Some(4096);
        ensure_managed_config(&path, &settings).unwrap();

        let mut document: Value =
            serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(document["destination"]["manifest"]["max_calls"], 999);
        document["destination"]["manifest"]["max_calls"] = json!(64);
        document["web"] = json!({
            "enabled": true,
            "search": true,
            "fetch": true,
            "timeout_seconds": 15,
            "allowed_hosts": ["example.com"],
            "capture_default": "metadata",
            "api_key_env": "DEEPSEEK_API_KEY"
        });
        fs::write(&path, serde_json::to_string_pretty(&document).unwrap()).unwrap();

        ensure_managed_config(&path, &settings).unwrap();
        let rewritten: Value =
            serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(rewritten["web"]["enabled"], true);
        assert_eq!(rewritten["destination"]["manifest"]["max_calls"], 64);
        assert_eq!(rewritten["web"]["allowed_hosts"][0], "example.com");
        assert_eq!(rewritten["app"]["profile"], "production");
        // Legacy settings without the switch keep the hand-edited block,
        // including the environment variable that user chose.
        assert_eq!(rewritten["web"]["api_key_env"], "DEEPSEEK_API_KEY");
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn managed_web_switch_owns_only_the_flag_and_credential_route() {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join(format!("rg08-web-switch-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let root = dir.canonicalize().unwrap();
        let path = root.join("desktop-managed-host.json");
        let mut settings = HostSettings::default();
        settings.instance_root = Some(root.to_string_lossy().to_string());
        settings.request_budget_units = Some(200_000);
        settings.output_reserve_units = Some(4096);
        settings.web_enabled = Some(true);
        ensure_managed_config(&path, &settings).unwrap();

        let enabled: Value =
            serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(enabled["web"]["enabled"], true);
        // The only credential the application can inject is KEY_ENV.
        assert_eq!(enabled["web"]["api_key_env"], KEY_ENV);

        // A user-owned policy field survives the next settings-driven rewrite.
        let mut document = enabled.clone();
        document["web"]["allowed_hosts"] = json!(["example.com"]);
        document["web"]["timeout_seconds"] = json!(15);
        fs::write(&path, serde_json::to_string_pretty(&document).unwrap()).unwrap();

        settings.web_enabled = Some(false);
        ensure_managed_config(&path, &settings).unwrap();
        let disabled: Value =
            serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(disabled["web"]["enabled"], false);
        assert_eq!(disabled["web"]["allowed_hosts"][0], "example.com");
        assert_eq!(disabled["web"]["timeout_seconds"], 15);
        assert_eq!(disabled["web"]["api_key_env"], KEY_ENV);
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn settings_json_keeps_web_enabled_optional() {
        let legacy: HostSettings =
            serde_json::from_str(r#"{"schema":"desktop.settings.v1"}"#).unwrap();
        assert_eq!(legacy.web_enabled, None);
        // A legacy settings file is not rewritten with a null field.
        assert!(!serde_json::to_string(&legacy).unwrap().contains("web_enabled"));
        let enabled: HostSettings =
            serde_json::from_str(r#"{"schema":"desktop.settings.v1","web_enabled":true}"#)
                .unwrap();
        assert_eq!(enabled.web_enabled, Some(true));
        let text = serde_json::to_string(&enabled).unwrap();
        assert!(text.contains("\"web_enabled\":true"));
        assert!(!text.to_lowercase().contains("api_key\":"));
    }

    #[test]
    fn external_web_switch_touches_only_the_flag() {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join(format!("rg08-external-web-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("host.json");
        let original = json!({
            "schema": "desktop.host.v1",
            "store": {"root": "x"},
            "web": {
                "search": true,
                "fetch": false,
                "timeout_seconds": 12,
                "allowed_hosts": ["example.com"],
                "capture_default": "evidence"
            }
        });
        fs::write(&path, serde_json::to_string_pretty(&original).unwrap()).unwrap();
        set_external_web_enabled(&path, true).unwrap();
        let enabled: Value =
            serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(enabled["web"]["enabled"], true);
        assert_eq!(enabled["web"]["fetch"], false);
        assert_eq!(enabled["web"]["timeout_seconds"], 12);
        assert_eq!(enabled["web"]["allowed_hosts"][0], "example.com");
        assert_eq!(enabled["web"]["capture_default"], "evidence");
        assert_eq!(enabled["store"]["root"], "x");
        assert!(enabled["web"].get("api_key_env").is_none());

        set_external_web_enabled(&path, false).unwrap();
        let disabled: Value =
            serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(disabled["web"]["enabled"], false);
        assert_eq!(disabled["web"]["allowed_hosts"][0], "example.com");

        // An absent block is created with just the switch.
        let bare = dir.join("bare.json");
        fs::write(&bare, r#"{"schema":"desktop.host.v1"}"#).unwrap();
        set_external_web_enabled(&bare, true).unwrap();
        let created: Value = serde_json::from_str(&fs::read_to_string(&bare).unwrap()).unwrap();
        assert_eq!(created["web"], json!({"enabled": true}));

        // A malformed document is refused without touching the file.
        let broken = dir.join("broken.json");
        fs::write(&broken, "{").unwrap();
        assert_eq!(
            set_external_web_enabled(&broken, true).unwrap_err().code,
            "CONFIG_INVALID"
        );
        assert_eq!(fs::read_to_string(&broken).unwrap(), "{");

        let not_object = dir.join("not-object.json");
        fs::write(&not_object, r#"{"web":"on"}"#).unwrap();
        assert_eq!(
            set_external_web_enabled(&not_object, true).unwrap_err().code,
            "CONFIG_INVALID"
        );
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn model_choices_are_closed_and_no_private_name_is_generated() {
        assert!(validate_model(DEFAULT_MODEL).is_ok());
        assert!(validate_model(PRO_MODEL).is_ok());
        assert_eq!(validate_model("other").unwrap_err().code, "INVALID_MODEL");

        let settings = HostSettings::default();
        let document = managed_config(
            &settings,
            Path::new("instance-data"),
            Path::new("instance-data/pcs.sqlite"),
            200_000,
            4096,
        );
        assert_eq!(document["app"]["profile"], "production");
        assert_eq!(document["store"]["root"], "instance-data");
        assert_eq!(document["budget"]["request_budget_units"], 200_000);
        assert!(document["app"].get("ai_display_name").is_none());
        assert_eq!(document["desktop"]["managed"], true);
        assert_eq!(
            document["destination"]["manifest"]["api_key_env"],
            KEY_ENV
        );
        assert_eq!(document["destination"]["provider"], "deepseek");
        assert_eq!(
            document["destination"]["manifest"]["thinking"],
            "enabled"
        );
        assert_eq!(
            document["destination"]["manifest"]["reasoning_effort"],
            "high"
        );
        assert_eq!(
            document["destination"]["manifest"]["protocol_version"],
            "EXP-01-THINKING-CANDIDATE-v1"
        );
        assert!(document["destination"]["manifest"]
            .get("temperature")
            .is_none());
        assert!(!document.to_string().contains("\u{5343}\u{5c81}"));
        assert!(!document.to_string().contains("sk-"));
        assert!(document["destination"]["manifest"].get("api_key").is_none());
        assert!(document["destination"]["manifest"]
            .get("api_key_env")
            .is_some());
    }

    #[test]
    fn settings_validation_rejects_untrusted_provider_and_url() {
        assert!(validate_generation("auto", 4096).is_err());
        assert!(validate_generation("on", 0).is_err());
        assert!(validate_generation("off", 393217).is_err());
        // J11: reasoning tokens share this budget, so a thinking manifest below
        // the measured floor is refused instead of producing empty proposals.
        assert!(validate_generation("on", 4096).is_err());
        assert!(validate_generation("on", 8191).is_err());
        assert!(validate_generation("on", MIN_OUTPUT_TOKENS).is_ok());
        assert!(validate_generation("on", 393216).is_ok());
        let mut settings = HostSettings::default();
        settings.provider = "other".to_string();
        assert!(validate_settings(settings).is_err());

        let mut settings = HostSettings::default();
        settings.base_url = "https://example.invalid".to_string();
        assert!(validate_settings(settings).is_err());
    }

    #[test]
    fn external_memory_settings_touch_only_owned_fields() {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join(format!("rg08-external-memory-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("host.json");
        let original = json!({
            "schema": "desktop.host.v1",
            "memory": {
                "timezone": "UTC",
                "recall": {"enabled": false, "future_budget": 9},
                "future_memory_field": "keep"
            }
        });
        fs::write(&path, serde_json::to_string_pretty(&original).unwrap()).unwrap();
        set_external_memory_settings(&path, true, 4, 2048, 1500, "Asia/Shanghai").unwrap();
        let changed: Value =
            serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(changed["memory"]["recall"]["enabled"], true);
        assert_eq!(changed["memory"]["recall"]["max_items"], 4);
        assert_eq!(changed["memory"]["recall"]["budget_units"], 2048);
        assert_eq!(changed["memory"]["recall"]["timeout_seconds"], 1.5);
        assert_eq!(changed["memory"]["timezone"], "Asia/Shanghai");
        assert_eq!(changed["memory"]["recall"]["future_budget"], 9);
        assert_eq!(changed["memory"]["future_memory_field"], "keep");
        assert!(set_external_memory_settings(&path, true, 4, 2048, 1500, "Mars/Base").is_err());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn managed_memory_settings_preserve_unowned_memory_fields() {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join(format!("rg08-managed-memory-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let root = dir.canonicalize().unwrap();
        let path = root.join("desktop-managed-host.json");
        let mut settings = HostSettings::default();
        settings.instance_root = Some(root.to_string_lossy().to_string());
        settings.request_budget_units = Some(200_000);
        settings.output_reserve_units = Some(4096);
        settings.memory_recall_enabled = Some(true);
        settings.memory_recall_max_items = Some(3);
        settings.memory_recall_budget_units = Some(1024);
        settings.memory_recall_timeout_ms = Some(1200);
        settings.memory_timezone = Some("UTC".to_string());
        ensure_managed_config(&path, &settings).unwrap();
        let first: Value =
            serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(first["memory"]["recall"]["enabled"], true);
        assert_eq!(first["memory"]["recall"]["max_items"], 3);
        assert_eq!(first["memory"]["recall"]["timeout_seconds"], 1.2);
        assert_eq!(first["memory"]["timezone"], "UTC");

        let mut document = first.clone();
        document["memory"]["future_memory_field"] = json!("keep");
        document["memory"]["recall"]["future_budget"] = json!(9);
        fs::write(&path, serde_json::to_string_pretty(&document).unwrap()).unwrap();
        settings.memory_recall_enabled = Some(false);
        ensure_managed_config(&path, &settings).unwrap();
        let second: Value =
            serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(second["memory"]["recall"]["enabled"], false);
        assert_eq!(second["memory"]["future_memory_field"], "keep");
        assert_eq!(second["memory"]["recall"]["future_budget"], 9);

        settings.memory_recall_enabled = None;
        settings.memory_recall_max_items = None;
        settings.memory_recall_budget_units = None;
        settings.memory_recall_timeout_ms = None;
        settings.memory_timezone = None;
        ensure_managed_config(&path, &settings).unwrap();
        let third: Value =
            serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(third["memory"]["future_memory_field"], "keep");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn settings_validation_requires_a_real_root_and_a_sane_budget() {
        let mut settings = HostSettings::default();
        settings.instance_root = Some("relative/root".to_string());
        assert!(validate_settings(settings).is_err());

        let mut settings = HostSettings::default();
        settings.request_budget_units = Some(1000);
        settings.output_reserve_units = Some(900);
        assert!(validate_settings(settings).is_err());

        let mut settings = HostSettings::default();
        settings.request_budget_units = Some(200_000);
        assert!(validate_settings(settings).is_err());
    }
}
