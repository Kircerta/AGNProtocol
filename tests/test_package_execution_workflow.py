from __future__ import annotations

from agn.governance import execution_workflow as workflow


def test_package_execution_workflow_exposes_metadata() -> None:
    assert workflow.PACKAGE_PATH == "agn.governance.execution_workflow"
    assert workflow.LEGACY_SCRIPT_SHIM == "scripts/agn2_execution_workflow.py"


def test_package_execution_workflow_can_build_delegate_request() -> None:
    payload = workflow.build_delegate_request(
        instruction="Summarize the bounded worker task.",
        task_profile="general_analysis",
        risk_level="low",
        input_refs=["agn://artifact/" + "b" * 64],
        output_expectation="Return one paragraph.",
        task_id="delegate-package-test",
    )
    assert payload["task_id"] == "delegate-package-test"
    assert payload["metadata"]["worker_only"] is True
