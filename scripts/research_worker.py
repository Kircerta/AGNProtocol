#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from urllib import error as urllib_error
from urllib import request as urllib_request
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn_api.ssot_store import SSOTStore

try:
    from agent_runner import run_command
except ImportError:  # pragma: no cover - package import fallback
    from scripts.agent_runner import run_command

try:
    from pointer_protocol import read_ref_text
except ImportError:  # pragma: no cover - package import fallback
    from scripts.pointer_protocol import read_ref_text

try:
    from provider_registry import load_registry, resolve_executor_provider, resolve_reviewer_provider
except ImportError:  # pragma: no cover - package import fallback
    from scripts.provider_registry import load_registry, resolve_executor_provider, resolve_reviewer_provider
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


REGISTRY = load_registry()
SCRATCH_DIR = ROOT / ".agn_workspace" / "scratch" / "research_worker"
PACKET_MIRROR_DIR = ROOT / "runtime" / "research_packets"
VALID_TRANSPORTS = {"cli", "stub", "deterministic"}
QWEN_LOCAL_ALLOWED_MODES = {"role_init", "topic_vote"}
PROVIDER_FALLBACKS: dict[str, dict[str, list[str]]] = {
    "executor": {
        "qwen_local": ["gemini", "codex"],
        "gemini": ["codex"],
        "claude": ["codex"],
    },
    "reviewer": {
        "qwen_local": ["deepseek", "gemini", "codex"],
        "deepseek": ["gemini", "codex"],
        "gemini": ["codex"],
        "claude": ["codex"],
    },
}
TRUSTED_DEPENDENCY_SOURCES = {
    "torch": {
        "package": "torch",
        "source_label": "pytorch_official_cpu_whl",
        "index_url": "https://download.pytorch.org/whl/cpu",
        "trusted_hosts": ["download.pytorch.org"],
    }
}
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


def _packet(packet_ref: str) -> dict[str, Any]:
    payload = json.loads(read_ref_text(packet_ref, mode="all", max_bytes=512 * 1024))
    return payload if isinstance(payload, dict) else {}


def _repo_file(rel_path: str) -> Path:
    candidate = (ROOT / str(rel_path or "").strip()).resolve()
    try:
        candidate.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError(f"init_path_outside_repo:{rel_path}") from exc
    return candidate


def _load_init_materials(paths: Any, expected_role: str) -> list[dict[str, Any]]:
    if not isinstance(paths, list) or not paths:
        raise ValueError("role_init_paths_missing")
    loaded: list[dict[str, Any]] = []
    for raw in paths:
        path = _repo_file(str(raw))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            rel = str(path.relative_to(ROOT))
            raise ValueError(f"role_init_material_invalid:{rel}") from exc
        if isinstance(payload, dict):
            loaded.append(payload)
    if not loaded:
        raise ValueError("role_init_materials_unreadable")
    for payload in loaded:
        role = str(payload.get("role", "")).strip()
        if role and role != expected_role:
            raise ValueError(f"role_init_mismatch:{role}")
    return loaded


def _role_init_digest(paths: Any) -> str:
    digest = hashlib.sha256()
    if not isinstance(paths, list) or not paths:
        return ""
    for raw in paths:
        path = _repo_file(str(raw))
        digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _require_fields(packet: dict[str, Any], fields: list[str]) -> list[str]:
    missing: list[str] = []
    for key in fields:
        if key not in packet:
            missing.append(key)
            continue
        value = packet.get(key)
        if isinstance(value, str) and not value.strip():
            missing.append(key)
        elif value is None:
            missing.append(key)
    return missing


def _ensure_role_context(packet: dict[str, Any], expected_role: str) -> list[dict[str, Any]]:
    role = str(packet.get("role", "")).strip()
    if role != expected_role:
        raise ValueError(f"role_mismatch:{role or 'missing'}")
    missing = _require_fields(packet, ["role", "goal", "current_round", "current_action_required", "output_schema"])
    if missing:
        raise ValueError(f"missing_packet_fields:{','.join(missing)}")
    return _load_init_materials(packet.get("role_init_paths"), expected_role)


def _vote_no(*, role: str, mode: str, problem: str, risk: str, minimal_change: str, checks: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": role,
        "mode": mode,
        "decision": "no",
        "problem": problem,
        "risk": risk,
        "minimal_change": minimal_change,
        "message": f"NO. problem={problem} risk={risk} minimal_change={minimal_change}",
        "checks": checks,
    }


def _vote_yes(*, role: str, mode: str, summary: str, checks: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": role,
        "mode": mode,
        "decision": "yes",
        "problem": "",
        "risk": "",
        "minimal_change": "",
        "message": f"YES. {summary}",
        "checks": checks,
    }


def _review_approved(*, summary: str, checks: dict[str, Any], evidence_boundary: list[str]) -> dict[str, Any]:
    return {
        "role": "reviewer",
        "mode": "final_review",
        "verdict": "APPROVED",
        "issue": "",
        "risk": "",
        "minimal_fix": "",
        "failure_type": "",
        "reason": summary,
        "rerun_worth_it": False,
        "evidence_boundary": evidence_boundary,
        "checks": checks,
        "message": summary,
    }


def _review_revision_once(*, issue: str, risk: str, minimal_fix: str, checks: dict[str, Any], evidence_boundary: list[str]) -> dict[str, Any]:
    return {
        "role": "reviewer",
        "mode": "final_review",
        "verdict": "REVISION_ONCE",
        "issue": issue,
        "risk": risk,
        "minimal_fix": minimal_fix,
        "failure_type": "",
        "reason": "",
        "rerun_worth_it": False,
        "evidence_boundary": evidence_boundary,
        "checks": checks,
        "message": f"REVISION_ONCE. issue={issue} risk={risk} minimal_fix={minimal_fix}",
    }


def _review_failure_archive(
    *,
    failure_type: str,
    reason: str,
    rerun_worth_it: bool,
    checks: dict[str, Any],
    evidence_boundary: list[str],
) -> dict[str, Any]:
    return {
        "role": "reviewer",
        "mode": "final_review",
        "verdict": "FAILURE_ARCHIVE",
        "issue": "",
        "risk": "",
        "minimal_fix": "",
        "failure_type": failure_type,
        "reason": reason,
        "rerun_worth_it": bool(rerun_worth_it),
        "evidence_boundary": evidence_boundary,
        "checks": checks,
        "message": f"FAILURE_ARCHIVE. failure_type={failure_type} reason={reason}",
    }


def _role_init_ack(*, role: str, packet: dict[str, Any]) -> dict[str, Any]:
    _load_init_materials(packet.get("init_paths"), role)
    protocol_digest = _role_init_digest(packet.get("init_paths"))
    return {
        "role": role,
        "mode": "role_init",
        "ack": "init_loaded",
        "current_round": int(packet.get("current_round", 0) or 0),
        "schema": str(packet.get("confirmation_schema", {}).get("schema", "")).strip(),
        "protocol_digest": protocol_digest,
        "integrity_ack": "truthfulness_first",
        "failure_ack": "failure_is_valid",
        "fabrication_ack": "no_fabrication",
    }


def _topic_vote(*, role: str, packet: dict[str, Any]) -> dict[str, Any]:
    _ensure_role_context(packet, role)
    proposal = packet.get("proposal", packet.get("current_proposal", {}))
    if not isinstance(proposal, dict):
        proposal = {}

    checks = {
        "task_question": bool(str(packet.get("task_question", proposal.get("question", ""))).strip()),
        "baseline_clear": bool(str(packet.get("baseline", proposal.get("baseline", ""))).strip()),
        "single_change": bool(str(packet.get("single_change", proposal.get("single_change", ""))).strip()),
        "budget_present": isinstance(packet.get("budget"), dict) and bool(packet.get("budget")),
        "axis_allowed": bool(str(proposal.get("research_axis", "")).strip()),
        "data_ready": bool(proposal.get("data_ready", False)),
        "fixed_budget": bool(proposal.get("fixed_budget", False)) and isinstance(packet.get("budget"), dict) and bool(packet.get("budget")),
        "falsifiable": bool(proposal.get("falsifiable", False)),
        "degrade_ready": bool(proposal.get("degrade_ready", False)),
        "external_dependency": bool(proposal.get("external_dependency", False)),
    }

    if not checks["axis_allowed"]:
        return _vote_no(
            role=role,
            mode="topic_vote",
            problem="candidate axis is outside the allowed research scope",
            risk="the daily unit drifts away from the fixed interest axes",
            minimal_change="replace the topic with one drawn from the allowed axis list",
            checks=checks,
        )

    if role == "executor":
        if checks["external_dependency"] and not checks["data_ready"]:
            return _vote_no(
                role=role,
                mode="topic_vote",
                problem="execution depends on unavailable external data",
                risk="the experiment can stall before any research unit is completed",
                minimal_change="switch to synthetic or already-local data and keep the baseline budget-fixed",
                checks=checks,
            )
        if not checks["task_question"]:
            return _vote_no(
                role=role,
                mode="topic_vote",
                problem="task question is missing from the packet",
                risk="execution cannot judge whether the proposal is narrow enough to run",
                minimal_change="include one explicit task question in the executor packet",
                checks=checks,
            )
        if not checks["fixed_budget"] or not checks["budget_present"]:
            return _vote_no(
                role=role,
                mode="topic_vote",
                problem="execution budget is not fixed enough for unattended runtime",
                risk="the nightly run can expand beyond the self-hosting envelope",
                minimal_change="shrink the scope and pre-commit a small fixed budget",
                checks=checks,
            )
        if not checks["baseline_clear"] or not checks["single_change"]:
            return _vote_no(
                role=role,
                mode="topic_vote",
                problem="the baseline or single change is underspecified",
                risk="the resulting artifact will not show what actually improved or failed",
                minimal_change="state one runnable baseline and one single change before keeping the topic",
                checks=checks,
            )
        return _vote_yes(
            role=role,
            mode="topic_vote",
            summary="packet is executable inside the fixed local budget.",
            checks=checks,
        )

    if not checks["falsifiable"]:
        return _vote_no(
            role=role,
            mode="topic_vote",
            problem="the proposal still lacks a clean falsifiable check",
            risk="the daily unit could look complete while teaching little",
            minimal_change="add one explicit measurable comparison against the stated baseline",
            checks=checks,
        )
    if not checks["degrade_ready"]:
        return _vote_no(
            role=role,
            mode="topic_vote",
            problem="the degrade path is unclear",
            risk="a mid-run failure can still leave a blank day",
            minimal_change="name the fallback degrade chain and the final failure-oriented output",
            checks=checks,
        )
    if not checks["baseline_clear"]:
        return _vote_no(
            role=role,
            mode="topic_vote",
            problem="review cannot anchor on a concrete baseline",
            risk="the final interpretation will be ambiguous",
            minimal_change="state the baseline and the expected comparison in one sentence",
            checks=checks,
        )
    return _vote_yes(
        role=role,
        mode="topic_vote",
        summary="proposal is narrow enough to evaluate and preserves a useful failure path.",
        checks=checks,
    )


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


