from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import desktop_adapter as da


def test_desktop_observe_runs_gui_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gui_agent = tmp_path / "gui-agent"
    gui_agent.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(da, "GUI_AGENT_BIN", gui_agent)
    monkeypatch.setattr(da, "DESKTOP_LOG_DIR", tmp_path / "logs")
    commands: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout='{"ok": true, "app": "Ghostty"}', stderr="")

    monkeypatch.setattr(da.subprocess, "run", fake_run)
    result = da.run_desktop_action(
        {
            "action_type": "DESKTOP_OBSERVE",
            "trace_id": "trace-desktop",
            "params": {"surface": "ghostty_status"},
        }
    )
    assert result["ok"] is True
    assert commands[0][0] == str(gui_agent)
    assert "--log-file" in commands[0]


def test_desktop_status_is_available_without_gui_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(da, "GUI_AGENT_BIN", tmp_path / "missing-gui-agent")
    monkeypatch.setattr(da, "DESKTOP_LOG_DIR", tmp_path / "logs")
    result = da.run_desktop_action(
        {
            "action_type": "DESKTOP_OBSERVE",
            "trace_id": "trace-desktop-status",
            "params": {"surface": "status"},
        }
    )
    assert result["ok"] is True
    assert result["stdout"]["surface"] == "status"
    assert result["stdout"]["gui_agent_exists"] is False


def test_terminal_input_requires_explicit_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gui_agent = tmp_path / "gui-agent"
    gui_agent.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(da, "GUI_AGENT_BIN", gui_agent)
    monkeypatch.setattr(da, "DESKTOP_LOG_DIR", tmp_path / "logs")

    blocked = da.run_desktop_action(
        {
            "action_type": "TERMINAL_INPUT",
            "trace_id": "trace-blocked",
            "allow_execute": False,
            "params": {"text": "echo hi"},
        }
    )
    assert blocked["ok"] is False
    assert blocked["failure_class"] == "unsafe_action_blocked"

    commands: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout='{"ok": true, "sent": true}', stderr="")

    monkeypatch.setattr(da.subprocess, "run", fake_run)
    allowed = da.run_desktop_action(
        {
            "action_type": "TERMINAL_INPUT",
            "trace_id": "trace-allowed",
            "allow_execute": True,
            "audit_refs": ["agn://artifact/" + "b" * 64],
            "approval_context": {"decision": "approved", "gate_id": "gate-1"},
            "params": {"text": "echo hi", "enter": True},
        }
    )
    assert allowed["ok"] is True
    assert "--execute" in commands[0]
