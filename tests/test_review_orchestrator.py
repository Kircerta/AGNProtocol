from __future__ import annotations

from pathlib import Path

from scripts import review_orchestrator as ro


def test_run_review_parses_structured_provider_outputs(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "target.py"
    target.write_text("print('hello')\n", encoding="utf-8")

    def fake_parallel(**_kwargs):
        return {
            "claude": {
                "state": "completed",
                "returncode": 0,
                "stdout": '{"verdict":"revise","confidence":"high","core_reasoning":["Need one fix"],"risks":["stability drift"],"missing_evidence":["test run"],"recommended_action":["add regression test"],"escalate_to_human":false}',
                "stderr": "",
                "timed_out": False,
                "duration_ms": 1.0,
            },
            "gemini": {
                "state": "completed",
                "returncode": 0,
                "stdout": '{"verdict":"reject","confidence":"medium","core_reasoning":["Still unsafe"],"risks":["runtime failure"],"missing_evidence":[],"recommended_action":["rework handler"],"escalate_to_human":false}',
                "stderr": "",
                "timed_out": False,
                "duration_ms": 1.0,
            },
        }

    monkeypatch.setattr(ro, "_run_parallel_with_logging", fake_parallel)
    payload = ro.run_review(
        file_path=target,
        include_dir=tmp_path,
        review_goal="Review the file",
        extra_context="",
        claude_model="opus",
        gemini_model="pro",
        excerpt_chars=2000,
        timeout_sec=30.0,
        max_rounds=2,
    )
    assert payload["status"] == "completed"
    assert payload["overall_verdict"]["verdict"] == "reject"
    assert payload["round_policy"]["max_rounds"] == 2
    assert payload["providers"]["claude"]["structured_verdict"]["verdict"] == "revise"
