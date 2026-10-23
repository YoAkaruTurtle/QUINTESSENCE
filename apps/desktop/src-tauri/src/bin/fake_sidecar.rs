//! Test child used by `src-tauri/tests/host.rs`; never bundled with the app.

use std::io::{Read, Write};
use std::thread;
use std::time::Duration;

use serde_json::{json, Value};

fn read_frame<R: Read>(reader: &mut R) -> Option<Vec<u8>> {
    let mut header = [0u8; 4];
    if reader.read_exact(&mut header).is_err() {
        return None;
    }
    let length = u32::from_be_bytes(header) as usize;
    let mut body = vec![0u8; length];
    if reader.read_exact(&mut body).is_err() {
        return None;
    }
    Some(body)
}

fn encode(value: &Value) -> Vec<u8> {
    let body = serde_json::to_vec(value).unwrap_or_default();
    let mut frame = Vec::with_capacity(4 + body.len());
    frame.extend_from_slice(&(body.len() as u32).to_be_bytes());
    frame.extend_from_slice(&body);
    frame
}

fn reply_for(body: &[u8]) -> Vec<u8> {
    let request: Value = serde_json::from_slice(body).unwrap_or(Value::Null);
    let id = request.get("id").cloned().unwrap_or(Value::Null);
    encode(&json!({
        "v": "desktop.v1",
        "id": id,
        "ok": true,
        "result": {"pong": true, "mode": "fake"},
    }))
}

fn main() {
    let mut mode = "ok".to_string();
    let mut arguments = std::env::args().skip(1);
    while let Some(argument) = arguments.next() {
        if argument == "--mode" {
            if let Some(value) = arguments.next() {
                mode = value;
            }
        }
    }
    let stdin = std::io::stdin();
    let mut input = stdin.lock();
    let stdout = std::io::stdout();
    let mut output = stdout.lock();

    match mode.as_str() {
        "hang" => {
            let _ = read_frame(&mut input);
            thread::sleep(Duration::from_secs(120));
        }
        "crash" => {
            let _ = read_frame(&mut input);
            std::process::exit(3);
        }
        "two" => {
            let first = read_frame(&mut input).expect("first frame");
            let second = read_frame(&mut input).expect("second frame");
            let response = [reply_for(&first), reply_for(&second)].concat();
            let _ = output.write_all(&response);
            let _ = output.flush();
            thread::sleep(Duration::from_millis(100));
        }
        "chunked" => {
            let request = read_frame(&mut input).expect("frame");
            let frame = reply_for(&request);
            let _ = output.write_all(&frame[..1]);
            let _ = output.flush();
            thread::sleep(Duration::from_millis(30));
            let _ = output.write_all(&frame[1..3]);
            let _ = output.flush();
            thread::sleep(Duration::from_millis(30));
            let _ = output.write_all(&frame[3..]);
            let _ = output.flush();
            thread::sleep(Duration::from_millis(100));
        }
        "bad" => {
            let _ = read_frame(&mut input);
            let _ = output.write_all(&[0u8, 0, 0, 0]);
            let _ = output.flush();
            thread::sleep(Duration::from_secs(30));
        }
        _ => {
            while let Some(request) = read_frame(&mut input) {
                let _ = output.write_all(&reply_for(&request));
                let _ = output.flush();
            }
        }
    }
}
