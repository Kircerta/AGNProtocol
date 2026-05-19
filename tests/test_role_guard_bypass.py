from __future__ import annotations

import os
from pathlib import Path

import pytest

from agn.core.guarded_io import atomic_replace, write_bytes, write_text
from agn.core.role_guard import check_command, check_write_path, get_current_role

ROOT = Path(__file__).resolve().parents[1]


def test_coordinator_blocks_shell_wrapper_bypass() -> None:
    ok, reason = check_command(["bash", "-lc", "git apply foo.patch"], role="coordinator")
    assert ok is False
    assert "blocked_secondary_exec_container" in reason


def test_coordinator_blocks_python_c_wrapper_bypass() -> None:
    ok, reason = check_command(["python3", "-c", "import os; os.system('git apply foo.patch')"], role="coordinator")
    assert ok is False
    assert "blocked_secondary_exec_container" in reason


def test_coordinator_blocks_env_prefixed_git_apply() -> None:
    ok, reason = check_command(["FOO=1", "git", "apply", "foo.patch"], role="coordinator")
    assert ok is False
    assert "blocked_" in reason

    ok2, reason2 = check_command(["env", "FOO=1", "git", "apply", "foo.patch"], role="coordinator")
    assert ok2 is False
    assert "blocked_" in reason2


def test_path_traversal_is_rejected() -> None:
    target = ROOT / "memory" / ".." / "results" / "traversal.txt"
    ok, reason = check_write_path(target, role="coordinator")
    assert ok is False
    assert "write_dir_not_allowed" in reason


def test_symlink_escape_is_rejected() -> None:
    link = ROOT / "memory" / ".role_guard_test_results_link"
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink((ROOT / "results").as_posix(), link.as_posix())
        escaped = link / "escape.txt"
        ok, reason = check_write_path(escaped, role="coordinator")
        assert ok is False
        assert "write_dir_not_allowed" in reason
    finally:
        if link.exists() or link.is_symlink():
            link.unlink()


def test_guarded_text_and_bytes_writes_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGN_ROLE", "coordinator")
    monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "agn_network")
    monkeypatch.setenv("AGN_ENFORCE_ROLE_GUARD", "1")

    allowed_path = ROOT / "dispatch" / ".role_guard_text_ok.txt"
    blocked_path = ROOT / "results" / ".role_guard_bytes_blocked.bin"
    try:
        write_text(allowed_path, "ok\n")
        assert allowed_path.exists()
        with pytest.raises(PermissionError):
            write_bytes(blocked_path, b"blocked")
    finally:
        if allowed_path.exists():
            allowed_path.unlink()
        if blocked_path.exists():
            blocked_path.unlink()


def test_guarded_atomic_replace_enforced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_ROLE", "coordinator")
    monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "agn_network")
    monkeypatch.setenv("AGN_ENFORCE_ROLE_GUARD", "1")

    src = tmp_path / "tmp.txt"
    src.write_text("x", encoding="utf-8")
    blocked_dst = ROOT / "results" / ".role_guard_replace_blocked.txt"
    with pytest.raises(PermissionError):
        atomic_replace(src, blocked_dst)
    assert src.exists()


def test_missing_role_never_defaults_to_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGN_ROLE", raising=False)
    monkeypatch.delenv("AGN_COMPAT_ADMIN", raising=False)
    assert get_current_role() != "admin"
