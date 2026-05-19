from __future__ import annotations

from agn.governance.operator_brief import build_operator_brief


def test_operator_brief_separates_attention_from_information() -> None:
    payload = build_operator_brief(
        task_summary="Inspect Twitter in Chrome on the current host.",
        risk_level="medium",
        system_snapshot={
            "lifecycle": {"status": "stopped"},
            "system_mode": {"mode": "normal"},
        },
        execution_checks=[
            {"check": "authority_model", "status": "ok", "detail": "authority is intact"},
            {"check": "lifecycle_state", "status": "attention", "detail": "Lifecycle status: stopped"},
            {"check": "memory_recall", "status": "ok", "detail": "consulted"},
        ],
        task_start_kernel={
            "summary": {"host_readiness": "attention"},
            "memory_recall": {
                "priors": [{"id": "one"}, {"id": "two"}],
            },
            "tool_reality_cards": [{"tool_identity": {"tool_id": "browser-use"}}],
            "host_info": {
                "host_identity": {"host_id": "laptop-portable"},
                "freshness": {"status": "stale", "summary": "Current host facts are stale."},
                "task_readiness": {"status": "attention", "summary": "Current host is missing one or more task-specific capabilities."},
            },
        },
        recommended_surfaces=[
            {"surface": "agn2_system", "reason": "canonical status", "entry": "python3 scripts/agn2_system.py status"}
        ],
    )
    assert payload["status"] == "attention"
    assert payload["package_path"] == "agn.governance.operator_brief"
    assert payload["legacy_script_shim"] == "scripts/agn_operator_brief.py"
    assert payload["counts"]["attention"] >= 1
    assert payload["counts"]["informational"] >= 1
    assert payload["top_surfaces"][0]["surface"] == "agn2_system"
