from __future__ import annotations

import json
from pathlib import Path

from agn.governance import reconstruction_status as ars


def test_build_reconstruction_status_reports_current_phase(monkeypatch, tmp_path: Path) -> None:
    tracker_path = tmp_path / "reconstruction_tracker.json"
    tracker_path.write_text(
        json.dumps(
            {
                "program": {"program_id": "agn2_reconstruction", "active_phase_id": "phase_2"},
                "phases": [
                    {"phase_id": "phase_1", "display_name": "Phase 1", "status": "completed", "summary": "done", "completion_commit": "abc"},
                    {"phase_id": "phase_2", "display_name": "Phase 2", "status": "next", "summary": "next boundary"},
                ],
                "milestone_log": [{"date": "2026-03-23", "title": "foundation", "commit": "abc"}],
                "component_classes": [{"class_id": "system_core"}],
                "host_caveats": [{"id": "tauri_remote_host"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ars, "TRACKER_PATH", tracker_path)

    payload = ars.build_reconstruction_status()
    assert payload["schema_version"] == "agn.reconstruction_status.v1"
    assert payload["package_path"] == "agn.governance.reconstruction_status"
    assert payload["legacy_script_shim"] == "scripts/agn_reconstruction_status.py"
    assert payload["current_phase"]["phase_id"] == "phase_2"
    assert payload["phase_counts"]["completed"] == 1
    assert payload["milestone_count"] == 1


def test_reconstruction_next_step_tracks_phase_semantics(monkeypatch) -> None:
    monkeypatch.setattr(
        ars,
        "build_reconstruction_status",
        lambda: {
            "current_phase": {
                "phase_id": "phase_2_governance_enforcement_boundary",
                "summary": "narrow execution boundaries",
            }
        },
    )
    payload = ars.recommend_next_step()
    assert payload["phase_id"] == "phase_2_governance_enforcement_boundary"
    assert "dispatcher-owned boundaries" in payload["next_step"]
