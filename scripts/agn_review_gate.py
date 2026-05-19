#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports" / "review_gate"

try:
    from capability_snapshot import build_capability_snapshot
except ImportError:  # pragma: no cover
    from scripts.capability_snapshot import build_capability_snapshot


ALLOWED_REVIEWERS = ("claude", "gemini")
FORBIDDEN_REVIEWERS = ("qwen_local", "deepseek")
REVIEW_ABORT_SEMANTICS = [
    "If review is forbidden, abort the reviewer lane and use local verification instead.",
    "If provider availability or evidence is insufficient, abort automatic acceptance and escalate.",
    "Review remains single-round by default and hard-capped at two rounds through the orchestrator.",
]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _safe_slug(text: str, *, default: str, max_len: int = 48) -> str:
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


def _text_flags(task_summary: str) -> dict[str, bool]:
    text = str(task_summary or "").lower()
    return {
        "architecture": any(token in text for token in ("architecture", "design", "system boundary", "protocol", "governance")),
        "ambiguity": any(token in text for token in ("unclear", "ambiguous", "multiple causes", "uncertain", "blind spot")),
        "mechanical": any(token in text for token in ("rename", "format", "typo", "small edit", "mechanical", "obvious fix")),
        "local_fact": any(token in text for token in ("test failure", "lint", "local check", "unit test", "compile error")),
        "human_approval": any(token in text for token in ("approval", "sign-off", "before asking human", "before asking admin")),
        "experiment_review": any(token in text for token in ("experiment result", "result review", "recheck result", "replicate finding")),
    }


def _decision(
    *,
    task_summary: str,
    risk_level: str,
    change_scope: str,
    uncertainty: str,
    local_verification_available: bool,
    mechanical_task: bool,
    root_cause_unclear: bool,
    before_human_approval: bool,
    experiment_results_need_review: bool,
) -> dict[str, Any]:
    flags = _text_flags(task_summary)
    must_review_reasons: list[str] = []
    forbid_review_reasons: list[str] = []

    if risk_level == "high":
        must_review_reasons.append("high-risk change should receive flagship review before final judgment.")
    if change_scope in {"architecture", "cross_cutting"} or flags["architecture"]:
        must_review_reasons.append("architecture or cross-cutting change benefits from an external audit lane.")
    if uncertainty in {"medium", "high"} or flags["ambiguity"] or root_cause_unclear:
        must_review_reasons.append("root cause or interpretation is still uncertain.")
    if before_human_approval or flags["human_approval"]:
        must_review_reasons.append("last external audit before human approval is explicitly requested.")
    if experiment_results_need_review or flags["experiment_review"]:
        must_review_reasons.append("critical experiment result should be independently rechecked.")

    if mechanical_task or flags["mechanical"]:
        forbid_review_reasons.append("mechanical local change should not go to flagship review.")
    if local_verification_available or flags["local_fact"]:
        forbid_review_reasons.append("local verification can settle the question more directly than reviewer prose.")
    if risk_level == "low" and change_scope == "local" and not root_cause_unclear and not before_human_approval and not experiment_results_need_review:
        forbid_review_reasons.append("low-risk local work defaults to no flagship review.")

    if must_review_reasons:
        verdict = "required"
    elif len(forbid_review_reasons) >= 2:
        verdict = "forbidden"
    else:
        verdict = "optional"

    return {
        "verdict": verdict,
        "must_review_reasons": must_review_reasons,
        "forbid_review_reasons": forbid_review_reasons,
        "flags": flags,
    }


def choose_reviewer(capability: dict[str, Any]) -> str:
    provider_roles = capability.get("provider_policy", {}).get("provider_roles", {})
    for name in ALLOWED_REVIEWERS:
        if bool((provider_roles.get(name) or {}).get("available")):
            return name
    return ""


def build_review_command(*, file_path: str, goal: str, reviewer: str) -> str:
    command = [
        "python3",
        "scripts/agn2_execution_workflow.py",
        "review",
        "--file",
        str(Path(file_path).resolve()),
        "--goal",
        goal,
    ]
    if reviewer == "claude":
        command.extend(["--claude-model", "opus"])
    elif reviewer == "gemini":
        command.extend(["--gemini-model", "pro"])
    return " ".join(shlex.quote(item) for item in command)


