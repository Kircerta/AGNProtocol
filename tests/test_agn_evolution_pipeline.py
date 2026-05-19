from __future__ import annotations

import json
from pathlib import Path

from agn.architecture import evolution_pipeline as aep


def test_build_evolution_pipeline_resolves_touchpoints(monkeypatch, tmp_path: Path) -> None:
    registry_path = tmp_path / "evolution_pipeline_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "integration_tiers": [{"tier_id": "toolbox_catalog"}],
                "pipelines": [
                    {
                        "pipeline_id": "external_intake",
                        "display_name": "External Intake",
                        "purpose": "Assess new repos.",
                        "default_target_tier": "toolbox_catalog",
                        "stages": [{"stage_id": "fit_scan"}],
                        "touchpoints": ["external_toolbox", "infrastructure_map"],
                    }
                ],
                "future_risks": [{"risk_id": "context_pollution"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(aep, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(
        aep,
        "build_infrastructure_map",
        lambda: {
            "modules": [
                {
                    "module_id": "external_toolbox",
                    "display_name": "External Toolbox",
                    "district": "memory",
                    "status": "active",
                    "available": True,
                    "resolved_entry": "python3 scripts/agn_external_toolbox.py list",
                },
                {
                    "module_id": "infrastructure_map",
                    "display_name": "Infrastructure Map",
                    "district": "task_start",
                    "status": "active",
                    "available": True,
                    "resolved_entry": "python3 scripts/agn_infrastructure_map.py show",
                },
            ]
        },
    )
    monkeypatch.setattr(
        aep,
        "build_external_toolbox_inventory",
        lambda: {
            "count": 2,
            "entries": [
                {"name": "browser-use", "mount_mode": "runtime_optional"},
                {"name": "deepagents", "mount_mode": "reference_only"},
            ],
        },
    )

    payload = aep.build_evolution_pipeline()
    assert payload["schema_version"] == "agn.evolution_pipeline.v1"
    assert payload["package_path"] == "agn.architecture.evolution_pipeline"
    assert payload["legacy_script_shim"] == "scripts/agn_evolution_pipeline.py"
    assert payload["current_context"]["external_toolbox_count"] == 2
    assert payload["current_context"]["runtime_optional_mounts"] == ["browser-use"]
    assert payload["pipelines"][0]["touchpoint_summary"]["available_count"] == 2


def test_recommend_pipeline_for_new_repo_integration(monkeypatch) -> None:
    monkeypatch.setattr(
        aep,
        "build_evolution_pipeline",
        lambda: {
            "pipelines": [
                {"pipeline_id": "external_intake", "display_name": "External Intake", "purpose": "Assess", "touchpoints": [{"module_id": "external_toolbox"}]},
                {"pipeline_id": "controlled_fusion", "display_name": "Controlled Fusion", "purpose": "Fuse", "touchpoints": [{"module_id": "tool_reality_cards"}]},
                {"pipeline_id": "governed_upgrade", "display_name": "Governed Upgrade", "purpose": "Upgrade", "touchpoints": [{"module_id": "task_start_kernel"}]},
                {"pipeline_id": "retirement_and_archive", "display_name": "Retirement", "purpose": "Retire", "touchpoints": [{"module_id": "infrastructure_map"}]},
            ]
        },
    )
    payload = aep.recommend_pipeline(change_summary="Integrate a new GitHub browser automation repo into AGN as a wrapper.")
    assert payload["package_path"] == "agn.architecture.evolution_pipeline"
    sequence = [item["pipeline_id"] for item in payload["recommended_sequence"]]
    assert sequence == ["external_intake", "controlled_fusion"]
    assert payload["recommended_target_tier"] == "bounded_wrapper"


def test_recommend_pipeline_for_cleanup_prefers_retirement(monkeypatch) -> None:
    monkeypatch.setattr(
        aep,
        "build_evolution_pipeline",
        lambda: {
            "pipelines": [
                {"pipeline_id": "retirement_and_archive", "display_name": "Retirement", "purpose": "Retire", "touchpoints": [{"module_id": "infrastructure_map"}]},
            ]
        },
    )
    payload = aep.recommend_pipeline(change_summary="Archive this redundant paused legacy module and clean up stale docs.")
    assert payload["primary_pipeline_id"] == "retirement_and_archive"
    assert payload["recommended_target_tier"] == "paused_legacy"
