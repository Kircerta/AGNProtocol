from __future__ import annotations

from pathlib import Path

from agn.runtime import host_info


ROOT = Path(__file__).resolve().parents[1]
HOST_FIXTURE = ROOT / "testing" / "fixtures" / "federated_host_state" / "macbook_air.json"


def test_package_host_info_exposes_package_metadata(monkeypatch) -> None:
    monkeypatch.setenv("AGN_HOST_STATE_PATH", str(HOST_FIXTURE))
    payload = host_info.build_host_info(task_summary="Inspect host info package migration", refresh=False)
    assert payload["package_path"] == "agn.runtime.host_info"
    assert payload["legacy_script_shim"] == "scripts/agn_host_info.py"
