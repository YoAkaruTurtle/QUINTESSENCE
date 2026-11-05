//! Desktop-only UI preferences.
//!
//! The AI display name is an interaction preference, not a PCS identity.  It is
//! stored with a prepared instance (or the legacy host settings), never in this repository, never
//! in the installer, and never in the PCS store.  Changing it cannot change a
//! namespace, source identity, authorization, Persona or model destination.
//!
//! UI-F04 adds a display-only conversation layer: archived conversations and
//! renamed display titles.  Both are application UI preferences scoped by the
//! F03 instance fingerprint.  They never write a conversation, turn, message,
//! artifact, memory, cognition or database row.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};

use crate::sidecar::HostError;

pub const DEFAULT_AI_DISPLAY_NAME: &str = "助手";
const MAX_NAME_CHARS: usize = 40;
const FILE_NAME: &str = "ui-preferences.json";
/// Debug-only base directory for `ui-preferences.json`; a release build ignores
/// it.  It exists so Lab evidence never reads or writes the real app config.
const PREFERENCES_DIR_ENV: &str = "QUINTESSENCE_DESKTOP_PREFERENCES_DIR";
pub const MAX_CONVERSATION_TITLE_CHARS: usize = 40;
pub const MAX_ARCHIVED_CONVERSATIONS: usize = 200;
pub const MAX_CONVERSATION_TITLES: usize = 500;
/// One archived conversation id.  It carries no message, turn or artifact
/// reference and no namespace material: the surrounding scope key owns that.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct ArchivedConversation {
    #[serde(default)]
    pub conversation_id: String,
    #[serde(default)]
    pub archived_at_ms: u64,
}

/// The display-only conversation slice of one instance.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct ConversationUiScope {
    #[serde(default)]
    pub archived: Vec<ArchivedConversation>,
    #[serde(default)]
    pub titles: BTreeMap<String, String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct ConversationLimits {
    pub archived: usize,
    pub titles: usize,
    pub title_chars: usize,
}

#[derive(Clone, Debug, Serialize)]
pub struct ConversationUiView {
    /// True when the host could prove its instance identity.  Without one the
    /// view is empty and writes are refused; another instance is never used.
    pub scoped: bool,
    pub archived: Vec<ArchivedConversation>,
    pub titles: BTreeMap<String, String>,
    pub limits: ConversationLimits,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct UiPreferences {
    #[serde(default)]
    pub sidebar_collapsed: bool,
    #[serde(default = "default_panel_height")]
    pub panel_height: u32,
    #[serde(default = "default_language")]
    pub language: String,
    #[serde(default = "default_theme")]
    pub theme: String,
    #[serde(default)]
    pub ai_display_name: Option<String>,
    /// `true` = user explicitly chose a name; `false` = user explicitly chose
    /// the neutral default.  Missing = legacy/automatic preference.
    #[serde(default)]
    pub ai_display_name_explicit: Option<bool>,
    /// Display-only conversation state keyed by the instance fingerprint.
    /// Older files have no such key and load unchanged.
    #[serde(default)]
    pub conversation_ui: BTreeMap<String, ConversationUiScope>,
}

fn default_language() -> String { "zh-CN".into() }
fn default_panel_height() -> u32 { 320 }
fn default_theme() -> String { "dark".into() }
impl Default for UiPreferences {
    fn default() -> Self { Self { sidebar_collapsed: false, panel_height: default_panel_height(), language: default_language(), theme: default_theme(), ai_display_name: None, ai_display_name_explicit: None, conversation_ui: BTreeMap::new() } }
}

pub fn path_for(app: &AppHandle) -> PathBuf {
    if let Some(selected) = std::env::var_os("QUINTESSENCE_DESKTOP_STATE_DIR").map(PathBuf::from) {
        if crate::paths::validate_instance_root(&selected).is_ok() { return selected.join(FILE_NAME); }
    }
    let default = app
        .path()
        .app_config_dir()
        .unwrap_or_else(|_| std::env::temp_dir().join("quintessence-desktop"));
    resolve_preferences_dir(default).0.join(FILE_NAME)
}

/// Resolve the preferences directory.  The environment override exists only in
/// a debug build; a release binary ignores it even when the variable is set.
/// The second value reports a rejected override (never an empty fallback).
pub fn resolve_preferences_dir(default: PathBuf) -> (PathBuf, bool) {
    #[cfg(debug_assertions)]
    {
        if let Some(raw) = std::env::var_os(PREFERENCES_DIR_ENV) {
            return match crate::logs::validated_dir(Path::new(&raw)) {
                Some(path) => (path, false),
                None => (default, true),
            };
        }
    }
    (default, false)
}

/// A 64-hex instance fingerprint from `logs::InstanceIdentity::fingerprint`.
pub fn valid_instance_scope(value: &str) -> bool {
    value.len() == 64 && value.chars().all(|ch| ch.is_ascii_hexdigit())
}

/// Canonical lowercase UUID.  Anything else is refused rather than stored.
pub fn normalize_conversation_id(value: &str) -> Option<String> {
    let text = value.trim();
    if text.len() != 36 {
        return None;
    }
    for (index, ch) in text.chars().enumerate() {
        let allowed = match index {
            8 | 13 | 18 | 23 => ch == '-',
            _ => ch.is_ascii_hexdigit(),
        };
        if !allowed {
            return None;
        }
    }
    Some(text.to_ascii_lowercase())
}

/// A conversation display title: trimmed, 1..=40 characters, no control
/// characters.  The database title is validated separately and never written.
pub fn validate_conversation_title(value: &str) -> Result<String, HostError> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err(HostError::new("INVALID_PARAMS", "会话显示标题不能为空"));
    }
    if trimmed.chars().count() > MAX_CONVERSATION_TITLE_CHARS {
        return Err(HostError::new(
            "INVALID_PARAMS",
            "会话显示标题不能超过 40 个字符",
        ));
    }
    if trimmed.chars().any(char::is_control) {
        return Err(HostError::new(
            "INVALID_PARAMS",
            "会话显示标题不能包含控制字符",
        ));
    }
    Ok(trimmed.to_string())
}

