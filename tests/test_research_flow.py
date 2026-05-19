from __future__ import annotations

import builtins
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agn_api.ssot_store import SSOTStore
from scripts.coordinator_heartbeat import run_tick
from scripts.event_sourcing import load_checkpoint, load_events
from scripts.pointer_protocol import read_ref_text, write_json_artifact, write_text_artifact
import scripts.research_flow as research_flow
import scripts.research_worker as research_worker
from scripts.research_flow import run_research_unit

try:
    import research_flow as coordinator_research_flow
except Exception:  # pragma: no cover - fallback when imported through scripts package only
    coordinator_research_flow = research_flow


def _git_env() -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": "AGN Test",
        "GIT_AUTHOR_EMAIL": "agn-test@example.com",
        "GIT_COMMITTER_NAME": "AGN Test",
        "GIT_COMMITTER_EMAIL": "agn-test@example.com",
    }


def _init_research_repo(base: Path) -> Path:
    remote = base / "research-remote.git"
    repo = base / "research-repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("# Research Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True, env=_git_env())
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True, capture_output=True, text=True)
    return repo


def _init_blog_repo(base: Path) -> Path:
    remote = base / "blog-remote.git"
    repo = base / "blog-repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "config.toml").write_text('baseURL = "https://example.com/"\nlanguageCode = "en-us"\ntitle = "Test Blog"\n', encoding="utf-8")
    science_dir = repo / "content" / "AGNResearch"
    science_dir.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("# Blog Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True, env=_git_env())
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True, capture_output=True, text=True)
    return repo


@pytest.fixture(autouse=True)
def _research_publish_target(tmp_path, monkeypatch):
    repo = _init_research_repo(tmp_path)
    blog_repo = _init_blog_repo(tmp_path)
    monkeypatch.setenv("AGN_RESEARCH_REPO_PATH", str(repo))
    monkeypatch.setenv("AGN_RESEARCH_WORK_BRANCH", "main")
    monkeypatch.setenv("AGN_DEFAULT_REPO_PATH", str(repo))
    monkeypatch.setenv("AGN_DEFAULT_WORK_BRANCH", "main")
    monkeypatch.setenv("AGN_RESEARCH_BLOG_REPO_PATH", str(blog_repo))
    monkeypatch.setenv("AGN_RESEARCH_BLOG_BRANCH", "main")
    monkeypatch.setenv("AGN_RESEARCH_BLOG_SCIENCE_DIR", "content/AGNResearch")
    return repo


def _force_delivery_ack(monkeypatch, delivered: bool) -> None:
    monkeypatch.setenv("AGN_RESEARCH_WORKER_TRANSPORT", "stub")
    monkeypatch.setattr(research_flow, "_telegram_delivery_confirmed", lambda **kwargs: delivered)
    monkeypatch.setattr(coordinator_research_flow, "_telegram_delivery_confirmed", lambda **kwargs: delivered)


def _save_protocol_task(*, task_id: str, reviewer_provider: str, risk_level: str = "low", side_effect_level: str = "read_only") -> dict[str, object]:
    task = {
        "id": task_id,
        "source": "test",
        "request_text": "Route a bounded structured packet.",
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
        "risk_level": risk_level,
        "side_effect_level": side_effect_level,
        "agn_managed": True,
    }
    SSOTStore(research_flow.ROOT / "ssot").save_task(task)
    return task


def test_auto_research_runs_to_publish_and_delivery(monkeypatch) -> None:
    _force_delivery_ack(monkeypatch, True)

    summary = run_research_unit(
        task_id=f"test-auto-done-{uuid4().hex[:8]}",
        unit_date="2026-03-11",
        scenario="daily",
        max_steps=24,
        chat_id="test-chat",
    )

    assert summary["research_trigger_mode"] == "auto"
    assert summary["research_phase"] == "done"
    assert summary["state"] == "DELIVERED"
    assert summary["publish_status"] == "ok"
    assert summary["push_status"] == "ok"
    assert summary["publish_receipt_ref"].startswith("agn://")
    assert summary["telegram_receipt_ref"].startswith("agn://")
    assert summary["final_report_ref"].startswith("agn://")
    assert summary["essay_ref"].startswith("agn://")
    assert summary["code_bundle_ref"].startswith("agn://")
    assert summary["result_summary_ref"].startswith("agn://")
    assert summary["commit_hash"]


def test_daily_research_task_inherits_separate_publish_repo() -> None:
    task_id = f"test-research-target-{uuid4().hex[:8]}"
    task = research_flow._ensure_task(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        chat_id="test-chat",
    )

    assert str(task.get("repo_path", "")).strip()
    assert Path(str(task.get("repo_path", "")).strip()).resolve() != research_flow.ROOT.resolve()
    assert str(task.get("work_branch", "")).strip() == "main"
    assert str(task.get("blog_repo_path", "")).strip()
    assert str(task.get("blog_work_branch", "")).strip() == "main"
    assert str(task.get("blog_science_dir", "")).strip() == "content/AGNResearch"


def test_manual_research_locks_intake_and_skips_topic_discovery(monkeypatch) -> None:
    _force_delivery_ack(monkeypatch, True)
    task_id = f"test-manual-done-{uuid4().hex[:8]}"
    question = "Can a tiny model recover masked local spectrum structure?"
    hypothesis = "A tiny 1D convolutional autoencoder beats linear spectral interpolation on local band masking."

    summary = run_research_unit(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        max_steps=24,
        chat_id="test-chat",
        research_mode="manual",
        question=question,
        hypothesis=hypothesis,
    )

    checkpoint = load_checkpoint(task_id) or {}
    intake = json.loads(read_ref_text(str(checkpoint.get("intake_ref", "")).strip(), mode="all", max_bytes=512 * 1024))

    assert summary["research_trigger_mode"] == "manual"
    assert summary["research_phase"] == "done"
    assert summary["selected_topic_id"].startswith("manual-")
    assert str(checkpoint.get("intake_ref", "")).startswith("agn://")
    assert str(checkpoint.get("coordinator_preflight_ref", "")).startswith("agn://")
    assert str(checkpoint.get("governance_lock_ref", "")).startswith("agn://")
    assert str(checkpoint.get("research_plan_ref", "")).startswith("agn://")
    assert not str(checkpoint.get("daily_brief_ref", "")).strip()
    assert intake["mode"] == "manual_intake"
    assert intake["question"] == question
    assert intake["hypothesis"] == hypothesis


