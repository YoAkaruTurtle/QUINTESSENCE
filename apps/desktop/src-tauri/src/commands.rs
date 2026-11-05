//! Typed Tauri commands.  The WebView only gets the operations registered in
//! `lib.rs`; every conversation command forwards a fixed key set and lets the
//! Python host own authority, retention and destination checks.

use std::fs;
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Value};
use tauri::State;

use crate::key_store;
use crate::host_settings;
use crate::preferences;
use crate::sidecar::{HostError, HostReply, SidecarManager};
use crate::AppState;

static INSTANCE_SWITCHING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
struct InstanceSwitchGuard;
impl Drop for InstanceSwitchGuard { fn drop(&mut self) { INSTANCE_SWITCHING.store(false, std::sync::atomic::Ordering::Release); } }

static CREDENTIALS_PENDING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
/// One writer for `desktop-settings.json` and the generated host config.
///
/// Generation and web saves both rewrite the same two files; a shared guard
/// makes them serialize instead of losing one field through a last-writer-wins
/// race.  It is deliberately independent of the sidecar's own save mutex.
static SETTINGS_SAVING: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);

struct SettingsSaveGuard;

impl Drop for SettingsSaveGuard {
    fn drop(&mut self) {
        SETTINGS_SAVING.store(false, std::sync::atomic::Ordering::Release);
    }
}

fn begin_settings_save() -> Option<SettingsSaveGuard> {
    if SETTINGS_SAVING.swap(true, std::sync::atomic::Ordering::AcqRel) {
        None
    } else {
        Some(SettingsSaveGuard)
    }
}

/// RG02 conversation methods.  These names are the only bridge methods the
/// new Tauri commands may call.
pub const METHOD_CONVERSATION_CREATE: &str = "conversation.create";
pub const METHOD_CONVERSATION_LIST: &str = "conversation.list";
pub const METHOD_CONVERSATION_GET: &str = "conversation.get";
pub const METHOD_CONVERSATION_SEND: &str = "conversation.send";
pub const METHOD_CONVERSATION_SEARCH: &str = "conversation.search";
pub const METHOD_MEMORY_SEARCH: &str = "memory.search";
pub const METHOD_TURN_RESULT: &str = "turn.result";
pub const METHOD_TURN_EVENTS: &str = "turn.events";
pub const METHOD_TURN_STEPS: &str = "turn.steps";
pub const METHOD_TURN_TRACE: &str = "turn.trace";
pub const METHOD_CONVERSATION_USAGE: &str = "conversation.usage";
pub const METHOD_TURN_CANCEL: &str = "turn.cancel";
pub const METHOD_POSTPROCESS_STATUS: &str = "postprocess.status";
pub const METHOD_POSTPROCESS_RETRY: &str = "postprocess.retry";
pub const METHOD_MAINTENANCE_STATUS: &str = "maintenance.status";
pub const METHOD_MAINTENANCE_RUN: &str = "maintenance.run";
pub const METHOD_MAINTENANCE_CONTROL: &str = "maintenance.control";
pub const METHOD_COGNITION_CANDIDATES: &str = "cognition.candidates.list";
pub const METHOD_COGNITION_DECIDE: &str = "cognition.candidate.decide";
pub const METHOD_PROFILE_STATUS: &str = "profile.status";
pub const METHOD_PROFILE_READ: &str = "profile.read";
pub const METHOD_PROFILE_PUBLISH: &str = "profile.publish";
pub const METHOD_PROFILE_ACTIVATE: &str = "profile.activate";
pub const METHOD_PROFILE_LIST: &str = "profile.list";
pub const METHOD_PROFILE_READ_BODY: &str = "profile.read_body";
pub const METHOD_PROFILE_IMPORT_LIST: &str = "profile.import.list";
pub const METHOD_PROFILE_IMPORT_PREVIEW: &str = "profile.import.preview";
pub const METHOD_PROFILE_PUBLISH_REVIEWED: &str = "profile.publish_reviewed";
pub const METHOD_PREFERENCE_LIST: &str = "preference.list";
pub const METHOD_PREFERENCE_REVOKE: &str = "preference.revoke";

/// RG05 cognition reads are short DB reads on the single DB worker.
pub const COGNITION_READ_TIMEOUT: Duration = Duration::from_secs(60);
/// A cognition decision may queue behind a write transaction.
pub const COGNITION_DECIDE_TIMEOUT: Duration = Duration::from_secs(120);

/// The persona status answer is an in-memory read on the sidecar reader thread.
pub const PROFILE_STATUS_TIMEOUT: Duration = Duration::from_secs(15);
/// Profile read/publish/activate queue on the single DB worker.
pub const PROFILE_ACTION_TIMEOUT: Duration = Duration::from_secs(120);

/// A send returns after T1 commits; it must never wait for the model.
pub const CONVERSATION_SEND_TIMEOUT: Duration = Duration::from_secs(120);
/// A read may queue behind a non-turn maintenance job; 300 s is insurance, not
/// the normal path (R2 refuses reads while a turn is running).
pub const CONVERSATION_READ_TIMEOUT: Duration = Duration::from_secs(300);
/// A reader-thread answer (ring hit or NOT_RUNNING); never queues behind a turn.
pub const TURN_CANCEL_TIMEOUT: Duration = Duration::from_secs(25);
/// One bounded memory search runs a short local adapter process.
pub const MEMORY_SEARCH_TIMEOUT: Duration = Duration::from_secs(30);
/// Maintenance status is an in-memory service view on the reader thread.
pub const MAINTENANCE_STATUS_TIMEOUT: Duration = Duration::from_secs(25);
/// One bounded maintenance slice; 90 s slice + one manual repair headroom.
pub const MAINTENANCE_RUN_TIMEOUT: Duration = Duration::from_secs(300);
/// Pause/cancel/yield transitions are bounded state writes.
pub const MAINTENANCE_CONTROL_TIMEOUT: Duration = Duration::from_secs(30);

pub fn conversation_send_params(
    session_id: &str,
    conversation_id: &str,
    client_request_id: &str,
    text: &str,
    confirmed_destination_id: Option<&str>,
) -> Value {
    json!({
        "session_id": session_id,
        "conversation_id": conversation_id,
        "client_request_id": client_request_id,
        "text": text,
        "confirmed_destination_id": confirmed_destination_id,
    })
}

pub fn conversation_get_params(
    session_id: &str,
    conversation_id: &str,
    before_turn_no: Option<u32>,
    before_seq: Option<u32>,
    limit: Option<u32>,
) -> Value {
    json!({
        "session_id": session_id,
        "conversation_id": conversation_id,
        "before_turn_no": before_turn_no,
        "before_seq": before_seq,
        "limit": limit,
    })
}

pub fn conversation_list_params(
    session_id: &str,
    limit: Option<u32>,
    cursor: Option<&str>,
) -> Value {
    json!({
        "session_id": session_id,
        "limit": limit,
        "cursor": cursor,
    })
}

pub fn conversation_create_params(session_id: &str, mode: &str) -> Value {
    json!({"session_id": session_id, "mode": mode})
}

pub fn turn_result_params(session_id: &str, turn_id: &str) -> Value {
    json!({"session_id": session_id, "turn_id": turn_id})
}

pub fn turn_events_params(
    session_id: &str,
    turn_id: &str,
    after_seq: Option<u32>,
    limit: Option<u32>,
) -> Value {
    json!({
        "session_id": session_id,
        "turn_id": turn_id,
        "after_seq": after_seq,
        "limit": limit,
    })
}

pub fn turn_steps_params(
    session_id: &str,
    turn_id: &str,
    after_step_no: Option<u32>,
    limit: Option<u32>,
) -> Value {
    json!({
        "session_id": session_id,
        "turn_id": turn_id,
        "after_step_no": after_step_no,
        "limit": limit,
    })
}

pub fn turn_cancel_params(session_id: &str, turn_id: &str) -> Value {
    json!({"session_id": session_id, "turn_id": turn_id})
}

pub fn maintenance_run_params(session_id: &str, task_id: Option<&str>) -> Value {
    let _ = session_id;
    json!({
        "event_ids": [],
        "task_id": task_id,
        "limit": null,
    })
}

