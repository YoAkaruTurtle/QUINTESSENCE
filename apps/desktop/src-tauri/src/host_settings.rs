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

use crate::sidecar::HostError;

pub const PROVIDER: &str = "deepseek";
pub const DEFAULT_MODEL: &str = "deepseek-flash";
pub const PRO_MODEL: &str = "deepseek-v4-pro";
pub const BASE_URL: &str = "https://api.deepseek.com";
pub const KEY_ENV: &str = "QUINTESSENCE_DESKTOP_MODEL_KEY";
pub const SETTINGS_SCHEMA: &str = "desktop.settings.v1";
pub const SETTINGS_FILE: &str = "desktop-settings.json";
pub const MANAGED_CONFIG_FILE: &str = "desktop-managed-host.json";
pub const LEGACY_CONFIG_FILE: &str = "desktop-host.json";

pub const MODEL_CHOICES: [&str; 2] = [DEFAULT_MODEL, PRO_MODEL];

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct HostSettings {
    #[serde(default = "schema")]
    pub schema: String,
    #[serde(default = "provider")]
    pub provider: String,
    #[serde(default = "default_model")]
    pub model: String,
    #[serde(default = "base_url")]
    pub base_url: String,
}

fn schema() -> String { SETTINGS_SCHEMA.to_string() }
fn provider() -> String { PROVIDER.to_string() }
fn default_model() -> String { DEFAULT_MODEL.to_string() }
fn base_url() -> String { BASE_URL.to_string() }

impl Default for HostSettings {
    fn default() -> Self {
        Self {
            schema: schema(),
            provider: provider(),
            model: default_model(),
            base_url: base_url(),
        }
    }
}

pub fn data_dir(app: &AppHandle) -> PathBuf {
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
    fs::write(path, text).map_err(|_| {
        HostError::new("SETTINGS_UNAVAILABLE", "模型设置无法写入应用配置目录")
    })
}

pub fn validate_settings(settings: HostSettings) -> Result<HostSettings, HostError> {
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
    Ok(settings)
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
    if let Some(path) = requested {
        return (path, false);
    }
    if let Some(value) = std::env::var_os("QUINTESSENCE_DESKTOP_CONFIG") {
        if !value.is_empty() {
            return (PathBuf::from(value), false);
        }
    }
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
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|_| {
            HostError::new("CONFIG_UNAVAILABLE", "无法创建应用数据目录")
        })?;
    }
    let database = data_dir_from_config(path).join("pcs.sqlite");
    let document = managed_config(settings, &database);
    let text = serde_json::to_string_pretty(&document).map_err(|_| {
        HostError::new("CONFIG_UNAVAILABLE", "托管宿主配置无法序列化")
    })?;
    fs::write(path, text).map_err(|_| {
        HostError::new("CONFIG_UNAVAILABLE", "托管宿主配置无法写入")
    })
}

fn data_dir_from_config(path: &Path) -> PathBuf {
    path.parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| std::env::temp_dir().join("quintessence-desktop"))
}

fn managed_config(settings: &HostSettings, database: &Path) -> Value {
    json!({
        "schema": "desktop.host.v1",
        "app": {
            "profile": "production",
            "label": "QUINTESSENCE"
        },
        "store": {
            "database": database.to_string_lossy()
        },
        "host": {
            "namespace": "quintessence.desktop",
            "host_instance": "host:desktop",
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
                "max_calls": 64,
                "max_output_tokens_per_call": 4096,
                "max_wall_seconds": 120,
                "thinking": "disabled",
                "temperature": 0
            }
        },
        "desktop": {
            "managed": true,
            "provision": true
        }
    })
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
    fn model_choices_are_closed_and_no_private_name_is_generated() {
        assert!(validate_model(DEFAULT_MODEL).is_ok());
        assert!(validate_model(PRO_MODEL).is_ok());
        assert_eq!(validate_model("other").unwrap_err().code, "INVALID_MODEL");

        let settings = HostSettings::default();
        let document = managed_config(&settings, Path::new("pcs.sqlite"));
        assert_eq!(document["app"]["profile"], "production");
        assert!(document["app"].get("ai_display_name").is_none());
        assert_eq!(document["desktop"]["managed"], true);
        assert_eq!(
            document["destination"]["manifest"]["api_key_env"],
            KEY_ENV
        );
        assert_eq!(document["destination"]["provider"], "deepseek");
        assert!(!document.to_string().contains("\u{5343}\u{5c81}"));
    }

    #[test]
    fn settings_validation_rejects_untrusted_provider_and_url() {
        let mut settings = HostSettings::default();
        settings.provider = "other".to_string();
        assert!(validate_settings(settings).is_err());

        let mut settings = HostSettings::default();
        settings.base_url = "https://example.invalid".to_string();
        assert!(validate_settings(settings).is_err());
    }
}
