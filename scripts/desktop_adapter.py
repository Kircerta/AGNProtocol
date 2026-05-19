#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
import subprocess
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

try:
    from agn.core.desktop_provider import get_desktop_control_bin
    GUI_AGENT_BIN = get_desktop_control_bin()
except ImportError:  # pragma: no cover — fallback if package not on path
    GUI_AGENT_BIN = Path(str(Path.home() / ".codex" / "bin" / "gui-agent"))

DESKTOP_LOG_DIR = ROOT / "runtime" / "desktop_actions"
WRITE_ACTION_TYPES = {"TERMINAL_SPAWN", "TERMINAL_INPUT", "TERMINAL_SEND_KEY"}

try:
    from agn.core.emergency_stop import desktop_mode as governance_desktop_mode
except ImportError:  # pragma: no cover
    from emergency_stop import desktop_mode as governance_desktop_mode


def _parse_json_output(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except Exception:
        return None
    return decoded if isinstance(decoded, dict) else None


def _safe_log_path(trace_id: str) -> Path:
    DESKTOP_LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in str(trace_id or "desktop").strip())
    return DESKTOP_LOG_DIR / f"{safe or 'desktop'}.jsonl"


def _build_common_cmd(trace_id: str) -> list[str]:
    return [str(GUI_AGENT_BIN), "--log-file", str(_safe_log_path(trace_id))]


def _bool(value: Any) -> bool:
    return bool(value)


def _local_status_payload(trace_id: str) -> dict[str, Any]:
    return {
        "ok": True,
        "action_type": "DESKTOP_OBSERVE",
        "trace_id": trace_id,
        "write_capable": False,
        "executed": False,
        "command": [],
        "stdout": {
            "ok": True,
            "surface": "status",
            "gui_agent_exists": GUI_AGENT_BIN.exists(),
            "gui_agent_path": str(GUI_AGENT_BIN),
            "desktop_mode": governance_desktop_mode(),
        },
        "stderr": "",
        "return_code": 0,
        "log_file": str(_safe_log_path(trace_id)),
        "audit_refs": [],
    }


def _desktop_observe(params: dict[str, Any], trace_id: str) -> tuple[list[str], bool]:
    surface = str(params.get("surface", "")).strip().lower()
    cmd = _build_common_cmd(trace_id)
    if surface == "status":
        cmd.extend(["status"])
    elif surface == "frontmost":
        cmd.extend(["frontmost"])
    elif surface == "mouse_position":
        cmd.extend(["mouse-pos"])
    elif surface == "screenshot":
        cmd.extend(["screenshot"])
        if str(params.get("path", "")).strip():
            cmd.extend(["--path", str(params["path"]).strip()])
        if str(params.get("app", "")).strip():
            cmd.extend(["--app", str(params["app"]).strip()])
        if str(params.get("window_name", "")).strip():
            cmd.extend(["--window-name", str(params["window_name"]).strip()])
        if _bool(params.get("active_window", False)):
            cmd.append("--active-window")
        if str(params.get("region", "")).strip():
            cmd.extend(["--region", str(params["region"]).strip()])
    elif surface == "ghostty_status":
        cmd.extend(["ghostty", "status"])
    elif surface == "ghostty_windows":
        cmd.extend(["ghostty", "windows"])
    elif surface == "ghostty_tabs":
        cmd.extend(["ghostty", "tabs"])
        if str(params.get("window_id", "")).strip():
            cmd.extend(["--window-id", str(params["window_id"]).strip()])
    elif surface == "ghostty_terminals":
        cmd.extend(["ghostty", "terminals"])
    else:
        raise ValueError(f"unsupported_desktop_observe_surface:{surface}")
    return cmd, False


def _desktop_focus(params: dict[str, Any], trace_id: str) -> tuple[list[str], bool]:
    surface = str(params.get("surface", "")).strip().lower()
    cmd = _build_common_cmd(trace_id)
    if surface == "activate_app":
        app = str(params.get("app", "")).strip()
        if not app:
            raise ValueError("missing:app")
        cmd.extend(["activate", "--app", app])
    elif surface == "ghostty_focus":
        cmd.extend(["ghostty", "focus"])
        if str(params.get("window_id", "")).strip():
            cmd.extend(["--window-id", str(params["window_id"]).strip()])
        if str(params.get("tab_id", "")).strip():
            cmd.extend(["--tab-id", str(params["tab_id"]).strip()])
        if str(params.get("terminal_id", "")).strip():
            cmd.extend(["--terminal-id", str(params["terminal_id"]).strip()])
    else:
        raise ValueError(f"unsupported_desktop_focus_surface:{surface}")
    return cmd, False


