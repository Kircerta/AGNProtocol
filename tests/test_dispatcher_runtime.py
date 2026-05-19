from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import dispatcher_runtime as dr


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime" / "dispatcher"
    monkeypatch.setattr(dr, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(dr, "REQUESTS_DIR", runtime_dir / "requests")
    monkeypatch.setattr(dr, "RESULTS_DIR", runtime_dir / "results")


def test_dispatcher_routes_memory_record_and_persists_trace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    bus_messages: list[dict[str, object]] = []
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(dr, "publish_message", lambda payload: {**payload, "id": f"msg-{len(bus_messages)+1}", "ack_required": bool(payload.get("ack_required", False))} if not bus_messages.append(payload) else {})
    monkeypatch.setattr(dr, "acknowledge_message", lambda *_args, **_kwargs: {"ack_status": "acked"})
    monkeypatch.setattr(dr, "append_event", lambda **kwargs: events.append((str(kwargs.get("event_type", "")), str(kwargs.get("trace_id", "")))))
    monkeypatch.setattr(dr, "append_record", lambda payload: {**payload, "record_id": "mem-1"})

    result = dr.dispatch_request(
        {
            "trace_id": "trace-dispatch",
            "task_id": "task-dispatch",
            "caller": "codex",
            "target": "memory_recorder",
            "target_kind": "memory_recorder",
            "intent": "record_fact",
            "reason": "capture architecture decision",
            "risk_level": "low",
            "input_payload": {
                "kind": "decision",
                "summary": "dispatcher became canonical",
                "fact_payload": {"dispatcher": True},
                "confidence": "high",
            },
        }
    )
    assert result["ok"] is True
    assert Path(result["request_ref"]).exists()
    assert Path(result["result_ref"]).exists()
    assert events[0] == ("DISPATCHER_REQUEST_CREATED", "trace-dispatch")
    assert events[-1] == ("DISPATCHER_REQUEST_COMPLETED", "trace-dispatch")


def test_dispatcher_routes_legacy_task_through_compat_shim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(dr, "publish_message", lambda payload: {**payload, "id": "msg-legacy", "ack_required": bool(payload.get("ack_required", False))})
    monkeypatch.setattr(dr, "acknowledge_message", lambda *_args, **_kwargs: {"ack_status": "acked"})
    monkeypatch.setattr(dr, "append_event", lambda **_kwargs: None)

    def fake_run(cmd, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "task_id": "legacy"}), stderr="")

    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    result = dr.dispatch_request(
        {
            "trace_id": "trace-legacy",
            "task_id": "task-legacy",
            "caller": "codex",
            "target": "legacy_task",
            "target_kind": "legacy_task",
            "intent": "compat_run",
            "reason": "exercise old chain through adapter",
            "risk_level": "medium",
            "input_payload": {"task_id": "legacy", "request_text": "compat"},
        }
    )
    assert result["ok"] is True
    assert result["result"]["handler"] == "legacy_task"


def test_dispatcher_routes_vision_with_quarantine_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(dr, "publish_message", lambda payload: {**payload, "id": "msg-vision", "ack_required": bool(payload.get("ack_required", False))})
    monkeypatch.setattr(dr, "acknowledge_message", lambda *_args, **_kwargs: {"ack_status": "acked"})
    monkeypatch.setattr(dr, "append_event", lambda **_kwargs: None)
    monkeypatch.setattr(
        dr,
        "parse_vision_ref",
        lambda **_kwargs: {
            "ok": True,
            "security_ref": "agn://artifact/" + "s" * 64,
            "security_scan": {"quarantined": True},
            "ocr_redacted": True,
            "evidence_refs": {"ocr_text_ref": "agn://artifact/" + "e" * 64},
        },
    )

    result = dr.dispatch_request(
        {
            "trace_id": "trace-vision",
            "task_id": "task-vision",
            "caller": "codex",
            "target": "vision_parser",
            "target_kind": "vision_parser",
            "intent": "inspect_visual",
            "reason": "summarize visual security posture",
            "risk_level": "low",
            "input_refs": ["agn://artifact/" + "a" * 64],
        }
    )
    assert result["ok"] is True
    payload = result["result"]
    assert payload["handler"] == "vision_parser"
    assert payload["quarantined_any"] is True
    assert payload["redacted_any"] is True
    assert payload["evidence_refs_present"] is True
    assert payload["evidence_result_indexes"] == [0]
    assert payload["security_refs"] == ["agn://artifact/" + "s" * 64]


def test_dispatcher_routes_multi_image_vision_with_distinct_attempts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(dr, "publish_message", lambda payload: {**payload, "id": "msg-vision-multi", "ack_required": bool(payload.get("ack_required", False))})
    monkeypatch.setattr(dr, "acknowledge_message", lambda *_args, **_kwargs: {"ack_status": "acked"})
    monkeypatch.setattr(dr, "append_event", lambda **_kwargs: None)
    seen_calls: list[tuple[int, str]] = []

    def fake_parse_vision_ref(*, task_id: str, attempt: int, image_ref: str) -> dict[str, object]:
        seen_calls.append((attempt, image_ref))
        return {
            "ok": True,
            "task_id": task_id,
            "attempt": attempt,
            "image_ref": image_ref,
            "security_scan": {"quarantined": False},
            "ocr_redacted": False,
            "evidence_refs": {},
        }

    monkeypatch.setattr(dr, "parse_vision_ref", fake_parse_vision_ref)

    result = dr.dispatch_request(
        {
            "trace_id": "trace-vision-multi",
            "task_id": "task-vision-multi",
            "caller": "codex",
            "target": "vision_parser",
            "target_kind": "vision_parser",
            "intent": "inspect_visual",
            "reason": "preserve per-image artifacts in a multi-image request",
            "risk_level": "low",
            "input_refs": [
                "agn://artifact/" + "a" * 64,
                "agn://artifact/" + "b" * 64,
            ],
        }
    )

    assert result["ok"] is True
    assert seen_calls == [
        (1, "agn://artifact/" + "a" * 64),
        (2, "agn://artifact/" + "b" * 64),
    ]


def test_dispatcher_routes_provider_with_forced_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(dr, "publish_message", lambda payload: {**payload, "id": "msg-provider", "ack_required": bool(payload.get("ack_required", False))})
    monkeypatch.setattr(dr, "acknowledge_message", lambda *_args, **_kwargs: {"ack_status": "acked"})
    monkeypatch.setattr(dr, "append_event", lambda **_kwargs: None)
    seen: dict[str, object] = {}

    def fake_run_routed_task(task_payload, *, output_path, forced_provider=""):
        seen["task_payload"] = dict(task_payload)
        seen["forced_provider"] = forced_provider
        return {"ok": True, "result": {"content": "ok"}}

    monkeypatch.setattr(dr, "run_routed_task", fake_run_routed_task)
    result = dr.dispatch_request(
        {
            "trace_id": "trace-provider",
            "task_id": "task-provider",
            "caller": "codex",
            "target": "provider_router",
            "target_kind": "provider",
            "intent": "generate_text",
            "reason": "respect explicit provider override",
            "risk_level": "low",
            "input_payload": {
                "task_id": "task-provider",
                "instruction": "hello",
                "forced_provider": "claude",
            },
        }
    )
    assert result["ok"] is True
    assert seen["forced_provider"] == "claude"
    assert "forced_provider" not in seen["task_payload"]
