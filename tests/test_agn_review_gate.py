from __future__ import annotations

from scripts.agn_review_gate import build_payload


def test_review_gate_requires_review_for_architecture_and_uncertainty() -> None:
    payload = build_payload(
        task_summary="Architecture decision is still unclear and needs external audit",
        risk_level="medium",
        change_scope="architecture",
        uncertainty="high",
        local_verification_available=False,
        mechanical_task=False,
        root_cause_unclear=True,
        before_human_approval=False,
        experiment_results_need_review=False,
        file_path="scripts/dispatcher_runtime.py",
        review_goal="Review this file.",
    )
    assert payload["verdict"] == "required"
    assert payload["reviewer_lane"] in {"claude", "gemini", ""}


def test_review_gate_forbids_mechanical_low_risk_change() -> None:
    payload = build_payload(
        task_summary="Small mechanical rename after local test already passes",
        risk_level="low",
        change_scope="local",
        uncertainty="low",
        local_verification_available=True,
        mechanical_task=True,
        root_cause_unclear=False,
        before_human_approval=False,
        experiment_results_need_review=False,
        file_path="",
        review_goal="",
    )
    assert payload["verdict"] == "forbidden"
    assert any("abort" in item.lower() for item in payload["abort_semantics"])
