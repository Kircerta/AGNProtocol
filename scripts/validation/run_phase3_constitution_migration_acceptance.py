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
    "tests/test_package_constitution.py",
    "tests/test_package_emergency_stop.py",
    "tests/test_package_control_daemon.py",
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
    script_payload_cmd = (
        "import json, agn2_constitution as const; "
        "print(json.dumps({'package_path': const.PACKAGE_PATH, 'legacy_script_shim': const.LEGACY_SCRIPT_SHIM}, ensure_ascii=True))"
    )
    script_show = _run(
        [sys.executable, "-c", script_payload_cmd],
        extra_env={"PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'scripts'}"},
    )
    script_payload = json.loads(script_show.stdout)

    package_show = _run(
        [
            sys.executable,
            "-c",
            (
                "from agn.core.constitution import PACKAGE_PATH, LEGACY_SCRIPT_SHIM; "
                "import json; print(json.dumps({'package_path': PACKAGE_PATH, 'legacy_script_shim': LEGACY_SCRIPT_SHIM}, ensure_ascii=True))"
            ),
        ],
        extra_env={"PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'scripts'}"},
    )
    package_payload = json.loads(package_show.stdout)

    shim_text = (ROOT / "scripts" / "agn2_constitution.py").read_text(encoding="utf-8")
    control_daemon_text = (ROOT / "src" / "agn" / "governance" / "control_daemon.py").read_text(encoding="utf-8")
    emergency_stop_text = (ROOT / "src" / "agn" / "core" / "emergency_stop.py").read_text(encoding="utf-8")
    bridge_text = (ROOT / "src" / "agn" / "governance" / "bridge.py").read_text(encoding="utf-8")

    return {
        "script_surface": {
            "returncode": int(script_show.returncode),
            "package_path": str(script_payload.get("package_path", "")),
            "pass": int(script_show.returncode) == 0 and str(script_payload.get("package_path", "")) == "agn.core.constitution",
        },
        "package_surface": {
            "returncode": int(package_show.returncode),
            "package_path": str(package_payload.get("package_path", "")),
            "pass": int(package_show.returncode) == 0 and str(package_payload.get("package_path", "")) == "agn.core.constitution",
        },
        "script_shim_static": {
            "shim_import_present": "agn.core.constitution" in shim_text,
            "pass": "agn.core.constitution" in shim_text,
        },
        "control_daemon_import": {
            "package_import_present": "from agn.core.constitution import issuer_is_authorized, load_constitution" in control_daemon_text,
            "pass": "from agn.core.constitution import issuer_is_authorized, load_constitution" in control_daemon_text,
        },
        "emergency_stop_import": {
            "package_import_present": "from agn.core.constitution import emergency_stop_policy" in emergency_stop_text,
            "pass": "from agn.core.constitution import emergency_stop_policy" in emergency_stop_text,
        },
        "agn1_bridge_import": {
            "package_import_present": "from agn.core.constitution import load_constitution" in bridge_text,
            "pass": "from agn.core.constitution import load_constitution" in bridge_text,
        },
    }


def build_report() -> dict[str, Any]:
    tests = _run_tests()
    checks = _migration_checks()
    overall_pass = tests["pass"] and all(bool(item["pass"]) for item in checks.values())
    return {
        "schema_version": "agn.validation.phase3_constitution_migration.v1",
        "generated_at": utc_now_iso(),
        "objective": "Verify that constitution loading and policy helpers now live in src/agn/core while the legacy script remains a compatible shim and active runtime consumers import the package implementation.",
        "binary_verdict": {
            "targeted_tests_green": bool(tests["pass"]),
            "script_surface_works": bool(checks["script_surface"]["pass"]),
            "package_surface_works": bool(checks["package_surface"]["pass"]),
            "script_shim_present": bool(checks["script_shim_static"]["pass"]),
            "active_runtime_imports_package": bool(
                checks["control_daemon_import"]["pass"]
                and checks["emergency_stop_import"]["pass"]
                and checks["agn1_bridge_import"]["pass"]
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
    report_path = REPORT_DIR / f"{timestamp}-phase3-constitution-migration-acceptance.json"
    latest = REPORT_DIR / "phase3-constitution-migration-acceptance.latest.json"
    atomic_write_json(report_path, payload)
    atomic_write_json(latest, payload)
    print(json.dumps({**payload, "report_path": str(report_path)}, ensure_ascii=True, indent=2))
    return 0 if payload["binary_verdict"]["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
