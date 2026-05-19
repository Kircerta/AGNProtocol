#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "reports" / "provider_contract_validation.json"
LOW_RISK_TASK = ROOT / "runtime" / "model_router_examples" / "low_risk_task.json"
DEEPSEEK_FALLBACK_DIR = ROOT / "reports" / "model_router" / "provider_contract.deepseek_fallback"
DEEPSEEK_FALLBACK_OUTPUT = DEEPSEEK_FALLBACK_DIR / "provider_result.json"


def _run(cmd: list[str], *, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
    )
    stdout = str(proc.stdout or "")
    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(stdout) if stdout.strip() else None
    except Exception:
        parsed = None
    return {
        "command": cmd,
        "returncode": int(proc.returncode),
        "stdout": stdout,
        "stderr": str(proc.stderr or ""),
        "parsed": parsed if isinstance(parsed, dict) else {},
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    status_live = _run([sys.executable, "scripts/agent_collaboration.py", "status"])
    route_live = _run([sys.executable, "scripts/agent_collaboration.py", "route", "--from-json-file", str(LOW_RISK_TASK)])
    route_qwen_missing = _run(
        [sys.executable, "scripts/agent_collaboration.py", "route", "--from-json-file", str(LOW_RISK_TASK)],
        extra_env={"QWEN_LOCAL_MODEL": "/tmp/agn_missing_qwen_contract"},
    )
    run_qwen_missing = _run(
        [
            sys.executable,
            "scripts/agent_collaboration.py",
            "run",
            "--from-json-file",
            str(LOW_RISK_TASK),
            "--output",
            str(DEEPSEEK_FALLBACK_DIR),
        ],
        extra_env={"QWEN_LOCAL_MODEL": "/tmp/agn_missing_qwen_contract"},
    )

    status_live_parsed = status_live.get("parsed", {})
    route_live_parsed = route_live.get("parsed", {})
    route_qwen_missing_parsed = route_qwen_missing.get("parsed", {})
    run_qwen_missing_parsed = run_qwen_missing.get("parsed", {})

    qwen_live_available = bool(status_live_parsed.get("qwen_local", {}).get("available"))
    deepseek_live_available = bool(status_live_parsed.get("deepseek", {}).get("available"))
    route_live_provider = str(route_live_parsed.get("selected_provider", ""))
    route_live_notes = [str(item) for item in route_live_parsed.get("operational_notes", [])]

    checks = {
        "live_status_ok": status_live["returncode"] == 0 and bool(status_live_parsed.get("qwen_local")) and deepseek_live_available,
        "live_route_matches_qwen_state": route_live["returncode"] == 0
        and (
            (qwen_live_available and route_live_provider == "qwen_local")
            or (
                not qwen_live_available
                and route_live_provider == "deepseek"
                and any(item.startswith("qwen_local_on_hold:") for item in route_live_notes)
            )
        ),
        "missing_qwen_marks_on_hold": route_qwen_missing["returncode"] == 0 and any(
            str(item).startswith("qwen_local_on_hold:") for item in route_qwen_missing_parsed.get("operational_notes", [])
        ),
        "missing_qwen_falls_back_to_deepseek": run_qwen_missing["returncode"] == 0
        and run_qwen_missing_parsed.get("route_decision", {}).get("selected_provider") == "deepseek",
    }

    summary = {
        "ok": all(checks.values()),
        "checks": checks,
        "artifacts": {
            "report": str(REPORT_PATH),
            "fallback_output": str(DEEPSEEK_FALLBACK_OUTPUT),
        },
    }
    _write_json(REPORT_PATH, summary)
    print(json.dumps({"ok": summary["ok"], "report": str(REPORT_PATH)}, ensure_ascii=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
