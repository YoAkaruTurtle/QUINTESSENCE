//! Process-level single-instance guard.
//!
//! A per-user Windows named mutex makes the first application process the only
//! one that can run setup (and therefore the only one that can start a
//! sidecar).  A second process focuses the first window and exits.  This is
//! deliberately narrower than a permanent PCS-wide exclusion: the guard names
//! the desktop application, not the data instance.

#[cfg(windows)]
use std::ffi::c_void;
#[cfg(windows)]
use std::thread;
#[cfg(windows)]
use std::time::Duration;

#[cfg(windows)]
use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, HANDLE,
};
#[cfg(windows)]
use windows_sys::Win32::System::Threading::CreateMutexW;
#[cfg(windows)]
use windows_sys::Win32::UI::WindowsAndMessaging::{
    FindWindowW, SetForegroundWindow, ShowWindow, SW_RESTORE,
};

const INSTANCE_NAME: &str = "Local\\QUINTESSENCE.Desktop.Consult.v1";

/// Keeps the named mutex alive for the lifetime of the first instance.
pub struct InstanceGuard {
    #[cfg(windows)]
    handle: HANDLE,
}

#[cfg(windows)]
unsafe impl Send for InstanceGuard {}

/// Return `Some(guard)` for the first instance, or focus the running window and
/// return `None` for a second instance.
pub fn acquire_or_focus(window_title: &str) -> Option<InstanceGuard> {
    #[cfg(windows)]
    {
        let name = wide(INSTANCE_NAME);
        let handle = unsafe { CreateMutexW(std::ptr::null(), 0, name.as_ptr()) };
        if handle.is_null() {
            // If the OS refuses the mutex, running is safer than silently
            // losing the window; the data-instance lock still protects SQLite.
            eprintln!("instance mutex unavailable; continuing without it");
            return Some(InstanceGuard { handle });
        }
        if unsafe { GetLastError() } == ERROR_ALREADY_EXISTS {
            unsafe {
                CloseHandle(handle);
            }
            focus_existing(window_title);
            return None;
        }
        Some(InstanceGuard { handle })
    }
    #[cfg(not(windows))]
    {
        let _ = window_title;
        Some(InstanceGuard {})
    }
}

#[cfg(windows)]
impl Drop for InstanceGuard {
    fn drop(&mut self) {
        if !self.handle.is_null() {
            unsafe {
                CloseHandle(self.handle);
            }
        }
    }
}

#[cfg(windows)]
fn focus_existing(window_title: &str) {
    let title = wide(window_title);
    // The first window may still be loading; wait briefly for it.
    for _ in 0..50 {
        let handle = unsafe { FindWindowW(std::ptr::null(), title.as_ptr()) };
        if !handle.is_null() {
            unsafe {
                ShowWindow(handle, SW_RESTORE);
                SetForegroundWindow(handle);
            }
            return;
        }
        thread::sleep(Duration::from_millis(200));
    }
}

#[cfg(windows)]
fn wide(value: &str) -> Vec<u16> {
    let mut units: Vec<u16> = value.encode_utf16().collect();
    units.push(0);
    units
}

#[cfg(windows)]
#[allow(dead_code)]
fn _assert_handle_type(_: *mut c_void) {}
