"""AGN admin control daemon.

This is the real package implementation for the governed admin command daemon.
The legacy script remains only as a CLI compatibility shim.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn_api.ssot_store import SSOTStore
from agn.governance.read_models import refresh_read_models

from agn.core.admin_control import append_admin_audit, atomic_write_json, isolated_agents_path, load_json, repo_root
from agn.core.constitution import issuer_is_authorized, load_constitution
from agn.core.emergency_stop import activate_emergency_stop, is_emergency_stop_active, release_emergency_stop
from agn.core.policy_gate import decide_gate, load_gate_entry
from agn.dispatch.dispatcher import dispatch_request
from agn.dispatch.event_store import append_event, enqueue_control_command, load_checkpoint, transition_state, write_checkpoint
from agn.governance.commands import (
    ack_admin_command,
    list_pending_admin_commands,
    load_command_payload,
    move_command,
    validate_admin_command,
)
from agn.governance.council import create_council_case


PACKAGE_PATH = "agn.governance.control_daemon"
LEGACY_SCRIPT_SHIM = "scripts/control_daemon.py"


def _root() -> Path:
    return repo_root()


def _ssot_dir() -> Path:
    return _root() / "ssot"


def _load_isolated_agents() -> dict[str, Any]:
    return load_json(isolated_agents_path(), default={"isolated_agents": []})


def _save_isolated_agents(payload: dict[str, Any]) -> None:
    atomic_write_json(isolated_agents_path(), payload)


def _task_trace(task_id: str, explicit_trace_id: str = "") -> str:
    if explicit_trace_id:
        return explicit_trace_id
    checkpoint = load_checkpoint(task_id) or {}
    return str(checkpoint.get("trace_id", "")).strip()


def _ack_and_move(path: Path, *, status: str, note: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = load_command_payload(path)
    command_id = str(payload.get("command_id", path.stem)).strip()
    ack = ack_admin_command(command_id, actor="control_daemon", status=status, note=note, result=result or {})
    move_command(path, status="done" if status in {"executed", "queued_for_approval", "held"} else "failed")
    return ack


def _reject(path: Path, note: str, *, result: dict[str, Any] | None = None) -> dict[str, Any]:
    return _ack_and_move(path, status="rejected", note=note, result=result)


def _execute_task_control(command: dict[str, Any]) -> dict[str, Any]:
    task_id = str(command.get("target_id", "")).strip()
    trace_id = _task_trace(task_id, str(command.get("trace_id", "")).strip())
    payload = {
        "control_id": str(command.get("command_id", "")).strip(),
        "task_id": task_id,
        "payload": dict(command.get("payload", {})),
    }
    mapping = {
        "PAUSE_TASK": "PAUSE",
        "RESUME_TASK": "RESUME",
        "CANCEL_TASK": "STOP",
    }
    command_name = str(command.get("command", "")).strip().upper()
    if command_name in mapping:
        payload["control_type"] = mapping[command_name]
        enqueue_control_command(payload)
        if trace_id and task_id:
            append_event(trace_id=trace_id, task_id=task_id, event_type="ADMIN_COMMAND_EXECUTED", payload={"command": command_name})
        return {"ok": True, "enqueued_control": payload["control_type"], "task_id": task_id, "trace_id": trace_id}
    if command_name == "REPRIORITIZE_TASK":
        store = SSOTStore(_ssot_dir())
        with store.locked_update(task_id) as task:
            if task is None:
                raise ValueError(f"task_not_found:{task_id}")
            task["priority"] = str((command.get("payload", {}) or {}).get("priority", "")).strip() or "normal"
        if trace_id and task_id:
            append_event(trace_id=trace_id, task_id=task_id, event_type="ADMIN_TASK_REPRIORITIZED", payload={"priority": task["priority"]})
        return {"ok": True, "task_id": task_id, "priority": task["priority"]}
    if command_name == "RETRY_TASK":
        checkpoint = load_checkpoint(task_id) or {"task_id": task_id, "trace_id": trace_id or f"trace-{task_id}"}
        checkpoint["paused"] = False
        checkpoint["awaiting_admin_response"] = False
        checkpoint["admin_hold_reason"] = ""
        checkpoint["state"] = "PLANNED"
        checkpoint["state_reason"] = "admin retry requested"
        write_checkpoint(task_id, checkpoint)
        if checkpoint.get("trace_id"):
            append_event(trace_id=str(checkpoint.get("trace_id", "")).strip(), task_id=task_id, event_type="ADMIN_TASK_RETRY_REQUESTED", payload={})
        return {"ok": True, "task_id": task_id, "state": "PLANNED"}
    if command_name == "FORCE_ESCALATE_TASK":
        if trace_id and task_id:
            success, reason = transition_state(trace_id=trace_id, task_id=task_id, to_state="NEED_ADMIN", reason="admin force escalate")
            if success:
                append_event(trace_id=trace_id, task_id=task_id, event_type="ADMIN_FORCE_ESCALATE", payload={})
                return {"ok": True, "task_id": task_id, "state": "NEED_ADMIN"}
            checkpoint = load_checkpoint(task_id) or {"task_id": task_id, "trace_id": trace_id}
            checkpoint["state"] = "NEED_ADMIN"
            checkpoint["state_reason"] = f"admin force escalate fallback:{reason}"
            write_checkpoint(task_id, checkpoint)
            append_event(trace_id=trace_id, task_id=task_id, event_type="ADMIN_FORCE_ESCALATE", payload={"fallback_reason": reason})
            return {"ok": True, "task_id": task_id, "state": "NEED_ADMIN", "fallback_reason": reason}
        return {"ok": False, "error": "missing_trace_id"}
    raise ValueError(f"unsupported_task_command:{command_name}")


def _execute_gate_command(command: dict[str, Any]) -> dict[str, Any]:
    gate_id = str(command.get("target_id", "")).strip()
    gate = load_gate_entry(gate_id)
    if gate is None:
        raise ValueError(f"gate_not_found:{gate_id}")
    command_name = str(command.get("command", "")).strip().upper()
    decision_map = {
        "APPROVE_GATE": "approved",
        "REJECT_GATE": "rejected",
        "HOLD_GATE": "held",
        "REQUEST_REVIEW": "review_requested",
    }
    if command_name in decision_map:
        if command_name == "APPROVE_GATE" and is_emergency_stop_active():
            raise ValueError("emergency_stop_blocks_gate_approval")
        decision = decide_gate(gate_id, decision=decision_map[command_name], decided_by=str(command.get("issuer", "")).strip(), note=str(command.get("reason", "")).strip())
        if decision["decision"] == "approved":
            request = load_json(Path(str(gate.get("request_ref", "")).strip()))
            resumed = dispatch_request(
                {
                    **request,
                    "approval_context": {
                        "gate_id": gate_id,
                        "decision": "approved",
                        "approved_by": str(command.get("issuer", "")).strip(),
                    },
                }
            )
            return {"ok": bool(resumed.get("ok", False)), "gate_id": gate_id, "dispatch_result": resumed}
        return {"ok": True, "gate_id": gate_id, "decision": decision["decision"]}
    if command_name == "ESCALATE_COUNCIL":
        case = create_council_case(
            {
                "trace_id": str(gate.get("trace_id", "")).strip(),
                "task_id": str(gate.get("task_id", "")).strip(),
                "trigger": "policy_gate_escalation",
                "reason": str(command.get("reason", "")).strip() or str(gate.get("reason", "")).strip(),
                "input_refs": [str(gate.get("request_ref", "")).strip()],
                "reviewers": (command.get("payload", {}) or {}).get("reviewers", ["gemini", "deepseek", "codex"]),
            }
        )
        decide_gate(
            gate_id,
            decision="council_escalated",
            decided_by=str(command.get("issuer", "")).strip(),
            note=str(command.get("reason", "")).strip(),
            council_case_id=str(case.get("case_id", "")).strip(),
        )
        return {"ok": True, "gate_id": gate_id, "case_id": case["case_id"]}
    raise ValueError(f"unsupported_gate_command:{command_name}")


def _execute_system_command(command: dict[str, Any]) -> dict[str, Any]:
    name = str(command.get("command", "")).strip().upper()
    issuer = str(command.get("issuer", "")).strip()
    reason = str(command.get("reason", "")).strip()
    trace_id = str(command.get("trace_id", "")).strip()
    if name == "EMERGENCY_STOP":
        return {"ok": True, "system_mode": activate_emergency_stop(issuer=issuer, reason=reason, trace_id=trace_id)}
    if name == "RELEASE_STOP":
        return {"ok": True, "system_mode": release_emergency_stop(issuer=issuer, reason=reason, trace_id=trace_id)}
    raise ValueError(f"unsupported_system_command:{name}")


def _execute_agent_command(command: dict[str, Any]) -> dict[str, Any]:
    payload = _load_isolated_agents()
    current = [str(item).strip() for item in payload.get("isolated_agents", []) if str(item).strip()]
    target = str(command.get("target_id", "")).strip()
    name = str(command.get("command", "")).strip().upper()
    if name == "ISOLATE_AGENT":
        if target and target not in current:
            current.append(target)
    elif name == "UNISOLATE_AGENT":
        current = [item for item in current if item != target]
    else:
        raise ValueError(f"unsupported_agent_command:{name}")
    updated = {"isolated_agents": sorted(set(current))}
    _save_isolated_agents(updated)
    return {"ok": True, **updated}


def execute_admin_command(path: Path) -> dict[str, Any]:
    command = load_command_payload(path)
    errors = validate_admin_command(command)
    if errors:
        return _reject(path, "schema_invalid", result={"errors": errors})
    issuer = str(command.get("issuer", "")).strip()
    if not issuer_is_authorized(issuer, load_constitution()):
        return _reject(path, "issuer_not_authorized")
    if is_emergency_stop_active() and str(command.get("command", "")).strip().upper() in {"RESUME_TASK", "RETRY_TASK", "APPROVE_GATE"}:
        return _reject(path, "emergency_stop_active")

    # Authorized admin commands receive a short-lived override nonce so they can
    # mutate constitution-protected state without leaving a reusable bypass.
    from uuid import uuid4

    nonce = uuid4().hex
    nonce_path = _root() / "runtime" / "admin_control" / ".override_nonce"
    nonce_path.parent.mkdir(parents=True, exist_ok=True)
    nonce_path.write_text(nonce, encoding="utf-8")
    os.environ["AGN_ADMIN_OVERRIDE"] = nonce
    try:
        target_type = str(command.get("target_type", "")).strip().lower()
        if target_type == "task":
            result = _execute_task_control(command)
        elif target_type == "gate":
            result = _execute_gate_command(command)
        elif target_type == "system":
            result = _execute_system_command(command)
        elif target_type == "agent":
            result = _execute_agent_command(command)
        else:
            raise ValueError(f"unsupported_target_type:{target_type}")
        append_admin_audit(
            "admin_command_executed",
            command_id=str(command.get("command_id", "")).strip(),
            command=str(command.get("command", "")).strip(),
            trace_id=str(command.get("trace_id", "")).strip(),
        )
        refresh_read_models()
        return _ack_and_move(path, status="executed", note="executed", result=result)
    except Exception as exc:
        refresh_read_models()
        return _ack_and_move(path, status="failed", note=f"{type(exc).__name__}:{exc}", result={"error": str(exc)})
    finally:
        os.environ.pop("AGN_ADMIN_OVERRIDE", None)
        if nonce_path.exists():
            nonce_path.unlink(missing_ok=True)


def run_once(*, max_commands: int = 20) -> dict[str, Any]:
    processed: list[dict[str, Any]] = []
    for path in list_pending_admin_commands()[: max(1, int(max_commands))]:
        processed.append(execute_admin_command(path))
    if processed:
        refresh_read_models()
    return {"ok": True, "processed": len(processed), "acks": processed}


def run_loop(*, interval: float = 2.0, max_commands: int = 20) -> None:
    """Continuously poll the admin command queue and process governed commands."""
    import signal
    import time

    append_admin_audit("control_daemon_loop_started", interval=interval, max_commands=max_commands)
    print(json.dumps({"event": "control_daemon_loop_started", "interval": interval, "max_commands": max_commands}))

    running = True

    def _handle_signal(signum: int, _frame: Any) -> None:
        nonlocal running
        running = False
        append_admin_audit("control_daemon_loop_stopping", signal=signum)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    cycle = 0
    while running:
        cycle += 1
        try:
            result = run_once(max_commands=max_commands)
            processed_count = result.get("processed", 0)
            if processed_count:
                print(json.dumps({"event": "cycle_processed", "cycle": cycle, "processed": processed_count}))
        except Exception as exc:
            print(json.dumps({"event": "cycle_error", "cycle": cycle, "error": str(exc)}))
            append_admin_audit("control_daemon_cycle_error", cycle=cycle, error=str(exc))
        try:
            time.sleep(max(0.5, float(interval)))
        except (KeyboardInterrupt, SystemExit):
            running = False

    append_admin_audit("control_daemon_loop_stopped", cycles_completed=cycle)
    print(json.dumps({"event": "control_daemon_loop_stopped", "cycles_completed": cycle}))


def main() -> int:
    parser = argparse.ArgumentParser(description="AGN2.0 admin control daemon")
    sub = parser.add_subparsers(dest="command", required=True)

    once_parser = sub.add_parser("run-once", help="Process pending commands once and exit")
    once_parser.add_argument("--max-commands", type=int, default=20)

    loop_parser = sub.add_parser("run-loop", help="Persistent control daemon loop")
    loop_parser.add_argument("--interval", type=float, default=2.0, help="Seconds between poll cycles")
    loop_parser.add_argument("--max-commands", type=int, default=20)

    args = parser.parse_args()
    if args.command == "run-once":
        print(json.dumps(run_once(max_commands=args.max_commands), ensure_ascii=True))
        return 0
    if args.command == "run-loop":
        run_loop(interval=args.interval, max_commands=args.max_commands)
        return 0
    return 1


__all__ = [
    "LEGACY_SCRIPT_SHIM",
    "PACKAGE_PATH",
    "execute_admin_command",
    "main",
    "run_loop",
    "run_once",
]
