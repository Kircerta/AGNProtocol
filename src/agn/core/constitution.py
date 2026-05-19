"""AGN constitution policy helpers.

This is the real package implementation for AGN's constitution-loading,
validation, and policy helper surface. The legacy script remains only as a
compatibility shim.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agn.core.admin_control import governance_root, load_json


PACKAGE_PATH = "agn.core.constitution"
LEGACY_SCRIPT_SHIM = "scripts/agn2_constitution.py"
DEFAULT_CONSTITUTION: dict[str, Any] = {
    "version": "agn2.0-2026-03-13",
    "admin": {
        "sovereignty_model": "single_human_admin",
        "authorized_issuers": ["admin"],
        "sole_ssot": True,
        "final_responsibility": True,
        "final_arbiter": True,
    },
    "runtime_hierarchy": {
        "layers": [
            "human_admin",
            "admin_control_plane",
            "constitution_layer",
            "runtime_layer",
        ],
        "runtime_must_not_override_governance": True,
    },
    "high_risk_policy": {
        "default_auto_execute": False,
        "policy_gate_required": True,
        "desktop_write_requires_gate": True,
        "constitution_zone_requires_admin": True,
    },
    "transparency": {
        "summary_view_required": True,
        "raw_view_required": True,
        "disallow_fake_chain_of_thought": True,
    },
    "immutability": {
        "agent_may_not_modify": [
            "agn2/governance/constitution.json",
            "agn2/governance/policy_gate.json",
            "runtime/admin_control/system_mode.json",
        ],
        "agent_may_not_self_elevate": True,
        "agent_may_not_change_authority_hierarchy": True,
    },
    "council_review": {
        "required_on": [
            "contamination_suspected",
            "missing_evidence",
            "high_risk_critical_action",
            "review_disagreement",
            "root_cause_non_convergent",
            "policy_gate_escalation",
            "constitution_change_request",
        ],
        "reviewer_count": 3,
        "unanimous_approve_required": True,
    },
    "emergency_stop": {
        "admin_only": True,
        "dispatcher_accepts_new_work": False,
        "desktop_mode": "observe_only",
        "external_reviewers_paused": True,
    },
}


def validate_constitution(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return ["constitution_must_be_object"]
    errors: list[str] = []
    admin = payload.get("admin")
    if not isinstance(admin, dict):
        errors.append("missing:admin")
    else:
        issuers = admin.get("authorized_issuers")
        if not isinstance(issuers, list) or not [item for item in issuers if str(item).strip()]:
            errors.append("invalid:admin.authorized_issuers")
        if not bool(admin.get("sole_ssot", False)):
            errors.append("invalid:admin.sole_ssot")
    council = payload.get("council_review")
    if not isinstance(council, dict):
        errors.append("missing:council_review")
    else:
        if int(council.get("reviewer_count", 0) or 0) != 3:
            errors.append("invalid:council_review.reviewer_count")
        if not bool(council.get("unanimous_approve_required", False)):
            errors.append("invalid:council_review.unanimous_approve_required")
    transparency = payload.get("transparency")
    if not isinstance(transparency, dict):
        errors.append("missing:transparency")
    else:
        if not bool(transparency.get("summary_view_required", False)):
            errors.append("invalid:transparency.summary_view_required")
        if not bool(transparency.get("raw_view_required", False)):
            errors.append("invalid:transparency.raw_view_required")
    return errors


def load_constitution(path: Path | None = None) -> dict[str, Any]:
    target = path or (governance_root() / "constitution.json")
    payload = load_json(target, default=DEFAULT_CONSTITUTION)
    errors = validate_constitution(payload)
    if errors:
        return dict(DEFAULT_CONSTITUTION)
    return payload


def authorized_admin_issuers(constitution: dict[str, Any] | None = None) -> set[str]:
    payload = constitution or load_constitution()
    admin = payload.get("admin", {}) if isinstance(payload.get("admin"), dict) else {}
    raw = admin.get("authorized_issuers", [])
    return {str(item).strip() for item in raw if str(item).strip()} or {"admin"}


def issuer_is_authorized(issuer: str, constitution: dict[str, Any] | None = None) -> bool:
    return str(issuer).strip() in authorized_admin_issuers(constitution)


def emergency_stop_policy(constitution: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = constitution or load_constitution()
    stop = payload.get("emergency_stop", {})
    return stop if isinstance(stop, dict) else {}


def council_policy(constitution: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = constitution or load_constitution()
    review = payload.get("council_review", {})
    return review if isinstance(review, dict) else {}
