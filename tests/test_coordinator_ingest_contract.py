from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]


def test_repo_task_requires_repo_context() -> None:
    task_id = f"test-ingest-repo-missing-{uuid4().hex[:10]}"
    dispatch_file = ROOT / "dispatch" / f"{task_id}.json"
    if dispatch_file.exists():
        dispatch_file.unlink()

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/coordinator_ingest.py",
            "--task-id",
            task_id,
            "--task-kind",
            "repo",
            "--request-text",
            "repo task missing context",
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=20.0,
    )

    assert proc.returncode == 1
    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is False
    assert "repo task missing repo_path" in payload["error"]
    assert not dispatch_file.exists()


def test_reingest_same_task_id_reuses_existing_correlation() -> None:
    task_id = f"test-ingest-reuse-{uuid4().hex[:10]}"
    first = subprocess.run(
        [
            sys.executable,
            "scripts/coordinator_ingest.py",
            "--task-id",
            task_id,
            "--task-kind",
            "protocol",
            "--request-text",
            "first ingest",
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=20.0,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    first_payload = json.loads(first.stdout.strip())
    corr = str(first_payload.get("correlation_id", ""))
    assert corr

    second = subprocess.run(
        [
            sys.executable,
            "scripts/coordinator_ingest.py",
            "--task-id",
            task_id,
            "--task-kind",
            "protocol",
            "--request-text",
            "second ingest",
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=20.0,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    second_payload = json.loads(second.stdout.strip())
    assert str(second_payload.get("correlation_id", "")) == corr