def _apply_spawn_options(cmd: list[str], params: dict[str, Any]) -> None:
    if str(params.get("cwd", "")).strip():
        cmd.extend(["--cwd", str(params["cwd"]).strip()])
    if str(params.get("command", "")).strip():
        cmd.extend(["--command", str(params["command"]).strip()])
    if str(params.get("input", "")).strip():
        cmd.extend(["--input", str(params["input"]).strip()])
    env_entries = params.get("env", [])
    if isinstance(env_entries, dict):
        env_entries = [f"{key}={value}" for key, value in env_entries.items()]
    if isinstance(env_entries, list):
        for item in env_entries:
            text = str(item).strip()
            if text:
                cmd.extend(["--env", text])
    if str(params.get("font_size", "")).strip():
        cmd.extend(["--font-size", str(params["font_size"]).strip()])
    if _bool(params.get("wait_after_command", False)):
        cmd.append("--wait-after-command")


def _terminal_spawn(params: dict[str, Any], trace_id: str, allow_execute: bool) -> tuple[list[str], bool]:
    mode = str(params.get("mode", "")).strip().lower()
    cmd = _build_common_cmd(trace_id)
    if mode == "new_window":
        cmd.extend(["ghostty", "new-window"])
    elif mode == "new_tab":
        cmd.extend(["ghostty", "new-tab"])
        if str(params.get("window_id", "")).strip():
            cmd.extend(["--window-id", str(params["window_id"]).strip()])
    elif mode == "split":
        direction = str(params.get("direction", "")).strip().lower()
        if direction not in {"right", "left", "down", "up"}:
            raise ValueError("invalid:direction")
        cmd.extend(["ghostty", "split"])
        if str(params.get("terminal_id", "")).strip():
            cmd.extend(["--terminal-id", str(params["terminal_id"]).strip()])
        cmd.append(direction)
    else:
        raise ValueError(f"unsupported_terminal_spawn_mode:{mode}")
    _apply_spawn_options(cmd, params)
    cmd.append("--execute" if allow_execute else "--dry-run")
    return cmd, True


def _terminal_input(params: dict[str, Any], trace_id: str, allow_execute: bool) -> tuple[list[str], bool]:
    text = str(params.get("text", "")).strip()
    if not text:
        raise ValueError("missing:text")
    cmd = _build_common_cmd(trace_id)
    cmd.extend(["ghostty", "input"])
    if str(params.get("terminal_id", "")).strip():
        cmd.extend(["--terminal-id", str(params["terminal_id"]).strip()])
    if _bool(params.get("enter", False)):
        cmd.append("--enter")
    cmd.append("--execute" if allow_execute else "--dry-run")
    cmd.append(text)
    return cmd, True


def _terminal_send_key(params: dict[str, Any], trace_id: str, allow_execute: bool) -> tuple[list[str], bool]:
    key = str(params.get("key", "")).strip()
    if not key:
        raise ValueError("missing:key")
    cmd = _build_common_cmd(trace_id)
    cmd.extend(["ghostty", "send-key"])
    if str(params.get("terminal_id", "")).strip():
        cmd.extend(["--terminal-id", str(params["terminal_id"]).strip()])
    if str(params.get("modifiers", "")).strip():
        cmd.extend(["--modifiers", str(params["modifiers"]).strip()])
    if str(params.get("action", "")).strip():
        cmd.extend(["--action", str(params["action"]).strip()])
    cmd.append("--execute" if allow_execute else "--dry-run")
    cmd.append(key)
    return cmd, True


def build_desktop_command(action: dict[str, Any]) -> tuple[list[str], bool]:
    if not GUI_AGENT_BIN.exists():
        raise ValueError(f"adapter_unavailable:{GUI_AGENT_BIN}")
    action_type = str(action.get("action_type", "")).strip().upper()
    trace_id = str(action.get("trace_id", "")).strip() or "desktop-trace"
    params = action.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("invalid:params")
    allow_execute = bool(action.get("allow_execute", False))
    if action_type == "DESKTOP_OBSERVE":
        return _desktop_observe(params, trace_id)
    if action_type == "DESKTOP_FOCUS":
        return _desktop_focus(params, trace_id)
    if action_type == "TERMINAL_SPAWN":
        return _terminal_spawn(params, trace_id, allow_execute)
    if action_type == "TERMINAL_INPUT":
        return _terminal_input(params, trace_id, allow_execute)
    if action_type == "TERMINAL_SEND_KEY":
        return _terminal_send_key(params, trace_id, allow_execute)
    raise ValueError(f"unsupported_action_type:{action_type}")


