from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_KEYS = {
    "ok",
    "task_id",
    "attempt",
    "decision",
    "commit_hash",
    "no_change_reason",
    "result_path",
    "verdict_path",
    "fail_reasons",
}


def test_run_agn_task_stdout_is_single_json_object_contract() -> None:
    task_id = f"test-run-agn-task-{uuid4().hex[:10]}"
    payload = {
        "task_id": task_id,
        "task_kind": "protocol",
        "source": "openclaw",
        "request_text": "one shot contract check",
    }
    proc = subprocess.run(
        [sys.executable, "scripts/run_agn_task.py", "--from-stdin"],
        cwd=str(ROOT),
        input=json.dumps(payload, ensure_ascii=True),
        text=True,
        capture_output=True,
        timeout=30.0,
    )

    assert proc.returncode == 0
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    decoded = json.loads(lines[0])
    assert EXPECTED_KEYS.issubset(decoded.keys())
    assert decoded["task_id"] == task_id
    assert isinstance(decoded["fail_reasons"], list)
