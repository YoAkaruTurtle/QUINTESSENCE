//! Floating status for the selected folder. It observes the host over the
//! existing sidecar channel and owns only its own window controls.

use serde::Serialize;
use serde_json::{json, Value};
use tauri::{AppHandle, Manager, PhysicalPosition, State, Url, WebviewUrl, WebviewWindow};

use crate::sidecar::{HostError, HostReply};
use crate::AppState;

pub(crate) const LABEL: &str = "dreaming-orb";
const PAGE: &str = "dreaming-orb.html";
const WIDTH: f64 = 188.0;
const HEIGHT: f64 = 66.0;
const SCREEN_MARGIN: f64 = 24.0;

pub(crate) fn allow_invoke(label: &str, command: &str) -> bool {
    let orb_only = matches!(command, "dreaming_orb_drag" | "dreaming_orb_hide");
    match label {
        "main" => !orb_only,
        LABEL => orb_only || command == "dreaming_status",
        _ => false,
    }
}

pub(crate) fn destroy(app: &AppHandle) {
    if let Some(orb) = app.get_webview_window(LABEL) {
        let _ = orb.destroy();
    }
}

fn available_on_host(state: &AppState) -> bool {
    cfg!(windows)
        && state.managed_config
        && state.instance_scope.is_some()
        && crate::instance::is_folder_config(&state.config_path)
}

#[derive(Serialize)]
struct DreamingStatus {
    state: &'static str,
    reason: Option<&'static str>,
    semantic_reason: Option<&'static str>,
    enabled: bool,
    can_configure: bool,
    journals_enabled: bool,
    can_configure_journals: bool,
    can_show_orb: bool,
    language: &'static str,
    theme: &'static str,
}

impl DreamingStatus {
    fn new(language: &'static str, theme: &'static str) -> Self {
        Self {
            state: "disconnected",
            reason: None,
            semantic_reason: None,
            enabled: false,
            can_configure: false,
            journals_enabled: false,
            can_configure_journals: false,
            can_show_orb: false,
            language,
            theme,
        }
    }

    fn from_info(
        info: &Value,
        host_allows_orb: bool,
        language: &'static str,
        theme: &'static str,
    ) -> Self {
        let mut status = Self::new(language, theme);
        let Some(dreaming) = info.get("dreaming").and_then(Value::as_object) else {
            return status;
        };
        let connected = info
            .get("methods")
            .and_then(Value::as_array)
            .is_some_and(|methods| methods.iter().any(|method| method == "dreaming.configure"));
        if !connected {
            return status;
        }
        status.state = match dreaming.get("state").and_then(Value::as_str) {
            Some("idle" | "waiting") => "waiting",
            Some("shallow") => "shallow",
            Some("deep") => "deep",
            Some("thinking") => "thinking",
            Some("foreground" | "yielded") => "foreground",
            Some("disabled") => "disabled",
            Some("paused") => "paused",
            Some("failed") => "failed",
            Some("discarded") => "discarded",
            _ => "unavailable",
        };
        status.reason = match dreaming.get("reason").and_then(Value::as_str) {
            Some("maintenance_pending") => Some("maintenance_pending"),
            Some("wake_budget") => Some("wake_budget"),
            Some("transport_draining") => Some("transport_draining"),
            Some("model_not_authorized") => Some("model_not_authorized"),
            Some("purpose_not_allowed") => Some("purpose_not_allowed"),
            Some("calls_exhausted") => Some("calls_exhausted"),
            Some("key_missing" | "MODEL_KEY_MISSING") => Some("model_key_missing"),
            _ => None,
        };
        status.semantic_reason = match dreaming
            .get("semantic_reason")
            .and_then(Value::as_str)
            .or_else(|| dreaming.get("reason").and_then(Value::as_str))
        {
            Some("embedding_unavailable") => Some("embedding_unavailable"),
            Some("scope_recall_unavailable") => Some("scope_recall_unavailable"),
            Some("vector_unavailable") => Some("vector_unavailable"),
            _ => None,
        };
        status.enabled = dreaming.get("enabled").and_then(Value::as_bool) == Some(true);
        status.can_configure = dreaming.get("can_configure").and_then(Value::as_bool) == Some(true);
        status.journals_enabled =
            dreaming.get("journals_enabled").and_then(Value::as_bool) == Some(true);
        status.can_configure_journals =
            dreaming.get("can_configure_journals").and_then(Value::as_bool) == Some(true);
        status.can_show_orb =
            host_allows_orb && info.pointer("/host/ready").and_then(Value::as_bool) == Some(true);
        status
    }
}

