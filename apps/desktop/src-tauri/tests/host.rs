//! Host-side failure semantics: timeout, crash, protocol violation, coalescing.
#![cfg(feature = "host-tests")]

use std::process::Command;
use std::sync::Arc;
use std::time::Duration;

use serde_json::Value;

use quintessence_desktop_lib::sidecar::{HostReply, SidecarInfo, SidecarManager};

fn fake(mode: &str) -> SidecarManager {
    let executable = env!("CARGO_BIN_EXE_fake_sidecar");
    let mut command = Command::new(executable);
    command.arg("--mode").arg(mode);
    SidecarManager::spawn_test(command, SidecarInfo::dummy()).expect("fake sidecar")
}

fn error(reply: &HostReply) -> &quintessence_desktop_lib::sidecar::HostError {
    reply.error.as_ref().expect("expected an error reply")
}

#[test]
fn timeout_terminates_the_child_and_returns_result_unknown() {
    let manager = fake("hang");
    let started = std::time::Instant::now();
    let reply = manager.request_with_timeout(
        "bridge.ping",
        serde_json::json!({}),
        Duration::from_millis(500),
    );
    assert!(!reply.ok);
    assert_eq!("TIMEOUT", error(&reply).code);
    assert!(error(&reply).result_unknown);
    assert!(started.elapsed() < Duration::from_secs(5));
    assert!(!manager.is_alive());
    manager.shutdown();
}

#[test]
fn child_crash_fails_the_pending_request_with_unknown_result() {
    let manager = fake("crash");
    let reply = manager.request_with_timeout(
        "bridge.ping",
        serde_json::json!({}),
        Duration::from_secs(5),
    );
    assert!(!reply.ok);
    assert_eq!("SIDECAR_EXIT", error(&reply).code);
    assert!(error(&reply).result_unknown);
    assert!(!manager.is_alive());
    manager.shutdown();
}

#[test]
fn protocol_violation_fails_pending_and_kills_the_child() {
    let manager = fake("bad");
    let reply = manager.request_with_timeout(
        "bridge.ping",
        serde_json::json!({}),
        Duration::from_secs(5),
    );
    assert!(!reply.ok);
    assert_eq!("PROTOCOL_ERROR", error(&reply).code);
    assert!(error(&reply).result_unknown);
    assert!(!manager.is_alive());
    manager.shutdown();
}

#[test]
fn coalesced_responses_are_routed_to_both_requests() {
    let manager = Arc::new(fake("two"));
    let first = {
        let manager = Arc::clone(&manager);
        std::thread::spawn(move || {
            manager.request_with_timeout(
                "bridge.ping",
                serde_json::json!({}),
                Duration::from_secs(5),
            )
        })
    };
    let second = {
        let manager = Arc::clone(&manager);
        std::thread::spawn(move || {
            manager.request_with_timeout(
                "bridge.ping",
                serde_json::json!({}),
                Duration::from_secs(5),
            )
        })
    };
    let first = first.join().expect("first thread");
    let second = second.join().expect("second thread");
    assert!(first.ok, "{first:?}");
    assert!(second.ok, "{second:?}");
    manager.shutdown();
}

#[test]
fn chunked_response_is_reassembled_across_reads() {
    let manager = fake("chunked");
    let reply = manager.request_with_timeout(
        "bridge.ping",
        serde_json::json!({}),
        Duration::from_secs(5),
    );
    assert!(reply.ok, "{reply:?}");
    assert_eq!(
        Some(true),
        reply
            .result
            .as_ref()
            .and_then(|value| value.get("pong"))
            .and_then(Value::as_bool)
    );
    manager.shutdown();
}
