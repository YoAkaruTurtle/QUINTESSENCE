//! QUINTESSENCE desktop Consult shell.
//!
//! Trusted-host boundary: Rust is the single application process, resolves and
//! spawns the installed one-folder Python sidecar, owns its lifecycle and
//! framed stdio, re-reads `app.info` before each action and exposes only
//! `app.info`, `app.confirm_destination`, `consult.interact`, the six
//! `review.*` reads/decisions and `bridge.ping` to the WebView.  The WebView has
//! no shell, filesystem, HTTP or process capability.

mod commands;
mod key_store;
mod host_settings;
mod paths;
mod preferences;
mod session;
pub mod sidecar;
mod single_instance;

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use tauri::{Manager, RunEvent};

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
            if managed_config {
                if let Err(error) = host_settings::ensure_managed_config(&config_path, &settings) {
                    // Keep the app open: Settings can still report the failure.
                    let paths = paths::resolve(app.handle()).ok();
                    app.manage(AppState {
                        manager: Mutex::new(None),
                        startup_error: Some(error),
                        session: Mutex::new(HostSession::default()),
                        preferences: Mutex::new(preferences::load(&preferences::path_for(app.handle()))),
                        preferences_path: preferences::path_for(app.handle()),
                        config_path,
                        sidecar_paths: paths,
                        started,
                        managed_config,
                        settings_path,
                        settings: Mutex::new(settings),
                        api_key_env: host_settings::KEY_ENV.to_string(),
                    });
                    return Ok(());
                }
                let _ = host_settings::save(&settings_path, &settings);
            }
            let api_key_env = host_settings::api_key_env(&config_path);
            let api_key = key_store::read().ok().flatten();
            let preferences_path = preferences::path_for(app.handle());
            let preferences = preferences::load(&preferences_path);
            let resolved = paths::resolve(app.handle());
            let (manager, startup_error, sidecar_paths) = match resolved {
                Ok(paths) => match SidecarManager::spawn_with_env(
                    &paths,
                    &config_path,
                    Some(api_key_env.as_str()),
                    api_key.as_deref(),
                ) {
                    Ok(manager) => (Some(Arc::new(manager)), None, Some(paths)),
                    Err(error) => (None, Some(error), Some(paths)),
                },
                Err(error) => (None, Some(error), None),
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
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::app_info,
            commands::bridge_ping,
            commands::confirm_destination,
            commands::consult_interact,
            commands::review_work_list,
            commands::review_candidates,
            commands::review_candidate_detail,
            commands::review_generate,
            commands::review_resolve,
            commands::review_result,
            commands::set_ai_display_name,
            commands::model_status,
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
            let manager = state.manager.lock().ok().and_then(|guard| guard.clone());
            if let Some(manager) = manager {
                manager.shutdown();
            }
        }
    });
}
