from __future__ import annotations

from agn.governance import operator_brief as brief


def test_package_operator_brief_exposes_package_metadata() -> None:
    payload = brief.build_operator_brief(
        task_summary="Inspect package migration state",
        risk_level="medium",
        system_snapshot={"lifecycle": {"status": "running"}, "system_mode": {"mode": "normal"}},
        execution_checks=[],
        task_start_kernel={
            "summary": {"host_readiness": "ready"},
            "memory_recall": {"priors": []},
            "tool_reality_cards": [],
            "host_info": {
                "host_identity": {"host_id": "macbook"},
                "freshness": {"status": "fresh"},
                "task_readiness": {"status": "ready"},
            },
        },
        recommended_surfaces=[],
    )
    assert payload["package_path"] == "agn.governance.operator_brief"
    assert payload["legacy_script_shim"] == "scripts/agn_operator_brief.py"
