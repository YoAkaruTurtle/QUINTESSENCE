//! QUINTESSENCE desktop Consult shell.
//!
//! Trusted-host boundary: Rust is the single application process, resolves and
//! spawns the installed one-folder Python sidecar, owns its lifecycle and
//! framed stdio, re-reads `app.info` before each action and exposes only
//! `app.info`, `app.confirm_destination`, `consult.interact`, the RG02
//! `conversation.*`/`turn.result` methods, the six `review.*` reads/decisions,
//! the host-only display-only conversation preference commands and
//! `bridge.ping` to the WebView.  The WebView has no shell, filesystem,
//! HTTP or process capability.

pub mod commands;
mod key_store;
mod host_settings;
mod instance;
pub mod logs;
mod paths;
mod preferences;
mod session;
pub mod sidecar;
mod single_instance;

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use tauri::{Manager, RunEvent};

use logs::Level;
use session::HostSession;
use sidecar::{HostError, SidecarManager, SidecarPaths};

pub const WINDOW_TITLE: &str = "QUINTESSENCE · Consult";

pub struct AppState {
    pub manager: Mutex<Option<Arc<SidecarManager>>>,
    pub startup_error: Option<HostError>,
    pub session: Mutex<HostSession>,
    pub preferences: Mutex<preferences::UiPreferences>,
    pub preferences_path: PathBuf,
    pub config_path: PathBuf,
    pub sidecar_paths: Option<SidecarPaths>,
    pub started: Instant,
    pub managed_config: bool,
    pub settings_path: PathBuf,
    pub settings: Mutex<host_settings::HostSettings>,
    pub api_key_env: String,
    pub logs: Arc<logs::LogStore>,
    pub host_ready: Mutex<Option<bool>>,
    /// Path-free F03 instance fingerprint; `None` when no authority could be
    /// proven.  Display-only conversation preferences are scoped by it and are
    /// refused (never written unscoped) without one.
    pub instance_scope: Option<String>,
}

/// Production always uses the fixed application log **base** directory.  Only a
/// debug build honours `QUINTESSENCE_DESKTOP_LOG_DIR` (absolute, existing, no
/// link escape) so Lab evidence never lands in the real app data directory.
/// `scope_dir` then selects `instances/<fingerprint>` or `boot/`.
fn resolve_log_base(app: &tauri::AppHandle, config: &std::path::Path) -> (PathBuf, bool) {
    if instance::is_folder_config(config) { return (config.parent().unwrap().join("logs"), false); }
    if let Some(selected) = std::env::var_os("QUINTESSENCE_DESKTOP_STATE_DIR").map(PathBuf::from) {
        if paths::validate_instance_root(&selected).is_ok() { return (selected.join("logs"), false); }
    }
    let default = app
        .path()
        .app_log_dir()
        .unwrap_or_else(|_| std::env::temp_dir().join("quintessence-desktop").join("logs"));
    logs::resolve_base(default)
}

/// The host authority is the instance identity it can prove from its own
/// config: namespace + host_instance + absolute instance root.  A managed
/// config derives it from the host settings; an external config is read once at
/// startup.  Without one, logs go to the isolated `boot/` scope.
fn resolve_identity(
    config_path: &std::path::Path,
    managed_config: bool,
    settings: &host_settings::HostSettings,
) -> Option<logs::InstanceIdentity> {
    if instance::is_folder_config(config_path) {
        return logs::InstanceIdentity::from_config_path(config_path);
    }
    if managed_config {
        let root = settings.instance_root.as_deref()?;
        logs::InstanceIdentity::new(
            host_settings::MANAGED_NAMESPACE,
            host_settings::MANAGED_HOST_INSTANCE,
            std::path::Path::new(root),
        )
    } else {
        logs::InstanceIdentity::from_config_path(config_path)
    }
}

#[derive(Clone, Default)]
struct CliArgs {
    config: Option<PathBuf>,
}

