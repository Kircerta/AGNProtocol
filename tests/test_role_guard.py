"""Tests for the AGN Role Guard system."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import agn.core.role_guard as rg
from agn.core.role_guard import check_command, check_write_path, get_current_role


class TestGetCurrentRole:
    def test_default_is_coordinator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGN_ROLE", raising=False)
        monkeypatch.delenv("AGN_COMPAT_ADMIN", raising=False)
        assert get_current_role() == "coordinator"

    def test_compat_mode_allows_default_admin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGN_ROLE", raising=False)
        monkeypatch.setenv("AGN_COMPAT_ADMIN", "1")
        assert get_current_role() == "admin"

    def test_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGN_ROLE", "coordinator")
        assert get_current_role() == "coordinator"

    def test_normalizes_case(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGN_ROLE", " Executor ")
        assert get_current_role() == "executor"


class TestCheckCommand:
    def test_admin_allows_everything(self) -> None:
        ok, reason = check_command(["git", "apply", "foo.patch"], role="admin")
        assert ok is True
        assert reason == ""

    def test_coordinator_blocks_git_apply(self) -> None:
        ok, reason = check_command(["git", "apply", "foo.patch"], role="coordinator")
        assert ok is False
        assert "blocked_" in reason

    def test_coordinator_blocks_git_commit(self) -> None:
        ok, reason = check_command(["git", "commit", "-m", "test"], role="coordinator")
        assert ok is False
        assert "blocked_" in reason

    def test_coordinator_blocks_git_push(self) -> None:
        ok, reason = check_command(["git", "push", "origin", "main"], role="coordinator")
        assert ok is False

    def test_coordinator_blocks_codex_exec(self) -> None:
        ok, reason = check_command(["codex", "exec", "--sandbox", "workspace-write"], role="coordinator")
        assert ok is False

    def test_coordinator_blocks_sed_i(self) -> None:
        ok, reason = check_command(["sed", "-i", "s/foo/bar/", "file.py"], role="coordinator")
        assert ok is False

    def test_coordinator_blocks_rm_rf(self) -> None:
        ok, reason = check_command(["rm", "-rf", "src/"], role="coordinator")
        assert ok is False

    def test_coordinator_allows_git_status(self) -> None:
        ok, reason = check_command(["git", "status"], role="coordinator")
        assert ok is True

    def test_coordinator_allows_git_log(self) -> None:
        ok, reason = check_command(["git", "log", "--oneline"], role="coordinator")
        assert ok is True

    def test_coordinator_allows_git_diff(self) -> None:
        ok, reason = check_command(["git", "diff"], role="coordinator")
        assert ok is True

    def test_coordinator_utility_request_git_clone(self) -> None:
        ok, reason = check_command(["git", "clone", "https://example.com/repo.git"], role="coordinator")
        assert ok is False
        assert reason.startswith("utility_request_required:")

    def test_executor_allows_most_commands(self) -> None:
        ok, _ = check_command(["git", "apply", "foo.patch"], role="executor")
        assert ok is True

    def test_executor_blocks_rm_rf_root(self) -> None:
        ok, _ = check_command(["rm", "-rf", "/"], role="executor")
        assert ok is False

    def test_reviewer_blocks_git_apply(self) -> None:
        ok, _ = check_command(["git", "apply", "foo.patch"], role="reviewer")
        assert ok is False

    def test_reviewer_allows_codex_exec(self) -> None:
        ok, _ = check_command(["codex", "exec", "--sandbox", "read-only"], role="reviewer")
        assert ok is True

    def test_reviewer_allows_git_status(self) -> None:
        ok, _ = check_command(["git", "status"], role="reviewer")
        assert ok is True

    def test_coordinator_blocks_env_chdir_git_apply(self) -> None:
        ok, reason = check_command(["env", "-C", "/tmp", "git", "apply", "foo.patch"], role="coordinator")
        assert ok is False
        assert "blocked_" in reason

    def test_config_parse_failure_is_fail_closed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        bad = tmp_path / "role_permissions.json"
        bad.write_text("{invalid-json", encoding="utf-8")
        monkeypatch.setattr(rg, "CONFIG_PATH", bad)
        monkeypatch.setattr(rg, "_cached_config", None)
        monkeypatch.setattr(rg, "_cached_mtime", 0.0)
        monkeypatch.setattr(rg, "_pattern_cache", {})
        monkeypatch.setattr(rg, "_pattern_cache_mtime", 0.0)
        ok, reason = rg.check_command(["git", "apply", "foo.patch"], role="coordinator")
        assert ok is False
        assert reason.startswith("blocked_")

    def test_explicit_disable_does_not_bypass_guard_inside_agn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGN_ENFORCE_ROLE_GUARD", "0")
        monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "agn_network")
        ok, reason = check_command(["git", "apply", "foo.patch"], role="coordinator")
        assert ok is False
        assert "blocked_" in reason

    def test_explicit_disable_only_works_for_outside_agn_coordinator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGN_ENFORCE_ROLE_GUARD", "0")
        monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "outside_agn")
        ok, reason = check_command(["git", "apply", "foo.patch"], role="coordinator")
        assert ok is True
        assert reason == ""


class TestCheckWritePath:
    def test_admin_allows_any_path(self) -> None:
        ok, _ = check_write_path("/some/random/path", role="admin")
        assert ok is True

    def test_coordinator_allows_dispatch(self) -> None:
        ok, _ = check_write_path(ROOT / "dispatch" / "task.json", role="coordinator")
        assert ok is True

    def test_coordinator_allows_ssot(self) -> None:
        ok, _ = check_write_path(ROOT / "ssot" / "task.json", role="coordinator")
        assert ok is True

    def test_coordinator_blocks_results(self) -> None:
        ok, reason = check_write_path(ROOT / "results" / "task.json", role="coordinator")
        assert ok is False
        assert "write_dir_not_allowed" in reason

    def test_executor_allows_results(self) -> None:
        ok, _ = check_write_path(ROOT / "results" / "task.1.json", role="executor")
        assert ok is True

    def test_executor_blocks_ssot(self) -> None:
        ok, _ = check_write_path(ROOT / "ssot" / "task.json", role="executor")
        assert ok is False

    def test_reviewer_allows_verdicts(self) -> None:
        ok, _ = check_write_path(ROOT / "verdicts" / "task.1.json", role="reviewer")
        assert ok is True

    def test_reviewer_blocks_dispatch(self) -> None:
        ok, _ = check_write_path(ROOT / "dispatch" / "task.json", role="reviewer")
        assert ok is False

    def test_all_roles_allow_audit(self) -> None:
        audit_path = ROOT / "audit" / "events.jsonl"
        for role in ("coordinator", "executor", "reviewer"):
            ok, _ = check_write_path(audit_path, role=role)
            assert ok is True, f"audit write blocked for role={role}"