pub fn maintenance_control_params(
    session_id: &str,
    task_id: Option<&str>,
    action: &str,
) -> Value {
    let _ = session_id;
    json!({"task_id": task_id, "action": action})
}

pub fn turn_trace_params(session_id: &str, turn_id: &str) -> Value {
    json!({"session_id": session_id, "turn_id": turn_id})
}

pub fn conversation_usage_params(
    session_id: &str,
    conversation_id: &str,
    turn_id: Option<&str>,
) -> Value {
    json!({
        "session_id": session_id,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
    })
}

pub fn conversation_search_params(
    session_id: &str,
    query: &str,
    limit: Option<u32>,
    conversation_id: Option<&str>,
    before: Option<&str>,
    include_full: Option<bool>,
) -> Value {
    json!({
        "session_id": session_id,
        "query": query,
        "limit": limit,
        "conversation_id": conversation_id,
        "before": before,
        "include_full": include_full,
    })
}

pub fn memory_search_params(
    session_id: &str,
    query: &str,
    limit: Option<u32>,
    cursor: Option<&str>,
) -> Value {
    json!({
        "session_id": session_id,
        "query": query,
        "limit": limit,
        "cursor": cursor,
    })
}

pub fn postprocess_status_params(
    session_id: &str,
    conversation_id: &str,
    turn_ids: Option<&[String]>,
) -> Value {
    json!({
        "session_id": session_id,
        "conversation_id": conversation_id,
        "turn_ids": turn_ids,
    })
}

pub fn postprocess_retry_params(session_id: &str, turn_id: &str) -> Value {
    json!({"session_id": session_id, "turn_id": turn_id})
}

pub fn cognition_candidates_params(session_id: &str, limit: Option<u32>) -> Value {
    json!({"session_id": session_id, "limit": limit})
}

pub fn cognition_decide_params(
    session_id: &str,
    candidate_id: &str,
    artifact_version: u32,
    decision: &str,
) -> Value {
    json!({
        "session_id": session_id,
        "candidate_id": candidate_id,
        "artifact_version": artifact_version,
        "decision": decision,
    })
}

pub fn profile_publish_params(
    session_id: &str,
    profile_id: &str,
    body: Value,
    review_event_id: &str,
) -> Value {
    json!({
        "session_id": session_id,
        "profile_id": profile_id,
        "body": body,
        "review_event_id": review_event_id,
    })
}

pub fn profile_activate_params(
    session_id: &str,
    profile_id: &str,
    record_version: u32,
) -> Value {
    json!({
        "session_id": session_id,
        "profile_id": profile_id,
        "record_version": record_version,
    })
}

pub fn profile_read_body_params(
    session_id: &str,
    record_id: &str,
    record_version: u32,
) -> Value {
    json!({
        "session_id": session_id,
        "record_id": record_id,
        "record_version": record_version,
    })
}

pub fn profile_import_preview_params(session_id: &str, name: &str) -> Value {
    json!({"session_id": session_id, "name": name})
}

pub fn profile_publish_reviewed_params(
    session_id: &str,
    import_id: &str,
    profile_id: Option<&str>,
    confirm: bool,
) -> Value {
    json!({
        "session_id": session_id,
        "import_id": import_id,
        "profile_id": profile_id,
        "confirm": confirm,
    })
}

pub fn preference_list_params(session_id: &str, conversation_id: Option<&str>) -> Value {
    json!({"session_id": session_id, "conversation_id": conversation_id})
}

pub fn preference_revoke_params(
    session_id: &str,
    conversation_id: &str,
    dimension: &str,
    reason: Option<&str>,
) -> Value {
    json!({
        "session_id": session_id,
        "conversation_id": conversation_id,
        "dimension": dimension,
        "reason": reason,
    })
}

fn lock_error() -> HostReply {
    HostReply::error(HostError::new("HOST_INTERNAL", "宿主状态锁不可用"))
}

fn manager(state: &AppState) -> Result<Arc<SidecarManager>, HostReply> {
    let manager = state
        .manager
        .lock()
        .map_err(|_| HostReply::error(HostError::new("HOST_INTERNAL", "sidecar 状态锁不可用")))?
        .clone();
    manager.ok_or_else(|| {
        HostReply::error(
            state
                .startup_error
                .clone()
                .unwrap_or_else(|| HostError::new("SIDECAR_UNAVAILABLE", "sidecar 未初始化")),
        )
    })
}

async fn call(
    state: &AppState,
    method: &'static str,
    params: Value,
    timeout: Duration,
) -> HostReply {
    let started = std::time::Instant::now();
    let manager = match manager(state) {
        Ok(manager) => manager,
        Err(reply) => {
            state
                .logs
                .record_ipc(method, &reply, started.elapsed().as_millis() as u64);
            return reply;
        }
    };
    let reply = match tauri::async_runtime::spawn_blocking(move || {
        if INSTANCE_SWITCHING.load(std::sync::atomic::Ordering::Acquire) && !matches!(method, "app.info" | "bridge.ping") {
            return HostReply::error(HostError::new("BUSY", "正在连接实例，请稍后重试"));
        }
        manager.request_with_timeout(method, params, timeout)
    })
    .await
    {
        Ok(reply) => reply,
        Err(_) => HostReply::error(HostError::new("HOST_INTERNAL", "sidecar 调用线程失败")),
    };
    state
        .logs
        .record_ipc(method, &reply, started.elapsed().as_millis() as u64);
    reply
}

