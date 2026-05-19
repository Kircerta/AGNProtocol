from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import coordinator_ingest
from agent_runner import dispatch_path
from pointer_protocol import TASKS_DIR


def _clean_task_files(task_id: str) -> None:
    dpath = dispatch_path(task_id)
    if dpath.exists():
        dpath.unlink()
    workspace_task = TASKS_DIR / task_id
    if workspace_task.exists():
        shutil.rmtree(workspace_task)


def test_coordinator_ingest_emits_pointer_dispatch_artifacts() -> None:
    task_id = "test-pointer-dispatch-1"
    _clean_task_files(task_id)

    result = coordinator_ingest.run(
        task_id=task_id,
        request_text="fix pointer dispatch integration",
        source="test",
        correlation_id=None,
        criteria_json=None,
        criterion_items=[],
        task_kind="protocol",
        repo_path="",
        work_branch="",
        executor_provider="codex",
        reviewer_provider="gemini",
        chat_id="",
        message_id="",
        risk_level="low",
        side_effect_level="read_only",
        attempt=1,
    )

    assert result["ok"] is True
    dispatch_file = dispatch_path(task_id)
    assert dispatch_file.exists()

    payload = json.loads(dispatch_file.read_text(encoding="utf-8"))
    assert payload.get("lazy_loading_protocol") == "pointer_v1"

    refs = payload.get("artifact_refs")
    assert isinstance(refs, list)
    assert len(refs) >= 1

    first = refs[0]
    assert isinstance(first, dict)
    assert first.get("artifact_id") == "instructions"
    assert str(first.get("ref", "")).startswith("agn://artifact/")

    manifest_path = TASKS_DIR / task_id / "attempt_1" / "manifest.json"
    assert manifest_path.exists()
