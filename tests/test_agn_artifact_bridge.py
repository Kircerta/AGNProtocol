from __future__ import annotations

from pathlib import Path

from scripts.agn_artifact_bridge import capture_and_register, infer_media_type, inspect_ref, register_local_path


def test_infer_media_type_for_jsonl_and_png() -> None:
    assert infer_media_type(Path("report.jsonl")) == "application/jsonl"
    assert infer_media_type(Path("shot.png")) == "image/png"


def test_register_and_inspect_round_trip(tmp_path) -> None:
    source = tmp_path / "note.txt"
    source.write_text("hello artifact bridge\n", encoding="utf-8")

    registered = register_local_path(
        task_id="artifact-bridge-test",
        attempt=1,
        path=str(source),
        artifact_id="note",
        source="test",
    )
    inspected = inspect_ref(registered["ref"], mode="all", start_line=1, end_line=20, max_bytes=4096)
    assert inspected["media_type"] == "text/plain"
    assert "hello artifact bridge" in inspected["preview"]


def test_capture_and_register_uses_governed_desktop_action(monkeypatch, tmp_path) -> None:
    capture_path = tmp_path / "shot.png"
    capture_path.write_bytes(b"png")
    monkeypatch.setattr(
        "scripts.agn_artifact_bridge.dispatch_desktop_action",
        lambda *_args, **_kwargs: {
            "ok": True,
            "dispatch_meta": {"request_id": "dispatch-desktop"},
            "result": {"ok": True, "path": str(capture_path)},
        },
    )
    payload = capture_and_register(
        task_id="artifact-capture-test",
        attempt=1,
        capture_path=str(capture_path),
        artifact_id="shot",
        app="Preview",
        window_name="",
        active_window=False,
        region="",
    )
    assert payload["capture"]["ok"] is True
    assert payload["dispatch_meta"]["request_id"] == "dispatch-desktop"
    assert payload["registered"]["artifact_id"] == "shot"
