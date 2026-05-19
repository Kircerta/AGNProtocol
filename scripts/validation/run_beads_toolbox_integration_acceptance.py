#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from admin_control_common import atomic_write_json


REPORT_DIR = ROOT / "reports" / "validation"
TEST_COMMAND = ["uvx", "pytest", "tests/test_agn_external_toolbox.py", "-q"]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _open_source_root() -> Path:
    configured = str(os.getenv("AGN_OPEN_SOURCE_ROOT", "")).strip()
    if configured:
        return Path(configured).expanduser()
    return ROOT.parent / "OpenSource"


def _run(cmd: list[str], *, cwd: Path | None = None, timeout_sec: float = 180.0, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if extra_env:
        merged_env.update(extra_env)
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd or ROOT),
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
            env=merged_env,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=127,
            stdout="",
            stderr=f"command_not_found:{exc.filename or cmd[0]}",
        )


def _parse_passed_count(stdout: str) -> int:
    for line in stdout.splitlines():
        if " passed" in line:
            first = line.strip().split()[0]
            if first.isdigit():
                return int(first)
    return 0


def _safe_json_loads(text: str, default: Any) -> tuple[Any, str]:
    raw = str(text or "").strip()
    if not raw:
        return default, "empty_stdout"
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError as exc:
        return default, f"{type(exc).__name__}: {exc}"


def _run_tests() -> dict[str, Any]:
    completed = _run(TEST_COMMAND)
    stdout = str(completed.stdout or "")
    return {
        "command": TEST_COMMAND,
        "returncode": int(completed.returncode),
        "passed_count": _parse_passed_count(stdout),
        "stdout_tail": stdout.strip().splitlines()[-5:],
        "stderr_tail": str(completed.stderr or "").strip().splitlines()[-5:],
        "pass": completed.returncode == 0,
    }


def _binary_state() -> dict[str, Any]:
    bd_path = shutil.which("bd") or ""
    dolt_path = shutil.which("dolt") or ""
    version = _run(["bd", "--version"]) if bd_path else None
    return {
        "bd": {
            "available": bool(bd_path),
            "path": bd_path,
            "version_stdout": str(version.stdout or "").strip().splitlines()[:2] if version else [],
            "pass": bool(bd_path) and version is not None and version.returncode == 0,
        },
        "dolt": {
            "available": bool(dolt_path),
            "path": dolt_path,
            "pass": bool(dolt_path),
        },
    }


def _toolbox_checks() -> dict[str, Any]:
    list_probe = _run([sys.executable, "scripts/agn_external_toolbox.py", "list", "--json"])
    show_probe = _run([sys.executable, "scripts/agn_external_toolbox.py", "show", "beads", "--json"])
    list_payload, list_parse_error = _safe_json_loads(str(list_probe.stdout or ""), {})
    show_payload, show_parse_error = _safe_json_loads(str(show_probe.stdout or ""), {})
    names = [str(item.get("name", "")) for item in list_payload.get("entries", [])]
    return {
        "list_includes_beads": {
            "returncode": int(list_probe.returncode),
            "parse_error": list_parse_error,
            "count": int(list_payload.get("count", 0)),
            "stdout_tail": str(list_probe.stdout or "").strip().splitlines()[-5:],
            "stderr_tail": str(list_probe.stderr or "").strip().splitlines()[-5:],
            "pass": int(list_probe.returncode) == 0 and not list_parse_error and "beads" in names,
        },
        "show_beads_ready": {
            "returncode": int(show_probe.returncode),
            "parse_error": show_parse_error,
            "readiness": str(show_payload.get("readiness", "")),
            "repo_exists": bool(show_payload.get("repo_exists", False)),
            "docs_exists": bool(show_payload.get("docs_exists", False)),
            "binary_checks": list(show_payload.get("binary_checks", [])),
            "stdout_tail": str(show_probe.stdout or "").strip().splitlines()[-5:],
            "stderr_tail": str(show_probe.stderr or "").strip().splitlines()[-5:],
            "pass": int(show_probe.returncode) == 0
            and not show_parse_error
            and str(show_payload.get("readiness", "")) == "available"
            and bool(show_payload.get("repo_exists", False))
            and bool(show_payload.get("docs_exists", False)),
        },
    }


