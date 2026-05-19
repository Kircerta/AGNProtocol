from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import vision_parser as vp


def test_parse_vision_ref_emits_structured_outputs(monkeypatch) -> None:
    monkeypatch.setattr(vp, "_require_vision_dependencies", lambda: None)
    monkeypatch.setattr(vp, "_resolve_image_ref", lambda _ref: vp.Path("/tmp/sample.png"))
    monkeypatch.setattr(vp, "_image_dimensions", lambda _path: (640, 480))
    monkeypatch.setattr(
        vp,
        "_ocr_words",
        lambda _path: [
            {"text": "Ghostty", "confidence": 98.0, "left": 10, "top": 20, "width": 50, "height": 12},
            {"text": "Ghostty", "confidence": 97.0, "left": 70, "top": 20, "width": 50, "height": 12},
            {"text": "Status", "confidence": 96.0, "left": 130, "top": 20, "width": 40, "height": 12},
        ],
    )
    monkeypatch.setattr(vp, "write_text_artifact", lambda **_kwargs: SimpleNamespace(ref="agn://artifact/" + "c" * 64))
    refs = iter(
        [
            "agn://artifact/" + "d" * 64,
            "agn://artifact/" + "e" * 64,
            "agn://artifact/" + "f" * 64,
            "agn://artifact/" + "1" * 64,
            "agn://artifact/" + "2" * 64,
            "agn://artifact/" + "3" * 64,
        ]
    )
    monkeypatch.setattr(vp, "write_json_artifact", lambda **_kwargs: SimpleNamespace(ref=next(refs)))

    payload = vp.parse_vision_ref(task_id="vision-task", attempt=1, image_ref="agn://artifact/" + "a" * 64)
    assert payload["ok"] is True
    assert payload["word_count"] == 3
    assert payload["dimensions"] == {"width": 640, "height": 480}
    assert payload["summary_ref"].startswith("agn://artifact/")
    assert payload["ocr_text_ref"].startswith("agn://artifact/")
    assert payload["ui_tree_ref"].startswith("agn://artifact/")
    assert payload["security_ref"].startswith("agn://artifact/")
    assert payload["security_scan"]["quarantined"] is False


def test_parse_vision_ref_redacts_sensitive_outputs(monkeypatch) -> None:
    monkeypatch.setattr(vp, "_require_vision_dependencies", lambda: None)
    monkeypatch.setattr(vp, "_resolve_image_ref", lambda _ref: vp.Path("/tmp/sample.png"))
    monkeypatch.setattr(vp, "_image_dimensions", lambda _path: (640, 480))
    monkeypatch.setattr(
        vp,
        "_ocr_words",
        lambda _path: [
            {"text": "Password", "confidence": 98.0, "left": 10, "top": 20, "width": 50, "height": 12},
            {"text": "admin@example.com", "confidence": 97.0, "left": 70, "top": 20, "width": 120, "height": 12},
        ],
    )
    text_artifacts: list[tuple[str, str]] = []
    json_payloads: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        vp,
        "write_text_artifact",
        lambda **kwargs: text_artifacts.append((str(kwargs["artifact_id"]), str(kwargs["content"]))) or SimpleNamespace(ref="agn://artifact/" + str(len(text_artifacts)).rjust(64, "c")),
    )
    monkeypatch.setattr(
        vp,
        "write_json_artifact",
        lambda **kwargs: json_payloads.append((str(kwargs["artifact_id"]), dict(kwargs["payload"]))) or SimpleNamespace(ref="agn://artifact/" + str(len(json_payloads)).rjust(64, "d")),
    )

    payload = vp.parse_vision_ref(task_id="vision-task", attempt=1, image_ref="agn://artifact/" + "a" * 64)
    assert payload["ocr_redacted"] is True
    assert payload["security_scan"]["quarantined"] is True
    assert any(artifact_id == "vision_ocr_text" and "[REDACTED:password_field]" in item for artifact_id, item in text_artifacts)
    assert any(artifact_id == "vision_ocr_text" and "[REDACTED:email_address]" in item for artifact_id, item in text_artifacts)
    assert any(artifact_id == "vision_ocr_text_evidence" and "admin@example.com" in item for artifact_id, item in text_artifacts)
    security_payloads = [item for artifact_id, item in json_payloads if artifact_id == "vision_security_scan"]
    assert security_payloads
    assert security_payloads[-1]["quarantined"] is True
    assert payload["evidence_refs"]["ocr_text_ref"].startswith("agn://artifact/")


def test_register_image_path_writes_file_artifact(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "shot.png"
    source.write_bytes(b"fake-png")
    monkeypatch.setattr(
        vp,
        "write_file_artifact",
        lambda **kwargs: SimpleNamespace(ref="agn://artifact/" + "a" * 64, artifact_id=str(kwargs["artifact_id"]), media_type=str(kwargs["media_type"])),
    )
    payload = vp.register_image_path(task_id="vision-task", attempt=1, image_path=str(source))
    assert payload["image_ref"].startswith("agn://artifact/")
    assert payload["media_type"] == "image/png"
    assert payload["image_path"] == str(Path(source).resolve())


def test_parse_vision_ref_requires_local_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(vp.shutil, "which", lambda _name: "")
    with pytest.raises(RuntimeError, match="vision_dependencies_missing:sips,tesseract"):
        vp.parse_vision_ref(task_id="vision-task", attempt=1, image_ref="agn://artifact/" + "a" * 64)