def test_manual_research_clears_seeded_fallback_when_explicit_question_present(monkeypatch) -> None:
    monkeypatch.setenv("AGN_RESEARCH_WORKER_TRANSPORT", "stub")
    task_id = f"test-manual-seed-clear-{uuid4().hex[:8]}"
    task = research_flow._ensure_task(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        chat_id="test-chat",
        research_mode="autonomy",
        manual_seed_topic_id="local_global_dependency",
    )
    task = research_flow._ensure_task(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        chat_id="test-chat",
        research_mode="manual",
        question="Does weighted logistic regression stay stable under label noise?",
        hypothesis="A confidence-weighted rule improves balanced accuracy.",
        baseline="plain logistic regression",
        single_change="confidence-based down-weighting",
    )

    summary = research_flow.drive_research_task(store=SSOTStore(research_flow.ROOT / "ssot"), task=task)
    checkpoint = load_checkpoint(task_id) or {}
    intake = json.loads(read_ref_text(str(checkpoint.get("intake_ref", "")).strip(), mode="all", max_bytes=512 * 1024))
    candidate = intake.get("candidate", {})

    assert summary["research_trigger_mode"] == "manual"
    assert str(task.get("manual_seed_topic_id", "")).strip() == ""
    assert candidate["problem"] == "Does weighted logistic regression stay stable under label noise?"
    assert str(candidate.get("topic_id", "")).startswith("manual-")
    assert candidate["survey_note"] == "Admin supplied the research question directly; the coordinator must stay on this topic."


def test_manual_research_without_intake_enters_explicit_admin_hold(monkeypatch) -> None:
    monkeypatch.setenv("AGN_RESEARCH_WORKER_TRANSPORT", "stub")

    summary = run_research_unit(
        task_id=f"test-manual-hold-{uuid4().hex[:8]}",
        unit_date="2026-03-11",
        scenario="daily",
        max_steps=3,
        chat_id="test-chat",
        research_mode="manual",
        question="",
        hypothesis="",
    )

    assert summary["research_trigger_mode"] == "manual"
    assert summary["research_phase"] == "manual_intake"
    assert summary["awaiting_admin_response"] is False
    assert summary["protocol_blocked"] is True
    assert summary["protocol_block_reason"] == "daily_research_contract_incomplete"
    assert "question_must_be_non_empty" in summary["governance_missing"]
    assert "hypothesis_must_be_non_empty" in summary["governance_missing"]


def test_auto_research_waits_in_brief_window(monkeypatch) -> None:
    monkeypatch.setenv("AGN_RESEARCH_WORKER_TRANSPORT", "stub")
    task_id = f"test-auto-wait-{uuid4().hex[:8]}"
    task = research_flow._ensure_task(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        chat_id="test-chat",
        research_mode="autonomy",
        awaiting_admin_until="2099-03-11 15:00",
    )

    summary = research_flow.drive_research_task(store=SSOTStore(research_flow.ROOT / "ssot"), task=task)

    assert summary["research_trigger_mode"] == "auto"
    assert summary["research_phase"] == "brief_wait"
    assert summary["coordinator_preflight_ref"].startswith("agn://")
    assert summary["awaiting_admin_response"] is True
    assert summary["admin_hold_reason"] == ""
    assert summary["admin_hold_until"] == "2099-03-11 15:00"


def test_delivery_is_not_terminal_without_verified_telegram_ack(monkeypatch) -> None:
    _force_delivery_ack(monkeypatch, False)

    summary = run_research_unit(
        task_id=f"test-delivery-hold-{uuid4().hex[:8]}",
        unit_date="2026-03-11",
        scenario="daily",
        max_steps=24,
        chat_id="test-chat",
    )

    assert summary["research_phase"] == "delivery"
    assert summary["state"] == "DELIVERY_GATE"
    assert summary["completion_ready"] is False
    assert summary["publish_status"] == "ok"
    assert summary["push_status"] == "ok"
    assert summary["telegram_receipt_ref"].startswith("agn://")


def test_done_phase_requires_publish_and_delivery_artifacts(monkeypatch) -> None:
    monkeypatch.setenv("AGN_RESEARCH_WORKER_TRANSPORT", "stub")
    task_id = f"test-done-tamper-{uuid4().hex[:8]}"
    task = research_flow._ensure_task(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        chat_id="test-chat",
        research_mode="manual",
        question="Can a tiny model recover local structure?",
        hypothesis="A tiny model beats linear interpolation.",
    )
    checkpoint = research_flow._ensure_checkpoint(
        task_id=task_id,
        trace_id=str(task.get("correlation_id", "")).strip(),
        unit_date="2026-03-11",
        scenario="daily",
        trigger_mode="manual",
    )
    research_flow._merge_checkpoint(
        task_id,
        checkpoint,
        research_phase="done",
        state="DELIVERED",
        completion_ready=True,
        admin_delivery_status="delivered",
    )

    summary = research_flow.drive_research_task(store=SSOTStore(research_flow.ROOT / "ssot"), task=task)

    assert summary["research_phase"] != "done"
    assert summary["protocol_blocked"] is True
    assert summary["governance_missing"]


def test_executor_blocks_cross_family_substitution_when_torch_is_missing(monkeypatch) -> None:
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "torch":
            raise ModuleNotFoundError("torch")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(
        research_worker,
        "run_command",
        lambda **kwargs: SimpleNamespace(
            command=kwargs.get("cmd", []),
            cwd=str(kwargs.get("cwd", "")),
            return_code=1,
            stdout="",
            stderr="install failed",
            duration_ms=1.0,
            timed_out=False,
        ),
    )

    result = research_worker._run_experiment(
        {
            "role": "executor",
            "goal": "Run the requested experiment faithfully.",
            "current_round": 1,
            "current_action_required": "Return an execution result or a failure note.",
            "output_schema": {"status": "ok|failure_note"},
            "role_init_paths": research_flow._role_init_paths("executor"),
            "proposal": {"topic_id": "manual-topic"},
            "required_method_family": "tiny_conv_autoencoder",
            "same_family_only": True,
            "allow_trusted_dependency_installs": True,
            "strategy_candidates": ["full", "baseline_only"],
        }
    )

    assert result["status"] == "failure_note"
    assert result["exception_category"] == "SYSTEM_DEGRADE.dependency_unavailable"
    assert result["same_family_only"] is True
    assert "tiny_conv_autoencoder_requires_torch" in result["error"]
    assert result["dependency_install_attempts"][0]["status"] == "failed"
    assert result["dependency_install_attempts"][0]["source_label"] == "pytorch_official_cpu_whl"


