from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from agn.core import constitution as const


def test_package_constitution_exposes_metadata() -> None:
    assert const.PACKAGE_PATH == "agn.core.constitution"
    assert const.LEGACY_SCRIPT_SHIM == "scripts/agn2_constitution.py"


def test_load_constitution_falls_back_to_default_when_payload_invalid(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    target = tmp_path / "agn2" / "governance" / "constitution.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"admin": {"authorized_issuers": []}}', encoding="utf-8")

    payload = const.load_constitution()

    assert payload == const.DEFAULT_CONSTITUTION


def test_issuer_is_authorized_uses_loaded_constitution(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    target = tmp_path / "agn2" / "governance" / "constitution.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = deepcopy(const.DEFAULT_CONSTITUTION)
    payload["admin"]["authorized_issuers"] = ["admin", "reviewer"]
    target.write_text(__import__("json").dumps(payload), encoding="utf-8")

    assert const.issuer_is_authorized("reviewer") is True
    assert const.issuer_is_authorized("unknown") is False


def test_council_policy_returns_mapping() -> None:
    payload = deepcopy(const.DEFAULT_CONSTITUTION)
    policy = const.council_policy(payload)
    assert policy["reviewer_count"] == 3
    assert policy["unanimous_approve_required"] is True
