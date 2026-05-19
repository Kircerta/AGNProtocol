from __future__ import annotations

from pathlib import Path

from agn.core import emergency_stop as es


def test_package_emergency_stop_exposes_metadata() -> None:
    assert es.PACKAGE_PATH == "agn.core.emergency_stop"
    assert es.LEGACY_SCRIPT_SHIM == "scripts/emergency_stop.py"


def test_package_initialize_system_mode_creates_signed_normal_mode_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    payload = es.initialize_system_mode(issuer="agn2_system", reason="bootstrap")
    assert payload["dispatcher_accepts_new_work"] is True
    assert payload["updated_at"]
    mode_path = tmp_path / "runtime" / "admin_control" / "system_mode.json"
    assert mode_path.exists()