/// Re-read `app.info`, attach the desktop-only UI preference and cache it.
async fn refresh(state: &AppState) -> Result<Value, HostReply> {
    let reply = call(state, "app.info", json!({}), Duration::from_secs(25)).await;
    if !reply.ok {
        return Err(reply);
    }
    let mut info = reply.result.clone().unwrap_or(Value::Null);
    let host_default = info
        .get("ai_display_name")
        .and_then(Value::as_str)
        .map(str::to_string);
    let (name, source) = {
        let preferences = state
            .preferences
            .lock()
            .map_err(|_| HostReply::error(HostError::new("HOST_INTERNAL", "偏好锁不可用")))?;
        preferences::effective(&preferences, host_default.as_deref())
    };
    if let Some(object) = info.as_object_mut() {
        object.insert(
            "ui".to_string(),
            json!({"ai_display_name": name, "source": source}),
        );
    }
    let mut session = state
        .session
        .lock()
        .map_err(|_| HostReply::error(HostError::new("HOST_INTERNAL", "会话锁不可用")))?;
    session.update(info.clone()).map_err(HostReply::error)?;
    drop(session);
    // Ready transitions are logged once each; polling `app.info` stays quiet.
    let ready = info
        .get("host")
        .and_then(|value| value.get("ready"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let changed = {
        let mut last = state
            .host_ready
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let changed = last.map(|previous| previous != ready).unwrap_or(true);
        if changed {
            *last = Some(ready);
        }
        changed
    };
    if changed {
        if ready {
            state
                .logs
                .record_lifecycle(crate::logs::Level::Info, "host.ready", "sidecar=ready");
        } else {
            let code = info
                .get("host")
                .and_then(|value| value.get("reason_code"))
                .and_then(Value::as_str)
                .unwrap_or("UNKNOWN");
            state.logs.record_lifecycle(
                crate::logs::Level::Warn,
                "host.not_ready",
                &format!("code={code}"),
            );
        }
    }
    Ok(info)
}

/// Re-read `app.info`, then prove the caller belongs to the current session.
async fn ready_session(state: &AppState, session_id: &str) -> Option<HostReply> {
    if let Err(reply) = refresh(state).await {
        return Some(reply);
    }
    let session = match state.session.lock() {
        Ok(session) => session,
        Err(_) => {
            return Some(HostReply::error(HostError::new(
                "HOST_INTERNAL",
                "会话锁不可用",
            )))
        }
    };
    session
        .validate_session(session_id)
        .err()
        .map(HostReply::error)
}

async fn model_status_value(state: &AppState) -> Value {
    let info_reply = call(state, "app.info", json!({}), Duration::from_secs(10)).await;
    let info = info_reply.result.clone().unwrap_or(Value::Null);
    let settings = state
        .settings
        .lock()
        .map(|settings| settings.clone())
        .unwrap_or_default();
    let key_in_environment = std::env::var(&state.api_key_env).map(|value| !value.is_empty()).unwrap_or(false);
    let key_state = if key_in_environment { Ok(None) } else { key_store::read() };
    let key_saved = key_state.as_ref().map(|value| value.is_some()).unwrap_or(false);
    let key_store_error = key_state.err().map(|error| error.code);
    let managed = state.managed_config;
    let provider = if managed {
        settings.provider.clone()
    } else {
        info.get("destination")
            .and_then(|value| value.get("provider"))
            .and_then(Value::as_str)
            .unwrap_or(settings.provider.as_str())
            .to_string()
    };
    let model = if managed {
        settings.model.clone()
    } else {
        info.get("destination")
            .and_then(|value| value.get("model"))
            .and_then(Value::as_str)
            .unwrap_or(settings.model.as_str())
            .to_string()
    };
    let base_url = if managed {
        settings.base_url.clone()
    } else {
        host_settings::base_url_from_config(&state.config_path, settings.base_url.as_str())
    };
    let host = info.get("host").cloned().unwrap_or(Value::Null);
    json!({
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "managed": managed,
        "model_choices": host_settings::MODEL_CHOICES,
        "key_saved": key_saved,
        "key_available": key_saved || key_in_environment,
        "credential_source": if key_in_environment { "environment" } else if key_saved { "credential_manager" } else { "none" },
        "credential_restart_required": CREDENTIALS_PENDING.load(std::sync::atomic::Ordering::Acquire),
        "key_store_error": key_store_error,
        "sidecar_available": state.manager.lock().map(|guard| guard.is_some()).unwrap_or(false),
        "host_ready": host.get("ready").and_then(Value::as_bool).unwrap_or(false),
        "reason_code": host.get("reason_code").cloned().unwrap_or(Value::Null),
        "reason_detail": host.get("reason_detail").cloned().unwrap_or(Value::Null),
    })
}

#[tauri::command]
pub async fn model_status(state: State<'_, AppState>) -> Result<HostReply, String> {
    Ok(HostReply::ok(model_status_value(&state).await))
}

#[tauri::command]
pub async fn save_model_api_key(
    state: State<'_, AppState>,
    api_key: String,
) -> Result<HostReply, String> {
    if let Err(error) = key_store::validate_key(&api_key) {
        return Ok(HostReply::error(error));
    }
    if let Err(error) = key_store::save(&api_key) {
        return Ok(HostReply::error(error));
    }
    CREDENTIALS_PENDING.store(true, std::sync::atomic::Ordering::Release);
    let status = model_status_value(&state).await;
    Ok(HostReply::ok(json!({
        "saved": true,
        "restart_required": true,
        "status": status,
    })))
}

#[tauri::command]
pub async fn delete_model_api_key(state: State<'_, AppState>) -> Result<HostReply, String> {
    let deleted = match key_store::delete() {
        Ok(value) => value,
        Err(error) => return Ok(HostReply::error(error)),
    };
    CREDENTIALS_PENDING.store(true, std::sync::atomic::Ordering::Release);
    let status = model_status_value(&state).await;
    Ok(HostReply::ok(json!({
        "deleted": deleted,
        "restart_required": true,
        "status": status,
    })))
}

#[tauri::command]
pub async fn set_model_choice(
    state: State<'_, AppState>,
    model: String,
) -> Result<HostReply, String> {
    let status = generation_status_value(&state);
    let mode = status["sdrpe_mode"].as_str().unwrap_or("off").to_string();
    let tokens = status["max_output_tokens"].as_u64().unwrap_or(8192) as u32;
    save_generation_settings(state, mode, tokens, model).await
}

fn generation_status_value(state: &AppState) -> Value {
    let doc = std::fs::read_to_string(&state.config_path).ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok()).unwrap_or(Value::Null);
    json!({
        "managed": state.managed_config,
        "model": doc.pointer("/destination/model"),
        "model_choices": if state.managed_config { host_settings::MODEL_CHOICES.to_vec() } else { vec![] },
        "sdrpe_mode": doc.pointer("/conversation/sdrpe/mode").and_then(Value::as_str).unwrap_or("off"),
        "max_output_tokens": doc.pointer("/destination/manifest/max_output_tokens_per_call"),
        "temperature": doc.pointer("/destination/manifest/temperature"),
        "thinking": doc.pointer("/destination/manifest/thinking"),
        "effect": "next_turn"
    })
}

#[tauri::command]
pub async fn generation_settings(state: State<'_, AppState>) -> Result<HostReply, String> {
    Ok(HostReply::ok(generation_status_value(&state)))
}

#[tauri::command]
pub async fn save_generation_settings(
    state: State<'_, AppState>,
    sdrpe_mode: String,
    max_output_tokens: u32,
    model: String,
) -> Result<HostReply, String> {
    let _saving = match begin_settings_save() {
        Some(guard) => guard,
        None => return Ok(HostReply::error(HostError::new("BUSY", "正在保存设置"))),
    };
    if let Err(error) = host_settings::validate_generation(&sdrpe_mode, max_output_tokens) {
        return Ok(HostReply::error(error));
    }
    if !state.managed_config {
        let status = generation_status_value(&state);
        if status["model"].as_str() != Some(model.as_str()) || status["max_output_tokens"].as_u64() != Some(max_output_tokens as u64) {
            return Ok(HostReply::error(HostError::new("MODEL_CONFIG_EXTERNAL", "外部模型清单由宿主管理；可调整 SDRPE")));
        }
        let mut doc: Value = match std::fs::read_to_string(&state.config_path).ok().and_then(|text| serde_json::from_str(&text).ok()) {
            Some(doc) => doc,
            None => return Ok(HostReply::error(HostError::new("SETTINGS_UNAVAILABLE", "无法读取当前宿主配置"))),
        };
        if doc.get("conversation").is_none() { doc["conversation"] = json!({}); }
        if !doc["conversation"].is_object() {
            return Ok(HostReply::error(HostError::new("SETTINGS_INVALID", "宿主 conversation 配置不是对象")));
        }
        doc["conversation"]["sdrpe"] = json!({"mode": sdrpe_mode});
        if let Err(error) = host_settings::atomic_write(&state.config_path, &serde_json::to_string_pretty(&doc).unwrap()) {
            return Ok(HostReply::error(error));
        }
        let applied = call(&state, "settings.reload", json!({}), Duration::from_secs(300)).await;
        return Ok(HostReply::ok(json!({"saved": true, "application": applied, "status": generation_status_value(&state)})));
    }
    if let Err(error) = host_settings::validate_model(&model) {
        return Ok(HostReply::error(error));
    }
    let mut settings = match state.settings.lock() {
        Ok(settings) => settings.clone(),
        Err(_) => return Ok(lock_error()),
    };
    let previous = settings.clone();
    settings.model = model;
    settings.sdrpe_mode = sdrpe_mode;
    settings.max_output_tokens = max_output_tokens;
    if let Err(error) = host_settings::save(&state.settings_path, &settings) {
        return Ok(HostReply::error(error));
    }
    if let Err(error) = host_settings::ensure_managed_config(&state.config_path, &settings) {
        if host_settings::save(&state.settings_path, &previous).is_err() {
            return Ok(HostReply::error(HostError::unknown("SETTINGS_UNAVAILABLE", "保存未完成，持久化状态无法确认")));
        }
        return Ok(HostReply::error(error));
    }
    if let Ok(mut current) = state.settings.lock() {
        *current = settings;
    }
    let applied = call(&state, "settings.reload", json!({}), Duration::from_secs(300)).await;
    let status = generation_status_value(&state);
    Ok(HostReply::ok(json!({
        "saved": true,
        "application": applied,
        "status": status,
    })))
}

/// RG08: the shape reported when the sidecar cannot answer at all.
fn default_web_status() -> Value {
    json!({
        "enabled": false,
        "configured": false,
        "declared": false,
        "search": false,
        "fetch": false,
        "credential_present": false,
        "credential_env": Value::Null,
        "allowed_hosts": 0,
        "host_policy": Value::Null,
        "source": Value::Null,
        "missing": ["host"],
    })
}

/// Compose the renderer-facing web status without inventing a capability.
///
/// Everything about the configured surface (switch, credential presence,
/// allowlist policy, missing configuration facts, source) comes from
/// `app.info.capabilities.web`.  This function only adds the two host facts the
/// sidecar cannot know: whether the app may rewrite the configuration
/// (`editable`) and whether a credential change still waits for a restart.
fn compose_web_status(
    info: &Value,
    managed: bool,
    config_writable: bool,
    sidecar_available: bool,
    credential_restart_required: bool,
) -> Value {
    let mut status = match info
        .pointer("/capabilities/web")
        .and_then(Value::as_object)
    {
        Some(object) => Value::Object(object.clone()),
        None => default_web_status(),
    };
    let source_known = status
        .get("source")
        .and_then(Value::as_str)
        .is_some();
    let editable = sidecar_available && source_known && (managed || config_writable);
    let host = info.get("host").cloned().unwrap_or(Value::Null);
    let object = status
        .as_object_mut()
        .expect("web status is always an object");
    object.insert("managed".to_string(), json!(managed));
    object.insert("editable".to_string(), json!(editable));
    object.insert(
        "sidecar_available".to_string(),
        json!(sidecar_available),
    );
    object.insert(
        "credential_restart_required".to_string(),
        json!(credential_restart_required),
    );
    object.insert(
        "host_ready".to_string(),
        json!(host.get("ready").and_then(Value::as_bool).unwrap_or(false)),
    );
    object.insert(
        "reason_code".to_string(),
        host.get("reason_code").cloned().unwrap_or(Value::Null),
    );
    object.insert("apply".to_string(), json!("next_turn"));
    status
}

fn config_is_writable(path: &Path) -> bool {
    fs::metadata(path)
        .map(|metadata| !metadata.permissions().readonly())
        .unwrap_or(false)
}

async fn web_status_value(state: &AppState) -> Value {
    let reply = call(state, "app.info", json!({}), Duration::from_secs(10)).await;
    let sidecar_available = reply.ok;
    let info = reply.result.unwrap_or(Value::Null);
    compose_web_status(
        &info,
        state.managed_config,
        config_is_writable(&state.config_path),
        sidecar_available,
        CREDENTIALS_PENDING.load(std::sync::atomic::Ordering::Acquire),
    )
}

/// RG08: read the real web status.  Read-only; never triggers a network call.
#[tauri::command]
pub async fn web_settings(state: State<'_, AppState>) -> Result<HostReply, String> {
    Ok(HostReply::ok(web_status_value(&state).await))
}

/// RG08: flip the real web switch.
///
/// A managed install writes the switch into `desktop-settings.json` and
/// regenerates the managed host config, preserving the rest of its `web`
/// block.  An external/lab config is rewritten atomically in place, touching
/// only `web.enabled`.  Either way the sidecar reloads at the worker boundary:
/// the change applies to the next turn and never interrupts a running one.
#[tauri::command]
pub async fn save_web_settings(
    state: State<'_, AppState>,
    enabled: bool,
) -> Result<HostReply, String> {
    let _saving = match begin_settings_save() {
        Some(guard) => guard,
        None => return Ok(HostReply::error(HostError::new("BUSY", "正在保存设置"))),
    };
    if state.managed_config {
        let mut settings = match state.settings.lock() {
            Ok(settings) => settings.clone(),
            Err(_) => return Ok(lock_error()),
        };
        let previous = settings.clone();
        settings.web_enabled = Some(enabled);
        if let Err(error) = host_settings::save(&state.settings_path, &settings) {
            return Ok(HostReply::error(error));
        }
        if let Err(error) = host_settings::ensure_managed_config(&state.config_path, &settings) {
            if host_settings::save(&state.settings_path, &previous).is_err() {
                return Ok(HostReply::error(HostError::unknown(
                    "SETTINGS_UNAVAILABLE",
                    "保存未完成，持久化状态无法确认",
                )));
            }
            return Ok(HostReply::error(error));
        }
        if let Ok(mut current) = state.settings.lock() {
            *current = settings;
        }
    } else if let Err(error) =
        host_settings::set_external_web_enabled(&state.config_path, enabled)
    {
        return Ok(HostReply::error(error));
    }
    let applied = call(&state, "settings.reload", json!({}), Duration::from_secs(300)).await;
    let applied_ok = applied.ok;
    let status = web_status_value(&state).await;
    Ok(HostReply::ok(json!({
        "saved": true,
        "applied": applied_ok,
        "application": applied,
        "status": status,
    })))
}

fn default_memory_recall_status() -> Value {
    json!({
        "enabled": false,
        "configured": false,
        "declared": false,
        "bridge_configured": false,
        "max_items": 4,
        "budget_units": 2048,
        "timeout_seconds": 2.0,
        "timezone": "Asia/Shanghai",
        "source": Value::Null,
        "missing": ["host"],
        "apply": "next_turn",
    })
}

fn compose_memory_recall_status(
    info: &Value,
    managed: bool,
    config_writable: bool,
    sidecar_available: bool,
) -> Value {
    let mut status = match info.pointer("/capabilities/memory/recall").and_then(Value::as_object) {
        Some(object) => Value::Object(object.clone()),
        None => default_memory_recall_status(),
    };
    let source_known = status.get("source").and_then(Value::as_str).is_some();
    let bridge_ready = status
        .get("bridge_configured")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let editable = sidecar_available
        && source_known
        && bridge_ready
        && (managed || config_writable);
    let object = status
        .as_object_mut()
        .expect("memory recall status is always an object");
    object.insert("managed".to_string(), json!(managed));
    object.insert("editable".to_string(), json!(editable));
    object.insert("sidecar_available".to_string(), json!(sidecar_available));
    object.insert(
        "host_ready".to_string(),
        json!(info
            .pointer("/host/ready")
            .and_then(Value::as_bool)
            .unwrap_or(false)),
    );
    object.insert("apply".to_string(), json!("next_turn"));
    status
}

async fn memory_recall_status_value(state: &AppState) -> Value {
    let reply = call(state, "app.info", json!({}), Duration::from_secs(10)).await;
    let sidecar_available = reply.ok;
    let info = reply.result.unwrap_or(Value::Null);
    compose_memory_recall_status(
        &info,
        state.managed_config,
        config_is_writable(&state.config_path),
        sidecar_available,
    )
}

/// RG08 R-B: read the real automatic-recall status.  Never triggers recall.
#[tauri::command]
pub async fn memory_recall_settings(state: State<'_, AppState>) -> Result<HostReply, String> {
    Ok(HostReply::ok(memory_recall_status_value(&state).await))
}

/// RG08 R-B: save the bounded automatic-recall settings.
///
/// Managed installs write `desktop-settings.json` and regenerate the managed
/// host config, preserving every other `memory` field.  External/Lab configs
/// are rewritten atomically, touching only the recall/timezone fields.  Either
/// path calls `settings.reload` at the worker boundary, so the change applies
/// from the next turn and never interrupts a running one.
#[tauri::command]
pub async fn save_memory_recall_settings(
    state: State<'_, AppState>,
    enabled: bool,
    max_items: u32,
    budget_units: u32,
    timeout_ms: u32,
    timezone: String,
) -> Result<HostReply, String> {
    let _saving = match begin_settings_save() {
        Some(guard) => guard,
        None => return Ok(HostReply::error(HostError::new("BUSY", "正在保存设置"))),
    };
    if !host_settings::MEMORY_TIMEZONES.contains(&timezone.as_str()) {
        return Ok(HostReply::error(HostError::new(
            "SETTINGS_INVALID",
            "自动召回时区不受支持",
        )));
    }
    if state.managed_config {
        let mut settings = match state.settings.lock() {
            Ok(settings) => settings.clone(),
            Err(_) => return Ok(lock_error()),
        };
        let previous = settings.clone();
        settings.memory_recall_enabled = Some(enabled);
        settings.memory_recall_max_items = Some(max_items);
        settings.memory_recall_budget_units = Some(budget_units);
        settings.memory_recall_timeout_ms = Some(timeout_ms);
        settings.memory_timezone = Some(timezone.clone());
        if let Err(error) = host_settings::save(&state.settings_path, &settings) {
            return Ok(HostReply::error(error));
        }
        if let Err(error) = host_settings::ensure_managed_config(&state.config_path, &settings) {
            if host_settings::save(&state.settings_path, &previous).is_err() {
                return Ok(HostReply::error(HostError::unknown(
                    "SETTINGS_UNAVAILABLE",
                    "保存未完成，持久化状态无法确认",
                )));
            }
            return Ok(HostReply::error(error));
        }
        if let Ok(mut current) = state.settings.lock() {
            *current = settings;
        }
    } else if let Err(error) = host_settings::set_external_memory_settings(
        &state.config_path,
        enabled,
        max_items,
        budget_units,
        timeout_ms,
        &timezone,
    ) {
        return Ok(HostReply::error(error));
    }
    let applied = call(&state, "settings.reload", json!({}), Duration::from_secs(300)).await;
    let applied_ok = applied.ok;
    let status = memory_recall_status_value(&state).await;
    Ok(HostReply::ok(json!({
        "saved": true,
        "applied": applied_ok,
        "application": applied,
        "status": status,
    })))
}

#[tauri::command]
pub async fn test_model_connection(
    state: State<'_, AppState>,
    session_id: String,
) -> Result<HostReply, String> {
    if CREDENTIALS_PENDING.load(std::sync::atomic::Ordering::Acquire) {
        return Ok(HostReply::error(HostError::new("RESTART_REQUIRED", "凭据已更改，重启应用后才能测试新凭据")));
    }
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        "model.test_connection",
        json!({"session_id": session_id}),
        Duration::from_secs(45),
    )
    .await)
}

