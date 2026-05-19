from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]


def test_listener_rejects_repo_without_repo_context_and_does_not_dispatch() -> None:
    task_id = f"test-tg-gate-{uuid4().hex[:10]}"
    dispatch_file = ROOT / "dispatch" / f"{task_id}.json"
    if dispatch_file.exists():
        dispatch_file.unlink()

    payload = {
        "task_id": task_id,
        "task_kind": "repo",
        "request_text": "fix launch db error",
    }
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/telegram_listener.py",
            "--stdin",
            "--stdin-chat-id",
            "local-test-chat",
            "--stdin-message-id",
            "1",
        ],
        cwd=str(ROOT),
        input=json.dumps(payload, ensure_ascii=True),
        text=True,
        capture_output=True,
        timeout=20.0,
        env={**os.environ, "AGN_TELEGRAM_ADMIN_CHAT_ID": "local-test-chat"},
    )

    assert proc.returncode == 0
    assert "task_kind=repo requires repo_path, work_branch" in proc.stdout
    assert not dispatch_file.exists()


def test_listener_plain_dialogue_is_not_dispatched() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/telegram_listener.py",
            "--stdin",
            "--stdin-chat-id",
            "local-test-chat",
            "--stdin-message-id",
            "2",
        ],
        cwd=str(ROOT),
        input="除了向你发布研究任务外，你还可以做一些什么？",
        text=True,
        capture_output=True,
        timeout=20.0,
        env={**os.environ, "AGN_TELEGRAM_ADMIN_CHAT_ID": "local-test-chat"},
    )

    assert proc.returncode == 0
    assert "plain dialogue was not dispatched" in proc.stdout
