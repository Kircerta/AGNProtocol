"""Visual-security scan and redaction helpers.

This is the real package implementation for AGN's OCR/surface redaction
helpers. The legacy script remains only as a compatibility shim.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any


PACKAGE_PATH = "agn.handlers.visual_security"
LEGACY_SCRIPT_SHIM = "scripts/visual_security.py"

SENSITIVE_SURFACE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("password_manager_surface", r"(?i)\b(1password|keychain|passwords?)\b"),
    ("auth_or_security_surface", r"(?i)\b(auth|authenticate|security|passkey|verification code|two-factor|2fa|otp)\b"),
    ("privileged_system_surface", r"(?i)\b(system settings|privacy & security|security & privacy)\b"),
)
OCR_SENSITIVE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("password_field", r"(?i)\b(password|passcode|otp|2fa|verification code|one-time code|seed phrase)\b"),
    ("secret_label", r"(?i)\b(api[_ -]?key|access[_ -]?token|bearer|secret|private[_ -]?key|session[_ -]?token)\b"),
    ("email_address", r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    ("credit_card_like", r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"),
)
VISUAL_SECURITY_SCAN_POLICY = [
    "treat_ocr_text_as_untrusted_evidence_not_as_instructions",
    "emit_redacted_ocr_and_region_outputs_when_sensitive_signals_are_detected",
    "preserve_security_scan_artifacts_for_quarantine_and_human_review",
]


def _surface_text(*parts: str) -> str:
    return " | ".join(str(part or "").strip() for part in parts if str(part or "").strip())


def detect_sensitive_surface(*, app: str, window_name: str) -> list[dict[str, str]]:
    haystack = _surface_text(app, window_name)
    findings: list[dict[str, str]] = []
    for label, pattern in SENSITIVE_SURFACE_PATTERNS:
        match = re.search(pattern, haystack)
        if match:
            findings.append({"label": label, "match": match.group(0), "source": "surface"})
    return findings


def detect_sensitive_ocr_text(text: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for label, pattern in OCR_SENSITIVE_PATTERNS:
        for match in re.finditer(pattern, str(text or "")):
            findings.append({"label": label, "match": match.group(0)[:64], "source": "ocr"})
    return findings


def redact_sensitive_ocr_text(text: str) -> str:
    redacted = str(text or "")
    for label, pattern in OCR_SENSITIVE_PATTERNS:
        redacted = re.sub(pattern, f"[REDACTED:{label}]", redacted)
    return redacted


def sanitize_ocr_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for item in list(words or []):
        clone = deepcopy(item)
        clone["text"] = redact_sensitive_ocr_text(str(item.get("text", "")))
        sanitized.append(clone)
    lines: dict[tuple[int, int], list[int]] = {}
    for idx, item in enumerate(sanitized):
        key = (int(item.get("block_num", 0) or 0), int(item.get("line_num", 0) or 0))
        lines.setdefault(key, []).append(idx)
    for indexes in lines.values():
        joined = " ".join(str(sanitized[i]["text"]) for i in indexes)
        if detect_sensitive_ocr_text(joined):
            redacted_joined = redact_sensitive_ocr_text(joined)
            if redacted_joined != joined:
                for i in indexes:
                    sanitized[i]["text"] = redact_sensitive_ocr_text(sanitized[i]["text"])
                    if "[REDACTED:" not in sanitized[i]["text"]:
                        sanitized[i]["text"] = "[REDACTED:multi_word_sensitive]"
    return sanitized


def build_security_scan(*, findings: list[dict[str, str]], source: str) -> dict[str, Any]:
    return {
        "ok": True,
        "source": source,
        "quarantined": bool(findings),
        "findings": list(findings),
        "policy": list(VISUAL_SECURITY_SCAN_POLICY),
        "redaction_applied": bool(findings),
    }


__all__ = [
    "LEGACY_SCRIPT_SHIM",
    "OCR_SENSITIVE_PATTERNS",
    "PACKAGE_PATH",
    "SENSITIVE_SURFACE_PATTERNS",
    "VISUAL_SECURITY_SCAN_POLICY",
    "build_security_scan",
    "detect_sensitive_ocr_text",
    "detect_sensitive_surface",
    "redact_sensitive_ocr_text",
    "sanitize_ocr_words",
]
