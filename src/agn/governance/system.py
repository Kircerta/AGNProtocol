"""AGN lifecycle entry.

This is the real package implementation for AGN's top-level lifecycle surface.
The legacy script remains only as a CLI compatibility shim.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn.governance.read_models import refresh_read_models

from agn.core.admin_control import (
    agn2_root,
    append_admin_audit,
    atomic_write_json,
    control_plane_root,
    lifecycle_state_path,
    load_json,
    repo_root,
)
from agn.core.emergency_stop import initialize_system_mode, load_system_mode
from agn.governance.commands import submit_admin_command
from agn.governance.control_daemon import run_once as control_daemon_run_once
from capability_snapshot import build_capability_snapshot
from provider_registry import CAPABILITIES_PATH, atomic_write_json as write_registry_json, load_registry, probe_capabilities

from agn.dispatch.bus import expire_messages


PACKAGE_PATH = "agn.governance.system"
LEGACY_SCRIPT_SHIM = "scripts/agn2_system.py"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _lifecycle_state() -> dict[str, Any]:
    return load_json(
        lifecycle_state_path(),
        default={
            "system_id": "AGN2.0",
            "status": "stopped",
            "started_at": "",
            "last_refresh_at": "",
            "last_command": "",
            "last_reason": "",
            "control_plane_root": "agn2/control_plane",
            "governance_root": "agn2/governance",
        },
    )


def _write_lifecycle(payload: dict[str, Any]) -> dict[str, Any]:
    atomic_write_json(lifecycle_state_path(), payload)
    return payload


def _update_lifecycle(*, status: str, command: str, reason: str) -> dict[str, Any]:
    current = _lifecycle_state()
    now = utc_now_iso()
    payload = {
        **current,
        "system_id": "AGN2.0",
        "status": status,
        "started_at": (current.get("started_at") or now) if status == "running" else current.get("started_at", ""),
        "last_refresh_at": now,
        "last_command": command,
        "last_reason": reason,
        "control_plane_root": "agn2/control_plane",
        "governance_root": "agn2/governance",
        "manifest": "agn2/system_manifest.json",
    }
    append_admin_audit("agn2_lifecycle_updated", status=status, command=command, reason=reason)
    return _write_lifecycle(payload)


def _probe_and_write_capabilities() -> dict[str, Any]:
    capabilities = probe_capabilities(load_registry())
    write_registry_json(CAPABILITIES_PATH, capabilities)
    return capabilities


def _control_plane_status() -> dict[str, Any]:
    cargo_tauri = shutil.which("cargo-tauri")
    return {
        "root": str(control_plane_root()),
        "tauri_cli_available": bool(cargo_tauri),
        "tauri_cli": cargo_tauri or "",
        "cargo_toml_exists": (control_plane_root() / "src-tauri" / "Cargo.toml").exists(),
    }


def refresh_system(*, run_daemon: bool = True, expire_bus_messages: bool = True) -> dict[str, Any]:
    system_mode = initialize_system_mode(issuer="agn2_system", reason="agn2 refresh bootstrap")
    capabilities = _probe_and_write_capabilities()
    capability_snapshot = build_capability_snapshot()
    expired = expire_messages() if expire_bus_messages else []
    daemon = control_daemon_run_once(max_commands=20) if run_daemon else {"ok": True, "processed": 0, "acks": []}
    read_models = refresh_read_models()
    lifecycle = _update_lifecycle(status="running", command="refresh", reason="agn2 refresh")
    return {
        "ok": True,
        "capabilities_generated": bool(capabilities),
        "capability_snapshot": capability_snapshot,
        "expired_bus_messages": len(expired),
        "control_daemon": daemon,
        "read_models": read_models,
        "lifecycle": lifecycle,
        "system_mode": system_mode,
        "control_plane": _control_plane_status(),
    }


def cmd_start(_args: argparse.Namespace) -> int:
    payload = refresh_system(run_daemon=True, expire_bus_messages=True)
    payload["lifecycle"] = _update_lifecycle(status="running", command="start", reason="agn2 start")
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_refresh(_args: argparse.Namespace) -> int:
    payload = refresh_system(run_daemon=True, expire_bus_messages=True)
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    overview_path = repo_root() / "runtime" / "admin_control" / "read_models" / "overview.json"
    approval_path = repo_root() / "runtime" / "admin_control" / "read_models" / "approval_gate.json"
    capability_path = repo_root() / "runtime" / "admin_control" / "read_models" / "capability_snapshot.json"
    discipline_path = repo_root() / "runtime" / "admin_control" / "read_models" / "execution_discipline.json"
    payload = {
        "ok": True,
        "system_id": "AGN2.0",
        "identity_doc": str((agn2_root() / "SYSTEM_IDENTITY.md").relative_to(repo_root())),
        "manifest": str((agn2_root() / "system_manifest.json").relative_to(repo_root())),
        "lifecycle": _lifecycle_state(),
        "system_mode": load_system_mode(),
        "overview": load_json(overview_path),
        "approval_gate": load_json(approval_path),
        "capability_snapshot": load_json(capability_path, default=build_capability_snapshot()),
        "execution_discipline": load_json(discipline_path, default={}),
        "control_plane": _control_plane_status(),
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_capabilities(_args: argparse.Namespace) -> int:
    payload = build_capability_snapshot()
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def _submit_and_apply(command: str, *, reason: str) -> dict[str, Any]:
    submitted = submit_admin_command(
        {
            "issuer": "admin",
            "command": command,
            "target_type": "system",
            "target_id": "",
            "reason": reason,
            "trace_id": f"agn2-{command.lower()}",
        }
    )
    daemon = control_daemon_run_once(max_commands=20)
    read_models = refresh_read_models()
    lifecycle_status = "frozen" if command == "EMERGENCY_STOP" else "running"
    lifecycle = _update_lifecycle(status=lifecycle_status, command=command.lower(), reason=reason)
    return {
        "ok": True,
        "submitted": submitted,
        "control_daemon": daemon,
        "read_models": read_models,
        "lifecycle": lifecycle,
        "system_mode": load_system_mode(),
    }


def cmd_emergency_stop(args: argparse.Namespace) -> int:
    payload = _submit_and_apply("EMERGENCY_STOP", reason=str(args.reason).strip() or "agn2 emergency stop")
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_release_stop(args: argparse.Namespace) -> int:
    payload = _submit_and_apply("RELEASE_STOP", reason=str(args.reason).strip() or "agn2 release stop")
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_control_plane_check(_args: argparse.Namespace) -> int:
    cp_root = control_plane_root() / "src-tauri"
    output = subprocess.run(
        ["cargo", "check", "-q"],
        cwd=str(cp_root),
        text=True,
        capture_output=True,
        check=False,
    )
    payload = {
        "ok": output.returncode == 0,
        "control_plane_root": str(cp_root),
        "tauri_cli_available": bool(shutil.which("cargo-tauri")),
        "stdout": str(output.stdout or "").strip(),
        "stderr": str(output.stderr or "").strip(),
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload["ok"] else 1


def cmd_validate(_args: argparse.Namespace) -> int:
    validation_script = repo_root() / "scripts" / "validation" / "run_agn2_consolidation_validation.py"
    output = subprocess.run(
        [sys.executable, str(validation_script)],
        cwd=str(repo_root()),
        text=True,
        capture_output=True,
        check=False,
    )
    parsed_stdout: Any
    try:
        parsed_stdout = json.loads(str(output.stdout or "{}"))
    except Exception:
        parsed_stdout = {"raw_stdout": str(output.stdout or "").strip()}
    payload = {
        "ok": output.returncode == 0,
        "validation_script": str(validation_script.relative_to(repo_root())),
        "stdout": parsed_stdout,
        "stderr": str(output.stderr or "").strip(),
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Canonical AGN2.0 lifecycle entry")
    sub = parser.add_subparsers(dest="command", required=True)

    start_parser = sub.add_parser("start", help="Prepare AGN2.0 lifecycle state, provider probe, daemon tick, and read models")
    start_parser.set_defaults(func=cmd_start)

    refresh_parser = sub.add_parser("refresh", help="Refresh AGN2.0 provider state, daemon queue, bus expiry, and read models")
    refresh_parser.set_defaults(func=cmd_refresh)

    status_parser = sub.add_parser("status", help="Show consolidated AGN2.0 lifecycle and control-plane status")
    status_parser.set_defaults(func=cmd_status)

    capabilities_parser = sub.add_parser("capabilities", help="Show the current AGN2.0 capability snapshot")
    capabilities_parser.set_defaults(func=cmd_capabilities)

    stop_parser = sub.add_parser("emergency-stop", help="Submit and apply a formal emergency stop command")
    stop_parser.add_argument("--reason", default="")
    stop_parser.set_defaults(func=cmd_emergency_stop)

    release_parser = sub.add_parser("release-stop", help="Submit and apply a formal release-stop command")
    release_parser.add_argument("--reason", default="")
    release_parser.set_defaults(func=cmd_release_stop)

    cp_parser = sub.add_parser("control-plane-check", help="Run a compile check for the AGN2.0 control plane")
    cp_parser.set_defaults(func=cmd_control_plane_check)

    validate_parser = sub.add_parser("validate", help="Run AGN2.0 consolidation-level validation")
    validate_parser.set_defaults(func=cmd_validate)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
