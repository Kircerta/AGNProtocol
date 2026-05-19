from __future__ import annotations

from pathlib import Path

from agn.governance import commands as acp


def test_submit_and_ack_admin_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    payload = acp.submit_admin_command(
        {
            "issuer": "admin",
            "command": "EMERGENCY_STOP",
            "target_type": "system",
            "reason": "freeze runtime",
            "trace_id": "trace-admin-1",
        }
    )
    pending = tmp_path / "runtime" / "admin_control" / "commands" / "pending" / f"{payload['command_id']}.json"
    assert pending.exists()

    ack = acp.ack_admin_command(payload["command_id"], actor="control_daemon", status="executed", note="ok")
    assert ack["status"] == "executed"
    assert ack["trace_id"] == "trace-admin-1"


def test_validate_admin_command_rejects_missing_reason() -> None:
    errors = acp.validate_admin_command(
        {
            "issuer": "admin",
            "command": "PAUSE_TASK",
            "target_type": "task",
            "target_id": "task-1",
            "payload": {},
        }
    )
    assert "missing:reason" in errors


def test_package_commands_exposes_metadata() -> None:
    assert acp.PACKAGE_PATH == "agn.governance.commands"
    assert acp.LEGACY_SCRIPT_SHIM == "scripts/admin_command_protocol.py"
