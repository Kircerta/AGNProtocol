from __future__ import annotations

from pathlib import Path

import pytest

from agn.core import role_guard as rg


def test_package_role_guard_exposes_metadata() -> None:
    assert rg.PACKAGE_PATH == "agn.core.role_guard"
    assert rg.LEGACY_SCRIPT_SHIM == "scripts/role_guard.py"


def test_package_role_guard_config_parse_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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


def test_package_role_guard_honors_outside_agn_disable_for_coordinator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGN_ENFORCE_ROLE_GUARD", "0")
    monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "outside_agn")
    ok, reason = rg.check_command(["git", "apply", "foo.patch"], role="coordinator")
    assert ok is True
    assert reason == ""
