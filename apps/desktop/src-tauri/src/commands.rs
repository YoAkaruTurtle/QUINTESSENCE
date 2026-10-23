//! Typed Tauri commands.  The WebView only gets these five operations.

use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Value};
use tauri::State;

use crate::preferences;
use crate::sidecar::{HostError, HostReply, SidecarManager};
use crate::AppState;

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
    let manager = match manager(state) {
        Ok(manager) => manager,
        Err(reply) => return reply,
    };
    match tauri::async_runtime::spawn_blocking(move || {
        manager.request_with_timeout(method, params, timeout)
    })
    .await
    {
        Ok(reply) => reply,
        Err(_) => HostReply::error(HostError::new("HOST_INTERNAL", "sidecar 调用线程失败")),
    }
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
    preferences.ai_display_name = validated;
    if let Err(error) = preferences::save(&state.preferences_path, &preferences) {
        return Ok(HostReply::error(error));
    }
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
pub async fn app_info(state: State<'_, AppState>) -> Result<HostReply, String> {
    match refresh(&state).await {
        Ok(info) => Ok(HostReply::ok(info)),
        Err(reply) => Ok(reply),
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
