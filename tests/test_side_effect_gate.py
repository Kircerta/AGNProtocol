from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]


def test_external_publish_requires_admin_approval(tmp_path: Path) -> None:
    task_id = f"test-side-effect-gate-{uuid4().hex[:10]}"
    repo_path = tmp_path / "repo"
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True, text=True)

    ingest_proc = subprocess.run(
        [
            sys.executable,
            "scripts/coordinator_ingest.py",
            "--task-id",
            task_id,
            "--task-kind",
            "repo",
            "--request-text",
            "attempt external publish",
            "--repo-path",
            str(repo_path),
            "--work-branch",
            "codex/test-side-effect",
            "--side-effect-level",
            "external_publish",
            "--risk-level",
            "high",
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

    result_file = ROOT / "results" / f"{task_id}.1.json"
    assert result_file.exists()
    result_payload = json.loads(result_file.read_text(encoding="utf-8"))
    fail_reasons = result_payload.get("fail_reasons", [])
    assert "external_publish_not_approved" in fail_reasons

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

    verdict_file = ROOT / "verdicts" / f"{task_id}.1.json"
    assert verdict_file.exists()
    verdict_payload = json.loads(verdict_file.read_text(encoding="utf-8"))
    assert verdict_payload.get("decision") == "reject"
    issues = verdict_payload.get("issues", [])
    assert isinstance(issues, list) and len(issues) >= 1
    assert "external_publish_not_approved" in json.dumps(issues, ensure_ascii=True)

    side_effect_events = []
    with (ROOT / "audit" / "events.jsonl").open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("task_id") != task_id:
                continue
            if event.get("action") == "side_effect_denied":
                side_effect_events.append(event)
    assert side_effect_events
