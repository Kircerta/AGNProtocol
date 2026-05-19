"""Council review case and verdict persistence helpers.

This is the real package implementation for AGN's council review surface.
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
    council_cases_dir,
    council_index_path,
    council_verdicts_dir,
    ensure_admin_dirs,
    load_json,
    safe_name,
    utc_now_iso,
)
from agn.governance.review_contract import normalize_structured_verdict


PACKAGE_PATH = "agn.governance.council"
LEGACY_SCRIPT_SHIM = "scripts/council_review.py"


def _case_path(case_id: str) -> Path:
    return council_cases_dir() / f"{safe_name(case_id, default='case')}.json"


def _verdict_path(case_id: str, reviewer: str) -> Path:
    stamp = utc_now_iso().replace(":", "").replace("-", "")
    name = f"{safe_name(case_id, default='case')}.{safe_name(reviewer, default='reviewer')}.{stamp}.json"
    return council_verdicts_dir() / name


def create_council_case(raw: dict[str, Any]) -> dict[str, Any]:
    ensure_admin_dirs()
    reviewers = [str(item).strip() for item in raw.get("reviewers", []) if str(item).strip()]
    if len(reviewers) != 3:
        raise ValueError("council_requires_exactly_three_reviewers")
    case = {
        "case_id": str(raw.get("case_id", "")).strip() or f"council-{uuid4().hex[:12]}",
        "created_at": str(raw.get("created_at", "")).strip() or utc_now_iso(),
        "trace_id": str(raw.get("trace_id", "")).strip(),
        "task_id": str(raw.get("task_id", "")).strip(),
        "trigger": str(raw.get("trigger", "")).strip() or "policy_gate_escalation",
        "reason": str(raw.get("reason", "")).strip(),
        "input_refs": [str(item).strip() for item in raw.get("input_refs", []) if str(item).strip()],
        "reviewers": reviewers,
        "required_verdicts": 3,
        "unanimous_approve_required": True,
    }
    atomic_write_json(_case_path(case["case_id"]), case)
    append_jsonl(
        council_index_path(),
        {
            "kind": "case_created",
            "ts": case["created_at"],
            "case_id": case["case_id"],
            "trace_id": case["trace_id"],
            "task_id": case["task_id"],
            "trigger": case["trigger"],
        },
    )
    append_admin_audit(
        "council_case_created",
        case_id=case["case_id"],
        trace_id=case["trace_id"],
        task_id=case["task_id"],
        trigger=case["trigger"],
    )
    return case


def load_council_case(case_id: str) -> dict[str, Any] | None:
    path = _case_path(case_id)
    if not path.exists():
        return None
    return load_json(path)


def append_council_verdict(case_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    ensure_admin_dirs()
    case = load_council_case(case_id)
    if case is None:
        raise ValueError(f"council_case_not_found:{case_id}")
    reviewer = str(raw.get("reviewer", "")).strip()
    if reviewer not in set(case.get("reviewers", [])):
        raise ValueError(f"reviewer_not_authorized:{reviewer}")
    verdict = normalize_structured_verdict(raw.get("verdict") if isinstance(raw.get("verdict"), dict) else raw)
    payload = {
        "case_id": case_id,
        "ts": utc_now_iso(),
        "reviewer": reviewer,
        "trace_id": str(case.get("trace_id", "")).strip(),
        "task_id": str(case.get("task_id", "")).strip(),
        "structured_verdict": verdict,
    }
    atomic_write_json(_verdict_path(case_id, reviewer), payload)
    append_jsonl(
        council_index_path(),
        {
            "kind": "verdict_recorded",
            "ts": payload["ts"],
            "case_id": case_id,
            "reviewer": reviewer,
            "trace_id": payload["trace_id"],
            "task_id": payload["task_id"],
            "verdict": verdict["verdict"],
        },
    )
    append_admin_audit(
        "council_verdict_recorded",
        case_id=case_id,
        reviewer=reviewer,
        trace_id=payload["trace_id"],
        verdict=verdict["verdict"],
    )
    return payload


def council_verdicts(case_id: str) -> list[dict[str, Any]]:
    prefix = safe_name(case_id, default="case")
    entries: list[dict[str, Any]] = []
    for path in sorted(council_verdicts_dir().glob(f"{prefix}.*.json")):
        payload = load_json(path)
        if payload:
            entries.append(payload)
    return entries


def aggregate_council_case(case_id: str) -> dict[str, Any]:
    case = load_council_case(case_id)
    if case is None:
        raise ValueError(f"council_case_not_found:{case_id}")
    verdicts = council_verdicts(case_id)
    normalized = [normalize_structured_verdict(item.get("structured_verdict")) for item in verdicts]
    required = int(case.get("required_verdicts", 3) or 3)
    if len(normalized) < required:
        return {
            "case_id": case_id,
            "status": "pending",
            "received_verdicts": len(normalized),
            "required_verdicts": required,
            "decision": "awaiting_verdicts",
        }
    unanimous = all(item["verdict"] == "approve" and not bool(item["escalate_to_human"]) for item in normalized)
    return {
        "case_id": case_id,
        "status": "complete",
        "received_verdicts": len(normalized),
        "required_verdicts": required,
        "decision": "approved" if unanimous else "escalate_to_admin",
        "unanimous_approve": unanimous,
        "trace_id": str(case.get("trace_id", "")).strip(),
        "task_id": str(case.get("task_id", "")).strip(),
        "reviewers": [str(item.get("reviewer", "")).strip() for item in verdicts],
        "verdicts": normalized,
    }


__all__ = [
    "LEGACY_SCRIPT_SHIM",
    "PACKAGE_PATH",
    "aggregate_council_case",
    "append_council_verdict",
    "council_verdicts",
    "create_council_case",
    "load_council_case",
]
