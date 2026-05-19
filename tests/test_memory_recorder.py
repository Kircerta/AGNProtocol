from __future__ import annotations

from pathlib import Path

import pytest

from scripts import memory_recorder as mr


def test_append_record_and_iter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mr, "RECORDS_DIR", tmp_path / "memory" / "records")
    record = mr.append_record(
        {
            "kind": "fact",
            "scope": "agenticnetwork/runtime",
            "summary": "dispatcher became canonical entrypoint",
            "fact_payload": {"dispatcher": True},
            "source_refs": ["agn://artifact/" + "a" * 64],
            "trace_id": "trace-3",
            "task_id": "task-3",
            "author": "codex",
            "confidence": "high",
        }
    )
    assert record["record_id"].startswith("mem-")
    loaded = mr.iter_records(scope="agenticnetwork/runtime")
    assert len(loaded) == 1
    assert loaded[0]["summary"] == "dispatcher became canonical entrypoint"


def test_invalid_memory_record_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mr, "RECORDS_DIR", tmp_path / "memory" / "records")
    with pytest.raises(ValueError):
        mr.append_record({"kind": "unknown", "summary": "", "fact_payload": []})


def test_memory_record_rejects_oversized_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mr, "RECORDS_DIR", tmp_path / "memory" / "records")
    quarantine_dir = tmp_path / "reports" / "memory_recorder" / "quarantine"
    monkeypatch.setattr(mr, "QUARANTINE_DIR", quarantine_dir)
    with pytest.raises(mr.MemoryRecordValidationError) as exc_info:
        mr.append_record(
            {
                "kind": "fact",
                "scope": "agn2/codex",
                "summary": "x" * (mr.MAX_SUMMARY_CHARS + 1),
                "fact_payload": {"blob": "y" * (mr.MAX_FACT_PAYLOAD_BYTES + 100)},
                "source_refs": [],
            }
        )
    assert "quarantine_ref=" in str(exc_info.value)
    quarantine_files = list(quarantine_dir.rglob("*.json"))
    assert len(quarantine_files) == 1
    payload = __import__("json").loads(quarantine_files[0].read_text(encoding="utf-8"))
    assert payload["kind"] == "memory_append_quarantine"
    assert "summary_too_large" in payload["errors"]
