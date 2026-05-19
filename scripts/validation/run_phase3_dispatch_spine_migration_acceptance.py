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
    "tests/test_event_sourcing.py",
    "tests/test_runtime_bus.py",
    "tests/test_dispatcher_runtime.py",
    "tests/test_package_event_store.py",
    "tests/test_package_bus.py",
    "tests/test_package_dispatcher.py",
    "tests/test_control_daemon.py",
    "tests/test_package_control_daemon.py",
    "tests/test_agn2_system.py",
    "-q",
]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _run(cmd: list[str], *, timeout_sec: float = 240.0, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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
    script_probe = _run(
        [
            sys.executable,
            "-c",
            (
                "import json, event_sourcing as es, runtime_bus as bus, dispatcher_runtime as dr; "
                "print(json.dumps({"
                "'event_store': {'package_path': es.PACKAGE_PATH, 'legacy_script_shim': es.LEGACY_SCRIPT_SHIM}, "
                "'bus': {'package_path': bus.PACKAGE_PATH, 'legacy_script_shim': bus.LEGACY_SCRIPT_SHIM}, "
                "'dispatcher': {'package_path': dr.PACKAGE_PATH, 'legacy_script_shim': dr.LEGACY_SCRIPT_SHIM}"
                "}, ensure_ascii=True))"
            ),
        ],
        extra_env=env,
    )
    script_payload = json.loads(script_probe.stdout)

    package_probe = _run(
        [
            sys.executable,
            "-c",
            (
                "from agn.dispatch import event_store as es, bus, dispatcher as dr; "
                "import json; "
                "print(json.dumps({"
                "'event_store': {'package_path': es.PACKAGE_PATH, 'legacy_script_shim': es.LEGACY_SCRIPT_SHIM}, "
                "'bus': {'package_path': bus.PACKAGE_PATH, 'legacy_script_shim': bus.LEGACY_SCRIPT_SHIM}, "
                "'dispatcher': {'package_path': dr.PACKAGE_PATH, 'legacy_script_shim': dr.LEGACY_SCRIPT_SHIM}"
                "}, ensure_ascii=True))"
            ),
        ],
        extra_env=env,
    )
    package_payload = json.loads(package_probe.stdout)

    event_shim_text = (ROOT / "scripts" / "event_sourcing.py").read_text(encoding="utf-8")
    bus_shim_text = (ROOT / "scripts" / "runtime_bus.py").read_text(encoding="utf-8")
    dispatcher_shim_text = (ROOT / "scripts" / "dispatcher_runtime.py").read_text(encoding="utf-8")
    event_package_text = (ROOT / "src" / "agn" / "dispatch" / "event_store.py").read_text(encoding="utf-8")
    bus_package_text = (ROOT / "src" / "agn" / "dispatch" / "bus.py").read_text(encoding="utf-8")
    dispatcher_package_text = (ROOT / "src" / "agn" / "dispatch" / "dispatcher.py").read_text(encoding="utf-8")
    control_daemon_text = (ROOT / "src" / "agn" / "governance" / "control_daemon.py").read_text(encoding="utf-8")
    execution_gateway_text = (ROOT / "src" / "agn" / "governance" / "execution_gateway.py").read_text(encoding="utf-8")
    system_text = (ROOT / "src" / "agn" / "governance" / "system.py").read_text(encoding="utf-8")

    return {
        "script_surfaces": {
            "returncode": int(script_probe.returncode),
            "pass": int(script_probe.returncode) == 0
            and str(script_payload.get("event_store", {}).get("package_path", "")) == "agn.dispatch.event_store"
            and str(script_payload.get("bus", {}).get("package_path", "")) == "agn.dispatch.bus"
            and str(script_payload.get("dispatcher", {}).get("package_path", "")) == "agn.dispatch.dispatcher",
        },
        "package_surfaces": {
            "returncode": int(package_probe.returncode),
            "pass": int(package_probe.returncode) == 0
            and str(package_payload.get("event_store", {}).get("package_path", "")) == "agn.dispatch.event_store"
            and str(package_payload.get("bus", {}).get("package_path", "")) == "agn.dispatch.bus"
            and str(package_payload.get("dispatcher", {}).get("package_path", "")) == "agn.dispatch.dispatcher",
        },
        "script_shims": {
            "event_store_alias": "from agn.dispatch import event_store as _impl" in event_shim_text and "sys.modules[__name__] = _impl" in event_shim_text,
            "bus_alias": "from agn.dispatch import bus as _impl" in bus_shim_text and "sys.modules[__name__] = _impl" in bus_shim_text,
            "dispatcher_alias": "from agn.dispatch import dispatcher as _impl" in dispatcher_shim_text and "sys.modules[__name__] = _impl" in dispatcher_shim_text,
            "pass": all(
                check in text
                for check, text in (
                    ("from agn.dispatch import event_store as _impl", event_shim_text),
                    ("from agn.dispatch import bus as _impl", bus_shim_text),
                    ("from agn.dispatch import dispatcher as _impl", dispatcher_shim_text),
                )
            )
            and all("sys.modules[__name__] = _impl" in text for text in (event_shim_text, bus_shim_text, dispatcher_shim_text)),
        },
        "package_impls": {
            "event_store_real": "def append_event(" in event_package_text and "def transition_state(" in event_package_text,
            "bus_real": "def publish_message(" in bus_package_text and "def expire_messages(" in bus_package_text,
            "dispatcher_real": "def dispatch_request(" in dispatcher_package_text and "HANDLERS =" in dispatcher_package_text,
            "pass": "def append_event(" in event_package_text
            and "def transition_state(" in event_package_text
            and "def publish_message(" in bus_package_text
            and "def expire_messages(" in bus_package_text
            and "def dispatch_request(" in dispatcher_package_text
            and "HANDLERS =" in dispatcher_package_text,
        },
        "active_imports": {
            "control_daemon_event_store": "from agn.dispatch.event_store import append_event, enqueue_control_command, load_checkpoint, transition_state, write_checkpoint" in control_daemon_text,
            "control_daemon_dispatcher": "from agn.dispatch.dispatcher import dispatch_request" in control_daemon_text,
            "execution_gateway_dispatcher": "from agn.dispatch.dispatcher import dispatch_request" in execution_gateway_text,
            "system_bus": "from agn.dispatch.bus import expire_messages" in system_text,
            "pass": "from agn.dispatch.event_store import append_event, enqueue_control_command, load_checkpoint, transition_state, write_checkpoint" in control_daemon_text
            and "from agn.dispatch.dispatcher import dispatch_request" in control_daemon_text
            and "from agn.dispatch.dispatcher import dispatch_request" in execution_gateway_text
            and "from agn.dispatch.bus import expire_messages" in system_text,
        },
    }


def build_report() -> dict[str, Any]:
    tests = _run_tests()
    checks = _migration_checks()
    overall_pass = tests["pass"] and all(bool(item["pass"]) for item in checks.values())
    return {
        "schema_version": "agn.validation.phase3_dispatch_spine_migration.v1",
        "generated_at": utc_now_iso(),
        "objective": "Verify that AGN's dispatch spine now lives in src/agn/dispatch while the legacy scripts remain compatibility shims and active governance surfaces import the package implementation directly.",
        "binary_verdict": {
            "targeted_tests_green": bool(tests["pass"]),
            "script_surfaces_work": bool(checks["script_surfaces"]["pass"]),
            "package_surfaces_work": bool(checks["package_surfaces"]["pass"]),
            "script_shims_present": bool(checks["script_shims"]["pass"]),
            "package_impls_present": bool(checks["package_impls"]["pass"]),
            "active_runtime_imports_package": bool(checks["active_imports"]["pass"]),
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
    report_path = REPORT_DIR / f"{timestamp}-phase3-dispatch-spine-migration-acceptance.json"
    latest = REPORT_DIR / "phase3-dispatch-spine-migration-acceptance.latest.json"
    atomic_write_json(report_path, payload)
    atomic_write_json(latest, payload)
    print(json.dumps({**payload, "report_path": str(report_path)}, ensure_ascii=True, indent=2))
    return 0 if payload["binary_verdict"]["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
