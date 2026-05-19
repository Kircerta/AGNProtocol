from __future__ import annotations

from pathlib import Path

from scripts import policy_gate as pg


def test_policy_gate_creates_and_decides_entry(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    request_ref = tmp_path / "request.json"
    request_ref.write_text("{}", encoding="utf-8")
    request = {
        "trace_id": "trace-gate-1",
        "task_id": "task-gate-1",
        "caller": "admin",
        "target": "desktop_adapter",
        "target_kind": "desktop_adapter",
        "intent": "terminal input",
        "reason": "send command",
        "risk_level": "high",
        "input_payload": {"action_type": "TERMINAL_INPUT"},
    }

    evaluation = pg.evaluate_dispatch_request(request)
    assert evaluation["requires_gate"] is True
    assert evaluation["rule_id"] == "desktop_write_phase1_gate"

    gate = pg.create_gate_entry(request=request, request_ref=str(request_ref), evaluation=evaluation)
    assert gate["trace_id"] == "trace-gate-1"
    assert len(pg.pending_gate_entries()) == 1

    decision = pg.decide_gate(gate["gate_id"], decision="approved", decided_by="admin", note="approved")
    assert decision["decision"] == "approved"
    assert pg.effective_gate_state(gate["gate_id"])["status"] == "approved"


def test_policy_gate_blocks_write_action_even_if_target_kind_is_mislabeled() -> None:
    request = {
        "trace_id": "trace-gate-2",
        "task_id": "task-gate-2",
        "caller": "codex",
        "target": "claude",
        "target_kind": "provider",
        "intent": "spawn terminal via mislabeled provider",
        "reason": "attempt bypass",
        "risk_level": "low",
        "input_payload": {"action_type": "TERMINAL_SPAWN"},
    }
    evaluation = pg.evaluate_dispatch_request(request)
    assert evaluation["requires_gate"] is True
    assert evaluation["rule_id"] == "builtin_write_action_type"
    assert evaluation["requires_audit_refs"] is True
