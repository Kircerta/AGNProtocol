from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agent_runner import _build_reviewer_compact_payload, _build_reviewer_prompt


def test_reviewer_prompt_uses_compact_payload_and_context_ref() -> None:
    dispatch = {
        "task_id": "t-1",
        "attempt": 2,
        "acceptance_criteria": [{"id": "AC-1", "text": "ok"}],
        "artifact_refs": [{"artifact_id": "instructions", "ref": "agn://artifact/" + "a" * 64}],
    }
    result_payload = {
        "task_id": "t-1",
        "attempt": 2,
        "commit_hash": "abc123",
        "no_change_reason": "",
        "diff_snapshot": "diff --git a b\n" + ("x" * 6000),
        "commands_ran": [{"command": "echo 1"}],
        "work_log": [{"op": "x"}] * 20,
        "artifact_refs": [{"artifact_id": "execution_log", "ref": "agn://artifact/" + "b" * 64}],
    }

    compact = _build_reviewer_compact_payload(dispatch=dispatch, result_payload=result_payload)
    prompt = _build_reviewer_prompt(compact_payload=compact, context_ref="agn://artifact/" + "c" * 64)

    assert "review_context_ref=" in prompt
    assert "pointer_v1" in prompt
    assert len(compact["result"]["work_log_excerpt"]) == 5
    assert len(compact["result"]["diff_snapshot"]) < 1000
