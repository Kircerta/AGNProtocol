#!/usr/bin/env python3
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn_api.ssot_store import SSOTStore
from event_sourcing import (
    append_event,
    heartbeat_tick,
    load_checkpoint,
    load_events,
    transition_state,
    write_checkpoint,
)
from pointer_protocol import read_ref_text, resolve_ref_path, task_attempt_dir, write_json_artifact, write_text_artifact
try:
    from agn_notify_runtime import enqueue_message
except ImportError:  # pragma: no cover - package import fallback
    from scripts.agn_notify_runtime import enqueue_message
try:
    from network_runtime import acknowledge_coordinator_refresh, publish_runtime_surface
except ImportError:  # pragma: no cover - package import fallback
    from scripts.network_runtime import acknowledge_coordinator_refresh, publish_runtime_surface
try:
    from research_runtime import (
        resolve_research_blog_branch,
        resolve_research_blog_repo_path,
        resolve_research_blog_science_dir,
        resolve_research_publish_branch,
        resolve_research_publish_repo_path,
    )
except ImportError:  # pragma: no cover - package import fallback
    from scripts.research_runtime import (
        resolve_research_blog_branch,
        resolve_research_blog_repo_path,
        resolve_research_blog_science_dir,
        resolve_research_publish_branch,
        resolve_research_publish_repo_path,
    )

PROFILE_PATH = ROOT / "config" / "research_profile.json"
COORDINATOR_POLICY_PATH = ROOT / "config" / "research_coordinator_policy.json"
EXCEPTION_POLICY_PATH = ROOT / "config" / "research_exception_policy.json"
COORDINATOR_PLAYBOOK_PATH = ROOT / "config" / "research_coordinator_playbook.json"
ROLE_INIT_DIR = ROOT / "config" / "role_init"
COMMON_ROLE_INIT = ROLE_INIT_DIR / "research_protocol_core.json"
ROLE_INIT_FILES = {
    "executor": ROLE_INIT_DIR / "executor_role_init.json",
    "reviewer": ROLE_INIT_DIR / "reviewer_role_init.json",
}
ATTEMPT = 1
MANUAL_QUESTION_PLACEHOLDER = "Select one daily research question inside the allowed axis set."
MANUAL_HYPOTHESIS_PLACEHOLDER = "A single constrained change should either beat the baseline or yield a valuable failure note."
RESEARCH_OUTPUT_DIR = ROOT / "research_outputs"


def _load_json_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


COORDINATOR_POLICY = _load_json_config(COORDINATOR_POLICY_PATH)
EXCEPTION_POLICY = _load_json_config(EXCEPTION_POLICY_PATH)
COORDINATOR_PLAYBOOK = _load_json_config(COORDINATOR_PLAYBOOK_PATH)
RESEARCH_ADMIN_HOLD_REASONS = set(
    str(item).strip()
    for item in (COORDINATOR_POLICY.get("allowed_admin_holds", []) or [])
    if str(item).strip()
) or {"manual_intake_missing", "brief_reply_window_open"}


def _default_executor_provider() -> str:
    return str(os.getenv("EXECUTOR_PROVIDER", "codex") or "codex").strip().lower() or "codex"


def _default_reviewer_provider() -> str:
    return str(os.getenv("REVIEWER_PROVIDER", "gemini") or "gemini").strip().lower() or "gemini"


def _default_admin_chat_id() -> str:
    return str(os.getenv("AGN_TELEGRAM_ADMIN_CHAT_ID", "") or "").strip()


def _default_research_repo_path() -> str:
    return str(resolve_research_publish_repo_path() or "").strip()


def _default_research_work_branch() -> str:
    return str(resolve_research_publish_branch() or "main").strip() or "main"


def _is_infra_repo_path(value: str) -> bool:
    clean = str(value or "").strip()
    if not clean:
        return False
    try:
        return Path(clean).expanduser().resolve() == ROOT.resolve()
    except Exception:
        return False


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _load_profile() -> dict[str, Any]:
    payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _today_iso() -> str:
    return date.today().isoformat()


def _repo_rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except Exception:
        return str(path)


def _role_init_paths(role: str) -> list[str]:
    specific = ROLE_INIT_FILES.get(str(role).strip())
    paths = [COMMON_ROLE_INIT]
    if specific is not None:
        paths.append(specific)
    return [_repo_rel(path) for path in paths]


def _role_init_digest(role: str) -> str:
    digest = hashlib.sha256()
    for rel_path in _role_init_paths(role):
        path = ROOT / rel_path
        digest.update(rel_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _trim(value: Any, limit: int) -> str:
    return str(value or "").strip()[: max(16, int(limit))]


def _prefer(*values: Any) -> str:
    for value in values:
        clean = str(value or "").strip()
        if clean:
            return clean
    return ""


def _packet_schema(role: str, mode: str) -> str:
    return f"{str(role).strip()}_{str(mode).strip()}_v1"


def _role_goal(role: str, mode: str) -> str:
    clean_role = str(role).strip()
    clean_mode = str(mode).strip()
    if clean_role == "executor" and clean_mode == "topic_vote":
        return "Judge executability only."
    if clean_role == "executor" and clean_mode == "run_experiment":
        return "Run the experiment within budget and degrade instead of stopping."
    if clean_role == "reviewer" and clean_mode == "topic_vote":
        return "Find flaws and return yes or no."
    if clean_role == "reviewer" and clean_mode == "final_review":
        return "Audit the unit and return APPROVED, REVISION_ONCE, or FAILURE_ARCHIVE with evidence boundary."
    return "Act only on the current packet."


def _role_init_packet(*, role: str, round_no: int, mode: str, task_id: str) -> dict[str, Any]:
    protocol_digest = _role_init_digest(role)
    return {
        "step": "role_init",
        "packet_schema": _packet_schema(role, "role_init"),
        "task_id": task_id,
        "role": role,
        "current_round": round_no,
        "current_mode": mode,
        "goal": _role_goal(role, mode),
        "init_paths": _role_init_paths(role),
        "protocol_digest": protocol_digest,
        "integrity_contract": {
            "integrity_ack": "truthfulness_first",
            "failure_ack": "failure_is_valid",
            "fabrication_ack": "no_fabrication",
        },
        "confirmation_schema": {
            "ack": "init_loaded",
            "role": role,
            "current_round": round_no,
            "schema": _packet_schema(role, mode),
            "protocol_digest": protocol_digest,
            "integrity_ack": "truthfulness_first",
            "failure_ack": "failure_is_valid",
            "fabrication_ack": "no_fabrication",
        },
    }


def _safe_topic_id(text: str, *, prefix: str) -> str:
    raw = str(text or "").strip().lower()
    chars: list[str] = []
    for ch in raw:
        if ch.isalnum():
            chars.append(ch)
        else:
            chars.append("-")
    compact = "-".join(part for part in "".join(chars).split("-") if part)
    return f"{prefix}-{compact[:40] or 'topic'}"


def _research_mode_default(*, source: str, scenario: str) -> str:
    src = str(source or "").strip().lower()
    if "telegram" in src and "autonomy" not in src:
        return "manual"
    if src.startswith("manual"):
        return "manual"
    if scenario == "manual":
        return "manual"
    return "autonomy"


def _infer_axis(*, question: str, hypothesis: str) -> str:
    blob = f"{question} {hypothesis}".lower()
    if "transformer" in blob:
        return "Transformer 及其变体"
    if "gan" in blob:
        return "GAN"
    if "attention" in blob or "gated" in blob:
        return "经典注意力机制衍生与替代"
    if any(token in blob for token in ["interpolation", "parametric", "non-parametric", "frequency response", "freq", "spectrum"]):
        return "参数化与非参数化方法"
    return "机器学习在信号处理中的应用"


def _infer_focus(*, question: str, hypothesis: str) -> str:
    blob = f"{question} {hypothesis}".lower()
    if any(token in blob for token in ["separation", "source separation", "vocal", "stem"]):
        return "音频分离"
    if any(token in blob for token in ["frequency response", "freq", "spectrum", "spectral", "band", "interpolation"]):
        return "频响曲线自动校正"
    if any(token in blob for token in ["local", "global", "reconstruct", "recovery", "missing", "occlusion"]):
        return "局部恢复整体"
    return "音频线性依赖与结构关系"


def _infer_baseline(*, question: str, hypothesis: str) -> str:
    blob = f"{question} {hypothesis}".lower()
    if any(token in blob for token in ["interpolation", "frequency", "freq", "spectrum", "spectral", "band"]):
        return "linear spectral interpolation baseline"
    if "attention" in blob or "gated" in blob:
        return "uniform local averaging baseline"
    return "global lag-energy baseline"


def _infer_single_change(*, question: str, hypothesis: str) -> str:
    blob = f"{question} {hypothesis}".lower()
    if any(token in blob for token in ["autoencoder", "conv", "convolution"]):
        return "tiny 1D convolutional autoencoder"
    if "transformer" in blob:
        return "tiny sparse transformer surrogate"
    if "attention" in blob or "gated" in blob:
        return "deterministic gated local aggregator"
    return "local-to-global non-parametric vote"


def _manual_seed_candidate(task: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    seed_topic_id = str(task.get("manual_seed_topic_id", "")).strip()
    question = str(task.get("question", "")).strip()
    hypothesis = str(task.get("hypothesis", "")).strip()
    if seed_topic_id and not (question and hypothesis):
        seeded = _topic_by_id(profile, seed_topic_id)
        if isinstance(seeded, dict):
            seeded["survey_note"] = "Coordinator selected the safest same-day fallback at manual start."
            return seeded

    method_family = _manual_method_family(task)
    axis = str(task.get("research_axis", "")).strip() or _infer_axis(question=question, hypothesis=hypothesis)
    focus = _infer_focus(question=question, hypothesis=hypothesis)
    baseline = str(task.get("baseline", "")).strip() or _infer_baseline(question=question, hypothesis=hypothesis)
    single_change = str(task.get("single_change", "")).strip() or _infer_single_change(question=question, hypothesis=hypothesis)
    title = str(task.get("manual_title", "")).strip() or question[:140] or "Manual daily research proposal"
    topic_id = str(task.get("manual_topic_id", "")).strip() or _safe_topic_id(title, prefix="manual")
    return {
        "topic_id": topic_id,
        "axis": axis,
        "focus": focus,
        "title": title,
        "problem": question,
        "core_idea": hypothesis,
        "method_family": "manual",
        "required_method_family": method_family,
        "same_family_only": True,
        "baseline": baseline,
        "single_change": single_change,
        "survey_note": "Admin supplied the research question directly; the coordinator must stay on this topic.",
        "data_ready": True,
        "baseline_clear": bool(baseline),
        "fixed_budget": True,
        "falsifiable": bool(question and hypothesis),
        "degrade_ready": True,
        "external_dependency": False,
        "safe_fallback": False,
        "learning_value": 0.85,
    }


def _revise_manual_candidate(*, task: dict[str, Any], prior: dict[str, Any], round_no: int) -> dict[str, Any]:
    revised = deepcopy(prior)
    revised["topic_id"] = f"{str(prior.get('topic_id', 'manual')).strip()}_rev{round_no - 1}"
    revised["title"] = f"{str(prior.get('title', 'manual topic')).strip()} (round {round_no} narrowed)"
    revised["problem"] = (
        f"{str(task.get('question', '')).strip()} "
        "Restrict the test to one local synthetic surrogate and one fixed comparison."
    ).strip()
    revised["core_idea"] = (
        f"{str(task.get('hypothesis', '')).strip()} "
        "Constrain the change to one tiny model and one baseline under a fixed budget."
    ).strip()
    revised["data_ready"] = True
    revised["fixed_budget"] = True
    revised["baseline_clear"] = True
    revised["falsifiable"] = True
    revised["degrade_ready"] = True
    revised["external_dependency"] = False
    revised["survey_note"] = "Round revision narrows the admin-provided topic without changing its core claim."
    revised["required_method_family"] = str(prior.get("required_method_family", _manual_method_family(task))).strip()
    revised["same_family_only"] = True
    return revised


def _ensure_task(
    *,
    task_id: str,
    unit_date: str,
    scenario: str,
    executor_provider: str = "",
    reviewer_provider: str = "",
    chat_id: str = "",
    source: str = "research_daily",
    research_mode: str = "",
    research_axis: str = "",
    question: str = "",
    hypothesis: str = "",
    baseline: str = "",
    single_change: str = "",
    manual_seed_topic_id: str = "",
    awaiting_admin_until: str = "",
    daily_brief_ref: str = "",
) -> dict[str, Any]:
    store = SSOTStore(ROOT / "ssot")
    existing = store.get_task(task_id)
    if existing is not None:
        task = dict(existing)
        changed = False
        clean_chat_id = str(chat_id or task.get("chat_id", "") or _default_admin_chat_id()).strip()
        clean_executor = str(executor_provider or task.get("executor_provider", "") or _default_executor_provider()).strip().lower()
        clean_reviewer = str(reviewer_provider or task.get("reviewer_provider", "") or _default_reviewer_provider()).strip().lower()
        clean_mode = str(research_mode or task.get("research_mode", "") or _research_mode_default(source=source, scenario=scenario)).strip().lower()
        fallback_repo_path = _default_research_repo_path()
        current_repo_path = str(task.get("repo_path", "")).strip()
        clean_repo_path = fallback_repo_path if (not current_repo_path or _is_infra_repo_path(current_repo_path)) else current_repo_path
        clean_work_branch = str(task.get("work_branch", "") or _default_research_work_branch()).strip()
        clean_blog_repo_path = str(task.get("blog_repo_path", "") or resolve_research_blog_repo_path() or "").strip()
        clean_blog_work_branch = str(task.get("blog_work_branch", "") or resolve_research_blog_branch() or "main").strip() or "main"
        clean_blog_science_dir = str(task.get("blog_science_dir", "") or resolve_research_blog_science_dir() or "content/AGNResearch").strip() or "content/AGNResearch"
        trigger_mode = "manual" if clean_mode == "manual" else "auto"
        if clean_chat_id and str(task.get("chat_id", "")).strip() != clean_chat_id:
            task["chat_id"] = clean_chat_id
            changed = True
        if clean_executor and str(task.get("executor_provider", "")).strip().lower() != clean_executor:
            task["executor_provider"] = clean_executor
            changed = True
        if clean_reviewer and str(task.get("reviewer_provider", "")).strip().lower() != clean_reviewer:
            task["reviewer_provider"] = clean_reviewer
            changed = True
        if str(task.get("repo_path", "")).strip() != clean_repo_path:
            task["repo_path"] = clean_repo_path
            changed = True
        if str(task.get("work_branch", "")).strip() != clean_work_branch:
            task["work_branch"] = clean_work_branch
            changed = True
        if str(task.get("blog_repo_path", "")).strip() != clean_blog_repo_path:
            task["blog_repo_path"] = clean_blog_repo_path
            changed = True
        if str(task.get("blog_work_branch", "")).strip() != clean_blog_work_branch:
            task["blog_work_branch"] = clean_blog_work_branch
            changed = True
        if str(task.get("blog_science_dir", "")).strip() != clean_blog_science_dir:
            task["blog_science_dir"] = clean_blog_science_dir
            changed = True
        for key, value in {
            "research_mode": clean_mode,
            "research_trigger_mode": trigger_mode,
            "research_axis": str(research_axis or task.get("research_axis", "")).strip(),
            "question": str(question or task.get("question", "")).strip(),
            "hypothesis": str(hypothesis or task.get("hypothesis", "")).strip(),
            "baseline": str(baseline or task.get("baseline", "")).strip(),
            "single_change": str(single_change or task.get("single_change", "")).strip(),
            "manual_seed_topic_id": (
                str(manual_seed_topic_id or "").strip()
                if clean_mode == "manual" and str(question or task.get("question", "")).strip() and str(hypothesis or task.get("hypothesis", "")).strip()
                else str(manual_seed_topic_id or task.get("manual_seed_topic_id", "")).strip()
            ),
            "awaiting_admin_until": str(awaiting_admin_until or task.get("awaiting_admin_until", "")).strip(),
            "daily_brief_ref": str(daily_brief_ref or task.get("daily_brief_ref", "")).strip(),
        }.items():
            if str(task.get(key, "")).strip() != str(value or "").strip():
                task[key] = value
                changed = True
        for key, value in {
            "allow_external_publish": True,
            "admin_approved": True,
            "side_effect_level": "local_write",
            "allow_trusted_dependency_installs": True,
        }.items():
            if task.get(key) != value:
                task[key] = value
                changed = True
        trusted_sources = [
            "https://download.pytorch.org/whl/cpu",
            "https://pypi.org/simple",
        ]
        if list(task.get("trusted_dependency_sources", []) or []) != trusted_sources:
            task["trusted_dependency_sources"] = trusted_sources
            changed = True
        if str(task.get("admin_approved_by", "")).strip() != "daily_research_start":
            task["admin_approved_by"] = "daily_research_start"
            changed = True
        if not str(task.get("admin_approved_at", "")).strip():
            task["admin_approved_at"] = utc_now_iso()
            changed = True
        if changed:
            _save_task(task)
            return task
        return existing

    trace_id = f"research-{task_id}-{uuid4().hex[:8]}"
    clean_mode = str(research_mode or _research_mode_default(source=source, scenario=scenario)).strip().lower() or "autonomy"
    trigger_mode = "manual" if clean_mode == "manual" else "auto"
    default_question = "" if clean_mode == "manual" else MANUAL_QUESTION_PLACEHOLDER
    default_hypothesis = "" if clean_mode == "manual" else MANUAL_HYPOTHESIS_PLACEHOLDER
    task = {
        "id": task_id,
        "source": source,
        "request_text": f"Daily research unit for {unit_date}",
        "request_summary": f"Research unit {unit_date}",
        "agn_managed": True,
        "review_requested": True,
        "decision": None,
        "status": "pending",
        "correlation_id": trace_id,
        "acceptance_criteria": [
            {"id": "AC-1", "text": "survey and shortlist refs must be archived"},
            {"id": "AC-2", "text": "raw coordinator/executor/reviewer communication must be archived"},
            {"id": "AC-3", "text": "final archive must contain a paper or failure note plus review verdict"},
        ],
        "task_kind": "daily_research",
        "repo_path": _default_research_repo_path(),
        "work_branch": _default_research_work_branch(),
        "blog_repo_path": str(resolve_research_blog_repo_path() or "").strip(),
        "blog_work_branch": str(resolve_research_blog_branch() or "main").strip() or "main",
        "blog_science_dir": str(resolve_research_blog_science_dir() or "content/AGNResearch").strip() or "content/AGNResearch",
        "executor_provider": str(executor_provider or _default_executor_provider()).strip().lower() or "codex",
        "reviewer_provider": str(reviewer_provider or _default_reviewer_provider()).strip().lower() or "gemini",
        "risk_level": "low",
        "side_effect_level": "local_write",
        "allow_external_publish": True,
        "allow_trusted_dependency_installs": True,
        "trusted_dependency_sources": [
            "https://download.pytorch.org/whl/cpu",
            "https://pypi.org/simple",
        ],
        "admin_approved": True,
        "admin_approved_by": "daily_research_start",
        "admin_approved_at": utc_now_iso(),
        "lock_state": "active",
        "workflow_kind": "research_daily",
        "unit_date": unit_date,
        "scenario": scenario,
        "research_mode": clean_mode,
        "research_trigger_mode": trigger_mode,
        "research_axis": str(research_axis or "").strip(),
        "question": str(question or default_question).strip(),
        "hypothesis": str(hypothesis or default_hypothesis).strip(),
        "baseline": str(baseline or "To be fixed during proposal.").strip(),
        "single_change": str(single_change or "To be fixed during proposal.").strip(),
        "budget": {
            "max_cases": 6,
            "max_runtime_sec": 120,
            "mode": "local_synthetic",
        },
        "round": 0,
        "proposal_version": 0,
        "decision_mode": "three_round_coordinator",
        "failure_mode_allowed": True,
        "manual_seed_topic_id": str(manual_seed_topic_id or "").strip(),
        "awaiting_admin_until": str(awaiting_admin_until or "").strip(),
        "daily_brief_ref": str(daily_brief_ref or "").strip(),
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    clean_chat_id = str(chat_id or _default_admin_chat_id()).strip()
    if clean_chat_id:
        task["chat_id"] = clean_chat_id
    store.save_task(task)
    return task


def _checkpoint_base(*, task_id: str, trace_id: str, unit_date: str, scenario: str, trigger_mode: str) -> dict[str, Any]:
    initial_phase = "manual_intake" if str(trigger_mode).strip().lower() == "manual" else "auto_survey"
    return {
        "task_id": task_id,
        "trace_id": trace_id,
        "state": "CREATED",
        "paused": False,
        "last_event_time": "",
        "research_phase": initial_phase,
        "proposal_state": "proposal_created",
        "research_status": "proposal_created",
        "round": 0,
        "proposal_version": 0,
        "research_trigger_mode": str(trigger_mode).strip().lower() or "auto",
        "unit_date": unit_date,
        "scenario": scenario,
        "message_count": 0,
        "packet_chars_total": 0,
        "max_packet_chars": 0,
        "degrade_index": 0,
        "anomaly": False,
        "degraded": False,
        "rejected": False,
        "entered_third_round": False,
        "force_degrade": False,
        "force_reorganize": False,
        "force_anomaly": False,
        "forced_fallback_topic_id": "",
        "message_refs": [],
        "issue_history": [],
        "notified_keys": [],
        "proposal_refs": [],
        "round_records_ref": "",
        "trace_index_ref": "",
        "daily_brief_ref": "",
        "daily_brief_deadline": "",
        "awaiting_admin_response": False,
        "admin_hold_reason": "",
        "admin_hold_until": "",
        "intake_ref": "",
        "coordinator_preflight_ref": "",
        "governance_lock_ref": "",
        "research_plan_ref": "",
        "selection_decision_ref": "",
        "round_state_ref": "",
        "review_revision_count": 0,
        "review_revision_ref": "",
        "essay_ref": "",
        "code_bundle_ref": "",
        "result_summary_ref": "",
        "raw_results_ref": "",
        "data_record_ref": "",
        "reproduce_ref": "",
        "publish_receipt_ref": "",
        "publish_status": "",
        "push_status": "",
        "commit_hash": "",
        "telegram_receipt_ref": "",
        "forced_decision_ref": "",
        "final_report_ref": "",
        "admin_completion_message_id": "",
        "admin_delivery_status": "",
        "admin_delivery_checked_at": "",
        "notification_records": [],
        "protocol_blocked": False,
        "protocol_block_reason": "",
        "protocol_block_ref": "",
        "protocol_violation_count": 0,
        "governance_ready": False,
        "governance_missing": [],
        "completion_ready": False,
        "recent_event_label": "",
    }


def _merge_checkpoint(task_id: str, checkpoint: dict[str, Any], **updates: Any) -> dict[str, Any]:
    latest = load_checkpoint(task_id) or checkpoint
    merged = dict(latest)
    for key, value in updates.items():
        merged[key] = value
    if "last_event_time" not in merged or not str(merged.get("last_event_time", "")).strip():
        merged["last_event_time"] = utc_now_iso()
    write_checkpoint(task_id, merged)
    return load_checkpoint(task_id) or merged


def _ensure_checkpoint(*, task_id: str, trace_id: str, unit_date: str, scenario: str, trigger_mode: str = "auto") -> dict[str, Any]:
    checkpoint = load_checkpoint(task_id)
    if checkpoint is not None:
        return checkpoint
    payload = _checkpoint_base(task_id=task_id, trace_id=trace_id, unit_date=unit_date, scenario=scenario, trigger_mode=trigger_mode)
    write_checkpoint(task_id, payload)
    return load_checkpoint(task_id) or payload


def _task(trace_id: str, task_id: str) -> dict[str, Any]:
    store = SSOTStore(ROOT / "ssot")
    task = store.get_task(task_id) or {}
    task["correlation_id"] = trace_id
    return task


def _save_task(task: dict[str, Any]) -> None:
    task["updated_at"] = utc_now_iso()
    store = SSOTStore(ROOT / "ssot")
    task_id = str(task.get("id", "")).strip()
    if task_id:
        with store.locked_update(task_id) as existing:
            if existing is not None:
                existing.update(task)
            else:
                store.save_task(task)
                return
    else:
        store.save_task(task)


def _research_chat_id(task: dict[str, Any]) -> str:
    return str(task.get("chat_id", "") or _default_admin_chat_id()).strip()


def _trigger_mode(task: dict[str, Any]) -> str:
    raw = str(task.get("research_trigger_mode", "")).strip().lower()
    if raw in {"manual", "auto"}:
        return raw
    mode = str(task.get("research_mode", "")).strip().lower()
    return "manual" if mode == "manual" else "auto"


def _manual_method_family(task: dict[str, Any]) -> str:
    blob = f"{str(task.get('question', '')).strip()} {str(task.get('hypothesis', '')).strip()}".lower()
    if any(token in blob for token in ["1d conv", "卷积", "autoencoder", "自编码", "convolution"]):
        return "tiny_conv_autoencoder"
    if "transformer" in blob:
        return "tiny_transformer"
    return "generic_learning_model"


def _task_output_dir(task_id: str) -> Path:
    return RESEARCH_OUTPUT_DIR / str(task_id or "research").replace("/", "_")


def _notify_once(
    *,
    task_id: str,
    trace_id: str,
    checkpoint: dict[str, Any],
    key: str,
    text: str,
    message_kind: str = "progress",
) -> dict[str, Any]:
    notified = list(checkpoint.get("notified_keys", []) or [])
    clean_key = str(key or "").strip()
    if not clean_key or clean_key in notified:
        return checkpoint

    task = _task(trace_id, task_id)
    chat_id = _research_chat_id(task)
    if not chat_id:
        return checkpoint

    try:
        payload = enqueue_message(
            text=text,
            chat_id=chat_id,
            task_id=task_id,
            correlation_id=trace_id,
            message_kind=message_kind,
            source="research_flow",
        )
    except Exception:
        return checkpoint
    notified.append(clean_key)
    notification_records = list(checkpoint.get("notification_records", []) or [])
    if isinstance(payload, dict):
        notification_records.append(
            {
                "key": clean_key,
                "message_id": str(payload.get("message_id", "")).strip(),
                "created_at": str(payload.get("created_at", "")).strip(),
                "chat_id": str(payload.get("chat_id", "")).strip(),
                "kind": str(payload.get("kind", "")).strip(),
            }
        )
    updates: dict[str, Any] = {
        "notified_keys": notified[-64:],
        "notification_records": notification_records[-64:],
    }
    if clean_key == "admin_completion_report":
        updates["admin_completion_message_id"] = str((payload or {}).get("message_id", "")).strip()
        updates["admin_delivery_status"] = "queued" if str((payload or {}).get("message_id", "")).strip() else ""
        updates["admin_delivery_checked_at"] = utc_now_iso()
    return _merge_checkpoint(task_id, checkpoint, **updates)


def _bool_env(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "1" if default else "0") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _manual_input_missing(task: dict[str, Any]) -> bool:
    question = str(task.get("question", "")).strip()
    hypothesis = str(task.get("hypothesis", "")).strip()
    if not question or not hypothesis:
        return True
    return question == MANUAL_QUESTION_PLACEHOLDER and hypothesis == MANUAL_HYPOTHESIS_PLACEHOLDER


def _parse_local_deadline(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    parsed = _parse_iso(raw)
    if parsed is None:
        return None
    return parsed.astimezone().replace(tzinfo=None)


def _admin_wait_checkpoint(
    *,
    task_id: str,
    checkpoint: dict[str, Any],
    phase: str,
    reason: str,
    hold_until: str = "",
    event_label: str,
    recent_event: str,
) -> dict[str, Any]:
    clean_reason = str(reason or "").strip()
    if clean_reason not in RESEARCH_ADMIN_HOLD_REASONS:
        raise ValueError(f"invalid_research_admin_hold_reason:{clean_reason}")
    return _merge_checkpoint(
        task_id,
        checkpoint,
        research_phase=phase,
        research_status=event_label,
        awaiting_admin_response=True,
        admin_hold_reason=clean_reason,
        admin_hold_until=str(hold_until or "").strip(),
        protocol_blocked=False,
        protocol_block_reason="",
        governance_missing=[],
        completion_ready=False,
        recent_event_label=recent_event,
        last_event_time=utc_now_iso(),
    )


def _discussion_wait_active(*, task: dict[str, Any], checkpoint: dict[str, Any]) -> tuple[bool, str]:
    if not bool(checkpoint.get("awaiting_admin_response", False)):
        return False, ""
    hold_until = str(checkpoint.get("daily_brief_deadline", "") or task.get("awaiting_admin_until", "")).strip()
    deadline = _parse_local_deadline(hold_until)
    if deadline is None:
        return False, ""
    if datetime.now() < deadline:
        return True, hold_until
    return False, hold_until


def _write_governance_lock(*, task_id: str, trigger_mode: str, question: str, hypothesis: str) -> str:
    artifact = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="governance_lock",
        payload={
            "task_id": task_id,
            "trigger_mode": trigger_mode,
            "question": question,
            "hypothesis": hypothesis,
            "locked_at": utc_now_iso(),
            "policy_ref": str(COORDINATOR_POLICY_PATH.relative_to(ROOT)),
        },
        filename="governance_lock.json",
        source="research_flow",
    )
    return artifact.ref


def _coordinator_reference_docs() -> list[str]:
    return [
        "config/agent_role_contracts.json",
        "config/research_coordinator_policy.json",
        "config/research_exception_policy.json",
        "config/research_coordinator_playbook.json",
        "documentation/admin/OPENCLAW_COORDINATOR_SOUL_NATSURA.md",
        "documentation/admin/OPENCLAW_COORDINATOR_MEMORY_SEED_2026-03-11.md",
        "documentation/admin/OPENCLAW_COORDINATOR_REFRESH_CHECKLIST.md",
    ]


def _write_coordinator_preflight(*, task_id: str, task: dict[str, Any], trigger_mode: str) -> str:
    mode = str(trigger_mode).strip().lower() or "auto"
    playbook = COORDINATOR_PLAYBOOK.get(mode, {})
    steps = list(playbook.get("steps", []) or [])
    artifact = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="coordinator_preflight",
        payload={
            "task_id": task_id,
            "trigger_mode": mode,
            "question": str(task.get("question", "")).strip(),
            "hypothesis": str(task.get("hypothesis", "")).strip(),
            "responsibility_version": str(COORDINATOR_POLICY.get("version", "")).strip() or "1",
            "playbook_version": str(COORDINATOR_PLAYBOOK.get("version", "")).strip() or "1",
            "reference_docs": _coordinator_reference_docs(),
            "steps": steps,
            "generated_at": utc_now_iso(),
        },
        filename="coordinator_preflight.json",
        source="research_flow",
    )
    return artifact.ref


def _write_coordinator_refresh(*, task_id: str, refresh_ack: dict[str, Any]) -> str:
    artifact = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="coordinator_refresh",
        payload=refresh_ack,
        filename="coordinator_refresh.json",
        source="research_flow",
    )
    return artifact.ref


def _write_research_plan(*, task_id: str, task: dict[str, Any], candidate: dict[str, Any]) -> str:
    artifact = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="research_plan",
        payload={
            "task_id": task_id,
            "question": str(task.get("question", "")).strip(),
            "hypothesis": str(task.get("hypothesis", "")).strip(),
            "baseline": str(task.get("baseline", "")).strip(),
            "single_change": str(task.get("single_change", "")).strip(),
            "method_family": str(candidate.get("method_family", "")).strip(),
            "same_family_only": bool(candidate.get("same_family_only", False)),
            "generated_at": utc_now_iso(),
        },
        filename="research_plan.json",
        source="research_flow",
    )
    return artifact.ref


