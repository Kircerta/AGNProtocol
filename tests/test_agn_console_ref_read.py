from __future__ import annotations

from pathlib import Path

from agn_console_test_utils import make_console_client


def _seed_task_with_trace(store, *, task_id: str, trace_id: str) -> None:
    import agn_api.main as main

    store.save_task(
        {
            "id": task_id,
            "source": "manual",
            "request_text": "ref read",
            "review_requested": True,
            "correlation_id": trace_id,
        }
    )
    main.es.write_checkpoint(
        task_id,
        {
            "task_id": task_id,
            "trace_id": trace_id,
            "state": "PLANNED",
            "paused": False,
            "last_event_time": "2026-03-04T00:00:00+00:00",
        },
    )


def test_read_object_ref_excerpt_and_truncation(tmp_path: Path, monkeypatch) -> None:
    client, store, root = make_console_client(tmp_path, monkeypatch)
    task_id = "console-ref-1"
    trace_id = "trace-console-ref-1"
    _seed_task_with_trace(store, task_id=task_id, trace_id=trace_id)

    dispatch_path = root / "dispatch" / f"{task_id}.json"
    dispatch_path.write_text("\n".join([f"line-{idx}-" + ("x" * 64) for idx in range(1, 200)]), encoding="utf-8")

    ref = f"agn://object/dispatch/{trace_id}/1"
    response = client.get(
        "/api/agn/v1/refs/read",
        params={"ref": ref, "mode": "tail", "tail_lines": 20, "max_bytes": 256, "task_id": task_id},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ref"] == ref
    assert payload["truncated"] is True
    assert payload["bytes"] > 256
    assert "truncated-by-max-bytes" in payload["content_excerpt"]


def test_read_artifact_ref_uses_pointer_resolver(tmp_path: Path, monkeypatch) -> None:
    import agn_api.main as main

    client, _, root = make_console_client(tmp_path, monkeypatch)
    artifact_file = root / "reports" / "artifact.txt"
    artifact_file.write_text("artifact content\n" * 20, encoding="utf-8")

    ref = "agn://artifact/" + ("a" * 64)

    def _fake_resolve(_ref: str) -> Path:
        assert _ref == ref
        return artifact_file

    monkeypatch.setattr(main, "resolve_ref_path", _fake_resolve)

    response = client.get(
        "/api/agn/v1/refs/read",
        params={"ref": ref, "mode": "tail", "tail_lines": 5, "max_bytes": 4096},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["media_type"] == "text/plain"
    assert payload["truncated"] is False
    assert "artifact content" in payload["content_excerpt"]


def test_read_ref_rejects_invalid_scheme(tmp_path: Path, monkeypatch) -> None:
    client, _, _ = make_console_client(tmp_path, monkeypatch)

    response = client.get("/api/agn/v1/refs/read", params={"ref": "file:///tmp/secret"})
    assert response.status_code == 400


def test_object_ref_without_task_hint_rejected_when_required(tmp_path: Path, monkeypatch) -> None:
    client, _, _ = make_console_client(tmp_path, monkeypatch)
    ref = "agn://object/dispatch/trace-missing/1"

    response = client.get("/api/agn/v1/refs/read", params={"ref": ref})
    assert response.status_code == 400
