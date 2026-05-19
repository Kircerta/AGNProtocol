from __future__ import annotations

from pathlib import Path

from scripts.agn_visual_operator import VISUAL_SECURITY_BOUNDARY, build_gui_suggestions, build_visual_payload, find_matches


def test_find_matches_prefers_exact_or_contains_hits() -> None:
    regions = [
        {"kind": "ocr_word", "text": "Submit", "bounds": {"left": 10, "top": 20, "width": 80, "height": 24}, "confidence": 95.0},
        {"kind": "ocr_word", "text": "Cancel", "bounds": {"left": 120, "top": 20, "width": 80, "height": 24}, "confidence": 92.0},
    ]
    ui_tree = {"children": []}
    matches = find_matches(target_texts=["submit"], regions=regions, ui_tree=ui_tree)
    assert matches
    assert matches[0]["candidate_text"] == "Submit"
    assert matches[0]["center"]["x"] == 50


def test_build_gui_suggestions_emits_activation_move_click_and_type() -> None:
    matches = [
        {
            "query": "submit",
            "center": {"x": 50, "y": 32},
        }
    ]
    commands = build_gui_suggestions(
        matches=matches,
        app="Preview",
        type_text="hello",
        press_key="enter",
        log_file=__import__("pathlib").Path("/tmp/gui-agent-log.jsonl"),
    )
    kinds = [item["kind"] for item in commands]
    assert kinds == ["activate_app", "move", "click", "type", "key"]


def test_build_visual_payload_exposes_security_boundary(monkeypatch, tmp_path: Path) -> None:
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(b"png")
    monkeypatch.setattr(
        "scripts.agn_visual_operator.register_image_path",
        lambda **_kwargs: {"image_ref": "agn://artifact/test-image"},
    )
    monkeypatch.setattr(
        "scripts.agn_visual_operator.dispatch_vision_refs",
        lambda *_args, **_kwargs: {
            "ok": True,
            "results": [
                {
                    "regions_ref": "agn://artifact/regions",
                    "ui_tree_ref": "agn://artifact/ui",
                    "ocr_text_ref": "agn://artifact/ocr",
                    "security_scan": {"quarantined": False, "findings": [], "redaction_applied": False},
                }
            ],
        },
    )
    monkeypatch.setattr(
        "scripts.agn_visual_operator._load_json_ref",
        lambda ref: {"regions": [{"kind": "ocr_word", "text": "Submit", "bounds": {"left": 10, "top": 10, "width": 20, "height": 10}, "confidence": 99.0}]} if ref.endswith("regions") else {"children": []},
    )
    monkeypatch.setattr("scripts.agn_visual_operator._load_text_ref", lambda _ref: "Submit")
    payload = build_visual_payload(
        task_id="visual-boundary-test",
        attempt=1,
        trace_id="trace-visual-boundary-test",
        image_path=str(image_path),
        image_ref="",
        capture_path="",
        app="Preview",
        window_name="",
        active_window=False,
        region="",
        target_texts=["Submit"],
        type_text="",
        press_key="",
        apply_activate=False,
        apply_click=False,
        apply_type=False,
        apply_key=False,
    )
    assert payload["security_boundary"] == VISUAL_SECURITY_BOUNDARY
    assert payload["controller_handling_rules"] == VISUAL_SECURITY_BOUNDARY["untrusted_ocr_policy"]


def test_build_visual_payload_quarantines_sensitive_surface_before_parsing(monkeypatch, tmp_path: Path) -> None:
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(b"png")
    called = {"vision": False}
    monkeypatch.setattr(
        "scripts.agn_visual_operator.register_image_path",
        lambda **_kwargs: {"image_ref": "agn://artifact/test-image"},
    )
    monkeypatch.setattr(
        "scripts.agn_visual_operator.dispatch_vision_refs",
        lambda **_kwargs: called.__setitem__("vision", True),
    )
    payload = build_visual_payload(
        task_id="visual-sensitive-surface",
        attempt=1,
        trace_id="trace-visual-sensitive-surface",
        image_path=str(image_path),
        image_ref="",
        capture_path="",
        app="1Password",
        window_name="Vault",
        active_window=False,
        region="",
        target_texts=["Submit"],
        type_text="",
        press_key="",
        apply_activate=True,
        apply_click=True,
        apply_type=False,
        apply_key=False,
    )
    assert payload["quarantined"] is True
    assert payload["vision"] is None
    assert payload["gui_agent_suggestions"] == []
    assert payload["controller_handling_rules"] == VISUAL_SECURITY_BOUNDARY["untrusted_ocr_policy"]
    assert called["vision"] is False


def test_build_visual_payload_redacts_sensitive_ocr_and_blocks_execution(monkeypatch, tmp_path: Path) -> None:
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(b"png")
    monkeypatch.setattr(
        "scripts.agn_visual_operator.register_image_path",
        lambda **_kwargs: {"image_ref": "agn://artifact/test-image"},
    )
    monkeypatch.setattr(
        "scripts.agn_visual_operator.dispatch_vision_refs",
        lambda *_args, **_kwargs: {
            "ok": True,
            "results": [
                {
                    "regions_ref": "agn://artifact/regions",
                    "ui_tree_ref": "agn://artifact/ui",
                    "ocr_text_ref": "agn://artifact/ocr",
                    "security_scan": {
                        "quarantined": True,
                        "findings": [
                            {"label": "password_field", "match": "Password", "source": "ocr"},
                            {"label": "email_address", "match": "admin@example.com", "source": "ocr"},
                        ],
                        "redaction_applied": True,
                    },
                }
            ],
        },
    )
    monkeypatch.setattr(
        "scripts.agn_visual_operator._load_json_ref",
        lambda ref: {"regions": [{"kind": "ocr_word", "text": "Sign in", "bounds": {"left": 10, "top": 10, "width": 20, "height": 10}, "confidence": 99.0}]} if ref.endswith("regions") else {"children": []},
    )
    monkeypatch.setattr(
        "scripts.agn_visual_operator._load_text_ref",
        lambda _ref: "[REDACTED:password_field]: hunter2\nEmail: [REDACTED:email_address]\nSubmit",
    )
    monkeypatch.setattr(
        "scripts.agn_visual_operator._run_commands",
        lambda _commands: [{"should_not_run": True}],
    )
    payload = build_visual_payload(
        task_id="visual-sensitive-ocr",
        attempt=1,
        trace_id="trace-visual-sensitive-ocr",
        image_path=str(image_path),
        image_ref="",
        capture_path="",
        app="Preview",
        window_name="",
        active_window=False,
        region="",
        target_texts=["Submit"],
        type_text="",
        press_key="",
        apply_activate=False,
        apply_click=True,
        apply_type=False,
        apply_key=False,
    )
    assert payload["quarantined"] is True
    assert payload["ocr_preview_redacted"] is True
    assert "[REDACTED:password_field]" in payload["ocr_preview"]
    assert "[REDACTED:email_address]" in payload["ocr_preview"]
    assert payload["execution_results"] == []
    assert payload["execution_blockers"]
    assert payload["controller_handling_rules"] == VISUAL_SECURITY_BOUNDARY["untrusted_ocr_policy"]
