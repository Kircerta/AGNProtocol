"""Phase 2 regression tests: critical bugs in AGN1.0 sealed subsystem.

Tests cover bugs found during the deep architectural investigation
of executor_worker, reviewer_worker, coordinator_loop, and the
agn2_governance_bridge.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ── Bug F: executor_worker timeout overwrites successful code ──────


def test_executor_timeout_does_not_overwrite_success() -> None:
    """If the executor task completes with code=0 but the monotonic check
    fires after the deadline, the result must remain code=0 (processed),
    NOT code=124 (timeout).  Verified via source inspection since the
    executor_worker module requires the sealed AGN1.0 import chain."""
    source = (Path(__file__).resolve().parents[1] / "scripts" / "executor_worker.py").read_text()
    # The fix: "if task_timed_out and code != 0:" instead of "if task_timed_out:"
    assert "task_timed_out and code != 0" in source, (
        "executor_worker timeout should only apply when code != 0"
    )
    # Negative check: the old pattern must not exist
    # (bare "if task_timed_out:" followed immediately by "code = 124")
    import re
    bare_timeout = re.findall(r"if task_timed_out:\s+code = 124", source)
    assert len(bare_timeout) == 0, (
        f"Found old pattern 'if task_timed_out: code = 124' without code != 0 guard"
    )


# ── Bug G: governance bridge fails-open on malformed evaluation ────


def test_governance_bridge_fails_closed_on_missing_requires_gate(monkeypatch) -> None:
    """If evaluate_dispatch_request returns a dict without 'requires_gate',
    the bridge must default to blocked (fail-closed), not allowed."""
    import scripts.agn2_governance_bridge as bridge

    # Return evaluation with missing 'requires_gate' key
    monkeypatch.setattr(bridge, "evaluate_dispatch_request", lambda req: {"rule_id": "test"})
    monkeypatch.setattr(bridge, "create_gate_entry", lambda **kw: {"gate_id": "gate-test"})
    monkeypatch.setattr(bridge, "append_admin_audit", lambda *a, **kw: None)

    result = bridge.evaluate_agn1_dispatch(
        task_id="task-g",
        risk_level="high",
        side_effect_level="write",
        request_summary="test",
    )
    assert result["allowed"] is False, "Missing 'requires_gate' should fail-closed"


def test_governance_bridge_fails_closed_on_evaluation_crash(monkeypatch) -> None:
    """If evaluate_dispatch_request raises an exception, the bridge must
    return allowed=False, not propagate the crash."""
    import scripts.agn2_governance_bridge as bridge

    monkeypatch.setattr(bridge, "evaluate_dispatch_request", lambda req: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(bridge, "append_admin_audit", lambda *a, **kw: None)

    result = bridge.evaluate_agn1_dispatch(
        task_id="task-crash",
        risk_level="high",
        side_effect_level="write",
        request_summary="test",
    )
    assert result["allowed"] is False, "Evaluation crash should fail-closed"
    assert result["rule_id"] == "evaluation_error"


def test_governance_bridge_allows_when_requires_gate_false(monkeypatch) -> None:
    """Normal flow: when requires_gate is explicitly False, dispatch is allowed."""
    import scripts.agn2_governance_bridge as bridge

    monkeypatch.setattr(bridge, "evaluate_dispatch_request", lambda req: {"requires_gate": False, "rule_id": ""})
    monkeypatch.setattr(bridge, "append_admin_audit", lambda *a, **kw: None)

    result = bridge.evaluate_agn1_dispatch(
        task_id="task-ok",
        risk_level="low",
        side_effect_level="read_only",
        request_summary="test",
    )
    assert result["allowed"] is True


# ── Bug H: constitution cache invalidation ─────────────────────────


def test_constitution_cache_invalidated_on_file_change(monkeypatch, tmp_path: Path) -> None:
    """Protected paths cache should reload when constitution file mtime changes."""
    import scripts.agn2_governance_bridge as bridge

    # Reset module cache
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

    # Update constitution
    constitution_v2 = dict(constitution_v1)
    constitution_v2["immutability"] = dict(constitution_v1["immutability"])
    constitution_v2["immutability"]["agent_may_not_modify"] = ["secret_v2.json"]
    constitution_file.write_text(json.dumps(constitution_v2), encoding="utf-8")
    # Touch file to ensure different mtime
    os.utime(constitution_file, (time.time() + 1, time.time() + 1))

    monkeypatch.setattr(bridge, "load_constitution", lambda: constitution_v2)

    paths_v2 = bridge._load_protected_paths()
    assert any("secret_v2" in p for p in paths_v2), f"Cache not invalidated: {paths_v2}"
    assert not any("secret_v1" in p for p in paths_v2), f"Old paths still present: {paths_v2}"


# ── Bug I: reviewer hallucination false positive on infrastructure ──


def test_is_infrastructure_failure_none_verdict_is_infra() -> None:
    """When verdict_payload is None (file couldn't be loaded or reviewer
    crashed), _is_infrastructure_failure must return True."""
    from scripts.reviewer_worker import _is_infrastructure_failure

    assert _is_infrastructure_failure(None) is True
    assert _is_infrastructure_failure(None, verdict_file_exists=True) is True
    assert _is_infrastructure_failure(None, verdict_file_exists=False) is True


def test_is_infrastructure_failure_valid_reject_is_not_infra() -> None:
    """A genuine content-based reject should NOT be treated as infrastructure."""
    from scripts.reviewer_worker import _is_infrastructure_failure

    verdict = {"decision": "reject", "fail_reasons": ["content_quality_low"]}
    assert _is_infrastructure_failure(verdict) is False


def test_is_infrastructure_failure_provider_unavailable_is_infra() -> None:
    """A reviewer_unavailable error in fail_reasons is infrastructure."""
    from scripts.reviewer_worker import _is_infrastructure_failure

    verdict = {"decision": "reject", "fail_reasons": ["reviewer_unavailable:timeout"]}
    assert _is_infrastructure_failure(verdict) is True


# ── Bug J: reviewer timeout does not overwrite success ──────────────


def test_reviewer_timeout_does_not_overwrite_success() -> None:
    """Verify the reviewer_worker has the same timeout fix as executor:
    code=0 must not be overwritten by code=124 on deadline exceeded."""
    import inspect
    import scripts.reviewer_worker as rw

    source = inspect.getsource(rw.process_once)
    # The fix should contain "task_timed_out and code != 0"
    assert "task_timed_out and code != 0" in source, (
        "reviewer_worker timeout should only apply when code != 0"
    )


# ── Bug K: coordinator stale recovery uses locked_update ────────────


def test_coordinator_stale_recovery_uses_locked_update() -> None:
    """_recover_stale_dispatches must use locked_update instead of
    raw get_task+save_task to prevent TOCTOU races."""
    import inspect
    import scripts.coordinator_loop as cl

    source = inspect.getsource(cl._recover_stale_dispatches)
    assert "locked_update" in source, (
        "_recover_stale_dispatches should use locked_update for atomic read-modify-write"
    )
    assert "store.save_task" not in source, (
        "_recover_stale_dispatches should NOT use raw save_task"
    )


# ── Bug L: coordinator retry reset uses locked_update ───────────────


def test_coordinator_retry_reset_uses_locked_update() -> None:
    """Retry state reset in process_once must use locked_update."""
    import inspect
    import scripts.coordinator_loop as cl

    source = inspect.getsource(cl.process_once)
    # Check the retry section uses locked_update
    assert "locked_update" in source, (
        "process_once retry reset should use locked_update"
    )


# ── Bug M: coordinator gate default is fail-safe ────────────────────


def test_coordinator_gate_default_is_fail_safe() -> None:
    """The coordinator must default to blocked (False) when the gate result
    is missing the 'allowed' key."""
    import inspect
    import scripts.coordinator_loop as cl

    source = inspect.getsource(cl.process_once)
    # Should contain get("allowed", False) not get("allowed", True)
    assert 'get("allowed", False)' in source, (
        "coordinator gate check should default to False (fail-safe)"
    )


# ── Bug N: coordinator lock file cleanup ────────────────────────────


def test_coordinator_dispatch_lock_file_cleanup() -> None:
    """Lock files created for dispatch TOCTOU protection must be cleaned up."""
    import inspect
    import scripts.coordinator_loop as cl

    source = inspect.getsource(cl.process_once)
    assert "lock_file.unlink" in source, (
        "dispatch lock files should be cleaned up after use"
    )
