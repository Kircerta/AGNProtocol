from __future__ import annotations

from pathlib import Path

import pytest

from agn.dispatch import dispatcher as dd


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime" / "dispatcher"
    monkeypatch.setattr(dd, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(dd, "REQUESTS_DIR", runtime_dir / "requests")
    monkeypatch.setattr(dd, "RESULTS_DIR", runtime_dir / "results")


def test_package_dispatcher_exposes_metadata() -> None:
    assert dd.PACKAGE_PATH == "agn.dispatch.dispatcher"
    assert dd.LEGACY_SCRIPT_SHIM == "scripts/dispatcher_runtime.py"


def test_package_dispatcher_memory_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(dd, "publish_message", lambda payload: {**payload, "id": "msg-pkg-dd", "ack_required": bool(payload.get("ack_required", False))})
    monkeypatch.setattr(dd, "acknowledge_message", lambda *_args, **_kwargs: {"ack_status": "acked"})
    monkeypatch.setattr(dd, "append_event", lambda **_kwargs: None)
    monkeypatch.setattr(dd, "append_record", lambda payload: {**payload, "record_id": "mem-pkg-dd"})

    result = dd.dispatch_request(
        {
            "trace_id": "trace-pkg-dd",
            "task_id": "task-pkg-dd",
            "caller": "codex",
            "target": "memory_recorder",
            "target_kind": "memory_recorder",
            "intent": "record_fact",
            "reason": "package dispatcher route",
            "risk_level": "low",
            "input_payload": {
                "kind": "fact",
                "summary": "package dispatcher route works",
            },
        }
    )

    assert result["ok"] is True
    assert Path(result["request_ref"]).exists()
    assert Path(result["result_ref"]).exists()