impl CliArgs {
    fn parse() -> Self {
        let mut parsed = Self::default();
        let mut arguments = std::env::args().skip(1);
        while let Some(argument) = arguments.next() {
            if let Some(value) = argument.strip_prefix("--config=") {
                parsed.config = Some(PathBuf::from(value));
            } else if argument == "--config" {
                if let Some(value) = arguments.next() {
                    parsed.config = Some(PathBuf::from(value));
                }
            }
        }
        parsed
    }
}

pub fn run() {
    instance::wait_for_restart_parent();
    // A second process never reaches setup, so it can neither spawn a second
    // sidecar nor write the same data instance.
    let _instance = match single_instance::acquire_or_focus(WINDOW_TITLE) {
        Some(guard) => guard,
        None => return,
    };

    let args = CliArgs::parse();
    let started = Instant::now();

    let builder = tauri::Builder::default()
        .setup(move |app| {
            let (config_path, managed_config) =
                host_settings::resolve_config_path(app.handle(), args.config.clone());
            let settings_path = host_settings::settings_path(app.handle());
            let settings = host_settings::load(&settings_path);
            // Revalidate the folder before the runtime opens any component.
            let folder_error = if instance::is_folder_config(&config_path) {
                paths::resolve(app.handle()).and_then(|paths| instance::prepare_folder(&paths, config_path.parent().unwrap(), &settings)).err()
            } else { None };
            let identity = resolve_identity(&config_path, managed_config, &settings);
            let instance_scope = instance::scope(&config_path).or_else(|| identity.as_ref().map(|identity| identity.fingerprint()));
            let (log_base, log_dir_rejected) = resolve_log_base(app.handle(), &config_path);
            let (log_dir, log_scope) = logs::scope_dir(&log_base, identity.as_ref());
            let log_store = logs::LogStore::open_in(log_dir, log_scope);
            if log_dir_rejected {
                log_store.record(
                    Level::Warn,
                    Some("host.log_dir_rejected"),
                    "reason=invalid_override",
                );
            }
            log_store.record_lifecycle(
                Level::Info,
                "host.start",
                &format!(
                    "version={} managed={managed_config} scope={}",
                    env!("CARGO_PKG_VERSION"),
                    log_scope.as_str()
                ),
            );
            if managed_config || folder_error.is_some() {
                let setup = if let Some(error) = folder_error { Err(error) }
                    else if instance::is_folder_config(&config_path) { Ok(()) }
                    else { host_settings::ensure_managed_config(&config_path, &settings) };
                if let Err(error) = setup {
                    // Keep the app open: Settings can still report the failure.
                    log_store.record_lifecycle(
                        Level::Error,
                        "host.start_failed",
                        &format!("code={}", error.code),
                    );
                    let paths = paths::resolve(app.handle()).ok();
                    app.manage(AppState {
                        manager: Mutex::new(None),
                        startup_error: Some(error),
                        session: Mutex::new(HostSession::default()),
                        preferences: Mutex::new(preferences::load(&instance::preferences_path(&config_path, preferences::path_for(app.handle())))),
                        preferences_path: instance::preferences_path(&config_path, preferences::path_for(app.handle())),
                        config_path: config_path.clone(),
                        sidecar_paths: paths,
                        started,
                        managed_config,
                        settings_path,
                        settings: Mutex::new(settings),
                        api_key_env: host_settings::KEY_ENV.to_string(),
                        logs: Arc::clone(&log_store),
                        host_ready: Mutex::new(Some(false)),
                        instance_scope: instance_scope.clone(),
                    });
                    return Ok(());
                }
                let _ = host_settings::save(&settings_path, &settings);
            }
            let api_key_env = host_settings::api_key_env(&config_path);
            let api_key = std::env::var(&api_key_env).ok().filter(|value| !value.is_empty())
                .or_else(|| key_store::read().ok().flatten());
            let preferences_path = instance::preferences_path(&config_path, preferences::path_for(app.handle()));
            let preferences = preferences::load(&preferences_path);
            let resolved = paths::resolve(app.handle());
            let (manager, startup_error, sidecar_paths) = match resolved {
                Ok(paths) => match SidecarManager::spawn_with_env(
                    &paths,
                    &config_path,
                    Some(api_key_env.as_str()),
                    api_key.as_deref(),
                    Some(Arc::clone(&log_store)),
                ) {
                    Ok(manager) => {
                        log_store.record_lifecycle(
                            Level::Info,
                            "sidecar.spawned",
                            &format!("dev_fallback={}", paths.dev_fallback),
                        );
                        (Some(Arc::new(manager)), None, Some(paths))
                    }
                    Err(error) => {
                        log_store.record_lifecycle(
                            Level::Error,
                            "sidecar.spawn_failed",
                            &format!("code={}", error.code),
                        );
                        (None, Some(error), Some(paths))
                    }
                },
                Err(error) => {
                    log_store.record_lifecycle(
                        Level::Error,
                        "sidecar.spawn_failed",
                        &format!("code={}", error.code),
                    );
                    (None, Some(error), None)
                }
            };

            app.manage(AppState {
                manager: Mutex::new(manager),
                startup_error,
                session: Mutex::new(HostSession::default()),
                preferences: Mutex::new(preferences),
                preferences_path,
                config_path,
                sidecar_paths,
                started,
                managed_config,
                settings_path,
                settings: Mutex::new(settings),
                api_key_env,
                logs: Arc::clone(&log_store),
                host_ready: Mutex::new(None),
                instance_scope,
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::app_info,
            commands::instance_status,
            commands::choose_instance_folder,
            commands::connect_instance,
            commands::bridge_ping,
            commands::confirm_destination,
            commands::consult_interact,
            commands::conversation_create,
            commands::conversation_list,
            commands::conversation_get,
            commands::conversation_search,
            commands::memory_search,
            commands::conversation_send,
            commands::turn_result,
            commands::turn_events,
            commands::turn_steps,
            commands::turn_trace,
            commands::conversation_usage,
            commands::turn_cancel,
            commands::postprocess_status,
            commands::postprocess_retry,
            commands::maintenance_status,
            commands::maintenance_run,
            commands::maintenance_control,
            commands::save_maintenance_enabled,
            commands::cognition_candidates,
            commands::cognition_candidate_decide,
            commands::profile_status,
            commands::profile_read,
            commands::profile_publish,
            commands::profile_activate,
            commands::profile_list,
            commands::profile_read_body,
            commands::profile_import_list,
            commands::profile_import_preview,
            commands::profile_publish_reviewed,
            commands::preference_list,
            commands::preference_revoke,
            commands::review_work_list,
            commands::review_candidates,
            commands::review_candidate_detail,
            commands::review_generate,
            commands::review_resolve,
            commands::review_result,
            commands::set_ai_display_name,
            commands::logs_read,
            commands::ui_preferences,
            commands::save_ui_preferences,
            commands::save_layout_preferences,
            commands::conversation_display_preferences,
            commands::set_conversation_display_preference,
            commands::model_status,
            commands::generation_settings,
            commands::save_generation_settings,
            commands::web_settings,
            commands::save_web_settings,
            commands::memory_recall_settings,
            commands::save_memory_recall_settings,
            commands::save_model_api_key,
            commands::delete_model_api_key,
            commands::set_model_choice,
            commands::test_model_connection
        ]);

    let application = builder
        .build(tauri::generate_context!())
        .expect("failed to build the Tauri application");

    application.run(|handle, event| {
        if let RunEvent::Exit = event {
            let state = handle.state::<AppState>();
            state
                .logs
                .record_lifecycle(Level::Info, "host.stop", "shutdown");
            let manager = state.manager.lock().ok().and_then(|guard| guard.clone());
            if let Some(manager) = manager {
                manager.shutdown();
            }
        }
    });
}
