from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]


def test_protocol_task_uses_fake_path_even_in_real_mode() -> None:
    task_id = f"test-task-kind-protocol-{uuid4().hex[:10]}"
    result_file = ROOT / "results" / f"{task_id}.1.json"
    verdict_file = ROOT / "verdicts" / f"{task_id}.1.json"
    for path in (result_file, verdict_file):
        if path.exists():
            path.unlink()

    ingest_proc = subprocess.run(
        [
            sys.executable,
            "scripts/coordinator_ingest.py",
            "--task-id",
            task_id,
            "--task-kind",
            "protocol",
            "--source",
            "agn_smoke",
            "--request-text",
            "protocol route regression",
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=20.0,
    )
    assert ingest_proc.returncode == 0

    exec_proc = subprocess.run(
        [
            sys.executable,
            "scripts/executor_worker.py",
            "--once",
            "--mode",
            "real",
            "--task-id",
            task_id,
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=30.0,
    )
    assert exec_proc.returncode == 0
    assert result_file.exists()
    result_payload = json.loads(result_file.read_text(encoding="utf-8"))
    work_log = result_payload.get("work_log", [])
    assert isinstance(work_log, list)
    assert len(work_log) > 0
    assert str(work_log[0].get("op", "")).startswith("operation_")

    review_proc = subprocess.run(
        [
            sys.executable,
            "scripts/reviewer_worker.py",
            "--once",
            "--mode",
            "real",
            "--task-id",
            task_id,
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=30.0,
    )
    assert review_proc.returncode == 0
    assert verdict_file.exists()
