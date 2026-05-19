from __future__ import annotations

import json

import scripts.network_runtime as network_runtime


def test_publish_runtime_surface_writes_briefing_and_change_event(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(network_runtime, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(network_runtime, "AUTONOMY_CONFIG_PATH", tmp_path / "research_autonomy_config.json")
    monkeypatch.setattr(network_runtime, "RUNTIME_STATE_PATH", tmp_path / "network_runtime_state.json")
    monkeypatch.setattr(network_runtime, "BRIEFING_JSON_PATH", tmp_path / "coordinator_network_briefing.json")
    monkeypatch.setattr(network_runtime, "BRIEFING_MD_PATH", tmp_path / "coordinator_network_briefing.md")
    monkeypatch.setattr(network_runtime, "CHANGE_EVENT_PATH", tmp_path / "coordinator_change_event.json")
    monkeypatch.setattr(network_runtime, "CHANGE_EVENT_HISTORY_PATH", tmp_path / "coordinator_change_events.jsonl")
    monkeypatch.setattr(network_runtime, "DUTY_REFRESH_PATH", tmp_path / "coordinator_duty_refresh.json")
    monkeypatch.setattr(network_runtime, "REFRESH_MESSAGE_PATH", tmp_path / "coordinator_refresh_message.txt")
    monkeypatch.setattr(network_runtime, "FIRST_TEST_MESSAGE_PATH", tmp_path / "coordinator_first_research_test.txt")
    monkeypatch.setattr(network_runtime, "_provider_summary", lambda: {"executors_available": ["codex"], "reviewers_available": ["codex"], "default_executor": "codex", "default_reviewer": "codex"})
    monkeypatch.setattr(network_runtime, "append_audit", lambda **kwargs: None)

    briefing, change_event = network_runtime.publish_runtime_surface(reason="test_publish", force_change_event=True)

    assert briefing["telegram_management"]["commands"] == [
        "/agn help",
        "/agn status",
        "/agn costs",
        "/research start",
        "/research status",
        "/research pause",
        "/research fallback",
        "/research mark-exception",
        "/research windows",
        "/research set-morning HH:MM",
        "/research set-afternoon HH:MM",
        "/research auto on",
        "/research auto off",
    ]
    assert change_event["reason"] == "test_publish"
    assert briefing["autonomy"]["afternoon_window"] == "15:00"
    assert network_runtime.BRIEFING_JSON_PATH.exists()
    assert network_runtime.BRIEFING_MD_PATH.exists()
    assert network_runtime.CHANGE_EVENT_PATH.exists()
    rendered = network_runtime.BRIEFING_MD_PATH.read_text(encoding="utf-8")
    assert "/research degrade" not in rendered
    assert "/research start" in network_runtime.render_help_text()
    assert "/research start minimal" in network_runtime.render_help_text()


def test_acknowledge_coordinator_refresh_writes_ack(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(network_runtime, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(network_runtime, "AUTONOMY_CONFIG_PATH", tmp_path / "research_autonomy_config.json")
    monkeypatch.setattr(network_runtime, "RUNTIME_STATE_PATH", tmp_path / "network_runtime_state.json")
    monkeypatch.setattr(network_runtime, "BRIEFING_JSON_PATH", tmp_path / "coordinator_network_briefing.json")
    monkeypatch.setattr(network_runtime, "BRIEFING_MD_PATH", tmp_path / "coordinator_network_briefing.md")
    monkeypatch.setattr(network_runtime, "CHANGE_EVENT_PATH", tmp_path / "coordinator_change_event.json")
    monkeypatch.setattr(network_runtime, "CHANGE_EVENT_HISTORY_PATH", tmp_path / "coordinator_change_events.jsonl")
    monkeypatch.setattr(network_runtime, "DUTY_REFRESH_PATH", tmp_path / "coordinator_duty_refresh.json")
    monkeypatch.setattr(network_runtime, "REFRESH_MESSAGE_PATH", tmp_path / "coordinator_refresh_message.txt")
    monkeypatch.setattr(network_runtime, "FIRST_TEST_MESSAGE_PATH", tmp_path / "coordinator_first_research_test.txt")
    monkeypatch.setattr(network_runtime, "_provider_summary", lambda: {"executors_available": ["codex"], "reviewers_available": ["codex"], "default_executor": "codex", "default_reviewer": "codex"})
    monkeypatch.setattr(network_runtime, "append_audit", lambda **kwargs: None)

    ack = network_runtime.acknowledge_coordinator_refresh(actor="coordinator_loop", refresh_mode="startup")

    assert ack["actor"] == "coordinator_loop"
    assert ack["refresh_mode"] == "startup"
    assert network_runtime.DUTY_REFRESH_PATH.exists()
    written = json.loads(network_runtime.DUTY_REFRESH_PATH.read_text(encoding="utf-8"))
    assert written["confirmed_main_chain"] == ["event_sourcing", "coordinator_heartbeat", "ssot_store", "task_engine", "dashboard"]
    assert "/research degrade" not in written["confirmed_telegram_commands"]
