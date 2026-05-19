from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

import jwt
from fastapi.testclient import TestClient

from agn_api.config import AppConfig
from agn_api.main import create_app
from agn_api.ssot_store import SSOTStore


SECRET = "test-secret-at-least-32-bytes-long"
ALGO = "HS256"



def _token(sub: str = "reviewer-1") -> str:
    payload = {
        "sub": sub,
        "exp": datetime.now(tz=timezone.utc) + timedelta(minutes=10),
    }
    return jwt.encode(payload, SECRET, algorithm=ALGO)



def _write_task(ssot_dir: Path, task_id: str, review_requested: bool = False) -> None:
    store = SSOTStore(ssot_dir)
    store.save_task(
        {
            "id": task_id,
            "title": f"Task {task_id}",
            "review_requested": review_requested,
        }
    )



def _make_client(tmp_path: Path) -> tuple[TestClient, Path]:
    ssot_dir = tmp_path / "ssot"
    audit_path = tmp_path / "audit" / "events.jsonl"
    config = AppConfig(
        ssot_dir=ssot_dir,
        audit_log_path=audit_path,
        jwt_secret=SECRET,
        jwt_algorithm=ALGO,
    )
    app = create_app(config)
    return TestClient(app), audit_path



def test_list_tasks_returns_derived_status(tmp_path: Path) -> None:
    _write_task(tmp_path / "ssot", "task-1", review_requested=False)
    _write_task(tmp_path / "ssot", "task-2", review_requested=True)

    client, _ = _make_client(tmp_path)
    response = client.get("/api/tasks")

    assert response.status_code == 200
    data = response.json()["tasks"]
    assert len(data) == 2

    status_map = {task["id"]: task["status"] for task in data}
    assert status_map["task-1"] == "pending"
    assert status_map["task-2"] == "needs_review"



def test_get_task_detail(tmp_path: Path) -> None:
    _write_task(tmp_path / "ssot", "task-detail", review_requested=True)

    client, _ = _make_client(tmp_path)
    response = client.get("/api/tasks/task-detail")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "task-detail"
    assert data["status"] == "needs_review"



def test_approve_task_requires_jwt_and_persists(tmp_path: Path) -> None:
    _write_task(tmp_path / "ssot", "task-approve", review_requested=True)

    client, audit_path = _make_client(tmp_path)
    unauthorized = client.post("/api/tasks/task-approve/approve")
    assert unauthorized.status_code == 401

    authorized = client.post(
        "/api/tasks/task-approve/approve",
        headers={"Authorization": f"Bearer {_token('alice')}"},
    )

    assert authorized.status_code == 200
    payload = authorized.json()
    assert payload["status"] == "approved"
    assert payload["reviewed_by"] == "alice"

    detail = client.get("/api/tasks/task-approve")
    assert detail.status_code == 200
    assert detail.json()["status"] == "approved"

    lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines
    event = json.loads(lines[-1])
    assert {"route", "status", "task_id", "timestamp"}.issubset(event)



def test_reject_task_persists_immediately(tmp_path: Path) -> None:
    _write_task(tmp_path / "ssot", "task-reject", review_requested=True)

    client, _ = _make_client(tmp_path)
    response = client.post(
        "/api/tasks/task-reject/reject",
        headers={"Authorization": f"Bearer {_token('bob')}"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"

    detail = client.get("/api/tasks/task-reject")
    assert detail.status_code == 200
    assert detail.json()["status"] == "rejected"


def test_dashboard_route(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    modern = client.get("/dashboard")
    assert modern.status_code == 200
    assert "AGN 1.0 Operator Console" in modern.text