def _write_selection_decision(
    *,
    task_id: str,
    candidate: dict[str, Any],
    round_no: int,
    trigger_mode: str,
    reason: str,
) -> str:
    artifact = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id=f"selection_decision_round_{round_no}",
        payload={
            "task_id": task_id,
            "trigger_mode": trigger_mode,
            "round": round_no,
            "selected_topic_id": str(candidate.get("topic_id", "")).strip(),
            "title": str(candidate.get("title", "")).strip(),
            "reason": reason,
            "generated_at": utc_now_iso(),
        },
        filename=f"selection_decision_round_{round_no}.json",
        source="research_flow",
    )
    return artifact.ref


def _write_round_state(*, task_id: str, round_record: dict[str, Any]) -> str:
    artifact = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id=f"round_state_{int(round_record.get('round', 0) or 0)}",
        payload=round_record,
        filename=f"round_state_{int(round_record.get('round', 0) or 0)}.json",
        source="research_flow",
    )
    return artifact.ref


def _result_summary_payload(*, task: dict[str, Any], checkpoint: dict[str, Any]) -> dict[str, Any]:
    result = checkpoint.get("experiment_result")
    if not isinstance(result, dict):
        result = {}
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    final_review = checkpoint.get("final_review")
    if not isinstance(final_review, dict):
        final_review = {}
    return {
        "task_id": str(task.get("id", "")).strip(),
        "question": str(task.get("question", "")).strip(),
        "hypothesis": str(task.get("hypothesis", "")).strip(),
        "baseline": str(task.get("baseline", "")).strip(),
        "single_change": str(task.get("single_change", "")).strip(),
        "outcome_kind": str(checkpoint.get("outcome_kind", "")).strip(),
        "review_verdict": str(final_review.get("verdict", final_review.get("decision", ""))).strip(),
        "metrics": metrics,
        "unverified_metrics": result.get("unverified_metrics", {}) if isinstance(result.get("unverified_metrics"), dict) else {},
        "status": str(result.get("status", "")).strip(),
        "strategy": str(result.get("strategy", "")).strip(),
        "empirical_execution": bool(checkpoint.get("empirical_execution", result.get("empirical_execution", False))),
        "truthfulness_status": str(checkpoint.get("truthfulness_status", result.get("truthfulness_status", ""))).strip(),
        "truthfulness_reason": str(checkpoint.get("truthfulness_reason", result.get("truthfulness_reason", ""))).strip(),
        "degraded": bool(checkpoint.get("degraded", False)),
        "dependency_install_attempts": list(result.get("dependency_install_attempts", []) or []),
        "generated_at": utc_now_iso(),
    }


def _dataset_record_payload(*, task: dict[str, Any], checkpoint: dict[str, Any]) -> dict[str, Any]:
    method_family = _manual_method_family(task) if _trigger_mode(task) == "manual" else "local_global_signal"
    base = {
        "task_id": str(task.get("id", "")).strip(),
        "question": str(task.get("question", "")).strip(),
        "hypothesis": str(task.get("hypothesis", "")).strip(),
        "baseline": str(task.get("baseline", "")).strip(),
        "single_change": str(task.get("single_change", "")).strip(),
        "method_family": method_family,
        "trigger_mode": str(checkpoint.get("research_trigger_mode", "")).strip(),
        "generated_at": utc_now_iso(),
    }
    if method_family == "generic_learning_model":
        base["dataset_kind"] = "synthetic_linear_binary_classification"
        base["generation"] = {
            "seeds": [11, 23, 37],
            "noise_levels": [0.0, 0.05, 0.1, 0.15, 0.2, 0.25],
            "train_size": 160,
            "test_size": 240,
            "label_rule": "y = 1[(1.25 * x1) + (-0.9 * x2) - 0.15 >= 0]",
            "feature_range": [-1.0, 1.0],
            "training_rule": {
                "epochs": 12,
                "learning_rate": 0.12,
                "bias_offset": 1.0,
            },
        }
    else:
        base["dataset_kind"] = "synthetic_lag_signal"
        base["generation"] = {
            "target_lags": [3, 4, 5, 3, 4, 5],
            "sequence_length": 48,
            "window": 12,
            "stride": 6,
            "missing_pattern": "contiguous local zeros at indices {12+idx, 13+idx, 24+idx}",
            "signal_rule": "sin((t+idx)/3) + cos((t+2*idx)/5) + 0.72 * x[t-lag]",
        }
    return base


def _raw_results_payload(*, task: dict[str, Any], checkpoint: dict[str, Any]) -> dict[str, Any]:
    result = checkpoint.get("experiment_result")
    if not isinstance(result, dict):
        result = {}
    return {
        "task_id": str(task.get("id", "")).strip(),
        "experiment_result": result,
        "empirical_execution": bool(checkpoint.get("empirical_execution", result.get("empirical_execution", False))),
        "truthfulness_status": str(checkpoint.get("truthfulness_status", result.get("truthfulness_status", ""))).strip(),
        "truthfulness_reason": str(checkpoint.get("truthfulness_reason", result.get("truthfulness_reason", ""))).strip(),
        "experiment_summary_ref": str(checkpoint.get("experiment_summary_ref", "")).strip(),
        "experiment_raw_ref": str(checkpoint.get("experiment_raw_ref", "")).strip(),
        "experiment_log_ref": str(checkpoint.get("experiment_log_ref", "")).strip(),
        "generated_at": utc_now_iso(),
    }


def _reproduce_body(*, task: dict[str, Any], checkpoint: dict[str, Any]) -> str:
    empirical_execution = bool(checkpoint.get("empirical_execution", False))
    truthfulness_reason = str(checkpoint.get("truthfulness_reason", "")).strip()
    return "\n".join(
        [
            "# Reproduce",
            "",
            f"- task_id: `{str(task.get('id', '')).strip()}`",
            f"- question: {str(task.get('question', '')).strip() or 'n/a'}",
            f"- baseline: {str(task.get('baseline', '')).strip() or 'n/a'}",
            f"- single_change: {str(task.get('single_change', '')).strip() or 'n/a'}",
            "",
            "## Files",
            "- `experiment.py`: the reproduction script for the bounded experiment.",
            "- `raw_results.json`: the structured raw experiment output captured by AGN.",
            "- `data_record.json`: the data generation or dataset record needed to reconstruct the run.",
            "- `result_summary.json`: the summarized interpretation consumed by the final report.",
            "",
            "## Authenticity",
            f"- empirical_execution: `{str(empirical_execution).lower()}`",
            f"- truthfulness_reason: {truthfulness_reason or 'n/a'}",
            "",
            "## Run",
            "```bash",
            "python experiment.py > rerun_results.json",
            "```",
            "",
            "## Validate",
            "- Compare `rerun_results.json` against `raw_results.json` and `result_summary.json`.",
            "- Confirm the metrics and qualitative interpretation in `essay.md` and `final_report.md` are supported.",
            "",
            "## Trace Refs",
            f"- raw_results_ref: `{str(checkpoint.get('raw_results_ref', '')).strip() or 'n/a'}`",
            f"- data_record_ref: `{str(checkpoint.get('data_record_ref', '')).strip() or 'n/a'}`",
            f"- experiment_log_ref: `{str(checkpoint.get('experiment_log_ref', '')).strip() or 'n/a'}`",
            f"- trace_index_ref: `{str(checkpoint.get('trace_index_ref', '')).strip() or 'n/a'}`",
            "",
        ]
    )


NON_EMPIRICAL_MARKERS = (
    "dry_run",
    "theoretical simulation",
    "simulated",
    "simulation confirms",
    "metrics derived from",
    "tool unavailable",
    "not executed",
    "without execution",
    "theoretical",
)


def _text_has_non_empirical_marker(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in NON_EMPIRICAL_MARKERS)


def _experiment_truthfulness(result: dict[str, Any]) -> tuple[bool, str, str]:
    if not isinstance(result, dict):
        return False, "non_empirical", "experiment_result_missing"
    notes = result.get("notes")
    if not isinstance(notes, list):
        notes = []
    notes_text = "\n".join(str(item).strip() for item in notes if str(item).strip())
    strategy = str(result.get("strategy", "")).strip().lower()
    error = str(result.get("error", "")).strip()
    empirical = bool(result.get("empirical_execution", False))
    truthfulness_status = str(result.get("truthfulness_status", "")).strip().lower()
    truthfulness_reason = str(result.get("truthfulness_reason", "")).strip()
    if truthfulness_status == "empirical" and empirical:
        return True, "empirical", truthfulness_reason or "verifiable_local_execution"
    if strategy == "dry_run" or _text_has_non_empirical_marker(notes_text) or _text_has_non_empirical_marker(error):
        return False, "non_empirical", truthfulness_reason or notes_text or error or "executor_reported_non_empirical_execution"
    if empirical:
        return True, "empirical", truthfulness_reason or "verifiable_local_execution"
    if str(result.get("status", "")).strip().lower() == "failure_note":
        return False, "failure_note", truthfulness_reason or error or "executor_reported_failure_note"
    return False, "non_empirical", truthfulness_reason or "experiment_result_has_no_verifiable_execution_evidence"


def _block_non_empirical_result(*, result: dict[str, Any]) -> dict[str, Any]:
    blocked = dict(result)
    original_metrics = blocked.get("metrics")
    blocked["unverified_metrics"] = original_metrics if isinstance(original_metrics, dict) else {}
    blocked["metrics"] = {}
    blocked["status"] = "failure_note"
    blocked["strategy"] = "failure_note"
    blocked["empirical_execution"] = False
    blocked["truthfulness_status"] = "non_empirical"
    blocked["truthfulness_reason"] = str(blocked.get("truthfulness_reason", "")).strip() or "experiment metrics are not backed by verifiable local execution evidence"
    blocked["error"] = f"non_empirical_execution:{blocked['truthfulness_reason']}"
    notes = blocked.get("notes")
    if not isinstance(notes, list):
        notes = []
    clean_notes = [str(item).strip() for item in notes if str(item).strip()]
    clean_notes.insert(0, "Non-empirical execution was blocked: the worker returned simulated or unverified results without local execution evidence.")
    blocked["notes"] = clean_notes[:8]
    completed_work = blocked.get("completed_work")
    if not isinstance(completed_work, list):
        completed_work = []
    cleaned_work = [str(item).strip() for item in completed_work if str(item).strip()]
    cleaned_work.append("non-empirical result captured for audit and downgraded to failure note")
    blocked["completed_work"] = cleaned_work
    return blocked


def _perceptron_reproduction_script(*, task: dict[str, Any], checkpoint: dict[str, Any]) -> str:
    payload = _result_summary_payload(task=task, checkpoint=checkpoint)
    expected_metrics = payload.get("metrics", {})
    return "\n".join(
        [
            '"""Reproduction script for the bounded perceptron-vs-baseline research unit."""',
            "",
            "from __future__ import annotations",
            "",
            "import json",
            "import math",
            "import random",
            "from statistics import mean, pstdev",
            "",
            "SEEDS = [11, 23, 37]",
            f"NOISE_LEVELS = {json.dumps(expected_metrics.get('noise_levels', [0.0, 0.05, 0.1, 0.15, 0.2, 0.25]), ensure_ascii=True)}",
            "TRAIN_SIZE = 160",
            "TEST_SIZE = 240",
            "EPOCHS = 12",
            "LEARNING_RATE = 0.12",
            "BIAS = 1.0",
            "",
            "",
            "def make_split(seed: int, noise: float) -> tuple[list[tuple[list[float], int]], list[tuple[list[float], int]]]:",
            "    rng = random.Random(seed)",
            "    weights = (1.25, -0.9)",
            "    threshold = 0.15",
            "    train: list[tuple[list[float], int]] = []",
            "    test: list[tuple[list[float], int]] = []",
            "    for collection, size in ((train, TRAIN_SIZE), (test, TEST_SIZE)):",
            "        for _ in range(size):",
            "            x1 = rng.uniform(-1.0, 1.0)",
            "            x2 = rng.uniform(-1.0, 1.0)",
            "            score = weights[0] * x1 + weights[1] * x2 - threshold",
            "            label = 1 if score >= 0 else 0",
            "            if collection is train and rng.random() < noise:",
            "                label = 1 - label",
            "            collection.append(([x1, x2], label))",
            "    return train, test",
            "",
            "",
            "def train_perceptron(train: list[tuple[list[float], int]]) -> tuple[list[float], float]:",
            "    w = [0.0, 0.0]",
            "    b = 0.0",
            "    for _ in range(EPOCHS):",
            "        for features, label in train:",
            "            activation = w[0] * features[0] + w[1] * features[1] + b + BIAS",
            "            pred = 1 if activation >= 0 else 0",
            "            update = LEARNING_RATE * (label - pred)",
            "            if update != 0.0:",
            "                w[0] += update * features[0]",
            "                w[1] += update * features[1]",
            "                b += update",
            "    return w, b",
            "",
            "",
            "def predict_perceptron(model: tuple[list[float], float], features: list[float]) -> int:",
            "    w, b = model",
            "    return 1 if (w[0] * features[0] + w[1] * features[1] + b + BIAS) >= 0 else 0",
            "",
            "",
            "def predict_majority(train: list[tuple[list[float], int]], _: list[float]) -> int:",
            "    positives = sum(label for _, label in train)",
            "    negatives = len(train) - positives",
            "    return 1 if positives >= negatives else 0",
            "",
            "",
            "def balanced_accuracy(y_true: list[int], y_pred: list[int]) -> float:",
            "    tp = tn = fp = fn = 0",
            "    for truth, pred in zip(y_true, y_pred):",
            "        if truth == 1 and pred == 1:",
            "            tp += 1",
            "        elif truth == 0 and pred == 0:",
            "            tn += 1",
            "        elif truth == 0 and pred == 1:",
            "            fp += 1",
            "        else:",
            "            fn += 1",
            "    tpr = tp / (tp + fn) if (tp + fn) else 0.0",
            "    tnr = tn / (tn + fp) if (tn + fp) else 0.0",
            "    return (tpr + tnr) / 2.0",
            "",
            "",
            "def run_case(noise: float) -> dict[str, float | list[float]]:",
            "    perc_scores: list[float] = []",
            "    base_scores: list[float] = []",
            "    for seed in SEEDS:",
            "        train, test = make_split(seed, noise)",
            "        model = train_perceptron(train)",
            "        truths = [label for _, label in test]",
            "        perceptron_preds = [predict_perceptron(model, features) for features, _ in test]",
            "        majority_preds = [predict_majority(train, features) for features, _ in test]",
            "        perc_scores.append(round(balanced_accuracy(truths, perceptron_preds), 4))",
            "        base_scores.append(round(balanced_accuracy(truths, majority_preds), 4))",
            "    return {",
            "        'noise': noise,",
            "        'perceptron_mean': round(mean(perc_scores), 4),",
            "        'perceptron_std': round(pstdev(perc_scores), 4),",
            "        'baseline_mean': round(mean(base_scores), 4),",
            "        'baseline_std': round(pstdev(base_scores), 4),",
            "        'perceptron_scores': perc_scores,",
            "        'baseline_scores': base_scores,",
            "    }",
            "",
            "",
            "def main() -> None:",
            f"    metadata = {json.dumps({k: payload[k] for k in ['task_id', 'question', 'hypothesis', 'baseline', 'single_change', 'outcome_kind', 'review_verdict']}, ensure_ascii=True, indent=2)}",
            f"    metadata['expected_metrics'] = {json.dumps(expected_metrics, ensure_ascii=True, indent=2)}",
            "    runs = [run_case(noise) for noise in NOISE_LEVELS]",
            "    metadata['reproduction_runs'] = runs",
            "    metadata['summary'] = {",
            "        'avg_perceptron_bal_acc': round(mean(item['perceptron_mean'] for item in runs), 4),",
            "        'avg_baseline_bal_acc': round(mean(item['baseline_mean'] for item in runs), 4),",
            "        'variance_trend': 'increasing' if runs[-1]['perceptron_std'] >= runs[0]['perceptron_std'] else 'flat_or_decreasing',",
            "    }",
            "    print(json.dumps(metadata, ensure_ascii=False, indent=2))",
            "",
            "",
            'if __name__ == "__main__":',
            "    main()",
            "",
        ]
    )


