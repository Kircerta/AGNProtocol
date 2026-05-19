#!/usr/bin/env python3
from __future__ import annotations

"""Paused compatibility shim for the retired multi-host posture surface.

AGN currently operates from a single-host model inside each checkout. The Human
Admin decides which machine is active before opening or using this repo. The
active host-context surface is `scripts/agn_host_info.py` plus `HOST_INFO.md`.

This file remains only so older entry points fail closed with a clear answer
instead of continuing to imply a live multi-host comparison layer.
"""

import argparse
from datetime import datetime, timezone
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

try:
    from agn_host_info import build_host_info
except ImportError:  # pragma: no cover
    from scripts.agn_host_info import build_host_info


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _paused_payload(*, task_summary: str = "") -> dict[str, Any]:
    host_info = build_host_info(task_summary=task_summary, refresh=False)
    identity = host_info.get("host_identity", {}) if isinstance(host_info.get("host_identity"), dict) else {}
    return {
        "schema_version": "agn.host_posture_brief.paused.v1",
        "generated_at": utc_now_iso(),
        "ok": True,
        "status": "paused",
        "reason": "multi_host_posture_paused",
        "detail": "Multi-host posture comparison is paused. Use scripts/agn_host_info.py and HOST_INFO.md for current-host facts.",
        "current_host_id": str(identity.get("host_id", "")).strip(),
        "current_host_display_name": str(identity.get("display_name", "")).strip(),
        "task_summary": str(task_summary or "").strip(),
        "recommended_surface": {
            "command": "python3 scripts/agn_host_info.py show",
            "markdown": "HOST_INFO.md",
        },
    }


def build_host_posture_brief(
    *,
    task_summary: str = "",
    needs_control_plane: bool = False,
    needs_desktop: bool = False,
    needs_history: bool = False,
    needs_worker: bool = False,
    host_state_path: Path | None = None,
    federated_hosts_path: Path | None = None,
) -> dict[str, Any]:
    _ = (needs_control_plane, needs_desktop, needs_history, needs_worker, host_state_path, federated_hosts_path)
    return _paused_payload(task_summary=task_summary)


def as_runtime_router_compatibility_view(
    *,
    task_summary: str = "",
    needs_control_plane: bool = False,
    needs_desktop: bool = False,
    needs_history: bool = False,
    needs_worker: bool = False,
    host_state_path: Path | None = None,
    federated_hosts_path: Path | None = None,
) -> dict[str, Any]:
    _ = (needs_control_plane, needs_desktop, needs_history, needs_worker, host_state_path, federated_hosts_path)
    payload = _paused_payload(task_summary=task_summary)
    payload.update(
        {
            "schema_version": "agn.runtime_router.paused_compat.v1",
            "recommended_host_id": "",
            "selected_host_id": "",
            "selection_changed": False,
        }
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Paused compatibility entry. Use scripts/agn_host_info.py instead of multi-host posture.")
    parser.add_argument("--task-summary", default="")
    args = parser.parse_args()
    print(json.dumps(_paused_payload(task_summary=str(args.task_summary or "").strip()), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
