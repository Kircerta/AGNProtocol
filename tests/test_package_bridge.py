from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agn.governance import bridge


def test_package_bridge_exposes_metadata() -> None:
    assert bridge.PACKAGE_PATH == "agn.governance.bridge"
    assert bridge.LEGACY_SCRIPT_SHIM == "scripts/agn2_governance_bridge.py"


def test_package_bridge_fails_closed_on_missing_requires_gate(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "evaluate_dispatch_request", lambda req: {"rule_id": "test"})
    monkeypatch.setattr(bridge, "create_gate_entry", lambda **kw: {"gate_id": "gate-test"})
    monkeypatch.setattr(bridge, "append_admin_audit", lambda *a, **kw: None)

    result = bridge.evaluate_agn1_dispatch(
        task_id="task-g",
        risk_level="high",
        side_effect_level="write",
        request_summary="test",
    )
    assert result["allowed"] is False


def test_package_bridge_cache_invalidates_on_file_change(monkeypatch, tmp_path: Path) -> None:
    bridge._CONSTITUTION_PROTECTED_PATHS = None
    bridge._CONSTITUTION_MTIME = 0.0

    gov_dir = tmp_path / "agn2" / "governance"
    gov_dir.mkdir(parents=True, exist_ok=True)
    constitution_file = gov_dir / "constitution.json"

    constitution_v1 = {
        "version": "test",
        "admin": {"sovereignty_model": "single_human_admin", "authorized_issuers": ["admin"], "sole_ssot": True, "final_responsibility": True, "final_arbiter": True},
        "runtime_hierarchy": {"layers": ["human_admin"], "runtime_must_not_override_governance": True},
        "high_risk_policy": {"default_auto_execute": False, "policy_gate_required": True, "desktop_write_requires_gate": True, "constitution_zone_requires_admin": True},
        "transparency": {"summary_view_required": True, "raw_view_required": True, "disallow_fake_chain_of_thought": True},
        "immutability": {"agent_may_not_modify": ["secret_v1.json"], "agent_may_not_self_elevate": True, "agent_may_not_change_authority_hierarchy": True},
        "council_review": {"required_on": [], "reviewer_count": 3, "unanimous_approve_required": True},
        "emergency_stop": {"admin_only": True},
    }
    constitution_file.write_text(json.dumps(constitution_v1), encoding="utf-8")

    monkeypatch.setattr(bridge, "governance_root", lambda: gov_dir)
    monkeypatch.setattr(bridge, "load_constitution", lambda: constitution_v1)
    monkeypatch.setattr(bridge, "_ROOT", tmp_path)

    paths_v1 = bridge._load_protected_paths()
    assert any("secret_v1" in p for p in paths_v1)

    constitution_v2 = dict(constitution_v1)
    constitution_v2["immutability"] = dict(constitution_v1["immutability"])
    constitution_v2["immutability"]["agent_may_not_modify"] = ["secret_v2.json"]
    constitution_file.write_text(json.dumps(constitution_v2), encoding="utf-8")
    os.utime(constitution_file, (time.time() + 1, time.time() + 1))
    monkeypatch.setattr(bridge, "load_constitution", lambda: constitution_v2)

    paths_v2 = bridge._load_protected_paths()
    assert any("secret_v2" in p for p in paths_v2)
    assert not any("secret_v1" in p for p in paths_v2)
