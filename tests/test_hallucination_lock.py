from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]


def _run(
    cmd: list[str],
    timeout: float = 30.0,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )


def test_hallucination_lock_halts_dispatch_after_retries() -> None:
    task_id = f"test-lock-{uuid4().hex[:10]}"

    for attempt in (1, 2, 3):
        ingest = _run(
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
                f"lock retry attempt {attempt}",
                "--attempt",
                str(attempt),
            ]
        )
        assert ingest.returncode == 0

        exec_once = _run(
            [
                sys.executable,
                "scripts/executor_worker.py",
                "--once",
                "--mode",
                "real",
                "--task-id",
                task_id,
            ]
        )
        assert exec_once.returncode == 0

        review_once = _run(
            [
                sys.executable,
                "scripts/reviewer_worker.py",
                "--once",
                "--mode",
                "real",
                "--task-id",
                task_id,
            ],
            extra_env={"AGN_FAKE_REVIEWER_MODE": "always_reject"},
        )
        assert review_once.returncode == 0

    task_file = ROOT / "ssot" / f"{task_id}.json"
    assert task_file.exists()
    task_payload = json.loads(task_file.read_text(encoding="utf-8"))
    assert task_payload.get("lock_state") == "halted"
    assert int(task_payload.get("qa_retry_count", 0) or 0) >= 3
    assert str(task_payload.get("locked_at", "")).strip()

    dispatch_file = ROOT / "dispatch" / f"{task_id}.json"
    if dispatch_file.exists():
        dispatch_file.unlink()

    loop_once = _run([sys.executable, "scripts/coordinator_loop.py", "--once"])
    assert loop_once.returncode == 0
    assert not dispatch_file.exists()

    seen_lock_event = False
    with (ROOT / "audit" / "events.jsonl").open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("task_id") != task_id:
                continue
            if event.get("action") == "hallucination_lock_triggered":
                seen_lock_event = True
                break
    assert seen_lock_event
