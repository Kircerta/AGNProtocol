from __future__ import annotations

import json
from pathlib import Path

from scripts import coordinator_ingest
from scripts.agent_runner import dispatch_path

ROOT = Path(__file__).resolve().parents[1]


def _cleanup(task_id: str) -> None:
    dpath = dispatch_path(task_id)
    if dpath.exists():
        dpath.unlink()


def test_large_request_is_ref_only_in_dispatch() -> None:
    task_id = "test-dispatch-ref-only-large"
    _cleanup(task_id)
    try:
        huge = "X" * 120000
        result = coordinator_ingest.run(
            task_id=task_id,
            request_text=huge,
            source="test",
            correlation_id="corr-test-large",
            criteria_json=None,
            criterion_items=["AC-1:ref only"],
            task_kind="protocol",
            repo_path="",
            work_branch="",
            executor_provider="codex",
            reviewer_provider="gemini",
            chat_id="",
            message_id="",
            risk_level="low",
            side_effect_level="read_only",
            attempt=None,
        )
        assert result["ok"] is True
        dpath = dispatch_path(task_id)
        payload = json.loads(dpath.read_text(encoding="utf-8"))
        assert payload.get("request_text", "") == ""
        assert str(payload.get("request_text_ref", "")).startswith("agn://artifact/")
        assert len(str(payload.get("request_summary", ""))) > 0
        assert dpath.stat().st_size < 20000
    finally:
        _cleanup(task_id)


def test_small_request_keeps_inline_field() -> None:
    task_id = "test-dispatch-ref-only-small"
    _cleanup(task_id)
    try:
        text = "short request"
        result = coordinator_ingest.run(
            task_id=task_id,
            request_text=text,
            source="test",
            correlation_id="corr-test-small",
            criteria_json=None,
            criterion_items=["AC-1:inline allowed"],
            task_kind="protocol",
            repo_path="",
            work_branch="",
            executor_provider="codex",
            reviewer_provider="gemini",
            chat_id="",
            message_id="",
            risk_level="low",
            side_effect_level="read_only",
            attempt=None,
        )
        assert result["ok"] is True
        payload = json.loads(dispatch_path(task_id).read_text(encoding="utf-8"))
        assert payload.get("request_text") == text
        assert str(payload.get("request_text_ref", "")).startswith("agn://artifact/")
    finally:
        _cleanup(task_id)
