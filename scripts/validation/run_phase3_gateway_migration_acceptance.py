#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from admin_control_common import atomic_write_json


REPORT_DIR = ROOT / "reports" / "validation"
TEST_COMMAND = [
    "uvx",
    "pytest",
    "tests/test_package_execution_gateway.py",
    "tests/test_agn_governed_execution.py",
    "tests/test_handler_cli_isolation.py",
    "tests/test_dispatcher_runtime.py",
    "tests/test_agn_reconstruction_status.py",
    "-q",
]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _run(cmd: list[str], *, timeout_sec: float = 180.0, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = None
    if extra_env:
        env = {**dict(**extra_env), **{}}
    merged_env = None
    if extra_env is not None:
        import os

        merged_env = os.environ.copy()
        merged_env.update(extra_env)
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
        env=merged_env,
    )


def _run_tests() -> dict[str, Any]:
    completed = _run(TEST_COMMAND)
    stdout = str(completed.stdout or "")
    passed_count = 0
    for line in stdout.splitlines():
        if " passed" in line:
            parts = line.strip().split()
            if parts and parts[0].isdigit():
                passed_count = int(parts[0])
                break
    return {
        "command": TEST_COMMAND,
        "returncode": int(completed.returncode),
        "passed_count": passed_count,
        "stdout_tail": stdout.strip().splitlines()[-5:],
        "stderr_tail": str(completed.stderr or "").strip().splitlines()[-5:],
        "pass": completed.returncode == 0,
    }


def _script_and_package_checks() -> dict[str, Any]:
    script_show = _run([sys.executable, "scripts/agn_governed_execution.py", "show"])
    script_payload = json.loads(script_show.stdout)

    package_show = _run(
        [
            sys.executable,
            "-c",
            (
                "from agn.governance.execution_gateway import describe_gateway; "
                "import json; print(json.dumps(describe_gateway(), ensure_ascii=True))"
            ),
        ],
        extra_env={"PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'scripts'}"},
    )
    package_payload = json.loads(package_show.stdout)

    handler_guard = _run(
        [
            sys.executable,
            "-c",
            (
                "from agn.governance.handler_cli_guard import build_direct_handler_cli_block; "
                "import json; "
                "print(json.dumps(build_direct_handler_cli_block(handler_id='x', purpose='y', recommended_entrypoints=['z']), ensure_ascii=True))"
            ),
        ],
        extra_env={"PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'scripts'}"},
    )
    guard_payload = json.loads(handler_guard.stdout)

    script_text = (ROOT / "scripts" / "agn_governed_execution.py").read_text(encoding="utf-8")
    handler_shim_text = (ROOT / "scripts" / "agn_handler_cli_guard.py").read_text(encoding="utf-8")

    return {
        "script_show": {
            "returncode": int(script_show.returncode),
            "package_path": str(script_payload.get("package_path", "")),
            "legacy_script_shim": str(script_payload.get("legacy_script_shim", "")),
            "pass": int(script_show.returncode) == 0 and str(script_payload.get("package_path", "")) == "agn.governance.execution_gateway",
        },
        "package_show": {
            "returncode": int(package_show.returncode),
            "phase_alignment": str(package_payload.get("phase_alignment", "")),
            "origin_phase": str(package_payload.get("origin_phase", "")),
            "pass": int(package_show.returncode) == 0
            and str(package_payload.get("phase_alignment", "")) == "phase_3_gradual_implementation_migration",
        },
        "handler_guard_package": {
            "returncode": int(handler_guard.returncode),
            "error": str(guard_payload.get("error", "")),
            "pass": int(handler_guard.returncode) == 0
            and str(guard_payload.get("error", "")) == "direct_handler_cli_requires_explicit_ack",
        },
        "script_shim_static": {
            "execution_gateway_shim": "from agn.governance.execution_gateway import *" in script_text,
            "handler_guard_shim": "from agn.governance.handler_cli_guard import *" in handler_shim_text,
            "pass": "from agn.governance.execution_gateway import *" in script_text
            and "from agn.governance.handler_cli_guard import *" in handler_shim_text,
        },
    }


def _tracker_check() -> dict[str, Any]:
    completed = _run([sys.executable, "scripts/agn_reconstruction_status.py", "show"])
    payload = json.loads(completed.stdout)
    current_phase = payload.get("current_phase", {}) if isinstance(payload.get("current_phase", {}), dict) else {}
    latest_milestone = payload.get("latest_milestone", {}) if isinstance(payload.get("latest_milestone", {}), dict) else {}
    return {
        "returncode": int(completed.returncode),
        "current_phase": str(current_phase.get("phase_id", "")),
        "latest_milestone_title": str(latest_milestone.get("title", "")),
        "pass": int(completed.returncode) == 0
        and str(current_phase.get("phase_id", "")) == "phase_3_gradual_implementation_migration",
    }


def build_report() -> dict[str, Any]:
    tests = _run_tests()
    checks = _script_and_package_checks()
    tracker = _tracker_check()
    overall_pass = all(
        [
            tests["pass"],
            all(bool(item["pass"]) for item in checks.values()),
            bool(tracker["pass"]),
        ]
    )
    return {
        "schema_version": "agn.validation.phase3_gateway_migration.v1",
        "generated_at": utc_now_iso(),
        "objective": "Verify that governed execution gateway implementation now lives in src/agn while script entrypoints remain compatible shims.",
        "binary_verdict": {
            "targeted_tests_green": bool(tests["pass"]),
            "script_show_works": bool(checks["script_show"]["pass"]),
            "package_show_works": bool(checks["package_show"]["pass"]),
            "handler_guard_package_works": bool(checks["handler_guard_package"]["pass"]),
            "script_shims_present": bool(checks["script_shim_static"]["pass"]),
            "tracker_phase3_active": bool(tracker["pass"]),
            "overall_pass": bool(overall_pass),
        },
        "counts": {
            "tests_passed": int(tests["passed_count"]),
            "checks_passed": int(sum(1 for item in checks.values() if item["pass"])) + int(bool(tracker["pass"])),
        },
        "test_run": tests,
        "migration_checks": checks,
        "tracker_check": tracker,
    }


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_report()
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"{timestamp}-phase3-gateway-migration-acceptance.json"
    latest = REPORT_DIR / "phase3-gateway-migration-acceptance.latest.json"
    atomic_write_json(report_path, payload)
    atomic_write_json(latest, payload)
    print(json.dumps({**payload, "report_path": str(report_path)}, ensure_ascii=True, indent=2))
    return 0 if payload["binary_verdict"]["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