def build_payload(
    *,
    task_summary: str,
    risk_level: str,
    change_scope: str,
    uncertainty: str,
    local_verification_available: bool,
    mechanical_task: bool,
    root_cause_unclear: bool,
    before_human_approval: bool,
    experiment_results_need_review: bool,
    file_path: str,
    review_goal: str,
) -> dict[str, Any]:
    capability = build_capability_snapshot()
    gate = _decision(
        task_summary=task_summary,
        risk_level=risk_level,
        change_scope=change_scope,
        uncertainty=uncertainty,
        local_verification_available=local_verification_available,
        mechanical_task=mechanical_task,
        root_cause_unclear=root_cause_unclear,
        before_human_approval=before_human_approval,
        experiment_results_need_review=experiment_results_need_review,
    )
    reviewer = choose_reviewer(capability)
    command = ""
    if gate["verdict"] in {"required", "optional"} and reviewer and file_path:
        command = build_review_command(file_path=file_path, goal=review_goal, reviewer=reviewer)
    return {
        "ok": True,
        "generated_at": utc_now_iso(),
        "task_summary": task_summary,
        "risk_level": risk_level,
        "change_scope": change_scope,
        "uncertainty": uncertainty,
        "verdict": gate["verdict"],
        "must_review_reasons": gate["must_review_reasons"],
        "forbid_review_reasons": gate["forbid_review_reasons"],
        "reviewer_lane": reviewer,
        "allowed_reviewers": [name for name in ALLOWED_REVIEWERS if bool((capability.get("provider_policy", {}).get("provider_roles", {}).get(name) or {}).get("available"))],
        "forbidden_reviewers": list(FORBIDDEN_REVIEWERS),
        "structured_review_schema": capability.get("modules", {}).get("reviewer", {}).get("structured_schema", []),
        "abort_semantics": REVIEW_ABORT_SEMANTICS,
        "review_goal": review_goal,
        "review_command": command,
        "notes": [
            "Use review for ambiguity, architecture risk, critical experiments, or the final audit before human approval.",
            "Do not use review for obvious local fixes or facts that tests can settle directly.",
            "Worker-grade models are never valid flagship reviewers.",
        ],
    }


def _write_report(payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"{timestamp}-{_safe_slug(str(payload.get('task_summary', 'review-gate')), default='review-gate')}.json"
    latest = REPORT_DIR / "latest.json"
    _atomic_write_json(path, payload)
    _atomic_write_json(latest, payload)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Decide when flagship review is required, forbidden, or optional in AGN2.0.")
    parser.add_argument("--task-summary", required=True)
    parser.add_argument("--risk-level", choices=["low", "medium", "high"], default="low")
    parser.add_argument("--change-scope", choices=["local", "cross_cutting", "architecture"], default="local")
    parser.add_argument("--uncertainty", choices=["low", "medium", "high"], default="low")
    parser.add_argument("--local-verification-available", action="store_true")
    parser.add_argument("--mechanical-task", action="store_true")
    parser.add_argument("--root-cause-unclear", action="store_true")
    parser.add_argument("--before-human-approval", action="store_true")
    parser.add_argument("--experiment-results-need-review", action="store_true")
    parser.add_argument("--file", default="")
    parser.add_argument("--review-goal", default="Review this file for correctness, risk handling, and AGN2.0 alignment.")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()

    payload = build_payload(
        task_summary=str(args.task_summary).strip(),
        risk_level=str(args.risk_level).strip().lower(),
        change_scope=str(args.change_scope).strip().lower(),
        uncertainty=str(args.uncertainty).strip().lower(),
        local_verification_available=bool(args.local_verification_available),
        mechanical_task=bool(args.mechanical_task),
        root_cause_unclear=bool(args.root_cause_unclear),
        before_human_approval=bool(args.before_human_approval),
        experiment_results_need_review=bool(args.experiment_results_need_review),
        file_path=str(args.file).strip(),
        review_goal=str(args.review_goal),
    )
    if not args.no_write:
        payload["report_path"] = str(_write_report(payload))
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
