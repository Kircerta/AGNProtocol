#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn.governance.commands import submit_admin_command
from agn.governance.control_daemon import run_once as control_daemon_run_once
from agn.governance.read_models import refresh_read_models
from dispatcher_runtime import dispatch_request
from emergency_stop import load_system_mode


def main() -> int:
    task_id = f"agn2-consolidation-{uuid4().hex[:8]}"
    trace_id = f"agn2-consolidation-trace-{uuid4().hex[:8]}"

    refresh_read_models()

    gated = dispatch_request(
        {
            "trace_id": trace_id,
            "task_id": task_id,
            "caller": "admin",
            "target": "memory_recorder",
            "target_kind": "memory_recorder",
            "intent": "record_fact",
            "reason": "integration gate exercise",
            "risk_level": "high",
            "input_payload": {
                "kind": "fact",
                "summary": "agn2 consolidation validation",
                "fact_payload": {"phase": "consolidation"},
            },
        }
    )
    gate_id = str((gated.get("result", {}) if isinstance(gated.get("result"), dict) else {}).get("gate_id", "")).strip()

    submit_admin_command(
        {
            "issuer": "admin",
            "command": "APPROVE_GATE",
            "target_type": "gate",
            "target_id": gate_id,
            "reason": "validation approval",
            "trace_id": trace_id,
        }
    )
    approval = control_daemon_run_once(max_commands=20)

    submit_admin_command(
        {
            "issuer": "admin",
            "command": "EMERGENCY_STOP",
            "target_type": "system",
            "target_id": "",
            "reason": "validation stop",
            "trace_id": trace_id,
        }
    )
    stop = control_daemon_run_once(max_commands=20)
    stop_mode = load_system_mode()

    submit_admin_command(
        {
            "issuer": "admin",
            "command": "RELEASE_STOP",
            "target_type": "system",
            "target_id": "",
            "reason": "validation release",
            "trace_id": trace_id,
        }
    )
    release = control_daemon_run_once(max_commands=20)
    read_model = refresh_read_models()
    final_mode = load_system_mode()

    payload = {
        "ok": bool(gated.get("failure_class") == "policy_gate_pending")
        and bool(approval.get("processed", 0) >= 1)
        and bool(stop.get("processed", 0) >= 1)
        and bool(stop_mode.get("emergency_stop_active", False))
        and bool(release.get("processed", 0) >= 1)
        and not bool(final_mode.get("emergency_stop_active", False)),
        "trace_id": trace_id,
        "task_id": task_id,
        "gated_failure_class": gated.get("failure_class", ""),
        "approval_processed": approval.get("processed", 0),
        "stop_processed": stop.get("processed", 0),
        "release_processed": release.get("processed", 0),
        "stop_mode": stop_mode,
        "final_mode": final_mode,
        "read_models": read_model,
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
