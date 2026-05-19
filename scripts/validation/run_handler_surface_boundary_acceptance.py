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
from capability_snapshot import build_capability_snapshot


REPORT_DIR = ROOT / "reports" / "validation"
TEST_COMMAND = [
    "uvx",
    "pytest",
    "tests/test_agn_governed_execution.py",
    "tests/test_handler_cli_isolation.py",
    "tests/test_model_router.py",
    "tests/test_review_orchestrator.py",
    "tests/test_vision_parser.py",
    "tests/test_dispatcher_runtime.py",
    "-q",
]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _run(cmd: list[str], *, timeout_sec: float = 180.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
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


def _blocked_handler_checks() -> dict[str, Any]:
    checks = {
        "model_router": _run([sys.executable, "scripts/model_router.py", "route", "--from-stdin"]),
        "review_orchestrator": _run([sys.executable, "scripts/review_orchestrator.py", "--file", "README.md"]),
        "vision_parser": _run(
            [
                sys.executable,
                "scripts/vision_parser.py",
                "--task-id",
                "acceptance-vision",
                "--image-ref",
                "agn://artifact/" + ("a" * 64),
            ]
        ),
    }
    payload: dict[str, Any] = {}
    for name, completed in checks.items():
        decoded = json.loads(completed.stdout)
        payload[name] = {
            "returncode": int(completed.returncode),
            "error": str(decoded.get("error", "")),
            "override_flag": str(decoded.get("override_flag", "")),
            "recommended_entrypoints": list(decoded.get("recommended_entrypoints", [])),
            "pass": int(completed.returncode) == 2
            and str(decoded.get("error", "")) == "direct_handler_cli_requires_explicit_ack",
        }
    return payload


def _find_sample_png() -> Path:
    candidates = [
        ROOT / "reports" / "agn-browser-use-cli2-smoke.png",
        ROOT / "reports" / "agn-browser-background-x-smoke.png",
        ROOT / "reports" / "browser_use_smoke.png",
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    raise FileNotFoundError("no_sample_png_available_for_handler_surface_acceptance")


def _gateway_live_checks(*, expect_vision_available: bool) -> dict[str, Any]:
    show = _run([sys.executable, "scripts/agn_governed_execution.py", "show"])
    show_payload = json.loads(show.stdout)

    desktop = _run([sys.executable, "scripts/agn_governed_execution.py", "desktop", "--surface", "status"])
    desktop_payload = json.loads(desktop.stdout)

    if expect_vision_available:
        image_path = _find_sample_png()
        vision = _run(
            [
                sys.executable,
                "scripts/agn_governed_execution.py",
                "vision",
                "--task-id",
                "handler-boundary-acceptance-vision",
                "--image-path",
                str(image_path),
            ],
            timeout_sec=240.0,
        )
        vision_payload = json.loads(vision.stdout)
        vision_result = {
            "returncode": int(vision.returncode),
            "ok": bool(vision_payload.get("ok", False)),
            "result_count": len(vision_payload.get("results", [])) if isinstance(vision_payload.get("results", []), list) else 0,
            "registered_input": bool(vision_payload.get("registered_input")),
            "skipped": False,
            "pass": int(vision.returncode) == 0 and bool(vision_payload.get("ok", False)),
        }
    else:
        vision_result = {
            "returncode": 0,
            "ok": False,
            "result_count": 0,
            "registered_input": False,
            "skipped": True,
            "pass": True,
        }

    return {
        "gateway_show": {
            "returncode": int(show.returncode),
            "cli_commands": list(show_payload.get("cli_commands", [])),
            "pass": int(show.returncode) == 0 and "vision" in list(show_payload.get("cli_commands", [])),
        },
        "gateway_desktop_status": {
            "returncode": int(desktop.returncode),
            "ok": bool(desktop_payload.get("ok", False)),
            "surface": str((desktop_payload.get("result", {}) or {}).get("surface", "")),
            "pass": int(desktop.returncode) == 0 and bool(desktop_payload.get("ok", False)),
        },
        "gateway_vision": vision_result,
    }


def _surface_checks() -> dict[str, Any]:
    capability = build_capability_snapshot()
    surfaces = capability.get("surfaces", {}) if isinstance(capability.get("surfaces", {}), dict) else {}
    vision_surface = surfaces.get("vision_parser", {}) if isinstance(surfaces.get("vision_parser", {}), dict) else {}
    review_surface = surfaces.get("flagship_review", {}) if isinstance(surfaces.get("flagship_review", {}), dict) else {}
    gateway_surface = surfaces.get("governed_execution_gateway", {}) if isinstance(surfaces.get("governed_execution_gateway", {}), dict) else {}
    return {
        "vision_entry": str(vision_surface.get("entry", "")),
        "vision_available": bool(vision_surface.get("available", False)),
        "flagship_review_entry": str(review_surface.get("entry", "")),
        "gateway_entry": str(gateway_surface.get("entry", "")),
        "pass": str(vision_surface.get("entry", "")).startswith("python3 scripts/agn_governed_execution.py vision")
        and str(review_surface.get("entry", "")).startswith("python3 scripts/agn2_execution_workflow.py review")
        and bool(gateway_surface.get("available", False)),
    }


def build_report() -> dict[str, Any]:
    tests = _run_tests()
    blocked = _blocked_handler_checks()
    surfaces = _surface_checks()
    live = _gateway_live_checks(expect_vision_available=bool(surfaces["vision_available"]))
    overall_pass = all(
        [
            tests["pass"],
            all(bool(item["pass"]) for item in blocked.values()),
            all(bool(item["pass"]) for item in live.values()),
            bool(surfaces["pass"]),
        ]
    )
    return {
        "schema_version": "agn.validation.handler_surface_boundary.v1",
        "generated_at": utc_now_iso(),
        "objective": "Verify that implementation handler CLIs are explicitly gated while governed gateway entrypoints remain usable.",
        "binary_verdict": {
            "targeted_tests_green": bool(tests["pass"]),
            "model_router_cli_blocked": bool(blocked["model_router"]["pass"]),
            "review_orchestrator_cli_blocked": bool(blocked["review_orchestrator"]["pass"]),
            "vision_parser_cli_blocked": bool(blocked["vision_parser"]["pass"]),
            "gateway_show_ok": bool(live["gateway_show"]["pass"]),
            "gateway_desktop_status_ok": bool(live["gateway_desktop_status"]["pass"]),
            "gateway_vision_ok": bool(live["gateway_vision"]["pass"]),
            "surface_entries_aligned": bool(surfaces["pass"]),
            "overall_pass": bool(overall_pass),
        },
        "counts": {
            "tests_passed": int(tests["passed_count"]),
            "blocked_checks_passed": int(sum(1 for item in blocked.values() if item["pass"])),
            "live_checks_passed": int(sum(1 for item in live.values() if item["pass"])),
        },
        "test_run": tests,
        "blocked_handler_checks": blocked,
        "live_gateway_checks": live,
        "surface_checks": surfaces,
    }


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_report()
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"{timestamp}-handler-surface-boundary-acceptance.json"
    latest = REPORT_DIR / "handler-surface-boundary-acceptance.latest.json"
    atomic_write_json(report_path, payload)
    atomic_write_json(latest, payload)
    print(json.dumps({**payload, "report_path": str(report_path)}, ensure_ascii=True, indent=2))
    return 0 if payload["binary_verdict"]["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
