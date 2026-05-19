from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )


def _load_stdout_json(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(completed.stdout)


def test_model_router_cli_blocks_without_internal_ack() -> None:
    completed = _run("scripts/model_router.py", "route", "--from-stdin")
    assert completed.returncode == 2
    payload = _load_stdout_json(completed)
    assert payload["error"] == "direct_handler_cli_requires_explicit_ack"
    assert payload["handler_id"] == "model_router"


def test_review_orchestrator_cli_blocks_without_internal_ack() -> None:
    completed = _run("scripts/review_orchestrator.py", "--file", "README.md")
    assert completed.returncode == 2
    payload = _load_stdout_json(completed)
    assert payload["error"] == "direct_handler_cli_requires_explicit_ack"
    assert payload["handler_id"] == "review_orchestrator"


def test_vision_parser_cli_blocks_without_internal_ack() -> None:
    completed = _run("scripts/vision_parser.py", "--task-id", "vision-task", "--image-ref", "agn://artifact/" + ("a" * 64))
    assert completed.returncode == 2
    payload = _load_stdout_json(completed)
    assert payload["error"] == "direct_handler_cli_requires_explicit_ack"
    assert payload["handler_id"] == "vision_parser"


def test_model_router_cli_runs_with_internal_ack(tmp_path: Path) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(
        json.dumps({"prompt": "normalize this", "task_type": "text_normalization", "response_mode": "text"}),
        encoding="utf-8",
    )
    completed = _run(
        "scripts/model_router.py",
        "--internal-handler-cli",
        "route",
        "--from-json-file",
        str(task_path),
    )
    assert completed.returncode == 0
    payload = _load_stdout_json(completed)
    assert payload["task"]["prompt"] == "normalize this"
    assert "selected_provider" in payload
