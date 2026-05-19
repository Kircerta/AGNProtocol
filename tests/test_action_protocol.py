from __future__ import annotations

from scripts.action_protocol import build_action, validate_action_payload


def test_valid_action_payload_passes() -> None:
    payload = build_action(
        trace_id="trace-1",
        task_id="task-1",
        action_id="action-1",
        action_type="EXECUTE_CMD",
        inputs={"argv": ["echo", "ok"]},
        refs={},
        budget={"max_time_sec": 10, "max_disk_mb": 10, "max_log_kb": 10},
    )
    result = validate_action_payload(payload)
    assert result.valid is True
    assert result.errors == []


def test_oversized_inline_input_rejected() -> None:
    payload = build_action(
        trace_id="trace-2",
        task_id="task-2",
        action_id="action-2",
        action_type="WRITE_FILE",
        inputs={"content": "x" * 5000},
        refs={"target_path": "tmp/out.txt"},
        budget={"max_time_sec": 10, "max_disk_mb": 10, "max_log_kb": 10},
    )
    result = validate_action_payload(payload, inline_limit=1024)
    assert result.valid is False
    assert any("too large" in err for err in result.errors)


def test_unknown_field_rejected() -> None:
    payload = build_action(
        trace_id="trace-3",
        task_id="task-3",
        action_id="action-3",
        action_type="RETRY",
        inputs={"reason": "timeout"},
        refs={},
        budget={"max_time_sec": 10, "max_disk_mb": 10, "max_log_kb": 10},
    )
    payload["unexpected"] = {"x": 1}
    result = validate_action_payload(payload)
    assert result.valid is False
    assert any("unknown field" in err for err in result.errors)
