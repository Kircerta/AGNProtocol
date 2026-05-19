#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports" / "control_plane_operator_posture"
READ_MODELS_DIR = ROOT / "runtime" / "admin_control" / "read_models"
PENDING_COMMANDS_DIR = ROOT / "runtime" / "admin_control" / "commands" / "pending"
ACKS_DIR = ROOT / "runtime" / "admin_control" / "commands" / "acks"
ROLLBACK_AND_ABORT_SEMANTICS = [
    "Command preview is advisory only; no privileged mutation happens until a formal admin command is actually submitted.",
    "If a formal command fails, re-read canonical read models and inspect command ack or audit trail before compensating.",
    "Do not patch runtime internals directly as a rollback shortcut; abort into Control Plane visibility and escalate when state is unclear.",
]

try:
    from agn.governance.commands import COMMANDS, normalize_admin_command
except ImportError:  # pragma: no cover - package import fallback
    from scripts.admin_command_protocol import COMMANDS, normalize_admin_command

try:
    from capability_snapshot import build_capability_snapshot
except ImportError:  # pragma: no cover - package import fallback
    from scripts.capability_snapshot import build_capability_snapshot


TASK_COMMAND_HINTS = {
    "PAUSE_TASK": ("pause task", "pause workflow"),
    "RESUME_TASK": ("resume task", "resume workflow"),
    "CANCEL_TASK": ("cancel task", "cancel run", "stop this task"),
    "REPRIORITIZE_TASK": ("reprioritize", "change priority", "move priority"),
    "RETRY_TASK": ("retry task", "rerun task", "retry run"),
    "FORCE_ESCALATE_TASK": ("force escalate", "escalate task", "manual escalate"),
    "APPROVE_GATE": ("approve gate", "approve approval", "approve this gate"),
    "REJECT_GATE": ("reject gate", "deny gate"),
    "HOLD_GATE": ("hold gate", "pause gate", "keep gate blocked"),
    "REQUEST_REVIEW": ("request review", "ask reviewer", "flagship review"),
    "ESCALATE_COUNCIL": ("escalate council", "send to council"),
    "EMERGENCY_STOP": ("emergency stop", "panic stop", "stop system"),
    "RELEASE_STOP": ("release stop", "resume system", "release emergency stop"),
    "ISOLATE_AGENT": ("isolate agent", "quarantine agent"),
    "UNISOLATE_AGENT": ("unisolate agent", "restore agent"),
}


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _safe_slug(text: str, *, default: str, max_len: int = 56) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(text or "").strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    cleaned = cleaned.strip("-") or default
    return cleaned[:max_len].rstrip("-") or default


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def infer_signals(task_summary: str) -> dict[str, bool]:
    text = str(task_summary or "").lower()
    return {
        "needs_human_visibility": any(token in text for token in ("control plane", "approval gate", "task board", "human visibility", "admin view", "oversight")),
        "needs_governed_write": any(token in text for token in ("approve", "reject", "hold", "pause", "resume", "cancel", "reprioritize", "retry", "emergency stop", "release stop", "isolate", "formal command")),
        "needs_queue_observation": any(token in text for token in ("queue", "ack", "pending", "policy gate", "dispatcher", "task board", "approval gate", "bus", "council")),
        "needs_system_truth": any(token in text for token in ("status", "capabilities", "read model", "canonical", "lifecycle", "mode", "system truth")),
        "needs_history": any(token in text for token in ("conversation", "history", "trace", "audit", "monitor", "chat log", "message bus")),
    }


def infer_command(task_summary: str) -> str:
    text = str(task_summary or "").lower()
    for command, hints in TASK_COMMAND_HINTS.items():
        if any(hint in text for hint in hints):
            return command
    return ""


