from __future__ import annotations

import json
from pathlib import Path

from agn.governance.execution_workflow import build_preflight_payload
from scripts.agn_memory_recall import query_memory_recall


ROOT = Path(__file__).resolve().parents[1]
HOST_FIXTURE = ROOT / "testing" / "fixtures" / "federated_host_state" / "macbook_air.json"


def test_host_failure_prior_is_recalled_and_confirmed_by_runtime(monkeypatch) -> None:
    monkeypatch.setenv("AGN_HOST_STATE_PATH", str(HOST_FIXTURE))
    recall = query_memory_recall(
        task_summary="Use qwen_local and gui-agent on this Example Laptop to fix Ghostty setup.",
        task_type="desktop_gui",
        tools=["gui_agent"],
        providers=["qwen_local"],
    )
    host_prior = next(item for item in recall["priors"] if item["id"] == "host.macbook_air_portable.boundaries")
    assert host_prior["runtime_relation"] == "confirmed_by_runtime"
    assert recall["runtime_priority_notice"].startswith("Runtime facts remain authoritative")


def test_browser_use_boundary_is_recalled_without_overriding_runtime(monkeypatch) -> None:
    monkeypatch.setenv("AGN_HOST_STATE_PATH", str(HOST_FIXTURE))
    recall = query_memory_recall(
        task_summary="Use browser-use on my logged-in Chrome to monitor Twitter AI news.",
        task_type="social_monitoring",
        tools=["browser-use"],
    )
    boundary = next(item for item in recall["priors"] if item["id"] == "tool.browser_use.logged_in_chrome_boundary")
    assert boundary["runtime_relation"] == "memory_only"
    assert "supports persistent daemon sessions" in boundary["summary"]


def test_preflight_includes_memory_recall(monkeypatch) -> None:
    monkeypatch.setenv("AGN_HOST_STATE_PATH", str(HOST_FIXTURE))
    snapshot = {
        "system_mode": {"mode": "normal"},
        "lifecycle": {"status": "running"},
        "provider_summary": {
            "qwen_local": False,
            "deepseek": False,
            "gemini": False,
            "claude": True,
        },
    }
    payload = build_preflight_payload(
        task_summary="Use browser-use on my logged-in Chrome to monitor Twitter AI news.",
        risk_level="medium",
        task_id="task-1",
        trace_id="trace-1",
        subsystem="agn2",
        needs_control_plane=False,
        needs_desktop=True,
        needs_history=False,
        needs_worker=False,
        needs_review=False,
        snapshot=snapshot,
    )
    assert payload["memory_recall"]["query"]["task_type"] == "social_monitoring"
    assert payload["memory_recall"]["runtime_priority_notice"].startswith("Runtime facts remain authoritative")
    memory_check = next(item for item in payload["execution_checks"] if item["check"] == "memory_recall")
    assert memory_check["status"] == "ok"
    assert payload["operator_brief"]["status"] in {"ready", "attention"}