fn prune_scope(preferences: &mut UiPreferences, scope_key: &str) {
    let empty = preferences
        .conversation_ui
        .get(scope_key)
        .map(|scope| scope.archived.is_empty() && scope.titles.is_empty())
        .unwrap_or(false);
    if empty {
        preferences.conversation_ui.remove(scope_key);
    }
}

/// Apply one display preference action.  Pure enough to unit test without a
/// Tauri state: it only mutates `preferences`, and every error leaves the
/// caller's clone untouched.
pub fn apply_conversation_action(
    preferences: &mut UiPreferences,
    scope_key: &str,
    action: &str,
    conversation_id: Option<&str>,
    title: Option<&str>,
    now_ms: u64,
) -> Result<(), HostError> {
    if !valid_instance_scope(scope_key) {
        return Err(HostError::new("HOST_INTERNAL", "实例作用域不可用"));
    }
    if action == "restore_all" {
        if let Some(scope) = preferences.conversation_ui.get_mut(scope_key) {
            scope.archived.clear();
        }
        prune_scope(preferences, scope_key);
        return Ok(());
    }
    let id = conversation_id
        .and_then(normalize_conversation_id)
        .ok_or_else(|| HostError::new("INVALID_PARAMS", "conversation_id 不是有效 UUID"))?;
    match action {
        "rename" => {
            let value = title
                .ok_or_else(|| HostError::new("INVALID_PARAMS", "缺少会话显示标题"))?;
            let value = validate_conversation_title(value)?;
            let existing = preferences.conversation_ui.get(scope_key);
            let already_named = existing
                .map(|scope| scope.titles.contains_key(&id))
                .unwrap_or(false);
            let count = existing.map(|scope| scope.titles.len()).unwrap_or(0);
            if !already_named && count >= MAX_CONVERSATION_TITLES {
                return Err(HostError::new(
                    "INVALID_PARAMS",
                    "会话显示标题数量已达上限",
                ));
            }
            preferences
                .conversation_ui
                .entry(scope_key.to_string())
                .or_default()
                .titles
                .insert(id, value);
        }
        "clear_title" => {
            if let Some(scope) = preferences.conversation_ui.get_mut(scope_key) {
                scope.titles.remove(&id);
            }
            prune_scope(preferences, scope_key);
        }
        "archive" => {
            let existing = preferences.conversation_ui.get(scope_key);
            let already_archived = existing
                .map(|scope| scope.archived.iter().any(|item| item.conversation_id == id))
                .unwrap_or(false);
            if !already_archived {
                let count = existing.map(|scope| scope.archived.len()).unwrap_or(0);
                if count >= MAX_ARCHIVED_CONVERSATIONS {
                    return Err(HostError::new(
                        "INVALID_PARAMS",
                        "已归档会话数量已达上限",
                    ));
                }
                preferences
                    .conversation_ui
                    .entry(scope_key.to_string())
                    .or_default()
                    .archived
                    .push(ArchivedConversation {
                        conversation_id: id,
                        archived_at_ms: now_ms,
                    });
            }
        }
        "unarchive" => {
            if let Some(scope) = preferences.conversation_ui.get_mut(scope_key) {
                scope.archived.retain(|item| item.conversation_id != id);
            }
            prune_scope(preferences, scope_key);
        }
        _ => return Err(HostError::new("INVALID_PARAMS", "不支持的会话显示操作")),
    }
    Ok(())
}

