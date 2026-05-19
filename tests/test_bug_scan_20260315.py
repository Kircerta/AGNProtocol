"""Regression tests for bugs found in the 2026-03-15 commit scan.

Each test is named after the bug hypothesis that drove its creation.
All bugs were confirmed via experiments before the fix was applied.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

# ── Bug A: vision_parser._ocr_words ValueError on malformed TSV coords ──


def test_ocr_words_tolerates_non_numeric_coordinate_fields(monkeypatch) -> None:
    """_ocr_words should not crash when tesseract emits non-numeric
    values in the left/top/width/height columns."""
    from scripts import vision_parser as vp

    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\tBAD\t20\t50\t12\t98.0\tGhostty\n"
        "5\t1\t1\t1\t1\t2\t100\tNAN\t50\t12\t95.0\tStatus\n"
    )
    monkeypatch.setattr(vp, "_run_command", lambda _cmd: (0, tsv, ""))
    words = vp._ocr_words(Path("/tmp/fake.png"))
    assert len(words) == 2
    assert words[0]["text"] == "Ghostty"
    assert words[0]["left"] == 0  # BAD -> fallback to 0
    assert words[1]["top"] == 0  # NAN -> fallback to 0
    assert words[1]["left"] == 100  # valid int preserved


# ── Bug B: dispatcher_runtime._handler_reviewer ValueError on non-numeric payload ──


def test_handler_reviewer_tolerates_non_numeric_excerpt_chars(monkeypatch, tmp_path: Path) -> None:
    """Passing a non-numeric excerpt_chars in input_payload should not crash."""
    from scripts import dispatcher_runtime as dr

    _isolate_dispatcher(monkeypatch, tmp_path)
    monkeypatch.setattr(dr, "publish_message", lambda payload: {**payload, "id": "msg-review", "ack_required": False})
    monkeypatch.setattr(dr, "acknowledge_message", lambda *_a, **_kw: {"ack_status": "acked"})
    monkeypatch.setattr(dr, "append_event", lambda **_kw: None)

    review_calls: list[dict[str, object]] = []

    def fake_review(**kwargs):
        review_calls.append(kwargs)
        return {"verdict": "ok", "confidence": "high"}

    monkeypatch.setattr(dr, "run_review", fake_review)

    result = dr.dispatch_request(
        {
            "trace_id": "trace-review-b",
            "task_id": "task-review-b",
            "caller": "codex",
            "target": "reviewer",
            "target_kind": "reviewer",
            "intent": "review_file",
            "reason": "test non-numeric excerpt_chars",
            "risk_level": "low",
            "input_refs": ["scripts/dispatcher_runtime.py"],
            "input_payload": {
                "excerpt_chars": "auto",
                "timeout_sec": "fast",
            },
        }
    )
    assert result["ok"] is True
    assert review_calls
    assert review_calls[0]["excerpt_chars"] == 4000  # safe default
    assert review_calls[0]["timeout_sec"] == 600.0  # safe default


# ── Bug C: control_plane_read_model.build_overview_model double load_system_mode() ──


def test_overview_model_reads_system_mode_once(monkeypatch, tmp_path: Path) -> None:
    """build_overview_model should call load_system_mode() once to avoid
    TOCTOU inconsistency between generated_at and system_mode."""
    import inspect
    from agn.governance import read_models as crm

    source = inspect.getsource(crm.build_overview_model)
    count = source.count("load_system_mode()")
    assert count == 1, (
        f"build_overview_model calls load_system_mode() {count} times; "
        f"expected 1 to avoid TOCTOU race."
    )


# ── Bug D: control_plane_read_model._dispatcher_raw_entries unsorted truncation ──


def test_dispatcher_raw_entries_sorted_before_truncation(monkeypatch, tmp_path: Path) -> None:
    """_dispatcher_raw_entries should sort combined request + result entries
    by ts before truncating to the limit."""
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    from agn.governance import read_models as crm

    requests_dir = tmp_path / "runtime" / "dispatcher" / "requests"
    results_dir = tmp_path / "runtime" / "dispatcher" / "results"
    requests_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    # Create request with LATER timestamp
    (requests_dir / "req-1.json").write_text(
        json.dumps({"request_id": "req-1", "created_at": "2026-03-15T10:00:00+00:00", "trace_id": "t1", "task_id": "t1", "caller": "codex", "target_kind": "provider", "target": "claude", "intent": "test", "risk_level": "low"}),
        encoding="utf-8",
    )
    # Create result with EARLIER timestamp
    (results_dir / "req-0.json").write_text(
        json.dumps({"request_id": "req-0", "completed_at": "2026-03-15T09:00:00+00:00", "trace_id": "t0", "task_id": "t0", "target_kind": "provider", "target": "claude", "ok": True, "failure_class": "", "result": {"handler": "provider"}}),
        encoding="utf-8",
    )

    entries = crm._dispatcher_raw_entries(limit=10)
    assert len(entries) == 2
    # Entries should be sorted by ts: result (09:00) before request (10:00)
    assert entries[0]["kind"] == "dispatcher_result"
    assert entries[1]["kind"] == "dispatcher_request"


# ── Bug E: visual_security.sanitize_ocr_words multi-word pattern leak ──


def test_sanitize_ocr_words_catches_multi_word_sensitive_patterns() -> None:
    """sanitize_ocr_words must redact words that form multi-word
    sensitive patterns like 'verification code' or 'access token'."""
    from scripts.visual_security import sanitize_ocr_words

    words = [
        {"text": "Enter", "confidence": 95, "left": 0, "top": 0, "width": 50, "height": 12, "block_num": 1, "line_num": 1},
        {"text": "verification", "confidence": 95, "left": 60, "top": 0, "width": 80, "height": 12, "block_num": 1, "line_num": 1},
        {"text": "code", "confidence": 95, "left": 150, "top": 0, "width": 40, "height": 12, "block_num": 1, "line_num": 1},
        {"text": "below", "confidence": 95, "left": 200, "top": 0, "width": 40, "height": 12, "block_num": 1, "line_num": 1},
    ]

    sanitized = sanitize_ocr_words(words)
    sanitized_texts = [w["text"] for w in sanitized]
    # "verification" and "code" together form "verification code" which
    # matches the password_field pattern. They must be redacted.
    assert all("[REDACTED:" in text for text in sanitized_texts), (
        f"Expected all words on a sensitive line to be redacted, got: {sanitized_texts}"
    )


def test_sanitize_ocr_words_preserves_clean_lines() -> None:
    """Lines without sensitive patterns should pass through unchanged."""
    from scripts.visual_security import sanitize_ocr_words

    words = [
        {"text": "Hello", "confidence": 95, "left": 0, "top": 0, "width": 50, "height": 12, "block_num": 1, "line_num": 1},
        {"text": "World", "confidence": 95, "left": 60, "top": 0, "width": 50, "height": 12, "block_num": 1, "line_num": 1},
    ]
    sanitized = sanitize_ocr_words(words)
    assert [w["text"] for w in sanitized] == ["Hello", "World"]


# ── Helpers ──


def _isolate_dispatcher(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from scripts import dispatcher_runtime as dr

    runtime_dir = tmp_path / "runtime" / "dispatcher"
    monkeypatch.setattr(dr, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(dr, "REQUESTS_DIR", runtime_dir / "requests")
    monkeypatch.setattr(dr, "RESULTS_DIR", runtime_dir / "results")
