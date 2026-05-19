from __future__ import annotations

import json
from pathlib import Path
import sys

from agn.governance import execution_gateway as age


def test_dispatch_provider_task_routes_through_dispatcher(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_dispatch(raw):
        seen["raw"] = raw
        return {
            "ok": True,
            "request_id": "dispatch-provider",
            "request_ref": "/tmp/request.json",
            "result_ref": "/tmp/result.json",
            "failure_class": "",
            "result": {
                "ok": True,
                "handler": "provider",
                "envelope": {"ok": True, "result": {"content": "hello"}},
                "result_path": "/tmp/provider_result.json",
            },
        }

    monkeypatch.setattr(age, "dispatch_request", fake_dispatch)
    payload = age.dispatch_provider_task(
        {"task_id": "t-1", "instruction": "say hello"},
        caller="pytest",
        task_id="t-1",
        trace_id="trace-t-1",
        forced_provider="claude",
    )
    assert payload["ok"] is True
    assert payload["envelope"]["result"]["content"] == "hello"
    assert seen["raw"]["target_kind"] == "provider"
    assert seen["raw"]["input_payload"]["forced_provider"] == "claude"


def test_dispatch_memory_record_wraps_append_only_write(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_dispatch(raw):
        seen["raw"] = raw
        return {
            "ok": True,
            "request_id": "dispatch-memory",
            "request_ref": "/tmp/request.json",
            "result_ref": "/tmp/result.json",
            "failure_class": "",
            "result": {
                "ok": True,
                "handler": "memory_recorder",
                "record": {"record_id": "mem-1", "scope": "global"},
            },
        }

    monkeypatch.setattr(age, "dispatch_request", fake_dispatch)
    payload = age.dispatch_memory_record(
        {"kind": "fact", "summary": "remember this"},
        caller="pytest",
        task_id="task-memory",
        trace_id="trace-memory",
    )
    assert payload["ok"] is True
    assert payload["record"]["record_id"] == "mem-1"
    assert seen["raw"]["target_kind"] == "memory_recorder"
    assert seen["raw"]["caller"] == "pytest"


def test_dispatch_desktop_action_preserves_action_payload(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_dispatch(raw):
        seen["raw"] = raw
        return {
            "ok": True,
            "request_id": "dispatch-desktop",
            "request_ref": "/tmp/request.json",
            "result_ref": "/tmp/result.json",
            "failure_class": "",
            "result": {
                "ok": True,
                "handler": "desktop_adapter",
                "result": {"ok": True, "surface": "screenshot", "path": "/tmp/shot.png"},
            },
        }

    monkeypatch.setattr(age, "dispatch_request", fake_dispatch)
    payload = age.dispatch_desktop_action(
        {"action_type": "DESKTOP_OBSERVE", "params": {"surface": "screenshot", "path": "/tmp/shot.png"}},
        caller="pytest",
        task_id="desktop-task",
        trace_id="trace-desktop",
    )
    assert payload["ok"] is True
    assert payload["result"]["surface"] == "screenshot"
    assert seen["raw"]["target_kind"] == "desktop_adapter"
    assert seen["raw"]["input_payload"]["action_type"] == "DESKTOP_OBSERVE"


def test_cli_provider_command_uses_governed_gateway(monkeypatch, tmp_path: Path) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps({"prompt": "say hello", "task_type": "general_analysis"}), encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_dispatch_provider_task(task_payload, **kwargs):
        seen["task_payload"] = task_payload
        seen["kwargs"] = kwargs
        return {"ok": True, "envelope": {"ok": True, "result": {"content": "hello"}}}

    monkeypatch.setattr(age, "dispatch_provider_task", fake_dispatch_provider_task)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agn_governed_execution.py",
            "provider",
            "--from-json-file",
            str(task_path),
            "--force-provider",
            "claude",
        ],
    )
    assert age.main() == 0
    assert seen["task_payload"]["prompt"] == "say hello"
    assert seen["kwargs"]["forced_provider"] == "claude"


def test_cli_vision_command_registers_input_and_dispatches(monkeypatch, tmp_path: Path) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"fake image bytes")
    seen: dict[str, object] = {}

    def fake_dispatch_vision_refs(input_refs, **kwargs):
        seen["input_refs"] = input_refs
        seen["kwargs"] = kwargs
        return {"ok": True, "results": [{"summary_ref": "agn://artifact/" + ("b" * 64)}]}

    monkeypatch.setattr(age, "dispatch_vision_refs", fake_dispatch_vision_refs)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agn_governed_execution.py",
            "vision",
            "--task-id",
            "vision-task",
            "--image-path",
            str(image_path),
        ],
    )
    assert age.main() == 0
    assert len(seen["input_refs"]) == 1
    assert str(seen["input_refs"][0]).startswith("agn://artifact/")
    assert seen["kwargs"]["task_id"] == "vision-task"
