from __future__ import annotations

from agn.handlers import visual_security as vs


def test_package_visual_security_exposes_metadata() -> None:
    assert vs.PACKAGE_PATH == "agn.handlers.visual_security"
    assert vs.LEGACY_SCRIPT_SHIM == "scripts/visual_security.py"


def test_package_visual_security_redacts_multi_word_sensitive_patterns() -> None:
    words = [
        {"text": "Enter", "confidence": 95, "left": 0, "top": 0, "width": 50, "height": 12, "block_num": 1, "line_num": 1},
        {"text": "verification", "confidence": 95, "left": 60, "top": 0, "width": 80, "height": 12, "block_num": 1, "line_num": 1},
        {"text": "code", "confidence": 95, "left": 150, "top": 0, "width": 40, "height": 12, "block_num": 1, "line_num": 1},
        {"text": "below", "confidence": 95, "left": 200, "top": 0, "width": 40, "height": 12, "block_num": 1, "line_num": 1},
    ]

    sanitized = vs.sanitize_ocr_words(words)
    sanitized_texts = [w["text"] for w in sanitized]
    assert all("[REDACTED:" in text for text in sanitized_texts)
