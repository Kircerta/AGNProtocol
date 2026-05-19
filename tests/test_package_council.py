from __future__ import annotations

from agn.governance import council as gc


def test_package_council_exposes_metadata() -> None:
    assert gc.PACKAGE_PATH == "agn.governance.council"
    assert gc.LEGACY_SCRIPT_SHIM == "scripts/council_review.py"


def test_package_council_requires_unanimous_approve(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    approved_case = gc.create_council_case(
        {
            "trace_id": "trace-council-1",
            "task_id": "task-council-1",
            "reason": "critical action",
            "reviewers": ["gemini", "deepseek", "codex"],
        }
    )
    for reviewer in approved_case["reviewers"]:
        gc.append_council_verdict(
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
    approved = gc.aggregate_council_case(approved_case["case_id"])
    assert approved["decision"] == "approved"