def test_executor_attempts_trusted_dependency_install_before_continuing(monkeypatch) -> None:
    install_state = {"installed": False}
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "torch" and not install_state["installed"]:
            raise ModuleNotFoundError("torch")
        return original_import(name, *args, **kwargs)

    def fake_run_command(**kwargs):
        install_state["installed"] = True
        return SimpleNamespace(
            command=kwargs.get("cmd", []),
            cwd=str(kwargs.get("cwd", "")),
            return_code=0,
            stdout="ok",
            stderr="",
            duration_ms=1.0,
            timed_out=False,
        )

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(research_worker, "run_command", fake_run_command)

    result = research_worker._run_experiment(
        {
            "task_id": f"install-task-{uuid4().hex[:8]}",
            "role": "executor",
            "goal": "Run the requested experiment faithfully.",
            "current_round": 1,
            "current_action_required": "Return an execution result or a failure note.",
            "output_schema": {"status": "ok|failure_note"},
            "role_init_paths": research_flow._role_init_paths("executor"),
            "proposal": {"topic_id": "manual-topic", "title": "manual"},
            "required_method_family": "tiny_conv_autoencoder",
            "same_family_only": True,
            "allow_trusted_dependency_installs": True,
            "strategy_candidates": ["baseline_only"],
        }
    )

    assert result["status"] in {"ok", "degraded"}
    assert result["dependency_install_attempts"][0]["status"] == "installed"
    assert "Trusted dependency installation was attempted before execution continued." in result["notes"]


def test_run_real_transport_routes_role_init_to_qwen_local(monkeypatch) -> None:
    task_id = f"test-qwen-route-{uuid4().hex[:8]}"
    _save_protocol_task(task_id=task_id, reviewer_provider="qwen_local")
    packet = research_flow._role_init_packet(
        role="reviewer",
        round_no=1,
        mode="topic_vote",
        task_id=task_id,
    )
    packet["risk_level"] = "low"
    packet["side_effect_level"] = "read_only"
    packet_ref = write_json_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="qwen_role_init_packet",
        payload=packet,
        filename="qwen_role_init_packet.json",
        source="test",
    ).ref

    api_calls: list[str] = []

    def fake_run_api_provider(**kwargs):
        api_calls.append(str(kwargs.get("provider", "")))
        return (
            {
                "role": "reviewer",
                "mode": "role_init",
                "provider": "qwen_local",
                "transport": "api",
                "ack": "init_loaded",
                "current_round": 1,
                "schema": "reviewer_topic_vote_v1",
                "protocol_digest": research_flow._role_init_digest("reviewer"),
                "integrity_ack": "truthfulness_first",
                "failure_ack": "failure_is_valid",
                "fabrication_ack": "no_fabrication",
            },
            "",
        )

    monkeypatch.setattr(research_worker, "_run_api_provider", fake_run_api_provider)

    result = research_worker._run_real_transport(
        role="reviewer",
        mode="role_init",
        packet_ref=packet_ref,
        packet=packet,
    )

    assert result["provider"] == "qwen_local"
    assert result["transport"] == "api"
    assert result["requested_provider"] == "qwen_local"
    assert result["provider_attempts"] == ["qwen_local"]
    assert api_calls == ["qwen_local"]


def test_run_real_transport_blocks_qwen_local_for_final_review_and_falls_back(monkeypatch) -> None:
    task_id = f"test-qwen-fallback-{uuid4().hex[:8]}"
    _save_protocol_task(task_id=task_id, reviewer_provider="qwen_local")
    packet = {
        "task_id": task_id,
        "role": "reviewer",
        "goal": "Return a final review verdict.",
        "current_round": 2,
        "current_action_required": "Audit the packet and return the final review JSON.",
        "output_schema": {"verdict": "APPROVED|REVISION_ONCE|FAILURE_ARCHIVE"},
        "role_init_paths": research_flow._role_init_paths("reviewer"),
        "evidence_boundary": ["agn://artifact/test-evidence"],
        "risk_level": "low",
        "side_effect_level": "read_only",
    }
    packet_ref = write_json_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="qwen_final_review_packet",
        payload=packet,
        filename="qwen_final_review_packet.json",
        source="test",
    ).ref

    api_calls: list[str] = []
    cli_calls: list[str] = []

    def fake_run_api_provider(**kwargs):
        provider = str(kwargs.get("provider", ""))
        api_calls.append(provider)
        if provider == "deepseek":
            return None, "deepseek_api_key_missing:DEEPSEEK_API_KEY"
        raise AssertionError("qwen_local should be policy-blocked before API invocation")

    def fake_run_cli_provider(**kwargs):
        provider = str(kwargs.get("provider", ""))
        cli_calls.append(provider)
        return (
            {
                "role": "reviewer",
                "mode": "final_review",
                "provider": provider,
                "transport": "cli",
                "verdict": "APPROVED",
                "issue": "",
                "risk": "",
                "minimal_fix": "",
                "failure_type": "",
                "reason": "fallback reviewer accepted the bounded packet",
                "rerun_worth_it": False,
                "evidence_boundary": ["agn://artifact/test-evidence"],
                "message": "APPROVED",
            },
            "",
        )

    monkeypatch.setattr(research_worker, "_run_api_provider", fake_run_api_provider)
    monkeypatch.setattr(research_worker, "_run_cli_provider", fake_run_cli_provider)

    result = research_worker._run_real_transport(
        role="reviewer",
        mode="final_review",
        packet_ref=packet_ref,
        packet=packet,
    )

    assert result["provider"] == "gemini"
    assert result["fallback_from"] == "qwen_local"
    assert result["provider_attempts"] == ["qwen_local", "deepseek", "gemini"]
    assert api_calls == ["deepseek"]
    assert cli_calls == ["gemini"]


