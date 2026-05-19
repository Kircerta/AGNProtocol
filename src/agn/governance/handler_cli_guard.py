"""AGN direct-handler CLI guard utilities.

This module holds the real implementation for the direct handler CLI guard.
Legacy script entrypoints re-export from here while package imports use this
module directly.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def build_direct_handler_cli_block(
    *,
    handler_id: str,
    purpose: str,
    recommended_entrypoints: list[str],
    override_flag: str = "--internal-handler-cli",
    notes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "agn.direct_handler_cli_guard.v1",
        "generated_at": utc_now_iso(),
        "ok": False,
        "status": "blocked",
        "error": "direct_handler_cli_requires_explicit_ack",
        "handler_id": str(handler_id).strip(),
        "purpose": str(purpose).strip(),
        "override_flag": str(override_flag).strip() or "--internal-handler-cli",
        "recommended_entrypoints": [str(item).strip() for item in recommended_entrypoints if str(item).strip()],
        "notes": list(notes or []),
    }


def should_block_direct_handler_cli(acknowledged: bool) -> bool:
    return not bool(acknowledged)


def render_direct_handler_cli_block(
    *,
    handler_id: str,
    purpose: str,
    recommended_entrypoints: list[str],
    override_flag: str = "--internal-handler-cli",
    notes: list[str] | None = None,
) -> str:
    payload = build_direct_handler_cli_block(
        handler_id=handler_id,
        purpose=purpose,
        recommended_entrypoints=recommended_entrypoints,
        override_flag=override_flag,
        notes=notes,
    )
    return json.dumps(payload, ensure_ascii=True, indent=2)