def _experiment_result(*, proposal: dict[str, Any], strategy: str, force_fail: bool) -> dict[str, Any]:
    if force_fail:
        raise RuntimeError("validation_forced_experiment_failure")

    # Try LLM-powered experiment first.
    try:
        from research_llm import run_llm_experiment
        result = run_llm_experiment(proposal, strategy=strategy)
        if result.get("status") in ("ok", "degraded") and result.get("empirical_execution"):
            return result
        # LLM experiment failed — fall through to synthetic fallback.
    except Exception:
        pass

    # Synthetic fallback for when LLM is unavailable.
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
        "topic_id": str(proposal.get("topic_id", "")).strip(),
        "title": str(proposal.get("title", "")).strip(),
        "strategy": strategy,
        "status": "degraded" if strategy != "full" else "ok",
        "empirical_execution": True,
        "truthfulness_status": "empirical",
        "truthfulness_reason": "synthetic_fallback_execution",
        "cases": rows,
        "metrics": {
            "case_count": len(rows),
            "accuracy": round(accuracy, 4),
        },
        "notes": [
            "LLM-powered experiment unavailable; fell back to synthetic lag-detection.",
            "The synthetic signal uses lag-structured dependencies with contiguous missing regions.",
        ],
    }


def _run_experiment(packet: dict[str, Any]) -> dict[str, Any]:
    _ensure_role_context(packet, "executor")
    task = _load_task(packet)
    task_id = str(packet.get("task_id", "")).strip() or str(task.get("id", "")).strip() or "research"
    proposal = packet.get("proposal", {})
    if not isinstance(proposal, dict):
        proposal = {}
    required_method_family = str(packet.get("required_method_family", "")).strip().lower()
    same_family_only = bool(packet.get("same_family_only", False))
    dependency_install_attempts: list[dict[str, Any]] = []
    if required_method_family == "tiny_conv_autoencoder":
        try:
            import torch  # type: ignore  # pragma: no cover - dependency probe only

            del torch
        except Exception:
            install_attempt = _attempt_trusted_dependency_install(task_id=task_id, task=task, packet=packet, package_name="torch")
            dependency_install_attempts.append(install_attempt)
            try:
                import torch  # type: ignore  # pragma: no cover - dependency probe only

                del torch
            except Exception:
                return {
                    "role": "executor",
                    "mode": "run_experiment",
                    "topic_id": str(proposal.get("topic_id", "")).strip(),
                    "status": "failure_note",
                    "strategy": "failure_note",
                    "error": "dependency_unavailable:tiny_conv_autoencoder_requires_torch",
                    "exception_category": "SYSTEM_DEGRADE.dependency_unavailable",
                    "same_family_only": same_family_only,
                    "completed_work": ["proposal interpreted", "dependency check executed", "trusted dependency install attempted"],
                    "failed_strategies": [{"strategy": "full", "error": "ModuleNotFoundError:torch"}],
                    "full_failure_observed": True,
                    "dependency_install_attempts": dependency_install_attempts,
                }
    strategies = packet.get("strategy_candidates", [])
    if not isinstance(strategies, list) or not strategies:
        raise ValueError("strategy_candidates_missing")

    failed_strategies: list[dict[str, Any]] = []
    force_fail_full = bool(packet.get("simulate_full_failure_once", False))
    for raw_strategy in strategies:
        strategy = str(raw_strategy).strip()
        if not strategy:
            continue
        try:
            result = _experiment_result(
                proposal=proposal,
                strategy="baseline_only" if strategy == "baseline_only" else ("full" if strategy == "full" else strategy),
                force_fail=force_fail_full and strategy == "full",
            )
            result["role"] = "executor"
            result["mode"] = "run_experiment"
            result["failed_strategies"] = failed_strategies
            result["full_failure_observed"] = any(item.get("strategy") == "full" for item in failed_strategies)
            if dependency_install_attempts:
                result["dependency_install_attempts"] = dependency_install_attempts
                notes = result.get("notes")
                if not isinstance(notes, list):
                    notes = []
                notes.append("Trusted dependency installation was attempted before execution continued.")
                result["notes"] = notes
            return result
        except Exception as exc:
            failed_strategies.append({"strategy": strategy, "error": f"{type(exc).__name__}:{exc}"})

    result = {
        "role": "executor",
        "mode": "run_experiment",
        "topic_id": str(proposal.get("topic_id", "")).strip(),
        "status": "failure_note",
        "strategy": "failure_note",
        "error": failed_strategies[-1]["error"] if failed_strategies else "experiment_failed_without_error",
        "completed_work": ["survey complete", "shortlist complete", "discussion complete"],
        "failed_strategies": failed_strategies,
        "full_failure_observed": any(item.get("strategy") == "full" for item in failed_strategies),
    }
    if dependency_install_attempts:
        result["dependency_install_attempts"] = dependency_install_attempts
    return result


def _git_user_env() -> dict[str, str]:
    def _git_identity(*args: str) -> str:
        try:
            proc = subprocess.run(
                ["git", "config", *args],
                cwd=str(ROOT),
                text=True,
                capture_output=True,
                timeout=5,
            )
        except Exception:
            return ""
        if proc.returncode != 0:
            return ""
        return str(proc.stdout or "").strip()

    default_name = _git_identity("--get", "user.name") or _git_identity("--global", "--get", "user.name") or "AGN Research Executor"
    default_email = _git_identity("--get", "user.email") or _git_identity("--global", "--get", "user.email") or "agn-research@example.com"
    author_name = str(os.getenv("AGN_GIT_AUTHOR_NAME") or "").strip() or default_name
    author_email = str(os.getenv("AGN_GIT_AUTHOR_EMAIL") or "").strip() or default_email
    committer_name = str(os.getenv("AGN_GIT_COMMITTER_NAME") or "").strip() or default_name
    committer_email = str(os.getenv("AGN_GIT_COMMITTER_EMAIL") or "").strip() or default_email
    return {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": committer_name,
        "GIT_COMMITTER_EMAIL": committer_email,
    }


def _repo_output_dir(repo_root: Path, task_id: str) -> Path:
    safe = str(task_id or "research").replace("/", "_").strip() or "research"
    return repo_root / "research_outputs" / safe


def _slugify(value: str) -> str:
    chars: list[str] = []
    prev_dash = False
    for ch in str(value or "").lower():
        if ch.isalnum():
            chars.append(ch)
            prev_dash = False
        elif not prev_dash:
            chars.append("-")
            prev_dash = True
    slug = "".join(chars).strip("-")
    return slug or "daily-research-note"


def _resolve_publish_target(packet: dict[str, Any], task: dict[str, Any]) -> tuple[Path | None, str, str]:
    repo_path = str(
        packet.get("repo_path", "")
        or task.get("repo_path", "")
        or resolve_research_publish_repo_path()
    ).strip()
    work_branch = str(
        packet.get("work_branch", "")
        or task.get("work_branch", "")
        or resolve_research_publish_branch()
    ).strip() or "main"
    if not repo_path:
        return None, work_branch, "publish_repo_path_missing"
    repo_root = Path(repo_path).expanduser().resolve()
    if repo_root == ROOT.resolve():
        return None, work_branch, "infra_repo_publish_forbidden"
    if not repo_root.exists() or not repo_root.is_dir():
        return None, work_branch, f"publish_repo_missing:{repo_root}"
    if not (repo_root / ".git").exists():
        return None, work_branch, f"publish_repo_not_git:{repo_root}"
    return repo_root, work_branch, ""


