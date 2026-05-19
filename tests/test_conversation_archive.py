from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from scripts import conversation_archive as ca


def _config(tmp_path: Path, primary_db_path: Path) -> dict[str, object]:
    return {
        "scope": "codex_admin",
        "sources": {"codex_homes": [str(tmp_path / ".codex"), str(tmp_path / ".codex_agn")]},
        "storage": {
            "primary_db_path": str(primary_db_path),
            "spool_path": str(tmp_path / "runtime" / "conversation_archive" / "spool" / "pending.jsonl"),
            "state_path": str(tmp_path / "runtime" / "conversation_archive" / "state" / "offsets.json"),
            "status_path": str(tmp_path / "runtime" / "conversation_archive" / "status.json"),
            "audit_path": str(tmp_path / "runtime" / "conversation_archive" / "audit.jsonl"),
            "export_dir": str(tmp_path / "runtime" / "conversation_archive" / "exports"),
            "review_input_dir": str(tmp_path / "runtime" / "conversation_archive" / "review_inputs"),
            "review_report_dir": str(tmp_path / "reports" / "conversation_archive"),
        },
        "poll_interval_seconds": 30.0,
        "review": {
            "enabled": True,
            "cadence_hours": 72,
            "preferred_providers": ["claude", "gemini"],
            "claude_model": "opus",
            "gemini_model": "pro",
        },
    }


def _write_rollout(home: Path, session_id: str, messages: list[tuple[str, str, str]]) -> Path:
    session_dir = home / "sessions" / "2026" / "03" / "13"
    session_dir.mkdir(parents=True, exist_ok=True)
    (home / "session_index.jsonl").write_text(
        json.dumps({"id": session_id, "thread_name": "AGN2.0", "updated_at": "2026-03-13T21:49:52.170022Z"}) + "\n",
        encoding="utf-8",
    )
    path = session_dir / f"rollout-2026-03-13T21-49-52-{session_id}.jsonl"
    lines = [
        {
            "timestamp": "2026-03-13T21:49:52.170Z",
            "type": "session_meta",
            "payload": {"id": session_id},
        },
        {
            "timestamp": "2026-03-13T21:49:53.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "ignore me"}],
            },
        },
    ]
    for timestamp, role, text in messages:
        lines.append(
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
                },
            }
        )
    path.write_text("\n".join(json.dumps(item, ensure_ascii=True) for item in lines) + "\n", encoding="utf-8")
    return path


def test_read_rollout_delta_extracts_only_user_and_assistant(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    session_id = "019ce92c-f8b4-7a73-ad55-3d141442ca01"
    rollout = _write_rollout(
        home,
        session_id,
        [
            ("2026-03-13T21:43:37.099Z", "user", "Use AppleScript aggressively."),
            ("2026-03-13T21:44:00.171Z", "assistant", "I will integrate Ghostty."),
        ],
    )
    records, state = ca.read_rollout_delta(
        path=rollout,
        source_home=home,
        thread_names={session_id: "AGN2.0"},
        state_entry=None,
    )
    assert len(records) == 2
    assert records[0].message["role"] == "user"
    assert records[1].message["role"] == "assistant"
    assert state["line_number"] == 4


def test_scan_once_tracks_offsets_and_only_ingests_appends(tmp_path: Path) -> None:
    config = _config(tmp_path, tmp_path / "primary" / "conversation_archive.sqlite")
    ca.ensure_runtime_dirs(config)
    session_id = "019ce92c-f8b4-7a73-ad55-3d141442ca01"
    rollout = _write_rollout(
        tmp_path / ".codex_agn",
        session_id,
        [("2026-03-13T21:43:37.099Z", "user", "First message.")],
    )
    first = ca.scan_once(config)
    second = ca.scan_once(config)
    assert first["new_messages"] == 1
    assert second["new_messages"] == 0

    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-03-13T21:44:00.171Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Second message."}],
                    },
                },
                ensure_ascii=True,
            )
            + "\n"
        )
    third = ca.scan_once(config)
    assert third["new_messages"] == 1


