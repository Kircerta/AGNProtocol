from __future__ import annotations

from pathlib import Path

from agn.governance import execution_workflow as workflow


def test_build_preflight_payload_promotes_rich_execution_surfaces() -> None:
    payload = workflow.build_preflight_payload(
        task_summary="Inspect a risky GUI state transition",
        risk_level="high",
        task_id="task-1",
        trace_id="trace-1",
        subsystem="agn2",
        needs_control_plane=True,
        needs_desktop=True,
        needs_history=True,
        needs_worker=True,
        needs_review=True,
        snapshot={
            "system_mode": {"mode": "normal"},
            "lifecycle": {"status": "running"},
            "provider_summary": {"qwen_local": True, "deepseek": True, "gemini": True, "claude": False},
            "control_plane_app_exists": True,
            "conversation_monitor_app_exists": True,
            "gui_agent_exists": True,
            "ghostty_available": True,
        },
    )
    surfaces = {item["surface"] for item in payload["recommended_surfaces"]}
    assert "agn2_system" in surfaces
    assert "control_plane" in surfaces
    assert "ghostty_workspace" in surfaces
    assert "conversation_monitor" in surfaces
    assert "worker_delegate" in surfaces
    assert "flagship_review" in surfaces
    assert payload["task_start_kernel"]["schema_version"] == "agn.task_start_kernel.v1"
    assert payload["memory_recall"] == payload["task_start_kernel"]["memory_recall"]
    assert payload["host_info"] == payload["task_start_kernel"]["host_info"]
    assert payload["operator_brief"]["status"] in {"ready", "attention"}


def test_build_ghostty_workspace_commands_default_to_dry_run(tmp_path: Path) -> None:
    commands = workflow.build_ghostty_workspace_commands(cwd=tmp_path, execute=False, plain_shells=False)
    assert len(commands) == 4
    assert commands[0][2] == "new-window"
    assert commands[1][2] == "new-tab"
    assert all("--dry-run" in command for command in commands)
    assert any("--input" in command for command in commands)


def test_build_delegate_request_keeps_worker_scope_bounded() -> None:
    request = workflow.build_delegate_request(
        instruction="Extract the TODO labels from these notes",
        task_profile="general_analysis",
        risk_level="low",
        input_refs=["agn://artifact/" + "a" * 64],
        output_expectation="Return a flat bullet list.",
        task_id="delegate-1",
    )
    assert request["task_id"] == "delegate-1"
    assert request["task_profile"] == "general_analysis"
    assert request["risk_level"] == "low"
    assert request["metadata"]["worker_only"] is True
    assert "\"system_constraints\"" in request["prompt"]
    assert "\"user_instruction\": \"Extract the TODO labels from these notes\"" in request["prompt"]


def test_build_preflight_payload_refreshes_host_info(monkeypatch) -> None:
    seen: dict[str, bool] = {}

    def fake_kernel(**kwargs):
        seen["refresh_host_info"] = bool(kwargs.get("refresh_host_info"))
        return {
            "schema_version": "agn.task_start_kernel.v1",
            "runtime_snapshot": {"provider_summary": {"claude": True, "gemini": True, "deepseek": False, "qwen_local": False}},
            "memory_recall": {"priors": [], "tool_reality_cards": []},
            "host_info": {"task_readiness": {"status": "ready"}, "freshness": {"status": "fresh"}, "host_identity": {"host_id": "macbook"}},
            "summary": {"host_readiness": "ready"},
            "tool_reality_cards": [],
        }

    monkeypatch.setattr(workflow, "build_task_start_kernel", fake_kernel)

    payload = workflow.build_preflight_payload(
        task_summary="Refresh host info for preflight",
        risk_level="medium",
        task_id="task-refresh",
        trace_id="trace-refresh",
        subsystem="agn2",
        needs_control_plane=False,
        needs_desktop=False,
        needs_history=False,
        needs_worker=False,
        needs_review=False,
        snapshot={
            "system_mode": {"mode": "normal"},
            "lifecycle": {"status": "running"},
            "provider_summary": {"qwen_local": False, "deepseek": False, "gemini": True, "claude": True},
            "control_plane_app_exists": False,
            "conversation_monitor_app_exists": False,
            "gui_agent_exists": False,
            "ghostty_available": True,
        },
    )

    assert seen["refresh_host_info"] is True
    assert payload["task_start_kernel"]["schema_version"] == "agn.task_start_kernel.v1"