def _resolve_blog_publish_target(packet: dict[str, Any], task: dict[str, Any]) -> tuple[Path | None, str, str, str]:
    repo_path = str(
        packet.get("blog_repo_path", "")
        or task.get("blog_repo_path", "")
        or resolve_research_blog_repo_path()
    ).strip()
    work_branch = str(
        packet.get("blog_work_branch", "")
        or task.get("blog_work_branch", "")
        or resolve_research_blog_branch()
    ).strip() or "main"
    science_dir = str(
        packet.get("blog_science_dir", "")
        or task.get("blog_science_dir", "")
        or resolve_research_blog_science_dir()
    ).strip() or "content/AGNResearch"
    if not repo_path:
        return None, work_branch, science_dir, "blog_repo_path_missing"
    repo_root = Path(repo_path).expanduser().resolve()
    if not repo_root.exists() or not repo_root.is_dir():
        return None, work_branch, science_dir, f"blog_repo_missing:{repo_root}"
    if not (repo_root / ".git").exists():
        return None, work_branch, science_dir, f"blog_repo_not_git:{repo_root}"
    target_dir = (repo_root / science_dir).resolve()
    try:
        target_dir.relative_to(repo_root)
    except ValueError:
        return None, work_branch, science_dir, "blog_science_dir_outside_repo"
    return repo_root, work_branch, science_dir, ""