def _lag_recovery_reproduction_script(*, task: dict[str, Any], checkpoint: dict[str, Any]) -> str:
    payload = _result_summary_payload(task=task, checkpoint=checkpoint)
    return "\n".join(
        [
            '"""Reproduction script for the bounded local-to-global lag recovery surrogate."""',
            "",
            "from __future__ import annotations",
            "",
            "import json",
            "import math",
            "",
            "",
            "def synthetic_cases() -> list[dict[str, object]]:",
            "    cases = []",
            "    for idx, lag in enumerate((3, 4, 5, 3, 4, 5), start=1):",
            "        seq = []",
            "        for t in range(48):",
            "            base = math.sin((t + idx) / 3.0) + math.cos((t + 2 * idx) / 5.0)",
            "            if t >= lag:",
            "                base += 0.72 * seq[t - lag]",
            "            seq.append(round(base, 6))",
            "        missing = {12 + idx, 13 + idx, 24 + idx}",
            "        observed = [0.0 if pos in missing else value for pos, value in enumerate(seq)]",
            "        cases.append({'target_lag': lag, 'observed': observed})",
            "    return cases",
            "",
            "",
            "def best_lag(signal: list[float], max_lag: int = 8) -> int:",
            "    best = 1",
            "    best_score = float('inf')",
            "    for lag in range(1, max_lag + 1):",
            "        score = 0.0",
            "        count = 0",
            "        for idx in range(lag, len(signal)):",
            "            prev = signal[idx - lag]",
            "            cur = signal[idx]",
            "            if prev == 0.0 or cur == 0.0:",
            "                continue",
            "            score += abs(cur - prev)",
            "            count += 1",
            "        if count == 0:",
            "            continue",
            "        score /= count",
            "        if score < best_score:",
            "            best_score = score",
            "            best = lag",
            "    return best",
            "",
            "",
            "def local_global_lag(signal: list[float], max_lag: int = 8) -> int:",
            "    votes: dict[int, int] = {}",
            "    window = 12",
            "    for start in range(0, len(signal) - window + 1, 6):",
            "        segment = signal[start : start + window]",
            "        lag = best_lag(segment, max_lag=max_lag)",
            "        votes[lag] = votes.get(lag, 0) + 1",
            "    return sorted(votes.items(), key=lambda item: (item[1], -item[0]), reverse=True)[0][0]",
            "",
            "",
            "def main() -> None:",
            f"    metadata = {json.dumps({k: payload[k] for k in ['task_id', 'question', 'hypothesis', 'baseline', 'single_change', 'outcome_kind', 'review_verdict']}, ensure_ascii=True, indent=2)}",
            "    rows = []",
            "    for case in synthetic_cases():",
            "        signal = list(case['observed'])",
            "        baseline = best_lag(signal)",
            "        predicted = local_global_lag(signal)",
            "        rows.append({",
            "            'target_lag': case['target_lag'],",
            "            'baseline_lag': baseline,",
            "            'predicted_lag': predicted,",
            "            'correct': predicted == case['target_lag'],",
            "        })",
            "    metadata['reproduction_runs'] = rows",
            "    metadata['accuracy'] = round(sum(1 for row in rows if row['correct']) / max(1, len(rows)), 4)",
            "    print(json.dumps(metadata, ensure_ascii=False, indent=2))",
            "",
            "",
            'if __name__ == "__main__":',
            "    main()",
            "",
        ]
    )


def _experiment_script_body(*, task: dict[str, Any], checkpoint: dict[str, Any]) -> str:
    method_family = _manual_method_family(task) if _trigger_mode(task) == "manual" else "local_global_signal"
    if method_family == "generic_learning_model":
        return _perceptron_reproduction_script(task=task, checkpoint=checkpoint)
    return _lag_recovery_reproduction_script(task=task, checkpoint=checkpoint)


def _latest_round_record(checkpoint: dict[str, Any]) -> dict[str, Any]:
    issue_history = checkpoint.get("issue_history", [])
    if not isinstance(issue_history, list) or not issue_history:
        return {}
    record = issue_history[-1]
    return record if isinstance(record, dict) else {}


def _nonempty_ref(value: Any) -> bool:
    return str(value or "").strip().startswith("agn://")


def _valid_ref(value: Any) -> bool:
    ref = str(value or "").strip()
    if not ref.startswith("agn://"):
        return False
    try:
        path = resolve_ref_path(ref)
    except Exception:
        return False
    return path.exists() and path.is_file()


def _safe_task_id_local(task_id: str) -> str:
    raw = str(task_id or "").strip().replace("/", "_")
    raw = raw.lstrip(".")
    if not raw:
        raw = "unnamed"
    if len(raw) > 200:
        raw = raw[:200]
    return raw


def _task_manifest_has_ref(*, task_id: str, ref: str, attempt: int = ATTEMPT) -> bool:
    manifest_path = task_attempt_dir(_safe_task_id_local(task_id), max(1, int(attempt))) / "manifest.json"
    if not manifest_path.exists():
        return False
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    artifacts = payload.get("artifacts", {}) if isinstance(payload, dict) else {}
    if not isinstance(artifacts, dict):
        return False
    clean_ref = str(ref or "").strip()
    for artifact in artifacts.values():
        if not isinstance(artifact, dict):
            continue
        if str(artifact.get("ref", "")).strip() == clean_ref or str(artifact.get("legacy_ref", "")).strip() == clean_ref:
            return True
    return False


def _task_manifest_artifact(*, task_id: str, ref: str, attempt: int = ATTEMPT) -> dict[str, Any]:
    manifest_path = task_attempt_dir(_safe_task_id_local(task_id), max(1, int(attempt))) / "manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    artifacts = payload.get("artifacts", {}) if isinstance(payload, dict) else {}
    if not isinstance(artifacts, dict):
        return {}
    clean_ref = str(ref or "").strip()
    for artifact in artifacts.values():
        if not isinstance(artifact, dict):
            continue
        if str(artifact.get("ref", "")).strip() == clean_ref or str(artifact.get("legacy_ref", "")).strip() == clean_ref:
            return artifact
    return {}


def _valid_task_ref(value: Any, *, task_id: str) -> bool:
    ref = str(value or "").strip()
    if not _valid_ref(ref):
        return False
    return _task_manifest_has_ref(task_id=task_id, ref=ref, attempt=ATTEMPT)


def _load_json_ref(ref: Any) -> dict[str, Any]:
    clean = str(ref or "").strip()
    if not _valid_ref(clean):
        return {}
    try:
        payload = json.loads(read_ref_text(clean, mode="all", max_bytes=512 * 1024))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _role_init_ack_valid(payload: dict[str, Any], role: str) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    if str(payload.get("role", "")).strip() != role:
        return False
    if str(payload.get("ack", "")).strip() != "init_loaded":
        return False
    if str(payload.get("protocol_digest", "")).strip() != _role_init_digest(role):
        return False
    if str(payload.get("integrity_ack", "")).strip() != "truthfulness_first":
        return False
    if str(payload.get("failure_ack", "")).strip() != "failure_is_valid":
        return False
    if str(payload.get("fabrication_ack", "")).strip() != "no_fabrication":
        return False
    return True


def _message_event_exists(
    trace_id: str,
    *,
    ref: Any,
    actor: str = "",
    kind: str = "",
    round_no: int | None = None,
) -> bool:
    clean_ref = str(ref or "").strip()
    if not clean_ref:
        return False
    for event in load_events(trace_id):
        if str(event.get("event_type", "")).strip() != "RESEARCH_MESSAGE":
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if str(payload.get("message_ref", "")).strip() != clean_ref:
            continue
        if actor and str(payload.get("actor", payload.get("role", ""))).strip() != actor:
            continue
        if kind and str(payload.get("kind", "")).strip() != kind:
            continue
        if round_no is not None and int(payload.get("round", 0) or 0) != int(round_no):
            continue
        return True
    return False


def _payload_event_exists(trace_id: str, *, event_type: str, key: str, ref: Any) -> bool:
    clean_ref = str(ref or "").strip()
    if not clean_ref:
        return False
    for event in load_events(trace_id):
        if str(event.get("event_type", "")).strip() != str(event_type).strip():
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if str(payload.get(key, "")).strip() == clean_ref:
            return True
    return False


def _message_event_meta(
    trace_id: str,
    *,
    ref: Any,
    actor: str = "",
    kind: str = "",
    round_no: int | None = None,
) -> tuple[int | None, str]:
    clean_ref = str(ref or "").strip()
    if not clean_ref:
        return None, ""
    for idx, event in enumerate(load_events(trace_id)):
        if str(event.get("event_type", "")).strip() != "RESEARCH_MESSAGE":
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if str(payload.get("message_ref", "")).strip() != clean_ref:
            continue
        if actor and str(payload.get("actor", payload.get("role", ""))).strip() != actor:
            continue
        if kind and str(payload.get("kind", "")).strip() != kind:
            continue
        if round_no is not None and int(payload.get("round", 0) or 0) != int(round_no):
            continue
        return idx, str(event.get("ts", "")).strip()
    return None, ""


def _payload_event_meta(trace_id: str, *, event_type: str, key: str, ref: Any) -> tuple[int | None, str]:
    clean_ref = str(ref or "").strip()
    if not clean_ref:
        return None, ""
    for idx, event in enumerate(load_events(trace_id)):
        if str(event.get("event_type", "")).strip() != str(event_type).strip():
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if str(payload.get(key, "")).strip() != clean_ref:
            continue
        return idx, str(event.get("ts", "")).strip()
    return None, ""


def _nearest_preceding_event_meta(trace_id: str, *, event_type: str, before_index: int | None) -> tuple[int | None, str]:
    events = load_events(trace_id)
    if before_index is None:
        return None, ""
    upper = min(max(0, int(before_index) - 1), len(events) - 1)
    for idx in range(upper, -1, -1):
        event = events[idx]
        if str(event.get("event_type", "")).strip() != str(event_type).strip():
            continue
        return idx, str(event.get("ts", "")).strip()
    return None, ""


def _parse_iso(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _governance_missing(*, task: dict[str, Any], checkpoint: dict[str, Any], trace_id: str, phase: str) -> list[str]:
    missing: list[str] = []
    clean_phase = str(phase or "").strip().lower()
    task_id = str(task.get("id", "")).strip()
    trigger_mode = _trigger_mode(task)
    survey_ref = str(checkpoint.get("survey_ref", "")).strip()
    shortlist_ref = str(checkpoint.get("shortlist_ref", "")).strip()
    intake_ref = str(checkpoint.get("intake_ref", "")).strip()
    coordinator_refresh_ref = str(checkpoint.get("coordinator_refresh_ref", "")).strip()
    coordinator_preflight_ref = str(checkpoint.get("coordinator_preflight_ref", "")).strip()
    governance_lock_ref = str(checkpoint.get("governance_lock_ref", "")).strip()
    research_plan_ref = str(checkpoint.get("research_plan_ref", "")).strip()
    selection_decision_ref = str(checkpoint.get("selection_decision_ref", "")).strip()
    round_state_ref = str(checkpoint.get("round_state_ref", "")).strip()
    acceptance_spec_ref = str(task.get("acceptance_spec_ref", "")).strip()
    proposal_refs = checkpoint.get("proposal_refs", [])
    latest_round = _latest_round_record(checkpoint)
    issue_history = checkpoint.get("issue_history", [])
    if not isinstance(issue_history, list):
        issue_history = []
    round_no = int(checkpoint.get("round", 0) or 0)

    if clean_phase in {"manual_intake", "brief_wait", "selection_vote", "design", "execution", "writing", "review", "archive", "publish", "delivery", "done"}:
        if not _valid_task_ref(coordinator_refresh_ref, task_id=task_id):
            missing.append("coordinator_refresh_ref_missing")
        elif not _payload_event_exists(
            trace_id,
            event_type="RESEARCH_COORDINATOR_REFRESHED",
            key="coordinator_refresh_ref",
            ref=coordinator_refresh_ref,
        ):
            missing.append("coordinator_refresh_event_missing")
        if not _valid_task_ref(coordinator_preflight_ref, task_id=task_id):
            missing.append("coordinator_preflight_ref_missing")

    if trigger_mode == "manual":
        if clean_phase in {"manual_intake", "design", "execution", "writing", "review", "archive", "publish", "delivery", "done"}:
            if not _valid_task_ref(intake_ref, task_id=task_id):
                missing.append("intake_ref_missing")
            if not _valid_task_ref(governance_lock_ref, task_id=task_id):
                missing.append("governance_lock_ref_missing")
            if not _valid_task_ref(research_plan_ref, task_id=task_id):
                missing.append("research_plan_ref_missing")
    else:
        if not _valid_task_ref(survey_ref, task_id=task_id):
            missing.append("survey_ref_missing")
        if not _valid_task_ref(shortlist_ref, task_id=task_id):
            missing.append("shortlist_ref_missing")
        if clean_phase in {"brief_wait", "selection_vote", "design", "execution", "writing", "review", "archive", "publish", "delivery", "done"}:
            if not _valid_task_ref(str(checkpoint.get("daily_brief_ref", "")).strip(), task_id=task_id):
                missing.append("daily_brief_ref_missing")
            if not str(checkpoint.get("daily_brief_deadline", "")).strip():
                missing.append("brief_deadline_missing")
    criteria = task.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        missing.append("acceptance_criteria_missing")
    if not _valid_task_ref(acceptance_spec_ref, task_id=task_id):
        missing.append("acceptance_spec_ref_missing")
    if not isinstance(proposal_refs, list) or not any(_valid_task_ref(ref, task_id=task_id) for ref in proposal_refs):
        missing.append("proposal_refs_missing")
    if round_no < 1:
        missing.append("proposal_round_missing")
    governance_event_positions: list[tuple[str, int]] = []
    if trigger_mode != "manual" and _valid_task_ref(survey_ref, task_id=task_id):
        survey_event_idx, _ = _payload_event_meta(trace_id, event_type="RESEARCH_MANUAL_INTAKE_CREATED", key="survey_ref", ref=survey_ref)
        if survey_event_idx is None:
            survey_event_idx, _ = _payload_event_meta(trace_id, event_type="RESEARCH_SURVEY_CREATED", key="survey_ref", ref=survey_ref)
        if survey_event_idx is None:
            missing.append("survey_event_missing")
        else:
            governance_event_positions.append(("survey_event", survey_event_idx))
    if trigger_mode != "manual" and _valid_task_ref(shortlist_ref, task_id=task_id):
        shortlist_event_idx, _ = _payload_event_meta(trace_id, event_type="RESEARCH_SHORTLIST_CREATED", key="shortlist_ref", ref=shortlist_ref)
        if shortlist_event_idx is None:
            missing.append("shortlist_event_missing")
        else:
            governance_event_positions.append(("shortlist_event", shortlist_event_idx))

    if clean_phase in {"execution", "writing", "review", "archive", "publish", "delivery", "done", "experiment"}:
        if not isinstance(latest_round, dict) or not latest_round:
            missing.append("round_trace_missing")
        else:
            record_round_no = int(latest_round.get("round", round_no) or round_no)
            round_ref_checks = (
                ("proposal_ref", "coordinator", "proposal_packet"),
                ("executor_packet_ref", "coordinator", "topic_vote_packet"),
                ("reviewer_packet_ref", "coordinator", "topic_vote_packet"),
                ("executor_role_init_ref", "coordinator", "role_init_packet"),
                ("reviewer_role_init_ref", "coordinator", "role_init_packet"),
                ("executor_init_ack_ref", "executor", "role_init"),
                ("reviewer_init_ack_ref", "reviewer", "role_init"),
                ("executor_ref", "executor", "topic_vote"),
                ("reviewer_ref", "reviewer", "topic_vote"),
            )
            for key, actor, kind in round_ref_checks:
                ref = latest_round.get(key, "")
                if not _valid_task_ref(ref, task_id=task_id):
                    missing.append(f"{key}_missing")
                    continue
                event_idx, _ = _message_event_meta(trace_id, ref=ref, actor=actor, kind=kind, round_no=record_round_no)
                if event_idx is None:
                    missing.append(f"{key}_event_missing")
                    continue
                governance_event_positions.append((f"{key}_event", event_idx))
            executor_init = _load_json_ref(latest_round.get("executor_init_ack_ref", ""))
            reviewer_init = _load_json_ref(latest_round.get("reviewer_init_ack_ref", ""))
            if executor_init and not _role_init_ack_valid(executor_init, "executor"):
                missing.append("executor_init_ack_invalid")
            if reviewer_init and not _role_init_ack_valid(reviewer_init, "reviewer"):
                missing.append("reviewer_init_ack_invalid")
            executor_vote_payload = _load_json_ref(latest_round.get("executor_ref", ""))
            reviewer_vote_payload = _load_json_ref(latest_round.get("reviewer_ref", ""))
            if executor_vote_payload and str(executor_vote_payload.get("role", "")).strip() != "executor":
                missing.append("executor_vote_invalid")
            if reviewer_vote_payload and str(reviewer_vote_payload.get("role", "")).strip() != "reviewer":
                missing.append("reviewer_vote_invalid")
        if not _valid_task_ref(round_state_ref, task_id=task_id):
            missing.append("round_state_ref_missing")
        if not isinstance(checkpoint.get("current_candidate"), dict):
            missing.append("current_candidate_missing")
        if not str(checkpoint.get("selected_topic_id", "")).strip():
            missing.append("selected_topic_id_missing")
        if trigger_mode == "auto" and not _valid_task_ref(selection_decision_ref, task_id=task_id):
            missing.append("selection_decision_ref_missing")
        if round_no >= 3:
            if len([row for row in issue_history if isinstance(row, dict)]) < 2:
                missing.append("round_rejection_history_missing")
            else:
                seen_rounds = {int(row.get("round", 0) or 0) for row in issue_history if isinstance(row, dict)}
                if 1 not in seen_rounds or 2 not in seen_rounds:
                    missing.append("round_rejection_history_missing")
            if not bool(checkpoint.get("entered_third_round", False)):
                missing.append("third_round_marker_missing")
            if not _valid_task_ref(checkpoint.get("forced_decision_ref", ""), task_id=task_id):
                missing.append("forced_decision_ref_missing")
            else:
                forced_idx, _ = _message_event_meta(
                    trace_id,
                    ref=checkpoint.get("forced_decision_ref", ""),
                    actor="coordinator",
                    kind="forced_decision",
                    round_no=3,
                )
                if forced_idx is None:
                    missing.append("forced_decision_event_missing")
                else:
                    governance_event_positions.append(("forced_decision_event", forced_idx))
        else:
            executor_decision = str(latest_round.get("executor_decision", "")).strip().lower() if isinstance(latest_round, dict) else ""
            reviewer_decision = str(latest_round.get("reviewer_decision", "")).strip().lower() if isinstance(latest_round, dict) else ""
            if executor_decision != "yes":
                missing.append("executor_yes_missing")
            if reviewer_decision != "yes":
                missing.append("reviewer_yes_missing")
            if round_no == 2:
                prior_rejections = [row for row in issue_history if isinstance(row, dict) and int(row.get("round", 0) or 0) == 1]
                if not prior_rejections:
                    missing.append("round1_rejection_history_missing")

    if clean_phase in {"writing", "review", "archive", "publish", "delivery", "done"}:
        if not _valid_task_ref(checkpoint.get("experiment_ref", ""), task_id=task_id):
            missing.append("experiment_ref_missing")
        if not _valid_task_ref(checkpoint.get("experiment_summary_ref", ""), task_id=task_id):
            missing.append("experiment_summary_ref_missing")
        if not _valid_task_ref(checkpoint.get("experiment_raw_ref", ""), task_id=task_id):
            missing.append("executor_result_ref_missing")
        experiment_completed_idx: int | None = None
        experiment_started_idx: int | None = None
        experiment_started_ts = ""
        if _valid_task_ref(checkpoint.get("experiment_ref", ""), task_id=task_id):
            experiment_completed_idx, _ = _payload_event_meta(
                trace_id,
                event_type="RESEARCH_EXPERIMENT_COMPLETED",
                key="experiment_ref",
                ref=checkpoint.get("experiment_ref", ""),
            )
            if experiment_completed_idx is None:
                missing.append("experiment_event_missing")
            else:
                experiment_started_idx, experiment_started_ts = _nearest_preceding_event_meta(
                    trace_id,
                    event_type="RESEARCH_EXPERIMENT_STARTED",
                    before_index=experiment_completed_idx,
                )
                if experiment_started_idx is None:
                    missing.append("experiment_started_event_missing")
                else:
                    for label, position in governance_event_positions:
                        if position > experiment_started_idx:
                            missing.append(f"{label}_after_execution_started")
                    acceptance_spec_artifact = _task_manifest_artifact(task_id=task_id, ref=acceptance_spec_ref, attempt=ATTEMPT)
                    acceptance_spec_ts = _parse_iso(acceptance_spec_artifact.get("updated_at", ""))
                    started_dt = _parse_iso(experiment_started_ts)
                    if acceptance_spec_ts is not None and started_dt is not None and acceptance_spec_ts > started_dt:
                        missing.append("acceptance_spec_after_execution_started")

    if clean_phase in {"review", "archive", "publish", "delivery", "done"}:
        if not (
            _valid_task_ref(checkpoint.get("paper_ref", ""), task_id=task_id)
            or _valid_task_ref(checkpoint.get("failure_note_ref", ""), task_id=task_id)
        ):
            missing.append("paper_or_failure_note_missing")
        if (
            _valid_task_ref(checkpoint.get("paper_ref", ""), task_id=task_id)
            or _valid_task_ref(checkpoint.get("failure_note_ref", ""), task_id=task_id)
        ) and not _payload_event_exists(trace_id, event_type="RESEARCH_PAPER_WRITTEN", key="artifact_ref", ref=checkpoint.get("paper_ref", "") or checkpoint.get("failure_note_ref", "")):
            missing.append("paper_event_missing")

    if clean_phase in {"archive", "publish", "delivery", "done"}:
        final_review = checkpoint.get("final_review")
        if not isinstance(final_review, dict) or not final_review:
            missing.append("final_review_missing")
        if not _valid_task_ref(checkpoint.get("review_verdict_ref", ""), task_id=task_id):
            missing.append("review_verdict_ref_missing")
        elif not _payload_event_exists(trace_id, event_type="RESEARCH_FINAL_REVIEW", key="verdict_ref", ref=checkpoint.get("review_verdict_ref", "")):
            missing.append("review_event_missing")

    if clean_phase in {"publish", "delivery", "done"}:
        if not _valid_task_ref(checkpoint.get("essay_ref", ""), task_id=task_id):
            missing.append("essay_ref_missing")
        if not _valid_task_ref(checkpoint.get("code_bundle_ref", ""), task_id=task_id):
            missing.append("code_bundle_ref_missing")
        if not _valid_task_ref(checkpoint.get("raw_results_ref", ""), task_id=task_id):
            missing.append("raw_results_ref_missing")
        if not _valid_task_ref(checkpoint.get("data_record_ref", ""), task_id=task_id):
            missing.append("data_record_ref_missing")
        if not _valid_task_ref(checkpoint.get("reproduce_ref", ""), task_id=task_id):
            missing.append("reproduce_ref_missing")
        if not _valid_task_ref(checkpoint.get("result_summary_ref", ""), task_id=task_id):
            missing.append("result_summary_ref_missing")
        if not _valid_task_ref(checkpoint.get("archive_ref", ""), task_id=task_id):
            missing.append("archive_ref_missing")
        if not _valid_task_ref(checkpoint.get("trace_index_ref", ""), task_id=task_id):
            missing.append("trace_index_ref_missing")
        if not _valid_task_ref(checkpoint.get("final_report_ref", ""), task_id=task_id):
            missing.append("final_report_ref_missing")
    if clean_phase in {"done"}:
        if not _valid_task_ref(checkpoint.get("publish_receipt_ref", ""), task_id=task_id):
            missing.append("publish_receipt_ref_missing")
        if not str(checkpoint.get("commit_hash", "")).strip():
            missing.append("commit_hash_missing")
        if str(checkpoint.get("push_status", "")).strip() != "ok":
            missing.append("push_status_not_ok")
        if not _valid_task_ref(checkpoint.get("telegram_receipt_ref", ""), task_id=task_id):
            missing.append("telegram_receipt_ref_missing")
        if _valid_task_ref(checkpoint.get("final_report_ref", ""), task_id=task_id):
            if not _payload_event_exists(
                trace_id,
                event_type="RESEARCH_ADMIN_DELIVERED",
                key="final_report_ref",
                ref=checkpoint.get("final_report_ref", ""),
            ):
                missing.append("admin_delivery_event_missing")

    return sorted(set(missing))


def _delivery_ack_required(task: dict[str, Any]) -> bool:
    return str(task.get("task_kind", "")).strip() == "daily_research"


def _audit_events_path() -> Path:
    return ROOT / "audit" / "events.jsonl"


def _outbox_path() -> Path:
    return ROOT / "runtime" / "agn_telegram_outbox.jsonl"


def _completion_report_queued(*, task_id: str, trace_id: str, message_id: str, final_report_ref: str) -> bool:
    clean_message_id = str(message_id or "").strip()
    clean_final_report_ref = str(final_report_ref or "").strip()
    if not clean_message_id or not clean_final_report_ref:
        return False
    path = _outbox_path()
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return False
    for raw in reversed(lines[-4000:]):
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("message_id", "")).strip() != clean_message_id:
            continue
        if str(payload.get("task_id", "")).strip() != str(task_id).strip():
            continue
        if str(payload.get("correlation_id", "")).strip() != str(trace_id).strip():
            continue
        text = str(payload.get("text", "")).strip()
        if "[AGN research] completed" not in text:
            continue
        if f"final_report_ref={clean_final_report_ref}" not in text:
            continue
        return True
    return False


