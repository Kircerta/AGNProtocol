from __future__ import annotations

import json
from pathlib import Path

from agn.governance import commands as acp
from agn.governance import control_daemon as cd
from scripts import dispatcher_runtime as dr
from scripts import emergency_stop as es
from scripts import policy_gate as pg


def _isolate_dispatcher(monkeypatch, tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime" / "dispatcher"
    monkeypatch.setattr(dr, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(dr, "REQUESTS_DIR", runtime_dir / "requests")
    monkeypatch.setattr(dr, "RESULTS_DIR", runtime_dir / "results")
    monkeypatch.setattr(dr, "publish_message", lambda payload: {**payload, "id": "msg-1", "ack_required": bool(payload.get("ack_required", False))})
    monkeypatch.setattr(dr, "acknowledge_message", lambda *_args, **_kwargs: {"ack_status": "acked"})
    monkeypatch.setattr(dr, "append_event", lambda **_kwargs: None)
    monkeypatch.setattr(dr, "refresh_read_models", lambda: {"ok": True})
    # In test sandbox there is no system_mode.json; the fail-closed default
    # would block dispatch.  Explicitly allow work for gate/dispatch tests.
    monkeypatch.setattr(dr, "dispatcher_accepts_new_work", lambda: True)


def _init_system_mode(tmp_path: Path) -> None:
    """Create a valid system_mode.json so fail-closed logic doesn't block tests."""
    from agn.core.emergency_stop import initialize_system_mode
    initialize_system_mode(issuer="test", reason="test setup", trace_id="test-init")


def test_control_daemon_approves_gate_and_preserves_trace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    _init_system_mode(tmp_path)
    _isolate_dispatcher(monkeypatch, tmp_path)
    monkeypatch.setattr(dr, "append_record", lambda payload: {**payload, "record_id": "mem-1"})
    monkeypatch.setattr(cd, "refresh_read_models", lambda: {"ok": True})

    gated = dr.dispatch_request(
        {
            "trace_id": "trace-govern-1",
            "task_id": "task-govern-1",
            "caller": "admin",
            "target": "memory_recorder",
            "target_kind": "memory_recorder",
            "intent": "record_fact",
            "reason": "requires approval",
            "risk_level": "high",
            "input_payload": {
                "kind": "fact",
                "summary": "gated memory write",
                "fact_payload": {"x": 1},
            },
        }
    )
    assert gated["failure_class"] == "policy_gate_pending"
    gate_id = gated["result"]["gate_id"]

    acp.submit_admin_command(
        {
            "issuer": "admin",
            "command": "APPROVE_GATE",
            "target_type": "gate",
            "target_id": gate_id,
            "reason": "resume approved work",
            "trace_id": "trace-govern-1",
        }
    )
    result = cd.run_once(max_commands=5)
    assert result["processed"] == 1
    assert result["acks"][0]["status"] == "executed"
    assert pg.effective_gate_state(gate_id)["status"] == "approved"

    result_path = dr.RESULTS_DIR / "dispatch-"  # sentinel to ensure directory exists
    assert dr.RESULTS_DIR.exists()
    latest = sorted(dr.RESULTS_DIR.glob("*.json"))[-1]
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert payload["trace_id"] == "trace-govern-1"
    assert payload["ok"] is True


def test_control_daemon_executes_emergency_stop_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(cd, "refresh_read_models", lambda: {"ok": True})
    acp.submit_admin_command(
        {
            "issuer": "admin",
            "command": "EMERGENCY_STOP",
            "target_type": "system",
            "reason": "operator stop",
            "trace_id": "trace-stop-2",
        }
    )
    result = cd.run_once(max_commands=5)
    assert result["processed"] == 1
    assert result["acks"][0]["status"] == "executed"
    assert es.is_emergency_stop_active() is True
