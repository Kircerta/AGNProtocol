#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

try:
    from agn_refs import build_artifact_ref, parse_artifact_ref
except ImportError:  # pragma: no cover - package import fallback
    from scripts.agn_refs import build_artifact_ref, parse_artifact_ref

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = ROOT / ".agn_workspace"
TASKS_DIR = WORKSPACE_DIR / "tasks"
ARTIFACT_INDEX_PATH = WORKSPACE_DIR / "artifact_index.json"
_ARTIFACT_INDEX_CACHE: tuple[int, int, dict[str, dict[str, Any]]] | None = None

_LEGACY_REF_RE = re.compile(
    r"^agn://task/(?P<task>[^/]+)/attempt/(?P<attempt>\d+)/artifact/(?P<artifact>[^@]+)@sha256:(?P<sha>[a-f0-9]{64})$"
)


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    ref: str
    sha256: str
    bytes: int
    media_type: str
    rel_path: str


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def safe_task_id(task_id: str) -> str:
    raw = str(task_id or "").strip().replace("/", "_")
    raw = raw.lstrip(".")
    if not raw:
        raw = "unnamed"
    if len(raw) > 200:
        raw = raw[:200]
    return raw


def safe_artifact_id(artifact_id: str) -> str:
    normalized = str(artifact_id or "").strip().lower().replace(" ", "_")
    cleaned = re.sub(r"[^a-z0-9._-]", "_", normalized)
    return cleaned or "artifact"


def ensure_workspace_dirs() -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)


def task_attempt_dir(task_id: str, attempt: int) -> Path:
    safe_task = safe_task_id(task_id)
    safe_attempt = max(1, int(attempt))
    return TASKS_DIR / safe_task / f"attempt_{safe_attempt}"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            if not content.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))


