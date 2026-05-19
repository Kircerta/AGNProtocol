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


def _token(sub: str = "admin") -> str:
    payload = {
        "sub": sub,
        "exp": datetime.now(tz=timezone.utc) + timedelta(minutes=10),
    }
    return jwt.encode(payload, SECRET, algorithm=ALGO)


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
    return TestClient(app), ssot_dir


def test_unlock_and_external_publish_approval_endpoints(tmp_path: Path) -> None:
    client, ssot_dir = _make_client(tmp_path)
    store = SSOTStore(ssot_dir)
    task_id = "task-governance-1"
    store.save_task(
        {
            "id": task_id,
            "source": "manual",
            "request_text": "governance endpoint test",
            "agn_managed": True,
            "review_requested": True,
            "qa_retry_count": 3,
            "lock_state": "halted",
            "lock_reason": "qa_retry_count_threshold_reached:3",
            "locked_at": datetime.now(tz=timezone.utc).isoformat(),
            "allow_external_publish": False,
            "admin_approved": False,
        }
    )

    unauthorized_unlock = client.post(f"/api/tasks/{task_id}/unlock")
    assert unauthorized_unlock.status_code == 401

    unlock_resp = client.post(
        f"/api/tasks/{task_id}/unlock",
        headers={"Authorization": f"Bearer {_token('alice-admin')}"},
    )
    assert unlock_resp.status_code == 200
    unlock_payload = unlock_resp.json()
    assert unlock_payload["lock_state"] == "active"
    assert int(unlock_payload["qa_retry_count"]) == 0

    approve_resp = client.post(
        f"/api/tasks/{task_id}/approve-external-publish",
        headers={"Authorization": f"Bearer {_token('alice-admin')}"},
    )
    assert approve_resp.status_code == 200
    approve_payload = approve_resp.json()
    assert approve_payload["allow_external_publish"] is True
    assert approve_payload["admin_approved"] is True
    assert approve_payload["admin_approved_by"] == "alice-admin"

    detail = client.get(f"/api/tasks/{task_id}")
    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload["allow_external_publish"] is True
    assert detail_payload["admin_approved"] is True

    persisted = json.loads((ssot_dir / f"{task_id}.json").read_text(encoding="utf-8"))
    assert persisted["lock_state"] == "active"
    assert persisted["admin_approved"] is True