def test_heartbeat_hard_blocks_incomplete_daily_research_contract(monkeypatch) -> None:
    monkeypatch.setenv("AGN_RESEARCH_WORKER_TRANSPORT", "stub")
    task_id = f"test-contract-block-{uuid4().hex[:8]}"
    store = SSOTStore(research_flow.ROOT / "ssot")
    store.save_task(
        {
            "id": task_id,
            "agn_managed": True,
            "task_kind": "daily_research",
            "correlation_id": f"corr-{task_id}",
            "unit_date": "2026-03-11",
            "scenario": "daily",
            "research_mode": "manual",
            "research_axis": "",
            "question": "",
            "hypothesis": "",
            "baseline": "",
            "single_change": "",
            "budget": {},
            "round": 0,
            "proposal_version": 0,
            "decision_mode": "",
            "failure_mode_allowed": False,
            "review_requested": True,
            "status": "pending",
        }
    )

    result = run_tick(max_tasks=20, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
    summary = result["summaries"][0]
    checkpoint = load_checkpoint(task_id) or {}

    assert summary["backend"] == "research_main_chain_blocked"
    assert summary["protocol_blocked"] is True
    assert "research_trigger_mode_invalid" in summary["governance_missing"]
    assert checkpoint["protocol_block_reason"] == "daily_research_contract_incomplete"


def test_publish_research_forbids_infra_repo() -> None:
    task_id = f"test-publish-root-{uuid4().hex[:8]}"
    task = research_flow._ensure_task(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        chat_id="test-chat",
    )
    task["repo_path"] = str(research_flow.ROOT)
    task["work_branch"] = "main"
    SSOTStore(research_flow.ROOT / "ssot").save_task(task)

    essay_ref = write_text_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="essay",
        content="# Essay\n",
        media_type="text/markdown",
        filename="essay.md",
        source="test",
    ).ref
    final_report_ref = write_text_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="final_report",
        content="# Final\n",
        media_type="text/markdown",
        filename="final_report.md",
        source="test",
    ).ref
    result_summary_ref = write_json_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="result_summary",
        payload={"ok": True},
        filename="result_summary.json",
        source="test",
    ).ref
    code_bundle_ref = write_text_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="code_bundle",
        content="print('ok')\n",
        media_type="text/x-python",
        filename="experiment.py",
        source="test",
    ).ref

    result = research_worker._publish_research(
        {
            "task_id": task_id,
            "role": "executor",
            "mode": "publish_research",
            "goal": "Publish research outputs into the target repo.",
            "current_round": 1,
            "current_action_required": "Write the outputs into the configured research repo.",
            "output_schema": {"status": "ok|retry"},
            "role_init_paths": research_flow._role_init_paths("executor"),
            "essay_ref": essay_ref,
            "final_report_ref": final_report_ref,
            "result_summary_ref": result_summary_ref,
            "code_bundle_ref": code_bundle_ref,
        }
    )

    assert result["status"] == "retry"
    assert result["push_status"] == "failed"
    assert result["error"] == "infra_repo_publish_forbidden"


def test_publish_research_writes_hugo_science_post(monkeypatch, tmp_path) -> None:
    research_repo = Path(str(os.environ["AGN_RESEARCH_REPO_PATH"]))
    blog_repo = Path(str(os.environ["AGN_RESEARCH_BLOG_REPO_PATH"]))
    monkeypatch.setenv("AGN_RESEARCH_WORKER_TRANSPORT", "stub")
    monkeypatch.setenv("AGN_RESEARCH_REPO_PATH", str(research_repo))
    monkeypatch.setenv("AGN_RESEARCH_WORK_BRANCH", "main")
    monkeypatch.setenv("AGN_RESEARCH_BLOG_REPO_PATH", str(blog_repo))
    monkeypatch.setenv("AGN_RESEARCH_BLOG_BRANCH", "main")
    monkeypatch.setenv("AGN_RESEARCH_BLOG_SCIENCE_DIR", "content/AGNResearch")

    task_id = f"test-blog-sync-{uuid4().hex[:8]}"
    task = research_flow._ensure_task(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        chat_id="test-chat",
        research_mode="manual",
        question="Does weighted logistic regression stay stable under label noise?",
        hypothesis="A simple confidence-weighted rule improves balanced accuracy.",
    )
    task["repo_path"] = str(research_repo)
    task["work_branch"] = "main"
    task["blog_repo_path"] = str(blog_repo)
    task["blog_work_branch"] = "main"
    task["blog_science_dir"] = "content/AGNResearch"
    SSOTStore(research_flow.ROOT / "ssot").save_task(task)

    essay_ref = write_text_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="essay",
        content="# Mini Paper\n\n## Problem\nA compact test.\n\n## Method\nUse $p(y=1\\mid x)=\\sigma(w^\\top x+b)$.\n",
        media_type="text/markdown",
        filename="essay.md",
        source="test",
    ).ref
    final_report_ref = write_text_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="final_report",
        content="# Final Report\n\n- status: ok\n",
        media_type="text/markdown",
        filename="final_report.md",
        source="test",
    ).ref
    result_summary_ref = write_json_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="result_summary",
        payload={"ok": True},
        filename="result_summary.json",
        source="test",
    ).ref
    raw_results_ref = write_json_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="raw_results",
        payload={"runs": [1, 2, 3]},
        filename="raw_results.json",
        source="test",
    ).ref
    data_record_ref = write_json_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="data_record",
        payload={"dataset_kind": "synthetic"},
        filename="data_record.json",
        source="test",
    ).ref
    reproduce_ref = write_text_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="reproduce",
        content="# Reproduce\n\npython experiment.py > rerun_results.json\n",
        media_type="text/markdown",
        filename="reproduce.md",
        source="test",
    ).ref
    code_bundle_ref = write_text_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="code_bundle",
        content="print('ok')\n",
        media_type="text/x-python",
        filename="experiment.py",
        source="test",
    ).ref

    result = research_worker._publish_research(
        {
            "task_id": task_id,
            "role": "executor",
            "mode": "publish_research",
            "goal": "Publish research outputs into the target repo and blog.",
            "current_round": 1,
            "current_action_required": "Write the outputs into the configured research repo and blog.",
            "output_schema": {"status": "ok|retry"},
            "role_init_paths": research_flow._role_init_paths("executor"),
            "title": "Noise Robust Logistic Regression",
            "question": str(task.get("question", "")).strip(),
            "hypothesis": str(task.get("hypothesis", "")).strip(),
            "research_axis": "Machine Learning",
            "unit_date": "2026-03-11",
            "essay_ref": essay_ref,
            "final_report_ref": final_report_ref,
            "result_summary_ref": result_summary_ref,
            "raw_results_ref": raw_results_ref,
            "data_record_ref": data_record_ref,
            "reproduce_ref": reproduce_ref,
            "code_bundle_ref": code_bundle_ref,
            "archive_ref": "agn://artifact/archive",
            "trace_index_ref": "agn://artifact/trace",
            "outcome_kind": "mini_paper",
            "empirical_execution": True,
            "repo_path": str(research_repo),
            "work_branch": "main",
            "blog_repo_path": str(blog_repo),
            "blog_work_branch": "main",
            "blog_science_dir": "content/AGNResearch",
        }
    )

    assert result["status"] == "ok"
    assert result["blog_post_path"].startswith("content/AGNResearch/")
    post_path = blog_repo / result["blog_post_path"]
    assert post_path.exists()
    content = post_path.read_text(encoding="utf-8")
    assert "draft = false" in content
    assert "hiddenInHomeList = true" in content
    assert 'title = "Noise Robust Logistic Regression"' in content
    assert "## Problem" in content
    assert "$p(y=1\\mid x)=\\sigma(w^\\top x+b)$" in content


