from __future__ import annotations

from scripts import council_review as cr


def test_council_requires_unanimous_approve(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    approved_case = cr.create_council_case(
        {
            "trace_id": "trace-council-1",
            "task_id": "task-council-1",
            "reason": "critical action",
            "reviewers": ["gemini", "deepseek", "codex"],
        }
    )
    for reviewer in approved_case["reviewers"]:
        cr.append_council_verdict(
            approved_case["case_id"],
            {
                "reviewer": reviewer,
                "verdict": "approve",
                "confidence": "high",
                "core_reasoning": ["looks sound"],
                "risks": [],
                "missing_evidence": [],
                "recommended_action": ["proceed"],
                "escalate_to_human": False,
            },
        )
    approved = cr.aggregate_council_case(approved_case["case_id"])
    assert approved["decision"] == "approved"

    rejected_case = cr.create_council_case(
        {
            "trace_id": "trace-council-2",
            "task_id": "task-council-2",
            "reason": "critical action",
            "reviewers": ["gemini", "deepseek", "codex"],
        }
    )
    verdicts = [
        ("gemini", "approve"),
        ("deepseek", "reject"),
        ("codex", "approve"),
    ]
    for reviewer, verdict in verdicts:
        cr.append_council_verdict(
            rejected_case["case_id"],
            {
                "reviewer": reviewer,
                "verdict": verdict,
                "confidence": "medium",
                "core_reasoning": ["checked"],
                "risks": [] if verdict == "approve" else ["insufficient evidence"],
                "missing_evidence": [],
                "recommended_action": ["escalate"] if verdict != "approve" else ["proceed"],
                "escalate_to_human": verdict != "approve",
            },
        )
    escalated = cr.aggregate_council_case(rejected_case["case_id"])
    assert escalated["decision"] == "escalate_to_admin"