/// The current instance slice, or an honestly empty unscoped view.  Another
/// instance's entries are never returned.
pub fn conversation_view(
    preferences: &UiPreferences,
    scope_key: Option<&str>,
) -> ConversationUiView {
    let limits = ConversationLimits {
        archived: MAX_ARCHIVED_CONVERSATIONS,
        titles: MAX_CONVERSATION_TITLES,
        title_chars: MAX_CONVERSATION_TITLE_CHARS,
    };
    match scope_key.filter(|key| valid_instance_scope(key)) {
        Some(key) => {
            let (archived, titles) = preferences
                .conversation_ui
                .get(key)
                .map(|scope| (scope.archived.clone(), scope.titles.clone()))
                .unwrap_or_default();
            ConversationUiView { scoped: true, archived, titles, limits }
        }
        None => ConversationUiView {
            scoped: false,
            archived: Vec::new(),
            titles: BTreeMap::new(),
            limits,
        },
    }
}

/// Drop hand-edited or legacy garbage instead of feeding it to the WebView.
fn sanitize_conversation_ui(
    raw: BTreeMap<String, ConversationUiScope>,
) -> BTreeMap<String, ConversationUiScope> {
    let mut scopes = BTreeMap::new();
    for (key, scope) in raw {
        let key = key.trim().to_ascii_lowercase();
        if !valid_instance_scope(&key) {
            continue;
        }
        let mut archived = Vec::new();
        let mut seen = BTreeSet::new();
        for item in scope.archived {
            let Some(id) = normalize_conversation_id(&item.conversation_id) else {
                continue;
            };
            if !seen.insert(id.clone()) || archived.len() >= MAX_ARCHIVED_CONVERSATIONS {
                continue;
            }
            archived.push(ArchivedConversation {
                conversation_id: id,
                archived_at_ms: item.archived_at_ms,
            });
        }
        let mut titles = BTreeMap::new();
        for (id, title) in scope.titles {
            if titles.len() >= MAX_CONVERSATION_TITLES {
                break;
            }
            let Some(id) = normalize_conversation_id(&id) else {
                continue;
            };
            let Ok(title) = validate_conversation_title(&title) else {
                continue;
            };
            titles.insert(id, title);
        }
        if !archived.is_empty() || !titles.is_empty() {
            scopes.insert(key, ConversationUiScope { archived, titles });
        }
    }
    scopes
}