def test_archive_stage_emits_reproducibility_artifacts(monkeypatch) -> None:
    monkeypatch.setenv("AGN_RESEARCH_WORKER_TRANSPORT", "stub")
    task_id = f"test-archive-repro-{uuid4().hex[:8]}"
    task = research_flow._ensure_task(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        chat_id="test-chat",
        research_mode="manual",
        question="Does a fixed-rate perceptron outperform a majority baseline under mild label noise?",
        hypothesis="Perceptron should improve balanced accuracy but lose stability as noise rises.",
        baseline="majority-class baseline",
        single_change="replace the constant classifier with a fixed-rate perceptron",
    )
    trace_id = str(task.get("correlation_id", "")).strip()
    checkpoint = research_flow._ensure_checkpoint(
        task_id=task_id,
        trace_id=trace_id,
        unit_date="2026-03-11",
        scenario="daily",
        trigger_mode="manual",
    )
    paper_ref = write_text_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="mini_paper",
        content="# Mini Paper\n\nBounded synthetic result.\n",
        media_type="text/markdown",
        filename="mini_paper.md",
        source="test",
    ).ref
    verdict_ref = write_text_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="review_verdict",
        content='{"verdict":"APPROVED"}',
        media_type="application/json",
        filename="review_verdict.json",
        source="test",
    ).ref
    checkpoint = research_flow._merge_checkpoint(
        task_id,
        checkpoint,
        state="REVIEW_DONE",
        research_phase="archive",
        current_candidate=research_flow._manual_seed_candidate(task, research_flow._load_profile()),
        essay_ref=paper_ref,
        paper_ref=paper_ref,
        final_review={"verdict": "APPROVED"},
        review_verdict_ref=verdict_ref,
        experiment_ref=write_json_artifact(
            task_id=task_id,
            attempt=1,
            artifact_id="experiment_result",
            payload={"status": "ok"},
            filename="experiment_result.json",
            source="test",
        ).ref,
        experiment_summary_ref=write_json_artifact(
            task_id=task_id,
            attempt=1,
            artifact_id="experiment_summary",
            payload={"ok": True},
            filename="experiment_summary.json",
            source="test",
        ).ref,
        experiment_raw_ref=write_text_artifact(
            task_id=task_id,
            attempt=1,
            artifact_id="executor_result",
            content='{"status":"ok"}',
            media_type="application/json",
            filename="executor_result.json",
            source="test",
        ).ref,
        experiment_log_ref=write_text_artifact(
            task_id=task_id,
            attempt=1,
            artifact_id="experiment_log",
            content="python experiment output",
            media_type="text/plain",
            filename="experiment.log",
            source="test",
        ).ref,
        experiment_result={
            "status": "ok",
            "strategy": "full",
            "metrics": {"noise_levels": [0.0, 0.05], "perceptron_bal_acc": [0.9, 0.85], "baseline_bal_acc": [0.5, 0.5]},
        },
    )
    monkeypatch.setattr(research_flow, "_require_governance", lambda **kwargs: (kwargs["checkpoint"], True))

    summary = research_flow._archive_stage(task_id=task_id, trace_id=trace_id, checkpoint=checkpoint)

    assert str(summary.get("raw_results_ref", "")).startswith("agn://")
    assert str(summary.get("data_record_ref", "")).startswith("agn://")
    assert str(summary.get("reproduce_ref", "")).startswith("agn://")


