from __future__ import annotations

import json

import scripts.telegram_listener as telegram_listener


def test_agn_help_reports_curated_commands(monkeypatch) -> None:
    messages: list[str] = []
    reasons: list[str] = []

    monkeypatch.setattr(telegram_listener, "publish_runtime_surface", lambda **kwargs: reasons.append(str(kwargs.get("reason", ""))))
    monkeypatch.setattr(telegram_listener, "telegram_send_message", lambda **kwargs: messages.append(str(kwargs["text"])))

    handled = telegram_listener._handle_research_command(
        chat_id="chat-1",
        text="/agn help",
        token=None,
        dry_run=False,
        timeout_sec=5.0,
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert handled is True
    assert reasons == ["telegram_help"]
    assert len(messages) == 1
    assert "Plain dialogue is not auto-dispatched" in messages[0]
    assert "/research start" in messages[0]
    assert "/research start minimal" in messages[0]
    assert "/research auto off" in messages[0]


def test_research_start_without_payload_opens_manual_intake(monkeypatch) -> None:
    messages: list[str] = []
    saved_sessions: list[dict[str, object]] = []

    monkeypatch.setattr(telegram_listener, "_set_pending_research_session", lambda chat_id, payload: saved_sessions.append({"chat_id": chat_id, **payload}))
    monkeypatch.setattr(telegram_listener, "telegram_send_message", lambda **kwargs: messages.append(str(kwargs["text"])))

    handled = telegram_listener._handle_research_command(
        chat_id="chat-1",
        text="/research start",
        token=None,
        dry_run=False,
        timeout_sec=5.0,
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert handled is True
    assert saved_sessions
    assert saved_sessions[0]["mode"] == "await_manual_research_input"
    assert "Research Question:" in messages[0]
    assert "/research start minimal" in messages[0]


def test_research_start_minimal_bootstraps_task(monkeypatch) -> None:
    messages: list[str] = []
    queued: list[dict[str, str]] = []

    monkeypatch.setattr(
        telegram_listener,
        "_queue_research_start",
        lambda **kwargs: queued.append({key: str(value) for key, value in kwargs.items()}) or "research-2026-03-11",
    )

    handled = telegram_listener._handle_research_command(
        chat_id="chat-1",
        text="/research start minimal executor=claude reviewer=codex date=2026-03-11",
        token=None,
        dry_run=False,
        timeout_sec=5.0,
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert handled is True
    assert queued
    assert queued[0]["research_mode"] == "manual"
    assert queued[0]["manual_seed_topic_id"] == "local_global_dependency"
    assert queued[0]["executor_provider"] == "claude"
    assert queued[0]["reviewer_provider"] == "codex"


def test_resolve_research_start_task_id_generates_unique_id_without_explicit_task_id(monkeypatch) -> None:
    monkeypatch.setattr(telegram_listener, "_active_brief_task_id", lambda chat_id, unit_date: "")
    monkeypatch.setattr(telegram_listener, "_should_reuse_active_brief_task", lambda task_id, chat_id: False)

    task_id = telegram_listener._resolve_research_start_task_id(
        explicit_task_id="",
        chat_id="chat-1",
        unit_date="2026-03-11",
    )

    assert task_id.startswith("research-2026-03-11-")
    assert task_id != "research-2026-03-11"


def test_resolve_research_start_task_id_reuses_active_brief_task(monkeypatch) -> None:
    monkeypatch.setattr(telegram_listener, "_active_brief_task_id", lambda chat_id, unit_date: "research-2026-03-11")
    monkeypatch.setattr(telegram_listener, "_should_reuse_active_brief_task", lambda task_id, chat_id: True)

    task_id = telegram_listener._resolve_research_start_task_id(
        explicit_task_id="",
        chat_id="chat-1",
        unit_date="2026-03-11",
    )

    assert task_id == "research-2026-03-11"


def test_queue_research_start_uses_generated_unique_id_by_default(monkeypatch) -> None:
    sent: list[str] = []
    saved: list[tuple[str, str, str]] = []
    marked: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        telegram_listener,
        "_resolve_research_start_task_id",
        lambda **kwargs: "research-2026-03-11-abcd1234",
    )
    monkeypatch.setattr(
        telegram_listener,
        "ensure_research_task",
        lambda **kwargs: {"id": kwargs["task_id"], "correlation_id": "corr-123"},
    )
    monkeypatch.setattr(telegram_listener, "save_corr_mapping", lambda correlation_id, task_id, chat_id: saved.append((correlation_id, task_id, chat_id)))
    monkeypatch.setattr(telegram_listener, "_mark_autonomy_manual_override", lambda unit_date, task_id, chat_id: marked.append((unit_date, task_id, chat_id)))
    monkeypatch.setattr(telegram_listener, "publish_runtime_surface", lambda **kwargs: None)
    monkeypatch.setattr(telegram_listener, "telegram_send_message", lambda **kwargs: sent.append(str(kwargs["text"])))

    task_id = telegram_listener._queue_research_start(
        chat_id="chat-1",
        unit_date="2026-03-11",
        scenario="daily",
        executor_provider="gemini",
        reviewer_provider="gemini",
        research_mode="manual",
        question="Q",
        hypothesis="H",
        baseline="B",
        single_change="S",
        research_axis="Machine Learning",
        manual_seed_topic_id="",
        explicit_task_id="",
        token=None,
        dry_run=True,
        timeout_sec=5.0,
        source="telegram_manual",
    )

    assert task_id == "research-2026-03-11-abcd1234"
    assert saved == [("corr-123", "research-2026-03-11-abcd1234", "chat-1")]
    assert marked == [("2026-03-11", "research-2026-03-11-abcd1234", "chat-1")]
    assert any("task_id=research-2026-03-11-abcd1234" in message for message in sent)


def test_pending_manual_submission_launches_task(monkeypatch) -> None:
    queued: list[dict[str, str]] = []
    cleared: list[str] = []

    monkeypatch.setattr(
        telegram_listener,
        "_pending_research_session",
        lambda chat_id: {
            "unit_date": "2026-03-11",
            "scenario": "daily",
            "executor_provider": "codex",
            "reviewer_provider": "codex",
            "task_id": "research-2026-03-11",
        },
    )
    monkeypatch.setattr(telegram_listener, "_active_brief_task_id", lambda chat_id, unit_date: "")
    monkeypatch.setattr(telegram_listener, "_clear_pending_research_session", lambda chat_id: cleared.append(chat_id))
    monkeypatch.setattr(
        telegram_listener,
        "_queue_research_start",
        lambda **kwargs: queued.append({key: str(value) for key, value in kwargs.items()}) or "research-2026-03-11",
    )

    handled = telegram_listener._handle_pending_research_submission(
        chat_id="chat-1",
        text=(
            "Research Question: Can a tiny model recover local spectrum structure under masking?\n"
            "Hypothesis: A tiny 1D convolutional autoencoder will beat linear spectral interpolation."
        ),
        token=None,
        dry_run=False,
        timeout_sec=5.0,
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert handled is True
    assert cleared == ["chat-1"]
    assert queued
    assert queued[0]["question"].startswith("Can a tiny model")
    assert queued[0]["hypothesis"].startswith("A tiny 1D convolutional autoencoder")


def test_research_pause_queues_existing_task(monkeypatch) -> None:
    messages: list[str] = []
    queued: list[dict[str, object]] = []

    monkeypatch.setattr(telegram_listener, "_resolve_research_task_id", lambda raw_task_id, chat_id: "research-2026-03-11")
    monkeypatch.setattr(telegram_listener, "enqueue_control_command", lambda payload: queued.append(dict(payload)))
    monkeypatch.setattr(telegram_listener, "publish_runtime_surface", lambda **kwargs: None)
    monkeypatch.setattr(telegram_listener, "telegram_send_message", lambda **kwargs: messages.append(str(kwargs["text"])))

    handled = telegram_listener._handle_research_command(
        chat_id="chat-1",
        text="/research pause",
        token=None,
        dry_run=False,
        timeout_sec=5.0,
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert handled is True
    assert queued == [{"control_type": "PAUSE", "task_id": "research-2026-03-11", "payload": {}}]
    assert any("control=PAUSE" in message for message in messages)


def test_research_mark_exception_queues_existing_task(monkeypatch) -> None:
    messages: list[str] = []
    queued: list[dict[str, object]] = []

    monkeypatch.setattr(telegram_listener, "_resolve_research_task_id", lambda raw_task_id, chat_id: "research-2026-03-11")
    monkeypatch.setattr(telegram_listener, "enqueue_control_command", lambda payload: queued.append(dict(payload)))
    monkeypatch.setattr(telegram_listener, "publish_runtime_surface", lambda **kwargs: None)
    monkeypatch.setattr(telegram_listener, "telegram_send_message", lambda **kwargs: messages.append(str(kwargs["text"])))

    handled = telegram_listener._handle_research_command(
        chat_id="chat-1",
        text="/research mark-exception",
        token=None,
        dry_run=False,
        timeout_sec=5.0,
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert handled is True
    assert queued == [{"control_type": "MARK_ANOMALY", "task_id": "research-2026-03-11", "payload": {}}]
    assert any("control=MARK_ANOMALY" in message for message in messages)


def test_research_fallback_without_topic_queues_safe_fallback(monkeypatch) -> None:
    messages: list[str] = []
    queued: list[dict[str, object]] = []

    monkeypatch.setattr(telegram_listener, "_resolve_research_task_id", lambda raw_task_id, chat_id: "research-2026-03-11")
    monkeypatch.setattr(telegram_listener, "enqueue_control_command", lambda payload: queued.append(dict(payload)))
    monkeypatch.setattr(telegram_listener, "publish_runtime_surface", lambda **kwargs: None)
    monkeypatch.setattr(telegram_listener, "telegram_send_message", lambda **kwargs: messages.append(str(kwargs["text"])))

    handled = telegram_listener._handle_research_command(
        chat_id="chat-1",
        text="/research fallback",
        token=None,
        dry_run=False,
        timeout_sec=5.0,
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert handled is True
    assert queued == [{"control_type": "FALLBACK_TOPIC", "task_id": "research-2026-03-11", "payload": {"fallback_topic_id": ""}}]
    assert any("topic=auto-safe-fallback" in message for message in messages)


def test_research_windows_reports_current_schedule(monkeypatch) -> None:
    messages: list[str] = []

    monkeypatch.setattr(telegram_listener, "publish_runtime_surface", lambda **kwargs: None)
    monkeypatch.setattr(
        telegram_listener,
        "load_autonomy_config",
        lambda: {"auto_enabled": True, "morning_window": "09:00", "afternoon_window": "15:00"},
    )
    monkeypatch.setattr(telegram_listener, "effective_windows", lambda payload: ["09:00", "15:00"])
    monkeypatch.setattr(telegram_listener, "telegram_send_message", lambda **kwargs: messages.append(str(kwargs["text"])))

    handled = telegram_listener._handle_research_command(
        chat_id="chat-1",
        text="/research windows",
        token=None,
        dry_run=False,
        timeout_sec=5.0,
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert handled is True
    assert len(messages) == 1
    assert "auto_enabled=True" in messages[0]
    assert "morning=09:00" in messages[0]
    assert "afternoon=15:00" in messages[0]
    assert "effective=09:00, 15:00" in messages[0]


def test_research_set_morning_updates_schedule(monkeypatch) -> None:
    messages: list[str] = []

    monkeypatch.setattr(telegram_listener, "publish_runtime_surface", lambda **kwargs: None)
    monkeypatch.setattr(
        telegram_listener,
        "save_autonomy_config",
        lambda payload: {"auto_enabled": True, "morning_window": str(payload["morning_window"]), "afternoon_window": "15:00"},
    )
    monkeypatch.setattr(telegram_listener, "effective_windows", lambda payload: [payload["morning_window"], payload["afternoon_window"]])
    monkeypatch.setattr(telegram_listener, "telegram_send_message", lambda **kwargs: messages.append(str(kwargs["text"])))

    handled = telegram_listener._handle_research_command(
        chat_id="chat-1",
        text="/research set-morning 08:30",
        token=None,
        dry_run=False,
        timeout_sec=5.0,
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert handled is True
    assert "morning=08:30" in messages[0]
    assert "effective=08:30, 15:00" in messages[0]


def test_research_set_afternoon_updates_schedule(monkeypatch) -> None:
    messages: list[str] = []

    monkeypatch.setattr(telegram_listener, "publish_runtime_surface", lambda **kwargs: None)
    monkeypatch.setattr(
        telegram_listener,
        "save_autonomy_config",
        lambda payload: {"auto_enabled": True, "morning_window": "09:00", "afternoon_window": str(payload["afternoon_window"])},
    )
    monkeypatch.setattr(telegram_listener, "effective_windows", lambda payload: [payload["morning_window"], payload["afternoon_window"]])
    monkeypatch.setattr(telegram_listener, "telegram_send_message", lambda **kwargs: messages.append(str(kwargs["text"])))

    handled = telegram_listener._handle_research_command(
        chat_id="chat-1",
        text="/research set-afternoon 15:45",
        token=None,
        dry_run=False,
        timeout_sec=5.0,
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert handled is True
    assert "afternoon=15:45" in messages[0]
    assert "effective=09:00, 15:45" in messages[0]


def test_research_auto_off_updates_schedule(monkeypatch) -> None:
    messages: list[str] = []

    monkeypatch.setattr(telegram_listener, "publish_runtime_surface", lambda **kwargs: None)
    monkeypatch.setattr(
        telegram_listener,
        "save_autonomy_config",
        lambda payload: {"auto_enabled": bool(payload["auto_enabled"]), "morning_window": "09:00", "afternoon_window": "15:00"},
    )
    monkeypatch.setattr(telegram_listener, "effective_windows", lambda payload: ["09:00", "15:00"])
    monkeypatch.setattr(telegram_listener, "telegram_send_message", lambda **kwargs: messages.append(str(kwargs["text"])))

    handled = telegram_listener._handle_research_command(
        chat_id="chat-1",
        text="/research auto off",
        token=None,
        dry_run=False,
        timeout_sec=5.0,
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert handled is True
    assert "auto_enabled=False" in messages[0]


def test_research_degrade_is_not_supported(monkeypatch) -> None:
    messages: list[str] = []
    monkeypatch.setattr(telegram_listener, "_resolve_research_task_id", lambda raw_task_id, chat_id: "research-2026-03-11")
    monkeypatch.setattr(telegram_listener, "telegram_send_message", lambda **kwargs: messages.append(str(kwargs["text"])))

    handled = telegram_listener._handle_research_command(
        chat_id="chat-1",
        text="/research degrade",
        token=None,
        dry_run=False,
        timeout_sec=5.0,
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert handled is True
    assert "unknown command: degrade" in messages[0]


def test_process_message_does_not_dispatch_plain_dialogue(monkeypatch) -> None:
    messages: list[str] = []
    dispatches: list[dict[str, object]] = []

    monkeypatch.setattr(telegram_listener, "telegram_send_message", lambda **kwargs: messages.append(str(kwargs["text"])))
    monkeypatch.setattr(
        telegram_listener,
        "call_coordinator",
        lambda payload, timeout_sec: dispatches.append(dict(payload)) or (0, "{}", ""),
    )

    telegram_listener.process_message(
        update_id=1,
        chat_id="chat-1",
        message_id="17",
        text="除了向你发布研究任务外，你还可以做一些什么？",
        token=None,
        dry_run=False,
        allowed_chats={"chat-1"},
        timeout_sec=5.0,
        default_repo_path="",
        default_work_branch="",
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert not dispatches
    assert len(messages) == 1
    assert "plain dialogue was not dispatched" in messages[0]


def test_process_message_dispatches_explicit_json_task_payload(monkeypatch) -> None:
    messages: list[str] = []
    dispatches: list[dict[str, object]] = []
    saved: list[tuple[str, str, str]] = []

    monkeypatch.setattr(telegram_listener, "telegram_send_message", lambda **kwargs: messages.append(str(kwargs["text"])))
    monkeypatch.setattr(
        telegram_listener,
        "call_coordinator",
        lambda payload, timeout_sec: dispatches.append(dict(payload))
        or (
            0,
            json.dumps(
                {
                    "attempt": 1,
                    "task_id": str(payload["task_id"]),
                    "correlation_id": str(payload["correlation_id"]),
                },
                ensure_ascii=True,
            ),
            "",
        ),
    )
    monkeypatch.setattr(telegram_listener, "save_corr_mapping", lambda correlation_id, task_id, chat_id: saved.append((correlation_id, task_id, chat_id)))

    telegram_listener.process_message(
        update_id=1,
        chat_id="chat-1",
        message_id="18",
        text=json.dumps({"task_id": "task-json-1", "task_kind": "protocol", "request_text": "summarize current AGN status"}),
        token=None,
        dry_run=False,
        allowed_chats={"chat-1"},
        timeout_sec=5.0,
        default_repo_path="",
        default_work_branch="",
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert len(dispatches) == 1
    assert dispatches[0]["task_id"] == "task-json-1"
    assert dispatches[0]["request_text"] == "summarize current AGN status"
    assert saved == [("tg-chat-1-18", "task-json-1", "chat-1")]
    assert len(messages) == 1
    assert "[AGN] accepted" in messages[0]


def test_process_message_does_not_inherit_repo_defaults_for_generic_protocol_task(monkeypatch) -> None:
    dispatches: list[dict[str, object]] = []

    monkeypatch.setattr(telegram_listener, "telegram_send_message", lambda **kwargs: None)
    monkeypatch.setattr(
        telegram_listener,
        "call_coordinator",
        lambda payload, timeout_sec: dispatches.append(dict(payload))
        or (
            0,
            json.dumps(
                {
                    "attempt": 1,
                    "task_id": str(payload["task_id"]),
                    "correlation_id": str(payload["correlation_id"]),
                },
                ensure_ascii=True,
            ),
            "",
        ),
    )
    monkeypatch.setattr(telegram_listener, "save_corr_mapping", lambda correlation_id, task_id, chat_id: None)

    telegram_listener.process_message(
        update_id=1,
        chat_id="chat-1",
        message_id="20",
        text=json.dumps({"task_id": "task-json-2", "request_text": "summarize active sessions"}),
        token=None,
        dry_run=False,
        allowed_chats={"chat-1"},
        timeout_sec=5.0,
        default_repo_path="/tmp/default-repo",
        default_work_branch="main",
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert len(dispatches) == 1
    assert dispatches[0]["task_kind"] == "protocol"
    assert dispatches[0]["repo_path"] == ""
    assert dispatches[0]["work_branch"] == ""


def test_process_message_dispatches_explicit_compact_task_payload(monkeypatch) -> None:
    messages: list[str] = []
    dispatches: list[dict[str, object]] = []

    monkeypatch.setattr(telegram_listener, "telegram_send_message", lambda **kwargs: messages.append(str(kwargs["text"])))
    monkeypatch.setattr(
        telegram_listener,
        "call_coordinator",
        lambda payload, timeout_sec: dispatches.append(dict(payload))
        or (
            0,
            json.dumps(
                {
                    "attempt": 1,
                    "task_id": str(payload["task_id"]),
                    "correlation_id": str(payload["correlation_id"]),
                },
                ensure_ascii=True,
            ),
            "",
        ),
    )
    monkeypatch.setattr(telegram_listener, "save_corr_mapping", lambda correlation_id, task_id, chat_id: None)

    telegram_listener.process_message(
        update_id=1,
        chat_id="chat-1",
        message_id="19",
        text=(
            "TASK_ID=tg-explicit-1\n"
            "TASK_KIND=protocol\n"
            "REQUEST_TEXT=report current coordinator health"
        ),
        token=None,
        dry_run=False,
        allowed_chats={"chat-1"},
        timeout_sec=5.0,
        default_repo_path="",
        default_work_branch="",
        default_executor_provider="codex",
        default_reviewer_provider="gemini",
    )

    assert len(dispatches) == 1
    assert dispatches[0]["task_id"] == "tg-explicit-1"
    assert dispatches[0]["request_text"] == "report current coordinator health"
    assert len(messages) == 1
    assert "[AGN] accepted" in messages[0]