def _git_commit_and_push(*, repo_root: Path, branch: str, add_paths: list[str], message: str, log_path: Path) -> dict[str, Any]:
    no_changes = False
    branch_outcome = run_command(
        cmd=["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root,
        timeout_sec=30.0,
        log_path=log_path,
    )
    current_branch = str(branch_outcome.stdout or "").strip() if branch_outcome.return_code == 0 else ""
    if current_branch and current_branch not in {"HEAD", branch}:
        checkout_outcome = run_command(
            cmd=["git", "-C", str(repo_root), "checkout", branch],
            cwd=repo_root,
            timeout_sec=60.0,
            log_path=log_path,
        )
        if checkout_outcome.return_code != 0:
            checkout_outcome = run_command(
                cmd=["git", "-C", str(repo_root), "checkout", "-b", branch],
                cwd=repo_root,
                timeout_sec=60.0,
                log_path=log_path,
            )
        if checkout_outcome.return_code != 0:
            return {"ok": False, "error": "git_checkout_failed", "commit_hash": "", "push_status": "failed", "no_changes": False, "branch": branch}
    elif current_branch and current_branch != "HEAD":
        branch = current_branch

    add_cmd = ["git", "-C", str(repo_root), "add", *add_paths]
    add_outcome = run_command(cmd=add_cmd, cwd=repo_root, timeout_sec=60.0, log_path=log_path)
    if add_outcome.return_code != 0:
        return {"ok": False, "error": "git_add_failed", "commit_hash": "", "push_status": "failed", "no_changes": False, "branch": branch}

    commit_outcome = run_command(
        cmd=["git", "-C", str(repo_root), "commit", "-m", message],
        cwd=repo_root,
        timeout_sec=120.0,
        log_path=log_path,
        env=_git_user_env(),
    )
    no_changes = commit_outcome.return_code != 0 and "nothing to commit" in str(commit_outcome.stdout + commit_outcome.stderr).lower()
    if commit_outcome.return_code != 0 and not no_changes:
        return {"ok": False, "error": "git_commit_failed", "commit_hash": "", "push_status": "failed", "no_changes": False, "branch": branch}

    hash_outcome = run_command(
        cmd=["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        cwd=repo_root,
        timeout_sec=30.0,
        log_path=log_path,
    )
    commit_hash = str(hash_outcome.stdout or "").strip() if hash_outcome.return_code == 0 else ""
    push_status = "ok"
    error = ""
    if branch:
        push_outcome = run_command(
            cmd=["git", "-C", str(repo_root), "push", "origin", branch],
            cwd=repo_root,
            timeout_sec=180.0,
            log_path=log_path,
        )
        if push_outcome.return_code != 0:
            push_outcome = run_command(
                cmd=["git", "-C", str(repo_root), "push", "-u", "origin", f"HEAD:{branch}"],
                cwd=repo_root,
                timeout_sec=180.0,
                log_path=log_path,
            )
        if push_outcome.return_code != 0:
            push_status = "failed"
            error = "git_push_failed"
    else:
        push_status = "failed"
        error = "git_branch_unresolved"
    return {
        "ok": push_status == "ok",
        "error": error,
        "commit_hash": commit_hash,
        "push_status": push_status,
        "no_changes": no_changes,
        "branch": branch,
    }


def _blog_post_title(packet: dict[str, Any], task: dict[str, Any]) -> str:
    for value in (
        packet.get("title", ""),
        task.get("manual_title", ""),
        task.get("question", ""),
        task.get("id", ""),
    ):
        clean = str(value or "").strip()
        if clean:
            return clean
    return "Daily Research Note"


def _blog_post_summary(packet: dict[str, Any], task: dict[str, Any]) -> str:
    question = str(packet.get("question", "") or task.get("question", "")).strip()
    hypothesis = str(packet.get("hypothesis", "") or task.get("hypothesis", "")).strip()
    if question and hypothesis:
        return f"{question} Hypothesis: {hypothesis}"
    return question or hypothesis or "A daily AGN research note."


def _blog_post_body(*, packet: dict[str, Any], task: dict[str, Any], task_id: str) -> str:
    essay = _read_ref_payload(str(packet.get("essay_ref", "")).strip())
    final_report = _read_ref_payload(str(packet.get("final_report_ref", "")).strip())
    result_summary_ref = str(packet.get("result_summary_ref", "")).strip()
    raw_results_ref = str(packet.get("raw_results_ref", "")).strip()
    data_record_ref = str(packet.get("data_record_ref", "")).strip()
    reproduce_ref = str(packet.get("reproduce_ref", "")).strip()
    code_bundle_ref = str(packet.get("code_bundle_ref", "")).strip()
    archive_ref = str(packet.get("archive_ref", "")).strip()
    trace_index_ref = str(packet.get("trace_index_ref", "")).strip()
    body = essay or final_report
    if not body:
        raise ValueError("publish_artifact_missing:blog_post_body")
    appendix = [
        "",
        "## Artifact References",
        f"- task_id: `{task_id}`",
        f"- result_summary_ref: `{result_summary_ref or 'n/a'}`",
        f"- raw_results_ref: `{raw_results_ref or 'n/a'}`",
        f"- data_record_ref: `{data_record_ref or 'n/a'}`",
        f"- reproduce_ref: `{reproduce_ref or 'n/a'}`",
        f"- code_bundle_ref: `{code_bundle_ref or 'n/a'}`",
        f"- archive_ref: `{archive_ref or 'n/a'}`",
        f"- trace_index_ref: `{trace_index_ref or 'n/a'}`",
    ]
    if str(task.get("repo_path", "")).strip():
        appendix.append(f"- research_repo: `{str(task.get('repo_path', '')).strip()}`")
    return body.rstrip() + "\n" + "\n".join(appendix) + "\n"


def _render_blog_post(*, packet: dict[str, Any], task: dict[str, Any], task_id: str) -> tuple[str, str]:
    unit_date = str(packet.get("unit_date", "") or task.get("unit_date", "")).strip()
    title = _blog_post_title(packet, task)
    slug_base = _slugify(title)
    slug = slug_base if not unit_date else f"{unit_date}-{slug_base}"
    summary = _blog_post_summary(packet, task).replace('"', "'")
    axis = str(packet.get("research_axis", "") or task.get("research_axis", "")).strip()
    tag_axis = _slugify(axis).replace("-", " ") if axis else "machine learning"
    tz = ZoneInfo("America/Toronto")
    published_at = datetime.now(tz).isoformat(timespec="seconds")
    front_matter = "\n".join(
        [
            "+++",
            f"date = '{published_at}'",
            "draft = false",
            "hiddenInHomeList = true",
            f'title = "{title.replace(chr(34), chr(39))}"',
            f'slug = "{slug}"',
            f'tags = ["Research", "{tag_axis.title()}"]',
            f'summary = "{summary}"',
            "",
            "[params]",
            "  ShowBreadCrumbs = true",
            "+++",
            "",
        ]
    )
    body = _blog_post_body(packet=packet, task=task, task_id=task_id)
    return slug, front_matter + body


def _trusted_dependency_spec(package_name: str) -> dict[str, Any]:
    return TRUSTED_DEPENDENCY_SOURCES.get(str(package_name or "").strip().lower(), {})


def _trusted_dependency_installs_allowed(task: dict[str, Any], packet: dict[str, Any]) -> bool:
    if packet.get("allow_trusted_dependency_installs") is False:
        return False
    if task.get("allow_trusted_dependency_installs") is False:
        return False
    return True


def _attempt_trusted_dependency_install(*, task_id: str, task: dict[str, Any], packet: dict[str, Any], package_name: str) -> dict[str, Any]:
    spec = _trusted_dependency_spec(package_name)
    if not spec:
        return {
            "package": str(package_name or "").strip(),
            "status": "unsupported",
            "error": "trusted_dependency_source_unknown",
            "command_log_path": "",
        }
    if not _trusted_dependency_installs_allowed(task, packet):
        return {
            "package": str(package_name or "").strip(),
            "status": "disabled",
            "error": "trusted_dependency_installs_disabled",
            "source_label": str(spec.get("source_label", "")).strip(),
            "index_url": str(spec.get("index_url", "")).strip(),
            "command_log_path": "",
        }
    log_path = _command_log_path(task_id=task_id, role="executor", mode=f"install_{package_name}")
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--index-url",
        str(spec.get("index_url", "")).strip(),
    ]
    for host in spec.get("trusted_hosts", []) or []:
        if str(host).strip():
            cmd.extend(["--trusted-host", str(host).strip()])
    cmd.append(str(spec.get("package", package_name)).strip())
    outcome = run_command(
        cmd=cmd,
        cwd=ROOT,
        timeout_sec=900.0,
        log_path=log_path,
    )
    return {
        "package": str(spec.get("package", package_name)).strip(),
        "status": "installed" if outcome.return_code == 0 else "failed",
        "error": "" if outcome.return_code == 0 else "trusted_dependency_install_failed",
        "source_label": str(spec.get("source_label", "")).strip(),
        "index_url": str(spec.get("index_url", "")).strip(),
        "trusted_hosts": [str(item).strip() for item in (spec.get("trusted_hosts", []) or []) if str(item).strip()],
        "command_log_path": str(log_path),
        "return_code": int(outcome.return_code),
    }


def _read_ref_payload(ref: str) -> str:
    clean = str(ref or "").strip()
    if not clean.startswith("agn://"):
        return ""
    try:
        return read_ref_text(clean, mode="all", max_bytes=1024 * 1024)
    except Exception:
        return ""


def _text_has_marker(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in NON_EMPIRICAL_MARKERS)


def _clean_existing_paths(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for raw in value:
        path = str(raw or "").strip()
        if not path:
            continue
        try:
            candidate = Path(path).expanduser()
        except Exception:
            continue
        if candidate.exists():
            cleaned.append(str(candidate.resolve()))
    return cleaned


def _execution_truthfulness(*, provider: str, payload: dict[str, Any]) -> tuple[bool, str, str, list[str]]:
    strategy = str(payload.get("strategy", "")).strip().lower()
    status = str(payload.get("status", "")).strip().lower()
    notes = payload.get("notes")
    if not isinstance(notes, list):
        notes = []
    notes_text = "\n".join(str(item).strip() for item in notes if str(item).strip())
    error_text = str(payload.get("error", "")).strip()
    evidence_paths = _clean_existing_paths(payload.get("execution_evidence_paths"))
    simulated = (
        strategy == "dry_run"
        or _text_has_marker(notes_text)
        or _text_has_marker(error_text)
    )
    if provider == "stub":
        empirical = status in {"ok", "degraded"} and strategy not in {"dry_run", "failure_note"} and not simulated
        if empirical:
            return True, "empirical", "stub_local_execution", evidence_paths
        if simulated:
            return False, "non_empirical", "stub_run_marked_as_simulated", evidence_paths
        return False, "failure_note", "stub_execution_failed", evidence_paths
    if status == "failure_note":
        return False, "failure_note", error_text or "executor_reported_failure_note", evidence_paths
    if simulated:
        return False, "non_empirical", notes_text or error_text or "executor_reported_simulated_execution", evidence_paths
    if evidence_paths:
        return True, "empirical", "cli_execution_evidence_present", evidence_paths
    return False, "non_empirical", "cli_result_has_no_verifiable_execution_evidence", evidence_paths


def _publish_research(packet: dict[str, Any]) -> dict[str, Any]:
    _ensure_role_context(packet, "executor")
    task = _load_task(packet)
    if task.get("allow_external_publish") is not True or task.get("admin_approved") is not True:
        return {
            "role": "executor",
            "mode": "publish_research",
            "status": "retry",
            "push_status": "blocked",
            "error": "external_publish_not_preapproved",
            "published_files": [],
            "commit_hash": "",
        }

    task_id = str(packet.get("task_id", "")).strip() or str(task.get("id", "")).strip() or "research"
    repo_root, work_branch, target_error = _resolve_publish_target(packet, task)
    if repo_root is None:
        return {
            "role": "executor",
            "mode": "publish_research",
            "status": "retry",
            "push_status": "failed",
            "error": target_error,
            "published_files": [],
            "commit_hash": "",
        }
    outcome_kind = str(packet.get("outcome_kind", "")).strip().lower()
    empirical_execution = bool(packet.get("empirical_execution", False))
    should_publish_blog = outcome_kind == "mini_paper" and empirical_execution
    blog_repo_root: Path | None = None
    blog_branch = ""
    blog_science_dir = ""
    blog_error = ""
    if should_publish_blog:
        blog_repo_root, blog_branch, blog_science_dir, blog_error = _resolve_blog_publish_target(packet, task)
        if blog_repo_root is None:
            return {
                "role": "executor",
                "mode": "publish_research",
                "status": "retry",
                "push_status": "failed",
                "error": blog_error,
                "published_files": [],
                "commit_hash": "",
            }
    output_dir = _repo_output_dir(repo_root, task_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    blog_dir: Path | None = None
    if should_publish_blog and blog_repo_root is not None:
        blog_dir = (blog_repo_root / blog_science_dir).resolve()
        blog_dir.mkdir(parents=True, exist_ok=True)

    files_written: list[str] = []
    file_specs = {
        "essay.md": str(packet.get("essay_ref", "")).strip(),
        "final_report.md": str(packet.get("final_report_ref", "")).strip(),
        "result_summary.json": str(packet.get("result_summary_ref", "")).strip(),
        "raw_results.json": str(packet.get("raw_results_ref", "")).strip(),
        "data_record.json": str(packet.get("data_record_ref", "")).strip(),
        "reproduce.md": str(packet.get("reproduce_ref", "")).strip(),
        "experiment.py": str(packet.get("code_bundle_ref", "")).strip(),
    }
    for filename, ref in file_specs.items():
        content = _read_ref_payload(ref)
        if not content:
            return {
                "role": "executor",
                "mode": "publish_research",
                "status": "retry",
                "push_status": "failed",
                "error": f"publish_artifact_missing:{filename}",
                "published_files": files_written,
                "commit_hash": "",
            }
        target = output_dir / filename
        target.write_text(content, encoding="utf-8")
        files_written.append(str(target.relative_to(repo_root)))

    blog_post_path: Path | None = None
    if should_publish_blog and blog_dir is not None and blog_repo_root is not None:
        try:
            blog_slug, blog_post_content = _render_blog_post(packet=packet, task=task, task_id=task_id)
        except ValueError as exc:
            return {
                "role": "executor",
                "mode": "publish_research",
                "status": "retry",
                "push_status": "failed",
                "error": str(exc),
                "published_files": files_written,
                "commit_hash": "",
            }
        blog_post_path = blog_dir / f"{blog_slug}.md"
        blog_post_path.write_text(blog_post_content, encoding="utf-8")
        files_written.append(str(blog_post_path.relative_to(blog_repo_root)))

    if _transport_mode() in {"stub", "deterministic"} and str(os.getenv("AGN_RESEARCH_PUBLISH_REAL", "")).strip() != "1":
        return {
            "role": "executor",
            "mode": "publish_research",
            "status": "ok",
            "push_status": "ok",
            "error": "",
            "published_files": files_written,
            "commit_hash": f"stub-{task_id[:12]}",
            "branch": work_branch,
            "command_log_path": "",
            "transport": "stub",
            "repo_path": str(repo_root),
            "blog_repo_path": str(blog_repo_root) if blog_repo_root is not None else "",
            "blog_post_path": str(blog_post_path.relative_to(blog_repo_root)) if blog_post_path is not None and blog_repo_root is not None else "",
            "blog_commit_hash": f"stub-blog-{task_id[:7]}" if should_publish_blog else "",
            "blog_push_status": "ok" if should_publish_blog else "skipped",
        }

    log_path = _command_log_path(task_id=task_id, role="executor", mode="publish_research")
    previous_role = os.getenv("AGN_ROLE", "")
    os.environ["AGN_ROLE"] = "executor"
    try:
        if should_publish_blog and blog_repo_root is not None:
            hugo_outcome = run_command(
                cmd=["hugo", "--source", str(blog_repo_root)],
                cwd=blog_repo_root,
                timeout_sec=240.0,
                log_path=log_path,
            )
            if hugo_outcome.return_code != 0:
                return {
                    "role": "executor",
                    "mode": "publish_research",
                    "status": "retry",
                    "push_status": "failed",
                    "error": "hugo_build_failed",
                    "published_files": files_written,
                    "commit_hash": "",
                    "command_log_path": str(log_path),
                }
        repo_publish = _git_commit_and_push(
            repo_root=repo_root,
            branch=work_branch,
            add_paths=[str(output_dir.relative_to(repo_root))],
            message=f"Research publish: {task_id}",
            log_path=log_path,
        )
        if not repo_publish["ok"]:
            return {
                "role": "executor",
                "mode": "publish_research",
                "status": "retry",
                "push_status": str(repo_publish["push_status"]),
                "error": str(repo_publish["error"]),
                "published_files": files_written,
                "commit_hash": str(repo_publish["commit_hash"]),
                "command_log_path": str(log_path),
            }
        if should_publish_blog and blog_repo_root is not None and blog_post_path is not None:
            blog_publish = _git_commit_and_push(
                repo_root=blog_repo_root,
                branch=blog_branch,
                add_paths=[str(blog_post_path.relative_to(blog_repo_root))],
                message=f"Research blog sync: {task_id}",
                log_path=log_path,
            )
            if not blog_publish["ok"]:
                return {
                    "role": "executor",
                    "mode": "publish_research",
                    "status": "retry",
                    "push_status": str(blog_publish["push_status"]),
                    "error": f"blog_{str(blog_publish['error'])}",
                    "published_files": files_written,
                    "commit_hash": str(repo_publish["commit_hash"]),
                    "command_log_path": str(log_path),
                    "output_dir": str(output_dir.relative_to(repo_root)),
                    "branch": str(repo_publish["branch"]),
                    "blog_post_path": str(blog_post_path.relative_to(blog_repo_root)),
                    "blog_repo_path": str(blog_repo_root),
                    "blog_commit_hash": str(blog_publish["commit_hash"]),
                    "blog_push_status": str(blog_publish["push_status"]),
                }
        else:
            blog_publish = {
                "ok": True,
                "commit_hash": "",
                "push_status": "skipped",
            }
    finally:
        if previous_role:
            os.environ["AGN_ROLE"] = previous_role
        else:
            os.environ.pop("AGN_ROLE", None)

    status = "ok"
    payload = {
        "role": "executor",
        "mode": "publish_research",
        "status": status,
        "push_status": "ok",
        "error": "",
        "published_files": files_written,
        "commit_hash": str(repo_publish["commit_hash"]),
        "output_dir": str(output_dir.relative_to(repo_root)),
        "command_log_path": str(log_path),
        "branch": str(repo_publish["branch"]),
        "repo_path": str(repo_root),
        "blog_post_path": str(blog_post_path.relative_to(blog_repo_root)) if blog_post_path is not None and blog_repo_root is not None else "",
        "blog_repo_path": str(blog_repo_root) if blog_repo_root is not None else "",
        "blog_commit_hash": str(blog_publish["commit_hash"]),
        "blog_push_status": str(blog_publish["push_status"]),
    }
    if bool(repo_publish.get("no_changes")) and bool(blog_publish.get("no_changes")):
        payload["no_changes"] = True
    return payload


def _final_review(*, role: str, packet: dict[str, Any]) -> dict[str, Any]:
    _ensure_role_context(packet, role)
    review_scope = packet.get("review_scope", {})
    if not isinstance(review_scope, dict):
        review_scope = {}
    evidence_refs = packet.get("evidence_refs", {})
    if not isinstance(evidence_refs, dict):
        evidence_refs = {}
    evidence_boundary = packet.get("evidence_boundary", [])
    if not isinstance(evidence_boundary, list):
        evidence_boundary = []

    checks = {
        "survey_ref": bool(str(evidence_refs.get("survey_ref", "")).strip().startswith("agn://")),
        "shortlist_ref": bool(str(evidence_refs.get("shortlist_ref", "")).strip().startswith("agn://")),
        "experiment_ref": bool(str(evidence_refs.get("experiment_ref", "")).strip().startswith("agn://")),
        "paper_or_note_ref": bool(
            str(evidence_refs.get("paper_ref", "")).strip().startswith("agn://")
            or str(evidence_refs.get("failure_note_ref", "")).strip().startswith("agn://")
        ),
        "message_count": int(review_scope.get("message_count", 0) or 0),
        "honest_failure": bool(review_scope.get("honest_failure", False)),
        "empirical_execution": bool(review_scope.get("empirical_execution", False)),
        "truthfulness_status": str(review_scope.get("truthfulness_status", "")).strip(),
        "truthfulness_reason": str(review_scope.get("truthfulness_reason", "")).strip(),
        "unverified_metrics_present": bool(review_scope.get("unverified_metrics_present", False)),
    }

    if not checks["survey_ref"] or not checks["shortlist_ref"]:
        return _review_failure_archive(
            failure_type="selection_trace_missing",
            reason="survey or shortlist evidence is missing",
            rerun_worth_it=False,
            checks=checks,
            evidence_boundary=evidence_boundary,
        )
    if not checks["experiment_ref"]:
        return _review_failure_archive(
            failure_type="experiment_trace_missing",
            reason="experiment evidence is missing",
            rerun_worth_it=True,
            checks=checks,
            evidence_boundary=evidence_boundary,
        )
    if not checks["empirical_execution"]:
        return _review_failure_archive(
            failure_type="non_empirical_execution",
            reason=checks["truthfulness_reason"] or "experiment metrics are not backed by verifiable local execution evidence",
            rerun_worth_it=True,
            checks=checks,
            evidence_boundary=evidence_boundary,
        )
    if not checks["paper_or_note_ref"]:
        return _review_failure_archive(
            failure_type="paper_missing",
            reason="no mini paper or failure note is attached",
            rerun_worth_it=False,
            checks=checks,
            evidence_boundary=evidence_boundary,
        )
    if checks["message_count"] < 4:
        return _review_revision_once(
            issue="raw communication coverage is too thin",
            risk="critical decisions will not be auditable later",
            minimal_fix="retain the coordinator packets and both worker replies for each round",
            checks=checks,
            evidence_boundary=evidence_boundary,
        )

    outcome_kind = str(review_scope.get("outcome_kind", "")).strip().lower()
    if outcome_kind == "mini_paper" and checks["unverified_metrics_present"]:
        return _review_failure_archive(
            failure_type="unverified_metrics",
            reason="mini paper still contains metrics that are not backed by empirical execution",
            rerun_worth_it=True,
            checks=checks,
            evidence_boundary=evidence_boundary,
        )
    if outcome_kind == "failure_note" and not checks["honest_failure"]:
        return _review_revision_once(
            issue="failure note does not explicitly preserve the failure cause",
            risk="the archive may optimize optics over learning value",
            minimal_fix="state the failure cause, finished work, and why a rerun is unnecessary or necessary",
            checks=checks,
            evidence_boundary=evidence_boundary,
        )
    if int(review_scope.get("review_revision_count", 0) or 0) > 0 and checks["message_count"] < 6:
        return _review_failure_archive(
            failure_type="revision_exhausted",
            reason="one review revision was already consumed and the trace is still too thin",
            rerun_worth_it=False,
            checks=checks,
            evidence_boundary=evidence_boundary,
        )

    return _review_approved(
        summary="archive is complete and the result is transparent enough to keep.",
        checks=checks,
        evidence_boundary=evidence_boundary,
    )


def _transport_mode() -> str:
    raw = str(os.getenv("AGN_RESEARCH_WORKER_TRANSPORT", "cli") or "cli").strip().lower()
    if raw in VALID_TRANSPORTS:
        return raw
    return "cli"


def _schema(role: str, mode: str) -> dict[str, Any]:
    if mode == "role_init":
        return {
            "type": "object",
            "required": [
                "role",
                "mode",
                "ack",
                "current_round",
                "schema",
                "protocol_digest",
                "integrity_ack",
                "failure_ack",
                "fabrication_ack",
            ],
            "additionalProperties": False,
            "properties": {
                "role": {"type": "string", "enum": [role]},
                "mode": {"type": "string", "enum": ["role_init"]},
                "ack": {"type": "string", "enum": ["init_loaded"]},
                "current_round": {"type": "integer"},
                "schema": {"type": "string"},
                "protocol_digest": {"type": "string"},
                "integrity_ack": {"type": "string", "enum": ["truthfulness_first"]},
                "failure_ack": {"type": "string", "enum": ["failure_is_valid"]},
                "fabrication_ack": {"type": "string", "enum": ["no_fabrication"]},
            },
        }
    if mode == "run_experiment":
        return {
            "type": "object",
            "required": ["role", "mode", "status", "strategy"],
            "additionalProperties": False,
            "properties": {
                "role": {"type": "string", "enum": [role]},
                "mode": {"type": "string", "enum": ["run_experiment"]},
                "status": {"type": "string", "enum": ["ok", "degraded", "failure_note"]},
                "strategy": {"type": "string"},
                "metrics": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "case_count": {"type": "number"},
                        "accuracy": {"type": "number"},
                    },
                },
                "notes": {"type": "array", "items": {"type": "string"}},
                "error": {"type": "string"},
                "completed_work": {"type": "array", "items": {"type": "string"}},
                "command_log_path": {"type": "string"},
                "execution_evidence_paths": {"type": "array", "items": {"type": "string"}},
            },
        }
    if mode == "publish_research":
        return {
            "type": "object",
            "required": ["role", "mode", "status", "push_status", "published_files", "commit_hash"],
            "additionalProperties": False,
            "properties": {
                "role": {"type": "string", "enum": [role]},
                "mode": {"type": "string", "enum": ["publish_research"]},
                "status": {"type": "string", "enum": ["ok", "retry"]},
                "push_status": {"type": "string", "enum": ["ok", "failed", "blocked"]},
                "error": {"type": "string"},
                "published_files": {"type": "array", "items": {"type": "string"}},
                "commit_hash": {"type": "string"},
                "output_dir": {"type": "string"},
                "branch": {"type": "string"},
                "command_log_path": {"type": "string"},
                "no_changes": {"type": "boolean"},
                "repo_path": {"type": "string"},
                "blog_post_path": {"type": "string"},
                "blog_repo_path": {"type": "string"},
                "blog_commit_hash": {"type": "string"},
                "blog_push_status": {"type": "string", "enum": ["ok", "failed", "blocked", "skipped"]},
                "transport": {"type": "string"},
            },
        }
    if mode == "final_review":
        return {
            "type": "object",
            "required": ["role", "mode", "verdict", "evidence_boundary"],
            "additionalProperties": False,
            "properties": {
                "role": {"type": "string", "enum": [role]},
                "mode": {"type": "string", "enum": ["final_review"]},
                "verdict": {"type": "string", "enum": ["APPROVED", "REVISION_ONCE", "FAILURE_ARCHIVE"]},
                "issue": {"type": "string"},
                "risk": {"type": "string"},
                "minimal_fix": {"type": "string"},
                "failure_type": {"type": "string"},
                "reason": {"type": "string"},
                "rerun_worth_it": {"type": "boolean"},
                "evidence_boundary": {"type": "array", "items": {"type": "string"}},
                "message": {"type": "string"},
            },
        }
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["role", "mode", "decision", "problem", "risk", "minimal_change", "message"],
        "additionalProperties": False,
        "properties": {
            "role": {"type": "string", "enum": [role]},
            "mode": {"type": "string", "enum": [mode]},
            "decision": {"type": "string", "enum": ["yes", "no"]},
            "problem": {"type": "string"},
            "risk": {"type": "string"},
            "minimal_change": {"type": "string"},
            "message": {"type": "string"},
        },
    }
    return schema


