"""AGN admin command protocol.

This is the real package implementation for AGN's formal admin command model.
The legacy script remains only as a compatibility shim.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from agn.core.admin_control import (
    append_admin_audit,
    append_jsonl,
    atomic_write_json,
    command_acks_dir,
    command_done_dir,
    command_failed_dir,
    command_index_path,
    command_pending_dir,
    ensure_admin_dirs,
    load_json,
    safe_name,
    utc_now_iso,
)


PACKAGE_PATH = "agn.governance.commands"
LEGACY_SCRIPT_SHIM = "scripts/admin_command_protocol.py"

COMMANDS = {
    "PAUSE_TASK",
    "RESUME_TASK",
    "CANCEL_TASK",
    "REPRIORITIZE_TASK",
    "RETRY_TASK",
    "FORCE_ESCALATE_TASK",
    "APPROVE_GATE",
    "REJECT_GATE",
    "HOLD_GATE",
    "REQUEST_REVIEW",
    "ESCALATE_COUNCIL",
    "EMERGENCY_STOP",
    "RELEASE_STOP",
    "ISOLATE_AGENT",
    "UNISOLATE_AGENT",
}
TARGET_TYPES = {"task", "gate", "agent", "system", "council"}
ACK_STATUSES = {"executed", "rejected", "failed", "queued_for_approval", "held"}
RISK_OVERRIDES = {"none", "acknowledged", "emergency"}


def _command_path(command_id: str) -> Path:
    return command_pending_dir() / f"{safe_name(command_id, default='command')}.json"


def _done_command_path(command_id: str) -> Path:
    return command_done_dir() / f"{safe_name(command_id, default='command')}.json"


def _failed_command_path(command_id: str) -> Path:
    return command_failed_dir() / f"{safe_name(command_id, default='command')}.json"


def _ack_path(command_id: str) -> Path:
    stamp = utc_now_iso().replace(":", "").replace("-", "")
    return command_acks_dir() / f"{safe_name(command_id, default='command')}.{stamp}.json"


def validate_admin_command(raw: dict[str, Any]) -> list[str]:
    if not isinstance(raw, dict):
        return ["command_must_be_object"]
    errors: list[str] = []
    for key in ("issuer", "command", "reason"):
        if not str(raw.get(key, "")).strip():
            errors.append(f"missing:{key}")
    command = str(raw.get("command", "")).strip().upper()
    if command and command not in COMMANDS:
        errors.append("invalid:command")
    target_type = str(raw.get("target_type", "")).strip().lower()
    if target_type and target_type not in TARGET_TYPES:
        errors.append("invalid:target_type")
    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        errors.append("invalid:payload")
    approval_context = raw.get("approval_context", {})
    if not isinstance(approval_context, dict):
        errors.append("invalid:approval_context")
    risk_override = str(raw.get("risk_override", "none")).strip().lower() or "none"
    if risk_override not in RISK_OVERRIDES:
        errors.append("invalid:risk_override")
    if command in {"EMERGENCY_STOP", "RELEASE_STOP"}:
        if target_type and target_type != "system":
            errors.append("invalid:target_type_for_system_command")
    elif command.endswith("_TASK"):
        if target_type and target_type != "task":
            errors.append("invalid:target_type_for_task_command")
    return errors


def normalize_admin_command(raw: dict[str, Any]) -> dict[str, Any]:
    command = str(raw.get("command", "")).strip().upper()
    target_type = str(raw.get("target_type", "")).strip().lower()
    if not target_type:
        if command in {"EMERGENCY_STOP", "RELEASE_STOP"}:
            target_type = "system"
        elif command.endswith("_TASK"):
            target_type = "task"
        elif command.endswith("_GATE"):
            target_type = "gate"
        elif "COUNCIL" in command:
            target_type = "council"
    return {
        "command_id": str(raw.get("command_id", "")).strip() or f"cmd-{uuid4().hex[:12]}",
        "timestamp": str(raw.get("timestamp", "")).strip() or utc_now_iso(),
        "issuer": str(raw.get("issuer", "")).strip(),
        "command": command,
        "target_type": target_type,
        "target_id": str(raw.get("target_id", "")).strip(),
        "reason": str(raw.get("reason", "")).strip(),
        "trace_id": str(raw.get("trace_id", "")).strip(),
        "payload": raw.get("payload", {}) if isinstance(raw.get("payload"), dict) else {},
        "requires_ack": bool(raw.get("requires_ack", True)),
        "risk_override": str(raw.get("risk_override", "none")).strip().lower() or "none",
        "approval_context": raw.get("approval_context", {}) if isinstance(raw.get("approval_context"), dict) else {},
    }


def submit_admin_command(raw: dict[str, Any]) -> dict[str, Any]:
    ensure_admin_dirs()
    payload = normalize_admin_command(raw)
    errors = validate_admin_command(payload)
    if errors:
        raise ValueError("invalid_admin_command:" + ",".join(errors))
    path = _command_path(payload["command_id"])
    if path.exists():
        raise ValueError(f"command_id_exists:{payload['command_id']}")
    atomic_write_json(path, payload)
    append_jsonl(
        command_index_path(),
        {
            "kind": "command",
            "ts": payload["timestamp"],
            "command_id": payload["command_id"],
            "issuer": payload["issuer"],
            "command": payload["command"],
            "target_type": payload["target_type"],
            "target_id": payload["target_id"],
            "trace_id": payload["trace_id"],
        },
    )
    append_admin_audit(
        "admin_command_submitted",
        command_id=payload["command_id"],
        command=payload["command"],
        issuer=payload["issuer"],
        trace_id=payload["trace_id"],
    )
    return payload


def load_admin_command(command_id: str) -> dict[str, Any] | None:
    target = _command_path(command_id)
    if target.exists():
        return load_json(target)
    target = _done_command_path(command_id)
    if target.exists():
        return load_json(target)
    target = _failed_command_path(command_id)
    if target.exists():
        return load_json(target)
    return None


def list_pending_admin_commands() -> list[Path]:
    ensure_admin_dirs()
    return sorted(command_pending_dir().glob("*.json"))


def load_command_payload(path: Path) -> dict[str, Any]:
    return load_json(path)


def move_command(path: Path, *, status: str) -> Path:
    ensure_admin_dirs()
    if status == "done":
        target = _done_command_path(path.stem)
    else:
        target = _failed_command_path(path.stem)
    if not path.exists():
        return target
    path.replace(target)
    return target


def ack_admin_command(
    command_id: str,
    *,
    actor: str,
    status: str,
    note: str = "",
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_admin_dirs()
    if status not in ACK_STATUSES:
        raise ValueError(f"invalid_ack_status:{status}")
    command = load_admin_command(command_id)
    if command is None:
        raise ValueError(f"command_not_found:{command_id}")
    ack = {
        "command_id": command_id,
        "ts": utc_now_iso(),
        "actor": str(actor).strip() or "control_daemon",
        "status": status,
        "note": str(note).strip(),
        "trace_id": str(command.get("trace_id", "")).strip(),
        "result": result or {},
    }
    atomic_write_json(_ack_path(command_id), ack)
    append_jsonl(
        command_index_path(),
        {
            "kind": "ack",
            "ts": ack["ts"],
            "command_id": command_id,
            "status": status,
            "actor": ack["actor"],
            "trace_id": ack["trace_id"],
        },
    )
    append_admin_audit(
        "admin_command_acked",
        command_id=command_id,
        status=status,
        actor=ack["actor"],
        trace_id=ack["trace_id"],
    )
    return ack


__all__ = [
    "ACK_STATUSES",
    "COMMANDS",
    "LEGACY_SCRIPT_SHIM",
    "PACKAGE_PATH",
    "RISK_OVERRIDES",
    "TARGET_TYPES",
    "ack_admin_command",
    "list_pending_admin_commands",
    "load_admin_command",
    "load_command_payload",
    "move_command",
    "normalize_admin_command",
    "submit_admin_command",
    "validate_admin_command",
]