def _telegram_delivery_confirmed(*, task_id: str, trace_id: str, message_id: str, final_report_ref: str) -> bool:
    clean_message_id = str(message_id or "").strip()
    clean_final_report_ref = str(final_report_ref or "").strip()
    if not clean_message_id or not clean_final_report_ref:
        return False
    if not _completion_report_queued(
        task_id=task_id,
        trace_id=trace_id,
        message_id=clean_message_id,
        final_report_ref=clean_final_report_ref,
    ):
        return False
    path = _audit_events_path()
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return False
    for raw in reversed(lines[-4000:]):
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("action", "")).strip() != "telegram_sent":
            continue
        if str(payload.get("message_id", "")).strip() != clean_message_id:
            continue
        if str(payload.get("task_id", "")).strip() != str(task_id).strip():
            continue
        if str(payload.get("correlation_id", "")).strip() != str(trace_id).strip():
            continue
        stage = str(payload.get("stage", "")).strip().lower()
        if stage and not stage.startswith("explicit:"):
            continue
        return True
    return False


def _completion_delivery_missing(*, task: dict[str, Any], trace_id: str, checkpoint: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    final_report_ref = str(checkpoint.get("final_report_ref", "")).strip()
    if not _payload_event_exists(trace_id, event_type="RESEARCH_ADMIN_DELIVERED", key="final_report_ref", ref=final_report_ref):
        missing.append("admin_delivery_event_missing")
    if _delivery_ack_required(task):
        delivered = _telegram_delivery_confirmed(
            task_id=str(task.get("id", "")).strip(),
            trace_id=trace_id,
            message_id=str(checkpoint.get("admin_completion_message_id", "")).strip(),
            final_report_ref=final_report_ref,
        )
        if not delivered:
            missing.append("admin_delivery_ack_missing")
    return missing


def _protocol_block(
    *,
    task_id: str,
    trace_id: str,
    checkpoint: dict[str, Any],
    task: dict[str, Any],
    phase: str,
    missing: list[str],
    repair_phase: str,
    reason: str,
    state: str,
) -> dict[str, Any]:
    clean_missing = [item for item in missing if str(item or "").strip()]
    artifact = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id=f"protocol_block_{phase}",
        payload={
            "task_id": task_id,
            "trace_id": trace_id,
            "phase": phase,
            "repair_phase": repair_phase,
            "reason": reason,
            "missing": clean_missing,
            "generated_at": utc_now_iso(),
        },
        filename=f"protocol_block_{phase}.json",
        source="research_flow",
    )
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="PROTOCOL_VIOLATION",
        payload={
            "phase": phase,
            "repair_phase": repair_phase,
            "reason": reason,
            "missing": clean_missing,
            "protocol_block_ref": artifact.ref,
        },
        severity="error",
    )
    _ensure_state(trace_id=trace_id, task_id=task_id, to_state=state, reason=reason)
    return _merge_checkpoint(
        task_id,
        checkpoint,
        research_phase=repair_phase,
        research_status="protocol_blocked",
        protocol_blocked=True,
        protocol_block_reason=reason,
        protocol_block_ref=artifact.ref,
        protocol_violation_count=int(checkpoint.get("protocol_violation_count", 0) or 0) + 1,
        governance_ready=False,
        governance_missing=clean_missing,
        completion_ready=False,
        recent_event_label="PROTOCOL_VIOLATION",
        last_event_time=utc_now_iso(),
    )


def _require_governance(
    *,
    task_id: str,
    trace_id: str,
    checkpoint: dict[str, Any],
    task: dict[str, Any],
    phase: str,
    repair_phase: str,
    state: str,
) -> tuple[dict[str, Any], bool]:
    missing = _governance_missing(task=task, checkpoint=checkpoint, trace_id=trace_id, phase=phase)
    if missing:
        checkpoint = _protocol_block(
            task_id=task_id,
            trace_id=trace_id,
            checkpoint=checkpoint,
            task=task,
            phase=phase,
            missing=missing,
            repair_phase=repair_phase,
            reason=f"governance_prerequisite_missing:{phase}",
            state=state,
        )
        return checkpoint, False
    checkpoint = _merge_checkpoint(
        task_id,
        checkpoint,
        protocol_blocked=False,
        protocol_block_reason="",
        protocol_block_ref="",
        governance_ready=True,
        governance_missing=[],
    )
    return checkpoint, True


def _sync_task_contract(
    *,
    task_id: str,
    trace_id: str,
    candidate: dict[str, Any],
    round_no: int,
    proposal_version: int,
) -> None:
    task = _task(trace_id, task_id)
    task["task_kind"] = "daily_research"
    task["research_axis"] = str(candidate.get("axis", "")).strip()
    task["question"] = str(candidate.get("problem", "")).strip()
    task["hypothesis"] = str(candidate.get("core_idea", "")).strip()
    task["baseline"] = str(candidate.get("baseline", "")).strip()
    task["single_change"] = str(candidate.get("single_change", "")).strip() or (
        f"Evaluate `{str(candidate.get('title', '')).strip()}` against the stated baseline with one constrained method family change."
    )
    task["round"] = max(0, int(round_no))
    task["proposal_version"] = max(0, int(proposal_version))
    task["decision_mode"] = "three_round_coordinator"
    task["failure_mode_allowed"] = True
    _save_task(task)


def _ensure_state(*, trace_id: str, task_id: str, to_state: str, reason: str) -> None:
    checkpoint = load_checkpoint(task_id) or {}
    current = str(checkpoint.get("state", "CREATED")).strip().upper() or "CREATED"
    target = str(to_state).strip().upper()
    if current == target:
        return
    transition_state(trace_id=trace_id, task_id=task_id, to_state=target, reason=reason)


def _append_message_ref(task_id: str, checkpoint: dict[str, Any], ref: str, packet_chars: int = 0) -> dict[str, Any]:
    refs = list(checkpoint.get("message_refs", []) or [])
    refs.append(ref)
    return _merge_checkpoint(
        task_id,
        checkpoint,
        message_refs=refs[-64:],
        message_count=int(checkpoint.get("message_count", 0) or 0) + 1,
        packet_chars_total=int(checkpoint.get("packet_chars_total", 0) or 0) + max(0, int(packet_chars)),
        max_packet_chars=max(int(checkpoint.get("max_packet_chars", 0) or 0), max(0, int(packet_chars))),
        last_event_time=utc_now_iso(),
    )


def _record_message(
    *,
    task_id: str,
    trace_id: str,
    checkpoint: dict[str, Any],
    actor: str,
    surface: str,
    kind: str,
    round_no: int,
    content: str,
    packet_chars: int = 0,
    in_reply_to: str = "",
    event_type: str = "RESEARCH_MESSAGE",
) -> tuple[dict[str, Any], str]:
    artifact = write_text_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id=f"{actor}_{kind}_round_{round_no}_{uuid4().hex[:6]}",
        content=content,
        media_type="application/json" if content.lstrip().startswith("{") else "text/plain",
        filename=f"{actor}_{kind}_round_{round_no}_{uuid4().hex[:6]}.txt",
        source="research_flow",
    )
    preview = content.replace("\n", " ")[:220]
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type=event_type,
        payload={
            "actor": actor,
            "role": actor,
            "surface": surface,
            "kind": kind,
            "attempt": ATTEMPT,
            "round": round_no,
            "message_ref": artifact.ref,
            "preview": preview,
            "packet_chars": packet_chars,
            "sha256": digest,
            "in_reply_to": in_reply_to,
        },
    )
    return _append_message_ref(task_id, checkpoint, artifact.ref, packet_chars=packet_chars), artifact.ref


def _record_json_message(
    *,
    task_id: str,
    trace_id: str,
    checkpoint: dict[str, Any],
    actor: str,
    surface: str,
    kind: str,
    round_no: int,
    payload: dict[str, Any],
    in_reply_to: str = "",
) -> tuple[dict[str, Any], str]:
    rendered = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
    return _record_message(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        actor=actor,
        surface=surface,
        kind=kind,
        round_no=round_no,
        content=rendered,
        packet_chars=len(rendered),
        in_reply_to=in_reply_to,
    )


def _proposal_packet(*, task: dict[str, Any], candidate: dict[str, Any], checkpoint: dict[str, Any], round_no: int) -> dict[str, Any]:
    issue_history = checkpoint.get("issue_history", [])
    if not isinstance(issue_history, list):
        issue_history = []
    return {
        "task_id": str(task.get("id", "")).strip(),
        "task_kind": "daily_research",
        "proposal_version": round_no,
        "round": round_no,
        "decision_mode": str(task.get("decision_mode", "")).strip() or "three_round_coordinator",
        "research_mode": str(task.get("research_mode", "")).strip() or "autonomy",
        "question": str(task.get("question", "")).strip(),
        "hypothesis": str(task.get("hypothesis", "")).strip(),
        "baseline": str(task.get("baseline", "")).strip(),
        "single_change": str(task.get("single_change", "")).strip(),
        "budget": task.get("budget", {}),
        "proposal": _proposal_outline(candidate, task),
        "prior_issue_tail": issue_history[-2:],
    }


def _latest_event_label(trace_id: str) -> str:
    events = load_events(trace_id)
    if not events:
        return "none"
    latest = events[-1]
    event_type = str(latest.get("event_type", "")).strip() or "unknown"
    payload = latest.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}
    round_no = str(payload.get("round", "")).strip()
    actor = str(payload.get("actor", payload.get("role", ""))).strip()
    suffix = ""
    if round_no:
        suffix += f" round={round_no}"
    if actor:
        suffix += f" actor={actor}"
    return f"{event_type}{suffix}"


def _trace_index_payload(*, task_id: str, trace_id: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for event in load_events(trace_id):
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        rows.append(
            {
                "task_id": task_id,
                "correlation_id": trace_id,
                "event_id": str(event.get("event_id", "")).strip(),
                "ts": str(event.get("ts", "")).strip(),
                "event_type": str(event.get("event_type", "")).strip(),
                "attempt": int(payload.get("attempt", ATTEMPT) or ATTEMPT),
                "round": int(payload.get("round", 0) or 0),
                "role": str(payload.get("role", payload.get("actor", ""))).strip(),
                "refs": [str(ref).strip() for ref in payload.values() if isinstance(ref, str) and str(ref).startswith("agn://")],
            }
        )
    return {
        "task_id": task_id,
        "correlation_id": trace_id,
        "entry_count": len(rows),
        "entries": rows,
    }


def _write_revision_artifact(*, task_id: str, checkpoint: dict[str, Any], issue: str, risk: str, minimal_fix: str) -> str:
    outcome_kind = str(checkpoint.get("outcome_kind", "")).strip()
    current_ref = str(checkpoint.get("paper_ref", "") if outcome_kind == "mini_paper" else checkpoint.get("failure_note_ref", "")).strip()
    current_body = read_ref_text(current_ref, mode="all", max_bytes=512 * 1024) if current_ref.startswith("agn://") else ""
    revised_body = (
        f"{current_body.rstrip()}\n\n"
        "## Review Revision\n"
        f"- issue: {issue}\n"
        f"- risk: {risk}\n"
        f"- minimal_fix: {minimal_fix}\n"
        "- coordinator_action: tightened the final note and preserved the reviewer concern verbatim.\n"
    )
    artifact = write_text_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id=f"{outcome_kind or 'paper'}_review_revision",
        content=revised_body,
        media_type="text/markdown",
        filename=f"{outcome_kind or 'paper'}_review_revision.md",
        source="research_flow",
    )
    return artifact.ref