#[tauri::command]
pub async fn set_ai_display_name(
    state: State<'_, AppState>,
    name: String,
) -> Result<HostReply, String> {
    let validated = match preferences::validate(&name) {
        Ok(value) => value,
        Err(error) => return Ok(HostReply::error(error)),
    };
    let mut preferences = match state.preferences.lock() {
        Ok(preferences) => preferences,
        Err(_) => {
            return Ok(HostReply::error(HostError::new(
                "HOST_INTERNAL",
                "偏好锁不可用",
            )))
        }
    };
    let mut updated = preferences.clone();
    updated.ai_display_name = validated;
    updated.ai_display_name_explicit = Some(updated.ai_display_name.is_some());
    if let Err(error) = preferences::save(&state.preferences_path, &updated) {
        return Ok(HostReply::error(error));
    }
    *preferences = updated;
    let host_default = state.session.lock().ok().and_then(|session| {
        session
            .info()
            .and_then(|info| info.get("ai_display_name"))
            .and_then(Value::as_str)
            .map(str::to_string)
    });
    let (effective, source) = preferences::effective(&preferences, host_default.as_deref());
    Ok(HostReply::ok(json!({
        "ai_display_name": effective,
        "source": source,
    })))
}

#[tauri::command]
pub async fn ui_preferences(state: State<'_, AppState>) -> Result<HostReply, String> {
    let value = state.preferences.lock().map_err(|_| "preferences unavailable")?;
    Ok(HostReply::ok(json!({"language": value.language, "theme": value.theme, "sidebar_collapsed": value.sidebar_collapsed, "panel_height": value.panel_height})))
}

