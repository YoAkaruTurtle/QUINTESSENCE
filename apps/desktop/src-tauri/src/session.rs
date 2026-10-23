//! Host-owned session state: the cached `app.info` and the destination checks.
//!
//! The WebView is never an authority.  Rust re-reads `app.info` before each
//! action, binds the call to the current host session and compares the
//! frontend's confirmation *intent* with the host-derived destination.  The
//! trusted Python host records the confirmation and applies the final check;
//! this layer only prevents an obviously mismatched call from leaving the
//! process.

use serde_json::Value;

use crate::sidecar::HostError;

#[derive(Default)]
pub struct HostSession {
    info: Option<Value>,
}

impl HostSession {
    pub fn update(&mut self, info: Value) -> Result<(), HostError> {
        let object = info
            .as_object()
            .ok_or_else(|| HostError::new("PROTOCOL_ERROR", "app.info 不是对象"))?;
        let session = object
            .get("session_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| HostError::new("PROTOCOL_ERROR", "app.info 缺少 session_id"))?;
        if session.len() > 128 {
            return Err(HostError::new(
                "PROTOCOL_ERROR",
                "app.info 的 session_id 过长",
            ));
        }
        if let Some(destination) = object.get("destination") {
            if !destination.is_null() {
                let destination = destination.as_object().ok_or_else(|| {
                    HostError::new("PROTOCOL_ERROR", "app.info 的 destination 不是对象")
                })?;
                destination
                    .get("destination_id")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                    .ok_or_else(|| {
                        HostError::new(
                            "PROTOCOL_ERROR",
                            "app.info 的 destination 缺少 destination_id",
                        )
                    })?;
                destination
                    .get("confirmation_required")
                    .and_then(Value::as_bool)
                    .ok_or_else(|| {
                        HostError::new(
                            "PROTOCOL_ERROR",
                            "app.info 的 destination 缺少 confirmation_required",
                        )
                    })?;
            }
        }
        self.info = Some(info);
        Ok(())
    }

    pub fn info(&self) -> Option<&Value> {
        self.info.as_ref()
    }

    fn session_id(&self) -> Option<&str> {
        self.info
            .as_ref()
            .and_then(|info| info.get("session_id"))
            .and_then(Value::as_str)
    }

    fn destination(&self) -> Option<&Value> {
        self.info
            .as_ref()
            .and_then(|info| info.get("destination"))
            .filter(|value| !value.is_null())
    }

    fn destination_id(&self) -> Option<&str> {
        self.destination()
            .and_then(|destination| destination.get("destination_id"))
            .and_then(Value::as_str)
    }

    fn confirmation_required(&self) -> bool {
        self.destination()
            .and_then(|destination| destination.get("confirmation_required"))
            .and_then(Value::as_bool)
            .unwrap_or(false)
    }

    pub fn validate_consult(
        &self,
        session_id: &str,
        confirmed_destination_id: Option<&str>,
    ) -> Result<(), HostError> {
        let current_session = self.session_id().ok_or_else(|| {
            HostError::new("HOST_NOT_READY", "宿主会话尚未就绪")
        })?;
        if session_id != current_session {
            return Err(HostError::new(
                "SESSION_MISMATCH",
                "请求属于另一个宿主会话",
            ));
        }
        let destination = self.destination_id().ok_or_else(|| {
            HostError::new("HOST_NOT_READY", "宿主尚无可用的目的地")
        })?;
        if self.confirmation_required() {
            match confirmed_destination_id {
                None => Err(HostError::new(
                    "CONFIRMATION_REQUIRED",
                    "外部目的地尚未在本会话中确认",
                )),
                Some(value) if value != destination => Err(HostError::new(
                    "CONFIRMATION_REQUIRED",
                    "目的地已变化；请重新确认当前目的地",
                )),
                Some(_) => Ok(()),
            }
        } else if let Some(value) = confirmed_destination_id {
            if value != destination {
                return Err(HostError::new(
                    "DESTINATION_MISMATCH",
                    "确认的目的地与当前目的地不一致",
                ));
            }
            Ok(())
        } else {
            Ok(())
        }
    }

    pub fn validate_confirm(
        &self,
        session_id: &str,
        destination_id: &str,
    ) -> Result<(), HostError> {
        let current_session = self.session_id().ok_or_else(|| {
            HostError::new("HOST_NOT_READY", "宿主会话尚未就绪")
        })?;
        if session_id != current_session {
            return Err(HostError::new(
                "SESSION_MISMATCH",
                "确认属于另一个宿主会话",
            ));
        }
        let current = self.destination_id().ok_or_else(|| {
            HostError::new("HOST_NOT_READY", "宿主尚无可用的目的地")
        })?;
        if destination_id != current {
            return Err(HostError::new(
                "DESTINATION_MISMATCH",
                "确认的目的地与当前目的地不一致",
            ));
        }
        Ok(())
    }

    /// The `host.ready` flag the UI shows; a missing cache is not ready.
    pub fn ready(&self) -> bool {
        self.info
            .as_ref()
            .and_then(|info| info.get("host"))
            .and_then(|host| host.get("ready"))
            .and_then(Value::as_bool)
            .unwrap_or(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn session(confirmation_required: bool) -> HostSession {
        let mut state = HostSession::default();
        state
            .update(json!({
                "session_id": "11111111-1111-4111-8111-111111111111",
                "host": {"ready": true},
                "destination": {
                    "destination_id": "sha256:abc",
                    "confirmation_required": confirmation_required,
                    "external": confirmation_required,
                },
            }))
            .unwrap();
        state
    }

    #[test]
    fn external_destination_requires_a_matching_confirmation() {
        let state = session(true);
        assert_eq!(
            state
                .validate_consult("11111111-1111-4111-8111-111111111111", None)
                .unwrap_err()
                .code,
            "CONFIRMATION_REQUIRED"
        );
        assert_eq!(
            state
                .validate_consult(
                    "11111111-1111-4111-8111-111111111111",
                    Some("sha256:other")
                )
                .unwrap_err()
                .code,
            "CONFIRMATION_REQUIRED"
        );
        assert!(state
            .validate_consult(
                "11111111-1111-4111-8111-111111111111",
                Some("sha256:abc")
            )
            .is_ok());
    }

    #[test]
    fn local_destination_needs_no_confirmation_but_rejects_a_foreign_id() {
        let state = session(false);
        assert!(state
            .validate_consult("11111111-1111-4111-8111-111111111111", None)
            .is_ok());
        assert_eq!(
            state
                .validate_consult(
                    "11111111-1111-4111-8111-111111111111",
                    Some("sha256:other")
                )
                .unwrap_err()
                .code,
            "DESTINATION_MISMATCH"
        );
    }

    #[test]
    fn a_stale_session_is_rejected_before_the_sidecar_call() {
        let state = session(true);
        assert_eq!(
            state
                .validate_consult("22222222-2222-4222-8222-222222222222", None)
                .unwrap_err()
                .code,
            "SESSION_MISMATCH"
        );
        assert_eq!(
            state
                .validate_confirm(
                    "22222222-2222-4222-8222-222222222222",
                    "sha256:abc"
                )
                .unwrap_err()
                .code,
            "SESSION_MISMATCH"
        );
    }

    #[test]
    fn confirmation_requires_the_current_destination() {
        let state = session(true);
        assert_eq!(
            state
                .validate_confirm(
                    "11111111-1111-4111-8111-111111111111",
                    "sha256:other"
                )
                .unwrap_err()
                .code,
            "DESTINATION_MISMATCH"
        );
        assert!(state
            .validate_confirm(
                "11111111-1111-4111-8111-111111111111",
                "sha256:abc"
            )
            .is_ok());
    }
}
