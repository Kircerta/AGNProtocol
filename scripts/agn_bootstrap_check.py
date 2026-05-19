#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

try:
    from admin_control_common import atomic_write_json
except ImportError:  # pragma: no cover
    from scripts.admin_control_common import atomic_write_json


WhichFn = Callable[[str], str | None]
SpecFn = Callable[[str], Any]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _command_check(command: str, *, required: bool, which: WhichFn = shutil.which, aliases: tuple[str, ...] = ()) -> dict[str, Any]:
    candidates = (command, *aliases)
    found = ""
    for candidate in candidates:
        path = which(candidate)
        if path:
            found = path
            break
    return {
        "kind": "command",
        "name": command,
        "required": required,
        "available": bool(found),
        "resolved_path": found,
    }


def _python_module_check(module: str, *, required: bool, find_spec: SpecFn = importlib.util.find_spec) -> dict[str, Any]:
    spec = find_spec(module)
    return {
        "kind": "python_module",
        "name": module,
        "required": required,
        "available": spec is not None,
    }


def _file_check(label: str, relative_path: str, *, required: bool) -> dict[str, Any]:
    path = ROOT / relative_path
    return {
        "kind": "file",
        "name": label,
        "path": str(path),
        "required": required,
        "available": path.exists(),
    }


def _provider_lane_summary(which: WhichFn = shutil.which) -> dict[str, Any]:
    lanes = {
        "codex_cli": bool(which("codex")),
        "claude_cli": bool(which("claude") or which("claude-code")),
        "gemini_cli": bool(which("gemini")),
        "deepseek_api_key": bool(str(__import__("os").getenv("DEEPSEEK_API_KEY", "")).strip()),
        "vertex_local_env": bool(str(__import__("os").getenv("VERTEX_LOCAL_BASE_URL", "")).strip()),
        "qwen_local_env": bool(str(__import__("os").getenv("QWEN_LOCAL_BASE_URL", "")).strip()),
    }
    return {
        "available_count": sum(1 for value in lanes.values() if bool(value)),
        "lanes": lanes,
    }


def _repo_venv_check() -> dict[str, Any]:
    venv_python = ROOT / ".venv" / "bin" / "python"
    using_repo_venv = str(sys.executable).startswith(str((ROOT / ".venv").resolve()))
    return {
        "kind": "environment",
        "name": "repo_venv_active",
        "required": False,
        "available": using_repo_venv,
        "venv_exists": venv_python.exists(),
        "current_python": sys.executable,
        "expected_python": str(venv_python),
    }


