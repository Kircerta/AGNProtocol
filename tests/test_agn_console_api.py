from __future__ import annotations

from pathlib import Path

from agn_api.task_engine import derive_status
from agn_console_test_utils import make_console_client

try:
    from action_protocol import build_action
except ImportError:  # pragma: no cover
    from scripts.action_protocol import build_action


BASE_BUDGET = {"max_time_sec": 120.0, "max_disk_mb": 128.0, "max_log_kb": 256.0}


def _seed_task(*, store, task_id: str, trace_id: str) -> None:
    import agn_api.main as main

    store.save_task(
        {
            "id": task_id,
            "source": "manual",
            "request_text": "verify api",
            "request_summary": "verify api summary",
            "review_requested": True,
            "risk_level": "low",
            "correlation_id": trace_id,
            "created_at": "2026-03-04T01:00:00+00:00",
        }
    )
    main.es.write_checkpoint(
        task_id,
        {
            "task_id": task_id,
            "trace_id": trace_id,
            "state": "EXEC_DONE",
            "paused": False,
            "spec_revision": 1,
            "last_event_time": "2026-03-04T01:01:00+00:00",
        },
    )
    main.es.append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="HEARTBEAT_TICK",
        payload={"note": "seed"},
    )
    main.es.append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="STATE_TRANSITION",
        payload={"from": "PLANNED", "to": "EXEC_DONE", "reason": "seed"},
    )


def test_agn_console_read_api_endpoints(tmp_path: Path, monkeypatch) -> None:
    client, store, _ = make_console_client(tmp_path, monkeypatch)
    task_id = "console-task-1"
    trace_id = "trace-console-task-1"
    _seed_task(store=store, task_id=task_id, trace_id=trace_id)

    overview = client.get("/api/agn/v1/overview")
    assert overview.status_code == 200
    overview_data = overview.json()
    assert "task_counts_by_state" in overview_data
    assert "queue_counts" in overview_data
    assert "watchdog_summary" in overview_data

    tasks = client.get("/api/agn/v1/tasks")
    assert tasks.status_code == 200
    payload = tasks.json()
    assert payload["total"] >= 1
    first = next(item for item in payload["tasks"] if item["id"] == task_id)
    assert first["checkpoint_state"] == "EXEC_DONE"
    assert first["status"] == derive_status({"review_requested": True})

    detail = client.get(f"/api/agn/v1/tasks/{task_id}")
    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload["id"] == task_id
    assert detail_payload["trace_id"] == trace_id

    checkpoint = client.get(f"/api/agn/v1/tasks/{task_id}/checkpoint")
    assert checkpoint.status_code == 200
    assert checkpoint.json()["state"] == "EXEC_DONE"

    timeline = client.get(f"/api/agn/v1/tasks/{task_id}/timeline?limit=50")
    assert timeline.status_code == 200
    events = timeline.json()["events"]
    assert any(item["event_type"] == "STATE_TRANSITION" for item in events)


def test_agn_console_pending_actions_and_trace_events(tmp_path: Path, monkeypatch) -> None:
    import agn_api.main as main

    client, store, _ = make_console_client(tmp_path, monkeypatch)
    task_id = "console-task-2"
    trace_id = "trace-console-task-2"
    _seed_task(store=store, task_id=task_id, trace_id=trace_id)

    action = build_action(
        trace_id=trace_id,
        task_id=task_id,
        action_id="exec-test-001",
        action_type="EXECUTE_CMD",
        inputs={"argv": ["echo", "hello"], "timeout_sec": 30},
        refs={},
        budget=BASE_BUDGET,
        source_role="coordinator",
        state_hint="DISPATCHED_EXEC",
    )
    main.es.enqueue_action(action)

    pending = client.get(f"/api/agn/v1/tasks/{task_id}/pending-actions")
    assert pending.status_code == 200
    actions = pending.json()["actions"]
    assert len(actions) == 1
    assert actions[0]["action_type"] == "EXECUTE_CMD"
    assert actions[0]["schema_valid"] is True

    trace_events = client.get(f"/api/agn/v1/traces/{trace_id}/events?limit=20")
    assert trace_events.status_code == 200
    assert trace_events.json()["trace_id"] == trace_id
    assert isinstance(trace_events.json()["events"], list)