#[tauri::command]
pub async fn dreaming_status(state: State<'_, AppState>) -> Result<HostReply, String> {
    let (language, theme) = state
        .preferences
        .lock()
        .map(|preferences| {
            (
                if preferences.language == "en" {
                    "en"
                } else {
                    "zh-CN"
                },
                if preferences.theme == "light" {
                    "light"
                } else {
                    "dark"
                },
            )
        })
        .unwrap_or(("zh-CN", "dark"));
    let status = crate::commands::observe_dreaming_info(&state)
        .await
        .map_or_else(
            || DreamingStatus::new(language, theme),
            |info| DreamingStatus::from_info(&info, available_on_host(&state), language, theme),
        );
    Ok(HostReply::ok(json!(status)))
}

#[tauri::command]
pub async fn open_dreaming_orb(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<HostReply, String> {
    let available = available_on_host(&state)
        && crate::commands::observe_dreaming_info(&state)
            .await
            .is_some_and(|info| {
                DreamingStatus::from_info(&info, true, "zh-CN", "dark").can_show_orb
            });
    if !available {
        return Ok(HostReply::error(HostError::new(
            "DREAMING_ORB_UNAVAILABLE",
            "当前实例的悬浮球不可用",
        )));
    }

    Ok(match show(&app) {
        Ok(()) => HostReply::ok(json!({ "visible": true })),
        Err(_) => HostReply::error(HostError::new("DREAMING_ORB_UNAVAILABLE", "无法显示悬浮球")),
    })
}

fn page_allowed(url: &Url, dev_page: Option<&Url>) -> bool {
    if dev_page == Some(url) {
        return true;
    }
    let is_local_protocol = match (url.scheme(), url.host_str()) {
        ("tauri", Some("localhost")) | ("http" | "https", Some("tauri.localhost")) => true,
        _ => false,
    };
    is_local_protocol
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
        && url.path() == format!("/{PAGE}")
        && url.query().is_none()
        && url.fragment().is_none()
}

fn show(app: &AppHandle) -> tauri::Result<()> {
    if let Some(orb) = app.get_webview_window(LABEL) {
        return orb.show();
    }
    let main = app
        .get_webview_window("main")
        .ok_or(tauri::Error::WindowNotFound)?;
    let monitor = main.current_monitor()?.or(app.primary_monitor()?);
    let dev_page = if tauri::is_dev() {
        app.config()
            .build
            .dev_url
            .as_ref()
            .and_then(|url| url.join(PAGE).ok())
    } else {
        None
    };
    let orb = tauri::WebviewWindowBuilder::new(app, LABEL, WebviewUrl::App(PAGE.into()))
        .title("Dreaming")
        .inner_size(WIDTH, HEIGHT)
        .decorations(false)
        .resizable(false)
        .maximizable(false)
        .minimizable(false)
        .skip_taskbar(true)
        .always_on_top(true)
        .transparent(true)
        .shadow(false)
        .focused(false)
        .visible(false)
        .incognito(true)
        .devtools(false)
        .zoom_hotkeys_enabled(false)
        .on_navigation(move |url| page_allowed(url, dev_page.as_ref()))
        .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
        .build()?;
    if let Some(monitor) = monitor {
        let scale = monitor.scale_factor();
        let area = monitor.work_area();
        let margin = (SCREEN_MARGIN * scale).round() as i32;
        let width = (WIDTH * scale).round() as i32;
        let height = (HEIGHT * scale).round() as i32;
        let x = (area.size.width as i32 - width - margin).max(0);
        let y = (area.size.height as i32 - height - margin).max(0);
        if let Err(error) = orb.set_position(PhysicalPosition::new(
            area.position.x + x,
            area.position.y + y,
        )) {
            let _ = orb.destroy();
            return Err(error);
        }
    }
    // An instance switch or a main-window close can race the async window build.
    // Both shutdown paths also destroy an already registered orb.
    if crate::commands::instance_switching() || app.get_webview_window("main").is_none() {
        let _ = orb.destroy();
        return Err(tauri::Error::WindowNotFound);
    }
    if let Err(error) = orb.show() {
        let _ = orb.destroy();
        return Err(error);
    }
    Ok(())
}

#[tauri::command]
pub fn dreaming_orb_drag(window: WebviewWindow) -> Result<(), &'static str> {
    window
        .start_dragging()
        .map_err(|_| "Could not move the Dreaming orb")
}

