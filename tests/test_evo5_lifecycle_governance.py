from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from scripts.event_sourcing import append_event, load_events, write_manifest
from scripts.lifecycle_governance import integrity_sweep, rebuild_index
from scripts.pointer_protocol import resolve_ref_path, write_text_artifact

ROOT = Path(__file__).resolve().parents[1]


def test_integrity_sweep_detects_missing_artifact() -> None:
    trace_id = f"trace-evo5-int-{uuid4().hex[:8]}"
    task_id = f"task-evo5-int-{uuid4().hex[:8]}"

    artifact = write_text_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="victim",
        content=f"hello-{uuid4().hex}",
        media_type="text/plain",
        filename="victim.txt",
        source="test",
    )
    append_event(trace_id=trace_id, task_id=task_id, event_type="ARTIFACT_LINKED", payload={"artifact_ref": artifact.ref})
    write_manifest(trace_id)

    victim = Path(resolve_ref_path(artifact.ref))
    victim.unlink(missing_ok=True)

    summary = integrity_sweep()
    assert summary["missing_count"] >= 1
    assert any(item.get("ref") == artifact.ref for item in summary["missing_refs"])

    events = load_events(trace_id)
    assert any(e.get("event_type") == "INTEGRITY_ALERT" for e in events)


def test_lifecycle_index_contains_delivered_runs() -> None:
    trace_id = f"trace-evo5-idx-{uuid4().hex[:8]}"
    task_id = f"task-evo5-idx-{uuid4().hex[:8]}"

    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="STATE_TRANSITION",
        payload={"from": "DELIVERY_GATE", "to": "DELIVERED", "reason": "test"},
    )

    out = rebuild_index()
    index_path = ROOT / str(out["index"])
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    items = payload.get("items", []) if isinstance(payload, dict) else []
    assert any(isinstance(item, dict) and item.get("trace_id") == trace_id and item.get("task_id") == task_id for item in items)
