from __future__ import annotations

import contextlib
from datetime import datetime

import pytest

import scripts.research_autonomy as research_autonomy


class _MorningDateTime(datetime):
    @classmethod
    def now(cls) -> "_MorningDateTime":
        return cls(2026, 3, 11, 9, 5)


class _AfternoonDateTime(datetime):
    @classmethod
    def now(cls) -> "_AfternoonDateTime":
        return cls(2026, 3, 11, 15, 5)


class _FakeStore:
    def __init__(self) -> None:
        self.task = {"id": "research-2026-03-11"}

    def get_task(self, task_id: str) -> dict[str, str]:
        return dict(self.task)

    def save_task(self, task: dict[str, str]) -> None:
        self.task = dict(task)

    @contextlib.contextmanager
    def locked_update(self, task_id: str):
        task = self.get_task(task_id)
        yield task
        if task is not None:
            self.save_task(task)


def test_scheduler_morning_window_builds_brief(monkeypatch) -> None:
    saved: list[dict[str, object]] = []
    summaries: list[dict[str, object]] = []
    checkpoints: list[dict[str, object]] = []
    store = _FakeStore()

    monkeypatch.setattr(research_autonomy, "datetime", _MorningDateTime)
    monkeypatch.setattr(research_autonomy, "_load_state", lambda: {"windows": {}, "days": {}})
    monkeypatch.setattr(research_autonomy, "_save_state", lambda payload: saved.append(payload))
    monkeypatch.setattr(research_autonomy, "_task_done", lambda task_id: False)
    monkeypatch.setattr(
        research_autonomy,
        "run_research_unit",
        lambda **kwargs: summaries.append(dict(kwargs)) or {"research_phase": "discussion", "task_id": kwargs["task_id"]},
    )
    monkeypatch.setattr(research_autonomy, "_send_daily_brief", lambda **kwargs: "agn://brief")
    monkeypatch.setattr(research_autonomy, "load_checkpoint", lambda task_id: {"task_id": task_id})
    monkeypatch.setattr(research_autonomy, "write_checkpoint", lambda task_id, payload: checkpoints.append(dict(payload)))
    monkeypatch.setattr(research_autonomy, "SSOTStore", lambda path: store)

    rc = research_autonomy._run_once(
        windows=["09:00", "15:00"],
        executor_provider="codex",
        reviewer_provider="gemini",
        chat_id="chat-1",
    )

    assert rc == 0
    assert summaries
    assert summaries[0]["max_steps"] == 1
    assert summaries[0]["source"] == "autonomy_morning"
    assert checkpoints
    assert checkpoints[-1]["daily_brief_ref"] == "agn://brief"
    assert saved
    assert saved[-1]["days"]["2026-03-11"]["brief_ref"] == "agn://brief"
    assert saved[-1]["windows"]["2026-03-11@09:00"]["status"] == "brief_sent"


def test_scheduler_afternoon_window_launches_autonomy_when_no_manual_override(monkeypatch) -> None:
    launches: list[tuple[str, str, str, str, str]] = []
    saved: list[dict[str, object]] = []

    monkeypatch.setattr(research_autonomy, "datetime", _AfternoonDateTime)
    monkeypatch.setattr(
        research_autonomy,
        "_load_state",
        lambda: {"windows": {"2026-03-11@09:00": {"status": "brief_sent"}}, "days": {"2026-03-11": {"task_id": "research-2026-03-11", "manual_override": False}}},
    )
    monkeypatch.setattr(research_autonomy, "_save_state", lambda payload: saved.append(payload))
    monkeypatch.setattr(research_autonomy, "_task_done", lambda task_id: False)
    monkeypatch.setattr(
        research_autonomy,
        "_launch",
        lambda task_id, unit_date, executor_provider, reviewer_provider, chat_id: launches.append(
            (task_id, unit_date, executor_provider, reviewer_provider, chat_id)
        ),
    )

    rc = research_autonomy._run_once(
        windows=["09:00", "15:00"],
        executor_provider="codex",
        reviewer_provider="gemini",
        chat_id="chat-1",
    )

    assert rc == 0
    assert launches == [("research-2026-03-11", "2026-03-11", "codex", "gemini", "chat-1")]
    assert saved[-1]["windows"]["2026-03-11@15:00"]["status"] == "launched_autonomy"


def test_scheduler_afternoon_window_respects_manual_override(monkeypatch) -> None:
    saved: list[dict[str, object]] = []

    monkeypatch.setattr(research_autonomy, "datetime", _AfternoonDateTime)
    monkeypatch.setattr(
        research_autonomy,
        "_load_state",
        lambda: {"windows": {"2026-03-11@09:00": {"status": "brief_sent"}}, "days": {"2026-03-11": {"task_id": "research-2026-03-11", "manual_override": True}}},
    )
    monkeypatch.setattr(research_autonomy, "_save_state", lambda payload: saved.append(payload))
    monkeypatch.setattr(research_autonomy, "_task_done", lambda task_id: False)
    monkeypatch.setattr(research_autonomy, "_launch", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not launch")))

    rc = research_autonomy._run_once(
        windows=["09:00", "15:00"],
        executor_provider="codex",
        reviewer_provider="gemini",
        chat_id="chat-1",
    )

    assert rc == 0
    assert saved[-1]["windows"]["2026-03-11@15:00"]["status"] == "manual_override"


def test_scheduler_respects_auto_disabled(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        research_autonomy,
        "load_autonomy_config",
        lambda: {"auto_enabled": False, "morning_window": "09:00", "afternoon_window": "15:00"},
    )
    monkeypatch.setattr(research_autonomy, "effective_windows", lambda payload: ["09:00", "15:00"])
    monkeypatch.setattr(research_autonomy.sys, "argv", ["research_autonomy.py", "--once"])

    rc = research_autonomy.main()

    assert rc == 0
    captured = capsys.readouterr()
    assert "\"auto_enabled\": false" in captured.out


def test_scheduler_loop_stays_alive_when_auto_disabled(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    sleeps: list[float] = []

    monkeypatch.setattr(
        research_autonomy,
        "load_autonomy_config",
        lambda: {"auto_enabled": False, "morning_window": "09:00", "afternoon_window": "15:00"},
    )
    monkeypatch.setattr(research_autonomy, "effective_windows", lambda payload: ["09:00", "15:00"])
    monkeypatch.setattr(research_autonomy, "_run_once", lambda **kwargs: calls.append(dict(kwargs)) or 0)

    def stop_after_sleep(seconds: float) -> None:
        sleeps.append(float(seconds))
        raise KeyboardInterrupt

    monkeypatch.setattr(research_autonomy.time, "sleep", stop_after_sleep)
    monkeypatch.setattr(research_autonomy.sys, "argv", ["research_autonomy.py"])

    with pytest.raises(KeyboardInterrupt):
        research_autonomy.main()

    assert not calls
    assert sleeps
