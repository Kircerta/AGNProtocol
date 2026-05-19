from __future__ import annotations

from pathlib import Path

import pytest

from agn.core import admin_control as acc


def test_package_admin_control_exposes_package_metadata() -> None:
    assert acc.PACKAGE_PATH == "agn.core.admin_control"
    assert acc.LEGACY_SCRIPT_SHIM == "scripts/admin_control_common.py"


def test_package_constitution_override_fails_closed_when_nonce_file_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(acc, "DEFAULT_ROOT", tmp_path)
    protected = tmp_path / "agn2" / "governance" / "constitution.json"
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AGN_ADMIN_OVERRIDE", "some-value")

    with pytest.raises(ValueError, match="nonce file is missing"):
        acc.atomic_write_json(protected, {"ok": True})