def test_paper_stage_uses_current_candidate_and_experiment_metrics(monkeypatch) -> None:
    monkeypatch.setenv("AGN_RESEARCH_WORKER_TRANSPORT", "stub")
    task_id = f"test-paper-dynamic-{uuid4().hex[:8]}"
    task = research_flow._ensure_task(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        chat_id="test-chat",
        research_mode="manual",
        question="Does a fixed-rate perceptron outperform a majority baseline under mild label noise?",
        hypothesis="Perceptron should improve balanced accuracy but lose stability as noise rises.",
        baseline="majority-class baseline",
        single_change="replace the constant classifier with a fixed-rate perceptron",
    )
    trace_id = str(task.get("correlation_id", "")).strip()
    checkpoint = research_flow._ensure_checkpoint(
        task_id=task_id,
        trace_id=trace_id,
        unit_date="2026-03-11",
        scenario="daily",
        trigger_mode="manual",
    )
    candidate = research_flow._manual_seed_candidate(task, research_flow._load_profile())
    checkpoint = research_flow._merge_checkpoint(
        task_id,
        checkpoint,
        state="EXEC_DONE",
        research_phase="writing",
        current_candidate=candidate,
        experiment_result={
            "status": "ok",
            "strategy": "full",
            "metrics": {
                "avg_balanced_accuracy": 0.883,
                "avg_baseline_accuracy": 0.5,
                "cases_completed": 6,
                "runtime_sec": 0.45,
            },
            "notes": [
                "Perceptron outperformed the majority baseline on every bounded synthetic case.",
            ],
            "empirical_execution": True,
            "truthfulness_status": "empirical",
            "truthfulness_reason": "verifiable_local_execution",
        },
        empirical_execution=True,
        truthfulness_status="empirical",
        truthfulness_reason="verifiable_local_execution",
    )
    monkeypatch.setattr(research_flow, "_require_governance", lambda **kwargs: (kwargs["checkpoint"], True))

    summary = research_flow._paper_stage(task_id=task_id, trace_id=trace_id, checkpoint=checkpoint)
    body = read_ref_text(str(summary.get("paper_ref", "")).strip(), mode="all", max_bytes=128 * 1024)

    assert "hidden lag variable" not in body
    assert "local-to-global method" not in body
    assert "fixed-rate perceptron" in body
    assert "avg_balanced_accuracy" in body
    assert "0.883" in body
    assert "cases_completed" in body


def test_experiment_script_body_generates_real_reproduction_code() -> None:
    task_id = f"test-experiment-script-{uuid4().hex[:8]}"
    task = research_flow._ensure_task(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        chat_id="test-chat",
        research_mode="manual",
        question="Does a fixed-rate perceptron outperform a majority baseline under mild label noise?",
        hypothesis="Perceptron should improve balanced accuracy but lose stability as noise rises.",
        baseline="majority-class baseline",
        single_change="replace the constant classifier with a fixed-rate perceptron",
    )
    trace_id = str(task.get("correlation_id", "")).strip()
    checkpoint = research_flow._ensure_checkpoint(
        task_id=task_id,
        trace_id=trace_id,
        unit_date="2026-03-11",
        scenario="daily",
        trigger_mode="manual",
    )
    checkpoint = research_flow._merge_checkpoint(
        task_id,
        checkpoint,
        experiment_result={
            "status": "ok",
            "strategy": "full",
            "metrics": {
                "noise_levels": [0.0, 0.05, 0.1],
                "perceptron_bal_acc": [0.96, 0.92, 0.88],
                "baseline_bal_acc": [0.5, 0.5, 0.5],
            },
        },
    )
    script = research_flow._experiment_script_body(task=task, checkpoint=checkpoint)

    assert "def train_perceptron" in script
    assert "def balanced_accuracy" in script
    assert "reproduction_runs" in script
    assert "payload =" not in script


def test_reviewer_final_review_packet_embeds_evidence_summary() -> None:
    task_id = f"test-final-review-packet-{uuid4().hex[:8]}"
    task = research_flow._ensure_task(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        chat_id="test-chat",
        research_mode="manual",
        question="Does a fixed-rate perceptron outperform a majority baseline under mild label noise?",
        hypothesis="Perceptron should improve balanced accuracy but lose stability as noise rises.",
    )
    trace_id = str(task.get("correlation_id", "")).strip()
    checkpoint = research_flow._ensure_checkpoint(
        task_id=task_id,
        trace_id=trace_id,
        unit_date="2026-03-11",
        scenario="daily",
        trigger_mode="manual",
    )
    paper_ref = write_text_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="mini_paper",
        content="# Mini Paper\n\nPerceptron beats the majority baseline in the bounded synthetic sweep.\n",
        media_type="text/markdown",
        filename="mini_paper.md",
        source="test",
    ).ref
    checkpoint = research_flow._merge_checkpoint(
        task_id,
        checkpoint,
        current_candidate=research_flow._manual_seed_candidate(task, research_flow._load_profile()),
        experiment_result={
            "status": "ok",
            "strategy": "full",
            "metrics": {"avg_balanced_accuracy": 0.883, "avg_baseline_accuracy": 0.5},
            "notes": ["Perceptron remained above baseline at all tested noise levels."],
            "empirical_execution": True,
            "truthfulness_status": "empirical",
            "truthfulness_reason": "verifiable_local_execution",
        },
        empirical_execution=True,
        truthfulness_status="empirical",
        truthfulness_reason="verifiable_local_execution",
        paper_ref=paper_ref,
        outcome_kind="mini_paper",
        issue_history=[{"round": 1, "executor_decision": "yes", "reviewer_decision": "yes"}],
    )

    packet = research_flow._reviewer_final_review_packet(task_id=task_id, checkpoint=checkpoint)
    prompt = research_worker._prompt_for_provider(
        provider="gemini",
        role="reviewer",
        mode="final_review",
        packet=packet,
        packet_path=Path("/tmp/final_review_packet.json"),
    )

    assert packet["paper_excerpt"].startswith("# Mini Paper")
    assert packet["experiment_summary"]["metrics"]["avg_balanced_accuracy"] == 0.883
    assert packet["review_scope"]["empirical_execution"] is True
    assert "Embedded packet JSON" in prompt
    assert "/runtime/research_packets/" not in prompt


