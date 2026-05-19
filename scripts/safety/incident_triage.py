#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REPORT_ROOT = ROOT / "reports" / "security"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _run(cmd: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        return {
            "command": cmd,
            "returncode": int(completed.returncode),
            "stdout": str(completed.stdout or ""),
            "stderr": str(completed.stderr or ""),
        }
    except Exception as exc:
        return {
            "command": cmd,
            "returncode": 1,
            "stdout": "",
            "stderr": f"{type(exc).__name__}:{exc}",
        }


def _stat_summary(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {"path": str(resolved), "exists": False}
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "exists": True,
        "size": int(stat.st_size),
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def _safe_env_summary() -> dict[str, str]:
    allow = ["PATH", "SHELL", "USER", "HOME", "CODEX_HOME", "UV_PYTHON", "TERM"]
    return {key: str(os.environ.get(key, "")) for key in allow}


def _launch_items() -> dict[str, Any]:
    targets = [
        Path.home() / "Library" / "LaunchAgents",
        Path("/Library/LaunchAgents"),
        Path("/Library/LaunchDaemons"),
    ]
    result: dict[str, Any] = {}
    for target in targets:
        resolved = target.expanduser().resolve()
        if not resolved.exists():
            result[str(resolved)] = {"exists": False, "items": []}
            continue
        items = sorted(item.name for item in resolved.glob("*.plist"))
        result[str(resolved)] = {"exists": True, "items": items}
    return result


def build_snapshot() -> dict[str, Any]:
    home = Path.home()
    shell_files = [
        home / ".zprofile",
        home / ".zshrc",
        home / ".bash_profile",
        home / ".bashrc",
    ]
    codex_files = [
        home / ".codex" / "AGENTS.md",
        home / ".codex" / "MACHINE_CONTEXT.md",
        home / ".codex" / "RECENT_MACHINE_SETUP.md",
        home / ".codex" / "DEBUG_PLAYBOOK.md",
        home / ".codex" / "state_5.sqlite",
    ]
    openclaw_files = [
        home / ".openclaw" / "openclaw.json",
        home / ".openclaw" / "workspace" / "SOUL.md",
        home / ".openclaw" / "workspace" / "USER.md",
        home / ".openclaw" / "workspace" / "HEARTBEAT.md",
        home / ".openclaw" / "workspace-coordinator" / "SOUL.md",
        home / ".openclaw" / "workspace-coordinator" / "USER.md",
        home / ".openclaw" / "workspace-coordinator" / "HEARTBEAT.md",
        home / ".openclaw" / "agents" / "kirara" / "sessions" / "sessions.json",
        home / ".openclaw" / "agents" / "coordinator" / "sessions" / "sessions.json",
    ]
    payload = {
        "generated_at": utc_now_iso(),
        "mode": "read_only_triage",
        "host": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "environment": _safe_env_summary(),
        "shell_files": [_stat_summary(path) for path in shell_files],
        "codex_files": [_stat_summary(path) for path in codex_files],
        "openclaw_files": [_stat_summary(path) for path in openclaw_files],
        "launch_items": _launch_items(),
        "commands": {
            "listening_ports": _run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]),
            "launchctl_user": _run(["launchctl", "list"]),
            "crontab": _run(["crontab", "-l"]),
            "git_status": _run(["git", "status", "--short"]),
        },
        "notes": [
            "This snapshot is read-only and intentionally avoids printing arbitrary environment variables.",
            "Review listening_ports, launch items, and shell/codex/openclaw file mtimes first if compromise is suspected.",
        ],
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect a read-only incident triage snapshot for the local AGN environment")
    parser.add_argument("--output", default="", help="Optional output path for the triage JSON")
    args = parser.parse_args()

    payload = build_snapshot()
    if args.output:
        output = Path(args.output).expanduser()
    else:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = REPORT_ROOT / f"incident_triage_{stamp}.json"
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