#[tauri::command]
pub async fn save_layout_preferences(state: State<'_, AppState>, sidebar_collapsed: bool, panel_height: u32) -> Result<HostReply, String> {
    if !(240..=1600).contains(&panel_height) {
        return Ok(HostReply::error(HostError::new("INVALID_PARAMS", "panel height must be 240..1600")));
    }
    let mut current = state.preferences.lock().map_err(|_| "preferences unavailable")?;
    let mut updated = current.clone();
    updated.sidebar_collapsed = sidebar_collapsed;
    updated.panel_height = panel_height;
    if let Err(error) = preferences::save(&state.preferences_path, &updated) { return Ok(HostReply::error(error)); }
    *current = updated;
    Ok(HostReply::ok(json!({"saved": true})))
}

#[tauri::command]
pub async fn save_ui_preferences(state: State<'_, AppState>, language: String, theme: String) -> Result<HostReply, String> {
    if !["zh-CN", "en"].contains(&language.as_str()) || !["light", "dark"].contains(&theme.as_str()) {
        return Ok(HostReply::error(HostError::new("INVALID_PARAMS", "unsupported language or theme")));
    }
    let mut current = state.preferences.lock().map_err(|_| "preferences unavailable")?;
    let mut updated = current.clone();
    updated.language = language;
    updated.theme = theme;
    if let Err(error) = preferences::save(&state.preferences_path, &updated) { return Ok(HostReply::error(error)); }
    *current = updated;
    Ok(HostReply::ok(json!({"language": current.language, "theme": current.theme})))
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis() as u64)
        .unwrap_or(0)
}

fn conversation_view_value(
    preferences: &preferences::UiPreferences,
    scope: Option<&str>,
) -> Result<Value, String> {
    serde_json::to_value(preferences::conversation_view(preferences, scope))
        .map_err(|_| "preferences unavailable".to_string())
}

/// Host-only display preferences for the left conversation list.  The WebView
/// gets the current instance slice only; it never supplies or sees the
/// fingerprint and never reaches the sidecar, the store or a database row.
#[tauri::command]
pub async fn conversation_display_preferences(
    state: State<'_, AppState>,
) -> Result<HostReply, String> {
    let preferences = state
        .preferences
        .lock()
        .map_err(|_| "preferences unavailable".to_string())?;
    Ok(HostReply::ok(conversation_view_value(
        &preferences,
        state.instance_scope.as_deref(),
    )?))
}

/// One closed-set write: rename / clear_title / archive / unarchive /
/// restore_all.  It updates only `ui-preferences.json`; a validation or write
/// failure leaves both the file and the in-memory state untouched.
#[tauri::command]
pub async fn set_conversation_display_preference(
    state: State<'_, AppState>,
    action: String,
    conversation_id: Option<String>,
    title: Option<String>,
) -> Result<HostReply, String> {
    let scope = match state.instance_scope.clone() {
        Some(scope) => scope,
        None => {
            return Ok(HostReply::error(HostError::new(
                "HOST_NOT_READY",
                "实例身份尚未建立，无法保存会话显示偏好",
            )))
        }
    };
    let mut current = state
        .preferences
        .lock()
        .map_err(|_| "preferences unavailable".to_string())?;
    let mut updated = current.clone();
    if let Err(error) = preferences::apply_conversation_action(
        &mut updated,
        &scope,
        &action,
        conversation_id.as_deref(),
        title.as_deref(),
        now_ms(),
    ) {
        return Ok(HostReply::error(error));
    }
    if let Err(error) = preferences::save(&state.preferences_path, &updated) {
        return Ok(HostReply::error(error));
    }
    *current = updated;
    Ok(HostReply::ok(conversation_view_value(
        &current,
        Some(&scope),
    )?))
}

#[tauri::command]
pub async fn app_info(state: State<'_, AppState>) -> Result<HostReply, String> {
    match refresh(&state).await {
        Ok(info) => Ok(HostReply::ok(info)),
        Err(reply) => Ok(reply),
    }
}

/// Host-owned bounded log read.  There is deliberately no path, directory,
/// file-name or glob parameter: the WebView can only choose a fixed source id,
/// a fixed level id, a bounded substring and a bounded page size.
#[tauri::command]
pub fn logs_read(
    state: State<'_, AppState>,
    source: Option<String>,
    level: Option<String>,
    query: Option<String>,
    limit: Option<u32>,
) -> Result<HostReply, String> {
    match state
        .logs
        .read(source.as_deref(), level.as_deref(), query.as_deref(), limit)
    {
        Ok(value) => Ok(HostReply::ok(value)),
        Err(error) => Ok(HostReply::error(error)),
    }
}

#[tauri::command]
pub async fn bridge_ping(state: State<'_, AppState>) -> Result<HostReply, String> {
    Ok(call(&state, "bridge.ping", json!({}), Duration::from_secs(20)).await)
}