def test_non_empirical_experiment_result_is_blocked_and_downgraded(monkeypatch) -> None:
    monkeypatch.setenv("AGN_RESEARCH_WORKER_TRANSPORT", "stub")
    task_id = f"test-non-empirical-{uuid4().hex[:8]}"
    task = research_flow._ensure_task(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        chat_id="test-chat",
        research_mode="manual",
        question="Does quadratic logistic regression outperform linear logistic regression on noisy circles?",
        hypothesis="Quadratic features should improve balanced accuracy.",
        baseline="plain linear logistic regression",
        single_change="add a fixed quadratic feature map",
    )
    trace_id = str(task.get("correlation_id", "")).strip()
    checkpoint = research_flow._ensure_checkpoint(
        task_id=task_id,
        trace_id=trace_id,
        unit_date="2026-03-11",
        scenario="daily",
        trigger_mode="manual",
    )
    checkpoint = research_flow._merge_checkpoint(
        task_id,
        checkpoint,
        state="PLANNED",
        research_phase="execution",
        round=1,
        survey_ref=write_json_artifact(task_id=task_id, attempt=1, artifact_id="survey", payload={"ok": True}, filename="survey.json", source="test").ref,
        shortlist_ref=write_json_artifact(task_id=task_id, attempt=1, artifact_id="shortlist", payload={"ok": True}, filename="shortlist.json", source="test").ref,
        proposal_ref=write_json_artifact(task_id=task_id, attempt=1, artifact_id="proposal", payload={"ok": True}, filename="proposal.json", source="test").ref,
        acceptance_spec_ref=write_json_artifact(task_id=task_id, attempt=1, artifact_id="acceptance", payload={"ok": True}, filename="acceptance.json", source="test").ref,
        round_records=[{"round": 1, "executor_decision": "yes", "reviewer_decision": "yes"}],
        issue_history=[{"round": 1, "executor_decision": "yes", "reviewer_decision": "yes"}],
        current_candidate=research_flow._manual_seed_candidate(task, research_flow._load_profile()),
    )

    def fake_dispatch_role(**kwargs):
        return kwargs["checkpoint"], {
            "payload": {
                "status": "ok",
                "strategy": "full",
                "metrics": {"poly_acc": 0.95, "linear_acc": 0.5},
                "notes": [
                    "Execution tool unavailable; downgraded to dry_run/theoretical simulation.",
                    "Simulation confirms the expected gain.",
                ],
                "provider": "gemini",
                "transport": "cli",
                "empirical_execution": False,
                "truthfulness_status": "non_empirical",
                "truthfulness_reason": "cli_result_has_no_verifiable_execution_evidence",
            },
            "message_ref": "agn://artifact/mock-executor-message",
        }

    monkeypatch.setattr(research_flow, "_require_governance", lambda **kwargs: (kwargs["checkpoint"], True))
    monkeypatch.setattr(research_flow, "_dispatch_role", fake_dispatch_role)

    summary = research_flow._experiment_stage(task_id=task_id, trace_id=trace_id, checkpoint=checkpoint, profile=research_flow._load_profile())

    assert summary["experiment_result"]["status"] == "failure_note"
    assert summary["experiment_result"]["metrics"] == {}
    assert summary["experiment_result"]["unverified_metrics"]["poly_acc"] == 0.95
    assert summary["empirical_execution"] is False
    assert summary["truthfulness_status"] == "non_empirical"
    events = load_events(trace_id)
    assert any(event.get("event_type") == "PROTOCOL_VIOLATION" for event in events)


def test_truthfulness_gate_rejects_simulated_execution_even_if_status_reports_ok() -> None:
    empirical, truthfulness_status, truthfulness_reason = research_flow._experiment_truthfulness(
        {
            "status": "ok",
            "strategy": "full",
            "notes": [
                "Simulated 6 synthetic Python debugging scenarios with debugger-style state tracking.",
                "Hypothesis supported in the simulated benchmark.",
            ],
            "metrics": {
                "cases_evaluated": 6,
                "experimental_error_localization_accuracy": 1.0,
            },
        }
    )

    assert empirical is False
    assert truthfulness_status == "non_empirical"
    assert "simulated" in truthfulness_reason.lower()


def test_role_init_ack_carries_protocol_digest_and_integrity_contract() -> None:
    packet = research_flow._role_init_packet(
        role="executor",
        round_no=1,
        mode="topic_vote",
        task_id=f"test-role-init-{uuid4().hex[:8]}",
    )

    ack = research_worker._role_init_ack(role="executor", packet=packet)

    assert ack["protocol_digest"] == research_flow._role_init_digest("executor")
    assert ack["integrity_ack"] == "truthfulness_first"
    assert ack["failure_ack"] == "failure_is_valid"
    assert ack["fabrication_ack"] == "no_fabrication"
    assert research_flow._role_init_ack_valid(ack, "executor") is True


def test_role_init_ack_validation_rejects_mismatched_digest() -> None:
    packet = research_flow._role_init_packet(
        role="reviewer",
        round_no=1,
        mode="final_review",
        task_id=f"test-role-init-invalid-{uuid4().hex[:8]}",
    )

    ack = research_worker._role_init_ack(role="reviewer", packet=packet)
    ack["protocol_digest"] = "mismatch"

    assert research_flow._role_init_ack_valid(ack, "reviewer") is False


def test_dispatch_role_blocks_when_role_init_ack_is_invalid(monkeypatch) -> None:
    task_id = f"test-invalid-init-{uuid4().hex[:8]}"
    task = research_flow._ensure_task(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        chat_id="test-chat",
    )
    trace_id = str(task.get("correlation_id", "")).strip()
    checkpoint = research_flow._ensure_checkpoint(
        task_id=task_id,
        trace_id=trace_id,
        unit_date="2026-03-11",
        scenario="daily",
        trigger_mode="manual",
    )
    calls: list[str] = []

    def fake_invoke_worker(**kwargs):
        calls.append(str(kwargs.get("mode", "")).strip())
        if str(kwargs.get("mode", "")).strip() == "role_init":
            return checkpoint, {
                "role": "executor",
                "mode": "role_init",
                "ack": "init_loaded",
                "current_round": 1,
                "schema": "executor_topic_vote_v1",
                "protocol_digest": "mismatch",
                "integrity_ack": "truthfulness_first",
                "failure_ack": "failure_is_valid",
                "fabrication_ack": "no_fabrication",
            }, "agn://artifact/invalid-init-ack"
        raise AssertionError("task packet should not dispatch after invalid role init ack")

    monkeypatch.setattr(research_flow, "_invoke_worker", fake_invoke_worker)

    checkpoint, dispatch = research_flow._dispatch_role(
        task_id=task_id,
        trace_id=trace_id,
        checkpoint=checkpoint,
        role="executor",
        mode="topic_vote",
        round_no=1,
        packet={"role": "executor", "goal": "Judge executability only."},
    )

    assert calls == ["role_init"]
    assert dispatch["packet_ref"] == ""
    assert dispatch["payload"]["decision"] == "no"
    assert dispatch["payload"]["problem"] == "role init acknowledgement invalid"
    events = load_events(trace_id)
    assert any(
        str(event.get("event_type", "")).strip() == "PROTOCOL_VIOLATION"
        and str((event.get("payload") or {}).get("reason", "")).strip() == "invalid_role_init_ack"
        for event in events
    )


