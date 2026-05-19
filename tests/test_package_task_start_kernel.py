from __future__ import annotations

from pathlib import Path

from agn.governance import task_start_kernel as kernel


ROOT = Path(__file__).resolve().parents[1]
HOST_FIXTURE = ROOT / "testing" / "fixtures" / "federated_host_state" / "macbook_air.json"


def test_package_task_start_kernel_exposes_package_metadata(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_HOST_STATE_PATH", str(HOST_FIXTURE))
    monkeypatch.setenv("HOME", str(tmp_path))
    browser_use_bin = tmp_path / ".browser-use-env" / "bin" / "browser-use"
    browser_use_bin.parent.mkdir(parents=True, exist_ok=True)
    browser_use_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    payload = kernel.build_task_start_kernel(
        task_summary="Inspect package migration state",
        risk_level="medium",
        snapshot={
            "system_mode": {"mode": "normal"},
            "lifecycle": {"status": "running"},
            "provider_summary": {"claude": True, "gemini": False, "deepseek": False, "qwen_local": False},
        },
    )
    assert payload["package_path"] == "agn.governance.task_start_kernel"
    assert payload["legacy_script_shim"] == "scripts/agn_task_start_kernel.py"