def _atomic_copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp_path = Path(tmp_name)
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as handle:
            shutil.copyfileobj(src, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_path(task_id: str, attempt: int) -> Path:
    return task_attempt_dir(task_id, attempt) / "manifest.json"


def _load_manifest(task_id: str, attempt: int) -> dict[str, Any]:
    path = _manifest_path(task_id, attempt)
    if not path.exists():
        return {
            "task_id": safe_task_id(task_id),
            "attempt": max(1, int(attempt)),
            "created_at": utc_now_iso(),
            "ready": True,
            "artifacts": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if not isinstance(payload.get("artifacts"), dict):
        payload["artifacts"] = {}
    payload.setdefault("task_id", safe_task_id(task_id))
    payload.setdefault("attempt", max(1, int(attempt)))
    payload.setdefault("created_at", utc_now_iso())
    payload.setdefault("ready", True)
    return payload


def _load_artifact_index() -> dict[str, dict[str, Any]]:
    global _ARTIFACT_INDEX_CACHE
    if not ARTIFACT_INDEX_PATH.exists():
        _ARTIFACT_INDEX_CACHE = None
        return {}
    stat = ARTIFACT_INDEX_PATH.stat()
    cache_key = (int(stat.st_mtime_ns), int(stat.st_size))
    if _ARTIFACT_INDEX_CACHE and _ARTIFACT_INDEX_CACHE[0] == cache_key[0] and _ARTIFACT_INDEX_CACHE[1] == cache_key[1]:
        return _ARTIFACT_INDEX_CACHE[2]
    try:
        payload = json.loads(ARTIFACT_INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        _ARTIFACT_INDEX_CACHE = (cache_key[0], cache_key[1], {})
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            continue
        if not isinstance(value, dict):
            continue
        normalized[key] = value
    _ARTIFACT_INDEX_CACHE = (cache_key[0], cache_key[1], normalized)
    return normalized


def _save_artifact_index(index: dict[str, dict[str, Any]]) -> None:
    global _ARTIFACT_INDEX_CACHE
    ensure_workspace_dirs()
    _atomic_write_json(ARTIFACT_INDEX_PATH, index)
    stat = ARTIFACT_INDEX_PATH.stat()
    _ARTIFACT_INDEX_CACHE = (int(stat.st_mtime_ns), int(stat.st_size), index)


def _index_artifact(*, sha256: str, task_id: str, attempt: int, artifact_id: str, rel_path: str, media_type: str, size_bytes: int) -> None:
    index = _load_artifact_index()
    index[str(sha256)] = {
        "task_id": safe_task_id(task_id),
        "attempt": max(1, int(attempt)),
        "artifact_id": safe_artifact_id(artifact_id),
        "path": str(rel_path),
        "media_type": str(media_type or "text/plain"),
        "bytes": int(size_bytes),
        "updated_at": utc_now_iso(),
    }
    _save_artifact_index(index)


def _legacy_ref(task_id: str, attempt: int, artifact_id: str, sha256: str) -> str:
    return (
        f"agn://task/{safe_task_id(task_id)}/attempt/{max(1, int(attempt))}/"
        f"artifact/{safe_artifact_id(artifact_id)}@sha256:{sha256}"
    )


def write_text_artifact(
    *,
    task_id: str,
    attempt: int,
    artifact_id: str,
    content: str,
    media_type: str,
    filename: str,
    source: str,
) -> ArtifactRef:
    ensure_workspace_dirs()
    safe_task = safe_task_id(task_id)
    safe_attempt = max(1, int(attempt))
    safe_id = safe_artifact_id(artifact_id)

    artifact_dir = task_attempt_dir(safe_task, safe_attempt)
    artifact_path = artifact_dir / filename
    rendered = str(content or "")
    _atomic_write_text(artifact_path, rendered)

    data = artifact_path.read_bytes()
    digest = _sha256_bytes(data)
    ref = build_artifact_ref(digest)
    rel_path = str(artifact_path.relative_to(ROOT))

    manifest = _load_manifest(safe_task, safe_attempt)
    artifacts = manifest.setdefault("artifacts", {})
    artifacts[safe_id] = {
        "artifact_id": safe_id,
        "filename": filename,
        "path": rel_path,
        "ref": ref,
        "legacy_ref": _legacy_ref(safe_task, safe_attempt, safe_id, digest),
        "sha256": digest,
        "bytes": len(data),
        "media_type": media_type,
        "source": source,
        "updated_at": utc_now_iso(),
    }
    manifest["updated_at"] = utc_now_iso()
    manifest["ready"] = True
    _atomic_write_json(_manifest_path(safe_task, safe_attempt), manifest)
    _index_artifact(
        sha256=digest,
        task_id=safe_task,
        attempt=safe_attempt,
        artifact_id=safe_id,
        rel_path=rel_path,
        media_type=media_type,
        size_bytes=len(data),
    )

    return ArtifactRef(
        artifact_id=safe_id,
        ref=ref,
        sha256=digest,
        bytes=len(data),
        media_type=media_type,
        rel_path=rel_path,
    )


def write_json_artifact(
    *,
    task_id: str,
    attempt: int,
    artifact_id: str,
    payload: dict[str, Any],
    filename: str,
    source: str,
) -> ArtifactRef:
    return write_text_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id=artifact_id,
        content=json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
        media_type="application/json",
        filename=filename,
        source=source,
    )


def write_file_artifact(
    *,
    task_id: str,
    attempt: int,
    artifact_id: str,
    source_path: str | Path,
    filename: str,
    media_type: str,
    source: str,
) -> ArtifactRef:
    ensure_workspace_dirs()
    safe_task = safe_task_id(task_id)
    safe_attempt = max(1, int(attempt))
    safe_id = safe_artifact_id(artifact_id)
    source_file = Path(source_path).expanduser().resolve()
    if not source_file.exists() or not source_file.is_file():
        raise ValueError(f"artifact_source_missing:{source_file}")

    chosen_name = filename.strip() if str(filename or "").strip() else source_file.name
    artifact_dir = task_attempt_dir(safe_task, safe_attempt)
    artifact_path = artifact_dir / chosen_name
    _atomic_copy_file(source_file, artifact_path)

    data = artifact_path.read_bytes()
    digest = _sha256_bytes(data)
    ref = build_artifact_ref(digest)
    rel_path = str(artifact_path.relative_to(ROOT))

    manifest = _load_manifest(safe_task, safe_attempt)
    artifacts = manifest.setdefault("artifacts", {})
    artifacts[safe_id] = {
        "artifact_id": safe_id,
        "filename": chosen_name,
        "path": rel_path,
        "ref": ref,
        "legacy_ref": _legacy_ref(safe_task, safe_attempt, safe_id, digest),
        "sha256": digest,
        "bytes": len(data),
        "media_type": str(media_type or "application/octet-stream"),
        "source": source,
        "updated_at": utc_now_iso(),
    }
    manifest["updated_at"] = utc_now_iso()
    manifest["ready"] = True
    _atomic_write_json(_manifest_path(safe_task, safe_attempt), manifest)
    _index_artifact(
        sha256=digest,
        task_id=safe_task,
        attempt=safe_attempt,
        artifact_id=safe_id,
        rel_path=rel_path,
        media_type=str(media_type or "application/octet-stream"),
        size_bytes=len(data),
    )

    return ArtifactRef(
        artifact_id=safe_id,
        ref=ref,
        sha256=digest,
        bytes=len(data),
        media_type=str(media_type or "application/octet-stream"),
        rel_path=rel_path,
    )


def ref_to_artifact_entry(ref: ArtifactRef) -> dict[str, Any]:
    return {
        "artifact_id": ref.artifact_id,
        "ref": ref.ref,
        "sha256": ref.sha256,
        "bytes": ref.bytes,
        "media_type": ref.media_type,
    }


def _resolve_workspace_rel_path(rel_path: str) -> Path:
    if not rel_path:
        raise ValueError("artifact_missing_path")
    resolved = (ROOT / str(rel_path)).resolve()
    workspace_root = WORKSPACE_DIR.resolve()
    if workspace_root not in resolved.parents and resolved != workspace_root:
        raise ValueError("artifact_path_outside_workspace")
    if not resolved.exists() or not resolved.is_file():
        raise ValueError("artifact_file_missing")
    return resolved


def _lookup_artifact_by_sha(sha256: str) -> dict[str, Any] | None:
    index = _load_artifact_index()
    entry = index.get(sha256)
    if isinstance(entry, dict):
        return entry

    # Fallback scan keeps backward compatibility if index is stale.
    for manifest in TASKS_DIR.glob("*/attempt_*/manifest.json"):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            continue
        artifacts = payload.get("artifacts", {}) if isinstance(payload, dict) else {}
        if not isinstance(artifacts, dict):
            continue
        for artifact in artifacts.values():
            if not isinstance(artifact, dict):
                continue
            if str(artifact.get("sha256", "")).strip() != sha256:
                continue
            rel_path = str(artifact.get("path", "")).strip()
            if not rel_path:
                continue
            discovered = {
                "task_id": str(payload.get("task_id", "")).strip(),
                "attempt": int(payload.get("attempt", 1) or 1),
                "artifact_id": str(artifact.get("artifact_id", "")).strip(),
                "path": rel_path,
                "media_type": str(artifact.get("media_type", "text/plain")),
                "bytes": int(artifact.get("bytes", 0) or 0),
            }
            _index_artifact(
                sha256=sha256,
                task_id=discovered["task_id"],
                attempt=int(discovered["attempt"]),
                artifact_id=discovered["artifact_id"],
                rel_path=rel_path,
                media_type=str(discovered["media_type"]),
                size_bytes=int(discovered["bytes"]),
            )
            return discovered
    return None


def parse_ref(ref: str) -> dict[str, str]:
    raw = str(ref or "").strip()

    legacy = _LEGACY_REF_RE.match(raw)
    if legacy:
        return {
            "ref_type": "legacy",
            "task_id": legacy.group("task"),
            "attempt": legacy.group("attempt"),
            "artifact_id": legacy.group("artifact"),
            "sha256": legacy.group("sha"),
        }

    try:
        sha = parse_artifact_ref(raw)
    except ValueError as exc:
        raise ValueError("invalid_pointer_ref") from exc

    parsed: dict[str, str] = {
        "ref_type": "artifact",
        "sha256": sha,
    }
    entry = _lookup_artifact_by_sha(sha)
    if isinstance(entry, dict):
        parsed["task_id"] = str(entry.get("task_id", ""))
        parsed["attempt"] = str(entry.get("attempt", "1"))
        parsed["artifact_id"] = str(entry.get("artifact_id", ""))
    return parsed


def resolve_ref_path(ref: str) -> Path:
    parsed = parse_ref(ref)
    ref_type = str(parsed.get("ref_type", "artifact"))

    if ref_type == "legacy":
        safe_task = safe_task_id(parsed["task_id"])
        attempt = max(1, int(parsed["attempt"]))
        safe_artifact = safe_artifact_id(parsed["artifact_id"])

        manifest = _load_manifest(safe_task, attempt)
        artifacts = manifest.get("artifacts", {}) if isinstance(manifest, dict) else {}
        artifact = artifacts.get(safe_artifact) if isinstance(artifacts, dict) else None
        if not isinstance(artifact, dict):
            raise ValueError("artifact_not_found")

        resolved = _resolve_workspace_rel_path(str(artifact.get("path", "")).strip())
        digest = _sha256_bytes(resolved.read_bytes())
        expected = str(parsed.get("sha256", ""))
        if digest != expected:
            raise ValueError("artifact_hash_mismatch")
        return resolved

    sha256 = str(parsed.get("sha256", "")).strip()
    entry = _lookup_artifact_by_sha(sha256)
    if not isinstance(entry, dict):
        raise ValueError("artifact_not_found")
    resolved = _resolve_workspace_rel_path(str(entry.get("path", "")).strip())
    digest = _sha256_bytes(resolved.read_bytes())
    if digest != sha256:
        raise ValueError("artifact_hash_mismatch")
    return resolved


def read_ref_text(ref: str, *, mode: str = "all", start_line: int = 1, end_line: int = 200, tail_lines: int = 200, max_bytes: int = 32768) -> str:
    path = resolve_ref_path(ref)
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    selected: list[str]

    norm_mode = str(mode or "all").strip().lower()
    if norm_mode == "tail":
        selected = lines[-max(1, tail_lines) :]
    elif norm_mode == "range":
        start = max(1, int(start_line)) - 1
        end = max(start + 1, int(end_line))
        selected = lines[start:end]
    else:
        selected = lines

    rendered = "\n".join(selected)
    encoded = rendered.encode("utf-8")
    if len(encoded) <= max_bytes:
        return rendered
    clipped = encoded[: max(1, max_bytes)].decode("utf-8", errors="ignore")
    return clipped + "\n...<truncated-by-max-bytes>..."


def search_ref_text(ref: str, *, pattern: str, max_matches: int = 50) -> list[dict[str, Any]]:
    path = resolve_ref_path(ref)
    regex = re.compile(pattern)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    matches: list[dict[str, Any]] = []
    for idx, line in enumerate(lines, start=1):
        if regex.search(line) is None:
            continue
        matches.append({"line": idx, "text": line})
        if len(matches) >= max(1, int(max_matches)):
            break
    return matches
