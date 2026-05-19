#!/usr/bin/env python3
from __future__ import annotations

"""Paused compatibility shim for the retired multi-host heartbeat surface.

Local host freshness is now derived directly from the current host-state file
and presented through `scripts/agn_host_info.py` and `HOST_INFO.md`.
"""

import argparse
from datetime import datetime, timezone
import json
import shutil
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

try:
    from admin_control_common import read_models_dir, repo_root
except ImportError:  # pragma: no cover
    from scripts.admin_control_common import read_models_dir, repo_root

try:
    from agn_host_state_probe import HOST_STATE_LOCAL_FILENAME
except ImportError:  # pragma: no cover
    from scripts.agn_host_state_probe import HOST_STATE_LOCAL_FILENAME


READ_MODEL_NAME = "host_state_heartbeat.json"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def heartbeat_root() -> Path:
    return repo_root() / "runtime" / "admin_control" / "host_state_heartbeat"


def local_host_state_path() -> Path:
    return read_models_dir() / HOST_STATE_LOCAL_FILENAME


def read_model_path() -> Path:
    return read_models_dir() / READ_MODEL_NAME


def _cleanup_legacy_outputs() -> None:
    try:
        read_model_path().unlink(missing_ok=True)
    except Exception:
        pass
    try:
        shutil.rmtree(heartbeat_root(), ignore_errors=True)
    except Exception:
        pass


def build_host_state_heartbeat_model(*, federated_hosts: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    _ = (federated_hosts, now)
    return {
        "schema_version": "agn.host_state_heartbeat.paused.v1",
        "generated_at": utc_now_iso(),
        "ok": True,
        "status": "paused",
        "reason": "multi_host_heartbeat_paused",
        "detail": "The dedicated heartbeat loop is paused. Refresh or inspect the current host through scripts/agn_host_info.py instead.",
        "source_path": str(local_host_state_path()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Paused compatibility entry. Use scripts/agn_host_info.py instead of the retired host-state heartbeat.")
    parser.add_argument("command", nargs="?", default="status")
    args = parser.parse_args()
    _cleanup_legacy_outputs()
    payload = build_host_state_heartbeat_model()
    _ = args
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