def read_model_refs() -> list[dict[str, str]]:
    refs = [
        ("system_status", READ_MODELS_DIR / "system_status.json", "Canonical mode, system health, and lifecycle truth."),
        ("capability_snapshot", READ_MODELS_DIR / "capability_snapshot.json", "Current tools, providers, skills, and execution surfaces."),
        ("execution_discipline", READ_MODELS_DIR / "execution_discipline.json", "Current governance and operator discipline summary."),
        ("task_board", READ_MODELS_DIR / "task_board.json", "Human-visible work queue and task board snapshot."),
        ("approval_gate", READ_MODELS_DIR / "approval_gate.json", "Approval queue and gate state."),
        ("dispatcher", READ_MODELS_DIR / "dispatcher_state.json", "Dispatcher requests, results, and queue posture."),
        ("bus", READ_MODELS_DIR / "bus_state.json", "Message-bus summary for agent coordination state."),
    ]
    return [{"name": name, "path": str(path), "why": why} for name, path, why in refs]


def build_command_preview(*, command: str, target_id: str, reason: str) -> dict[str, Any]:
    if not command:
        return {}
    preview = normalize_admin_command(
        {
            "issuer": "codex",
            "command": command,
            "target_id": target_id,
            "reason": reason or f"Operator posture preview for {command.lower()}",
            "requires_ack": True,
        }
    )
    return preview


def preferred_entry_for_command(command: str) -> dict[str, str]:
    if command == "EMERGENCY_STOP":
        return {
            "surface": "agn2_system",
            "entry": "python3 scripts/agn2_system.py emergency-stop --reason \"<reason>\"",
        }
    if command == "RELEASE_STOP":
        return {
            "surface": "agn2_system",
            "entry": "python3 scripts/agn2_system.py release-stop --reason \"<reason>\"",
        }
    return {
        "surface": "control_plane",
        "entry": f"Control Plane formal command path -> {PENDING_COMMANDS_DIR}",
    }


def choose_primary_surface(*, signals: dict[str, bool], command: str) -> str:
    if command or signals["needs_governed_write"]:
        return "formal_command_path"
    if signals["needs_human_visibility"]:
        return "control_plane"
    if signals["needs_queue_observation"] or signals["needs_system_truth"]:
        return "read_model"
    if signals["needs_history"]:
        return "conversation_monitor"
    return "ghostty_or_terminal"


def surface_sequence(*, capability: dict[str, Any], signals: dict[str, bool], command: str) -> list[dict[str, str]]:
    surfaces = capability.get("surfaces", {})
    sequence: list[dict[str, str]] = [
        {
            "surface": "lifecycle",
            "entry": str((surfaces.get("lifecycle") or {}).get("entry", "python3 scripts/agn2_system.py status")),
            "why": "Start from lifecycle truth before choosing any operator lane.",
        }
    ]
    if signals["needs_system_truth"] or signals["needs_queue_observation"] or signals["needs_human_visibility"] or command:
        sequence.append(
            {
                "surface": "read_model",
                "entry": "python3 scripts/control_plane_read_model.py refresh",
                "why": "Refresh canonical read models before human-visible monitoring or governed action.",
            }
        )
    if signals["needs_human_visibility"] or signals["needs_queue_observation"] or command:
        control_plane_entry = str((surfaces.get("control_plane") or {}).get("entry", "open '/Applications/AGN2.0 Control Plane.app'"))
        sequence.append(
            {
                "surface": "control_plane",
                "entry": control_plane_entry,
                "why": "Use the formal human surface when the task affects task boards, approval gates, or human oversight.",
            }
        )
    if signals["needs_history"]:
        monitor_entry = str((surfaces.get("conversation_monitor") or {}).get("entry", "open '/Applications/AGN Conversation Monitor.app'"))
        sequence.append(
            {
                "surface": "conversation_monitor",
                "entry": monitor_entry,
                "why": "Use the language-layer observation surface when evidence is conversational or trace-like.",
            }
        )
    if command or signals["needs_governed_write"]:
        preferred = preferred_entry_for_command(command)
        sequence.append(
            {
                "surface": "formal_command_path",
                "entry": preferred["entry"],
                "why": "Privileged mutation belongs in the formal command lane, never in ad hoc shell state edits.",
            }
        )
    if len(sequence) == 1:
        sequence.append(
            {
                "surface": "ghostty_or_terminal",
                "entry": "Use Ghostty or the terminal only after lifecycle truth is clear.",
                "why": "This task shape does not require control-plane or command-path escalation.",
            }
        )
    return sequence


