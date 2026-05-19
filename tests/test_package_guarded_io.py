from __future__ import annotations

from pathlib import Path

import pytest

from agn.core import guarded_io as gio


def test_package_guarded_io_exposes_metadata() -> None:
    assert gio.PACKAGE_PATH == "agn.core.guarded_io"
    assert gio.LEGACY_SCRIPT_SHIM == "scripts/guarded_io.py"


def test_package_guarded_io_atomic_write_json_obeys_role_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_ROLE", "coordinator")
    monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "agn_network")
    monkeypatch.setenv("AGN_ENFORCE_ROLE_GUARD", "1")
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))

    allowed = tmp_path / "dispatch" / "ok.json"
    blocked = tmp_path / "results" / "blocked.json"

    gio.atomic_write_json(allowed, {"ok": True})
    assert allowed.exists()

    with pytest.raises(PermissionError):
        gio.atomic_write_json(blocked, {"ok": False})
