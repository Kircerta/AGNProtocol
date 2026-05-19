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
    "tests/test_agn_infrastructure_map.py",
    "tests/test_package_infrastructure_map.py",
    "tests/test_agn_evolution_pipeline.py",
    "tests/test_control_plane_read_model.py",
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
    script_show = _run([sys.executable, "scripts/agn_infrastructure_map.py", "show"])
    script_payload = json.loads(script_show.stdout)

    package_show = _run(
        [
            sys.executable,
            "-c",
            (
                "from agn.architecture.infrastructure_map import build_infrastructure_map; "
                "import json; print(json.dumps(build_infrastructure_map(), ensure_ascii=True))"
            ),
        ],
        extra_env={"PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'scripts'}"},
    )
    package_payload = json.loads(package_show.stdout)

    recommend = _run([sys.executable, "scripts/agn_infrastructure_map.py", "recommend", "--task-summary", "Continue AGN architecture refactor and inspect browser automation surfaces"])
    recommend_payload = json.loads(recommend.stdout)

    shim_text = (ROOT / "scripts" / "agn_infrastructure_map.py").read_text(encoding="utf-8")
    evolution_text = (ROOT / "src" / "agn" / "architecture" / "evolution_pipeline.py").read_text(encoding="utf-8")
    read_model_text = (ROOT / "src" / "agn" / "governance" / "read_models.py").read_text(encoding="utf-8")

    return {
        "script_show": {
            "returncode": int(script_show.returncode),
            "package_path": str(script_payload.get("package_path", "")),
            "pass": int(script_show.returncode) == 0 and str(script_payload.get("package_path", "")) == "agn.architecture.infrastructure_map",
        },
        "package_show": {
            "returncode": int(package_show.returncode),
            "package_path": str(package_payload.get("package_path", "")),
            "pass": int(package_show.returncode) == 0 and str(package_payload.get("package_path", "")) == "agn.architecture.infrastructure_map",
        },
        "script_recommend": {
            "returncode": int(recommend.returncode),
            "package_path": str(recommend_payload.get("package_path", "")),
            "pass": int(recommend.returncode) == 0 and str(recommend_payload.get("package_path", "")) == "agn.architecture.infrastructure_map",
        },
        "script_shim_static": {
            "shim_import_present": "from agn.architecture.infrastructure_map import *" in shim_text,
            "pass": "from agn.architecture.infrastructure_map import *" in shim_text,
        },
        "evolution_import": {
            "package_import_present": "from agn.architecture.infrastructure_map import build_infrastructure_map" in evolution_text,
            "pass": "from agn.architecture.infrastructure_map import build_infrastructure_map" in evolution_text,
        },
        "read_model_import": {
            "package_import_present": "from agn.architecture.infrastructure_map import build_infrastructure_map, write_infrastructure_map" in read_model_text,
            "pass": "from agn.architecture.infrastructure_map import build_infrastructure_map, write_infrastructure_map" in read_model_text,
        },
    }


def build_report() -> dict[str, Any]:
    tests = _run_tests()
    checks = _migration_checks()
    overall_pass = tests["pass"] and all(bool(item["pass"]) for item in checks.values())
    return {
        "schema_version": "agn.validation.phase3_infrastructure_map_migration.v1",
        "generated_at": utc_now_iso(),
        "objective": "Verify that infrastructure map now lives in src/agn/architecture while the legacy script remains a compatible shim and architecture-facing consumers import the package implementation.",
        "binary_verdict": {
            "targeted_tests_green": bool(tests["pass"]),
            "script_show_works": bool(checks["script_show"]["pass"]),
            "package_show_works": bool(checks["package_show"]["pass"]),
            "script_recommend_works": bool(checks["script_recommend"]["pass"]),
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
    report_path = REPORT_DIR / f"{timestamp}-phase3-infrastructure-map-migration-acceptance.json"
    latest = REPORT_DIR / "phase3-infrastructure-map-migration-acceptance.latest.json"
    atomic_write_json(report_path, payload)
    atomic_write_json(latest, payload)
    print(json.dumps({**payload, "report_path": str(report_path)}, ensure_ascii=True, indent=2))
    return 0 if payload["binary_verdict"]["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