def build_bootstrap_check(*, which: WhichFn = shutil.which, find_spec: SpecFn = importlib.util.find_spec) -> dict[str, Any]:
    required_checks = [
        {
            "kind": "python",
            "name": "python_version",
            "required": True,
            "available": sys.version_info >= (3, 11),
            "detail": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        },
        _command_check("git", required=True, which=which),
        _command_check("python3", required=True, which=which),
        _command_check("uv", required=True, which=which),
        _file_check("pyproject", "pyproject.toml", required=True),
        _file_check("agents_doc", "AGENTS.md", required=True),
        _file_check("project_brief", "PROJECT_BRIEF.md", required=True),
        _file_check("runbook", "RUNBOOK.md", required=True),
        _file_check("lifecycle_entry", "scripts/agn2_system.py", required=True),
        _file_check("execution_workflow", "scripts/agn2_execution_workflow.py", required=True),
    ]

    recommended_checks = [
        _repo_venv_check(),
        _command_check("uvx", required=False, which=which),
        _command_check("rg", required=False, which=which),
        _command_check("make", required=False, which=which),
        _command_check("codex", required=False, which=which),
        _command_check("claude", required=False, which=which, aliases=("claude-code",)),
        _command_check("browser-use", required=False, which=which),
        _python_module_check("fastapi", required=False, find_spec=find_spec),
        _python_module_check("uvicorn", required=False, find_spec=find_spec),
        _python_module_check("jwt", required=False, find_spec=find_spec),
        _python_module_check("httpx", required=False, find_spec=find_spec),
        _python_module_check("pytest", required=False, find_spec=find_spec),
        _python_module_check("mcp", required=False, find_spec=find_spec),
        _python_module_check("chromadb", required=False, find_spec=find_spec),
        _file_check("package_root", "src/agn", required=False),
    ]

    optional_checks = [
        _command_check("ghostty", required=False, which=which),
        _command_check("google-chrome", required=False, which=which),
        _command_check("obsidian", required=False, which=which),
        _command_check("tesseract", required=False, which=which),
        _command_check("cloudflared", required=False, which=which),
        _command_check("cargo", required=False, which=which),
    ]

    provider_lanes = _provider_lane_summary(which=which)
    required_ok = all(bool(item.get("available")) for item in required_checks)
    recommended_missing = [item["name"] for item in recommended_checks if not bool(item.get("available"))]
    optional_available = [item["name"] for item in optional_checks if bool(item.get("available"))]

    next_steps: list[str] = []
    if not required_ok:
        next_steps.append("Install the missing required commands, Python packages, or repo files before attempting AGN startup.")
    else:
        next_steps.append("Run `python3 scripts/agn2_system.py validate` and `python3 scripts/agn2_system.py start`.")
    repo_venv = next((item for item in recommended_checks if item["name"] == "repo_venv_active"), None)
    if isinstance(repo_venv, dict) and bool(repo_venv.get("venv_exists")) and not bool(repo_venv.get("available")):
        next_steps.append("Activate the repo virtual environment with `source .venv/bin/activate` before expecting optional Python modules to appear in checks.")
    if not bool(next((item for item in recommended_checks if item["name"] == "uvx" and item["available"]), None)):
        next_steps.append("Install `uvx` via `uv` so validation and targeted pytest flows work as documented.")
    if provider_lanes["available_count"] == 0:
        next_steps.append("Configure at least one non-CEU helper lane if you want reviewer or worker assistance beyond the current Codex session.")
    else:
        next_steps.append("Provider/reviewer helper lanes are visible; run `python3 scripts/agn2_system.py capabilities` to confirm the current mix.")
    next_steps.append("Read `README.md`, `RUNBOOK.md`, `SECURITY.md`, and `PROJECT_BRIEF.md` before the first substantial task.")
    next_steps.append("Run `python3 scripts/agn2_execution_workflow.py preflight --task-summary \"First AGN task on this MacBook\"` before the first real task.")

    return {
        "schema_version": "agn.bootstrap_check.v1",
        "generated_at": utc_now_iso(),
        "repo_root": str(ROOT),
        "supported_bootstrap_target": "macbook_local_operator_node",
        "required_checks": required_checks,
        "recommended_checks": recommended_checks,
        "optional_checks": optional_checks,
        "provider_lanes": provider_lanes,
        "status": {
            "required_ready": required_ok,
            "recommended_missing_count": len(recommended_missing),
            "optional_available_count": len(optional_available),
            "first_task_ready": required_ok,
        },
        "next_steps": next_steps,
        "operator_guidance": [
            "Required checks are the floor for a trustworthy local AGN bootstrap.",
            "Recommended checks improve review, testing, browser execution, and operator ergonomics but are not required for the first task.",
            "Optional tools should be installed only when the task class needs them.",
        ],
    }


def write_bootstrap_check(payload: dict[str, Any], *, output_path: Path | None = None) -> Path:
    target = output_path or (ROOT / "runtime" / "admin_control" / "read_models" / "bootstrap_check.json")
    atomic_write_json(target, payload)
    return target


def cmd_check(args: argparse.Namespace) -> int:
    payload = build_bootstrap_check()
    target = None
    if args.output:
        target = Path(args.output).expanduser().resolve()
    if not args.no_write:
        write_bootstrap_check(payload, output_path=target)
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload["status"]["required_ready"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check whether a local MacBook-style AGN bootstrap is ready for first-task execution.")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Run the bootstrap readiness checks.")
    check.add_argument("--output", default="")
    check.add_argument("--no-write", action="store_true")
    check.set_defaults(func=cmd_check)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
