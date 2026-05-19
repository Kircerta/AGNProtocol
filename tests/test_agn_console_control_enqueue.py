from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest

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
            "request_text": "control enqueue test",
            "review_requested": True,
        }
    )


@pytest.mark.parametrize("control_type", ["PAUSE", "RESUME", "STOP", "STATUS", "MODIFY", "DEGRADE", "REORGANIZE", "MARK_ANOMALY", "FALLBACK_TOPIC"])
def test_control_enqueue_all_types(tmp_path: Path, monkeypatch, control_type: str) -> None:
    import agn_api.main as main

    client, store, _ = make_console_client(tmp_path, monkeypatch)
    task_id = f"console-control-{control_type.lower()}"
    _seed_task(store, task_id)

    payload: dict[str, object] = {"control_type": control_type, "payload": {}}
    if control_type == "MODIFY":
        payload["payload"] = {
            "request_summary": "new summary",
            "needs_context_read": True,
            "context_read_path": "README.md",
        }
    if control_type == "FALLBACK_TOPIC":
        payload["payload"] = {"fallback_topic_id": "local_global_dependency"}

    response = client.post(
        f"/api/agn/v1/tasks/{task_id}/controls",
        headers={"Authorization": f"Bearer {_token('ctl-admin')}"},
        json=payload,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["control_type"] == control_type

    pending = main.es.list_pending_control_commands(task_id=task_id)
    assert pending


def test_control_enqueue_rejects_invalid_type(tmp_path: Path, monkeypatch) -> None:
    client, store, _ = make_console_client(tmp_path, monkeypatch)
    task_id = "console-control-invalid"
    _seed_task(store, task_id)

    response = client.post(
        f"/api/agn/v1/tasks/{task_id}/controls",
        headers={"Authorization": f"Bearer {_token('ctl-admin')}"},
        json={"control_type": "REBOOT", "payload": {}},
    )
    assert response.status_code == 400


def test_modify_payload_rejects_unknown_fields(tmp_path: Path, monkeypatch) -> None:
    client, store, _ = make_console_client(tmp_path, monkeypatch)
    task_id = "console-control-modify-invalid"
    _seed_task(store, task_id)

    response = client.post(
        f"/api/agn/v1/tasks/{task_id}/controls",
        headers={"Authorization": f"Bearer {_token('ctl-admin')}"},
        json={
            "control_type": "MODIFY",
            "payload": {
                "request_summary": "ok",
                "repo_path": "/tmp/blocked",
            },
        },
    )
    assert response.status_code == 400


def test_fallback_topic_requires_topic_id(tmp_path: Path, monkeypatch) -> None:
    client, store, _ = make_console_client(tmp_path, monkeypatch)
    task_id = "console-control-fallback-invalid"
    _seed_task(store, task_id)

    response = client.post(
        f"/api/agn/v1/tasks/{task_id}/controls",
        headers={"Authorization": f"Bearer {_token('ctl-admin')}"},
        json={"control_type": "FALLBACK_TOPIC", "payload": {}},
    )
    assert response.status_code == 400


def test_control_enqueue_task_not_found(tmp_path: Path, monkeypatch) -> None:
    client, _, _ = make_console_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/agn/v1/tasks/missing-task/controls",
        headers={"Authorization": f"Bearer {_token('ctl-admin')}"},
        json={"control_type": "STATUS", "payload": {}},
    )
    assert response.status_code == 404
