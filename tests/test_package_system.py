from __future__ import annotations

from agn.governance import system as agn2


def test_package_system_exposes_metadata() -> None:
    assert agn2.PACKAGE_PATH == "agn.governance.system"
    assert agn2.LEGACY_SCRIPT_SHIM == "scripts/agn2_system.py"


def test_package_system_control_plane_status_shape() -> None:
    payload = agn2._control_plane_status()
    assert "root" in payload
    assert "tauri_cli_available" in payload
