from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt

from agn_console_test_utils import make_console_client


SECRET = "test-secret-at-least-32-bytes-long"
ALGO = "HS256"


def _token(sub: str = "admin") -> str:
    payload = {
        "sub": sub,
        "exp": datetime.now(tz=timezone.utc) + timedelta(minutes=10),
    }
    return jwt.encode(payload, SECRET, algorithm=ALGO)


def _seed_task(store, task_id: str) -> None:
    store.save_task(
        {
            "id": task_id,
            "source": "manual",
            "request_text": "auth test",
            "review_requested": True,
        }
    )


def test_control_enqueue_requires_jwt(tmp_path: Path, monkeypatch) -> None:
    client, store, _ = make_console_client(tmp_path, monkeypatch)
    task_id = "console-auth-1"
    _seed_task(store, task_id)

    response = client.post(
        f"/api/agn/v1/tasks/{task_id}/controls",
        json={"control_type": "PAUSE", "payload": {}},
    )
    assert response.status_code == 401


def test_control_enqueue_rejects_invalid_jwt(tmp_path: Path, monkeypatch) -> None:
    client, store, _ = make_console_client(tmp_path, monkeypatch)
    task_id = "console-auth-2"
    _seed_task(store, task_id)

    response = client.post(
        f"/api/agn/v1/tasks/{task_id}/controls",
        headers={"Authorization": "Bearer not-a-jwt"},
        json={"control_type": "PAUSE", "payload": {}},
    )
    assert response.status_code == 401


def test_control_enqueue_accepts_valid_jwt(tmp_path: Path, monkeypatch) -> None:
    client, store, _ = make_console_client(tmp_path, monkeypatch)
    task_id = "console-auth-3"
    _seed_task(store, task_id)

    response = client.post(
        f"/api/agn/v1/tasks/{task_id}/controls",
        headers={"Authorization": f"Bearer {_token('alice')}"},
        json={"control_type": "STATUS", "payload": {}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["control_type"] == "STATUS"
