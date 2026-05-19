"""Expanded desktop adapter tests (M3 audit fix).

Tests CLI entry points, governance blocking, provider abstraction,
and edge cases not covered by the original test_desktop_adapter.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import desktop_adapter as da


class TestDesktopAdapterCLI:
    """Tests for the CLI entry point (main function)."""

    def test_cli_status_subcommand(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(da, "GUI_AGENT_BIN", tmp_path / "missing")
        monkeypatch.setattr(da, "DESKTOP_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr("sys.argv", ["desktop_adapter.py", "status"])
        captured: list[str] = []
        monkeypatch.setattr("builtins.print", lambda x, **kw: captured.append(x))
        result = da.main()
        assert result == 0
        output = json.loads(captured[0])
        assert output["ok"] is True
        assert output["stdout"]["surface"] == "status"

    def test_cli_observe_subcommand(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gui_agent = tmp_path / "gui-agent"
        gui_agent.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(da, "GUI_AGENT_BIN", gui_agent)
        monkeypatch.setattr(da, "DESKTOP_LOG_DIR", tmp_path / "logs")

        def fake_run(cmd, **_kwargs):
            return SimpleNamespace(
                returncode=0, stdout='{"ok": true, "app": "Ghostty"}', stderr=""
            )

        monkeypatch.setattr(da.subprocess, "run", fake_run)
        monkeypatch.setattr(
            "sys.argv",
            ["desktop_adapter.py", "observe", "ghostty_status", "--trace-id", "test"],
        )
        captured: list[str] = []
        monkeypatch.setattr("builtins.print", lambda x, **kw: captured.append(x))
        result = da.main()
        assert result == 0

    def test_cli_no_subcommand_prints_help(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.argv", ["desktop_adapter.py"])
        captured: list[str] = []
        monkeypatch.setattr("builtins.print", lambda x, **kw: captured.append(x))
        result = da.main()
        assert result == 0


class TestDesktopProviderInfo:
    """Tests for the provider-info subcommand."""

    def test_provider_info_returns_metadata(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gui_agent = tmp_path / "gui-agent"
        gui_agent.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(da, "GUI_AGENT_BIN", gui_agent)
        monkeypatch.setattr("sys.argv", ["desktop_adapter.py", "provider-info"])
        captured: list[str] = []
        monkeypatch.setattr("builtins.print", lambda x, **kw: captured.append(x))
        result = da.main()
        assert result == 0
        output = json.loads(captured[0])
        assert "binary" in output or "exists" in output


class TestGovernanceBlocking:
    """Tests for observe-only mode enforcement."""

    def test_observe_only_blocks_terminal_spawn(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gui_agent = tmp_path / "gui-agent"
        gui_agent.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(da, "GUI_AGENT_BIN", gui_agent)
        monkeypatch.setattr(da, "DESKTOP_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(da, "governance_desktop_mode", lambda: "observe_only")

        result = da.run_desktop_action(
            {
                "action_type": "TERMINAL_SPAWN",
                "trace_id": "trace-blocked-spawn",
                "allow_execute": True,
                "audit_refs": ["agn://artifact/" + "a" * 64],
                "approval_context": {"decision": "approved", "gate_id": "gate-1"},
                "params": {"mode": "new_window"},
            }
        )
        assert result["ok"] is False
        assert result["failure_class"] == "emergency_stop_active"

    def test_observe_only_allows_desktop_observe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gui_agent = tmp_path / "gui-agent"
        gui_agent.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(da, "GUI_AGENT_BIN", gui_agent)
        monkeypatch.setattr(da, "DESKTOP_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(da, "governance_desktop_mode", lambda: "observe_only")

        def fake_run(cmd, **_kwargs):
            return SimpleNamespace(
                returncode=0, stdout='{"ok": true}', stderr=""
            )

        monkeypatch.setattr(da.subprocess, "run", fake_run)
        result = da.run_desktop_action(
            {
                "action_type": "DESKTOP_OBSERVE",
                "trace_id": "trace-observe-ok",
                "params": {"surface": "frontmost"},
            }
        )
        assert result["ok"] is True

    def test_terminal_send_key_without_approval_blocked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gui_agent = tmp_path / "gui-agent"
        gui_agent.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(da, "GUI_AGENT_BIN", gui_agent)
        monkeypatch.setattr(da, "DESKTOP_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(da, "governance_desktop_mode", lambda: "full")

        result = da.run_desktop_action(
            {
                "action_type": "TERMINAL_SEND_KEY",
                "trace_id": "trace-sendkey",
                "allow_execute": True,
                "audit_refs": ["agn://artifact/" + "a" * 64],
                "approval_context": {},  # no decision
                "params": {"key": "Return"},
            }
        )
        assert result["ok"] is False
        assert result["failure_class"] == "unsafe_action_blocked"


class TestEdgeCases:
    """Tests for error handling and edge cases."""

    def test_unsupported_action_type(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gui_agent = tmp_path / "gui-agent"
        gui_agent.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(da, "GUI_AGENT_BIN", gui_agent)
        monkeypatch.setattr(da, "DESKTOP_LOG_DIR", tmp_path / "logs")

        result = da.run_desktop_action(
            {
                "action_type": "UNKNOWN_ACTION",
                "trace_id": "trace-unknown",
                "params": {},
            }
        )
        assert result["ok"] is False
        assert result["failure_class"] == "schema_invalid"

    def test_invalid_params_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = da.run_desktop_action(
            {
                "action_type": "DESKTOP_OBSERVE",
                "trace_id": "trace-bad-params",
                "params": "not_a_dict",
            }
        )
        assert result["ok"] is False
        assert result["failure_class"] == "schema_invalid"

    def test_missing_gui_agent_blocks_non_status_observe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(da, "GUI_AGENT_BIN", tmp_path / "missing-gui-agent")
        monkeypatch.setattr(da, "DESKTOP_LOG_DIR", tmp_path / "logs")

        result = da.run_desktop_action(
            {
                "action_type": "DESKTOP_OBSERVE",
                "trace_id": "trace-missing",
                "params": {"surface": "frontmost"},
            }
        )
        assert result["ok"] is False
        assert result["failure_class"] == "schema_invalid"

    def test_run_from_json_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gui_agent = tmp_path / "gui-agent"
        gui_agent.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(da, "GUI_AGENT_BIN", gui_agent)
        monkeypatch.setattr(da, "DESKTOP_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(da, "governance_desktop_mode", lambda: "full")

        # Write a JSON action file for the `run` subcommand
        action = {
            "action_type": "DESKTOP_OBSERVE",
            "trace_id": "trace-json-run",
            "params": {"surface": "status"},
        }
        action_file = tmp_path / "action.json"
        action_file.write_text(json.dumps(action), encoding="utf-8")

        monkeypatch.setattr(
            "sys.argv",
            ["desktop_adapter.py", "run", "--from-json", str(action_file)],
        )
        captured: list[str] = []
        monkeypatch.setattr("builtins.print", lambda x, **kw: captured.append(x))
        result = da.main()
        assert result == 0
        output = json.loads(captured[0])
        assert output["ok"] is True
