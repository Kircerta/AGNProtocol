from __future__ import annotations

from pathlib import Path

from agn.governance.execution_workflow import build_preflight_payload
from agn.runtime.host_info import build_host_info, render_host_info_markdown


ROOT = Path(__file__).resolve().parents[1]
HOST_FIXTURE = ROOT / "testing" / "fixtures" / "federated_host_state" / "macbook_air.json"


def test_build_host_info_reflects_local_dependencies(monkeypatch) -> None:
    monkeypatch.setenv("AGN_HOST_STATE_PATH", str(HOST_FIXTURE))
    payload = build_host_info(task_summary="Use browser-use on Chrome to inspect OpenAI updates.", refresh=False)
    assert payload["package_path"] == "agn.runtime.host_info"
    assert payload["legacy_script_shim"] == "scripts/agn_host_info.py"
    assert payload["host_identity"]["host_id"]
    assert payload["dependencies"]["tools"]["available"]
    assert payload["task_readiness"]["required_capabilities"]


def test_render_host_info_markdown_mentions_single_host_surface(monkeypatch) -> None:
    monkeypatch.setenv("AGN_HOST_STATE_PATH", str(HOST_FIXTURE))
    payload = build_host_info(task_summary="", refresh=False)
    markdown = render_host_info_markdown(payload)
    assert "# HOST_INFO" in markdown
    assert "single active host-context surface" in markdown


def test_preflight_exposes_host_info(monkeypatch) -> None:
    monkeypatch.setenv("AGN_HOST_STATE_PATH", str(HOST_FIXTURE))
    snapshot = {
        "system_mode": {"mode": "normal"},
        "lifecycle": {"status": "running"},
        "provider_summary": {"claude": True, "gemini": False, "deepseek": False, "qwen_local": False},
    }
    payload = build_preflight_payload(
        task_summary="Use browser-use in Chrome to inspect OpenAI updates.",
        risk_level="medium",
        task_id="task-1",
        trace_id="trace-1",
        subsystem="agn2",
        needs_control_plane=False,
        needs_desktop=False,
        needs_history=False,
        needs_worker=False,
        needs_review=False,
        snapshot=snapshot,
    )
    assert payload["host_info"]["host_identity"]["host_id"]
    host_check = next(item for item in payload["execution_checks"] if item["check"] == "host_info")
    assert host_check["status"] in {"ok", "attention"}
