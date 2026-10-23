//! Desktop-only API-key custody.
//!
//! The key is stored as a generic credential in the current user's Windows
//! Credential Manager.  It never enters the host configuration, the PCS store,
//! logs, generated artifacts or the WebView.  The Rust host reads it only to
//! place it in the sidecar child's environment for the lifetime of that child.

use crate::sidecar::HostError;

pub const TARGET: &str = "QUINTESSENCE/deepseek-api-key";
const MAX_KEY_BYTES: usize = 512;
const MIN_KEY_BYTES: usize = 8;

/// Validate without retaining or echoing the value.
pub fn validate_key(value: &str) -> Result<(), HostError> {
    if value.len() < MIN_KEY_BYTES || value.len() > MAX_KEY_BYTES {
        return Err(HostError::new(
            "INVALID_API_KEY",
            "API Key 长度不正确",
        ));
    }
    if value != value.trim() {
        return Err(HostError::new(
            "INVALID_API_KEY",
            "API Key 不能包含首尾空白",
        ));
    }
    if !value.is_ascii()
        || value
            .chars()
            .any(|character| character.is_control() || character.is_whitespace())
    {
        return Err(HostError::new(
            "INVALID_API_KEY",
            "API Key 必须是可打印 ASCII 字符",
        ));
    }
    Ok(())
}

#[cfg(windows)]
mod platform {
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;
    use std::ptr;

    use windows_sys::Win32::Foundation::{GetLastError, ERROR_NOT_FOUND};
    use windows_sys::Win32::Security::Credentials::{
        CredDeleteW, CredFree, CredReadW, CredWriteW, CREDENTIALW, CRED_PERSIST_LOCAL_MACHINE,
        CRED_TYPE_GENERIC,
    };

    use super::{validate_key, HostError, MAX_KEY_BYTES, TARGET};

    fn wide(value: &str) -> Vec<u16> {
        OsStr::new(value)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect()
    }

    pub fn save(value: &str) -> Result<(), HostError> {
        validate_key(value)?;
        let mut target = wide(TARGET);
        let mut user = wide("quintessence");
        let mut blob = value.as_bytes().to_vec();
        let mut credential = CREDENTIALW::default();
        credential.Type = CRED_TYPE_GENERIC;
        credential.TargetName = target.as_mut_ptr();
        credential.CredentialBlobSize = blob.len() as u32;
        credential.CredentialBlob = blob.as_mut_ptr();
        credential.Persist = CRED_PERSIST_LOCAL_MACHINE;
        credential.UserName = user.as_mut_ptr();
        let ok = unsafe { CredWriteW(&credential, 0) };
        blob.fill(0);
        if ok == 0 {
            return Err(HostError::new(
                "KEY_STORE_FAILED",
                "API Key 无法写入 Windows 凭据管理器",
            ));
        }
        Ok(())
    }

    pub fn read() -> Result<Option<String>, HostError> {
        let target = wide(TARGET);
        let mut raw: *mut CREDENTIALW = ptr::null_mut();
        let ok = unsafe { CredReadW(target.as_ptr(), CRED_TYPE_GENERIC, 0, &mut raw) };
        if ok == 0 {
            let code = unsafe { GetLastError() };
            if code == ERROR_NOT_FOUND {
                return Ok(None);
            }
            return Err(HostError::new(
                "KEY_STORE_UNAVAILABLE",
                "无法读取 Windows 凭据管理器",
            ));
        }
        if raw.is_null() {
            return Err(HostError::new(
                "KEY_STORE_UNAVAILABLE",
                "Windows 凭据管理器返回了空记录",
            ));
        }
        let result = unsafe {
            let credential = &*raw;
            let size = credential.CredentialBlobSize as usize;
            if size == 0 || size > MAX_KEY_BYTES {
                Err(HostError::new("KEY_STORE_INVALID", "保存的 API Key 长度无效"))
            } else {
                let bytes = std::slice::from_raw_parts(credential.CredentialBlob, size);
                let mut copy = bytes.to_vec();
                let text = std::str::from_utf8(&copy)
                    .map(|value| value.to_string())
                    .map_err(|_| HostError::new("KEY_STORE_INVALID", "保存的 API Key 不是有效文本"));
                copy.fill(0);
                text.map(Some)
            }
        };
        unsafe { CredFree(raw as *const core::ffi::c_void) };
        result
    }

    pub fn delete() -> Result<bool, HostError> {
        let target = wide(TARGET);
        let ok = unsafe { CredDeleteW(target.as_ptr(), CRED_TYPE_GENERIC, 0) };
        if ok == 0 {
            let code = unsafe { GetLastError() };
            if code == ERROR_NOT_FOUND {
                return Ok(false);
            }
            return Err(HostError::new(
                "KEY_STORE_UNAVAILABLE",
                "无法删除 Windows 凭据管理器中的 API Key",
            ));
        }
        Ok(true)
    }
}

#[cfg(not(windows))]
mod platform {
    use super::HostError;

    pub fn save(_value: &str) -> Result<(), HostError> {
        Err(HostError::new(
            "KEY_STORE_UNAVAILABLE",
            "Windows 凭据管理器不可用",
        ))
    }

    pub fn read() -> Result<Option<String>, HostError> {
        Ok(None)
    }

    pub fn delete() -> Result<bool, HostError> {
        Ok(false)
    }
}

pub use platform::{delete, read, save};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_validation_never_echoes_the_value() {
        assert!(validate_key("sk-0123456789abcdef").is_ok());
        assert_eq!(
            validate_key(" short ").unwrap_err().code,
            "INVALID_API_KEY"
        );
        assert_eq!(validate_key("abc").unwrap_err().code, "INVALID_API_KEY");
        assert_eq!(
            validate_key("sk-0123\n456789").unwrap_err().code,
            "INVALID_API_KEY"
        );
        assert_eq!(
            validate_key("sk-0123456789\u{00e9}").unwrap_err().code,
            "INVALID_API_KEY"
        );
    }
}
