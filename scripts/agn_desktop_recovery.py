#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
REPORT_DIR = ROOT / "reports" / "desktop_recovery"

try:
    from agn.core.desktop_provider import get_desktop_control_bin
    GUI_AGENT_BIN = get_desktop_control_bin()
except ImportError:  # pragma: no cover
    GUI_AGENT_BIN = Path.home() / ".codex" / "bin" / "gui-agent"
DESKTOP_RECOVERY_BOUNDARY = {
    "default_mode": "recover_truth_before_retry",
    "permission_expectations": [
        "gui_agent_must_exist_and_have_os_level_permissions",
        "safety_blocks_are_authoritative_not_optional",
    ],
    "abort_conditions": [
        "missing_gui_agent_or_missing_permissions",
        "emergency_stop_or_observe_only_mode",
        "fresh_evidence_still_unclear_after_recovery_attempt",
    ],
}

try:
    from agn_visual_operator import build_visual_payload
except ImportError:  # pragma: no cover
    from scripts.agn_visual_operator import build_visual_payload


RECOVERABLE_FAILURES = {
    "unsafe_action_blocked": "Action was blocked by safety rules; recover by switching to observe-only evidence or obtaining required approval and audit refs.",
    "emergency_stop_active": "Desktop is in observe-only mode; do not retry write actions until stop is released.",
    "schema_invalid": "Payload shape was invalid; inspect the action schema before retrying.",
}


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


def _run_json(cmd: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "failure_class": "executable_not_found",
            "error": f"missing_executable:{cmd[0] if cmd else 'unknown'}",
            "returncode": 127,
            "stderr": "",
            "command": cmd,
        }
    stdout = str(completed.stdout or "").strip()
    try:
        parsed = json.loads(stdout) if stdout else {}
    except Exception:
        parsed = {"raw_stdout": stdout}
    if isinstance(parsed, dict):
        parsed.setdefault("returncode", int(completed.returncode))
        parsed.setdefault("stderr", str(completed.stderr or "").strip())
        parsed.setdefault("command", cmd)
    return parsed if isinstance(parsed, dict) else {"returncode": int(completed.returncode), "stdout": parsed}


def _frontmost() -> dict[str, Any]:
    return _run_json([str(GUI_AGENT_BIN), "frontmost"])


def _status() -> dict[str, Any]:
    return _run_json([str(GUI_AGENT_BIN), "status"])


def _activate(app: str) -> dict[str, Any]:
    return _run_json([str(GUI_AGENT_BIN), "activate", "--app", app])


def build_payload(
    *,
    task_id: str,
    expected_app: str,
    last_failure_class: str,
    last_error: str,
    capture_path: str,
    window_name: str,
    active_window: bool,
    target_texts: list[str],
    apply_activate: bool,
) -> dict[str, Any]:
    frontmost = _frontmost()
    status = _status()
    app_now = str(frontmost.get("app", "")).strip()
    recovery_plan: list[str] = []
    observations: list[str] = []

    if last_failure_class:
        message = RECOVERABLE_FAILURES.get(last_failure_class, f"Unhandled desktop failure class: {last_failure_class}")
        recovery_plan.append(message)
    if last_error:
        observations.append(f"last_error={last_error}")

    activation_result = None
    app_mismatch = bool(expected_app and app_now and expected_app != app_now)
    if app_mismatch:
        recovery_plan.append(f"Frontmost app is `{app_now}`, not `{expected_app}`; restore focus before retrying GUI automation.")
        if apply_activate:
            activation_result = _activate(expected_app)
            recovery_plan.append(f"Activated `{expected_app}` before refreshing screenshot evidence.")
    elif expected_app:
        observations.append(f"frontmost app already matches expected app `{expected_app}`")

    visual = None
    if capture_path:
        visual = build_visual_payload(
            task_id=task_id,
            attempt=1,
            trace_id=f"trace-{task_id}",
            image_path="",
            image_ref="",
            capture_path=capture_path,
            app=expected_app or app_now,
            window_name=window_name,
            active_window=active_window,
            region="",
            target_texts=target_texts,
            type_text="",
            press_key="",
            apply_activate=False,
            apply_click=False,
            apply_type=False,
            apply_key=False,
        )
        if not visual.get("matches"):
            recovery_plan.append("Fresh screenshot still did not locate the target text; inspect screenshot or refine target text.")
        else:
            recovery_plan.append("Fresh screenshot located at least one OCR/UI target candidate; retry from evidence, not memory.")
    elif target_texts:
        recovery_plan.append("Provide --capture-path when target text recovery depends on a fresh screenshot.")

    if not recovery_plan:
        recovery_plan.append("No specific recovery steps were required; the desktop surface looks ready for another attempt.")

    return {
        "ok": True,
        "generated_at": utc_now_iso(),
        "task_id": task_id,
        "frontmost": frontmost,
        "status": status,
        "expected_app": expected_app,
        "app_mismatch": app_mismatch,
        "activation_result": activation_result,
        "last_failure_class": last_failure_class,
        "last_error": last_error,
        "observations": observations,
        "recovery_plan": recovery_plan,
        "visual_followup": visual,
        "security_boundary": DESKTOP_RECOVERY_BOUNDARY,
        "notes": [
            "Desktop recovery starts by re-establishing truth: frontmost app, focus, and fresh screenshot evidence.",
            "Use activation and screenshot refresh before retrying clicks or typing.",
            "Safety blocks are not bugs; they are governance signals that require a different path.",
        ],
    }


def _write_report(payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"{timestamp}-{_safe_slug(str(payload.get('task_id', 'desktop-recovery')), default='desktop-recovery')}.json"
    latest = REPORT_DIR / "latest.json"
    _atomic_write_json(path, payload)
    _atomic_write_json(latest, payload)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover from GUI automation drift by re-establishing focus, screenshot evidence, and safe next steps.")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--expected-app", default="")
    parser.add_argument("--last-failure-class", default="")
    parser.add_argument("--last-error", default="")
    parser.add_argument("--capture-path", default="")
    parser.add_argument("--window-name", default="")
    parser.add_argument("--active-window", action="store_true")
    parser.add_argument("--target-text", action="append", default=[])
    parser.add_argument("--apply-activate", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()

    task_id = str(args.task_id).strip() or f"desktop-recovery-{_safe_slug(str(args.expected_app or 'task'), default='task')}"
    payload = build_payload(
        task_id=task_id,
        expected_app=str(args.expected_app).strip(),
        last_failure_class=str(args.last_failure_class).strip(),
        last_error=str(args.last_error).strip(),
        capture_path=str(args.capture_path).strip(),
        window_name=str(args.window_name).strip(),
        active_window=bool(args.active_window),
        target_texts=[str(item).strip() for item in list(args.target_text or []) if str(item).strip()],
        apply_activate=bool(args.apply_activate),
    )
    if not args.no_write:
        payload["report_path"] = str(_write_report(payload))
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
