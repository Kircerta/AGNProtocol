from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts import desktop_adapter as da
from scripts import dispatcher_runtime as dr
from scripts import emergency_stop as es


def _isolate_dispatcher(monkeypatch, tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime" / "dispatcher"
    monkeypatch.setattr(dr, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(dr, "REQUESTS_DIR", runtime_dir / "requests")
    monkeypatch.setattr(dr, "RESULTS_DIR", runtime_dir / "results")
    monkeypatch.setattr(dr, "publish_message", lambda payload: {**payload, "id": "msg-1", "ack_required": bool(payload.get("ack_required", False))})
    monkeypatch.setattr(dr, "acknowledge_message", lambda *_args, **_kwargs: {"ack_status": "acked"})
    monkeypatch.setattr(dr, "append_event", lambda **_kwargs: None)


def _authorize_system_mode_write(monkeypatch, tmp_path: Path) -> None:
    nonce_path = tmp_path / "runtime" / "admin_control" / ".override_nonce"
    nonce_path.parent.mkdir(parents=True, exist_ok=True)
    nonce_path.write_text("nonce-1", encoding="utf-8")
    monkeypatch.setenv("AGN_ADMIN_OVERRIDE", "nonce-1")
    monkeypatch.delenv("AGN_OVERRIDE_NONCE_PATH", raising=False)
    monkeypatch.setattr(dr, "refresh_read_models", lambda: {"ok": True})


def test_emergency_stop_blocks_dispatch_and_degrades_desktop(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    _isolate_dispatcher(monkeypatch, tmp_path)
    _authorize_system_mode_write(monkeypatch, tmp_path)
    monkeypatch.setattr(dr, "append_record", lambda payload: {**payload, "record_id": "mem-1"})
    es.activate_emergency_stop(issuer="admin", reason="test stop", trace_id="trace-stop")

    blocked = dr.dispatch_request(
        {
            "trace_id": "trace-stop",
            "task_id": "task-stop",
            "caller": "admin",
            "target": "memory_recorder",
            "target_kind": "memory_recorder",
            "intent": "record_fact",
            "reason": "should block",
            "risk_level": "low",
            "input_payload": {
                "kind": "fact",
                "summary": "blocked",
                "fact_payload": {"blocked": True},
            },
        }
    )
    assert blocked["failure_class"] == "emergency_stop_active"

    gui_agent = tmp_path / "gui-agent"
    gui_agent.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(da, "GUI_AGENT_BIN", gui_agent)
    monkeypatch.setattr(da, "DESKTOP_LOG_DIR", tmp_path / "logs")

    def fake_run(cmd, **_kwargs):
        return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(da.subprocess, "run", fake_run)
    observe = da.run_desktop_action({"action_type": "DESKTOP_OBSERVE", "trace_id": "trace-stop", "params": {"surface": "status"}})
    assert observe["ok"] is True

    write_action = da.run_desktop_action(
        {
            "action_type": "TERMINAL_INPUT",
            "trace_id": "trace-stop",
            "allow_execute": True,
            "audit_refs": ["agn://artifact/" + "a" * 64],
            "approval_context": {"decision": "approved", "gate_id": "gate-1"},
            "params": {"text": "echo hi"},
        }
    )
    assert write_action["failure_class"] == "emergency_stop_active"


def test_default_system_mode_allows_new_work_when_no_file_exists(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    mode = es.load_system_mode()
    assert mode["emergency_stop_active"] is False
    assert mode["dispatcher_accepts_new_work"] is True
    assert es.dispatcher_accepts_new_work() is True


def test_initialize_system_mode_creates_signed_normal_mode_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    payload = es.initialize_system_mode(issuer="agn2_system", reason="bootstrap")
    assert payload["dispatcher_accepts_new_work"] is True
    assert payload["updated_at"]
    mode_path = tmp_path / "runtime" / "admin_control" / "system_mode.json"
    assert mode_path.exists()
