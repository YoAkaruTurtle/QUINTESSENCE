//! Desktop-only UI preferences.
//!
//! The AI display name is an interaction preference, not a PCS identity.  It is
//! stored in the application config directory, never in this repository, never
//! in the installer, and never in the PCS store.  Changing it cannot change a
//! namespace, source identity, authorization, Persona or model destination.

use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};

use crate::sidecar::HostError;

pub const DEFAULT_AI_DISPLAY_NAME: &str = "助手";
const MAX_NAME_CHARS: usize = 40;
const FILE_NAME: &str = "ui-preferences.json";

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct UiPreferences {
    #[serde(default)]
    pub ai_display_name: Option<String>,
}

pub fn path_for(app: &AppHandle) -> PathBuf {
    app.path()
        .app_config_dir()
        .unwrap_or_else(|_| std::env::temp_dir().join("quintessence-desktop"))
        .join(FILE_NAME)
}

pub fn load(path: &Path) -> UiPreferences {
    let text = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(_) => return UiPreferences::default(),
    };
    match serde_json::from_str::<UiPreferences>(&text) {
        Ok(preferences) => UiPreferences {
            ai_display_name: preferences.ai_display_name.and_then(|name| {
                validate(&name).ok().flatten()
            }),
        },
        Err(_) => UiPreferences::default(),
    }
}

pub fn save(path: &Path, preferences: &UiPreferences) -> Result<(), HostError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|_| {
            HostError::new("PREFERENCES_UNAVAILABLE", "无法创建应用配置目录")
        })?;
    }
    let text = serde_json::to_string_pretty(preferences).map_err(|_| {
        HostError::new("PREFERENCES_UNAVAILABLE", "偏好设置无法序列化")
    })?;
    fs::write(path, text).map_err(|_| {
        HostError::new("PREFERENCES_UNAVAILABLE", "偏好设置无法写入应用配置目录")
    })
}

/// Return `None` for "use the host/default name", or the validated name.
pub fn validate(value: &str) -> Result<Option<String>, HostError> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Ok(None);
    }
    if trimmed.chars().count() > MAX_NAME_CHARS {
        return Err(HostError::new(
            "INVALID_DISPLAY_NAME",
            "对话显示名不能超过 40 个字符",
        ));
    }
    if trimmed.chars().any(char::is_control) {
        return Err(HostError::new(
            "INVALID_DISPLAY_NAME",
            "对话显示名不能包含控制字符",
        ));
    }
    Ok(Some(trimmed.to_string()))
}

/// Resolve the effective name and where it came from.
pub fn effective(
    preferences: &UiPreferences,
    host_default: Option<&str>,
) -> (String, &'static str) {
    if let Some(name) = preferences
        .ai_display_name
        .as_deref()
        .filter(|value| !value.is_empty())
    {
        return (name.to_string(), "preference");
    }
    if let Some(name) = host_default.filter(|value| !value.trim().is_empty()) {
        return (name.trim().to_string(), "host");
    }
    (DEFAULT_AI_DISPLAY_NAME.to_string(), "default")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display_name_is_trimmed_and_bounded() {
        assert_eq!(validate("  小助手  ").unwrap().as_deref(), Some("小助手"));
        assert_eq!(validate("   ").unwrap(), None);
        assert!(validate(&"a".repeat(41)).is_err());
        assert!(validate("bad\nname").is_err());
    }

    #[test]
    fn effective_name_prefers_preference_then_host_then_default() {
        let mut preferences = UiPreferences::default();
        assert_eq!(effective(&preferences, Some("HostName")), ("HostName".to_string(), "host"));
        assert_eq!(effective(&preferences, None), ("助手".to_string(), "default"));
        preferences.ai_display_name = Some("Chosen".to_string());
        assert_eq!(effective(&preferences, Some("HostName")), ("Chosen".to_string(), "preference"));
    }

    #[test]
    fn preferences_round_trip_outside_the_store() {
        let directory = std::env::temp_dir().join(format!(
            "quintessence-preferences-{}",
            std::process::id()
        ));
        let path = directory.join(FILE_NAME);
        let preferences = UiPreferences {
            ai_display_name: Some("Chosen".to_string()),
        };
        save(&path, &preferences).unwrap();
        let loaded = load(&path);
        assert_eq!(loaded.ai_display_name.as_deref(), Some("Chosen"));
        let _ = fs::remove_dir_all(&directory);
    }
}
