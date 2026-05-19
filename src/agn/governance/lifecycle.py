"""Lifecycle governance tools for integrity and index maintenance.

This is the real package implementation for AGN's lifecycle governance
helpers. The legacy script remains only as a compatibility shim.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

from agn.core.admin_control import repo_root
from agn.dispatch.event_store import append_event


ROOT = repo_root()
POLICY_PATH = ROOT / "config" / "lifecycle_policy.json"
SSOT_ROOT = ROOT / ".agn_workspace" / "event_driven" / "ssot"
MANIFEST_DIR = SSOT_ROOT / "manifests"
EVENTS_DIR = SSOT_ROOT / "events"
INDEX_DIR = SSOT_ROOT / "index"
DEFAULT_REPORT = ROOT / "reports" / "integrity_sweep_report.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from pointer_protocol import resolve_ref_path


PACKAGE_PATH = "agn.governance.lifecycle"
LEGACY_SCRIPT_SHIM = "scripts/lifecycle_governance.py"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _load_policy() -> dict[str, Any]:
    if not POLICY_PATH.exists():
        return {}
    try:
        payload = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _load_events(trace_id: str) -> list[dict[str, Any]]:
    path = EVENTS_DIR / f"{trace_id}.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def integrity_sweep(*, report_path: Path | None = None) -> dict[str, Any]:
    policy = _load_policy()
    cfg = policy.get("integrity_sweep", {}) if isinstance(policy.get("integrity_sweep"), dict) else {}
    target = report_path or (ROOT / str(cfg.get("report_path", str(DEFAULT_REPORT.relative_to(ROOT)))))

    missing: list[dict[str, str]] = []
    scanned = 0

    for manifest_path in sorted(MANIFEST_DIR.glob("*.manifest.json")):
        manifest = _load_manifest(manifest_path)
        trace_id = str(manifest.get("trace_id", manifest_path.stem.replace(".manifest", ""))).strip()
        refs = manifest.get("artifact_refs", []) if isinstance(manifest.get("artifact_refs"), list) else []
        for ref in refs:
            scanned += 1
            try:
                resolve_ref_path(str(ref))
            except Exception as exc:
                item = {
                    "trace_id": trace_id,
                    "ref": str(ref),
                    "error": f"{type(exc).__name__}:{exc}",
                }
                missing.append(item)
                events = _load_events(trace_id)
                task_id = str(events[-1].get("task_id", "")) if events else ""
                append_event(
                    trace_id=trace_id,
                    task_id=task_id,
                    event_type="INTEGRITY_ALERT",
                    payload={"missing_ref": str(ref), "error": item["error"], "source": "lifecycle_integrity_sweep"},
                    severity="error",
                )

    summary = {
        "generated_at": utc_now_iso(),
        "scanned_refs": scanned,
        "missing_count": len(missing),
        "missing_refs": missing,
    }
    _atomic_write_json(target, summary)
    return {"ok": len(missing) == 0, "report": str(target.relative_to(ROOT)), **summary}


def rebuild_index(*, index_path: Path | None = None) -> dict[str, Any]:
    policy = _load_policy()
    idx_cfg = policy.get("index", {}) if isinstance(policy.get("index"), dict) else {}
    target = index_path or (ROOT / str(idx_cfg.get("path", ".agn_workspace/event_driven/ssot/index/delivered_runs.json")))
    max_items = max(1, int(idx_cfg.get("max_items", 5000) or 5000))

    entries: list[dict[str, Any]] = []
    for event_file in sorted(EVENTS_DIR.glob("*.jsonl")):
        trace_id = event_file.stem
        events = _load_events(trace_id)
        if not events:
            continue
        delivered_idx = -1
        for idx, event in enumerate(events):
            if str(event.get("event_type", "")) == "STATE_TRANSITION":
                payload = event.get("payload", {})
                if isinstance(payload, dict) and str(payload.get("to", "")).upper() == "DELIVERED":
                    delivered_idx = idx
        if delivered_idx < 0:
            continue
        delivered_event = events[delivered_idx]
        task_id = str(delivered_event.get("task_id", "")).strip()
        key_refs: dict[str, str] = {}
        for event in events[: delivered_idx + 1]:
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                continue
            result_ref = str(payload.get("result_ref", "")).strip()
            if result_ref.startswith("agn://") and "result_ref" not in key_refs:
                key_refs["result_ref"] = result_ref
            detail = payload.get("detail")
            if isinstance(detail, dict):
                for key in ("summary_ref", "receipt_ref"):
                    node = detail.get(key)
                    ref = str((node or {}).get("ref", "")).strip() if isinstance(node, dict) else ""
                    if ref.startswith("agn://") and key not in key_refs:
                        key_refs[key] = ref

        entries.append(
            {
                "trace_id": trace_id,
                "task_id": task_id,
                "delivered_at": str(delivered_event.get("ts", "")),
                "summary": f"delivered trace {trace_id}",
                "key_refs": key_refs,
            }
        )

    entries = entries[-max_items:]
    payload = {
        "generated_at": utc_now_iso(),
        "count": len(entries),
        "items": entries,
    }
    _atomic_write_json(target, payload)
    return {"ok": True, "index": str(target.relative_to(ROOT)), "count": len(entries)}


def apply_retention() -> dict[str, Any]:
    policy = _load_policy()
    return {
        "ok": True,
        "policy_version": str(policy.get("version", "")),
        "note": "retention_policy_loaded_no_destructive_action",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evo5 lifecycle governance tools")
    parser.add_argument("command", choices=["integrity_sweep", "rebuild_index", "apply_retention"])
    args = parser.parse_args()

    if args.command == "integrity_sweep":
        out = integrity_sweep()
    elif args.command == "rebuild_index":
        out = rebuild_index()
    else:
        out = apply_retention()

    print(json.dumps(out, ensure_ascii=True))
    return 0 if bool(out.get("ok", False)) else 1


__all__ = [
    "DEFAULT_REPORT",
    "EVENTS_DIR",
    "INDEX_DIR",
    "LEGACY_SCRIPT_SHIM",
    "MANIFEST_DIR",
    "PACKAGE_PATH",
    "POLICY_PATH",
    "ROOT",
    "SSOT_ROOT",
    "apply_retention",
    "integrity_sweep",
    "main",
    "rebuild_index",
]


if __name__ == "__main__":
    raise SystemExit(main())