pub fn load(path: &Path) -> UiPreferences {
    let text = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(_) => return UiPreferences::default(),
    };
    match serde_json::from_str::<UiPreferences>(&text) {
        Ok(preferences) => {
            let preferences = UiPreferences {
                sidebar_collapsed: preferences.sidebar_collapsed,
                panel_height: preferences.panel_height.clamp(240, 1600),
                ai_display_name: preferences.ai_display_name.and_then(|name| {
                    validate(&name).ok().flatten()
                }),
                ai_display_name_explicit: preferences.ai_display_name_explicit,
                language: if ["zh-CN", "en"].contains(&preferences.language.as_str()) { preferences.language } else { default_language() },
                theme: if ["light", "dark"].contains(&preferences.theme.as_str()) { preferences.theme } else { default_theme() },
                conversation_ui: sanitize_conversation_ui(preferences.conversation_ui),
            };
            // Existing valid names belong to the instance. Do not infer that
            // one particular name is an automatic default and erase it.
            preferences
        }
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
    crate::host_settings::atomic_write(path, &text)
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
    if preferences.ai_display_name_explicit == Some(false) {
        return (DEFAULT_AI_DISPLAY_NAME.to_string(), "default");
    }
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
    use serde_json::{json, Value};

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
        preferences.ai_display_name_explicit = Some(true);
        assert_eq!(effective(&preferences, Some("HostName")), ("Chosen".to_string(), "preference"));
    }

    #[test]
    fn explicit_reset_uses_neutral_default_even_with_a_host_default() {
        let preferences = UiPreferences {
            ai_display_name: None,
            ai_display_name_explicit: Some(false),
            ..UiPreferences::default()
        };
        assert_eq!(
            effective(&preferences, Some("ExistingHostName")),
            ("助手".to_string(), "default")
        );
    }

    #[test]
    fn legacy_names_are_preserved_without_private_name_special_cases() {
        let directory = work_dir("legacy-names");
        let path = directory.join(FILE_NAME);
        for explicit in [None, Some(true)] {
            let mut document = serde_json::json!({"ai_display_name":"My existing assistant"});
            if let Some(explicit) = explicit { document["ai_display_name_explicit"] = serde_json::json!(explicit); }
            let original = document.to_string();
            fs::write(&path, &original).unwrap();
            let loaded = load(&path);
            assert_eq!(loaded.ai_display_name.as_deref(), Some("My existing assistant"));
            assert_eq!(effective(&loaded, None).0, "My existing assistant");
            assert_eq!(fs::read_to_string(&path).unwrap(), original);
        }
        fs::remove_file(&path).unwrap();
        let _ = fs::remove_dir(&directory);
    }

    #[test]
    fn preferences_round_trip_outside_the_store() {
        let directory = work_dir("round-trip-file");
        let path = directory.join(FILE_NAME);
        let preferences = UiPreferences {
            ai_display_name: Some("Chosen".to_string()),
            ai_display_name_explicit: Some(true),
            language: "en".to_string(),
            theme: "light".to_string(),
            sidebar_collapsed: true,
            panel_height: 400,
            ..UiPreferences::default()
        };
        save(&path, &preferences).unwrap();
        let loaded = load(&path);
        assert_eq!(loaded.ai_display_name.as_deref(), Some("Chosen"));
        assert_eq!(loaded.language, "en");
        assert_eq!(loaded.theme, "light");
        assert!(loaded.sidebar_collapsed);
        assert_eq!(loaded.panel_height, 400);
        let _ = fs::remove_dir_all(&directory);
    }

    // ------------------------------------------------- UI-F04 conversation UI

    fn work_dir(name: &str) -> PathBuf {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("preferences-tests")
            .join(format!("{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn scope_key(seed: u8) -> String {
        format!("{:02x}", seed).repeat(32)
    }

    fn conversation_id(value: u32) -> String {
        format!("aaaaaaaa-bbbb-4ccc-8ddd-{value:012x}")
    }

    fn snapshot(preferences: &UiPreferences) -> Value {
        serde_json::to_value(preferences).unwrap()
    }

    #[test]
    fn conversation_title_is_trimmed_and_bounded() {
        assert_eq!(
            validate_conversation_title("  项目笔记  ").unwrap(),
            "项目笔记"
        );
        assert_eq!(
            validate_conversation_title(&"会".repeat(40)).unwrap().chars().count(),
            40
        );
        assert!(validate_conversation_title("").is_err());
        assert!(validate_conversation_title("   ").is_err());
        assert!(validate_conversation_title(&"a".repeat(41)).is_err());
        assert!(validate_conversation_title("bad\nname").is_err());
        assert!(validate_conversation_title("bad\u{7f}name").is_err());
    }

    #[test]
    fn conversation_ids_are_canonical_uuids() {
        let id = conversation_id(7);
        assert_eq!(
            normalize_conversation_id(&id.to_ascii_uppercase()).unwrap(),
            id
        );
        assert_eq!(normalize_conversation_id(&format!("  {id}  ")).unwrap(), id);
        assert!(normalize_conversation_id("not-a-uuid").is_none());
        assert!(normalize_conversation_id("../../etc/passwd").is_none());
        assert!(normalize_conversation_id("aaaaaaaa-bbbb-4ccc-8ddd-00000000000g").is_none());
    }

    #[test]
    fn archive_is_idempotent_and_scoped_to_the_current_instance() {
        let a = scope_key(0xa1);
        let b = scope_key(0xb2);
        let id = conversation_id(1);
        let mut preferences = UiPreferences::default();
        apply_conversation_action(&mut preferences, &a, "archive", Some(&id), None, 1_000).unwrap();
        apply_conversation_action(&mut preferences, &a, "archive", Some(&id), None, 2_000).unwrap();
        assert_eq!(preferences.conversation_ui[&a].archived.len(), 1);
        assert_eq!(preferences.conversation_ui[&a].archived[0].archived_at_ms, 1_000);
        let view_a = conversation_view(&preferences, Some(&a));
        let view_b = conversation_view(&preferences, Some(&b));
        assert_eq!(view_a.archived.len(), 1);
        assert!(view_b.scoped);
        assert!(view_b.archived.is_empty());
        assert!(view_b.titles.is_empty());
    }

    #[test]
    fn restore_removes_one_or_all_without_touching_another_instance() {
        let a = scope_key(0xa1);
        let b = scope_key(0xb2);
        let (first, second, other) = (conversation_id(1), conversation_id(2), conversation_id(3));
        let mut preferences = UiPreferences::default();
        for id in [&first, &second] {
            apply_conversation_action(&mut preferences, &a, "archive", Some(id), None, 10).unwrap();
        }
        apply_conversation_action(&mut preferences, &b, "archive", Some(&other), None, 10).unwrap();
        apply_conversation_action(&mut preferences, &a, "unarchive", Some(&first), None, 0).unwrap();
        let view_a = conversation_view(&preferences, Some(&a));
        assert_eq!(view_a.archived.len(), 1);
        assert_eq!(view_a.archived[0].conversation_id, second);
        apply_conversation_action(&mut preferences, &a, "restore_all", None, None, 0).unwrap();
        assert!(conversation_view(&preferences, Some(&a)).archived.is_empty());
        assert!(!preferences.conversation_ui.contains_key(&a));
        assert_eq!(conversation_view(&preferences, Some(&b)).archived.len(), 1);
        // Unarchiving an id that is not archived is a no-op, not an error.
        apply_conversation_action(&mut preferences, &a, "unarchive", Some(&first), None, 0).unwrap();
    }

    #[test]
    fn titles_are_a_display_override_and_clear_restores_the_host_title() {
        let a = scope_key(0xa1);
        let id = conversation_id(4);
        let mut preferences = UiPreferences::default();
        apply_conversation_action(&mut preferences, &a, "rename", Some(&id), Some(" 新名字 "), 0)
            .unwrap();
        let view = conversation_view(&preferences, Some(&a));
        assert_eq!(view.titles[&id], "新名字");
        apply_conversation_action(&mut preferences, &a, "clear_title", Some(&id), None, 0).unwrap();
        assert!(conversation_view(&preferences, Some(&a)).titles.is_empty());
        assert!(!preferences.conversation_ui.contains_key(&a));
    }

    #[test]
    fn rejected_actions_never_mutate_the_preferences() {
        let a = scope_key(0xa1);
        let id = conversation_id(5);
        let mut preferences = UiPreferences::default();
        let before = snapshot(&preferences);
        assert_eq!(
            apply_conversation_action(&mut preferences, &a, "destroy", Some(&id), None, 0)
                .unwrap_err()
                .code,
            "INVALID_PARAMS"
        );
        assert!(apply_conversation_action(&mut preferences, &a, "rename", Some(&id), None, 0).is_err());
        assert!(apply_conversation_action(&mut preferences, &a, "rename", None, Some("x"), 0).is_err());
        assert!(apply_conversation_action(&mut preferences, &a, "archive", Some("bad"), None, 0).is_err());
        assert!(apply_conversation_action(&mut preferences, "short", "archive", Some(&id), None, 0).is_err());
        assert_eq!(snapshot(&preferences), before);
    }

    #[test]
    fn conversation_view_without_an_instance_identity_is_empty_and_unscoped() {
        let a = scope_key(0xa1);
        let id = conversation_id(6);
        let mut preferences = UiPreferences::default();
        apply_conversation_action(&mut preferences, &a, "archive", Some(&id), None, 1).unwrap();
        for key in [None, Some("not-a-fingerprint")] {
            let view = conversation_view(&preferences, key);
            assert!(!view.scoped);
            assert!(view.archived.is_empty());
            assert!(view.titles.is_empty());
            assert_eq!(view.limits.archived, MAX_ARCHIVED_CONVERSATIONS);
            assert_eq!(view.limits.titles, MAX_CONVERSATION_TITLES);
            assert_eq!(view.limits.title_chars, MAX_CONVERSATION_TITLE_CHARS);
        }
    }

    #[test]
    fn conversation_caps_reject_new_entries_only() {
        let a = scope_key(0xa1);
        let mut preferences = UiPreferences::default();
        for index in 0..MAX_ARCHIVED_CONVERSATIONS as u32 {
            let id = conversation_id(index + 10_000);
            apply_conversation_action(&mut preferences, &a, "archive", Some(&id), None, 1).unwrap();
        }
        let overflow = conversation_id(9_999);
        assert_eq!(
            apply_conversation_action(&mut preferences, &a, "archive", Some(&overflow), None, 1)
                .unwrap_err()
                .code,
            "INVALID_PARAMS"
        );
        assert_eq!(
            conversation_view(&preferences, Some(&a)).archived.len(),
            MAX_ARCHIVED_CONVERSATIONS
        );
        // Idempotent archive of an existing id still succeeds at the cap.
        apply_conversation_action(&mut preferences, &a, "archive", Some(&conversation_id(10_000)), None, 1)
            .unwrap();
    }

    #[test]
    fn title_cap_rejects_new_ids_but_allows_renaming_an_existing_one() {
        let a = scope_key(0xa1);
        let mut preferences = UiPreferences::default();
        for index in 0..MAX_CONVERSATION_TITLES as u32 {
            let id = conversation_id(index + 20_000);
            apply_conversation_action(&mut preferences, &a, "rename", Some(&id), Some("名字"), 0)
                .unwrap();
        }
        assert_eq!(
            apply_conversation_action(&mut preferences, &a, "rename", Some(&conversation_id(1)), Some("x"), 0)
                .unwrap_err()
                .code,
            "INVALID_PARAMS"
        );
        apply_conversation_action(&mut preferences, &a, "rename", Some(&conversation_id(20_000)), Some("改名"), 0)
            .unwrap();
        assert_eq!(
            conversation_view(&preferences, Some(&a)).titles[&conversation_id(20_000)],
            "改名"
        );
    }

    #[test]
    fn legacy_preferences_file_keeps_existing_keys_and_defaults_conversation_ui() {
        let directory = work_dir("legacy");
        let path = directory.join(FILE_NAME);
        fs::write(
            &path,
            r#"{"sidebar_collapsed":true,"panel_height":420,"language":"en","theme":"light","ai_display_name":"Renamed"}"#,
        )
        .unwrap();
        let loaded = load(&path);
        assert!(loaded.sidebar_collapsed);
        assert_eq!(loaded.panel_height, 420);
        assert_eq!(loaded.language, "en");
        assert_eq!(loaded.theme, "light");
        assert_eq!(loaded.ai_display_name.as_deref(), Some("Renamed"));
        assert!(loaded.conversation_ui.is_empty());
        let _ = fs::remove_dir_all(&directory);
    }

    #[test]
    fn corrupt_conversation_entries_are_dropped_on_load() {
        let directory = work_dir("corrupt");
        let path = directory.join(FILE_NAME);
        let a = scope_key(0xa1);
        let first = conversation_id(11);
        let second = conversation_id(12);
        let mut titles = serde_json::Map::new();
        titles.insert(first.clone(), json!("  项目  "));
        titles.insert("bad".to_string(), json!("x"));
        titles.insert(second.clone(), json!("bad\u{0007}name"));
        titles.insert(conversation_id(13), json!("x".repeat(41)));
        let mut scopes = serde_json::Map::new();
        scopes.insert(
            "short".to_string(),
            json!({"archived": [{"conversation_id": first, "archived_at_ms": 1}]}),
        );
        scopes.insert(
            a.clone(),
            json!({
                "archived": [
                    {"conversation_id": first.to_ascii_uppercase(), "archived_at_ms": 5},
                    {"conversation_id": first, "archived_at_ms": 6},
                    {"conversation_id": "bad", "archived_at_ms": 7}
                ],
                "titles": titles
            }),
        );
        let document = json!({"language": "zh-CN", "conversation_ui": scopes});
        fs::write(&path, serde_json::to_string(&document).unwrap()).unwrap();
        let loaded = load(&path);
        assert_eq!(loaded.conversation_ui.len(), 1);
        let scope = &loaded.conversation_ui[&a];
        assert_eq!(scope.archived.len(), 1);
        assert_eq!(scope.archived[0].conversation_id, first);
        assert_eq!(scope.archived[0].archived_at_ms, 5);
        assert_eq!(scope.titles.len(), 1);
        assert_eq!(scope.titles[&first], "项目");
        let _ = fs::remove_dir_all(&directory);
    }

    #[test]
    fn conversation_round_trip_preserves_every_other_preference() {
        let directory = work_dir("round-trip");
        let path = directory.join(FILE_NAME);
        let a = scope_key(0xa1);
        let id = conversation_id(21);
        let mut preferences = UiPreferences {
            sidebar_collapsed: true,
            panel_height: 512,
            language: "en".to_string(),
            theme: "light".to_string(),
            ai_display_name: Some("Chosen".to_string()),
            ai_display_name_explicit: Some(true),
            ..UiPreferences::default()
        };
        apply_conversation_action(&mut preferences, &a, "archive", Some(&id), None, 42).unwrap();
        apply_conversation_action(&mut preferences, &a, "rename", Some(&id), Some("显示名"), 0)
            .unwrap();
        save(&path, &preferences).unwrap();
        let loaded = load(&path);
        assert!(loaded.sidebar_collapsed);
        assert_eq!(loaded.panel_height, 512);
        assert_eq!(loaded.language, "en");
        assert_eq!(loaded.theme, "light");
        assert_eq!(loaded.ai_display_name.as_deref(), Some("Chosen"));
        assert_eq!(
            snapshot(&loaded)["conversation_ui"],
            snapshot(&preferences)["conversation_ui"]
        );
        let _ = fs::remove_dir_all(&directory);
    }

    #[test]
    fn preferences_dir_override_is_debug_only_and_validated() {
        static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
        let _guard = ENV_LOCK.lock().unwrap_or_else(|error| error.into_inner());
        let directory = work_dir("dir-override");
        let real = directory.join("prefs");
        fs::create_dir_all(&real).unwrap();
        let default = directory.join("default");
        std::env::set_var(PREFERENCES_DIR_ENV, real.as_os_str());
        let (resolved, rejected) = resolve_preferences_dir(default.clone());
        if cfg!(debug_assertions) {
            assert_eq!(resolved, real.canonicalize().unwrap());
            assert!(!rejected);
            std::env::set_var(PREFERENCES_DIR_ENV, "relative/prefs");
            let (resolved, rejected) = resolve_preferences_dir(default.clone());
            assert_eq!(resolved, default);
            assert!(rejected);
        } else {
            assert_eq!(resolved, default);
            assert!(!rejected);
        }
        std::env::remove_var(PREFERENCES_DIR_ENV);
        let _ = fs::remove_dir_all(&directory);
    }
}
