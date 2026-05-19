from __future__ import annotations

from pathlib import Path

from agn.governance import read_models as rm


def test_package_read_models_exposes_metadata_constants() -> None:
    assert rm.PACKAGE_PATH == "agn.governance.read_models"
    assert rm.LEGACY_SCRIPT_SHIM == "scripts/control_plane_read_model.py"


def test_package_read_models_can_build_execution_discipline_without_preflight(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    payload = rm.build_execution_discipline_model(
        {
            "provider_policy": {"reviewer_policy": {"preferred_order": ["claude", "gemini"]}},
            "surface_taxonomy": {"authority_control": ["control_plane"]},
        }
    )
    assert payload["has_preflight"] is False
    assert payload["status"] == "missing_preflight"
