from __future__ import annotations

from scripts.agn_automation_designer import build_payload


def test_automation_designer_detects_monitoring_candidate() -> None:
    payload = build_payload(
        task_summary="Monitor AGN status and capability drift every few hours and write a brief report",
        cadence="auto",
        interval_hours=4,
        weekday="MON",
        time_hhmm="09:00",
        workspaces=["<repo-root>"],
        deliverable="/tmp/agn-monitor.md",
        gating_rules=["runtime/admin_control/read_models/system_status.json exists"],
        status="ACTIVE",
    )
    assert payload["automation_candidate"] is True
    assert payload["classification"] == "monitoring"
    assert payload["automation_spec"]["rrule"].startswith("FREQ=HOURLY")


def test_automation_designer_blocks_high_trust_architecture_task() -> None:
    payload = build_payload(
        task_summary="Architecture governance review and final decision for a one-off control-plane redesign",
        cadence="auto",
        interval_hours=4,
        weekday="MON",
        time_hhmm="09:00",
        workspaces=[],
        deliverable="",
        gating_rules=[],
        status="ACTIVE",
    )
    assert payload["automation_candidate"] is False
    assert payload["blockers"]
