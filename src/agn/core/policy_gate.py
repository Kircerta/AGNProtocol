"""AGN policy-gate rules and queue helpers.

This is the real package implementation for AGN's dispatch policy-gate
evaluation, gate queue persistence, and approval-decision tracking.
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
    ensure_admin_dirs,
    governance_root,
    load_json,
    policy_gate_decisions_dir,
    policy_gate_index_path,
    policy_gate_queue_dir,
    safe_name,
    utc_now_iso,
)


PACKAGE_PATH = "agn.core.policy_gate"
LEGACY_SCRIPT_SHIM = "scripts/policy_gate.py"

WRITE_ACTION_TYPES = {"TERMINAL_SPAWN", "TERMINAL_INPUT", "TERMINAL_SEND_KEY"}

DEFAULT_POLICY: dict[str, Any] = {
    "version": "agn2.0-2026-03-13",
    "rules": [
        {
            "id": "desktop_write_phase1_gate",
            "match": {
                "target_kind": ["desktop_adapter"],
                "action_type": sorted(WRITE_ACTION_TYPES),
            },
            "decision": "queue_for_approval",
            "risk_level": "high",
            "requires_audit_refs": True,
            "council_required": False,
        },
        {
            "id": "high_risk_dispatch_gate",
            "match": {
                "risk_level": ["high"],
            },
            "decision": "queue_for_approval",
            "risk_level": "high",
            "requires_audit_refs": False,
            "council_required": True,
        },
    ],
}


def load_policy_gate(path: Path | None = None) -> dict[str, Any]:
    target = path or (governance_root() / "policy_gate.json")
    payload = load_json(target, default=DEFAULT_POLICY)
    rules = payload.get("rules", [])
    if not isinstance(rules, list) or not rules:
        return dict(DEFAULT_POLICY)
    return payload


def _request_action_type(request: dict[str, Any]) -> str:
    payload = request.get("input_payload", {})
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("action_type", "")).strip().upper()


def evaluate_dispatch_request(request: dict[str, Any]) -> dict[str, Any]:
    action_type = _request_action_type(request)
    target_kind = str(request.get("target_kind", "")).strip()
    if action_type in WRITE_ACTION_TYPES and target_kind != "desktop_adapter":
        return {
            "requires_gate": True,
            "rule_id": "builtin_write_action_type",
            "decision": "queue_for_approval",
            "council_required": False,
            "requires_audit_refs": True,
            "action_type": action_type,
            "target_kind": target_kind,
        }
    side_effect = str(request.get("side_effect_level", "")).strip().lower()
    if side_effect in {"destructive", "irreversible"}:
        return {
            "requires_gate": True,
            "rule_id": "builtin_destructive_side_effect",
            "decision": "queue_for_approval",
            "council_required": False,
            "requires_audit_refs": True,
            "action_type": action_type,
        }
    summary = str(request.get("request_summary", "")).lower()
    danger_keywords = {
        "rm -rf",
        "delete all",
        "drop table",
        "format disk",
        "constitution",
        "emergency_stop",
        "self-elevat",
        "credential",
        "exfiltrat",
        "ransomware",
        "curl.*bash",
        "wget.*sh",
        "eval(",
        "exec(",
        "os.system",
        "run the script",
        "run this script",
        "execute the script",
        "chmod +x",
        "sudo ",
        "/bin/bash",
        "/bin/sh",
        "keylogger",
        "payload.sh",
        "payload.py",
        "inject",
        "reverse shell",
    }
    if any(keyword in summary for keyword in danger_keywords):
        return {
            "requires_gate": True,
            "rule_id": "builtin_dangerous_keyword",
            "decision": "queue_for_approval",
            "council_required": False,
            "requires_audit_refs": True,
            "action_type": action_type,
        }
    policy = load_policy_gate()
    rules = policy.get("rules", [])
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        match = rule.get("match", {})
        if not isinstance(match, dict):
            continue
        target_kinds = match.get("target_kind", [])
        if isinstance(target_kinds, list) and target_kinds:
            if str(request.get("target_kind", "")).strip() not in {str(item).strip() for item in target_kinds}:
                continue
        action_types = match.get("action_type", [])
        if isinstance(action_types, list) and action_types:
            if action_type not in {str(item).strip().upper() for item in action_types}:
                continue
        risk_levels = match.get("risk_level", [])
        if isinstance(risk_levels, list) and risk_levels:
            if str(request.get("risk_level", "")).strip().lower() not in {
                str(item).strip().lower() for item in risk_levels
            }:
                continue
        return {
            "requires_gate": True,
            "rule_id": str(rule.get("id", "")).strip(),
            "decision": str(rule.get("decision", "queue_for_approval")).strip(),
            "council_required": bool(rule.get("council_required", False)),
            "requires_audit_refs": bool(rule.get("requires_audit_refs", False)),
            "action_type": action_type,
        }
    return {
        "requires_gate": False,
        "rule_id": "",
        "decision": "allow",
        "council_required": False,
        "requires_audit_refs": False,
        "action_type": action_type,
    }


def _gate_path(gate_id: str) -> Path:
    return policy_gate_queue_dir() / f"{safe_name(gate_id, default='gate')}.json"


def _decision_path(gate_id: str) -> Path:
    stamp = utc_now_iso().replace(":", "").replace("-", "")
    return policy_gate_decisions_dir() / f"{safe_name(gate_id, default='gate')}.{stamp}.json"


def create_gate_entry(*, request: dict[str, Any], request_ref: str, evaluation: dict[str, Any]) -> dict[str, Any]:
    ensure_admin_dirs()
    gate_id = f"gate-{uuid4().hex[:12]}"
    gate_path = _gate_path(gate_id)
    payload = {
        "gate_id": gate_id,
        "created_at": utc_now_iso(),
        "trace_id": str(request.get("trace_id", "")).strip(),
        "task_id": str(request.get("task_id", "")).strip(),
        "caller": str(request.get("caller", "")).strip(),
        "target": str(request.get("target", "")).strip(),
        "target_kind": str(request.get("target_kind", "")).strip(),
        "intent": str(request.get("intent", "")).strip(),
        "reason": str(request.get("reason", "")).strip(),
        "risk_level": str(request.get("risk_level", "medium")).strip(),
        "request_ref": str(request_ref).strip(),
        "policy_rule_id": str(evaluation.get("rule_id", "")).strip(),
        "action_type": str(evaluation.get("action_type", "")).strip(),
        "requires_audit_refs": bool(evaluation.get("requires_audit_refs", False)),
        "council_required": bool(evaluation.get("council_required", False)),
        "summary": f"{request.get('intent', '')} blocked by policy gate",
        "gate_ref": str(gate_path),
    }
    atomic_write_json(gate_path, payload)
    append_jsonl(
        policy_gate_index_path(),
        {
            "kind": "gate_created",
            "ts": payload["created_at"],
            "gate_id": gate_id,
            "trace_id": payload["trace_id"],
            "task_id": payload["task_id"],
            "policy_rule_id": payload["policy_rule_id"],
            "target_kind": payload["target_kind"],
        },
    )
    append_admin_audit(
        "policy_gate_created",
        gate_id=gate_id,
        trace_id=payload["trace_id"],
        task_id=payload["task_id"],
        policy_rule_id=payload["policy_rule_id"],
    )
    return payload


def load_gate_entry(gate_id: str) -> dict[str, Any] | None:
    path = _gate_path(gate_id)
    if not path.exists():
        return None
    return load_json(path)


def list_gate_entries() -> list[dict[str, Any]]:
    ensure_admin_dirs()
    items: list[dict[str, Any]] = []
    for path in sorted(policy_gate_queue_dir().glob("*.json")):
        payload = load_json(path)
        if payload:
            items.append(payload)
    return items


def gate_decisions(gate_id: str) -> list[dict[str, Any]]:
    prefix = safe_name(gate_id, default="gate")
    entries: list[dict[str, Any]] = []
    for path in sorted(policy_gate_decisions_dir().glob(f"{prefix}.*.json")):
        payload = load_json(path)
        if payload:
            entries.append(payload)
    return entries


def effective_gate_state(gate_id: str) -> dict[str, Any]:
    decisions = gate_decisions(gate_id)
    if not decisions:
        return {"gate_id": gate_id, "status": "pending"}
    latest = decisions[-1]
    return {"gate_id": gate_id, "status": str(latest.get("decision", "pending")).strip(), "decision": latest}


def pending_gate_entries() -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for item in list_gate_entries():
        gate_id = str(item.get("gate_id", "")).strip()
        if effective_gate_state(gate_id).get("status") == "pending":
            pending.append(item)
    return pending


def decide_gate(
    gate_id: str,
    *,
    decision: str,
    decided_by: str,
    note: str = "",
    council_case_id: str = "",
) -> dict[str, Any]:
    ensure_admin_dirs()
    if decision not in {"approved", "rejected", "held", "review_requested", "council_escalated"}:
        raise ValueError(f"invalid_gate_decision:{decision}")
    entry = load_gate_entry(gate_id)
    if entry is None:
        raise ValueError(f"gate_not_found:{gate_id}")
    payload = {
        "gate_id": gate_id,
        "ts": utc_now_iso(),
        "decision": decision,
        "decided_by": str(decided_by).strip() or "admin",
        "note": str(note).strip(),
        "trace_id": str(entry.get("trace_id", "")).strip(),
        "task_id": str(entry.get("task_id", "")).strip(),
        "council_case_id": str(council_case_id).strip(),
    }
    atomic_write_json(_decision_path(gate_id), payload)
    append_jsonl(
        policy_gate_index_path(),
        {
            "kind": "gate_decision",
            "ts": payload["ts"],
            "gate_id": gate_id,
            "decision": decision,
            "decided_by": payload["decided_by"],
            "trace_id": payload["trace_id"],
            "task_id": payload["task_id"],
        },
    )
    append_admin_audit(
        "policy_gate_decided",
        gate_id=gate_id,
        decision=decision,
        decided_by=payload["decided_by"],
        trace_id=payload["trace_id"],
    )
    return payload
