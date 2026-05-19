#!/usr/bin/env python3
from __future__ import annotations

import os
import re
from typing import Any

DEFAULT_REPO_ID = str(os.environ.get("AGN_REPO_ID", "main") or "main").strip() or "main"

_REF_PREFIX = "agn://"
_ARTIFACT_RE = re.compile(r"^agn://artifact/(?P<sha>[a-f0-9]{64})$")
_OBJECT_RE = re.compile(
    r"^agn://object/(?P<kind>[a-z0-9._-]+)/(?P<trace>[A-Za-z0-9._:-]+)/(?P<attempt>\d+)$"
)
_REPO_RE = re.compile(r"^agn://repo/(?P<repo_id>[A-Za-z0-9._-]+)$")

_ABS_PATH_HINTS = (
    "/Users/",
    str("/Volumes") + "/",
    "C:\\",
    "D:\\",
    "\\\\",
)
_REL_PATH_HINTS = (
    "../",
    "./",
    "~/",
)
_RESERVED_PATH_TOKENS = (
    "dispatch/",
    "results/",
    "verdicts/",
)


def _clean_token(raw: str, *, fallback: str) -> str:
    value = str(raw or "").strip()
    value = re.sub(r"[^A-Za-z0-9._:-]", "_", value)
    value = value.strip("._-")
    return value or fallback


def build_repo_ref(repo_id: str = "") -> str:
    rid = _clean_token(repo_id or DEFAULT_REPO_ID, fallback=DEFAULT_REPO_ID)
    return f"agn://repo/{rid}"


def parse_repo_ref(ref: str) -> str:
    match = _REPO_RE.match(str(ref or "").strip())
    if not match:
        raise ValueError("invalid_repo_ref")
    return str(match.group("repo_id"))


def build_artifact_ref(sha256: str) -> str:
    digest = str(sha256 or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("invalid_sha256")
    return f"agn://artifact/{digest}"


def parse_artifact_ref(ref: str) -> str:
    match = _ARTIFACT_RE.match(str(ref or "").strip())
    if not match:
        raise ValueError("invalid_artifact_ref")
    return str(match.group("sha"))


def build_object_ref(kind: str, trace_id: str, attempt: int) -> str:
    obj_kind = _clean_token(kind, fallback="object").lower()
    trace = _clean_token(trace_id, fallback="trace")
    n = max(1, int(attempt or 1))
    return f"agn://object/{obj_kind}/{trace}/{n}"


def parse_object_ref(ref: str) -> dict[str, str | int]:
    match = _OBJECT_RE.match(str(ref or "").strip())
    if not match:
        raise ValueError("invalid_object_ref")
    return {
        "kind": str(match.group("kind")),
        "trace_id": str(match.group("trace")),
        "attempt": int(match.group("attempt") or 1),
    }


def is_agn_ref(value: str) -> bool:
    return str(value or "").strip().startswith(_REF_PREFIX)


def is_supported_agn_ref(value: str) -> bool:
    ref = str(value or "").strip()
    return bool(_ARTIFACT_RE.match(ref) or _OBJECT_RE.match(ref) or _REPO_RE.match(ref))


def has_path_semantics(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if is_agn_ref(text):
        return False
    lowered = text.lower()
    if any(token.lower() in lowered for token in _ABS_PATH_HINTS):
        return True
    if any(text.startswith(token) for token in _REL_PATH_HINTS):
        return True
    if text.startswith("/"):
        return True
    if re.match(r"^[A-Za-z]:\\", text):
        return True
    if any(token in lowered for token in _RESERVED_PATH_TOKENS):
        return True
    return False


def find_path_like_values(value: Any, *, prefix: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, str):
        if has_path_semantics(value):
            hits.append(prefix or "<root>")
        return hits
    if isinstance(value, list):
        for idx, item in enumerate(value):
            path = f"{prefix}[{idx}]" if prefix else f"[{idx}]"
            hits.extend(find_path_like_values(item, prefix=path))
        return hits
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            path = f"{prefix}.{key}" if prefix else key
            hits.extend(find_path_like_values(item, prefix=path))
        return hits
    return hits