#[tauri::command]
pub async fn confirm_destination(
    state: State<'_, AppState>,
    session_id: String,
    destination_id: String,
) -> Result<HostReply, String> {
    let _ = match refresh(&state).await {
        Ok(info) => info,
        Err(reply) => return Ok(reply),
    };
    {
        let session = match state.session.lock() {
            Ok(session) => session,
            Err(_) => {
                return Ok(HostReply::error(HostError::new(
                    "HOST_INTERNAL",
                    "会话锁不可用",
                )))
            }
        };
        if let Err(error) = session.validate_confirm(&session_id, &destination_id) {
            return Ok(HostReply::error(error));
        }
    }
    Ok(call(
        &state,
        "app.confirm_destination",
        json!({"session_id": session_id, "destination_id": destination_id}),
        Duration::from_secs(20),
    )
    .await)
}

#[tauri::command]
pub async fn consult_interact(
    state: State<'_, AppState>,
    session_id: String,
    question: String,
    confirmed_destination_id: Option<String>,
) -> Result<HostReply, String> {
    let _ = match refresh(&state).await {
        Ok(info) => info,
        Err(reply) => return Ok(reply),
    };
    {
        let session = match state.session.lock() {
            Ok(session) => session,
            Err(_) => {
                return Ok(HostReply::error(HostError::new(
                    "HOST_INTERNAL",
                    "会话锁不可用",
                )))
            }
        };
        if let Err(error) =
            session.validate_consult(&session_id, confirmed_destination_id.as_deref())
        {
            return Ok(HostReply::error(error));
        }
    }
    Ok(call(
        &state,
        "consult.interact",
        json!({
            "session_id": session_id,
            "question": question,
            "confirmed_destination_id": confirmed_destination_id,
        }),
        Duration::from_secs(120),
    )
    .await)
}

