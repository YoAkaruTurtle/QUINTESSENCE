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

// ---------------------------------------------------------------------------
// RG02: the five conversation commands forward an exact, host-owned key set.
// ---------------------------------------------------------------------------

use quintessence_desktop_lib::commands::{
    conversation_create_params, conversation_get_params, conversation_list_params,
    conversation_search_params, conversation_send_params, memory_search_params,
    postprocess_retry_params, postprocess_status_params, turn_result_params,
    CONVERSATION_READ_TIMEOUT, CONVERSATION_SEND_TIMEOUT, MEMORY_SEARCH_TIMEOUT,
    METHOD_CONVERSATION_CREATE, METHOD_CONVERSATION_GET, METHOD_CONVERSATION_LIST,
    METHOD_CONVERSATION_SEARCH, METHOD_CONVERSATION_SEND, METHOD_MEMORY_SEARCH,
    METHOD_POSTPROCESS_RETRY, METHOD_POSTPROCESS_STATUS, METHOD_TURN_RESULT,
};

fn keys(value: &Value) -> Vec<String> {
    value
        .as_object()
        .expect("object")
        .keys()
        .cloned()
        .collect::<Vec<_>>()
}

#[test]
fn conversation_send_forwards_exactly_the_five_allowed_keys() {
    let value = conversation_send_params("s", "c", "r", "text", Some("d"));
    assert_eq!(
        keys(&value),
        vec![
            "client_request_id",
            "confirmed_destination_id",
            "conversation_id",
            "session_id",
            "text",
        ]
    );
    for forbidden in [
        "namespace",
        "host_instance",
        "interaction_id",
        "context_id",
        "retention",
        "privacy_class",
        "purposes",
        "authorization_ref",
        "origin_kind",
        "mode",
    ] {
        assert!(!value.as_object().unwrap().contains_key(forbidden));
    }
}

#[test]
fn conversation_read_helpers_forward_exact_keys() {
    assert_eq!(
        keys(&conversation_create_params("s", "chat")),
        vec!["mode", "session_id"]
    );
    assert_eq!(
        keys(&conversation_list_params("s", Some(10), Some("cur"))),
        vec!["cursor", "limit", "session_id"]
    );
    assert_eq!(
        keys(&conversation_get_params("s", "c", Some(2), Some(1), Some(10))),
        vec![
            "before_seq",
            "before_turn_no",
            "conversation_id",
            "limit",
            "session_id",
        ]
    );
    assert_eq!(
        keys(&turn_result_params("s", "t")),
        vec!["session_id", "turn_id"]
    );
}

#[test]
fn transcript_and_postprocess_helpers_forward_exact_keys() {
    assert_eq!(
        keys(&conversation_search_params(
            "s",
            "query",
            Some(5),
            Some("c"),
            Some("cursor"),
            Some(true)
        )),
        vec![
            "before",
            "conversation_id",
            "include_full",
            "limit",
            "query",
            "session_id",
        ]
    );
    assert_eq!(
        keys(&postprocess_status_params(
            "s",
            "c",
            Some(&["t1".to_string(), "t2".to_string()])
        )),
        vec!["conversation_id", "session_id", "turn_ids"]
    );
    assert_eq!(
        keys(&postprocess_retry_params("s", "t")),
        vec!["session_id", "turn_id"]
    );
    assert_eq!("conversation.search", METHOD_CONVERSATION_SEARCH);
    assert_eq!("postprocess.status", METHOD_POSTPROCESS_STATUS);
    assert_eq!("postprocess.retry", METHOD_POSTPROCESS_RETRY);
}

#[test]
fn memory_search_helper_forwards_only_query_limit_and_cursor() {
    assert_eq!(
        keys(&memory_search_params("s", "蓝鲸", Some(10), Some("cursor"))),
        vec!["cursor", "limit", "query", "session_id"]
    );
    assert_eq!(
        keys(&memory_search_params("s", "", None, None)),
        vec!["cursor", "limit", "query", "session_id"]
    );
    assert_eq!("memory.search", METHOD_MEMORY_SEARCH);
    assert!(MEMORY_SEARCH_TIMEOUT < CONVERSATION_READ_TIMEOUT);
}

#[test]
fn conversation_command_timeouts_are_explicit() {
    // A send returns after T1; reads are insurance against a non-turn job.
    assert_eq!(CONVERSATION_SEND_TIMEOUT, Duration::from_secs(120));
    assert_eq!(CONVERSATION_READ_TIMEOUT, Duration::from_secs(300));
    assert!(CONVERSATION_READ_TIMEOUT > CONVERSATION_SEND_TIMEOUT);
}

#[test]
fn conversation_bridge_methods_are_the_five_registered_names() {
    assert_eq!(METHOD_CONVERSATION_CREATE, "conversation.create");
    assert_eq!(METHOD_CONVERSATION_LIST, "conversation.list");
    assert_eq!(METHOD_CONVERSATION_GET, "conversation.get");
    assert_eq!(METHOD_CONVERSATION_SEND, "conversation.send");
    assert_eq!(METHOD_TURN_RESULT, "turn.result");
}