def test_agn_console_messages_endpoint(tmp_path: Path, monkeypatch) -> None:
    import agn_api.main as main

    client, store, _ = make_console_client(tmp_path, monkeypatch)
    task_id = "console-task-msg"
    trace_id = "trace-console-msg"
    _seed_task(store=store, task_id=task_id, trace_id=trace_id)
    main.es.append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="RESEARCH_MESSAGE",
        payload={
            "actor": "coordinator",
            "role": "coordinator",
            "surface": "openclaw",
            "kind": "proposal_packet",
            "attempt": 1,
            "round": 1,
            "message_ref": "agn://artifact/" + ("a" * 64),
            "preview": "packet",
            "packet_chars": 128,
            "sha256": "b" * 64,
        },
    )

    response = client.get(f"/api/agn/v1/tasks/{task_id}/messages?limit=20")
    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == task_id
    assert payload["messages"][0]["actor"] == "coordinator"
    assert payload["messages"][0]["role"] == "coordinator"
    assert payload["messages"][0]["attempt"] == 1
    assert payload["messages"][0]["task_id"] == task_id
    assert payload["messages"][0]["correlation_id"] == trace_id
    assert payload["messages"][0]["message_ref"].startswith("agn://artifact/")


def test_agn_console_controls_listing_by_status(tmp_path: Path, monkeypatch) -> None:
    import agn_api.main as main

    client, store, _ = make_console_client(tmp_path, monkeypatch)
    task_id = "console-task-3"
    trace_id = "trace-console-task-3"
    _seed_task(store=store, task_id=task_id, trace_id=trace_id)

    pending_path = main.es.enqueue_control_command(
        {
            "control_type": "PAUSE",
            "control_id": "ctl-pending",
            "task_id": task_id,
            "payload": {},
        }
    )
    done_path = main.es.enqueue_control_command(
        {
            "control_type": "STATUS",
            "control_id": "ctl-done",
            "task_id": task_id,
            "payload": {},
        }
    )
    failed_path = main.es.enqueue_control_command(
        {
            "control_type": "STOP",
            "control_id": "ctl-failed",
            "task_id": task_id,
            "payload": {},
        }
    )
    main.es.move_control_file(done_path, status="done")
    main.es.move_control_file(failed_path, status="failed")
    assert pending_path.exists()

    pending = client.get(f"/api/agn/v1/tasks/{task_id}/controls?status=pending")
    assert pending.status_code == 200
    assert any(item["control_id"] == "ctl-pending" for item in pending.json()["controls"])

    done = client.get(f"/api/agn/v1/tasks/{task_id}/controls?status=done")
    assert done.status_code == 200
    assert any(item["control_id"] == "ctl-done" for item in done.json()["controls"])

    failed = client.get(f"/api/agn/v1/tasks/{task_id}/controls?status=failed")
    assert failed.status_code == 200
    assert any(item["control_id"] == "ctl-failed" for item in failed.json()["controls"])


def test_agn_tasks_sorted_by_updated_at_desc(tmp_path: Path, monkeypatch) -> None:
    client, store, _ = make_console_client(tmp_path, monkeypatch)
    store.save_task(
        {
            "id": "task-old",
            "source": "manual",
            "request_text": "old",
            "review_requested": True,
            "updated_at": "2026-03-04T00:00:00+00:00",
        }
    )
    store.save_task(
        {
            "id": "task-new",
            "source": "manual",
            "request_text": "new",
            "review_requested": True,
            "updated_at": "2026-03-04T10:00:00+00:00",
        }
    )

    response = client.get("/api/agn/v1/tasks?limit=2")
    assert response.status_code == 200
    items = response.json()["tasks"]
    assert items[0]["id"] == "task-new"
    assert items[1]["id"] == "task-old"