def _proposal_outline(candidate: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    return {
        "topic_id": str(candidate.get("topic_id", "")).strip(),
        "research_axis": _prefer(task.get("research_axis", ""), candidate.get("axis", "")),
        "title": _trim(candidate.get("title", ""), 160),
        "question": _trim(_prefer(task.get("question", ""), candidate.get("problem", "")), 220),
        "hypothesis": _trim(_prefer(task.get("hypothesis", ""), candidate.get("core_idea", "")), 220),
        "baseline": _trim(_prefer(task.get("baseline", ""), candidate.get("baseline", "")), 160),
        "single_change": _trim(_prefer(task.get("single_change", ""), candidate.get("title", "")), 180),
        "data_ready": bool(candidate.get("data_ready", False)),
        "fixed_budget": bool(candidate.get("fixed_budget", False)),
        "falsifiable": bool(candidate.get("falsifiable", False)),
        "degrade_ready": bool(candidate.get("degrade_ready", False)),
        "external_dependency": bool(candidate.get("external_dependency", False)),
        "method_family": str(candidate.get("required_method_family", candidate.get("method_family", ""))).strip(),
        "same_family_only": bool(candidate.get("same_family_only", False)),
    }


def _executor_vote_packet(
    *,
    task: dict[str, Any],
    candidate: dict[str, Any],
    checkpoint: dict[str, Any],
    round_no: int,
) -> dict[str, Any]:
    return {
        "step": "task",
        "packet_schema": _packet_schema("executor", "topic_vote"),
        "task_id": str(task.get("id", "")).strip(),
        "role": "executor",
        "goal": _role_goal("executor", "topic_vote"),
        "current_round": round_no,
        "task_question": _trim(task.get("question", ""), 220),
        "baseline": _trim(task.get("baseline", ""), 160),
        "single_change": _trim(task.get("single_change", ""), 180),
        "budget": task.get("budget", {}),
        "current_action_required": "Return yes or no on executability.",
        "output_schema": {
            "decision": "yes|no",
            "if_no": ["problem", "risk", "minimal_change"],
        },
        "proposal": _proposal_outline(candidate, task),
        "evidence_refs": {
            "survey_ref": str(checkpoint.get("survey_ref", "")).strip(),
            "shortlist_ref": str(checkpoint.get("shortlist_ref", "")).strip(),
        },
        "role_init_paths": _role_init_paths("executor"),
    }


def _reviewer_vote_packet(
    *,
    task: dict[str, Any],
    candidate: dict[str, Any],
    checkpoint: dict[str, Any],
    round_no: int,
) -> dict[str, Any]:
    return {
        "step": "task",
        "packet_schema": _packet_schema("reviewer", "topic_vote"),
        "task_id": str(task.get("id", "")).strip(),
        "role": "reviewer",
        "goal": _role_goal("reviewer", "topic_vote"),
        "current_round": round_no,
        "current_proposal": _proposal_outline(candidate, task),
        "budget": task.get("budget", {}),
        "current_action_required": "Return yes or no on proposal soundness only.",
        "if_reject_must_include": ["problem", "risk", "minimal_change"],
        "output_schema": {
            "decision": "yes|no",
            "if_no": ["problem", "risk", "minimal_change"],
        },
        "evidence_refs": {
            "survey_ref": str(checkpoint.get("survey_ref", "")).strip(),
            "shortlist_ref": str(checkpoint.get("shortlist_ref", "")).strip(),
        },
        "role_init_paths": _role_init_paths("reviewer"),
    }


def _reviewer_final_review_packet(*, task_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
    candidate = checkpoint.get("current_candidate")
    if not isinstance(candidate, dict):
        candidate = {}
    experiment_result = checkpoint.get("experiment_result")
    if not isinstance(experiment_result, dict):
        experiment_result = {}
    experiment_metrics = experiment_result.get("metrics")
    if not isinstance(experiment_metrics, dict):
        experiment_metrics = {}
    paper_ref = str(checkpoint.get("paper_ref", "")).strip()
    failure_note_ref = str(checkpoint.get("failure_note_ref", "")).strip()
    paper_excerpt = ""
    paper_source_ref = paper_ref or failure_note_ref
    if _valid_task_ref(paper_source_ref, task_id=task_id):
        paper_excerpt = read_ref_text(paper_source_ref, mode="all", max_bytes=8000)[:3500]
    evidence_refs = {
        "survey_ref": str(checkpoint.get("survey_ref", "")).strip(),
        "shortlist_ref": str(checkpoint.get("shortlist_ref", "")).strip(),
        "daily_brief_ref": str(checkpoint.get("daily_brief_ref", "")).strip(),
        "daily_brief_deadline": str(checkpoint.get("daily_brief_deadline", "")).strip(),
        "experiment_ref": str(checkpoint.get("experiment_ref", "")).strip(),
        "paper_ref": str(checkpoint.get("paper_ref", "")).strip(),
        "failure_note_ref": str(checkpoint.get("failure_note_ref", "")).strip(),
    }
    evidence_boundary = [
        evidence_refs["survey_ref"],
        evidence_refs["shortlist_ref"],
        evidence_refs["experiment_ref"],
        evidence_refs["paper_ref"],
        evidence_refs["failure_note_ref"],
    ]
    evidence_boundary = [ref for ref in evidence_boundary if ref.startswith("agn://")]
    return {
        "step": "task",
        "packet_schema": _packet_schema("reviewer", "final_review"),
        "task_id": task_id,
        "role": "reviewer",
        "goal": _role_goal("reviewer", "final_review"),
        "current_round": max(1, int(checkpoint.get("round", 1) or 1)),
        "current_action_required": "Return APPROVED, REVISION_ONCE, or FAILURE_ARCHIVE on archive completeness, empirical execution authenticity, and evidence boundary.",
        "review_scope": {
            "outcome_kind": str(checkpoint.get("outcome_kind", "")).strip(),
            "message_count": int(checkpoint.get("message_count", 0) or 0),
            "honest_failure": str(checkpoint.get("outcome_kind", "")).strip() == "failure_note",
            "review_revision_count": int(checkpoint.get("review_revision_count", 0) or 0),
            "empirical_execution": bool(checkpoint.get("empirical_execution", experiment_result.get("empirical_execution", False))),
            "truthfulness_status": str(checkpoint.get("truthfulness_status", experiment_result.get("truthfulness_status", ""))).strip(),
            "truthfulness_reason": str(checkpoint.get("truthfulness_reason", experiment_result.get("truthfulness_reason", ""))).strip(),
            "unverified_metrics_present": isinstance(experiment_result.get("unverified_metrics"), dict) and bool(experiment_result.get("unverified_metrics")),
        },
        "proposal": {
            "topic_id": str(candidate.get("topic_id", "")).strip(),
            "title": str(candidate.get("title", "")).strip(),
            "problem": str(candidate.get("problem", "")).strip(),
            "core_idea": str(candidate.get("core_idea", "")).strip(),
            "baseline": str(candidate.get("baseline", "")).strip(),
            "single_change": str(candidate.get("single_change", "")).strip(),
            "method_family": str(candidate.get("required_method_family", candidate.get("method_family", ""))).strip(),
        },
        "experiment_summary": {
            "status": str(experiment_result.get("status", "")).strip(),
            "strategy": str(experiment_result.get("strategy", "")).strip(),
            "metrics": experiment_metrics,
            "unverified_metrics": experiment_result.get("unverified_metrics", {}) if isinstance(experiment_result.get("unverified_metrics"), dict) else {},
            "notes": list(experiment_result.get("notes", []) or []),
            "error": str(experiment_result.get("error", "")).strip(),
        },
        "paper_excerpt": paper_excerpt,
        "issue_history_tail": list(checkpoint.get("issue_history", []) or [])[-2:],
        "evidence_refs": evidence_refs,
        "evidence_boundary": evidence_boundary,
        "output_schema": {
            "verdict": "APPROVED|REVISION_ONCE|FAILURE_ARCHIVE",
            "evidence_boundary": "list[str]",
            "if_revision_once": ["issue", "risk", "minimal_fix"],
            "if_failure_archive": ["failure_type", "reason", "rerun_worth_it", "evidence_boundary"],
        },
        "role_init_paths": _role_init_paths("reviewer"),
    }


def _executor_experiment_packet(
    *,
    task: dict[str, Any],
    candidate: dict[str, Any],
    checkpoint: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    strategies = ["full", *[str(item).strip() for item in profile.get("degrade_chain", []) if str(item).strip()]]
    start_index = min(max(0, int(checkpoint.get("degrade_index", 0) or 0)), len(strategies) - 1)
    if bool(checkpoint.get("force_degrade", False)):
        start_index = min(start_index + 1, len(strategies) - 1)
    return {
        "step": "task",
        "packet_schema": _packet_schema("executor", "run_experiment"),
        "task_id": str(task.get("id", "")).strip(),
        "role": "executor",
        "goal": _role_goal("executor", "run_experiment"),
        "current_round": max(1, int(checkpoint.get("round", 1) or 1)),
        "task_question": _trim(task.get("question", ""), 220),
        "baseline": _trim(task.get("baseline", ""), 160),
        "single_change": _trim(task.get("single_change", ""), 180),
        "budget": task.get("budget", {}),
        "current_action_required": "Run the experiment empirically if possible and degrade through the provided strategies without fabricating results.",
        "output_schema": {
            "status": "ok|degraded|failure_note",
            "strategy": "string",
            "metrics": "object",
            "notes": "list[string]",
            "error": "string when failed",
            "execution_evidence_paths": "list[string] when empirical execution happened",
        },
        "proposal": _proposal_outline(candidate, task),
        "strategy_candidates": strategies[start_index:],
        "required_method_family": str(candidate.get("required_method_family", candidate.get("method_family", ""))).strip(),
        "same_family_only": bool(candidate.get("same_family_only", False)),
        "allow_trusted_dependency_installs": bool(task.get("allow_trusted_dependency_installs", True)),
        "trusted_dependency_sources": list(task.get("trusted_dependency_sources", []) or []),
        "simulate_full_failure_once": bool(candidate.get("simulate_experiment_failure_once", False))
        and not bool(checkpoint.get("experiment_failure_seen", False))
        and not bool(checkpoint.get("force_degrade", False)),
        "role_init_paths": _role_init_paths("executor"),
    }


def _candidate_score(candidate: dict[str, Any]) -> float:
    score = float(candidate.get("learning_value", 0.0) or 0.0)
    if bool(candidate.get("data_ready", False)):
        score += 0.10
    if bool(candidate.get("baseline_clear", False)):
        score += 0.08
    if bool(candidate.get("fixed_budget", False)):
        score += 0.08
    if bool(candidate.get("safe_fallback", False)):
        score += 0.08
    if bool(candidate.get("external_dependency", False)):
        score -= 0.20
    return round(score, 4)


def _allowed_axes(profile: dict[str, Any]) -> set[str]:
    raw = profile.get("allowed_axes", [])
    return {str(item).strip() for item in raw if str(item).strip()}


def _build_survey(profile: dict[str, Any]) -> dict[str, Any]:
    allowed = _allowed_axes(profile)
    rows: list[dict[str, Any]] = []

    # Static candidate pool from profile.
    for raw in profile.get("candidate_pool", []) or []:
        if not isinstance(raw, dict):
            continue
        candidate = deepcopy(raw)
        candidate["axis_allowed"] = str(candidate.get("axis", "")).strip() in allowed
        candidate["score"] = _candidate_score(candidate)
        candidate["source"] = "profile"
        rows.append(candidate)

    # Live AI topic discovery via web search + LLM selection.
    web_candidates = _discover_web_topics(profile)
    for candidate in web_candidates:
        candidate["axis_allowed"] = str(candidate.get("axis", candidate.get("research_axis", ""))).strip() in allowed or not allowed
        candidate["score"] = _candidate_score(candidate) + 0.5  # Boost fresh topics.
        candidate["source"] = "web_discovery"
        rows.append(candidate)

    rows.sort(key=lambda item: (float(item.get("score", 0.0)), str(item.get("topic_id", ""))), reverse=True)
    shortlist = rows[:4]
    return {
        "generated_at": utc_now_iso(),
        "allowed_axes": sorted(allowed),
        "focus_topics": profile.get("focus_topics", []),
        "candidates": rows,
        "shortlist_ids": [str(item.get("topic_id", "")).strip() for item in shortlist],
    }


def _discover_web_topics(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Use web search + LLM to discover fresh research topics."""
    try:
        from research_llm import survey_ai_topics, select_research_topic
    except ImportError:
        return []
    focus = profile.get("focus_topics", [])
    query_parts = ["latest AI machine learning research"]
    if focus:
        query_parts.append(" ".join(str(t) for t in focus[:3]))
    web_results = survey_ai_topics(query=" ".join(query_parts), limit=10)
    if not web_results:
        return []
    selected = select_research_topic(web_results, profile)
    if not selected or not selected.get("topic_id"):
        return []
    # Ensure all required fields.
    selected.setdefault("data_ready", True)
    selected.setdefault("fixed_budget", True)
    selected.setdefault("external_dependency", False)
    selected.setdefault("baseline_clear", True)
    selected.setdefault("degrade_ready", True)
    selected.setdefault("web_sources", web_results[:3])
    return [selected]


def _topic_by_id(profile: dict[str, Any], topic_id: str) -> dict[str, Any] | None:
    target = str(topic_id).strip()
    for candidate in profile.get("candidate_pool", []) or []:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("topic_id", "")).strip() == target:
            item = deepcopy(candidate)
            item["axis_allowed"] = str(item.get("axis", "")).strip() in _allowed_axes(profile)
            return item
    return None


def _choose_shortlist_candidate(survey: dict[str, Any], index: int) -> dict[str, Any]:
    candidates = survey.get("candidates", [])
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("survey_candidates_missing")
    bounded = min(max(0, index), len(candidates) - 1)
    return deepcopy(candidates[bounded])


def _revise_candidate(*, candidate: dict[str, Any], scenario: str) -> dict[str, Any]:
    revised = deepcopy(candidate)
    revised["topic_id"] = f"{str(candidate.get('topic_id', 'topic')).strip()}_rev1"
    revised["title"] = f"{str(candidate.get('title', 'topic')).strip()} (synthetic narrowing)"
    revised["problem"] = "Use the same core idea, but move to synthetic data and a single fixed nightly budget."
    revised["core_idea"] = "Keep the original topic axis, but remove the unstable data dependency and narrow execution to one small surrogate test."
    revised["data_ready"] = True
    revised["fixed_budget"] = True
    revised["external_dependency"] = False
    revised["baseline_clear"] = True
    revised["degrade_ready"] = True
    revised["axis_allowed"] = True
    revised["survey_note"] = "Revision 1 removes the external dependency but still needs an explicit falsifier."
    if scenario == "validation":
        revised["falsifiable"] = False
    else:
        revised["falsifiable"] = True
    return revised


def _choose_fallback(*, profile: dict[str, Any], forced_topic_id: str = "") -> dict[str, Any]:
    if forced_topic_id:
        forced = _topic_by_id(profile, forced_topic_id)
        if forced is not None:
            forced["axis_allowed"] = True
            forced["data_ready"] = True
            forced["fixed_budget"] = True
            forced["baseline_clear"] = True
            forced["falsifiable"] = True
            forced["degrade_ready"] = True
            return forced
    for raw in profile.get("candidate_pool", []) or []:
        if isinstance(raw, dict) and bool(raw.get("safe_fallback", False)):
            item = deepcopy(raw)
            item["axis_allowed"] = True
            return item
    raise ValueError("fallback_topic_missing")


def _invoke_worker(
    *,
    task_id: str,
    trace_id: str,
    checkpoint: dict[str, Any],
    role: str,
    mode: str,
    packet_ref: str,
    round_no: int,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    timeout_sec = max(30.0, float(os.getenv("AGN_RESEARCH_WORKER_TIMEOUT_SECONDS", "300") or "300"))
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "research_worker.py"),
        "--role",
        role,
        "--mode",
        mode,
        "--packet-ref",
        packet_ref,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=timeout_sec,
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        return_code = int(proc.returncode)
    except subprocess.TimeoutExpired as exc:
        stdout = str(exc.stdout or "").strip()
        stderr = f"timeout after {timeout_sec}s"
        return_code = 124
    checkpoint, message_ref = _record_message(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        actor=role,
        surface="cli",
        kind=mode,
        round_no=round_no,
        content=stdout or stderr or "{}",
        packet_chars=len(stdout or stderr or "{}"),
        in_reply_to=packet_ref,
    )
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        if mode == "run_experiment":
            payload = {
                "role": role,
                "mode": mode,
                "status": "failure_note",
                "strategy": "failure_note",
                "error": "worker output was not valid JSON",
                "completed_work": ["worker wake-up attempted"],
                "notes": [stdout[:240]],
            }
        elif mode == "role_init":
            payload = {
                "role": role,
                "mode": mode,
                "ack": "init_failed",
                "current_round": round_no,
                "schema": "",
                "message": stdout[:240],
            }
        else:
            payload = {
                "role": role,
                "mode": mode,
                "decision": "no",
                "problem": "worker output was not valid JSON",
                "risk": "the coordination loop cannot safely rely on this reply",
                "minimal_change": "re-run with JSON output",
                "message": stdout[:240],
            }
    if return_code != 0:
        if mode == "run_experiment":
            payload.setdefault("status", "failure_note")
            payload.setdefault("strategy", "failure_note")
            payload.setdefault("error", "worker returned non-zero exit")
            payload.setdefault("notes", [stderr[:240]])
        elif mode == "role_init":
            payload.setdefault("ack", "init_failed")
            payload.setdefault("current_round", round_no)
            payload.setdefault("schema", "")
            payload.setdefault("message", stderr[:240])
        else:
            payload.setdefault("decision", "no")
            payload.setdefault("problem", "worker returned non-zero exit")
            payload.setdefault("risk", "the role output may be incomplete")
            payload.setdefault("minimal_change", "fix the worker invocation and re-run")
            payload.setdefault("message", stderr[:240])
    return checkpoint, payload, message_ref


def _dispatch_role(
    *,
    task_id: str,
    trace_id: str,
    checkpoint: dict[str, Any],
    role: str,
    mode: str,
    round_no: int,
    packet: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    init_packet = _role_init_packet(role=role, round_no=round_no, mode=mode, task_id=task_id)
    checkpoint, init_packet_ref = _record_json_message(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        actor="coordinator",
        surface="openclaw",
        kind="role_init_packet",
        round_no=round_no,
        payload=init_packet,
    )
    checkpoint, init_ack, init_ack_ref = _invoke_worker(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        role=role,
        mode="role_init",
        packet_ref=init_packet_ref,
        round_no=round_no,
    )
    if not _role_init_ack_valid(init_ack, role):
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="PROTOCOL_VIOLATION",
            payload={
                "reason": "invalid_role_init_ack",
                "role": role,
                "mode": mode,
                "round": round_no,
                "init_ack_ref": init_ack_ref,
            },
            severity="error",
        )
        detail = _trim(json.dumps(init_ack, ensure_ascii=False), 240) or "role init acknowledgement invalid"
        if mode == "run_experiment":
            payload = {
                "role": role,
                "mode": mode,
                "status": "failure_note",
                "strategy": "failure_note",
                "error": "role init acknowledgement invalid",
                "completed_work": ["role init attempted"],
                "notes": [detail],
            }
        elif mode == "publish_research":
            payload = {
                "role": role,
                "mode": mode,
                "status": "retry",
                "push_status": "blocked",
                "error": "role init acknowledgement invalid",
                "published_files": [],
                "commit_hash": "",
            }
        elif mode == "final_review":
            payload = {
                "role": role,
                "mode": mode,
                "verdict": "FAILURE_ARCHIVE",
                "failure_type": "role_init_invalid",
                "reason": "reviewer role init acknowledgement did not match the required protocol contract",
                "rerun_worth_it": True,
                "evidence_boundary": [],
                "issue": "reviewer role init acknowledgement invalid",
                "risk": "the review could proceed without a verified protocol refresh",
                "minimal_fix": "reload reviewer role init and retry the final review",
            }
        else:
            payload = {
                "role": role,
                "mode": mode,
                "decision": "no",
                "problem": "role init acknowledgement invalid",
                "risk": "the worker may not have refreshed the required protocol before work",
                "minimal_change": "reload role init and retry the packet",
                "message": detail,
            }
        checkpoint, message_ref = _record_json_message(
            task_id=task_id,
            trace_id=trace_id,
            checkpoint=checkpoint,
            actor=role,
            surface="cli",
            kind=mode,
            round_no=round_no,
            payload=payload,
        )
        return checkpoint, {
            "payload": payload,
            "message_ref": message_ref,
            "packet_ref": "",
            "init_packet_ref": init_packet_ref,
            "init_ack_ref": init_ack_ref,
            "init_ack": init_ack,
        }
    checkpoint, task_packet_ref = _record_json_message(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        actor="coordinator",
        surface="openclaw",
        kind=f"{mode}_packet",
        round_no=round_no,
        payload=packet,
    )
    checkpoint, payload, message_ref = _invoke_worker(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        role=role,
        mode=mode,
        packet_ref=task_packet_ref,
        round_no=round_no,
    )
    return checkpoint, {
        "payload": payload,
        "message_ref": message_ref,
        "packet_ref": task_packet_ref,
        "init_packet_ref": init_packet_ref,
        "init_ack_ref": init_ack_ref,
        "init_ack": init_ack,
    }


def _survey_stage(*, task_id: str, trace_id: str, checkpoint: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    heartbeat_tick(trace_id=trace_id, task_id=task_id, note="research survey")
    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="PLANNED", reason="research survey started")
    task = _task(trace_id, task_id)
    trigger_mode = _trigger_mode(task)
    research_mode = str(task.get("research_mode", "")).strip().lower() or "autonomy"
    coordinator_preflight_ref = str(checkpoint.get("coordinator_preflight_ref", "")).strip()
    if not _valid_task_ref(coordinator_preflight_ref, task_id=task_id):
        coordinator_preflight_ref = _write_coordinator_preflight(task_id=task_id, task=task, trigger_mode=trigger_mode)
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="RESEARCH_COORDINATOR_PREFLIGHT_WRITTEN",
            payload={"coordinator_preflight_ref": coordinator_preflight_ref, "trigger_mode": trigger_mode},
        )
        checkpoint = _merge_checkpoint(
            task_id,
            checkpoint,
            coordinator_preflight_ref=coordinator_preflight_ref,
            last_event_time=utc_now_iso(),
            recent_event_label="RESEARCH_COORDINATOR_PREFLIGHT_WRITTEN",
        )
        task["coordinator_preflight_ref"] = coordinator_preflight_ref
        _save_task(task)
    if trigger_mode == "manual":
        if _manual_input_missing(task):
            checkpoint = _admin_wait_checkpoint(
                task_id=task_id,
                checkpoint=checkpoint,
                phase="manual_intake",
                reason="manual_intake_missing",
                hold_until="",
                event_label="awaiting_manual_intake",
                recent_event="RESEARCH_ADMIN_INPUT_REQUIRED",
            )
            return _notify_once(
                task_id=task_id,
                trace_id=trace_id,
                checkpoint=checkpoint,
                key="manual_intake_required",
                text=(
                    f"[AGN research] manual input required\n"
                    f"task_id={task_id}\n"
                    "Reply with:\n"
                    "Research Question: ...\n"
                    "Hypothesis: ..."
                ),
                message_kind="alert",
            )
        candidate = _manual_seed_candidate(task, profile)
        manual_input = {
            "generated_at": utc_now_iso(),
            "mode": "manual_intake",
            "question": str(task.get("question", "")).strip(),
            "hypothesis": str(task.get("hypothesis", "")).strip(),
            "research_axis": str(candidate.get("axis", "")).strip(),
            "baseline": str(candidate.get("baseline", "")).strip(),
            "single_change": str(candidate.get("single_change", "")).strip(),
            "candidate": candidate,
        }
        intake_ref = write_json_artifact(
            task_id=task_id,
            attempt=ATTEMPT,
            artifact_id="manual_research_intake",
            payload=manual_input,
            filename="manual_research_intake.json",
            source="research_flow",
        ).ref
        survey_ref = intake_ref
        governance_lock_ref = _write_governance_lock(
            task_id=task_id,
            trigger_mode=trigger_mode,
            question=str(task.get("question", "")).strip(),
            hypothesis=str(task.get("hypothesis", "")).strip(),
        )
        research_plan_ref = _write_research_plan(task_id=task_id, task=task, candidate=candidate)
        shortlist = {
            "created_at": utc_now_iso(),
            "shortlist": [str(candidate.get("topic_id", "")).strip()],
            "top_titles": [
                {
                    "topic_id": str(candidate.get("topic_id", "")).strip(),
                    "title": str(candidate.get("title", "")).strip(),
                    "score": float(candidate.get("learning_value", 0.0) or 0.0),
                }
            ],
        }
        shortlist_ref = write_json_artifact(
            task_id=task_id,
            attempt=ATTEMPT,
            artifact_id="manual_research_shortlist",
            payload=shortlist,
            filename="manual_research_shortlist.json",
            source="research_flow",
        ).ref
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="RESEARCH_MANUAL_INTAKE_CREATED",
            payload={"survey_ref": survey_ref, "intake_ref": intake_ref, "candidate_id": str(candidate.get("topic_id", "")).strip()},
        )
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="RESEARCH_GOVERNANCE_LOCKED",
            payload={"governance_lock_ref": governance_lock_ref, "trigger_mode": trigger_mode},
        )
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="RESEARCH_PLAN_WRITTEN",
            payload={"research_plan_ref": research_plan_ref},
        )
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="RESEARCH_SHORTLIST_CREATED",
            payload={"shortlist_ref": shortlist_ref, "shortlist_ids": shortlist.get("shortlist", [])},
        )
        checkpoint = _merge_checkpoint(
            task_id,
            checkpoint,
            research_phase="design",
            proposal_state="governance_locked",
            research_status="governance_locked",
            protocol_blocked=False,
            protocol_block_reason="",
            governance_missing=[],
            completion_ready=False,
            intake_ref=intake_ref,
            governance_lock_ref=governance_lock_ref,
            research_plan_ref=research_plan_ref,
            survey_ref=survey_ref,
            shortlist_ref=shortlist_ref,
            round=0,
            proposal_version=0,
            shortlist_ids=[str(candidate.get("topic_id", "")).strip()],
            current_candidate=candidate,
            awaiting_admin_response=False,
            admin_hold_reason="",
            admin_hold_until="",
            last_event_time=utc_now_iso(),
            recent_event_label="RESEARCH_GOVERNANCE_LOCKED",
        )
        return checkpoint

    survey = _build_survey(profile)
    survey_ref = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="research_survey",
        payload=survey,
        filename="research_survey.json",
        source="research_flow",
    ).ref
    shortlist = {
        "created_at": utc_now_iso(),
        "shortlist": survey.get("shortlist_ids", []),
        "top_titles": [
            {
                "topic_id": str(item.get("topic_id", "")).strip(),
                "title": str(item.get("title", "")).strip(),
                "score": float(item.get("score", 0.0) or 0.0),
            }
            for item in (survey.get("candidates", []) or [])[:4]
            if isinstance(item, dict)
        ],
    }
    shortlist_ref = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="research_shortlist",
        payload=shortlist,
        filename="research_shortlist.json",
        source="research_flow",
    ).ref
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="RESEARCH_SURVEY_CREATED",
        payload={"survey_ref": survey_ref, "candidate_count": len(survey.get("candidates", []))},
    )
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="RESEARCH_SHORTLIST_CREATED",
        payload={"shortlist_ref": shortlist_ref, "shortlist_ids": shortlist.get("shortlist", [])},
    )
    daily_brief_ref = str(checkpoint.get("daily_brief_ref", "")).strip()
    daily_brief_deadline = str(checkpoint.get("daily_brief_deadline", "")).strip() or str(task.get("awaiting_admin_until", "")).strip()
    if not daily_brief_ref:
        daily_brief_payload = {
            "task_id": task_id,
            "generated_at": utc_now_iso(),
            "shortlist_ref": shortlist_ref,
            "top_titles": shortlist.get("top_titles", []),
        }
        daily_brief_ref = write_json_artifact(
            task_id=task_id,
            attempt=ATTEMPT,
            artifact_id="daily_brief_placeholder",
            payload=daily_brief_payload,
            filename="daily_brief_placeholder.json",
            source="research_flow",
        ).ref
    if not daily_brief_deadline:
        daily_brief_deadline = utc_now_iso()
    checkpoint = _merge_checkpoint(
        task_id,
        checkpoint,
        research_phase="brief_wait" if str(task.get("awaiting_admin_until", "")).strip() else "selection_vote",
        proposal_state="proposal_created",
        research_status="proposal_created",
        protocol_blocked=False,
        protocol_block_reason="",
        governance_missing=[],
        completion_ready=False,
        survey_ref=survey_ref,
        shortlist_ref=shortlist_ref,
        daily_brief_ref=daily_brief_ref,
        daily_brief_deadline=daily_brief_deadline,
        round=0,
        proposal_version=0,
        shortlist_ids=shortlist.get("shortlist", []),
        awaiting_admin_response=bool(str(task.get("awaiting_admin_until", "")).strip()),
        admin_hold_reason="",
        admin_hold_until=daily_brief_deadline if str(task.get("awaiting_admin_until", "")).strip() else "",
        last_event_time=utc_now_iso(),
        recent_event_label="RESEARCH_SHORTLIST_CREATED",
    )
    return checkpoint


def _discussion_stage(*, task_id: str, trace_id: str, checkpoint: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    if bool(checkpoint.get("paused", False)):
        return checkpoint

    task = _task(trace_id, task_id)
    research_mode = str(task.get("research_mode", "")).strip().lower() or "autonomy"
    trigger_mode = _trigger_mode(task)
    if trigger_mode == "manual" and _manual_input_missing(task):
        checkpoint = _admin_wait_checkpoint(
            task_id=task_id,
            checkpoint=checkpoint,
            phase="manual_intake",
            reason="manual_intake_missing",
            hold_until="",
            event_label="awaiting_manual_intake",
            recent_event="RESEARCH_ADMIN_INPUT_REQUIRED",
        )
        return _notify_once(
            task_id=task_id,
            trace_id=trace_id,
            checkpoint=checkpoint,
            key="manual_intake_required",
            text=(
                f"[AGN research] manual input required\n"
                f"task_id={task_id}\n"
                "Reply with:\n"
                "Research Question: ...\n"
                "Hypothesis: ..."
            ),
            message_kind="alert",
        )
    wait_active, hold_until = _discussion_wait_active(task=task, checkpoint=checkpoint)
    if wait_active:
        return _admin_wait_checkpoint(
            task_id=task_id,
            checkpoint=checkpoint,
            phase="brief_wait",
            reason="brief_reply_window_open",
            hold_until=hold_until,
            event_label="awaiting_admin_response",
            recent_event="RESEARCH_ADMIN_WAIT",
        )
    if bool(checkpoint.get("awaiting_admin_response", False)):
        checkpoint = _merge_checkpoint(
            task_id,
            checkpoint,
            awaiting_admin_response=False,
            admin_hold_reason="",
            admin_hold_until="",
            research_status=str(checkpoint.get("proposal_state", "proposal_created")).strip() or "proposal_created",
        )

    survey_ref = str(checkpoint.get("survey_ref", "")).strip()
    if not survey_ref.startswith("agn://"):
        raise ValueError("survey_ref_missing")

    round_no = int(checkpoint.get("round", 0) or 0) + 1
    forced_fallback = str(checkpoint.get("forced_fallback_topic_id", "")).strip()
    survey_payload = json.loads(read_ref_text(survey_ref, mode="all", max_bytes=512 * 1024))
    scenario = str(checkpoint.get("scenario", "daily")).strip()
    candidate = None
    if round_no == 1:
        if trigger_mode == "manual":
            candidate = _manual_seed_candidate(task, profile)
        elif scenario == "validation":
            candidate = _topic_by_id(profile, "transformer_sep_external") or _choose_shortlist_candidate(survey_payload, 0)
        else:
            candidate = _choose_shortlist_candidate(survey_payload, 0)
    elif round_no == 2:
        prior = checkpoint.get("current_candidate")
        base = prior if isinstance(prior, dict) and prior else _choose_shortlist_candidate(survey_payload, 0)
        if trigger_mode == "manual":
            candidate = _revise_manual_candidate(task=task, prior=base, round_no=round_no)
        else:
            candidate = _revise_candidate(candidate=base, scenario=scenario)
    else:
        candidate = _choose_fallback(profile=profile, forced_topic_id=forced_fallback)

    if bool(checkpoint.get("force_reorganize", False)) and round_no < 3:
        candidate = _choose_fallback(profile=profile, forced_topic_id=forced_fallback) if trigger_mode != "manual" else _revise_manual_candidate(task=task, prior=candidate, round_no=max(2, round_no))
        checkpoint = _merge_checkpoint(task_id, checkpoint, force_reorganize=False)

    if trigger_mode == "auto" and round_no == 1:
        checkpoint = _merge_checkpoint(
            task_id,
            checkpoint,
            selection_decision_ref=_write_selection_decision(
                task_id=task_id,
                candidate=candidate,
                round_no=round_no,
                trigger_mode=trigger_mode,
                reason="auto_selection_for_design",
            ),
        )
    proposal_state = "proposal_created" if round_no == 1 else "proposal_revised_round2"
    _sync_task_contract(
        task_id=task_id,
        trace_id=trace_id,
        candidate=candidate,
        round_no=round_no,
        proposal_version=round_no,
    )
    checkpoint = _merge_checkpoint(
        task_id,
        checkpoint,
        round=round_no,
        proposal_version=round_no,
        proposal_state=proposal_state,
        research_status=proposal_state,
        current_candidate=candidate,
        awaiting_admin_response=False,
        research_phase="design",
        recent_event_label=f"proposal_round_{round_no}_created",
    )
    proposal_packet = _proposal_packet(task=task, candidate=candidate, checkpoint=checkpoint, round_no=round_no)
    checkpoint, proposal_ref = _record_json_message(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        actor="coordinator",
        surface="openclaw",
        kind="proposal_packet",
        round_no=round_no,
        payload=proposal_packet,
    )
    proposal_refs = list(checkpoint.get("proposal_refs", []) or [])
    proposal_refs.append(proposal_ref)
    checkpoint = _merge_checkpoint(task_id, checkpoint, proposal_refs=proposal_refs[-8:])
    executor_packet = _executor_vote_packet(
        task=task,
        candidate=candidate,
        checkpoint=checkpoint,
        round_no=round_no,
    )
    reviewer_packet = _reviewer_vote_packet(
        task=task,
        candidate=candidate,
        checkpoint=checkpoint,
        round_no=round_no,
    )

    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="DISPATCHED_EXEC", reason=f"round {round_no} executor vote dispatched")
    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="EXEC_RUNNING", reason=f"round {round_no} executor vote running")
    checkpoint, executor_dispatch = _dispatch_role(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        role="executor",
        mode="topic_vote",
        round_no=round_no,
        packet=executor_packet,
    )
    executor_vote = executor_dispatch["payload"]
    executor_ref = str(executor_dispatch["message_ref"]).strip()
    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="EXEC_DONE", reason=f"round {round_no} executor vote captured")

    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="DISPATCHED_REVIEW", reason=f"round {round_no} reviewer vote dispatched")
    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="REVIEW_RUNNING", reason=f"round {round_no} reviewer vote running")
    checkpoint, reviewer_dispatch = _dispatch_role(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        role="reviewer",
        mode="topic_vote",
        round_no=round_no,
        packet=reviewer_packet,
    )
    reviewer_vote = reviewer_dispatch["payload"]
    reviewer_ref = str(reviewer_dispatch["message_ref"]).strip()
    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="REVIEW_DONE", reason=f"round {round_no} reviewer vote captured")

    issue_history = list(checkpoint.get("issue_history", []) or [])
    round_record = {
        "round": round_no,
        "candidate_id": str(candidate.get("topic_id", "")).strip(),
        "proposal_ref": proposal_ref,
        "executor_packet_ref": str(executor_dispatch["packet_ref"]).strip(),
        "reviewer_packet_ref": str(reviewer_dispatch["packet_ref"]).strip(),
        "executor_role_init_ref": str(executor_dispatch["init_packet_ref"]).strip(),
        "reviewer_role_init_ref": str(reviewer_dispatch["init_packet_ref"]).strip(),
        "executor_init_ack_ref": str(executor_dispatch["init_ack_ref"]).strip(),
        "reviewer_init_ack_ref": str(reviewer_dispatch["init_ack_ref"]).strip(),
        "executor_ref": executor_ref,
        "reviewer_ref": reviewer_ref,
        "executor_decision": str(executor_vote.get("decision", "")).strip(),
        "reviewer_decision": str(reviewer_vote.get("decision", "")).strip(),
    }
    if str(executor_vote.get("decision", "")).strip() == "no":
        round_record["executor_issue"] = {
            "problem": str(executor_vote.get("problem", "")).strip(),
            "risk": str(executor_vote.get("risk", "")).strip(),
            "minimal_change": str(executor_vote.get("minimal_change", "")).strip(),
        }
    if str(reviewer_vote.get("decision", "")).strip() == "no":
        round_record["reviewer_issue"] = {
            "problem": str(reviewer_vote.get("problem", "")).strip(),
            "risk": str(reviewer_vote.get("risk", "")).strip(),
            "minimal_change": str(reviewer_vote.get("minimal_change", "")).strip(),
        }
    issue_history.append(round_record)
    round_state_ref = _write_round_state(task_id=task_id, round_record=round_record)

    accepted = (
        str(executor_vote.get("decision", "")).strip() == "yes"
        and str(reviewer_vote.get("decision", "")).strip() == "yes"
        and round_no < 3
    )

    if accepted:
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="RESEARCH_ROUND_APPROVED",
            payload={"round": round_no, "candidate_id": str(candidate.get("topic_id", "")).strip()},
        )
        _ensure_state(trace_id=trace_id, task_id=task_id, to_state="PLANNED", reason="research topic accepted and queued for execution")
        checkpoint = _merge_checkpoint(
            task_id,
            checkpoint,
            research_phase="execution",
            round=round_no,
            proposal_version=round_no,
            current_candidate=candidate,
            selected_topic_id=str(candidate.get("topic_id", "")).strip(),
            selection_decision_ref=str(checkpoint.get("selection_decision_ref", "")).strip()
            or _write_selection_decision(
                task_id=task_id,
                candidate=candidate,
                round_no=round_no,
                trigger_mode=trigger_mode,
                reason="proposal_accepted_for_execution",
            ),
            round_state_ref=round_state_ref,
            issue_history=issue_history,
            rejected=False,
            protocol_blocked=False,
            protocol_block_reason="",
            governance_missing=[],
            last_event_time=utc_now_iso(),
            recent_event_label="RESEARCH_ROUND_APPROVED",
        )
        if trigger_mode == "auto":
            checkpoint = _notify_once(
                task_id=task_id,
                trace_id=trace_id,
                checkpoint=checkpoint,
                key="workflow_started",
                text=(
                    f"[AGN research] started\n"
                    f"task_id={task_id}\n"
                    f"trigger_mode=auto\n"
                    f"topic={str(candidate.get('topic_id', '')).strip()}"
                ),
                message_kind="alert",
            )
        return checkpoint

    if round_no < 2:
        rejection_state = "proposal_rejected_round1"
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="RESEARCH_ROUND_REJECTED",
            payload={"round": round_no, "candidate_id": str(candidate.get("topic_id", "")).strip(), "next_round": round_no + 1},
            severity="warn",
        )
        _ensure_state(trace_id=trace_id, task_id=task_id, to_state="PLANNED", reason="coordinator regroup for next round")
        checkpoint = _merge_checkpoint(
            task_id,
            checkpoint,
            research_phase="design",
            round=round_no,
            proposal_version=round_no,
            current_candidate=candidate,
            round_state_ref=round_state_ref,
            issue_history=issue_history,
            proposal_state=rejection_state,
            research_status=rejection_state,
            last_rejected_state=rejection_state,
            rejected=True,
            last_event_time=utc_now_iso(),
            recent_event_label="RESEARCH_ROUND_REJECTED",
        )
        return checkpoint

    forced_topic = forced_fallback
    if trigger_mode == "manual" and not forced_topic:
        forced = _revise_manual_candidate(task=task, prior=candidate, round_no=3)
    else:
        if scenario == "validation" and not forced_topic:
            forced_topic = "local_global_dependency"
        forced = _choose_fallback(profile=profile, forced_topic_id=forced_topic)
    rejection_state = "proposal_rejected_round2"
    forced_payload = {
        "mode": "forced_decision",
        "round": 3,
        "reason": "second proposal still rejected; coordinator exercises the third organization right",
        "selected_topic_id": str(forced.get("topic_id", "")).strip(),
        "retained_issue_count": len(issue_history),
        "executor_last_decision": str(executor_vote.get("decision", "")).strip(),
        "reviewer_last_decision": str(reviewer_vote.get("decision", "")).strip(),
    }
    checkpoint, forced_ref = _record_json_message(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        actor="coordinator",
        surface="openclaw",
        kind="forced_decision",
        round_no=3,
        payload=forced_payload,
    )
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="RESEARCH_FORCED_DECISION",
        payload={
            "round": 3,
            "selected_topic_id": str(forced.get("topic_id", "")).strip(),
            "decision_ref": forced_ref,
        },
        severity="warn",
    )
    _sync_task_contract(
        task_id=task_id,
        trace_id=trace_id,
        candidate=forced,
        round_no=3,
        proposal_version=3,
    )
    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="PLANNED", reason="research forced decision queued for execution")
    checkpoint = _merge_checkpoint(
        task_id,
        checkpoint,
        research_phase="execution",
        selection_decision_ref=_write_selection_decision(
            task_id=task_id,
            candidate=forced,
            round_no=3,
            trigger_mode=trigger_mode,
            reason="coordinator_forced_decision",
        ),
        round_state_ref=round_state_ref,
        round=3,
        anomaly=True,
        entered_third_round=True,
        current_candidate=forced,
        selected_topic_id=str(forced.get("topic_id", "")).strip(),
        issue_history=issue_history,
        forced_decision_ref=forced_ref,
        proposal_version=3,
        proposal_state="coordinator_final_decision_round3",
        research_status="coordinator_final_decision_round3",
        last_rejected_state=rejection_state,
        rejected=True,
        protocol_blocked=False,
        protocol_block_reason="",
        governance_missing=[],
        last_event_time=utc_now_iso(),
        recent_event_label="RESEARCH_FORCED_DECISION",
    )
    if trigger_mode == "auto":
        checkpoint = _notify_once(
            task_id=task_id,
            trace_id=trace_id,
            checkpoint=checkpoint,
            key="workflow_started",
            text=(
                f"[AGN research] started\n"
                f"task_id={task_id}\n"
                f"trigger_mode=auto\n"
                f"topic={str(forced.get('topic_id', '')).strip()}"
            ),
            message_kind="alert",
        )
    return checkpoint


def _synthetic_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for idx, lag in enumerate((3, 4, 5, 3, 4, 5), start=1):
        seq: list[float] = []
        for t in range(48):
            base = math.sin((t + idx) / 3.0) + math.cos((t + 2 * idx) / 5.0)
            if t >= lag:
                base += 0.72 * seq[t - lag]
            seq.append(round(base, 6))
        missing = {12 + idx, 13 + idx, 24 + idx}
        observed = [0.0 if pos in missing else value for pos, value in enumerate(seq)]
        cases.append({"target_lag": lag, "observed": observed})
    return cases


def _best_lag(signal: list[float], max_lag: int = 8) -> int:
    best = 1
    best_score = float("inf")
    for lag in range(1, max_lag + 1):
        score = 0.0
        count = 0
        for idx in range(lag, len(signal)):
            prev = signal[idx - lag]
            cur = signal[idx]
            if prev == 0.0 or cur == 0.0:
                continue
            score += abs(cur - prev)
            count += 1
        if count == 0:
            continue
        score /= count
        if score < best_score:
            best_score = score
            best = lag
    return best


def _local_global_lag(signal: list[float], max_lag: int = 8) -> int:
    votes: dict[int, int] = {}
    window = 12
    for start in range(0, len(signal) - window + 1, 6):
        segment = signal[start : start + window]
        lag = _best_lag(segment, max_lag=max_lag)
        votes[lag] = votes.get(lag, 0) + 1
    return sorted(votes.items(), key=lambda item: (item[1], -item[0]), reverse=True)[0][0]


def _experiment_result(*, candidate: dict[str, Any], strategy: str, force_fail: bool) -> dict[str, Any]:
    if force_fail:
        raise RuntimeError("validation_forced_experiment_failure")

    cases = _synthetic_cases()
    rows: list[dict[str, Any]] = []
    for case in cases:
        signal = list(case["observed"])
        target = int(case["target_lag"])
        baseline = _best_lag(signal)
        predicted = baseline if strategy == "baseline_only" else _local_global_lag(signal)
        rows.append(
            {
                "target_lag": target,
                "baseline_lag": baseline,
                "predicted_lag": predicted,
                "correct": predicted == target,
            }
        )

    accuracy = sum(1 for row in rows if row["correct"]) / max(1, len(rows))
    return {
        "topic_id": str(candidate.get("topic_id", "")).strip(),
        "title": str(candidate.get("title", "")).strip(),
        "strategy": strategy,
        "status": "degraded" if strategy != "full" else "ok",
        "cases": rows,
        "metrics": {
            "case_count": len(rows),
            "accuracy": round(accuracy, 4),
        },
        "notes": [
            "The synthetic signal uses lag-structured dependencies with contiguous missing regions.",
            "The local-to-global vote is intentionally tiny so it stays self-hostable in a fixed nightly budget.",
        ],
    }


def _experiment_stage(*, task_id: str, trace_id: str, checkpoint: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    task = _task(trace_id, task_id)
    checkpoint, allowed = _require_governance(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        task=task,
        phase="execution",
        repair_phase="design",
        state="PLANNED",
    )
    if not allowed:
        return checkpoint

    candidate = checkpoint.get("current_candidate")
    if not isinstance(candidate, dict):
        raise ValueError("current_candidate_missing")

    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="RESEARCH_EXPERIMENT_STARTED",
        payload={"topic_id": str(candidate.get("topic_id", "")).strip()},
    )
    checkpoint = _merge_checkpoint(
        task_id,
        checkpoint,
        research_status="execution_started",
        last_event_time=utc_now_iso(),
    )
    packet = _executor_experiment_packet(
        task=task,
        candidate=candidate,
        checkpoint=checkpoint,
        profile=profile,
    )
    round_no = max(1, int(checkpoint.get("round", 1) or 1))
    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="DISPATCHED_EXEC", reason="research experiment dispatched")
    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="EXEC_RUNNING", reason="research experiment running")
    checkpoint, executor_dispatch = _dispatch_role(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        role="executor",
        mode="run_experiment",
        round_no=round_no,
        packet=packet,
    )
    result = executor_dispatch["payload"]
    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="EXEC_DONE", reason="research experiment captured")

    if not isinstance(result, dict):
        result = {}
    empirical_execution, truthfulness_status, truthfulness_reason = _experiment_truthfulness(result)
    if not empirical_execution:
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="PROTOCOL_VIOLATION",
            payload={
                "violation": "non_empirical_experiment_result",
                "truthfulness_status": truthfulness_status,
                "truthfulness_reason": truthfulness_reason,
                "worker_ref": str(executor_dispatch.get("message_ref", "")).strip(),
            },
            severity="error",
        )
        result = _block_non_empirical_result(result=result)
        empirical_execution = False
        truthfulness_status = str(result.get("truthfulness_status", "")).strip() or "non_empirical"
        truthfulness_reason = str(result.get("truthfulness_reason", "")).strip() or truthfulness_reason
    status_value = str(result.get("status", "")).strip()
    strategy = str(result.get("strategy", "")).strip()
    if not status_value:
        status_value = "failure_note"
    if not strategy:
        strategy = "failure_note"

    failed_strategies = result.get("failed_strategies", [])
    if not isinstance(failed_strategies, list):
        failed_strategies = []
    for item in failed_strategies:
        if not isinstance(item, dict):
            continue
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="RESEARCH_EXPERIMENT_FAILED",
            payload={
                "strategy": str(item.get("strategy", "")).strip(),
                "error": str(item.get("error", "")).strip(),
                "worker_ref": str(executor_dispatch.get("message_ref", "")).strip(),
            },
            severity="warn",
        )

    if status_value not in {"ok", "degraded", "failure_note"}:
        status_value = "failure_note"
        result = {
            "topic_id": str(candidate.get("topic_id", "")).strip(),
            "status": "failure_note",
            "strategy": "failure_note",
            "error": "executor returned invalid experiment payload",
            "completed_work": ["survey complete", "shortlist complete", "discussion complete"],
        }
        strategy = "failure_note"

    full_strategies = ["full", *[str(item).strip() for item in profile.get("degrade_chain", []) if str(item).strip()]]
    chosen_index = full_strategies.index(strategy) if strategy in full_strategies else len(full_strategies) - 1
    experiment_ref = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id=f"research_experiment_{strategy}",
        payload=result,
        filename=f"research_experiment_{strategy}.json",
        source="research_flow",
    ).ref
    experiment_log_ref = ""
    command_log_value = str(result.get("command_log_path", "")).strip() if isinstance(result, dict) else ""
    command_log_path = Path(command_log_value) if command_log_value else None
    if command_log_path is not None and command_log_path.exists():
        experiment_log_ref = write_text_artifact(
            task_id=task_id,
            attempt=ATTEMPT,
            artifact_id=f"research_experiment_{strategy}_log",
            content=command_log_path.read_text(encoding="utf-8", errors="replace")[:24000],
            media_type="text/plain",
            filename=f"research_experiment_{strategy}.log",
            source="research_flow",
        ).ref
    experiment_summary = {
        "raw_result_ref": str(executor_dispatch.get("message_ref", "")).strip(),
        "log_ref": experiment_log_ref,
        "metrics": result.get("metrics", {}) if isinstance(result, dict) else {},
        "unverified_metrics": result.get("unverified_metrics", {}) if isinstance(result.get("unverified_metrics"), dict) else {},
        "artifact_refs": [ref for ref in [experiment_ref, experiment_log_ref] if str(ref).startswith("agn://")],
        "observation": str((result.get("notes", [""]) or [""])[0]).strip() if isinstance(result, dict) else "",
        "execution_exception": bool(str(result.get("error", "")).strip()) if isinstance(result, dict) else True,
        "degraded": chosen_index > 0 or status_value != "ok",
        "empirical_execution": empirical_execution,
        "truthfulness_status": truthfulness_status,
        "truthfulness_reason": truthfulness_reason,
    }
    experiment_summary_ref = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="research_experiment_summary",
        payload=experiment_summary,
        filename="research_experiment_summary.json",
        source="research_flow",
    ).ref
    if strategy != "full":
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="RESEARCH_DEGRADE_APPLIED",
            payload={"strategy": strategy, "experiment_ref": experiment_ref},
            severity="warn",
        )
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="RESEARCH_EXPERIMENT_COMPLETED",
        payload={"strategy": strategy, "experiment_ref": experiment_ref},
    )
    checkpoint = _merge_checkpoint(
        task_id,
        checkpoint,
        research_phase="writing",
        experiment_ref=experiment_ref,
        experiment_summary_ref=experiment_summary_ref,
        experiment_log_ref=experiment_log_ref,
        experiment_raw_ref=str(executor_dispatch.get("message_ref", "")).strip(),
        experiment_result=result,
        experiment_failure_seen=bool(result.get("full_failure_observed", False)) or bool(failed_strategies),
        empirical_execution=empirical_execution,
        truthfulness_status=truthfulness_status,
        truthfulness_reason=truthfulness_reason,
        degrade_index=max(0, chosen_index),
        degraded=chosen_index > 0 or status_value != "ok",
        research_status="execution_degraded" if (chosen_index > 0 or status_value != "ok") else "execution_started",
        force_degrade=False,
        last_event_time=utc_now_iso(),
        recent_event_label="RESEARCH_EXPERIMENT_COMPLETED",
    )
    return checkpoint


def _paper_stage(*, task_id: str, trace_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
    task = _task(trace_id, task_id)
    checkpoint, allowed = _require_governance(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        task=task,
        phase="writing",
        repair_phase="experiment",
        state="EXEC_DONE",
    )
    if not allowed:
        return checkpoint

    candidate = checkpoint.get("current_candidate")
    result = checkpoint.get("experiment_result")
    if not isinstance(candidate, dict) or not isinstance(result, dict):
        raise ValueError("paper_stage_missing_inputs")

    empirical_execution = bool(checkpoint.get("empirical_execution", result.get("empirical_execution", False)))
    truthfulness_reason = str(checkpoint.get("truthfulness_reason", result.get("truthfulness_reason", ""))).strip()
    outcome_kind = "failure_note" if (str(result.get("status", "")).strip() == "failure_note" or not empirical_execution) else "mini_paper"
    metrics = result.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
    unverified_metrics = result.get("unverified_metrics", {})
    if not isinstance(unverified_metrics, dict):
        unverified_metrics = {}

    task_question = _prefer(task.get("question", ""), candidate.get("problem", ""))
    baseline = _prefer(task.get("baseline", ""), candidate.get("baseline", ""))
    single_change = _prefer(task.get("single_change", ""), candidate.get("single_change", ""))
    method_family = str(candidate.get("required_method_family", candidate.get("method_family", ""))).strip()
    method_lines = [
        f"Keep the research question fixed: {task_question}.",
        f"Use `{baseline or 'the stated baseline'}` as the comparison surface and change only `{single_change or 'one constrained modeling surface'}`.",
        f"Stay within the required method family `{method_family or 'generic_learning_model'}` and the fixed same-day budget.",
    ]
    if metrics:
        metric_labels = ", ".join(sorted(str(key).strip() for key in metrics.keys() if str(key).strip()))
        if metric_labels:
            method_lines.append(f"Measure the outcome with the experiment metrics returned by the executor: {metric_labels}.")
    notes = result.get("notes", [])
    if not isinstance(notes, list):
        notes = []
    if notes:
        method_lines.append(f"Observed execution note: {str(notes[0]).strip()}")
    method_lines.append(
        "Treat degradation as part of the protocol: if the constrained experiment fails, preserve the baseline, the bounded evidence, and the reason for stopping instead of fabricating completion."
    )
    if outcome_kind == "failure_note":
        method_lines.append("The final output preserves the failed step, the retained evidence, and the reason a mechanical rerun is or is not justified.")

    setup_lines = [
        f"- topic_id: `{str(candidate.get('topic_id', '')).strip()}`",
        f"- strategy: `{str(result.get('strategy', str(result.get('status', 'unknown')))).strip()}`",
        f"- method_family: `{method_family or 'generic_learning_model'}`",
        f"- anomaly: `{bool(checkpoint.get('anomaly', False))}`",
    ]
    if "cases_completed" in metrics:
        setup_lines.append(f"- cases_completed: `{metrics.get('cases_completed')}`")
    elif "case_count" in metrics:
        setup_lines.append(f"- case_count: `{metrics.get('case_count')}`")
    if "noise_levels" in metrics:
        setup_lines.append(f"- noise_levels: `{metrics.get('noise_levels')}`")
    if "runtime_sec" in metrics:
        setup_lines.append(f"- runtime_sec: `{metrics.get('runtime_sec')}`")

    result_lines = [f"- status: `{str(result.get('status', 'unknown')).strip()}`"]
    preferred_metric_order = [
        "avg_balanced_accuracy",
        "avg_baseline_accuracy",
        "accuracy",
        "baseline_accuracy",
        "runtime_sec",
    ]
    seen_metric_keys: set[str] = set()
    for key in preferred_metric_order:
        if key in metrics:
            result_lines.append(f"- {key}: `{metrics.get(key)}`")
            seen_metric_keys.add(key)
    for key in sorted(metrics.keys()):
        if key in seen_metric_keys:
            continue
        result_lines.append(f"- {key}: `{metrics.get(key)}`")
    if notes:
        for note in notes[:3]:
            clean = str(note).strip()
            if clean:
                result_lines.append(f"- note: {clean}")
    if unverified_metrics:
        result_lines.append("- unverified_metrics: preserved in `raw_results.json` for audit only; they are not treated as empirical experiment output.")

    if "avg_balanced_accuracy" in metrics and "avg_baseline_accuracy" in metrics:
        interpretation = (
            f"The tested method reached balanced accuracy {metrics.get('avg_balanced_accuracy')} "
            f"versus baseline {metrics.get('avg_baseline_accuracy')}, so the report should focus on whether the gain remains stable under the bounded noise sweep."
        )
    elif notes:
        interpretation = str(notes[0]).strip()
    else:
        interpretation = "Interpret the result only from the bounded experiment evidence preserved in this unit."

    if outcome_kind == "mini_paper":
        _ensure_state(trace_id=trace_id, task_id=task_id, to_state="SYNTHESIS", reason="research synthesis written for final review")
        # Try LLM-powered essay writing first.
        body = ""
        try:
            from research_llm import write_research_essay
            body = write_research_essay(proposal=candidate, result=result, task=task)
        except Exception:
            pass
        if body.strip():
            # Append structured results section so raw metrics are always auditable.
            body += "\n\n## Experimental Setup\n" + "\n".join(setup_lines)
            body += "\n\n## Result\n" + "\n".join(result_lines)
            body += "\n\n## Interpretation\n" + interpretation
        if not body.strip():
            # Fallback to template-based essay.
            body = "\n".join(
                [
                    "# Mini Paper",
                    "",
                    "## Problem",
                    str(candidate.get("problem", "")).strip(),
                    "",
                    "## Core Idea",
                    str(candidate.get("core_idea", "")).strip(),
                    "",
                    "## Method",
                    "\n".join([f"- {line}" for line in method_lines]),
                    "",
                    "## Experimental Setup",
                    "\n".join(setup_lines),
                    "",
                    "## Result",
                    "\n".join(result_lines),
                    "",
                    "## Interpretation",
                    interpretation,
                ]
            )
    else:
        rerun_worth_it = (not empirical_execution) or (bool(str(result.get("error", "")).strip()) and not bool(checkpoint.get("degraded", False)))
        failure_type = "non_empirical_execution" if not empirical_execution else ("execution_failure" if str(result.get("error", "")).strip() else "review_failure_archive")
        _ensure_state(trace_id=trace_id, task_id=task_id, to_state="SYNTHESIS", reason="failure synthesis written for final review")
        body = "\n".join(
            [
                "# Failure Note",
                "",
                "## Problem",
                str(candidate.get("problem", "")).strip(),
                "",
                "## Failure Cause",
                truthfulness_reason or str(result.get("error", "")).strip() or "The constrained run degraded to a failure-oriented unit.",
                "",
                "## failure_type",
                failure_type,
                "",
                "## rerun_worth_it",
                str(rerun_worth_it).lower(),
                "",
                "## If Not Rerun, Why",
                "The current evidence already shows the same bounded setup would repeat the same outcome." if not rerun_worth_it else "A rerun may still clarify whether the failure was transport- or packet-specific.",
                "",
                "## Next Step",
                "Narrow the scope again, keep the same baseline, and only change one modeling surface.",
                "",
                "## Method",
                "\n".join([f"- {line}" for line in method_lines]),
                "",
                "## Experimental Setup",
                "\n".join(setup_lines),
                "",
                "## Result",
                "\n".join(result_lines),
            ]
        )

    artifact = write_text_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id=outcome_kind,
        content=body,
        media_type="text/markdown",
        filename=f"{outcome_kind}.md",
        source="research_flow",
    )
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="RESEARCH_PAPER_WRITTEN",
        payload={"outcome_kind": outcome_kind, "artifact_ref": artifact.ref},
    )
    updates = {
        "research_phase": "review",
        "outcome_kind": outcome_kind,
        "research_status": "paper_ready",
        "essay_ref": artifact.ref,
        "last_event_time": utc_now_iso(),
        "recent_event_label": "RESEARCH_PAPER_WRITTEN",
    }
    if outcome_kind == "mini_paper":
        updates["paper_ref"] = artifact.ref
    else:
        updates["failure_note_ref"] = artifact.ref
    checkpoint = _merge_checkpoint(task_id, checkpoint, **updates)
    return checkpoint


def _review_stage(*, task_id: str, trace_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
    task = _task(trace_id, task_id)
    checkpoint, allowed = _require_governance(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        task=task,
        phase="review",
        repair_phase="writing",
        state="SYNTHESIS",
    )
    if not allowed:
        return checkpoint

    round_no = max(1, int(checkpoint.get("round", 1) or 1))
    packet = _reviewer_final_review_packet(task_id=task_id, checkpoint=checkpoint)
    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="DISPATCHED_REVIEW", reason="research final review dispatched")
    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="REVIEW_RUNNING", reason="research final review running")
    checkpoint, reviewer_dispatch = _dispatch_role(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        role="reviewer",
        mode="final_review",
        round_no=round_no,
        packet=packet,
    )
    verdict = reviewer_dispatch["payload"]
    verdict_ref = str(reviewer_dispatch["message_ref"]).strip()
    verdict_value = str(verdict.get("verdict", verdict.get("decision", ""))).strip().upper()
    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="REVIEW_DONE", reason="research final review captured")
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="RESEARCH_FINAL_REVIEW",
        payload={"verdict": verdict_value, "verdict_ref": verdict_ref},
    )

    if verdict_value == "REVISION_ONCE" and int(checkpoint.get("review_revision_count", 0) or 0) < 1:
        issue = str(verdict.get("issue", verdict.get("problem", ""))).strip() or "review requested one coordinator-side clarification"
        risk = str(verdict.get("risk", "")).strip() or "the archived unit may remain ambiguous"
        minimal_fix = str(verdict.get("minimal_fix", verdict.get("minimal_change", ""))).strip() or "apply one small revision and re-review once"
        revision_ref = _write_revision_artifact(
            task_id=task_id,
            checkpoint=checkpoint,
            issue=issue,
            risk=risk,
            minimal_fix=minimal_fix,
        )
        updated_fields = {"review_revision_count": 1, "review_revision_ref": revision_ref}
        if str(checkpoint.get("outcome_kind", "")).strip() == "mini_paper":
            updated_fields["paper_ref"] = revision_ref
        else:
            updated_fields["failure_note_ref"] = revision_ref
        checkpoint = _merge_checkpoint(task_id, checkpoint, **updated_fields)
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="RESEARCH_REVIEW_REVISION_APPLIED",
            payload={"revision_ref": revision_ref, "issue": issue},
            severity="warn",
        )
        packet = _reviewer_final_review_packet(task_id=task_id, checkpoint=checkpoint)
        _ensure_state(trace_id=trace_id, task_id=task_id, to_state="DISPATCHED_REVIEW", reason="research final review redispatched after one revision")
        _ensure_state(trace_id=trace_id, task_id=task_id, to_state="REVIEW_RUNNING", reason="research final review rerunning after one revision")
        checkpoint, reviewer_dispatch = _dispatch_role(
            task_id=task_id,
            trace_id=trace_id,
            checkpoint=checkpoint,
            role="reviewer",
            mode="final_review",
            round_no=round_no,
            packet=packet,
        )
        verdict = reviewer_dispatch["payload"]
        verdict_ref = str(reviewer_dispatch["message_ref"]).strip()
        verdict_value = str(verdict.get("verdict", verdict.get("decision", ""))).strip().upper()
        _ensure_state(trace_id=trace_id, task_id=task_id, to_state="REVIEW_DONE", reason="research final review recaptured after one revision")
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="RESEARCH_FINAL_REVIEW",
            payload={"verdict": verdict_value, "verdict_ref": verdict_ref, "after_revision": True},
        )

    if verdict_value == "APPROVED":
        research_status = "review_passed"
    else:
        research_status = "review_failed_archived"
        if verdict_value not in {"FAILURE_ARCHIVE", "REVISION_ONCE"}:
            verdict_value = "FAILURE_ARCHIVE"
        if verdict_value == "REVISION_ONCE":
            verdict = {
                "verdict": "FAILURE_ARCHIVE",
                "failure_type": "review_revision_exhausted",
                "reason": "one revision was already used and the archive still did not pass review",
                "rerun_worth_it": False,
                "evidence_boundary": packet.get("evidence_boundary", []),
                "issue": str(verdict.get("issue", verdict.get("problem", ""))).strip(),
                "risk": str(verdict.get("risk", "")).strip(),
                "minimal_fix": str(verdict.get("minimal_fix", verdict.get("minimal_change", ""))).strip(),
            }
            verdict_value = "FAILURE_ARCHIVE"

    checkpoint = _merge_checkpoint(
        task_id,
        checkpoint,
        research_phase="archive",
        final_review=verdict,
        review_verdict_ref=verdict_ref,
        research_status=research_status,
        last_event_time=utc_now_iso(),
        recent_event_label="RESEARCH_FINAL_REVIEW",
    )
    return checkpoint


def _final_report_body(*, task_id: str, checkpoint: dict[str, Any]) -> str:
    candidate = checkpoint.get("current_candidate")
    if not isinstance(candidate, dict):
        candidate = {}
    final_review = checkpoint.get("final_review")
    if not isinstance(final_review, dict):
        final_review = {}
    result_ref = str(checkpoint.get("paper_ref", "")).strip() or str(checkpoint.get("failure_note_ref", "")).strip()
    experiment_result = checkpoint.get("experiment_result")
    if not isinstance(experiment_result, dict):
        experiment_result = {}
    dependency_installs = experiment_result.get("dependency_install_attempts", [])
    if not isinstance(dependency_installs, list):
        dependency_installs = []
    return "\n".join(
        [
            "# Final Report",
            "",
            f"- task_id: `{task_id}`",
            f"- title: {str(candidate.get('title', '')).strip() or str(candidate.get('problem', '')).strip() or 'n/a'}",
            f"- question: {str(candidate.get('problem', '')).strip() or 'n/a'}",
            f"- review_verdict: `{str(final_review.get('verdict', final_review.get('decision', 'n/a'))).strip() or 'n/a'}`",
            f"- outcome_kind: `{str(checkpoint.get('outcome_kind', '')).strip() or 'n/a'}`",
            f"- round: `{int(checkpoint.get('round', 0) or 0)}`",
            f"- round2_used: `{bool(int(checkpoint.get('round', 0) or 0) >= 2)}`",
            f"- round3_used: `{bool(checkpoint.get('entered_third_round', False))}`",
            f"- degraded: `{bool(checkpoint.get('degraded', False))}`",
            f"- archive_ref: `{str(checkpoint.get('archive_ref', '')).strip() or 'n/a'}`",
            f"- trace_index_ref: `{str(checkpoint.get('trace_index_ref', '')).strip() or 'n/a'}`",
            f"- result_ref: `{result_ref or 'n/a'}`",
            f"- empirical_execution: `{bool(checkpoint.get('empirical_execution', False))}`",
            f"- truthfulness_status: `{str(checkpoint.get('truthfulness_status', '')).strip() or 'n/a'}`",
            f"- truthfulness_reason: {str(checkpoint.get('truthfulness_reason', '')).strip() or 'n/a'}",
            f"- publish_status: `{str(checkpoint.get('publish_status', '')).strip() or 'n/a'}`",
            f"- commit_hash: `{str(checkpoint.get('commit_hash', '')).strip() or 'n/a'}`",
            f"- dependency_install_attempts: `{len(dependency_installs)}`",
        ]
    )


def _executor_publish_packet(*, task_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
    task = SSOTStore(ROOT / "ssot").get_task(task_id) or {}
    candidate = checkpoint.get("current_candidate")
    if not isinstance(candidate, dict):
        candidate = {}
    return {
        "step": "task",
        "packet_schema": _packet_schema("executor", "publish_research"),
        "task_id": task_id,
        "role": "executor",
        "goal": "Materialize research outputs into the repository and publish them.",
        "current_round": max(1, int(checkpoint.get("round", 1) or 1)),
        "current_action_required": "Write the referenced outputs to the research repo, and only if the outcome is an empirical mini paper, generate a Hugo Science post, validate the blog build, then commit and push the required repos.",
        "output_schema": {
            "status": "ok|retry",
            "push_status": "ok|failed|blocked",
            "error": "string",
            "published_files": "list[string]",
            "commit_hash": "string",
        },
        "title": str(candidate.get("title", "")).strip() or str(task.get("question", "")).strip(),
        "question": str(task.get("question", "")).strip(),
        "hypothesis": str(task.get("hypothesis", "")).strip(),
        "research_axis": str(task.get("research_axis", "")).strip(),
        "unit_date": str(task.get("unit_date", "")).strip(),
        "essay_ref": str(checkpoint.get("essay_ref", "")).strip(),
        "final_report_ref": str(checkpoint.get("final_report_ref", "")).strip(),
        "result_summary_ref": str(checkpoint.get("result_summary_ref", "")).strip(),
        "raw_results_ref": str(checkpoint.get("raw_results_ref", "")).strip(),
        "data_record_ref": str(checkpoint.get("data_record_ref", "")).strip(),
        "reproduce_ref": str(checkpoint.get("reproduce_ref", "")).strip(),
        "code_bundle_ref": str(checkpoint.get("code_bundle_ref", "")).strip(),
        "archive_ref": str(checkpoint.get("archive_ref", "")).strip(),
        "trace_index_ref": str(checkpoint.get("trace_index_ref", "")).strip(),
        "outcome_kind": str(checkpoint.get("outcome_kind", "")).strip(),
        "empirical_execution": bool(checkpoint.get("empirical_execution", False)),
        "repo_path": str(task.get("repo_path", "")).strip(),
        "work_branch": str(task.get("work_branch", "")).strip(),
        "blog_repo_path": str(task.get("blog_repo_path", "")).strip() or str(resolve_research_blog_repo_path() or "").strip(),
        "blog_work_branch": str(task.get("blog_work_branch", "")).strip() or str(resolve_research_blog_branch() or "main").strip(),
        "blog_science_dir": str(task.get("blog_science_dir", "")).strip() or str(resolve_research_blog_science_dir() or "content/AGNResearch").strip(),
        "output_dir": f"research_outputs/{str(task_id or 'research').replace('/', '_')}",
        "role_init_paths": _role_init_paths("executor"),
    }


def _publish_stage(*, task_id: str, trace_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
    task = _task(trace_id, task_id)
    task["side_effect_level"] = "external_publish"
    _save_task(task)
    checkpoint, allowed = _require_governance(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        task=task,
        phase="publish",
        repair_phase="archive",
        state="DELIVERY_GATE",
    )
    if not allowed:
        return checkpoint

    packet = _executor_publish_packet(task_id=task_id, checkpoint=checkpoint)
    round_no = max(1, int(checkpoint.get("round", 1) or 1))
    checkpoint, executor_dispatch = _dispatch_role(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        role="executor",
        mode="publish_research",
        round_no=round_no,
        packet=packet,
    )
    result = executor_dispatch["payload"] if isinstance(executor_dispatch["payload"], dict) else {}
    publish_receipt_ref = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="publish_receipt",
        payload=result,
        filename="publish_receipt.json",
        source="research_flow",
    ).ref
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="RESEARCH_PUBLISH_ATTEMPTED",
        payload={"publish_receipt_ref": publish_receipt_ref, "push_status": str(result.get("push_status", "")).strip()},
        severity="warn" if str(result.get("status", "")).strip() != "ok" else "info",
    )
    publish_status = str(result.get("status", "")).strip().lower()
    push_status = str(result.get("push_status", "")).strip().lower()
    checkpoint = _merge_checkpoint(
        task_id,
        checkpoint,
        publish_receipt_ref=publish_receipt_ref,
        publish_status=publish_status,
        push_status=push_status,
        commit_hash=str(result.get("commit_hash", "")).strip(),
        last_event_time=utc_now_iso(),
        recent_event_label="RESEARCH_PUBLISH_ATTEMPTED",
    )
    if publish_status != "ok" or push_status != "ok":
        return _merge_checkpoint(
            task_id,
            checkpoint,
            research_phase="publish",
            research_status="publish_retry",
        )

    task["commit_hash"] = str(result.get("commit_hash", "")).strip()
    task["publish_receipt_ref"] = publish_receipt_ref
    task["side_effect_level"] = "external_publish"
    _save_task(task)
    return _merge_checkpoint(
        task_id,
        checkpoint,
        research_phase="delivery",
        research_status="delivery_pending",
        completion_ready=False,
        recent_event_label="RESEARCH_PUBLISH_COMPLETED",
    )


def _archive_stage(*, task_id: str, trace_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
    task = _task(trace_id, task_id)
    checkpoint, allowed = _require_governance(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        task=task,
        phase="archive",
        repair_phase="review",
        state="REVIEW_DONE",
    )
    if not allowed:
        return checkpoint

    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="DELIVERY_GATE", reason="research archive assembly")
    round_records_ref = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="research_round_records",
        payload={"task_id": task_id, "issue_history": list(checkpoint.get("issue_history", []) or [])},
        filename="research_round_records.json",
        source="research_flow",
    ).ref
    trace_index_ref = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="research_trace_index",
        payload=_trace_index_payload(task_id=task_id, trace_id=trace_id),
        filename="research_trace_index.json",
        source="research_flow",
    ).ref
    result_summary_ref = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="result_summary",
        payload=_result_summary_payload(task=task, checkpoint=checkpoint),
        filename="result_summary.json",
        source="research_flow",
    ).ref
    raw_results_ref = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="raw_results",
        payload=_raw_results_payload(task=task, checkpoint=checkpoint),
        filename="raw_results.json",
        source="research_flow",
    ).ref
    data_record_ref = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="data_record",
        payload=_dataset_record_payload(task=task, checkpoint=checkpoint),
        filename="data_record.json",
        source="research_flow",
    ).ref
    code_bundle_ref = write_text_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="experiment_code",
        content=_experiment_script_body(task=task, checkpoint=checkpoint),
        media_type="text/x-python",
        filename="experiment.py",
        source="research_flow",
    ).ref
    reproduce_ref = write_text_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="reproduce",
        content=_reproduce_body(task=task, checkpoint=checkpoint),
        media_type="text/markdown",
        filename="reproduce.md",
        source="research_flow",
    ).ref
    manifest = {
        "task_id": task_id,
        "trace_id": trace_id,
        "unit_date": str(checkpoint.get("unit_date", "")).strip(),
        "selected_topic_id": str(checkpoint.get("selected_topic_id", "")).strip(),
        "outcome_kind": str(checkpoint.get("outcome_kind", "")).strip(),
        "anomaly": bool(checkpoint.get("anomaly", False)),
        "survey_ref": str(checkpoint.get("survey_ref", "")).strip(),
        "shortlist_ref": str(checkpoint.get("shortlist_ref", "")).strip(),
        "experiment_ref": str(checkpoint.get("experiment_ref", "")).strip(),
        "paper_ref": str(checkpoint.get("paper_ref", "")).strip(),
        "failure_note_ref": str(checkpoint.get("failure_note_ref", "")).strip(),
        "review_verdict_ref": str(checkpoint.get("review_verdict_ref", "")).strip(),
        "proposal_refs": list(checkpoint.get("proposal_refs", []) or []),
        "round_records_ref": round_records_ref,
        "trace_index_ref": trace_index_ref,
        "essay_ref": str(checkpoint.get("essay_ref", "")).strip(),
        "result_summary_ref": result_summary_ref,
        "raw_results_ref": raw_results_ref,
        "data_record_ref": data_record_ref,
        "reproduce_ref": reproduce_ref,
        "code_bundle_ref": code_bundle_ref,
        "final_report_ref": "",
        "message_refs": list(checkpoint.get("message_refs", []) or []),
        "issue_history": list(checkpoint.get("issue_history", []) or []),
        "communication_budget": {
            "message_count": int(checkpoint.get("message_count", 0) or 0),
            "packet_chars_total": int(checkpoint.get("packet_chars_total", 0) or 0),
            "max_packet_chars": int(checkpoint.get("max_packet_chars", 0) or 0),
        },
        "archived_at": utc_now_iso(),
    }
    archive_ref = write_json_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="research_archive_manifest",
        payload=manifest,
        filename="research_archive_manifest.json",
        source="research_flow",
    ).ref
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="RESEARCH_ARCHIVED",
        payload={"archive_ref": archive_ref, "message_count": manifest["communication_budget"]["message_count"]},
    )
    final_report_ref = write_text_artifact(
        task_id=task_id,
        attempt=ATTEMPT,
        artifact_id="final_report",
        content=_final_report_body(
            task_id=task_id,
            checkpoint={
                **checkpoint,
                "archive_ref": archive_ref,
                "trace_index_ref": trace_index_ref,
                "result_summary_ref": result_summary_ref,
                "raw_results_ref": raw_results_ref,
                "data_record_ref": data_record_ref,
                "reproduce_ref": reproduce_ref,
                "code_bundle_ref": code_bundle_ref,
                "publish_status": "",
            },
        ),
        media_type="text/markdown",
        filename="final_report.md",
        source="research_flow",
    ).ref
    review = checkpoint.get("final_review")
    review_decision = ""
    if isinstance(review, dict):
        review_decision = str(review.get("verdict", review.get("decision", ""))).strip()

    task["archive_ref"] = archive_ref
    task["research_outcome_kind"] = str(checkpoint.get("outcome_kind", "")).strip()
    task["selected_topic_id"] = str(checkpoint.get("selected_topic_id", "")).strip()
    task["trace_index_ref"] = trace_index_ref
    task["result_summary_ref"] = result_summary_ref
    task["raw_results_ref"] = raw_results_ref
    task["data_record_ref"] = data_record_ref
    task["reproduce_ref"] = reproduce_ref
    task["code_bundle_ref"] = code_bundle_ref
    task["essay_ref"] = str(checkpoint.get("essay_ref", "")).strip()
    task["final_report_ref"] = final_report_ref
    task["research_review_decision"] = review_decision
    _save_task(task)

    checkpoint = _merge_checkpoint(
        task_id,
        checkpoint,
        research_phase="publish",
        archive_ref=archive_ref,
        round_records_ref=round_records_ref,
        trace_index_ref=trace_index_ref,
        result_summary_ref=result_summary_ref,
        raw_results_ref=raw_results_ref,
        data_record_ref=data_record_ref,
        reproduce_ref=reproduce_ref,
        code_bundle_ref=code_bundle_ref,
        final_report_ref=final_report_ref,
        research_status="publish_pending",
        completion_ready=False,
        last_event_time=utc_now_iso(),
        recent_event_label="RESEARCH_ARCHIVED",
    )
    return checkpoint


def _delivery_stage(*, task_id: str, trace_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
    task = _task(trace_id, task_id)
    checkpoint, allowed = _require_governance(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        task=task,
        phase="delivery",
        repair_phase="archive",
        state="DELIVERY_GATE",
    )
    if not allowed:
        return checkpoint

    message_id = str(checkpoint.get("admin_completion_message_id", "")).strip()
    chat_id = _research_chat_id(task)
    if not chat_id:
        return _protocol_block(
            task_id=task_id,
            trace_id=trace_id,
            checkpoint=checkpoint,
            task=task,
            phase="delivery",
            missing=["admin_chat_id_missing"],
            repair_phase="delivery",
            reason="admin_delivery_prerequisite_missing",
            state="DELIVERY_GATE",
        )
    if not message_id:
        candidate = checkpoint.get("current_candidate")
        if not isinstance(candidate, dict):
            candidate = {}
        final_review = checkpoint.get("final_review")
        if not isinstance(final_review, dict):
            final_review = {}
        abstract = (
            f"{str(candidate.get('title', '')).strip() or str(task.get('question', '')).strip()[:120]} | "
            f"outcome={str(checkpoint.get('outcome_kind', '')).strip() or 'n/a'} | "
            f"review={str(final_review.get('verdict', final_review.get('decision', 'n/a'))).strip() or 'n/a'}"
        )
        checkpoint = _notify_once(
            task_id=task_id,
            trace_id=trace_id,
            checkpoint=checkpoint,
            key="admin_completion_report",
            text=(
                f"[AGN research] completed\n"
                f"task_id={task_id}\n"
                f"abstract={abstract}\n"
                f"hypothesis_supported={str(final_review.get('verdict', '')).strip() == 'APPROVED'}\n"
                f"archive_ref={str(checkpoint.get('archive_ref', '')).strip()}\n"
                f"essay_ref={str(checkpoint.get('essay_ref', '')).strip()}\n"
                f"result_summary_ref={str(checkpoint.get('result_summary_ref', '')).strip()}\n"
                f"code_bundle_ref={str(checkpoint.get('code_bundle_ref', '')).strip()}\n"
                f"final_report_ref={str(checkpoint.get('final_report_ref', '')).strip()}\n"
                f"commit_hash={str(checkpoint.get('commit_hash', '')).strip()}\n"
                f"push_status={str(checkpoint.get('push_status', '')).strip()}"
            ),
            message_kind="alert",
        )
        message_id = str((load_checkpoint(task_id) or checkpoint).get("admin_completion_message_id", "")).strip()
    if not message_id:
        return _protocol_block(
            task_id=task_id,
            trace_id=trace_id,
            checkpoint=checkpoint,
            task=task,
            phase="delivery",
            missing=["admin_completion_message_missing"],
            repair_phase="delivery",
            reason="admin_delivery_prerequisite_missing",
            state="DELIVERY_GATE",
        )
    telegram_receipt_ref = str(checkpoint.get("telegram_receipt_ref", "")).strip()
    if not telegram_receipt_ref:
        telegram_receipt_ref = write_json_artifact(
            task_id=task_id,
            attempt=ATTEMPT,
            artifact_id="telegram_receipt",
            payload={
                "task_id": task_id,
                "correlation_id": trace_id,
                "chat_id": chat_id,
                "message_id": message_id,
                "final_report_ref": str(checkpoint.get("final_report_ref", "")).strip(),
                "generated_at": utc_now_iso(),
            },
            filename="telegram_receipt.json",
            source="research_flow",
        ).ref

    delivered = True
    delivery_status = "queued"
    if _delivery_ack_required(task):
        delivered = _telegram_delivery_confirmed(
            task_id=task_id,
            trace_id=trace_id,
            message_id=message_id,
            final_report_ref=str(checkpoint.get("final_report_ref", "")).strip(),
        )
        delivery_status = "delivered" if delivered else "awaiting_sent_ack"

    checkpoint = _merge_checkpoint(
        task_id,
        checkpoint,
        telegram_receipt_ref=telegram_receipt_ref,
        admin_delivery_status=delivery_status,
        admin_delivery_checked_at=utc_now_iso(),
        completion_ready=delivered,
        recent_event_label="RESEARCH_ADMIN_DELIVERY_PENDING" if not delivered else "RESEARCH_ADMIN_DELIVERED",
    )
    if not delivered:
        return checkpoint

    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="RESEARCH_ADMIN_DELIVERED",
        payload={
            "message_id": message_id,
            "final_report_ref": str(checkpoint.get("final_report_ref", "")).strip(),
            "archive_ref": str(checkpoint.get("archive_ref", "")).strip(),
        },
    )
    _ensure_state(trace_id=trace_id, task_id=task_id, to_state="DELIVERED", reason="admin final report delivered")
    final_review = checkpoint.get("final_review")
    review_decision = ""
    if isinstance(final_review, dict):
        review_decision = str(final_review.get("verdict", final_review.get("decision", ""))).strip()
    task["decision"] = "approved" if review_decision == "APPROVED" else "rejected"
    task["admin_delivery_status"] = "delivered"
    _save_task(task)
    return _merge_checkpoint(
        task_id,
        checkpoint,
        research_phase="done",
        research_status="archived",
        completion_ready=True,
        admin_delivery_status="delivered",
        recent_event_label="RESEARCH_ADMIN_DELIVERED",
        last_event_time=utc_now_iso(),
    )


def _stage_summary(task_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
    final_review = checkpoint.get("final_review")
    final_review_decision = ""
    if isinstance(final_review, dict):
        final_review_decision = str(final_review.get("verdict", final_review.get("decision", ""))).strip()
    return {
        "task_id": task_id,
        "trace_id": str(checkpoint.get("trace_id", "")).strip(),
        "state": str(checkpoint.get("state", "")).strip(),
        "research_trigger_mode": str(checkpoint.get("research_trigger_mode", "")).strip(),
        "research_phase": str(checkpoint.get("research_phase", "")).strip(),
        "proposal_state": str(checkpoint.get("proposal_state", "")).strip(),
        "research_status": str(checkpoint.get("research_status", "")).strip(),
        "round": int(checkpoint.get("round", 0) or 0),
        "proposal_version": int(checkpoint.get("proposal_version", 0) or 0),
        "selected_topic_id": str(checkpoint.get("selected_topic_id", "")).strip(),
        "anomaly": bool(checkpoint.get("anomaly", False)),
        "rejected": bool(checkpoint.get("rejected", False)),
        "entered_third_round": bool(checkpoint.get("entered_third_round", False)),
        "degraded": bool(checkpoint.get("degraded", False)),
        "degrade_index": int(checkpoint.get("degrade_index", 0) or 0),
        "message_count": int(checkpoint.get("message_count", 0) or 0),
        "packet_chars_total": int(checkpoint.get("packet_chars_total", 0) or 0),
        "max_packet_chars": int(checkpoint.get("max_packet_chars", 0) or 0),
        "archive_ref": str(checkpoint.get("archive_ref", "")).strip(),
        "outcome_kind": str(checkpoint.get("outcome_kind", "")).strip(),
        "review_decision": final_review_decision,
        "recent_event": str(checkpoint.get("recent_event_label", "")).strip() or _latest_event_label(str(checkpoint.get("trace_id", "")).strip()),
        "experiment_ref": str(checkpoint.get("experiment_ref", "")).strip(),
        "experiment_summary_ref": str(checkpoint.get("experiment_summary_ref", "")).strip(),
        "experiment_log_ref": str(checkpoint.get("experiment_log_ref", "")).strip(),
        "paper_ref": str(checkpoint.get("paper_ref", "")).strip(),
        "failure_note_ref": str(checkpoint.get("failure_note_ref", "")).strip(),
        "review_verdict_ref": str(checkpoint.get("review_verdict_ref", "")).strip(),
        "trace_index_ref": str(checkpoint.get("trace_index_ref", "")).strip(),
        "coordinator_preflight_ref": str(checkpoint.get("coordinator_preflight_ref", "")).strip(),
        "essay_ref": str(checkpoint.get("essay_ref", "")).strip(),
        "code_bundle_ref": str(checkpoint.get("code_bundle_ref", "")).strip(),
        "result_summary_ref": str(checkpoint.get("result_summary_ref", "")).strip(),
        "final_report_ref": str(checkpoint.get("final_report_ref", "")).strip(),
        "publish_receipt_ref": str(checkpoint.get("publish_receipt_ref", "")).strip(),
        "publish_status": str(checkpoint.get("publish_status", "")).strip(),
        "push_status": str(checkpoint.get("push_status", "")).strip(),
        "commit_hash": str(checkpoint.get("commit_hash", "")).strip(),
        "telegram_receipt_ref": str(checkpoint.get("telegram_receipt_ref", "")).strip(),
        "admin_delivery_status": str(checkpoint.get("admin_delivery_status", "")).strip(),
        "awaiting_admin_response": bool(checkpoint.get("awaiting_admin_response", False)),
        "admin_hold_reason": str(checkpoint.get("admin_hold_reason", "")).strip(),
        "admin_hold_until": str(checkpoint.get("admin_hold_until", "")).strip() or str(checkpoint.get("daily_brief_deadline", "")).strip(),
        "governance_ready": bool(checkpoint.get("governance_ready", False)),
        "governance_missing": list(checkpoint.get("governance_missing", []) or []),
        "protocol_blocked": bool(checkpoint.get("protocol_blocked", False)),
        "protocol_block_reason": str(checkpoint.get("protocol_block_reason", "")).strip(),
        "completion_ready": bool(checkpoint.get("completion_ready", False)),
        "event_count": len(load_events(str(checkpoint.get("trace_id", "")).strip())),
    }


def drive_research_task(*, store: SSOTStore, task: dict[str, Any]) -> dict[str, Any]:
    profile = _load_profile()
    task_id = str(task.get("id", "")).strip()
    trace_id = str(task.get("correlation_id", "")).strip() or f"research-{task_id}-{uuid4().hex[:8]}"
    if not str(task.get("correlation_id", "")).strip():
        task["correlation_id"] = trace_id
        _save_task(task)
    checkpoint = _ensure_checkpoint(
        task_id=task_id,
        trace_id=trace_id,
        unit_date=str(task.get("unit_date", _today_iso())).strip() or _today_iso(),
        scenario=str(task.get("scenario", "daily")).strip() or "daily",
        trigger_mode=_trigger_mode(task),
    )

    if bool(checkpoint.get("paused", False)):
        return _stage_summary(task_id, checkpoint)

    phase = str(checkpoint.get("research_phase", "auto_survey")).strip().lower() or "auto_survey"
    if phase == "done":
        checkpoint, allowed = _require_governance(
            task_id=task_id,
            trace_id=trace_id,
            checkpoint=checkpoint,
            task=task,
            phase="done",
            repair_phase="delivery",
            state="DELIVERY_GATE",
        )
        delivery_missing = _completion_delivery_missing(task=task, trace_id=trace_id, checkpoint=checkpoint) if allowed else []
        delivered = (
            str(checkpoint.get("state", "")).strip().upper() == "DELIVERED"
            and bool(checkpoint.get("completion_ready", False))
            and not delivery_missing
        )
        if not delivered:
            missing = delivery_missing or ["completion_ready_missing"]
            checkpoint = _protocol_block(
                task_id=task_id,
                trace_id=trace_id,
                checkpoint=checkpoint,
                task=task,
                phase="done",
                missing=missing,
                repair_phase="delivery",
                reason="done_phase_without_verified_admin_delivery",
                state="DELIVERY_GATE",
            )
            phase = "delivery"
        else:
            return _stage_summary(task_id, checkpoint)
    if phase in {"auto_survey", "manual_intake", "survey"}:
        checkpoint = _survey_stage(task_id=task_id, trace_id=trace_id, checkpoint=checkpoint, profile=profile)
    elif phase in {"brief_wait", "selection_vote", "design", "discussion"}:
        checkpoint = _discussion_stage(task_id=task_id, trace_id=trace_id, checkpoint=checkpoint, profile=profile)
    elif phase in {"execution", "experiment"}:
        checkpoint = _experiment_stage(task_id=task_id, trace_id=trace_id, checkpoint=checkpoint, profile=profile)
    elif phase == "writing":
        checkpoint = _paper_stage(task_id=task_id, trace_id=trace_id, checkpoint=checkpoint)
    elif phase == "review":
        checkpoint = _review_stage(task_id=task_id, trace_id=trace_id, checkpoint=checkpoint)
    elif phase == "archive":
        checkpoint = _archive_stage(task_id=task_id, trace_id=trace_id, checkpoint=checkpoint)
    elif phase == "publish":
        checkpoint = _publish_stage(task_id=task_id, trace_id=trace_id, checkpoint=checkpoint)
    elif phase == "delivery":
        checkpoint = _delivery_stage(task_id=task_id, trace_id=trace_id, checkpoint=checkpoint)
    elif phase != "done":
        raise ValueError(f"unknown_research_phase:{phase}")

    return _stage_summary(task_id, load_checkpoint(task_id) or checkpoint)


def run_research_unit(
    *,
    task_id: str,
    unit_date: str,
    scenario: str,
    max_steps: int = 16,
    executor_provider: str = "",
    reviewer_provider: str = "",
    chat_id: str = "",
    source: str = "research_daily",
    research_mode: str = "",
    research_axis: str = "",
    question: str = "",
    hypothesis: str = "",
    baseline: str = "",
    single_change: str = "",
    manual_seed_topic_id: str = "",
    awaiting_admin_until: str = "",
    daily_brief_ref: str = "",
) -> dict[str, Any]:
    publish_runtime_surface(reason="research_task_start")
    refresh_ack = acknowledge_coordinator_refresh(actor="coordinator_heartbeat", refresh_mode="task_start")
    task = _ensure_task(
        task_id=task_id,
        unit_date=unit_date,
        scenario=scenario,
        executor_provider=executor_provider,
        reviewer_provider=reviewer_provider,
        chat_id=chat_id,
        source=source,
        research_mode=research_mode,
        research_axis=research_axis,
        question=question,
        hypothesis=hypothesis,
        baseline=baseline,
        single_change=single_change,
        manual_seed_topic_id=manual_seed_topic_id,
        awaiting_admin_until=awaiting_admin_until,
        daily_brief_ref=daily_brief_ref,
    )
    from coordinator_heartbeat import run_tick

    checkpoint = _ensure_checkpoint(
        task_id=task_id,
        trace_id=str(task.get("correlation_id", "")).strip(),
        unit_date=unit_date,
        scenario=scenario,
        trigger_mode=_trigger_mode(task),
    )
    if not _valid_task_ref(str(checkpoint.get("coordinator_refresh_ref", "")).strip(), task_id=task_id):
        coordinator_refresh_ref = _write_coordinator_refresh(task_id=task_id, refresh_ack=refresh_ack)
        append_event(
            trace_id=str(task.get("correlation_id", "")).strip(),
            task_id=task_id,
            event_type="RESEARCH_COORDINATOR_REFRESHED",
            payload={
                "coordinator_refresh_ref": coordinator_refresh_ref,
                "refresh_mode": str(refresh_ack.get("refresh_mode", "")).strip(),
                "briefing_hash": str(refresh_ack.get("briefing_hash", "")).strip(),
            },
        )
        checkpoint = _merge_checkpoint(
            task_id,
            checkpoint,
            coordinator_refresh_ref=coordinator_refresh_ref,
            last_event_time=utc_now_iso(),
            recent_event_label="RESEARCH_COORDINATOR_REFRESHED",
        )
        task["coordinator_refresh_ref"] = coordinator_refresh_ref
        _save_task(task)
    checkpoint = _notify_once(
        task_id=task_id,
        trace_id=str(task.get("correlation_id", "")).strip(),
        checkpoint=checkpoint,
        key="task_started",
        text=(
            f"[AGN research] started\n"
            f"task_id={task_id}\n"
            f"scenario={scenario}\n"
            f"executor={str(task.get('executor_provider', '')).strip()}\n"
            f"reviewer={str(task.get('reviewer_provider', '')).strip()}"
        ),
    )
    for _ in range(max_steps):
        checkpoint = load_checkpoint(task_id) or checkpoint
        if str(checkpoint.get("research_phase", "")).strip() == "done":
            break
        run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
    return _stage_summary(task_id, load_checkpoint(task_id) or checkpoint)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run or resume one AGN research unit")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--unit-date", default="")
    parser.add_argument("--scenario", default="daily", choices=["daily", "validation"])
    parser.add_argument("--max-steps", type=int, default=16)
    parser.add_argument("--executor-provider", default=_default_executor_provider())
    parser.add_argument("--reviewer-provider", default=_default_reviewer_provider())
    parser.add_argument("--chat-id", default=_default_admin_chat_id())
    parser.add_argument("--source", default="research_daily")
    parser.add_argument("--research-mode", default="")
    parser.add_argument("--research-axis", default="")
    parser.add_argument("--question", default="")
    parser.add_argument("--hypothesis", default="")
    parser.add_argument("--baseline", default="")
    parser.add_argument("--single-change", default="")
    parser.add_argument("--manual-seed-topic-id", default="")
    parser.add_argument("--awaiting-admin-until", default="")
    parser.add_argument("--daily-brief-ref", default="")
    args = parser.parse_args()

    unit_date = str(args.unit_date or "").strip() or _today_iso()
    task_id = str(args.task_id or "").strip() or f"research-{unit_date}"
    summary = run_research_unit(
        task_id=task_id,
        unit_date=unit_date,
        scenario=str(args.scenario or "daily").strip(),
        max_steps=max(1, int(args.max_steps or 16)),
        executor_provider=str(args.executor_provider or "").strip().lower(),
        reviewer_provider=str(args.reviewer_provider or "").strip().lower(),
        chat_id=str(args.chat_id or "").strip(),
        source=str(args.source or "research_daily").strip() or "research_daily",
        research_mode=str(args.research_mode or "").strip().lower(),
        research_axis=str(args.research_axis or "").strip(),
        question=str(args.question or "").strip(),
        hypothesis=str(args.hypothesis or "").strip(),
        baseline=str(args.baseline or "").strip(),
        single_change=str(args.single_change or "").strip(),
        manual_seed_topic_id=str(args.manual_seed_topic_id or "").strip(),
        awaiting_admin_until=str(args.awaiting_admin_until or "").strip(),
        daily_brief_ref=str(args.daily_brief_ref or "").strip(),
    )
    print(json.dumps(summary, ensure_ascii=True))
    return 0 if str(summary.get("research_phase", "")).strip() == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
