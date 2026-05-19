#!/usr/bin/env python3
from __future__ import annotations

"""Paused compatibility shim for the retired federated host read model.

AGN currently treats machine choice as an operator decision. Each
checkout models only the active local machine through `HOST_INFO.md` and
`scripts/agn_host_info.py`.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

try:
    from admin_control_common import atomic_write_json, read_models_dir
except ImportError:  # pragma: no cover
    from scripts.admin_control_common import atomic_write_json, read_models_dir


READ_MODEL_NAME = "federated_hosts.json"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso8601(raw: str) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def build_federated_read_model(*, input_dir: Path | None = None, expected_hosts: list[str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    _ = (input_dir, expected_hosts, now)
    return {
        "schema_version": "agn.federated_hosts.paused.v1",
        "generated_at": utc_now_iso(),
        "ok": True,
        "status": "paused",
        "reason": "multi_host_read_model_paused",
        "detail": "Federated host aggregation is paused. Use scripts/agn_host_info.py and HOST_INFO.md for the active local host only.",
    }


def write_federated_read_model(payload: dict[str, Any], *, output_path: Path | None = None) -> Path:
    target = output_path or (read_models_dir() / READ_MODEL_NAME)
    atomic_write_json(target, payload)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Paused compatibility entry. Federated host read models are not active in the current AGN posture.")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    payload = build_federated_read_model()
    if args.output:
        write_federated_read_model(payload, output_path=Path(args.output).expanduser().resolve())
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
