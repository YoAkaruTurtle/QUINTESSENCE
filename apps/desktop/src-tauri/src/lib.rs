//! QUINTESSENCE desktop Consult shell.
//!
//! Trusted-host boundary: Rust is the single application process, resolves and
//! spawns the installed one-folder Python sidecar, owns its lifecycle and
//! framed stdio, re-reads `app.info` before each action and exposes only
//! `app.info`, `app.confirm_destination`, `consult.interact` and
//! `bridge.ping` to the WebView.  The WebView has no shell, filesystem, HTTP or
//! process capability.

mod commands;
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

fn resolve_config_path(app: &tauri::AppHandle, requested: Option<PathBuf>) -> PathBuf {
    if let Some(path) = requested {
        return path;
    }
    if let Some(value) = std::env::var_os("QUINTESSENCE_DESKTOP_CONFIG") {
        if !value.is_empty() {
            return PathBuf::from(value);
        }
    }
    let base = app
        .path()
        .app_local_data_dir()
        .unwrap_or_else(|_| std::env::temp_dir().join("quintessence-desktop"));
    let _ = std::fs::create_dir_all(&base);
    base.join("desktop-host.json")
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
            let config_path = resolve_config_path(app.handle(), args.config.clone());
            let preferences_path = preferences::path_for(app.handle());
            let preferences = preferences::load(&preferences_path);
            let resolved = paths::resolve(app.handle());
            let (manager, startup_error, sidecar_paths) = match resolved {
                Ok(paths) => match SidecarManager::spawn(&paths, &config_path) {
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
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::app_info,
            commands::bridge_ping,
            commands::confirm_destination,
            commands::consult_interact,
            commands::set_ai_display_name
        ]);

    let application = builder
        .build(tauri::generate_context!())
        .expect("failed to build the Tauri application");

    application.run(|handle, event| {
        if let RunEvent::Exit = event {
            let state = handle.state::<AppState>();
            let manager = state
                .manager
                .lock()
                .ok()
                .and_then(|guard| guard.clone());
            if let Some(manager) = manager {
                manager.shutdown();
            }
        }
    });
}
