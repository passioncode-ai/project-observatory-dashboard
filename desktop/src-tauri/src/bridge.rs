//! The engine bridge: one bounded call of `<program> full assistant <action>` with a JSON object on
//! stdin and one JSON answer on stdout (protocol `observatory-assistant/1`, docs/macos/SPEC.md
//! "Runtime and state machine"). The same rules as the macOS app's Bridge.swift: an absolute
//! program path, an argv array and no shell, the workspace through OBSERVATORY_HOME, bounded output,
//! a timeout that stops everything the call started, typed error codes and never a raw provider text.

use serde_json::Value;
use std::io::{Read, Write};
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

pub const PROTOCOL: &str = "observatory-assistant/1";
const MAX_OUTPUT: usize = 4 * 1024 * 1024;
const ACTIONS: &[&str] = &["status", "ask", "get", "list", "job", "cancel", "delete", "dashboard", "serve", "build"];

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct BridgeError {
    /// `backend-missing`, `backend-configuration`, `backend-timeout`, `backend-response-too-large`,
    /// `backend-invalid-response`, `backend-incompatible`, `backend-failed`, or the engine's own code.
    pub code: String,
    pub detail: Option<String>,
}

impl BridgeError {
    fn new(code: &str, detail: Option<String>) -> Self {
        BridgeError { code: code.to_string(), detail: detail.filter(|d| !d.is_empty()) }
    }
}

pub struct Backend {
    pub program: String,
    pub workspace: String,
}

/// An engine that answers without this protocol is an older release, not a broken one.
fn looks_incompatible(first_line: &str) -> bool {
    let l = first_line.to_lowercase();
    l.contains("unknown step") || l.contains("invalid choice") || l.contains("unrecognized arguments")
}

impl Backend {
    pub fn call(&self, action: &str, input: &Value, timeout: Duration) -> Result<Value, BridgeError> {
        if !ACTIONS.contains(&action) || !Path::new(&self.program).is_absolute() || !Path::new(&self.workspace).is_absolute() {
            return Err(BridgeError::new("backend-configuration", None));
        }
        if !Path::new(&self.program).is_file() {
            return Err(BridgeError::new("backend-missing", Some(self.program.clone())));
        }
        let mut command = Command::new(&self.program);
        command
            .args(["full", "assistant", action])
            .env("OBSERVATORY_HOME", &self.workspace)
            .env("PYTHONUTF8", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        own_group(&mut command);
        let mut child = command.spawn().map_err(|e| BridgeError::new("backend-failed", Some(e.to_string())))?;
        if let Some(mut stdin) = child.stdin.take() {
            // A child that exited before reading is EPIPE here, never a signal (Rust ignores SIGPIPE).
            let _ = stdin.write_all(input.to_string().as_bytes());
        }
        let stdout = child.stdout.take().expect("piped stdout");
        let stderr = child.stderr.take().expect("piped stderr");
        let (out_tx, out_rx) = mpsc::channel();
        let (err_tx, err_rx) = mpsc::channel();
        thread::spawn(move || out_tx.send(read_bounded(stdout)).ok());
        thread::spawn(move || err_tx.send(read_bounded(stderr)).ok());
        let deadline = Instant::now() + timeout;
        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => break status,
                Ok(None) if Instant::now() >= deadline => {
                    stop_tree(child.id());
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(BridgeError::new("backend-timeout", None));
                }
                Ok(None) => thread::sleep(Duration::from_millis(40)),
                Err(e) => return Err(BridgeError::new("backend-failed", Some(e.to_string()))),
            }
        };
        let (stdout, too_large) = out_rx.recv_timeout(Duration::from_secs(5)).unwrap_or((Vec::new(), false));
        let (stderr, _) = err_rx.recv_timeout(Duration::from_secs(5)).unwrap_or((Vec::new(), false));
        if too_large {
            return Err(BridgeError::new("backend-response-too-large", None));
        }
        let first_line: String = String::from_utf8_lossy(&stderr).lines().next().unwrap_or("").chars().take(240).collect();
        let doc: Value = match serde_json::from_slice(&stdout) {
            Ok(Value::Object(map)) => Value::Object(map),
            _ => {
                if !status.success() && looks_incompatible(&first_line) {
                    return Err(BridgeError::new("backend-incompatible", Some(first_line)));
                }
                if !status.success() {
                    return Err(BridgeError::new("backend-failed", Some(first_line)));
                }
                return Err(BridgeError::new("backend-invalid-response", None));
            }
        };
        if let Some(code) = doc.get("error").and_then(Value::as_str) {
            return Err(BridgeError::new(code, None));
        }
        if !status.success() {
            return Err(BridgeError::new("backend-failed", Some(first_line)));
        }
        if action == "status" && doc.get("protocol").and_then(Value::as_str) != Some(PROTOCOL) {
            let got = doc.get("protocol").and_then(Value::as_str).unwrap_or("missing");
            return Err(BridgeError::new("backend-incompatible", Some(format!("protocol {got}, expected {PROTOCOL}"))));
        }
        Ok(doc)
    }
}

