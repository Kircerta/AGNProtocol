"""AGN2.0 governance bridge for the AGN1.0 sealed subsystem.

This is the real package implementation for the bridge between AGN2.0's
governance layer and AGN1.0's sealed runtime.
The legacy script remains only as a compatibility shim.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agn.core.admin_control import append_admin_audit, governance_root, repo_root
from agn.core.constitution import load_constitution
from agn.core.emergency_stop import desktop_mode, dispatcher_accepts_new_work, is_emergency_stop_active
from agn.core.policy_gate import create_gate_entry, evaluate_dispatch_request


PACKAGE_PATH = "agn.governance.bridge"
LEGACY_SCRIPT_SHIM = "scripts/agn2_governance_bridge.py"
_ROOT = repo_root()
_CONSTITUTION_PROTECTED_PATHS: frozenset[str] | None = None
_CONSTITUTION_MTIME: float = 0.0


def global_emergency_stop_active() -> bool:
    return is_emergency_stop_active()


def global_accepts_new_work() -> bool:
    return dispatcher_accepts_new_work()


def emit_agn1_audit(action: str, *, worker: str = "", **extra: Any) -> None:
    append_admin_audit(f"agn1.{action}", subsystem="agn1", worker=worker, **extra)


def _load_protected_paths() -> frozenset[str]:
    global _CONSTITUTION_PROTECTED_PATHS, _CONSTITUTION_MTIME
    constitution_file = governance_root() / "constitution.json"
    current_mtime = 0.0
    try:
        current_mtime = constitution_file.stat().st_mtime if constitution_file.exists() else 0.0
    except OSError:
        pass
    if _CONSTITUTION_PROTECTED_PATHS is not None and current_mtime == _CONSTITUTION_MTIME:
        return _CONSTITUTION_PROTECTED_PATHS
    constitution = load_constitution()
    immutability = constitution.get("immutability", {})
    raw = immutability.get("agent_may_not_modify", [])
    resolved: set[str] = set()
    for item in raw:
        path = str(item).strip()
        if path:
            resolved.add(str((_ROOT / path).resolve()))
    _CONSTITUTION_PROTECTED_PATHS = frozenset(resolved)
    _CONSTITUTION_MTIME = current_mtime
    return _CONSTITUTION_PROTECTED_PATHS


def is_constitution_protected(path: Path | str) -> bool:
    return str(Path(path).resolve()) in _load_protected_paths()


def assert_write_allowed(path: Path | str) -> None:
    if is_constitution_protected(path):
        raise ValueError(
            f"constitution_immutability_violation: writing to {path} "
            f"is blocked by agn2/governance/constitution.json "
            f"(agent_may_not_modify)"
        )


def evaluate_agn1_dispatch(
    *,
    task_id: str,
    risk_level: str,
    side_effect_level: str,
    request_summary: str,
    correlation_id: str = "",
    worker: str = "coordinator",
) -> dict[str, Any]:
    request = {
        "task_id": task_id,
        "target_kind": "agn1_subsystem",
        "risk_level": risk_level,
        "side_effect_level": side_effect_level,
        "intent": request_summary[:200],
        "caller": worker,
        "reason": f"AGN1.0 coordinator dispatch: {task_id}",
        "trace_id": correlation_id or task_id,
    }
    try:
        evaluation = evaluate_dispatch_request(request)
    except Exception:
        emit_agn1_audit(
            "dispatch_gate_evaluation_error",
            worker=worker,
            task_id=task_id,
            risk_level=risk_level,
        )
        return {"allowed": False, "rule_id": "evaluation_error", "gate_id": ""}
    if evaluation.get("requires_gate") is False:
        return {"allowed": True, "rule_id": "", "gate_id": ""}
    gate = create_gate_entry(
        request=request,
        request_ref=f"agn1:dispatch:{task_id}",
        evaluation=evaluation,
    )
    emit_agn1_audit(
        "dispatch_gated",
        worker=worker,
        task_id=task_id,
        gate_id=str(gate.get("gate_id", "")),
        rule_id=str(evaluation.get("rule_id", "")),
        risk_level=risk_level,
    )
    return {
        "allowed": False,
        "rule_id": str(evaluation.get("rule_id", "")),
        "gate_id": str(gate.get("gate_id", "")),
        "gate": gate,
    }


__all__ = [
    "LEGACY_SCRIPT_SHIM",
    "PACKAGE_PATH",
    "_CONSTITUTION_MTIME",
    "_CONSTITUTION_PROTECTED_PATHS",
    "_ROOT",
    "_load_protected_paths",
    "assert_write_allowed",
    "desktop_mode",
    "emit_agn1_audit",
    "evaluate_agn1_dispatch",
    "global_accepts_new_work",
    "global_emergency_stop_active",
    "is_constitution_protected",
]
