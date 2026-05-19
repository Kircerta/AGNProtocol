from __future__ import annotations

import json
from pathlib import Path
import subprocess

from agn.governance import system as agn2


def test_agn2_start_writes_lifecycle(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(agn2, "_probe_and_write_capabilities", lambda: {"ok": True})
    monkeypatch.setattr(agn2, "build_capability_snapshot", lambda: {"surfaces": {"desktop_control": {"available": True}}})
    monkeypatch.setattr(agn2, "refresh_read_models", lambda: {"ok": True, "generated": ["overview"]})
    monkeypatch.setattr(agn2, "control_daemon_run_once", lambda max_commands=20: {"ok": True, "processed": 0, "acks": []})
    monkeypatch.setattr(agn2, "expire_messages", lambda: [])

    rc = agn2.cmd_start(type("Args", (), {})())
    assert rc == 0

    lifecycle = json.loads((tmp_path / "runtime" / "admin_control" / "lifecycle" / "agn2_system.json").read_text(encoding="utf-8"))
    assert lifecycle["status"] == "running"
    assert lifecycle["control_plane_root"] == "agn2/control_plane"
    system_mode = json.loads((tmp_path / "runtime" / "admin_control" / "system_mode.json").read_text(encoding="utf-8"))
    assert system_mode["dispatcher_accepts_new_work"] is True


def test_agn2_status_uses_capability_snapshot_read_model(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    read_models = tmp_path / "runtime" / "admin_control" / "read_models"
    read_models.mkdir(parents=True, exist_ok=True)
    (read_models / "overview.json").write_text(json.dumps({"counts": {}}), encoding="utf-8")
    (read_models / "approval_gate.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    (read_models / "capability_snapshot.json").write_text(json.dumps({"surfaces": {"vision_parser": {"available": True}}}), encoding="utf-8")
    (read_models / "execution_discipline.json").write_text(json.dumps({"status": "ready"}), encoding="utf-8")
    monkeypatch.setattr(agn2, "_lifecycle_state", lambda: {"status": "running"})
    monkeypatch.setattr(agn2, "load_system_mode", lambda: {"mode": "normal"})
    monkeypatch.setattr(agn2, "_control_plane_status", lambda: {"root": "agn2/control_plane"})
    rc = agn2.cmd_status(type("Args", (), {})())
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["capability_snapshot"]["surfaces"]["vision_parser"]["available"] is True
    assert payload["execution_discipline"]["status"] == "ready"


def test_agn2_capabilities_command_prints_snapshot(monkeypatch, capsys) -> None:
    monkeypatch.setattr(agn2, "build_capability_snapshot", lambda: {"surfaces": {"worker_delegate": {"available": True}}})
    rc = agn2.cmd_capabilities(type("Args", (), {})())
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["surfaces"]["worker_delegate"]["available"] is True


def test_agn2_emergency_stop_and_release_submit_formal_commands(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(agn2, "refresh_read_models", lambda: {"ok": True, "generated": ["overview"]})
    monkeypatch.setattr(agn2, "control_daemon_run_once", lambda max_commands=20: {"ok": True, "processed": 1, "acks": []})
    submitted: list[dict[str, object]] = []
    monkeypatch.setattr(agn2, "submit_admin_command", lambda payload: submitted.append(payload) or payload)
    monkeypatch.setattr(agn2, "load_system_mode", lambda: {"mode": "emergency_stop", "emergency_stop_active": True})

    stop_rc = agn2.cmd_emergency_stop(type("Args", (), {"reason": "test stop"})())
    release_rc = agn2.cmd_release_stop(type("Args", (), {"reason": "test release"})())

    assert stop_rc == 0
    assert release_rc == 0
    assert [item["command"] for item in submitted] == ["EMERGENCY_STOP", "RELEASE_STOP"]


def test_agn2_validate_runs_consolidation_validation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))

    def _fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"ok": True, "source": "validation-script"}),
            stderr="",
        )

    monkeypatch.setattr(agn2.subprocess, "run", _fake_run)
    rc = agn2.cmd_validate(type("Args", (), {})())
    assert rc == 0