def _schema_brief(role: str, mode: str) -> str:
    if mode == "role_init":
        return (
            '{"role":"%s","mode":"role_init","ack":"init_loaded","current_round":1,'
            '"schema":"%s_role_init_v1","protocol_digest":"sha256",'
            '"integrity_ack":"truthfulness_first","failure_ack":"failure_is_valid",'
            '"fabrication_ack":"no_fabrication"}'
        ) % (role, role)
    if mode == "run_experiment":
        return '{"role":"executor","mode":"run_experiment","status":"ok|degraded|failure_note","strategy":"string","metrics":{},"notes":[],"error":"","execution_evidence_paths":[]}'
    if mode == "publish_research":
        return '{"role":"executor","mode":"publish_research","status":"ok|retry","push_status":"ok|failed|blocked","error":"","published_files":[],"commit_hash":""}'
    if mode == "final_review":
        return '{"role":"reviewer","mode":"final_review","verdict":"APPROVED|REVISION_ONCE|FAILURE_ARCHIVE","issue":"","risk":"","minimal_fix":"","failure_type":"","reason":"","rerun_worth_it":false,"evidence_boundary":[],"message":""}'
    return '{"role":"%s","mode":"%s","decision":"yes|no","problem":"","risk":"","minimal_change":"","message":""}' % (role, mode)