def test_reviewer_final_review_rejects_non_empirical_execution() -> None:
    packet = {
        "role": "reviewer",
        "goal": "Audit the final archive honestly.",
        "current_round": 1,
        "current_action_required": "Return APPROVED, REVISION_ONCE, or FAILURE_ARCHIVE on archive completeness, empirical execution authenticity, and evidence boundary.",
        "output_schema": {"verdict": "APPROVED|REVISION_ONCE|FAILURE_ARCHIVE"},
        "role_init_paths": research_flow._role_init_paths("reviewer"),
        "review_scope": {
            "outcome_kind": "failure_note",
            "message_count": 8,
            "honest_failure": True,
            "review_revision_count": 0,
            "empirical_execution": False,
            "truthfulness_status": "non_empirical",
            "truthfulness_reason": "cli_result_has_no_verifiable_execution_evidence",
            "unverified_metrics_present": True,
        },
        "evidence_refs": {
            "survey_ref": "agn://artifact/survey",
            "shortlist_ref": "agn://artifact/shortlist",
            "experiment_ref": "agn://artifact/experiment",
            "failure_note_ref": "agn://artifact/failure",
        },
        "evidence_boundary": [
            "agn://artifact/survey",
            "agn://artifact/shortlist",
            "agn://artifact/experiment",
            "agn://artifact/failure",
        ],
    }

    verdict = research_worker._final_review(role="reviewer", packet=packet)

    assert verdict["verdict"] == "FAILURE_ARCHIVE"
    assert verdict["failure_type"] == "non_empirical_execution"


def test_publish_research_skips_blog_for_non_empirical_failure_note() -> None:
    research_repo = Path(str(os.environ["AGN_RESEARCH_REPO_PATH"]))
    blog_repo = Path(str(os.environ["AGN_RESEARCH_BLOG_REPO_PATH"]))
    task_id = f"test-skip-blog-{uuid4().hex[:8]}"
    task = research_flow._ensure_task(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        chat_id="test-chat",
        research_mode="manual",
        question="Did the bounded run fail honestly?",
        hypothesis="The task should archive without blog publication.",
    )
    task["repo_path"] = str(research_repo)
    task["work_branch"] = "main"
    task["blog_repo_path"] = str(blog_repo)
    task["blog_work_branch"] = "main"
    task["blog_science_dir"] = "content/AGNResearch"
    SSOTStore(research_flow.ROOT / "ssot").save_task(task)

    essay_ref = write_text_artifact(task_id=task_id, attempt=1, artifact_id="essay", content="# Failure Note\n", media_type="text/markdown", filename="essay.md", source="test").ref
    final_report_ref = write_text_artifact(task_id=task_id, attempt=1, artifact_id="final_report", content="# Final Report\n", media_type="text/markdown", filename="final_report.md", source="test").ref
    result_summary_ref = write_json_artifact(task_id=task_id, attempt=1, artifact_id="result_summary", payload={"ok": True}, filename="result_summary.json", source="test").ref
    raw_results_ref = write_json_artifact(task_id=task_id, attempt=1, artifact_id="raw_results", payload={"simulated": True}, filename="raw_results.json", source="test").ref
    data_record_ref = write_json_artifact(task_id=task_id, attempt=1, artifact_id="data_record", payload={"dataset_kind": "synthetic"}, filename="data_record.json", source="test").ref
    reproduce_ref = write_text_artifact(task_id=task_id, attempt=1, artifact_id="reproduce", content="# Reproduce\n", media_type="text/markdown", filename="reproduce.md", source="test").ref
    code_bundle_ref = write_text_artifact(task_id=task_id, attempt=1, artifact_id="code_bundle", content="print('ok')\n", media_type="text/x-python", filename="experiment.py", source="test").ref

    result = research_worker._publish_research(
        {
            "task_id": task_id,
            "role": "executor",
            "mode": "publish_research",
            "goal": "Publish research outputs into the target repo.",
            "current_round": 1,
            "current_action_required": "Write the outputs into the configured research repo.",
            "output_schema": {"status": "ok|retry"},
            "role_init_paths": research_flow._role_init_paths("executor"),
            "title": "Failure archive",
            "question": str(task.get("question", "")).strip(),
            "hypothesis": str(task.get("hypothesis", "")).strip(),
            "research_axis": "Machine Learning",
            "unit_date": "2026-03-11",
            "essay_ref": essay_ref,
            "final_report_ref": final_report_ref,
            "result_summary_ref": result_summary_ref,
            "raw_results_ref": raw_results_ref,
            "data_record_ref": data_record_ref,
            "reproduce_ref": reproduce_ref,
            "code_bundle_ref": code_bundle_ref,
            "archive_ref": "agn://artifact/archive",
            "trace_index_ref": "agn://artifact/trace",
            "repo_path": str(research_repo),
            "work_branch": "main",
            "blog_repo_path": str(blog_repo),
            "blog_work_branch": "main",
            "blog_science_dir": "content/AGNResearch",
            "outcome_kind": "failure_note",
            "empirical_execution": False,
        }
    )

    assert result["status"] == "ok"
    assert result["blog_push_status"] == "skipped"
    assert result["blog_post_path"] == ""


def test_healthy_manual_research_run_emits_no_protocol_violation(monkeypatch) -> None:
    _force_delivery_ack(monkeypatch, True)
    task_id = f"test-no-violation-{uuid4().hex[:8]}"

    summary = run_research_unit(
        task_id=task_id,
        unit_date="2026-03-11",
        scenario="daily",
        max_steps=24,
        chat_id="test-chat",
        research_mode="manual",
        question="Does a fixed-rate perceptron outperform a majority baseline under mild label noise?",
        hypothesis="Perceptron should improve balanced accuracy but lose stability as noise rises.",
        baseline="majority-class baseline",
        single_change="replace the constant classifier with a fixed-rate perceptron",
    )

    task = SSOTStore(research_flow.ROOT / "ssot").get_task(task_id) or {}
    events = load_events(str(task.get("correlation_id", "")).strip())
    assert summary["research_phase"] == "done"
    assert not [event for event in events if str(event.get("event_type", "")).strip() == "PROTOCOL_VIOLATION"]
