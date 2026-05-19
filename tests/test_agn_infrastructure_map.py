from __future__ import annotations

from pathlib import Path

from agn.architecture import infrastructure_map as aim


def test_infrastructure_map_exposes_active_and_compatibility_districts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        aim,
        "build_capability_snapshot",
        lambda: {
            "surfaces": {
                "lifecycle": {"available": True, "entry": "python3 scripts/agn2_system.py status"},
                "control_plane": {"available": True, "entry": "open '/Applications/AGN2.0 Control Plane.app'"},
                "host_info": {"available": True, "entry": "python3 scripts/agn_host_info.py show"},
                "task_start_kernel": {"available": True, "entry": "python3 scripts/agn_task_start_kernel.py build --task-summary \"...\""},
                "operator_brief": {"available": True, "entry": "python3 scripts/agn_operator_brief.py build --task-summary \"...\""},
                "evolution_pipeline": {"available": True, "entry": "python3 scripts/agn_evolution_pipeline.py show"},
                "reconstruction_status": {"available": True, "entry": "python3 scripts/agn_reconstruction_status.py show"},
                "governed_execution_gateway": {"available": True, "entry": "python3 scripts/agn_governed_execution.py"},
                "dispatcher": {"available": True, "entry": "python3 scripts/dispatcher_runtime.py dispatch --from-json-file <request.json>"},
                "worker_delegate": {"available": True, "entry": "python3 scripts/agn2_execution_workflow.py delegate --instruction \"...\""},
                "flagship_review": {"available": True, "entry": "python3 scripts/agn2_execution_workflow.py review --file <path>"},
                "desktop_control": {"available": False, "entry": "python3 scripts/desktop_adapter.py"},
                "external_toolbox": {"available": True, "entry": "python3 scripts/agn_external_toolbox.py list"},
                "tool_reality_cards": {"available": True, "entry": "python3 scripts/agn_tool_reality_cards.py build"},
            }
        },
    )
    payload = aim.build_infrastructure_map()
    assert payload["package_path"] == "agn.architecture.infrastructure_map"
    assert payload["legacy_script_shim"] == "scripts/agn_infrastructure_map.py"
    districts = {item["district_id"] for item in payload["districts"]}
    assert "task_start" in districts
    assert "evolution" in districts
    assert "compatibility" in districts

    modules = {item["module_id"]: item for item in payload["modules"]}
    assert modules["task_start_kernel"]["status"] == "active"
    assert modules["evolution_pipeline"]["status"] == "active"
    assert modules["reconstruction_status"]["status"] == "active"
    assert modules["governed_execution_gateway"]["status"] == "active"
    assert modules["agn_runtime_router"]["status"] == "compat_paused"


def test_infrastructure_recommendation_prefers_active_browser_path(monkeypatch) -> None:
    monkeypatch.setattr(
        aim,
        "build_infrastructure_map",
        lambda: {
            "modules": [
                {"module_id": "task_start_kernel", "display_name": "Task-Start Kernel", "district": "task_start", "resolved_entry": "kernel", "status": "active", "available": True},
                {"module_id": "preflight", "display_name": "Preflight", "district": "task_start", "resolved_entry": "preflight", "status": "active", "available": True},
                {"module_id": "host_info", "display_name": "Host Info", "district": "task_start", "resolved_entry": "host", "status": "active", "available": True},
                {"module_id": "browser_use_wrapper", "display_name": "Browser Use Wrapper", "district": "observation", "resolved_entry": "browser", "status": "active", "available": True},
                {"module_id": "governed_execution_gateway", "display_name": "Governed Execution Gateway", "district": "execution", "resolved_entry": "gateway", "status": "active", "available": True},
                {"module_id": "tool_reality_cards", "display_name": "Tool Reality Cards", "district": "memory", "resolved_entry": "cards", "status": "active", "available": True},
                {"module_id": "evolution_pipeline", "display_name": "Evolution Pipeline", "district": "evolution", "resolved_entry": "evolve", "status": "active", "available": True},
                {"module_id": "reconstruction_status", "display_name": "Reconstruction Status", "district": "evolution", "resolved_entry": "reconstruct", "status": "active", "available": True},
                {"module_id": "agn_runtime_router", "display_name": "Runtime Router", "district": "compatibility", "resolved_entry": "legacy", "status": "compat_paused", "available": True},
            ]
        },
    )
    payload = aim.recommend_modules(task_summary="Continue the AGN restructuring, integrate a new GitHub browser automation repo, and inspect Twitter AI news in Chrome.")
    assert payload["package_path"] == "agn.architecture.infrastructure_map"
    module_ids = [item["module_id"] for item in payload["recommendations"]]
    assert "task_start_kernel" in module_ids
    assert "browser_use_wrapper" in module_ids
    assert "governed_execution_gateway" in module_ids
    assert "evolution_pipeline" in module_ids
    assert "reconstruction_status" in module_ids
    assert any(item["module_id"] == "agn_runtime_router" for item in payload["avoid_by_default"])
