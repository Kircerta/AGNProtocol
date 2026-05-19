#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn_api.ssot_store import SSOTStore
from scripts.pointer_protocol import write_json_artifact
import scripts.research_flow as research_flow

REPORT_PATH = ROOT / "reports" / "qwen_local_provider_validation.json"


def _save_protocol_task(*, task_id: str, reviewer_provider: str = "qwen_local") -> dict[str, Any]:
    task = {
        "id": task_id,
        "source": "phase3_validation",
        "request_text": "Validate qwen_local provider routing.",
        "review_requested": True,
        "decision": None,
        "status": "pending",
        "correlation_id": f"corr-{task_id}",
        "acceptance_criteria": [{"id": "AC-1", "text": "Return strict JSON only."}],
        "task_kind": "protocol",
        "repo_path": "",
        "work_branch": "",
        "executor_provider": "codex",
        "reviewer_provider": reviewer_provider,
        "risk_level": "low",
        "side_effect_level": "read_only",
        "agn_managed": True,
    }
    SSOTStore(ROOT / "ssot").save_task(task)
    return task


def _write_packet(*, task_id: str, artifact_id: str, payload: dict[str, Any]) -> str:
    return write_json_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id=artifact_id,
        payload=payload,
        filename=f"{artifact_id}.json",
        source="phase3_validation",
    ).ref


def _run_worker(*, mode: str, packet_ref: str, env_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, "scripts/research_worker.py", "--role", "reviewer", "--mode", mode, "--packet-ref", packet_ref],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        timeout=240,
    )
    parsed: dict[str, Any] | None = None
    try:
        loaded = json.loads(str(proc.stdout or "").strip())
        if isinstance(loaded, dict):
            parsed = loaded
    except Exception:
        parsed = None
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "parsed": parsed or {},
    }


def _reviewer_topic_vote_packet(task_id: str) -> dict[str, Any]:
    return {
        "step": "task",
        "packet_schema": "reviewer_topic_vote_v1",
        "task_id": task_id,
        "role": "reviewer",
        "goal": "Judge only the current proposal.",
        "current_round": 1,
        "current_proposal": {
            "question": "Can a bounded local-global vote recover lag structure from local signals?",
            "baseline": "fixed global lag baseline",
            "single_change": "local-to-global voting over bounded windows",
            "research_axis": "time_series",
            "data_ready": True,
            "fixed_budget": True,
            "falsifiable": True,
            "degrade_ready": True,
            "external_dependency": False,
        },
        "budget": {"max_minutes": 15, "max_runs": 1},
        "current_action_required": "Return yes or no on proposal soundness only.",
        "if_reject_must_include": ["problem", "risk", "minimal_change"],
        "output_schema": {
            "decision": "yes|no",
            "if_no": ["problem", "risk", "minimal_change"],
        },
        "evidence_refs": {"survey_ref": "", "shortlist_ref": ""},
        "role_init_paths": research_flow._role_init_paths("reviewer"),
        "risk_level": "low",
        "side_effect_level": "read_only",
    }


def main() -> int:
    role_init_task_id = f"phase3-qwen-role-init-{uuid4().hex[:8]}"
    topic_vote_task_id = f"phase3-qwen-topic-vote-{uuid4().hex[:8]}"
    fallback_task_id = f"phase3-qwen-fallback-{uuid4().hex[:8]}"

    _save_protocol_task(task_id=role_init_task_id)
    _save_protocol_task(task_id=topic_vote_task_id)
    _save_protocol_task(task_id=fallback_task_id)

    role_init_packet = research_flow._role_init_packet(
        role="reviewer",
        round_no=1,
        mode="topic_vote",
        task_id=role_init_task_id,
    )
    role_init_packet["risk_level"] = "low"
    role_init_packet["side_effect_level"] = "read_only"

    topic_vote_packet = _reviewer_topic_vote_packet(topic_vote_task_id)
    fallback_packet = dict(role_init_packet)
    fallback_packet["task_id"] = fallback_task_id

    role_init_ref = _write_packet(task_id=role_init_task_id, artifact_id="qwen_role_init_packet", payload=role_init_packet)
    topic_vote_ref = _write_packet(task_id=topic_vote_task_id, artifact_id="qwen_topic_vote_packet", payload=topic_vote_packet)
    fallback_ref = _write_packet(task_id=fallback_task_id, artifact_id="qwen_fallback_role_init_packet", payload=fallback_packet)

    role_init_run = _run_worker(mode="role_init", packet_ref=role_init_ref)
    topic_vote_run = _run_worker(mode="topic_vote", packet_ref=topic_vote_ref)
    fallback_run = _run_worker(
        mode="role_init",
        packet_ref=fallback_ref,
        env_overrides={"QWEN_LOCAL_BASE_URL": "http://127.0.0.1:1/v1"},
    )

    summary = {
        "ok": False,
        "role_init": role_init_run,
        "topic_vote": topic_vote_run,
        "fallback": fallback_run,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    role_init_payload = role_init_run.get("parsed", {}) if isinstance(role_init_run.get("parsed", {}), dict) else {}
    topic_vote_payload = topic_vote_run.get("parsed", {}) if isinstance(topic_vote_run.get("parsed", {}), dict) else {}
    fallback_payload = fallback_run.get("parsed", {}) if isinstance(fallback_run.get("parsed", {}), dict) else {}

    role_init_ok = (
        role_init_run["returncode"] == 0
        and role_init_payload.get("provider") == "qwen_local"
        and role_init_payload.get("ack") == "init_loaded"
    )
    topic_vote_ok = (
        topic_vote_run["returncode"] == 0
        and topic_vote_payload.get("provider") == "qwen_local"
        and topic_vote_payload.get("decision") in {"yes", "no"}
    )
    fallback_ok = (
        fallback_run["returncode"] == 0
        and fallback_payload.get("provider") != "qwen_local"
        and str(fallback_payload.get("fallback_from", "")).strip() == "qwen_local"
        and isinstance(fallback_payload.get("provider_attempts"), list)
        and "qwen_local" in fallback_payload.get("provider_attempts", [])
    )
    summary["ok"] = bool(role_init_ok and topic_vote_ok and fallback_ok)
    summary["checks"] = {
        "role_init_ok": role_init_ok,
        "topic_vote_ok": topic_vote_ok,
        "fallback_ok": fallback_ok,
    }

    REPORT_PATH.write_text(json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": summary["ok"], "report": str(REPORT_PATH)}, ensure_ascii=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
