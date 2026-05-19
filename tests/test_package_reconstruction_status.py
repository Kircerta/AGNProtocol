from __future__ import annotations

from agn.governance import reconstruction_status as rs


def test_package_reconstruction_status_can_recommend_next_step() -> None:
    payload = rs.recommend_next_step(
        {
            "current_phase": {
                "phase_id": "phase_3_gradual_implementation_migration",
                "summary": "move modules gradually",
            }
        }
    )
    assert payload["phase_id"] == "phase_3_gradual_implementation_migration"
    assert "low-dependency module" in payload["next_step"]


def test_package_reconstruction_status_exposes_package_metadata() -> None:
    payload = rs.build_reconstruction_status()
    assert payload["package_path"] == "agn.governance.reconstruction_status"
    assert payload["legacy_script_shim"] == "scripts/agn_reconstruction_status.py"