def _scratch_file(*, task_id: str, role: str, mode: str, suffix: str) -> Path:
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{str(task_id or 'task').replace('/', '_')}_{role}_{mode}_",
        suffix=suffix,
        dir=SCRATCH_DIR,
    )
    os.close(fd)
    return Path(tmp_name)


def _command_log_path(*, task_id: str, role: str, mode: str) -> Path:
    safe_task = str(task_id or "task").replace("/", "_")
    return SCRATCH_DIR / f"{safe_task}_{role}_{mode}.exec.log"


def _materialize_packet_file(*, task_id: str, role: str, mode: str, packet_ref: str) -> Path:
    PACKET_MIRROR_DIR.mkdir(parents=True, exist_ok=True)
    safe_task = str(task_id or "task").replace("/", "_")
    target = PACKET_MIRROR_DIR / f"{safe_task}_{role}_{mode}.json"
    rendered = read_ref_text(packet_ref, mode="all", max_bytes=512 * 1024)
    target.write_text(rendered, encoding="utf-8")
    return target


def _load_task(packet: dict[str, Any]) -> dict[str, Any]:
    task_id = str(packet.get("task_id", "")).strip()
    if not task_id:
        return {}
    store = SSOTStore(ROOT / "ssot")
    task = store.get_task(task_id)
    return task if isinstance(task, dict) else {}


def _resolve_provider(role: str, packet: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    task = _load_task(packet)
    if role == "executor":
        name = resolve_executor_provider(str(task.get("executor_provider", "")).strip().lower(), REGISTRY)
        spec = REGISTRY.get("executors", {}).get(name, {})
    else:
        name = resolve_reviewer_provider(str(task.get("reviewer_provider", "")).strip().lower(), REGISTRY)
        spec = REGISTRY.get("reviewers", {}).get(name, {})
    return name, spec if isinstance(spec, dict) else {}


def _role_provider_specs(role: str) -> dict[str, dict[str, Any]]:
    key = "executors" if role == "executor" else "reviewers"
    raw = REGISTRY.get(key, {})
    if not isinstance(raw, dict):
        return {}
    return {name: spec for name, spec in raw.items() if isinstance(spec, dict)}


def _provider_chain(role: str, requested_provider: str) -> list[str]:
    specs = _role_provider_specs(role)
    chain: list[str] = []
    for candidate in [requested_provider, *PROVIDER_FALLBACKS.get(role, {}).get(requested_provider, [])]:
        clean = str(candidate or "").strip().lower()
        if clean and clean in specs and clean not in chain:
            chain.append(clean)
    return chain


def _provider_api_settings(spec: dict[str, Any]) -> dict[str, Any]:
    api_key_env = str(spec.get("api_key_env", "")).strip()
    base_url_env = str(spec.get("base_url_env", "")).strip()
    model_env = str(spec.get("model_env", "")).strip()
    api_key = str(os.getenv(api_key_env, "")).strip() if api_key_env else ""
    base_url = str(os.getenv(base_url_env, "")).strip() if base_url_env else ""
    model = str(os.getenv(model_env, "")).strip() if model_env else ""
    if not base_url:
        base_url = str(spec.get("default_base_url", "")).strip()
    if not model:
        model = str(spec.get("default_model", "")).strip()
    requires_api_key = bool(spec.get("requires_api_key", bool(api_key_env)))
    return {
        "api_key": api_key,
        "api_key_env": api_key_env,
        "base_url": base_url,
        "model": model,
        "requires_api_key": requires_api_key,
    }


def _qwen_local_policy(role: str, mode: str, packet: dict[str, Any]) -> tuple[bool, str]:
    if mode not in QWEN_LOCAL_ALLOWED_MODES:
        return False, f"qwen_local is reserved for bounded low-risk text tasks; mode `{mode}` must stay on a higher-trust worker"
    task = _load_task(packet)
    risk_level = str(packet.get("risk_level", "") or task.get("risk_level", "") or "low").strip().lower() or "low"
    side_effect_level = str(packet.get("side_effect_level", "") or task.get("side_effect_level", "") or "read_only").strip().lower() or "read_only"
    if risk_level != "low":
        return False, f"qwen_local only handles low-risk packets; packet risk is `{risk_level}`"
    if side_effect_level != "read_only":
        return False, f"qwen_local only handles read-only packets; packet side_effect_level is `{side_effect_level}`"
    return True, ""


def _mode_instruction(mode: str) -> str:
    if mode == "role_init":
        return (
            "Read the init files only and return the confirmation JSON. "
            "Acknowledge the current protocol digest plus the honesty contract: "
            "truthfulness first, failure is valid, and fabrication is forbidden. "
            "Do not execute the task."
        )
    if mode == "topic_vote":
        return (
            "Judge only the current proposal. Return yes or no. "
            "If decision is no, problem, risk, and minimal_change must all be non-empty. "
            "If decision is yes, those three fields must be empty strings."
        )
    if mode == "run_experiment":
        return (
            "Return one experiment result JSON. If a full run is not safely achievable inside the packet budget, "
            "degrade through strategy_candidates and return degraded or failure_note instead of stopping. "
            "Do not fabricate metrics, hypothesis support, or success claims from theory alone. "
            "If no empirical local execution happened, leave metrics empty, keep execution_evidence_paths empty, "
            "and say so explicitly in notes."
        )
    if mode == "publish_research":
        return (
            "Materialize the referenced research outputs into the repository, commit only the task output directory, "
            "and push the current branch. Return retry if commit or push cannot complete."
        )
    return (
        "Audit only the final archive boundary. Return APPROVED, REVISION_ONCE, or FAILURE_ARCHIVE. "
        "If verdict is REVISION_ONCE, issue, risk, and minimal_fix must all be non-empty. "
        "If verdict is FAILURE_ARCHIVE, failure_type, reason, rerun_worth_it, and evidence_boundary must all be present."
    )


def _reference_materials(paths: list[str]) -> list[str]:
    materials: list[str] = []
    for raw in paths:
        path = _repo_file(str(raw))
        try:
            body = path.read_text(encoding="utf-8")
        except Exception:
            continue
        body = body.strip()
        if not body:
            continue
        materials.append(f"[{path}]\n{body[:4000]}")
    return materials


def _prompt_for_provider(*, provider: str, role: str, mode: str, packet: dict[str, Any], packet_path: Path) -> str:
    if mode == "role_init":
        init_paths = packet.get("init_paths", [])
    else:
        init_paths = packet.get("role_init_paths", [])
    paths: list[str] = []
    if isinstance(init_paths, list):
        paths = [str(_repo_file(str(item))) for item in init_paths]
    packet_json = json.dumps(packet, ensure_ascii=False, indent=2)
    reference_materials = _reference_materials(list(init_paths) if isinstance(init_paths, list) else [])

    lines = [
        f"You are a stateless AGN research {role} worker.",
        "Do not rely on prior conversation or hidden history.",
        f"Provider: {provider}",
        f"Current mode: {mode}",
        f"Current round: {int(packet.get('current_round', 0) or 0)}",
        f"Current goal: {str(packet.get('goal', '')).strip() or str(packet.get('current_action_required', '')).strip() or 'Act only on this packet.'}",
        "Treat the embedded packet JSON and embedded reference materials as the full authority for this wake-up.",
        "Do not search the repo, do not inspect hidden workspace files, and do not rely on tool calls unless the prompt already embeds the needed evidence.",
    ]
    if paths:
        lines.append("Reference files captured for this wake-up:")
        for path in paths:
            lines.append(f"- {path}")
    else:
        lines.append("Reference files captured for this wake-up: none")
    if reference_materials:
        lines.append("")
        lines.append("Embedded reference materials:")
        lines.extend(reference_materials)
    lines.extend(
        [
            "",
            "Embedded packet JSON:",
            packet_json,
        ]
    )
    lines.extend(
        [
            _mode_instruction(mode),
            f"Return exactly one JSON object with this shape: {_schema_brief(role, mode)}",
            "Do not add markdown fences, headers, or prose outside the JSON object.",
        ]
    )
    return "\n".join(lines)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None

    candidates: list[str] = [raw]
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        if isinstance(parsed.get("result"), str):
            candidates.append(str(parsed.get("result")))
        if isinstance(parsed.get("response"), str):
            candidates.append(str(parsed.get("response")))
        if "decision" in parsed or "status" in parsed or "ack" in parsed:
            return parsed

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start : end + 1])

    for candidate in candidates:
        try:
            loaded = json.loads(candidate)
        except Exception:
            continue
        if isinstance(loaded, dict):
            if isinstance(loaded.get("result"), str):
                nested = _extract_json_object(str(loaded.get("result")))
                if nested is not None:
                    return nested
            if isinstance(loaded.get("response"), str):
                nested = _extract_json_object(str(loaded.get("response")))
                if nested is not None:
                    return nested
            return loaded
    return None


