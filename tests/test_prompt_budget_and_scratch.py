from __future__ import annotations

from pathlib import Path

import pytest

from scripts import agent_runner as ar


def test_format_cmd_redacts_long_prompt() -> None:
    prompt = "A" * 5000
    rendered = ar._format_cmd(["codex", "exec", "--cd", "/tmp/repo", prompt])
    assert "sha256=" in rendered
    assert "chars=5000" in rendered
    assert prompt[:50] not in rendered


def test_executor_prompt_budget_degrades_large_prompt() -> None:
    huge = "X" * 100
    huge_criteria = [{"id": "AC-1", "text": "T" * 40000}]
    prompt = ar._compose_codex_prompt(
        request_text=huge,
        request_summary="summary",
        request_text_ref="agn://artifact/" + ("a" * 64),
        acceptance_criteria=huge_criteria,
    )
    compact, degraded = ar._apply_executor_prompt_budget(
        prompt=prompt,
        request_summary="summary",
        request_text_ref="agn://artifact/" + ("a" * 64),
        acceptance_criteria=huge_criteria,
    )
    assert degraded is True
    assert len(compact) <= ar._EXECUTOR_PROMPT_MAX_CHARS
    assert "compact mode" in compact.lower()


def test_scratch_env_targets_scratch_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ar, "SCRATCH_ROOT", tmp_path / "scratch")
    env = ar._scratch_env_for_command(cmd=["echo", "x"], log_path=tmp_path / "log.txt")
    assert str(tmp_path / "scratch") in env["TMPDIR"]
    assert str(tmp_path / "scratch") in env["XDG_CACHE_HOME"]
    assert str(tmp_path / "scratch") in env["PIP_CACHE_DIR"]
    assert str(tmp_path / "scratch") in env["npm_config_cache"]
