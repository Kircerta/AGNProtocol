#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports" / "artifact_bridge"

try:
    from agn_governed_execution import dispatch_desktop_action
except ImportError:  # pragma: no cover
    from scripts.agn_governed_execution import dispatch_desktop_action

try:
    from pointer_protocol import parse_ref, read_ref_text, resolve_ref_path, write_file_artifact
except ImportError:  # pragma: no cover
    from scripts.pointer_protocol import parse_ref, read_ref_text, resolve_ref_path, write_file_artifact


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _safe_slug(text: str, *, default: str, max_len: int = 48) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(text or "").strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    cleaned = cleaned.strip("-") or default
    return cleaned[:max_len].rstrip("-") or default


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def infer_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    mapping = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".json": "application/json",
        ".jsonl": "application/jsonl",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".pdf": "application/pdf",
        ".csv": "text/csv",
        ".html": "text/html",
        ".log": "text/plain",
    }
    return mapping.get(suffix, "application/octet-stream")


def register_local_path(*, task_id: str, attempt: int, path: str, artifact_id: str, source: str) -> dict[str, Any]:
    local = Path(path).expanduser().resolve()
    media_type = infer_media_type(local)
    ref = write_file_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id=artifact_id,
        source_path=local,
        filename=local.name,
        media_type=media_type,
        source=source,
    )
    return {
        "artifact_id": ref.artifact_id,
        "ref": ref.ref,
        "sha256": ref.sha256,
        "bytes": ref.bytes,
        "media_type": ref.media_type,
        "path": str(local),
    }


def capture_and_register(
    *,
    task_id: str,
    attempt: int,
    capture_path: str,
    artifact_id: str,
    app: str,
    window_name: str,
    active_window: bool,
    region: str,
) -> dict[str, Any]:
    action = {
        "action_type": "DESKTOP_OBSERVE",
        "trace_id": f"trace-{task_id}",
        "params": {
            "surface": "screenshot",
            "path": capture_path,
            "app": app,
            "window_name": window_name,
            "active_window": active_window,
            "region": region,
        },
        "timeout_sec": 30.0,
    }
    capture = dispatch_desktop_action(
        action,
        caller="agn_artifact_bridge",
        task_id=task_id,
        trace_id=f"trace-{task_id}",
        intent="desktop_observe_screenshot",
        reason="artifact bridge screenshot capture",
        risk_level="low",
    )
    if not capture.get("ok"):
        raise RuntimeError(str(capture.get("error", "capture_failed")))
    registered = register_local_path(
        task_id=task_id,
        attempt=attempt,
        path=capture_path,
        artifact_id=artifact_id,
        source="agn_artifact_bridge_capture",
    )
    return {"capture": capture["result"], "dispatch_meta": capture["dispatch_meta"], "registered": registered}


def inspect_ref(ref: str, *, mode: str, start_line: int, end_line: int, max_bytes: int) -> dict[str, Any]:
    parsed = parse_ref(ref)
    resolved = resolve_ref_path(ref)
    media_type = infer_media_type(resolved)
    preview = ""
    if media_type.startswith("text/") or media_type in {"application/json", "application/jsonl", "text/markdown"}:
        preview = read_ref_text(ref, mode=mode, start_line=start_line, end_line=end_line, max_bytes=max_bytes)
    return {
        "ref": ref,
        "parsed": parsed,
        "resolved_path": str(resolved),
        "media_type": media_type,
        "bytes": int(resolved.stat().st_size),
        "preview": preview,
    }


def _write_report(payload: dict[str, Any], *, label: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"{timestamp}-{_safe_slug(label, default='artifact')}.json"
    latest = REPORT_DIR / "latest.json"
    _atomic_write_json(path, payload)
    _atomic_write_json(latest, payload)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge local files, screenshots, reports, and artifact refs into one inspectable artifact workflow.")
    sub = parser.add_subparsers(dest="command", required=True)

    register_parser = sub.add_parser("register", help="Register a local file path as an AGN artifact")
    register_parser.add_argument("--task-id", required=True)
    register_parser.add_argument("--attempt", type=int, default=1)
    register_parser.add_argument("--path", required=True)
    register_parser.add_argument("--artifact-id", default="artifact_bridge_input")
    register_parser.add_argument("--source", default="agn_artifact_bridge")
    register_parser.add_argument("--no-write", action="store_true")

    capture_parser = sub.add_parser("capture-register", help="Capture a screenshot through desktop_adapter and register it as an AGN artifact")
    capture_parser.add_argument("--task-id", required=True)
    capture_parser.add_argument("--attempt", type=int, default=1)
    capture_parser.add_argument("--capture-path", required=True)
    capture_parser.add_argument("--artifact-id", default="desktop_capture")
    capture_parser.add_argument("--app", default="")
    capture_parser.add_argument("--window-name", default="")
    capture_parser.add_argument("--active-window", action="store_true")
    capture_parser.add_argument("--region", default="")
    capture_parser.add_argument("--no-write", action="store_true")

    inspect_parser = sub.add_parser("inspect", help="Resolve an artifact ref and preview or inspect its content")
    inspect_parser.add_argument("--ref", required=True)
    inspect_parser.add_argument("--mode", choices=["all", "range", "tail"], default="range")
    inspect_parser.add_argument("--start-line", type=int, default=1)
    inspect_parser.add_argument("--end-line", type=int, default=80)
    inspect_parser.add_argument("--max-bytes", type=int, default=4096)
    inspect_parser.add_argument("--no-write", action="store_true")

    args = parser.parse_args()

    if args.command == "register":
        payload = {
            "ok": True,
            "generated_at": utc_now_iso(),
            "mode": "register",
            "result": register_local_path(
                task_id=str(args.task_id).strip(),
                attempt=max(1, int(args.attempt)),
                path=str(args.path).strip(),
                artifact_id=str(args.artifact_id).strip(),
                source=str(args.source).strip(),
            ),
        }
        if not args.no_write:
            payload["report_path"] = str(_write_report(payload, label=str(args.artifact_id)))
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    if args.command == "capture-register":
        payload = {
            "ok": True,
            "generated_at": utc_now_iso(),
            "mode": "capture-register",
            "result": capture_and_register(
                task_id=str(args.task_id).strip(),
                attempt=max(1, int(args.attempt)),
                capture_path=str(args.capture_path).strip(),
                artifact_id=str(args.artifact_id).strip(),
                app=str(args.app).strip(),
                window_name=str(args.window_name).strip(),
                active_window=bool(args.active_window),
                region=str(args.region).strip(),
            ),
        }
        if not args.no_write:
            payload["report_path"] = str(_write_report(payload, label=str(args.artifact_id)))
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    payload = {
        "ok": True,
        "generated_at": utc_now_iso(),
        "mode": "inspect",
        "result": inspect_ref(
            str(args.ref).strip(),
            mode=str(args.mode).strip(),
            start_line=max(1, int(args.start_line)),
            end_line=max(1, int(args.end_line)),
            max_bytes=max(256, int(args.max_bytes)),
        ),
    }
    if not args.no_write:
        payload["report_path"] = str(_write_report(payload, label="inspect"))
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