def test_scan_once_spools_when_primary_storage_is_unavailable(tmp_path: Path) -> None:
    config = _config(tmp_path, tmp_path / "missing-storage" / "conversation_archive.sqlite")
    ca.ensure_runtime_dirs(config)
    _write_rollout(
        tmp_path / ".codex",
        "019ce92c-f8b4-7a73-ad55-3d141442ca01",
        [("2026-03-13T21:43:37.099Z", "user", "Spool me.")],
    )
    result = ca.scan_once(config)
    assert result["stored_in"] == "local_spool"
    assert ca.count_spool_records(config) == 1


def test_run_review_writes_markdown_report(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path, tmp_path / "primary" / "conversation_archive.sqlite")
    ca.ensure_runtime_dirs(config)
    conn = ca.sqlite_connect(Path(config["storage"]["primary_db_path"]))  # type: ignore[index]
    ca.ensure_schema(conn)
    record = ca.IngestRecord(
        session={
            "session_id": "session-1",
            "thread_id": "session-1",
            "thread_name": "AGN2.0",
            "codex_home": str(tmp_path / ".codex"),
            "rollout_path": str(tmp_path / ".codex" / "sessions" / "x.jsonl"),
            "first_ts": "2026-03-13T21:43:37.099Z",
            "last_ts": "2026-03-13T21:44:00.171Z",
        },
        message={
            "message_id": "msg-1",
            "session_id": "session-1",
            "ts": "2026-03-13T21:43:37.099Z",
            "role": "user",
            "speaker": "admin",
            "body": "Use AppleScript better.",
            "body_hash": "hash-1",
            "local_date": "2026-03-13",
            "raw_ref": json.dumps({"rollout_path": "x", "line_number": 1}),
        },
    )
    ca.insert_records_into_db(config, [record], {"files": {}})

    def fake_run_provider_review(_config, provider: str, _prompt: str) -> dict[str, object]:
        return {
            "provider": provider,
            "returncode": 0,
            "stdout": "{}",
            "stderr": "",
            "parsed": {
                "summary": "Review summary.",
                "prompt_hygiene": ["Be more explicit."],
                "verbosity_waste": ["Trim long framing."],
                "instruction_conflicts": [],
                "ambiguity_sources": [],
                "workflow_efficiency_suggestions": ["State the desired end-state first."],
                "agent_error_inducing_patterns": [],
                "notable_positive_patterns": ["Clear goal orientation."],
                "recommended_adjustments": ["Use tighter acceptance criteria."],
            },
        }

    monkeypatch.setattr(ca, "run_provider_review", fake_run_provider_review)
    monkeypatch.setattr(ca, "select_provider", lambda *_args, **_kwargs: ["claude"])
    result = ca.run_review(config, requested_provider="auto", force=True)
    assert result["ok"] is True
    report_path = Path(str(result["report_path"]))
    assert report_path.exists()
    assert "Review summary." in report_path.read_text(encoding="utf-8")
    with sqlite3.connect(str(config["storage"]["primary_db_path"])) as check_conn:  # type: ignore[index]
        row = check_conn.execute("SELECT COUNT(*) FROM review_runs").fetchone()
    assert row[0] == 1


def test_review_payload_is_useful_rejects_empty_shell() -> None:
    empty = ca.normalize_review_payload({})
    rich = ca.normalize_review_payload({"summary": "Real summary.", "prompt_hygiene": ["One issue."]})
    assert ca.review_payload_is_useful(empty) is False
    assert ca.review_payload_is_useful(rich) is True


def test_load_config_filters_non_flagship_reviewers(tmp_path: Path) -> None:
    config_path = tmp_path / "archive-config.json"
    config_path.write_text(
        json.dumps(
            {
                "review": {
                    "preferred_providers": ["deepseek", "claude", "qwen", "gemini"],
                }
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    config = ca.load_config(config_path)
    assert config["review"]["preferred_providers"] == ["claude", "gemini"]  # type: ignore[index]


def test_select_provider_rejects_non_flagship_requested_provider(tmp_path: Path) -> None:
    config = _config(tmp_path, tmp_path / "primary" / "conversation_archive.sqlite")
    try:
        ca.select_provider(config, "deepseek")
    except ValueError as exc:
        assert str(exc) == "unsupported_review_provider:deepseek"
    else:
        raise AssertionError("expected unsupported_review_provider:deepseek")
