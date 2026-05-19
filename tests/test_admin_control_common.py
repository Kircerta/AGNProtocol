from __future__ import annotations

from pathlib import Path

import pytest

from scripts import admin_control_common as acc


def test_constitution_override_fails_closed_when_nonce_file_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(acc, "DEFAULT_ROOT", tmp_path)
    protected = tmp_path / "agn2" / "governance" / "constitution.json"
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AGN_ADMIN_OVERRIDE", "some-value")

    with pytest.raises(ValueError, match="nonce file is missing"):
        acc.atomic_write_json(protected, {"ok": True})


def test_constitution_override_rejects_env_redirected_nonce_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(acc, "DEFAULT_ROOT", tmp_path)
    protected = tmp_path / "agn2" / "governance" / "constitution.json"
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text("{}", encoding="utf-8")
    nonce = tmp_path / "runtime" / "admin_control" / ".override_nonce"
    nonce.parent.mkdir(parents=True, exist_ok=True)
    nonce.write_text("expected", encoding="utf-8")
    monkeypatch.setenv("AGN_ADMIN_OVERRIDE", "expected")
    monkeypatch.setenv("AGN_OVERRIDE_NONCE_PATH", str(tmp_path / "spoofed_nonce"))

    with pytest.raises(ValueError, match="canonical admin-control nonce file"):
        acc.atomic_write_json(protected, {"ok": True})