#[tauri::command]
pub fn dreaming_orb_hide(window: WebviewWindow) -> Result<(), &'static str> {
    window
        .destroy()
        .map_err(|_| "Could not hide the Dreaming orb")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn orb_ipc_only_reads_status_and_controls_itself() {
        assert!(allow_invoke(LABEL, "dreaming_status"));
        assert!(allow_invoke(LABEL, "dreaming_orb_drag"));
        assert!(allow_invoke(LABEL, "dreaming_orb_hide"));
        for command in [
            "save_dreaming_enabled",
            "open_dreaming_orb",
            "app_info",
            "conversation_send",
            "connect_instance",
        ] {
            assert!(!allow_invoke(LABEL, command));
        }
        assert!(allow_invoke("main", "save_dreaming_enabled"));
        assert!(!allow_invoke("main", "dreaming_orb_drag"));
        assert!(!allow_invoke("other", "dreaming_status"));
    }

    #[test]
    fn status_projects_only_known_values_and_drops_lost_instances() {
        let mut info = json!({
            "host": {"ready": true, "namespace": "synthetic.hidden"},
            "methods": ["dreaming.configure"],
            "dreaming": {
                "state": "deep", "reason": "arbitrary private text",
                "semantic_reason": "vector_unavailable", "enabled": true,
                "can_configure": true, "body": "private body",
            },
        });
        let status = json!(DreamingStatus::from_info(&info, true, "en", "light"));
        assert_eq!(
            status,
            json!({
                "state": "deep", "reason": null, "semantic_reason": "vector_unavailable",
                "enabled": true, "can_configure": true,
                "journals_enabled": false, "can_configure_journals": false,
                "can_show_orb": true, "language": "en", "theme": "light",
            })
        );
        info["host"]["ready"] = json!(false);
        info["dreaming"]["state"] = json!("failed");
        let failed = DreamingStatus::from_info(&info, true, "en", "light");
        assert_eq!(failed.state, "failed");
        assert!(!failed.can_show_orb);
        info["methods"] = json!([]);
        let disconnected = DreamingStatus::from_info(&info, true, "en", "light");
        assert_eq!(disconnected.state, "disconnected");
        assert!(!disconnected.enabled);
        assert!(!disconnected.can_configure);
    }

    #[test]
    fn orb_navigation_remains_on_its_packaged_or_dev_entry() {
        let dev = Url::parse("http://localhost:1420/dreaming-orb.html").unwrap();
        assert!(page_allowed(&dev, Some(&dev)));
        for url in [
            "tauri://localhost/dreaming-orb.html",
            "http://tauri.localhost/dreaming-orb.html",
        ] {
            assert!(page_allowed(&Url::parse(url).unwrap(), None));
        }
        for url in [
            "http://tauri.localhost/index.html",
            "http://tauri.localhost:8443/dreaming-orb.html",
            "http://localhost:1421/dreaming-orb.html",
            "https://example.test/dreaming-orb.html",
        ] {
            assert!(!page_allowed(&Url::parse(url).unwrap(), Some(&dev)));
        }
    }
}
