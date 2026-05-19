#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Deserialize;
use serde_json::{json, Value};
use std::env;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};
use time::{format_description::well_known::Rfc3339, OffsetDateTime};

#[derive(Debug, Deserialize)]
struct AdminCommandInput {
    issuer: String,
    command: String,
    target_type: String,
    target_id: Option<String>,
    reason: String,
    trace_id: Option<String>,
    payload: Option<Value>,
    requires_ack: Option<bool>,
    risk_override: Option<String>,
    approval_context: Option<Value>,
}

fn repo_root() -> PathBuf {
    if let Ok(raw) = env::var("AGN_REPO_ROOT") {
        let path = PathBuf::from(raw);
        if path.exists() {
            return path;
        }
    }
    let mut current = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    loop {
        if current.join(".git").exists() {
            return current;
        }
        if !current.pop() {
            break;
        }
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn read_model_path(name: &str) -> PathBuf {
    repo_root()
        .join("runtime")
        .join("admin_control")
        .join("read_models")
        .join(format!("{name}.json"))
}

fn command_pending_dir() -> PathBuf {
    repo_root()
        .join("runtime")
        .join("admin_control")
        .join("commands")
        .join("pending")
}

fn now_iso() -> String {
    OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .unwrap_or_else(|_| "1970-01-01T00:00:00Z".to_string())
}

fn next_command_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or(0);
    format!("cmd-ui-{nanos}")
}

fn atomic_write_json(path: &Path, payload: &Value) -> Result<(), String> {
    let parent = path.parent().ok_or_else(|| "missing_parent".to_string())?;
    fs::create_dir_all(parent).map_err(|err| format!("mkdir_failed:{err}"))?;
    let tmp_name = format!(
        ".{}.{}.tmp",
        path.file_name().and_then(|name| name.to_str()).unwrap_or("payload"),
        next_command_id()
    );
    let tmp_path = parent.join(tmp_name);
    let mut file = fs::File::create(&tmp_path).map_err(|err| format!("create_tmp_failed:{err}"))?;
    let text = serde_json::to_string_pretty(payload).map_err(|err| format!("json_encode_failed:{err}"))?;
    file.write_all(text.as_bytes())
        .and_then(|_| file.write_all(b"\n"))
        .and_then(|_| file.flush())
        .map_err(|err| format!("write_tmp_failed:{err}"))?;
    fs::rename(&tmp_path, path).map_err(|err| format!("rename_failed:{err}"))?;
    Ok(())
}

fn read_json(path: &Path) -> Result<Value, String> {
    let raw = fs::read_to_string(path).map_err(|err| format!("read_failed:{err}"))?;
    serde_json::from_str::<Value>(&raw).map_err(|err| format!("json_decode_failed:{err}"))
}

#[tauri::command]
fn load_read_model(name: String) -> Result<Value, String> {
    let path = read_model_path(name.as_str());
    read_json(&path)
}

#[tauri::command]
fn refresh_read_models() -> Result<Value, String> {
    let output = Command::new("python3")
        .arg("scripts/control_plane_read_model.py")
        .arg("refresh")
        .current_dir(repo_root())
        .output()
        .map_err(|err| format!("spawn_failed:{err}"))?;
    if !output.status.success() {
        return Err(format!(
            "refresh_failed:{}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str::<Value>(stdout.trim()).map_err(|err| format!("refresh_json_failed:{err}"))
}

#[tauri::command]
fn submit_admin_command(input: AdminCommandInput) -> Result<Value, String> {
    let command_id = next_command_id();
    let payload = json!({
        "command_id": command_id,
        "timestamp": now_iso(),
        "issuer": input.issuer,
        "command": input.command,
        "target_type": input.target_type,
        "target_id": input.target_id.unwrap_or_default(),
        "reason": input.reason,
        "trace_id": input.trace_id.unwrap_or_default(),
        "payload": input.payload.unwrap_or_else(|| json!({})),
        "requires_ack": input.requires_ack.unwrap_or(true),
        "risk_override": input.risk_override.unwrap_or_else(|| "none".to_string()),
        "approval_context": input.approval_context.unwrap_or_else(|| json!({}))
    });
    let target = command_pending_dir().join(format!("{command_id}.json"));
    atomic_write_json(&target, &payload)?;
    Ok(json!({
        "ok": true,
        "command_id": command_id,
        "path": target,
    }))
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            load_read_model,
            refresh_read_models,
            submit_admin_command
        ])
        .run(tauri::generate_context!())
        .expect("error while running AGN2.0 control plane");
}