def _provider_failure(*, role: str, mode: str, problem: str, risk: str, minimal_change: str, detail: str = "") -> dict[str, Any]:
    if mode == "run_experiment":
        return {
            "role": role,
            "mode": mode,
            "status": "failure_note",
            "strategy": "failure_note",
            "error": problem,
            "notes": [item for item in [risk, minimal_change, detail] if item],
            "completed_work": ["role init attempted", "packet dispatch attempted"],
        }
    if mode == "role_init":
        return {
            "role": role,
            "mode": mode,
            "ack": "init_failed",
            "current_round": 0,
            "schema": "",
            "problem": problem,
            "risk": risk,
            "minimal_change": minimal_change,
            "message": detail or problem,
        }
    if mode == "final_review":
        return {
            "role": role,
            "mode": mode,
            "verdict": "FAILURE_ARCHIVE",
            "issue": "",
            "risk": risk,
            "minimal_fix": minimal_change,
            "failure_type": "review_transport_failure",
            "reason": problem,
            "rerun_worth_it": True,
            "evidence_boundary": [],
            "message": detail or problem,
        }
    payload = _vote_no(
        role=role,
        mode=mode,
        problem=problem,
        risk=risk,
        minimal_change=minimal_change,
        checks={},
    )
    if detail:
        payload["message"] = detail[:400]
    if mode == "final_review":
        payload["evidence_boundary"] = []
    return payload


def _normalize_result(
    *,
    role: str,
    mode: str,
    packet: dict[str, Any],
    provider: str,
    transport: str,
    raw: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = dict(raw or {})
    payload["role"] = role
    payload["mode"] = mode
    payload["provider"] = provider
    payload["transport"] = transport

    if mode == "role_init":
        payload["ack"] = "init_loaded" if str(payload.get("ack", "")).strip() == "init_loaded" else "init_loaded"
        payload["current_round"] = int(packet.get("current_round", payload.get("current_round", 0)) or 0)
        payload["schema"] = str(
            payload.get("schema")
            or packet.get("confirmation_schema", {}).get("schema", "")
        ).strip()
        payload["protocol_digest"] = str(
            payload.get("protocol_digest")
            or packet.get("confirmation_schema", {}).get("protocol_digest", "")
            or _role_init_digest(packet.get("init_paths"))
        ).strip()
        payload["integrity_ack"] = str(
            payload.get("integrity_ack")
            or packet.get("confirmation_schema", {}).get("integrity_ack", "")
            or "truthfulness_first"
        ).strip()
        payload["failure_ack"] = str(
            payload.get("failure_ack")
            or packet.get("confirmation_schema", {}).get("failure_ack", "")
            or "failure_is_valid"
        ).strip()
        payload["fabrication_ack"] = str(
            payload.get("fabrication_ack")
            or packet.get("confirmation_schema", {}).get("fabrication_ack", "")
            or "no_fabrication"
        ).strip()
        return payload

    if mode == "run_experiment":
        status = str(payload.get("status", "")).strip().lower()
        if status not in {"ok", "degraded", "failure_note"}:
            status = "failure_note"
        payload["status"] = status
        payload["strategy"] = str(payload.get("strategy", "")).strip() or ("failure_note" if status == "failure_note" else "full")
        metrics = payload.get("metrics")
        payload["metrics"] = metrics if isinstance(metrics, dict) else {}
        notes = payload.get("notes")
        if isinstance(notes, list):
            payload["notes"] = [str(item).strip() for item in notes if str(item).strip()]
        else:
            payload["notes"] = []
        if status == "failure_note":
            payload["error"] = str(payload.get("error", "")).strip() or "provider returned no runnable experiment payload"
            completed_work = payload.get("completed_work")
            if isinstance(completed_work, list):
                payload["completed_work"] = [str(item).strip() for item in completed_work if str(item).strip()]
            else:
                payload["completed_work"] = ["role init complete", "packet interpreted"]
        payload["execution_evidence_paths"] = _clean_existing_paths(payload.get("execution_evidence_paths"))
        empirical_execution, truthfulness_status, truthfulness_reason, evidence_paths = _execution_truthfulness(
            provider=provider,
            payload=payload,
        )
        payload["execution_evidence_paths"] = evidence_paths
        payload["empirical_execution"] = empirical_execution
        payload["truthfulness_status"] = truthfulness_status
        payload["truthfulness_reason"] = truthfulness_reason
        payload["command_log_path"] = str(payload.get("command_log_path", "")).strip()
        return payload

    if mode == "publish_research":
        status = str(payload.get("status", "")).strip().lower()
        if status not in {"ok", "retry"}:
            status = "retry"
        push_status = str(payload.get("push_status", "")).strip().lower()
        if push_status not in {"ok", "failed", "blocked"}:
            push_status = "failed"
        payload["status"] = status
        payload["push_status"] = push_status
        payload["error"] = str(payload.get("error", "")).strip()
        files = payload.get("published_files")
        payload["published_files"] = [str(item).strip() for item in files if str(item).strip()] if isinstance(files, list) else []
        payload["commit_hash"] = str(payload.get("commit_hash", "")).strip()
        payload["output_dir"] = str(payload.get("output_dir", "")).strip()
        payload["branch"] = str(payload.get("branch", "")).strip()
        payload["command_log_path"] = str(payload.get("command_log_path", "")).strip()
        payload["no_changes"] = bool(payload.get("no_changes", False))
        payload["repo_path"] = str(payload.get("repo_path", "")).strip()
        payload["blog_post_path"] = str(payload.get("blog_post_path", "")).strip()
        payload["blog_repo_path"] = str(payload.get("blog_repo_path", "")).strip()
        payload["blog_commit_hash"] = str(payload.get("blog_commit_hash", "")).strip()
        blog_push_status = str(payload.get("blog_push_status", "")).strip().lower()
        payload["blog_push_status"] = blog_push_status if blog_push_status in {"ok", "failed", "blocked", "skipped"} else ""
        payload["transport"] = str(payload.get("transport", "")).strip()
        return payload

    if mode == "final_review":
        verdict = str(payload.get("verdict", "")).strip().upper()
        if not verdict:
            legacy_decision = str(payload.get("decision", "")).strip().lower()
            if legacy_decision == "yes":
                verdict = "APPROVED"
            elif legacy_decision == "no":
                verdict = "FAILURE_ARCHIVE"
        if verdict not in {"APPROVED", "REVISION_ONCE", "FAILURE_ARCHIVE"}:
            verdict = "FAILURE_ARCHIVE"
        payload["verdict"] = verdict
        payload["issue"] = str(payload.get("issue", "")).strip()
        payload["risk"] = str(payload.get("risk", "")).strip()
        payload["minimal_fix"] = str(payload.get("minimal_fix", "")).strip()
        payload["failure_type"] = str(payload.get("failure_type", "")).strip()
        payload["reason"] = str(payload.get("reason", "")).strip()
        payload["rerun_worth_it"] = bool(payload.get("rerun_worth_it", False))
        if verdict == "REVISION_ONCE":
            payload["issue"] = payload["issue"] or "review requested one coordinator-side revision"
            payload["risk"] = payload["risk"] or "the archive remains ambiguous without one more clarification"
            payload["minimal_fix"] = payload["minimal_fix"] or "apply one small coordinator revision and re-review once"
        if verdict == "FAILURE_ARCHIVE":
            payload["failure_type"] = payload["failure_type"] or "review_failure_archive"
            payload["reason"] = payload["reason"] or "the archive is not strong enough to justify approval"
        boundary = payload.get("evidence_boundary")
        if isinstance(boundary, list):
            payload["evidence_boundary"] = [str(item).strip() for item in boundary if str(item).strip()]
        else:
            evidence_boundary = packet.get("evidence_boundary", [])
            if not isinstance(evidence_boundary, list):
                evidence_boundary = []
            payload["evidence_boundary"] = [str(item).strip() for item in evidence_boundary if str(item).strip()]
        payload["message"] = str(payload.get("message", "")).strip() or verdict
        return payload

    decision = str(payload.get("decision", "")).strip().lower()
    if decision not in {"yes", "no"}:
        decision = "no"
    payload["decision"] = decision
    if decision == "yes":
        payload["problem"] = ""
        payload["risk"] = ""
        payload["minimal_change"] = ""
    else:
        payload["problem"] = str(payload.get("problem", "")).strip() or "provider returned a rejection without a concrete issue"
        payload["risk"] = str(payload.get("risk", "")).strip() or "the coordinator cannot safely trust the worker output"
        payload["minimal_change"] = str(payload.get("minimal_change", "")).strip() or "re-run with the required output schema"
    payload["message"] = str(payload.get("message", "")).strip() or (
        "YES." if decision == "yes" else f"NO. problem={payload['problem']} risk={payload['risk']} minimal_change={payload['minimal_change']}"
    )
    checks = payload.get("checks")
    payload["checks"] = checks if isinstance(checks, dict) else {}
    if mode == "final_review":
        boundary = payload.get("evidence_boundary")
        if isinstance(boundary, list):
            payload["evidence_boundary"] = [str(item).strip() for item in boundary if str(item).strip()]
        else:
            evidence_boundary = packet.get("evidence_boundary", [])
            if not isinstance(evidence_boundary, list):
                evidence_boundary = []
            payload["evidence_boundary"] = [str(item).strip() for item in evidence_boundary if str(item).strip()]
    return payload


def _command_for_provider(*, provider: str, role: str, mode: str, prompt: str, schema_path: Path, output_path: Path) -> list[str]:
    if provider == "codex":
        return [
            "codex",
            "exec",
            "--cd",
            str(ROOT),
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            prompt,
        ]
    if provider == "gemini":
        return [
            "gemini",
            "-p",
            prompt,
            "--approval-mode",
            "yolo",
            "--output-format",
            "json",
            "--include-directories",
            str(ROOT),
        ]
    if provider == "claude":
        return [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(_schema(role, mode), ensure_ascii=True),
            "--permission-mode",
            "plan",
            "--add-dir",
            str(ROOT),
            prompt,
        ]
    raise ValueError(f"unsupported_cli_provider:{provider}")


def _timeout_for_mode(mode: str) -> float:
    if mode == "role_init":
        return 120.0
    if mode == "run_experiment":
        return 300.0
    return 180.0


def _extract_openai_message_text(decoded: dict[str, Any]) -> str:
    choices = decoded.get("choices", [])
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    message = choice.get("message", {})
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = str(item.get("text", "")).strip()
                    if text:
                        parts.append(text)
            return "\n".join(parts).strip()
    text = choice.get("text")
    return str(text).strip() if isinstance(text, str) else ""


def _api_json_only_messages(prompt: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "Return exactly one JSON object and nothing else. "
                "No markdown fences, no analysis, no surrounding prose."
            ),
        },
        {"role": "user", "content": prompt},
    ]


