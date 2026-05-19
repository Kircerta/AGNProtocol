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
    "tests/test_agn_task_start_kernel.py",
    "tests/test_package_task_start_kernel.py",
    "tests/test_agn2_execution_workflow.py",
    "-q",
]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _run(cmd: list[str], *, timeout_sec: float = 180.0, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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


def _migration_checks() -> dict[str, Any]:
    script_build = _run([sys.executable, "scripts/agn_task_start_kernel.py", "build", "--task-summary", "Phase 3 kernel migration check"])
    script_payload = json.loads(script_build.stdout)
    package_build = _run(
        [
            sys.executable,
            "-c",
            (
                "from agn.governance.task_start_kernel import build_task_start_kernel; "
                "import json; "
                "print(json.dumps(build_task_start_kernel(task_summary='Phase 3 kernel migration check', risk_level='medium', snapshot={'system_mode': {'mode': 'normal'}, 'lifecycle': {'status': 'running'}, 'provider_summary': {'claude': True, 'gemini': True, 'deepseek': False, 'qwen_local': False}}), ensure_ascii=True))"
            ),
        ],
        extra_env={"PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'scripts'}"},
    )
    package_payload = json.loads(package_build.stdout)

    workflow_text = (ROOT / "src" / "agn" / "governance" / "execution_workflow.py").read_text(encoding="utf-8")
    shim_text = (ROOT / "scripts" / "agn_task_start_kernel.py").read_text(encoding="utf-8")

    return {
        "script_build": {
            "returncode": int(script_build.returncode),
            "package_path": str(script_payload.get("package_path", "")),
            "pass": int(script_build.returncode) == 0 and str(script_payload.get("package_path", "")) == "agn.governance.task_start_kernel",
        },
        "package_build": {
            "returncode": int(package_build.returncode),
            "package_path": str(package_payload.get("package_path", "")),
            "pass": int(package_build.returncode) == 0 and str(package_payload.get("package_path", "")) == "agn.governance.task_start_kernel",
        },
        "workflow_import": {
            "package_import_present": "from agn.governance.task_start_kernel import build_task_start_kernel" in workflow_text,
            "pass": "from agn.governance.task_start_kernel import build_task_start_kernel" in workflow_text,
        },
        "script_shim_static": {
            "shim_import_present": "from agn.governance.task_start_kernel import *" in shim_text,
            "pass": "from agn.governance.task_start_kernel import *" in shim_text,
        },
    }


def build_report() -> dict[str, Any]:
    tests = _run_tests()
    checks = _migration_checks()
    overall_pass = tests["pass"] and all(bool(item["pass"]) for item in checks.values())
    return {
        "schema_version": "agn.validation.phase3_task_start_kernel_migration.v1",
        "generated_at": utc_now_iso(),
        "objective": "Verify that task-start kernel implementation now lives in src/agn while the legacy script remains a compatible shim and preflight consumes the package import.",
        "binary_verdict": {
            "targeted_tests_green": bool(tests["pass"]),
            "script_build_works": bool(checks["script_build"]["pass"]),
            "package_build_works": bool(checks["package_build"]["pass"]),
            "workflow_uses_package_import": bool(checks["workflow_import"]["pass"]),
            "script_shim_present": bool(checks["script_shim_static"]["pass"]),
            "overall_pass": bool(overall_pass),
        },
        "counts": {
            "tests_passed": int(tests["passed_count"]),
            "checks_passed": int(sum(1 for item in checks.values() if item["pass"])),
        },
        "test_run": tests,
        "migration_checks": checks,
    }


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_report()
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"{timestamp}-phase3-task-start-kernel-migration-acceptance.json"
    latest = REPORT_DIR / "phase3-task-start-kernel-migration-acceptance.latest.json"
    atomic_write_json(report_path, payload)
    atomic_write_json(latest, payload)
    print(json.dumps({**payload, "report_path": str(report_path)}, ensure_ascii=True, indent=2))
    return 0 if payload["binary_verdict"]["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