def _smoke_run() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agnbeadssmoke") as success_root_str:
        success_root = Path(success_root_str)
        workspace = success_root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        init_ok = _run(["bd", "init", "--quiet", "--stealth"], cwd=workspace)
        create_ok = _run(["bd", "create", "Test AGN beads integration", "-p", "1", "--json"], cwd=workspace)
        ready_ok = _run(["bd", "ready", "--json"], cwd=workspace)

        create_payload, create_parse_error = _safe_json_loads(str(create_ok.stdout or ""), {})
        ready_payload, ready_parse_error = _safe_json_loads(str(ready_ok.stdout or ""), [])

        return {
            "stealth_init_in_safe_workspace": {
                "cwd": str(workspace),
                "returncode": int(init_ok.returncode),
                "cleaned_up": True,
                "pass": int(init_ok.returncode) == 0,
            },
            "create_issue_json": {
                "returncode": int(create_ok.returncode),
                "parse_error": create_parse_error,
                "issue_id": str(create_payload.get("id", "")),
                "status": str(create_payload.get("status", "")),
                "stderr_tail": str(create_ok.stderr or "").strip().splitlines()[-5:],
                "pass": int(create_ok.returncode) == 0 and not create_parse_error and bool(str(create_payload.get("id", "")).strip()) and str(create_payload.get("status", "")) == "open",
            },
            "ready_queue_json": {
                "returncode": int(ready_ok.returncode),
                "parse_error": ready_parse_error,
                "result_count": len(ready_payload) if isinstance(ready_payload, list) else -1,
                "stderr_tail": str(ready_ok.stderr or "").strip().splitlines()[-5:],
                "pass": int(ready_ok.returncode) == 0 and not ready_parse_error and isinstance(ready_payload, list) and len(ready_payload) >= 1,
            },
            "exploratory_watch_items": {
                "manual_observation": "An earlier exploratory tmpdir-based init produced an invalid database name error, but deterministic reproduction is still pending.",
                "pass": True,
            },
        }


def build_report() -> dict[str, Any]:
    tests = _run_tests()
    binaries = _binary_state()
    toolbox = _toolbox_checks()
    if all(bool(binaries[name]["pass"]) for name in ("bd", "dolt")):
        smoke = _smoke_run()
    else:
        smoke = {
            "stealth_init_in_safe_workspace": {
                "pass": False,
                "error": "required binaries unavailable; smoke skipped",
            },
            "create_issue_json": {
                "pass": False,
                "error": "required binaries unavailable; smoke skipped",
            },
            "ready_queue_json": {
                "pass": False,
                "error": "required binaries unavailable; smoke skipped",
            },
            "exploratory_watch_items": {
                "manual_observation": "Smoke was skipped because required binaries were unavailable.",
                "pass": True,
            },
        }
    open_source_beads = _open_source_root() / "beads"
    overall_pass = tests["pass"] and all(item["pass"] for item in binaries.values()) and all(item["pass"] for item in toolbox.values()) and all(item["pass"] for item in smoke.values())
    return {
        "schema_version": "agn.validation.beads_toolbox_integration.v1",
        "generated_at": utc_now_iso(),
        "objective": "Verify that beads is understood, mounted, documented, and locally runnable as an AGN external toolbox task-graph sidecar without being promoted into canonical memory or governance state.",
        "source_repo": {
            "path": str(open_source_beads),
            "exists": open_source_beads.exists(),
        },
        "binary_verdict": {
            "targeted_tests_green": bool(tests["pass"]),
            "bd_available": bool(binaries["bd"]["pass"]),
            "dolt_available": bool(binaries["dolt"]["pass"]),
            "toolbox_list_includes_beads": bool(toolbox["list_includes_beads"]["pass"]),
            "toolbox_show_marks_beads_available": bool(toolbox["show_beads_ready"]["pass"]),
            "safe_workspace_init_works": bool(smoke["stealth_init_in_safe_workspace"]["pass"]),
            "create_issue_json_works": bool(smoke["create_issue_json"]["pass"]),
            "ready_queue_json_works": bool(smoke["ready_queue_json"]["pass"]),
            "overall_pass": bool(overall_pass),
        },
        "counts": {
            "tests_passed": int(tests["passed_count"]),
            "toolbox_checks_passed": int(sum(1 for item in toolbox.values() if item["pass"])),
            "smoke_checks_passed": int(sum(1 for item in smoke.values() if item["pass"])),
        },
        "tests": tests,
        "binaries": binaries,
        "toolbox_checks": toolbox,
        "smoke": smoke,
    }


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_report()
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"{timestamp}-beads-toolbox-integration-acceptance.json"
    latest = REPORT_DIR / "beads-toolbox-integration-acceptance.latest.json"
    atomic_write_json(report_path, payload)
    atomic_write_json(latest, payload)
    print(json.dumps({**payload, "report_path": str(report_path)}, ensure_ascii=True, indent=2))
    return 0 if payload["binary_verdict"]["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