#[tauri::command]
pub async fn conversation_create(
    state: State<'_, AppState>,
    session_id: String,
    mode: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_CONVERSATION_CREATE,
        conversation_create_params(&session_id, &mode),
        CONVERSATION_SEND_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn conversation_list(
    state: State<'_, AppState>,
    session_id: String,
    limit: Option<u32>,
    cursor: Option<String>,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_CONVERSATION_LIST,
        conversation_list_params(&session_id, limit, cursor.as_deref()),
        CONVERSATION_READ_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn conversation_get(
    state: State<'_, AppState>,
    session_id: String,
    conversation_id: String,
    before_turn_no: Option<u32>,
    before_seq: Option<u32>,
    limit: Option<u32>,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_CONVERSATION_GET,
        conversation_get_params(
            &session_id,
            &conversation_id,
            before_turn_no,
            before_seq,
            limit,
        ),
        CONVERSATION_READ_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn conversation_search(
    state: State<'_, AppState>,
    session_id: String,
    query: String,
    limit: Option<u32>,
    conversation_id: Option<String>,
    before: Option<String>,
    include_full: Option<bool>,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_CONVERSATION_SEARCH,
        conversation_search_params(
            &session_id,
            &query,
            limit,
            conversation_id.as_deref(),
            before.as_deref(),
            include_full,
        ),
        CONVERSATION_READ_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn memory_search(
    state: State<'_, AppState>,
    session_id: String,
    query: String,
    limit: Option<u32>,
    cursor: Option<String>,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_MEMORY_SEARCH,
        memory_search_params(&session_id, &query, limit, cursor.as_deref()),
        MEMORY_SEARCH_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn conversation_send(
    state: State<'_, AppState>,
    session_id: String,
    conversation_id: String,
    client_request_id: String,
    text: String,
    confirmed_destination_id: Option<String>,
) -> Result<HostReply, String> {
    let _ = match refresh(&state).await {
        Ok(info) => info,
        Err(reply) => return Ok(reply),
    };
    {
        let session = match state.session.lock() {
            Ok(session) => session,
            Err(_) => return Ok(lock_error()),
        };
        if let Err(error) = session.validate_consult(
            &session_id,
            confirmed_destination_id.as_deref(),
        ) {
            return Ok(HostReply::error(error));
        }
    }
    Ok(call(
        &state,
        METHOD_CONVERSATION_SEND,
        conversation_send_params(
            &session_id,
            &conversation_id,
            &client_request_id,
            &text,
            confirmed_destination_id.as_deref(),
        ),
        CONVERSATION_SEND_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn turn_result(
    state: State<'_, AppState>,
    session_id: String,
    turn_id: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_TURN_RESULT,
        turn_result_params(&session_id, &turn_id),
        CONVERSATION_READ_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn turn_events(
    state: State<'_, AppState>,
    session_id: String,
    turn_id: String,
    after_seq: Option<u32>,
    limit: Option<u32>,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_TURN_EVENTS,
        turn_events_params(&session_id, &turn_id, after_seq, limit),
        CONVERSATION_READ_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn turn_steps(
    state: State<'_, AppState>,
    session_id: String,
    turn_id: String,
    after_step_no: Option<u32>,
    limit: Option<u32>,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_TURN_STEPS,
        turn_steps_params(&session_id, &turn_id, after_step_no, limit),
        CONVERSATION_READ_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn turn_trace(
    state: State<'_, AppState>,
    session_id: String,
    turn_id: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_TURN_TRACE,
        turn_trace_params(&session_id, &turn_id),
        CONVERSATION_READ_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn conversation_usage(
    state: State<'_, AppState>,
    session_id: String,
    conversation_id: String,
    turn_id: Option<String>,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_CONVERSATION_USAGE,
        conversation_usage_params(&session_id, &conversation_id, turn_id.as_deref()),
        CONVERSATION_READ_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn turn_cancel(
    state: State<'_, AppState>,
    session_id: String,
    turn_id: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_TURN_CANCEL,
        turn_cancel_params(&session_id, &turn_id),
        TURN_CANCEL_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn postprocess_status(
    state: State<'_, AppState>,
    session_id: String,
    conversation_id: String,
    turn_ids: Option<Vec<String>>,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_POSTPROCESS_STATUS,
        postprocess_status_params(&session_id, &conversation_id, turn_ids.as_deref()),
        CONVERSATION_READ_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn postprocess_retry(
    state: State<'_, AppState>,
    session_id: String,
    turn_id: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_POSTPROCESS_RETRY,
        postprocess_retry_params(&session_id, &turn_id),
        PROFILE_ACTION_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn maintenance_status(
    state: State<'_, AppState>,
    session_id: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_MAINTENANCE_STATUS,
        json!({"task_id": null}),
        MAINTENANCE_STATUS_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn maintenance_run(
    state: State<'_, AppState>,
    session_id: String,
    task_id: Option<String>,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_MAINTENANCE_RUN,
        maintenance_run_params(&session_id, task_id.as_deref()),
        MAINTENANCE_RUN_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn maintenance_control(
    state: State<'_, AppState>,
    session_id: String,
    task_id: Option<String>,
    action: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_MAINTENANCE_CONTROL,
        maintenance_control_params(&session_id, task_id.as_deref(), &action),
        MAINTENANCE_CONTROL_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn save_maintenance_enabled(
    state: State<'_, AppState>,
    session_id: String,
    enabled: bool,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    let _saving = match begin_settings_save() {
        Some(guard) => guard,
        None => return Ok(HostReply::error(HostError::new("BUSY", "正在保存设置"))),
    };
    if !state.managed_config {
        let mut doc: Value = match std::fs::read_to_string(&state.config_path)
            .ok()
            .and_then(|text| serde_json::from_str(&text).ok())
        {
            Some(doc) => doc,
            None => {
                return Ok(HostReply::error(HostError::new(
                    "SETTINGS_UNAVAILABLE",
                    "无法读取当前宿主配置",
                )))
            }
        };
        let mut block = match doc.get("maintenance").cloned() {
            Some(Value::Object(map)) => Value::Object(map),
            _ => json!({
                "permissions": ["maintenance"],
                "max_events": 1,
                "slice_seconds": 90.0,
                "model_timeout_seconds": 30.0,
            }),
        };
        block["enabled"] = json!(enabled);
        doc["maintenance"] = block;
        if let Err(error) = host_settings::atomic_write(
            &state.config_path,
            &serde_json::to_string_pretty(&doc).unwrap(),
        ) {
            return Ok(HostReply::error(error));
        }
        let applied = call(&state, "settings.reload", json!({}), Duration::from_secs(300)).await;
        return Ok(HostReply::ok(json!({
            "saved": true,
            "application": applied,
            "enabled": enabled,
        })));
    }
    let mut settings = match state.settings.lock() {
        Ok(settings) => settings.clone(),
        Err(_) => return Ok(lock_error()),
    };
    let previous = settings.clone();
    settings.maintenance_enabled = Some(enabled);
    if let Err(error) = host_settings::save(&state.settings_path, &settings) {
        return Ok(HostReply::error(error));
    }
    if let Err(error) = host_settings::ensure_managed_config(&state.config_path, &settings) {
        if host_settings::save(&state.settings_path, &previous).is_err() {
            return Ok(HostReply::error(HostError::unknown(
                "SETTINGS_UNAVAILABLE",
                "保存未完成，持久化状态无法确认",
            )));
        }
        return Ok(HostReply::error(error));
    }
    if let Ok(mut current) = state.settings.lock() {
        *current = settings;
    }
    let applied = call(&state, "settings.reload", json!({}), Duration::from_secs(300)).await;
    Ok(HostReply::ok(json!({
        "saved": true,
        "application": applied,
        "enabled": enabled,
    })))
}

#[tauri::command]
pub async fn cognition_candidates(
    state: State<'_, AppState>,
    session_id: String,
    limit: Option<u32>,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_COGNITION_CANDIDATES,
        cognition_candidates_params(&session_id, limit),
        COGNITION_READ_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn cognition_candidate_decide(
    state: State<'_, AppState>,
    session_id: String,
    candidate_id: String,
    artifact_version: u32,
    decision: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_COGNITION_DECIDE,
        cognition_decide_params(&session_id, &candidate_id, artifact_version, &decision),
        COGNITION_DECIDE_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn profile_status(
    state: State<'_, AppState>,
    session_id: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_PROFILE_STATUS,
        json!({"session_id": session_id}),
        PROFILE_STATUS_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn profile_read(
    state: State<'_, AppState>,
    session_id: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_PROFILE_READ,
        json!({"session_id": session_id}),
        PROFILE_ACTION_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn profile_publish(
    state: State<'_, AppState>,
    session_id: String,
    profile_id: String,
    body: serde_json::Value,
    review_event_id: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_PROFILE_PUBLISH,
        profile_publish_params(&session_id, &profile_id, body, &review_event_id),
        PROFILE_ACTION_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn profile_activate(
    state: State<'_, AppState>,
    session_id: String,
    profile_id: String,
    record_version: u32,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_PROFILE_ACTIVATE,
        profile_activate_params(&session_id, &profile_id, record_version),
        PROFILE_ACTION_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn profile_list(
    state: State<'_, AppState>,
    session_id: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_PROFILE_LIST,
        json!({"session_id": session_id}),
        PROFILE_ACTION_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn profile_read_body(
    state: State<'_, AppState>,
    session_id: String,
    record_id: String,
    record_version: u32,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_PROFILE_READ_BODY,
        profile_read_body_params(&session_id, &record_id, record_version),
        PROFILE_ACTION_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn profile_import_list(
    state: State<'_, AppState>,
    session_id: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_PROFILE_IMPORT_LIST,
        json!({"session_id": session_id}),
        PROFILE_ACTION_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn profile_import_preview(
    state: State<'_, AppState>,
    session_id: String,
    name: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_PROFILE_IMPORT_PREVIEW,
        profile_import_preview_params(&session_id, &name),
        PROFILE_ACTION_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn profile_publish_reviewed(
    state: State<'_, AppState>,
    session_id: String,
    import_id: String,
    profile_id: Option<String>,
    confirm: bool,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_PROFILE_PUBLISH_REVIEWED,
        profile_publish_reviewed_params(
            &session_id,
            &import_id,
            profile_id.as_deref(),
            confirm,
        ),
        PROFILE_ACTION_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn preference_list(
    state: State<'_, AppState>,
    session_id: String,
    conversation_id: Option<String>,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_PREFERENCE_LIST,
        preference_list_params(&session_id, conversation_id.as_deref()),
        PROFILE_ACTION_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn preference_revoke(
    state: State<'_, AppState>,
    session_id: String,
    conversation_id: String,
    dimension: String,
    reason: Option<String>,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        METHOD_PREFERENCE_REVOKE,
        preference_revoke_params(
            &session_id,
            &conversation_id,
            &dimension,
            reason.as_deref(),
        ),
        PROFILE_ACTION_TIMEOUT,
    )
    .await)
}

#[tauri::command]
pub async fn review_work_list(
    state: State<'_, AppState>,
    session_id: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        "review.work.list",
        json!({"session_id": session_id}),
        Duration::from_secs(25),
    )
    .await)
}

#[tauri::command]
pub async fn review_candidates(
    state: State<'_, AppState>,
    session_id: String,
    work_id: String,
    generation: Option<u32>,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        "review.candidates.list",
        json!({
            "session_id": session_id,
            "work_id": work_id,
            "generation": generation,
        }),
        Duration::from_secs(25),
    )
    .await)
}

#[tauri::command]
pub async fn review_candidate_detail(
    state: State<'_, AppState>,
    session_id: String,
    candidate_id: String,
    artifact_version: u32,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        "review.candidate.detail",
        json!({
            "session_id": session_id,
            "candidate_id": candidate_id,
            "artifact_version": artifact_version,
        }),
        Duration::from_secs(25),
    )
    .await)
}

#[tauri::command]
pub async fn review_generate(
    state: State<'_, AppState>,
    session_id: String,
    work_id: String,
    event_id: String,
    confirmed_destination_id: Option<String>,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    {
        let session = match state.session.lock() {
            Ok(session) => session,
            Err(_) => {
                return Ok(HostReply::error(HostError::new(
                    "HOST_INTERNAL",
                    "会话锁不可用",
                )))
            }
        };
        if let Err(error) =
            session.validate_consult(&session_id, confirmed_destination_id.as_deref())
        {
            return Ok(HostReply::error(error));
        }
    }
    Ok(call(
        &state,
        "review.generate",
        json!({
            "session_id": session_id,
            "work_id": work_id,
            "event_id": event_id,
            "confirmed_destination_id": confirmed_destination_id,
        }),
        Duration::from_secs(120),
    )
    .await)
}

#[tauri::command]
pub async fn review_resolve(
    state: State<'_, AppState>,
    session_id: String,
    candidate_id: String,
    artifact_version: u32,
    decision: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        "review.resolve",
        json!({
            "session_id": session_id,
            "candidate_id": candidate_id,
            "artifact_version": artifact_version,
            "decision": decision,
        }),
        Duration::from_secs(60),
    )
    .await)
}

#[tauri::command]
pub async fn review_result(
    state: State<'_, AppState>,
    session_id: String,
    interaction_id: String,
) -> Result<HostReply, String> {
    if let Some(reply) = ready_session(&state, &session_id).await {
        return Ok(reply);
    }
    Ok(call(
        &state,
        "review.result",
        json!({
            "session_id": session_id,
            "interaction_id": interaction_id,
        }),
        Duration::from_secs(30),
    )
    .await)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn host_info(web: Value) -> Value {
        json!({
            "host": {"ready": true, "reason_code": Value::Null},
            "capabilities": {"web": web},
        })
    }

    #[test]
    fn web_status_passes_through_the_host_truth() {
        let info = host_info(json!({
            "enabled": true,
            "configured": false,
            "declared": true,
            "search": true,
            "fetch": true,
            "credential_present": false,
            "credential_env": "DEEPSEEK_API_KEY",
            "allowed_hosts": 2,
            "host_policy": "allowlist",
            "source": "external",
            "missing": ["web.api_key_env"],
        }));
        let status = compose_web_status(&info, false, true, true, true);
        assert_eq!(status["enabled"], true);
        assert_eq!(status["configured"], false);
        assert_eq!(status["credential_present"], false);
        assert_eq!(status["credential_env"], "DEEPSEEK_API_KEY");
        assert_eq!(status["allowed_hosts"], 2);
        assert_eq!(status["host_policy"], "allowlist");
        assert_eq!(status["source"], "external");
        assert_eq!(status["missing"][0], "web.api_key_env");
        assert_eq!(status["editable"], true);
        assert_eq!(status["managed"], false);
        assert_eq!(status["apply"], "next_turn");
        assert_eq!(status["host_ready"], true);
        assert_eq!(status["credential_restart_required"], true);
    }

    #[test]
    fn web_status_without_a_host_capability_invents_nothing() {
        let status = compose_web_status(&json!({}), true, true, true, false);
        assert_eq!(status["enabled"], false);
        assert_eq!(status["configured"], false);
        assert_eq!(status["declared"], false);
        assert_eq!(status["source"], Value::Null);
        assert_eq!(status["missing"][0], "host");
        // No source means nothing is known about the config; do not write it.
        assert_eq!(status["editable"], false);
        assert_eq!(status["host_ready"], false);
    }

    #[test]
    fn web_status_respects_writability_and_a_known_source() {
        let external = host_info(json!({
            "enabled": false, "configured": false, "declared": true,
            "search": true, "fetch": true, "credential_present": false,
            "credential_env": "DEEPSEEK_API_KEY", "allowed_hosts": 0,
            "host_policy": "any_public_https", "source": "external",
            "missing": ["web.enabled"],
        }));
        assert_eq!(compose_web_status(&external, false, false, true, false)["editable"], false);
        assert_eq!(compose_web_status(&external, false, true, true, false)["editable"], true);
        // A managed install owns its generated config even if the file's
        // readonly bit is set; the writer reports any real failure.
        assert_eq!(compose_web_status(&external, true, false, true, false)["editable"], true);
        // No sidecar answer means no reliable source; do not flip a switch.
        assert_eq!(compose_web_status(&external, true, true, false, false)["editable"], false);

        let unknown_source = host_info(json!({"enabled": false, "source": Value::Null}));
        assert_eq!(compose_web_status(&unknown_source, true, true, true, false)["editable"], false);
    }
}


#[tauri::command]
pub async fn instance_status(state: State<'_, AppState>) -> Result<HostReply, String> {
    let mut status = crate::instance::status(&state.config_path);
    let info = call(&state, "app.info", json!({}), Duration::from_secs(10)).await;
    status["connected"] = json!(info.result.as_ref().and_then(|v| v.pointer("/host/ready")).and_then(Value::as_bool).unwrap_or(false));
    status["reason"] = info.error.map(|e| e.message).map(Value::String).unwrap_or_else(|| info.result.as_ref().and_then(|v| v.pointer("/host/reason_detail")).cloned().unwrap_or(Value::Null));
    Ok(HostReply::ok(status))
}

#[tauri::command]
pub async fn choose_instance_folder() -> Result<HostReply, String> {
    let selected = tauri::async_runtime::spawn_blocking(crate::instance::pick_folder).await;
    Ok(match selected { Ok(Ok(root)) => HostReply::ok(json!({"root":root})),
        Ok(Err(error)) => HostReply::error(error), Err(_) => HostReply::error(HostError::new("FOLDER_DIALOG_FAILED", "无法打开文件夹选择窗口")) })
}

#[derive(Clone, serde::Deserialize)]
pub struct LegacyInstanceAdoption { pub by: String, pub reason: String, pub confirm: bool }

#[derive(Clone, serde::Deserialize)]
pub struct ComponentDeclaration { pub by: String, pub reason: String, pub components: Vec<String>, pub confirm: bool }

#[tauri::command]
pub async fn connect_instance(app: tauri::AppHandle, state: State<'_, AppState>, root: String, legacy_adoption: Option<LegacyInstanceAdoption>, component_declaration: Option<ComponentDeclaration>) -> Result<HostReply, String> {
    let _saving = match begin_settings_save() { Some(guard) => guard, None => return Ok(HostReply::error(HostError::new("BUSY", "正在保存设置"))) };
    if INSTANCE_SWITCHING.swap(true, std::sync::atomic::Ordering::AcqRel) { return Ok(HostReply::error(HostError::new("BUSY", "正在连接实例"))); }
    let _switching = InstanceSwitchGuard;
    if let Err(error) = crate::paths::validate_instance_root(Path::new(&root)) { return Ok(HostReply::error(error)); }
    if let Some(request) = &legacy_adoption {
        if !request.confirm || request.by.trim().is_empty() || request.reason.trim().is_empty() || request.by.len() > 512 || request.reason.len() > 512 {
            return Ok(HostReply::error(HostError::new("INSTANCE_ADOPTION_INVALID", "旧实例接入必须明确确认历史限制，并填写操作者和原因")));
        }
    }
    if let Some(request) = &component_declaration {
        if !request.confirm || request.by.trim().is_empty() || request.reason.trim().is_empty() || request.by.len() > 512 || request.reason.len() > 512
            || request.components.len() != 1 || !matches!(request.components[0].as_str(), "pcs" | "memory") {
            return Ok(HostReply::error(HostError::new("INSTANCE_DECLARATION_INVALID", "必须明确确认组件从未建立，并填写操作者和原因")));
        }
    }
    let current = crate::instance::status(&state.config_path);
    let same_folder = current["portable"] == true && current["root"].as_str().map(|old| crate::paths::comparable(Path::new(old)) == crate::paths::comparable(Path::new(&root))).unwrap_or(false);
    if same_folder && (legacy_adoption.is_some() || component_declaration.is_some()) {
        return Ok(HostReply::error(HostError::new("INSTANCE_ADOPTION_REFUSED", "不能为已登记的当前实例重复创建安全基线")));
    }
    if same_folder {
        let info = call(&state, "app.info", json!({}), Duration::from_secs(10)).await;
        if info.result.as_ref().and_then(|v| v.pointer("/host/ready")).and_then(Value::as_bool) == Some(true) {
            return Ok(HostReply::ok(json!({"connected":true,"restart_required":false,"instance":current})));
        }
    }
    let manager = state.manager.lock().ok().and_then(|m| m.clone());
    if manager.is_some() {
        let ping = call(&state, "bridge.ping", json!({}), Duration::from_secs(10)).await;
        if ping.result.as_ref().and_then(|v| v.get("busy")).and_then(Value::as_bool) == Some(true) {
            return Ok(HostReply::error(HostError::new("BUSY", "请等当前回答或后台任务结束后再切换实例")));
        }
    }
    if same_folder { if let Some(existing) = &manager { existing.shutdown(); } }
    let paths = match &state.sidecar_paths { Some(paths) => paths.clone(), None => return Ok(HostReply::error(HostError::new("SIDECAR_NOT_FOUND", "运行组件不完整，无法准备实例"))) };
    let mut settings = match state.settings.lock() { Ok(s) => s.clone(), Err(_) => return Ok(lock_error()) };
    let prepare_settings = settings.clone();
    let selected_root = root.clone();
    let prepared = tauri::async_runtime::spawn_blocking(move || crate::instance::prepare_folder_with_adoption(&paths, Path::new(&selected_root), &prepare_settings,
        legacy_adoption.as_ref().map(|request| (request.by.as_str(), request.reason.as_str())),
        component_declaration.as_ref().map(|request| (request.by.as_str(), request.reason.as_str(), request.components.as_slice())))).await;
    let result = match prepared { Ok(Ok(result)) => result, Ok(Err(error)) => return Ok(HostReply::error(error)), Err(_) => return Ok(HostReply::error(HostError::new("INSTANCE_PREPARE_FAILED", "实例准备未完成"))) };
    settings.selected_instance = Some(root.clone());
    settings.instance_root = Some(root.clone());
    settings.request_budget_units.get_or_insert(2_000_000);
    settings.output_reserve_units.get_or_insert(4096);
    if let Err(error) = host_settings::save(&state.settings_path, &settings) { return Ok(HostReply::error(error)); }
    if let Ok(mut existing) = state.settings.lock() { *existing = settings; }
    if let Some(manager) = manager { manager.shutdown(); }
    std::env::set_var(crate::instance::SELECTED_ENV, &root);
    std::env::set_var("QUINTESSENCE_RESTART_PARENT", std::process::id().to_string());
    std::thread::spawn(move || { std::thread::sleep(Duration::from_millis(500)); app.request_restart(); });
    Ok(HostReply::ok(json!({"connected":true,"restart_required":true,"instance":result})))
}
