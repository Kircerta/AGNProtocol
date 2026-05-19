from __future__ import annotations

from pathlib import Path

from agn_console_test_utils import make_console_client


def _seed_task(store, task_id: str) -> None:
    store.save_task(
        {
            "id": task_id,
            "source": "manual",
            "request_text": "local-only guard",
            "review_requested": True,
        }
    )


def test_local_only_blocks_remote_hosts(tmp_path: Path, monkeypatch) -> None:
    import agn_api.main as main

    client, store, _ = make_console_client(tmp_path, monkeypatch, local_only_mode=True)
    _seed_task(store, "local-only-1")

    monkeypatch.setattr(main, "_request_client_host", lambda _request: "203.0.113.12")
    response = client.get("/api/agn/v1/overview")
    assert response.status_code == 403
    assert "Local-only mode" in response.json()["detail"]


def test_local_only_can_be_disabled_for_debug(tmp_path: Path, monkeypatch) -> None:
    import agn_api.main as main

    client, store, _ = make_console_client(tmp_path, monkeypatch, local_only_mode=False)
    _seed_task(store, "local-only-2")

    monkeypatch.setattr(main, "_request_client_host", lambda _request: "203.0.113.12")
    response = client.get("/api/agn/v1/overview")
    assert response.status_code == 200