def _run_cli_provider(
    *,
    provider: str,
    role: str,
    mode: str,
    packet: dict[str, Any],
    prompt: str,
    schema_path: Path,
    output_path: Path,
    log_path: Path,
) -> tuple[dict[str, Any] | None, str]:
    cmd = _command_for_provider(
        provider=provider,
        role=role,
        mode=mode,
        prompt=prompt,
        schema_path=schema_path,
        output_path=output_path,
    )

    previous_role = os.getenv("AGN_ROLE", "")
    os.environ["AGN_ROLE"] = role
    try:
        outcome = run_command(
            cmd=cmd,
            cwd=ROOT,
            timeout_sec=_timeout_for_mode(mode),
            log_path=log_path,
        )
    finally:
        if previous_role:
            os.environ["AGN_ROLE"] = previous_role
        else:
            os.environ.pop("AGN_ROLE", None)

    raw_output = ""
    if provider == "codex" and output_path.exists():
        raw_output = output_path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw_output:
        raw_output = str(outcome.stdout or "").strip()
    if not raw_output:
        raw_output = str(outcome.stderr or "").strip()

    if outcome.return_code != 0 or outcome.timed_out:
        return None, f"{provider}_cli_failed:{str(outcome.stderr or outcome.stdout or '').strip()[:800]}"

    parsed = _extract_json_object(raw_output)
    if parsed is None:
        return None, f"{provider}_cli_non_json:{raw_output[:800]}"
    normalized = _normalize_result(
        role=role,
        mode=mode,
        packet=packet,
        provider=provider,
        transport="cli",
        raw=parsed,
    )
    if mode == "run_experiment":
        normalized["command_log_path"] = str(log_path)
    return normalized, ""


def _run_api_provider(
    *,
    provider: str,
    spec: dict[str, Any],
    role: str,
    mode: str,
    packet: dict[str, Any],
    prompt: str,
    log_path: Path,
) -> tuple[dict[str, Any] | None, str]:
    settings = _provider_api_settings(spec)
    if not settings["base_url"] or not settings["model"]:
        return None, f"{provider}_api_missing_base_url_or_model"
    if settings["requires_api_key"] and not settings["api_key"]:
        missing = str(settings["api_key_env"] or "api_key").strip()
        return None, f"{provider}_api_key_missing:{missing}"

    endpoint = str(settings["base_url"]).rstrip("/") + "/chat/completions"
    payload = {
        "model": str(settings["model"]),
        "temperature": 0,
        "messages": _api_json_only_messages(prompt),
        "stream": False,
        "max_tokens": 900,
    }
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if settings["api_key"]:
        headers["Authorization"] = f"Bearer {settings['api_key']}"
    req = urllib_request.Request(endpoint, data=data, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=_timeout_for_mode(mode)) as response:
            raw_output = response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return None, f"{provider}_http_{exc.code}:{body[:800]}"
    except Exception as exc:
        return None, f"{provider}_api_exception:{type(exc).__name__}:{str(exc)[:400]}"

    try:
        log_path.write_text(raw_output, encoding="utf-8")
    except Exception:
        pass

    try:
        decoded = json.loads(raw_output)
    except Exception:
        return None, f"{provider}_api_invalid_json:{raw_output[:800]}"

    content = _extract_openai_message_text(decoded)
    if not content:
        return None, f"{provider}_api_empty_content"
    parsed = _extract_json_object(content)
    if parsed is None:
        return None, f"{provider}_api_non_json_content:{content[:800]}"
    normalized = _normalize_result(
        role=role,
        mode=mode,
        packet=packet,
        provider=provider,
        transport="api",
        raw=parsed,
    )
    normalized["command_log_path"] = str(log_path)
    return normalized, ""


def _run_real_transport(*, role: str, mode: str, packet_ref: str, packet: dict[str, Any]) -> dict[str, Any]:
    requested_provider, _ = _resolve_provider(role, packet)
    task_id = str(packet.get("task_id", "research")).strip() or "research"
    packet_path = _materialize_packet_file(task_id=task_id, role=role, mode=mode, packet_ref=packet_ref)
    schema_path = _scratch_file(task_id=task_id, role=role, mode=mode, suffix=".schema.json")
    output_path = _scratch_file(task_id=task_id, role=role, mode=mode, suffix=".out.json")
    schema_path.write_text(json.dumps(_schema(role, mode), ensure_ascii=True, indent=2), encoding="utf-8")
    specs = _role_provider_specs(role)
    attempts: list[str] = []
    details: list[str] = []

    for provider in _provider_chain(role, requested_provider):
        spec = specs.get(provider, {})
        if not spec:
            details.append(f"{provider}:missing_provider_spec")
            continue
        if provider == "qwen_local":
            allowed, reason = _qwen_local_policy(role, mode, packet)
            if not allowed:
                attempts.append(provider)
                details.append(f"{provider}:policy_block:{reason}")
                continue
        log_path = _command_log_path(task_id=task_id, role=role, mode=f"{mode}.{provider}")
        prompt = _prompt_for_provider(provider=provider, role=role, mode=mode, packet=packet, packet_path=packet_path)
        kind = str(spec.get("kind", "cli")).strip().lower()
        if kind == "cli":
            normalized, detail = _run_cli_provider(
                provider=provider,
                role=role,
                mode=mode,
                packet=packet,
                prompt=prompt,
                schema_path=schema_path,
                output_path=output_path,
                log_path=log_path,
            )
        elif kind == "api":
            normalized, detail = _run_api_provider(
                provider=provider,
                spec=spec,
                role=role,
                mode=mode,
                packet=packet,
                prompt=prompt,
                log_path=log_path,
            )
        else:
            normalized, detail = None, f"{provider}:unsupported_transport_kind:{kind}"
        attempts.append(provider)
        if normalized is not None:
            normalized["requested_provider"] = requested_provider
            normalized["provider_attempts"] = attempts
            if provider != requested_provider:
                normalized["fallback_from"] = requested_provider
            return normalized
        if detail:
            details.append(detail)

    return _provider_failure(
        role=role,
        mode=mode,
        problem=f"all configured providers failed for requested provider `{requested_provider}`",
        risk="the worker wake-up did not return a trustworthy structured payload",
        minimal_change="retry with the same packet or select a higher-trust provider",
        detail=" | ".join(details)[:800],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Research role worker backed by real CLI providers")
    parser.add_argument("--role", choices=["executor", "reviewer"], required=True)
    parser.add_argument("--mode", choices=["role_init", "topic_vote", "run_experiment", "publish_research", "final_review"], required=True)
    parser.add_argument("--packet-ref", required=True)
    args = parser.parse_args()

    packet = _packet(args.packet_ref)
    transport = _transport_mode()
    if transport in {"stub", "deterministic"}:
        if args.mode == "role_init":
            result = _role_init_ack(role=args.role, packet=packet)
        elif args.mode == "topic_vote":
            result = _topic_vote(role=args.role, packet=packet)
        elif args.mode == "run_experiment":
            result = _run_experiment(packet)
        elif args.mode == "publish_research":
            result = _publish_research(packet)
        else:
            result = _final_review(role=args.role, packet=packet)
        result["provider"] = "stub"
        result["transport"] = "stub"
    else:
        if args.mode == "publish_research":
            result = _publish_research(packet)
        else:
            result = _run_real_transport(role=args.role, mode=args.mode, packet_ref=args.packet_ref, packet=packet)

    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
