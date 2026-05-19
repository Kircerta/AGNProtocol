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
    "tests/test_package_guarded_io.py",
    "tests/test_role_guard_bypass.py",
    "tests/test_command_request_security.py",
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
    env = {"PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'scripts'}", "AGN_REPO_ROOT": str(ROOT)}
    script_show = _run(
        [
            sys.executable,
            "-c",
            (
                "import json, guarded_io as gio; "
                "print(json.dumps({'package_path': gio.PACKAGE_PATH, 'legacy_script_shim': gio.LEGACY_SCRIPT_SHIM}, ensure_ascii=True))"
            ),
        ],
        extra_env=env,
    )
    script_payload = json.loads(script_show.stdout)
    package_show = _run(
        [
            sys.executable,
            "-c",
            (
                "from agn.core.guarded_io import PACKAGE_PATH, LEGACY_SCRIPT_SHIM; "
                "import json; print(json.dumps({'package_path': PACKAGE_PATH, 'legacy_script_shim': LEGACY_SCRIPT_SHIM}, ensure_ascii=True))"
            ),
        ],
        extra_env=env,
    )
    package_payload = json.loads(package_show.stdout)

    shim_text = (ROOT / "scripts" / "guarded_io.py").read_text(encoding="utf-8")
    command_request_text = (ROOT / "scripts" / "command_request.py").read_text(encoding="utf-8")
    action_runner_text = (ROOT / "scripts" / "action_runner.py").read_text(encoding="utf-8")
    agent_runner_text = (ROOT / "scripts" / "agent_runner.py").read_text(encoding="utf-8")
    adversarial_probe_text = (ROOT / "scripts" / "validation" / "agn_adversarial_probe.py").read_text(encoding="utf-8")
    action_runner_import_smoke = _run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys, types; "
                "sys.path.insert(0, str(__import__('pathlib').Path(r'" + str(ROOT / "src") + "'))); "
                "sys.path.insert(0, str(__import__('pathlib').Path(r'" + str(ROOT) + "'))); "
                "sys.path.insert(0, str(__import__('pathlib').Path(r'" + str(ROOT / "scripts") + "'))); "
                "sys.modules['httpx'] = types.ModuleType('httpx'); "
                "import scripts.action_runner as ar; "
                "print(json.dumps({'has_run_pending': hasattr(ar, 'run_pending')}, ensure_ascii=True))"
            ),
        ],
        extra_env=env,
    )
    action_runner_smoke_payload = json.loads(str(action_runner_import_smoke.stdout or "{}")) if action_runner_import_smoke.returncode == 0 else {}

    return {
        "script_surface": {
            "returncode": int(script_show.returncode),
            "package_path": str(script_payload.get("package_path", "")),
            "pass": int(script_show.returncode) == 0 and str(script_payload.get("package_path", "")) == "agn.core.guarded_io",
        },
        "package_surface": {
            "returncode": int(package_show.returncode),
            "package_path": str(package_payload.get("package_path", "")),
            "pass": int(package_show.returncode) == 0 and str(package_payload.get("package_path", "")) == "agn.core.guarded_io",
        },
        "script_shim_static": {
            "shim_import_present": "from agn.core.guarded_io import *" in shim_text,
            "pass": "from agn.core.guarded_io import *" in shim_text,
        },
        "command_request_import": {
            "package_import_present": "from agn.core.guarded_io import atomic_write_json" in command_request_text,
            "pass": "from agn.core.guarded_io import atomic_write_json" in command_request_text,
        },
        "action_runner_import": {
            "package_import_present": "from agn.core.guarded_io import atomic_write_text, write_text" in action_runner_text,
            "pass": "from agn.core.guarded_io import atomic_write_text, write_text" in action_runner_text,
        },
        "action_runner_import_smoke": {
            "returncode": int(action_runner_import_smoke.returncode),
            "has_run_pending": bool(action_runner_smoke_payload.get("has_run_pending", False)),
            "pass": int(action_runner_import_smoke.returncode) == 0 and bool(action_runner_smoke_payload.get("has_run_pending", False)),
        },
        "agent_runner_import": {
            "package_import_present": "from agn.core.guarded_io import atomic_write_json as _guarded_atomic_write_json, write_text as _guarded_write_text" in agent_runner_text,
            "pass": "from agn.core.guarded_io import atomic_write_json as _guarded_atomic_write_json, write_text as _guarded_write_text" in agent_runner_text,
        },
        "adversarial_probe_import": {
            "package_import_present": "from agn.core.guarded_io import write_bytes, write_text" in adversarial_probe_text,
            "pass": "from agn.core.guarded_io import write_bytes, write_text" in adversarial_probe_text,
        },
    }


def build_report() -> dict[str, Any]:
    tests = _run_tests()
    checks = _migration_checks()
    overall_pass = tests["pass"] and all(bool(item["pass"]) for item in checks.values())
    return {
        "schema_version": "agn.validation.phase3_guarded_io_migration.v1",
        "generated_at": utc_now_iso(),
        "objective": "Verify that guarded filesystem write helpers now live in src/agn/core while the legacy script remains a compatibility shim and low-level request/action/execution paths import the package implementation.",
        "binary_verdict": {
            "targeted_tests_green": bool(tests["pass"]),
            "script_surface_works": bool(checks["script_surface"]["pass"]),
            "package_surface_works": bool(checks["package_surface"]["pass"]),
            "script_shim_present": bool(checks["script_shim_static"]["pass"]),
            "active_low_level_paths_import_package": bool(
                checks["command_request_import"]["pass"]
                and checks["action_runner_import"]["pass"]
                and checks["action_runner_import_smoke"]["pass"]
                and checks["agent_runner_import"]["pass"]
                and checks["adversarial_probe_import"]["pass"]
            ),
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
    report_path = REPORT_DIR / f"{timestamp}-phase3-guarded-io-migration-acceptance.json"
    latest = REPORT_DIR / "phase3-guarded-io-migration-acceptance.latest.json"
    atomic_write_json(report_path, payload)
    atomic_write_json(latest, payload)
    print(json.dumps({**payload, "report_path": str(report_path)}, ensure_ascii=True, indent=2))
    return 0 if payload["binary_verdict"]["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
