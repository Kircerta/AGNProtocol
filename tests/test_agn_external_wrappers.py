from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts import agn_browser_use_wrapper as browser_wrapper
from scripts import agn_hindsight_wrapper as hindsight_wrapper
from scripts import agn_promptfoo_wrapper as promptfoo_wrapper


def test_browser_wrapper_blocks_on_emergency_stop(monkeypatch, capsys) -> None:
    monkeypatch.setattr(browser_wrapper, "load_system_mode", lambda: {"mode": "emergency_stop", "emergency_stop_active": True})
    args = argparse.Namespace(
        url="https://example.com",
        session="",
        timeout_seconds=60,
        artifact_stem="",
        settle_seconds=2.0,
        headed=False,
        profile="",
        connect=False,
        cdp_url="",
        keep_session=False,
        max_active_sessions=1,
    )

    rc = browser_wrapper.cmd_run(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["error"] == "emergency_stop_active"


def test_browser_wrapper_prefers_cli_env_binary(monkeypatch, tmp_path: Path) -> None:
    cli_bin = tmp_path / ".browser-use-env" / "bin" / "browser-use"
    cli_bin.parent.mkdir(parents=True, exist_ok=True)
    cli_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    legacy_bin = tmp_path / ".agn_external_wrappers_venv" / "bin" / "browser-use"
    legacy_bin.parent.mkdir(parents=True, exist_ok=True)
    legacy_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    resolved = browser_wrapper._browser_use_bin()

    assert resolved == cli_bin.resolve()


def test_browser_wrapper_writes_artifacts(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(browser_wrapper, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(browser_wrapper, "load_system_mode", lambda: {"mode": "normal", "emergency_stop_active": False})

    def fake_run(session: str, timeout_seconds: int, context_args: list[str], *subcommand: str) -> dict[str, object]:
        action = subcommand[0]
        assert context_args == []
        if action == "open":
            return {"command": list(subcommand), "returncode": 0, "duration_seconds": 0.1, "stdout": "{}", "stderr": "", "parsed": {"success": True}}
        if action == "state":
            return {
                "command": list(subcommand),
                "returncode": 0,
                "duration_seconds": 0.1,
                "stdout": '{"url":"https://example.com"}',
                "stderr": "",
                "parsed": {"url": "https://example.com"},
            }
        if action == "screenshot":
            Path(subcommand[1]).write_bytes(b"png")
            return {"command": list(subcommand), "returncode": 0, "duration_seconds": 0.1, "stdout": "{}", "stderr": "", "parsed": {"saved": subcommand[1]}}
        raise AssertionError(f"unexpected subcommand: {subcommand}")

    monkeypatch.setattr(browser_wrapper, "_run_browser_use", fake_run)
    monkeypatch.setattr(browser_wrapper, "_close_session", lambda session, timeout_seconds, context_args: {"command": ["close"], "returncode": 0, "duration_seconds": 0.1, "stdout": "{}", "stderr": "", "parsed": {"shutdown": True}})
    args = argparse.Namespace(
        url="https://example.com",
        session="",
        timeout_seconds=60,
        artifact_stem="browser-smoke",
        settle_seconds=0.0,
        headed=False,
        profile="",
        connect=False,
        cdp_url="",
        keep_session=False,
        max_active_sessions=1,
    )

    rc = browser_wrapper.cmd_run(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["execution_mode"]["background_expected"] is True
    assert Path(tmp_path / "browser-smoke.json").exists()
    assert Path(tmp_path / "browser-smoke.state.json").exists()
    assert Path(tmp_path / "browser-smoke.png").exists()


def test_browser_wrapper_timeout_leaves_structured_failure(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(browser_wrapper, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(browser_wrapper, "load_system_mode", lambda: {"mode": "normal", "emergency_stop_active": False})
    monkeypatch.setattr(
        browser_wrapper,
        "_run_browser_use",
        lambda session, timeout_seconds, context_args, *subcommand: {
            "command": list(subcommand),
            "returncode": 124,
            "duration_seconds": 60.0,
            "stdout": "",
            "stderr": "timed out after 60 seconds",
            "parsed": None,
            "timed_out": True,
        }
        if subcommand[0] == "open"
        else {
            "command": list(subcommand),
            "returncode": 0,
            "duration_seconds": 0.1,
            "stdout": "{}",
            "stderr": "",
            "parsed": {"shutdown": True},
        },
    )
    monkeypatch.setattr(
        browser_wrapper,
        "_close_session",
        lambda session, timeout_seconds, context_args: {
            "command": ["close"],
            "returncode": 0,
            "duration_seconds": 0.1,
            "stdout": "{}",
            "stderr": "",
            "parsed": {"shutdown": True},
        },
    )
    monkeypatch.setattr(browser_wrapper, "_terminate_session_process", lambda session: {"attempted": False, "terminated": False, "reason": "close_succeeded"})
    monkeypatch.setattr(browser_wrapper, "_cleanup_session_runtime_paths", lambda session: [])
    args = argparse.Namespace(
        url="https://example.com",
        session="",
        timeout_seconds=60,
        artifact_stem="browser-timeout",
        settle_seconds=0.0,
        headed=False,
        profile="",
        connect=False,
        cdp_url="",
        keep_session=False,
        max_active_sessions=1,
    )

    rc = browser_wrapper.cmd_run(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["steps"][0]["timed_out"] is True
    assert Path(tmp_path / "browser-timeout.json").exists()


def test_browser_wrapper_rollback_is_idempotent(monkeypatch, capsys) -> None:
    monkeypatch.setattr(browser_wrapper, "load_system_mode", lambda: {"mode": "normal", "emergency_stop_active": False})
    monkeypatch.setattr(
        browser_wrapper,
        "_close_session",
        lambda session, timeout_seconds, context_args: {
            "command": ["close"],
            "returncode": 1,
            "duration_seconds": 0.1,
            "stdout": "session not found",
            "stderr": "",
            "parsed": None,
        },
    )
    args = argparse.Namespace(
        session="agn-browser-use-wrapper-20260321T062845Z",
        timeout_seconds=60,
        headed=False,
        profile="",
        connect=False,
        cdp_url="",
    )

    rc = browser_wrapper.cmd_rollback(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ok"] is True


def test_browser_wrapper_enforces_single_active_session_budget(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(browser_wrapper, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(browser_wrapper, "load_system_mode", lambda: {"mode": "normal", "emergency_stop_active": False})
    monkeypatch.setattr(
        browser_wrapper,
        "_list_active_sessions",
        lambda timeout_seconds: {
            "command": ["sessions"],
            "returncode": 0,
            "duration_seconds": 0.1,
            "stdout": '{"sessions":[{"name":"agn-browser-use-wrapper-existing"}]}',
            "stderr": "",
            "parsed": {"sessions": [{"name": "agn-browser-use-wrapper-existing"}]},
        },
    )
    args = argparse.Namespace(
        url="https://example.com",
        session="agn-browser-use-wrapper-new",
        timeout_seconds=60,
        artifact_stem="browser-budget",
        settle_seconds=0.0,
        headed=False,
        profile="",
        connect=False,
        cdp_url="",
        keep_session=False,
        max_active_sessions=1,
    )

    rc = browser_wrapper.cmd_run(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["error"] == "session_budget_exceeded"
    assert payload["session_budget"]["active_session_count_before_run"] == 1
    assert Path(tmp_path / "browser-budget.json").exists()


def test_browser_wrapper_prune_removes_stale_runtime_paths(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(browser_wrapper, "BROWSER_HOME", tmp_path)
    monkeypatch.setattr(browser_wrapper, "load_system_mode", lambda: {"mode": "normal", "emergency_stop_active": False})
    stale = tmp_path / "agn-stale.sock"
    stale.write_text("", encoding="utf-8")
    active = tmp_path / "agn-live.sock"
    active.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        browser_wrapper,
        "_list_active_sessions",
        lambda timeout_seconds: {
            "command": ["sessions"],
            "returncode": 0,
            "duration_seconds": 0.1,
            "stdout": '{"sessions":[{"name":"agn-live"}]}',
            "stderr": "",
            "parsed": {"sessions": [{"name": "agn-live"}]},
        },
    )
    monkeypatch.setattr(browser_wrapper, "_terminate_session_process", lambda session: {"attempted": session == "agn-stale", "terminated": False, "reason": "pid_file_missing"})
    args = argparse.Namespace(session_prefix="agn-", timeout_seconds=60)

    rc = browser_wrapper.cmd_prune(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert str(stale) in payload["removed_runtime_paths"]
    assert str(active) in payload["retained_runtime_paths"]
    assert not stale.exists()
    assert active.exists()


def test_hindsight_wrapper_rollback_removes_wrapper_paths(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(hindsight_wrapper, "HINDSIGHT_HOME", tmp_path)
    monkeypatch.setattr(hindsight_wrapper, "load_system_mode", lambda: {"mode": "normal", "emergency_stop_active": False})
    profiles = tmp_path / "profiles"
    profiles.mkdir(parents=True)
    profile = "agn-hindsight-wrapper-test"
    for suffix in (".log", ".lock", ".env"):
        (profiles / f"{profile}{suffix}").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        hindsight_wrapper,
        "_run_hindsight",
        lambda env, timeout_seconds, *args: {"command": list(args), "returncode": 0, "duration_seconds": 0.1, "stdout": "stopped", "stderr": "", "parsed": None},
    )
    args = argparse.Namespace(profile=profile, timeout_seconds=60)

    rc = hindsight_wrapper.cmd_rollback(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ok"] is True
    assert not any((profiles / f"{profile}{suffix}").exists() for suffix in (".log", ".lock", ".env"))


def test_promptfoo_wrapper_rejects_config_outside_repo(monkeypatch, tmp_path: Path, capsys) -> None:
    config = tmp_path / "outside.yaml"
    config.write_text("description: bad\n", encoding="utf-8")
    monkeypatch.setattr(promptfoo_wrapper, "load_system_mode", lambda: {"mode": "normal", "emergency_stop_active": False})
    args = argparse.Namespace(
        config=str(config),
        prompt="Return exactly: {{word}}",
        word="hello",
        expected="",
        timeout_seconds=60,
        artifact_stem="",
    )

    rc = promptfoo_wrapper.cmd_run(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["error"] == "invalid_input"


def test_promptfoo_wrapper_writes_generated_artifacts(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(promptfoo_wrapper, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(promptfoo_wrapper, "load_system_mode", lambda: {"mode": "normal", "emergency_stop_active": False})

    def fake_run(config_path: str, report_path: str, timeout_seconds: int) -> dict[str, object]:
        Path(report_path).write_text('{"results":{"passed":1}}', encoding="utf-8")
        return {"command": ["promptfoo"], "returncode": 0, "duration_seconds": 0.1, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(promptfoo_wrapper, "_run_promptfoo", fake_run)
    args = argparse.Namespace(
        config="",
        prompt="Return exactly: {{word}}",
        word="hello",
        expected="Return exactly: hello",
        timeout_seconds=60,
        artifact_stem="promptfoo-smoke",
    )

    rc = promptfoo_wrapper.cmd_run(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ok"] is True
    assert Path(tmp_path / "promptfoo-smoke.config.yaml").exists()
    assert Path(tmp_path / "promptfoo-smoke.json").exists()
    assert Path(tmp_path / "promptfoo-smoke.log").exists()