fn read_bounded(mut source: impl Read) -> (Vec<u8>, bool) {
    let mut out = Vec::new();
    let mut buf = [0u8; 65536];
    let mut too_large = false;
    loop {
        match source.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                if out.len() + n > MAX_OUTPUT {
                    too_large = true; // keep draining so the child is never blocked on a full pipe
                } else {
                    out.extend_from_slice(&buf[..n]);
                }
            }
        }
    }
    (out, too_large)
}

/// The call runs in a process group of its own, so a timeout stops what the engine started with it.
#[cfg(unix)]
fn own_group(command: &mut Command) {
    use std::os::unix::process::CommandExt;
    command.process_group(0);
}

/// No console window, and a group of its own (CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP).
#[cfg(windows)]
fn own_group(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    command.creation_flags(0x0800_0000 | 0x0000_0200);
}

#[cfg(unix)]
fn stop_tree(pid: u32) {
    // SAFETY: a plain kill(2) on the group this call created; a stale id only fails with ESRCH.
    unsafe {
        libc::kill(-(pid as i32), libc::SIGKILL);
    }
}

#[cfg(windows)]
fn stop_tree(pid: u32) {
    use std::os::windows::process::CommandExt;
    let _ = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .creation_flags(0x0800_0000)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

#[cfg(test)]
mod tests {
    use super::*;

    fn backend(program: &str) -> Backend {
        let workspace = std::env::temp_dir().to_string_lossy().to_string();
        Backend { program: program.to_string(), workspace }
    }

    #[test]
    fn a_missing_program_is_named_missing_not_misconfigured() {
        let missing = if cfg!(windows) { "C:\\nowhere\\project-observatory.exe" } else { "/nowhere/project-observatory" };
        let err = backend(missing).call("status", &serde_json::json!({}), Duration::from_secs(2)).unwrap_err();
        assert_eq!(err.code, "backend-missing");
        assert_eq!(err.detail.as_deref(), Some(missing));
    }

    #[test]
    fn a_relative_program_or_an_unknown_action_is_refused() {
        assert_eq!(backend("project-observatory").call("status", &serde_json::json!({}), Duration::from_secs(1)).unwrap_err().code,
                   "backend-configuration");
        let any = std::env::current_exe().unwrap().to_string_lossy().to_string();
        assert_eq!(backend(&any).call("rm", &serde_json::json!({}), Duration::from_secs(1)).unwrap_err().code,
                   "backend-configuration");
    }

    #[test]
    fn an_older_engine_is_incompatible_not_broken() {
        assert!(looks_incompatible("observatory.py: error: argument step: invalid choice: 'assistant'"));
        assert!(looks_incompatible("unknown step: assistant"));
        assert!(!looks_incompatible("Traceback (most recent call last):"));
    }

    /// A stand-in engine: answers per action the way the real one does, and hangs on `ask`.
    #[cfg(unix)]
    fn fake_engine() -> String {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("obs-fake-{}-{}", std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("project-observatory");
        std::fs::write(&path, r#"#!/bin/sh
cat > /dev/null
case "$3" in
  status) printf '{"protocol":"observatory-assistant/1","workspace":"%s","project_count":2}' "$OBSERVATORY_HOME" ;;
  dashboard) printf '{"error":"unknown-workspace"}' ;;
  ask) sleep 30 & wait ;;
  list) echo "unknown step: assistant" >&2; exit 2 ;;
  *) printf 'not json' ;;
esac
"#).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path.to_string_lossy().to_string()
    }

    #[cfg(unix)]
    #[test]
    fn a_real_call_carries_the_workspace_and_maps_every_outcome() {
        let b = backend(&fake_engine());
        let status = b.call("status", &serde_json::json!({}), Duration::from_secs(10)).unwrap();
        assert_eq!(status["workspace"].as_str().unwrap(), b.workspace);
        assert_eq!(b.call("dashboard", &serde_json::json!({}), Duration::from_secs(10)).unwrap_err().code, "unknown-workspace");
        assert_eq!(b.call("list", &serde_json::json!({}), Duration::from_secs(10)).unwrap_err().code, "backend-incompatible");
        assert_eq!(b.call("get", &serde_json::json!({}), Duration::from_secs(10)).unwrap_err().code, "backend-invalid-response");
    }

    #[cfg(unix)]
    #[test]
    fn a_timeout_stops_what_the_call_started() {
        let b = backend(&fake_engine());
        let started = Instant::now();
        assert_eq!(b.call("ask", &serde_json::json!({}), Duration::from_millis(500)).unwrap_err().code, "backend-timeout");
        assert!(started.elapsed() < Duration::from_secs(5), "the call waited for its child's child");
    }

    #[test]
    fn output_past_the_bound_is_reported_not_truncated_silently() {
        let big = vec![b'x'; MAX_OUTPUT + 10];
        let (kept, too_large) = read_bounded(&big[..]);
        assert!(too_large);
        assert!(kept.len() <= MAX_OUTPUT);
    }
}