def run_desktop_action(action: dict[str, Any]) -> dict[str, Any]:
    action_type = str(action.get("action_type", "")).strip().upper()
    trace_id = str(action.get("trace_id", "")).strip() or "desktop-trace"
    params = action.get("params", {})
    if not isinstance(params, dict):
        return {"ok": False, "failure_class": "schema_invalid", "error": "invalid:params"}
    if action_type == "DESKTOP_OBSERVE" and str(params.get("surface", "")).strip().lower() == "status":
        return _local_status_payload(trace_id)

    try:
        cmd, write_capable = build_desktop_command(action)
    except ValueError as exc:
        return {"ok": False, "failure_class": "schema_invalid", "error": str(exc)}

    allow_execute = bool(action.get("allow_execute", False))
    approval_context = action.get("approval_context", {})
    if not isinstance(approval_context, dict):
        approval_context = {}
    audit_refs = action.get("audit_refs", [])
    if not isinstance(audit_refs, list):
        audit_refs = []
    if governance_desktop_mode() == "observe_only" and action_type != "DESKTOP_OBSERVE":
        return {
            "ok": False,
            "action_type": action_type,
            "trace_id": trace_id,
            "write_capable": write_capable,
            "executed": False,
            "failure_class": "emergency_stop_active",
            "error": "desktop adapter is in observe-only mode",
            "command": cmd,
            "log_file": str(_safe_log_path(trace_id)),
        }
    if write_capable and (not allow_execute or not audit_refs):
        return {
            "ok": False,
            "action_type": action_type,
            "trace_id": trace_id,
            "write_capable": True,
            "executed": False,
            "failure_class": "unsafe_action_blocked",
            "error": "write actions require allow_execute=true and non-empty audit_refs",
            "command": cmd,
            "log_file": str(_safe_log_path(trace_id)),
        }
    if write_capable and str(approval_context.get("decision", "")).strip().lower() != "approved":
        return {
            "ok": False,
            "action_type": action_type,
            "trace_id": trace_id,
            "write_capable": True,
            "executed": False,
            "failure_class": "unsafe_action_blocked",
            "error": "write actions require explicit policy gate approval",
            "command": cmd,
            "log_file": str(_safe_log_path(trace_id)),
        }

    completed = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=max(5.0, float(action.get("timeout_sec", 30.0) or 30.0)),
        check=False,
    )
    parsed = _parse_json_output(completed.stdout or "")
    ok = completed.returncode == 0 and (parsed is None or bool(parsed.get("ok", True)))
    return {
        "ok": ok,
        "action_type": action_type,
        "trace_id": trace_id,
        "write_capable": write_capable,
        "executed": bool(allow_execute) if write_capable else completed.returncode == 0,
        "command": cmd,
        "stdout": parsed if parsed is not None else str(completed.stdout or "").strip(),
        "stderr": str(completed.stderr or "").strip(),
        "return_code": int(completed.returncode),
        "log_file": str(_safe_log_path(trace_id)),
        "audit_refs": [str(item).strip() for item in audit_refs if str(item).strip()],
    }


def main() -> int:
    """CLI entry point for desktop_adapter.

    Supports:
      desktop_adapter.py status                — Check adapter availability and mode
      desktop_adapter.py observe <surface>     — Read-only desktop observation
      desktop_adapter.py run --from-json <file> — Execute an action from a JSON file
      desktop_adapter.py provider-info         — Show desktop control provider details
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="desktop_adapter",
        description="AGN desktop adapter — governed desktop observation and action.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Check adapter availability and desktop mode")
    sub.add_parser("provider-info", help="Show desktop control binary provider details")

    obs = sub.add_parser("observe", help="Read-only desktop observation")
    obs.add_argument("surface", choices=["status", "frontmost", "mouse_position", "screenshot",
                                          "ghostty_status", "ghostty_windows", "ghostty_tabs",
                                          "ghostty_terminals"],
                     help="Observation surface")
    obs.add_argument("--trace-id", default="cli-desktop-observe")

    run_p = sub.add_parser("run", help="Execute an action from a JSON file")
    run_p.add_argument("--from-json", required=True, dest="json_file",
                       help="Path to JSON action file")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0

    import json as _json

    if args.command == "status":
        result = run_desktop_action({
            "action_type": "DESKTOP_OBSERVE",
            "trace_id": "cli-status",
            "params": {"surface": "status"},
        })
        print(_json.dumps(result, indent=2, ensure_ascii=True))
        return 0 if result.get("ok") else 1

    if args.command == "provider-info":
        try:
            from agn.core.desktop_provider import get_provider_info
            info = get_provider_info()
        except ImportError:
            info = {"binary": str(GUI_AGENT_BIN), "exists": GUI_AGENT_BIN.exists(), "source": "legacy"}
        print(_json.dumps(info, indent=2, ensure_ascii=True))
        return 0

    if args.command == "observe":
        result = run_desktop_action({
            "action_type": "DESKTOP_OBSERVE",
            "trace_id": args.trace_id,
            "params": {"surface": args.surface},
        })
        print(_json.dumps(result, indent=2, ensure_ascii=True))
        return 0 if result.get("ok") else 1

    if args.command == "run":
        action = _json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        result = run_desktop_action(action)
        print(_json.dumps(result, indent=2, ensure_ascii=True))
        return 0 if result.get("ok") else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
