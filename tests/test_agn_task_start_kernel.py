from __future__ import annotations

from pathlib import Path

from agn.governance.task_start_kernel import build_task_start_kernel


ROOT = Path(__file__).resolve().parents[1]
HOST_FIXTURE = ROOT / "testing" / "fixtures" / "federated_host_state" / "macbook_air.json"


def test_task_start_kernel_unifies_host_memory_and_tool_cards(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_HOST_STATE_PATH", str(HOST_FIXTURE))
    monkeypatch.setenv("HOME", str(tmp_path))
    browser_use_bin = tmp_path / ".browser-use-env" / "bin" / "browser-use"
    browser_use_bin.parent.mkdir(parents=True, exist_ok=True)
    browser_use_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    payload = build_task_start_kernel(
        task_summary="Use browser-use on my logged-in Chrome to monitor Twitter AI news.",
        risk_level="medium",
        snapshot={
            "system_mode": {"mode": "normal"},
            "lifecycle": {"status": "running"},
            "provider_summary": {"claude": True, "gemini": True, "deepseek": False, "qwen_local": False},
        },
        needs_desktop=True,
    )
    assert payload["schema_version"] == "agn.task_start_kernel.v1"
    assert payload["package_path"] == "agn.governance.task_start_kernel"
    assert payload["legacy_script_shim"] == "scripts/agn_task_start_kernel.py"
    assert payload["host_info"]["host_identity"]["host_id"]
    assert payload["memory_recall"]["query"]["task_type"] == "social_monitoring"
    assert payload["tool_reality_cards"]
    assert payload["summary"]["provider_count"] == 2
    assert payload["summary"]["status"] in {"ready", "attention"}
