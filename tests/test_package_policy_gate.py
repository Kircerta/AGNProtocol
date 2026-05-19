from __future__ import annotations

from pathlib import Path

from agn.core import policy_gate as pg


def test_package_policy_gate_exposes_metadata() -> None:
    assert pg.PACKAGE_PATH == "agn.core.policy_gate"
    assert pg.LEGACY_SCRIPT_SHIM == "scripts/policy_gate.py"


def test_package_policy_gate_creates_pending_gate_and_decision(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    request_ref = tmp_path / "request.json"
    request_ref.write_text("{}", encoding="utf-8")
    request = {
        "trace_id": "trace-gate-package-1",
        "task_id": "task-gate-package-1",
        "caller": "codex",
        "target": "desktop_adapter",
        "target_kind": "desktop_adapter",
        "intent": "terminal input",
        "reason": "package migration smoke",
        "risk_level": "high",
        "input_payload": {"action_type": "TERMINAL_INPUT"},
    }
    evaluation = pg.evaluate_dispatch_request(request)
    gate = pg.create_gate_entry(request=request, request_ref=str(request_ref), evaluation=evaluation)

    assert gate["gate_id"].startswith("gate-")
    assert pg.pending_gate_entries()[0]["gate_id"] == gate["gate_id"]

    decision = pg.decide_gate(gate["gate_id"], decision="approved", decided_by="admin")
    assert decision["decision"] == "approved"
    assert pg.effective_gate_state(gate["gate_id"])["status"] == "approved"