def build_payload(
    *,
    task_summary: str,
    risk_level: str,
    explicit_flags: dict[str, bool],
    command: str,
    target_id: str,
    reason: str,
) -> dict[str, Any]:
    inferred = infer_signals(task_summary)
    effective = {key: bool(explicit_flags.get(key) or inferred.get(key)) for key in inferred}
    effective_command = str(command).strip().upper() or infer_command(task_summary)
    if effective_command and effective_command in COMMANDS:
        effective["needs_governed_write"] = True
    capability = build_capability_snapshot()
    primary = choose_primary_surface(signals=effective, command=effective_command)
    preview = build_command_preview(command=effective_command, target_id=target_id, reason=reason)
    preferred_entry = preferred_entry_for_command(effective_command) if effective_command else {}
    notes = [
        "Use read models when you need canonical truth; do not trust local shell impressions for governance state.",
        "Use the Control Plane when the operator needs a visible monitoring or approval surface.",
        "Use the formal command path for privileged mutation or approval actions; never mutate runtime state directly.",
    ]
    if effective["needs_history"]:
        notes.append("Conversation Monitor is the right observation surface when the evidence is trace or language-layer state.")
    if not effective["needs_governed_write"]:
        notes.append("If the task stays read-only, prefer read models and status over writing any command envelope.")
    return {
        "ok": True,
        "generated_at": utc_now_iso(),
        "task_summary": task_summary,
        "risk_level": risk_level,
        "inferred_signals": inferred,
        "effective_signals": effective,
        "primary_surface": primary,
        "surface_sequence": surface_sequence(capability=capability, signals=effective, command=effective_command),
        "read_model_refs": read_model_refs(),
        "formal_command_path": {
            "required": bool(effective["needs_governed_write"] or effective_command),
            "command": effective_command,
            "preferred_entry": preferred_entry,
            "pending_dir": str(PENDING_COMMANDS_DIR),
            "ack_dir": str(ACKS_DIR),
            "envelope_preview": preview,
        },
        "rollback_and_abort_semantics": ROLLBACK_AND_ABORT_SEMANTICS,
        "notes": notes,
    }


def _write_report(payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = _safe_slug(str(payload.get("task_summary", "")), default="control-plane")
    path = REPORT_DIR / f"{timestamp}-{slug}.json"
    latest = REPORT_DIR / "latest.json"
    _atomic_write_json(path, payload)
    _atomic_write_json(latest, payload)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Decide when AGN work should switch to Control Plane, read models, or the formal admin command path.")
    parser.add_argument("--task-summary", required=True)
    parser.add_argument("--risk-level", choices=["low", "medium", "high"], default="low")
    parser.add_argument("--needs-human-visibility", action="store_true")
    parser.add_argument("--needs-governed-write", action="store_true")
    parser.add_argument("--needs-queue-observation", action="store_true")
    parser.add_argument("--needs-system-truth", action="store_true")
    parser.add_argument("--needs-history", action="store_true")
    parser.add_argument("--command", choices=sorted(COMMANDS), default="")
    parser.add_argument("--target-id", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()

    payload = build_payload(
        task_summary=str(args.task_summary).strip(),
        risk_level=str(args.risk_level).strip().lower(),
        explicit_flags={
            "needs_human_visibility": bool(args.needs_human_visibility),
            "needs_governed_write": bool(args.needs_governed_write),
            "needs_queue_observation": bool(args.needs_queue_observation),
            "needs_system_truth": bool(args.needs_system_truth),
            "needs_history": bool(args.needs_history),
        },
        command=str(args.command).strip().upper(),
        target_id=str(args.target_id).strip(),
        reason=str(args.reason).strip(),
    )
    if not args.no_write:
        payload["report_path"] = str(_write_report(payload))
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
