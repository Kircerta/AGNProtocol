from __future__ import annotations

from pathlib import Path

import pytest

from scripts import command_request as cr


def _isolate_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_ENFORCE_ROLE_GUARD", "0")
    monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "outside_agn")
    monkeypatch.setattr(cr, "COMMAND_REQUESTS_DIR", tmp_path / "requests")
    monkeypatch.setattr(cr, "UTILITY_SANDBOX_DIR", tmp_path / "sandbox")
    monkeypatch.setattr(cr, "AUDIT_PATH", tmp_path / "audit.jsonl")


def test_submit_request_rejects_unknown_operation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        cr.submit_request(operation="shell_exec", params={"cmd": "rm -rf /"})


def test_submit_request_enforces_repo_host_whitelist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("AGN_COMMAND_REQUEST_ALLOWED_HOSTS", "github.com")
    with pytest.raises(ValueError):
        cr.submit_request(
            operation="git_clone",
            params={"repo_url": "https://example.com/repo.git", "target_dir": "x"},
        )


def test_approve_execute_is_one_way(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_paths(monkeypatch, tmp_path)

    payload = cr.submit_request(
        operation="git_checkout",
        params={"repo_dir": "repo-a", "ref": "main"},
        requested_by_role="coordinator",
    )
    request_id = str(payload["request_id"])

    approved = cr.approve_request(request_id, approved_by="admin-a")
    assert approved is not None
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "admin-a"

    rejected = cr.reject_request(request_id, rejected_by="admin-b")
    assert rejected is not None
    assert rejected["status"] == "approved"

    executed_once = cr.execute_approved_requests(executed_by="admin-exec")
    assert len(executed_once) == 1
    assert executed_once[0]["status"] == "executed"
    assert executed_once[0]["executed_by"] == "admin-exec"

    executed_twice = cr.execute_approved_requests(executed_by="admin-exec")
    assert executed_twice == []

    loaded = cr.load_request(request_id)
    assert loaded is not None
    assert loaded["status"] == "executed"
