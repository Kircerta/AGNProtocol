from __future__ import annotations

import json
from pathlib import Path

from agn_api.ssot_store import SSOTStore
from agn.governance import read_models as crm
from scripts import policy_gate as pg


def test_read_model_summary_matches_raw_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(
        crm,
        "build_capability_snapshot",
        lambda: {
            "generated_at": "2026-03-13T10:09:00+00:00",
            "surfaces": {"vision_parser": {"available": True}},
            "provider_policy": {"reviewer_policy": {"preferred_order": ["claude", "gemini"]}},
            "surface_taxonomy": {"review": ["flagship_review"]},
        },
    )
    ssot_dir = tmp_path / "ssot"
    store = SSOTStore(ssot_dir)
    store.save_task(
        {
            "id": "task-read-1",
            "correlation_id": "trace-read-1",
            "request_summary": "first task",
            "review_requested": True,
            "risk_level": "medium",
            "executor_provider": "codex",
            "reviewer_provider": "gemini",
        }
    )
    store.save_task(
        {
            "id": "task-read-2",
            "correlation_id": "trace-read-2",
            "request_summary": "second task",
            "review_requested": False,
            "risk_level": "low",
            "executor_provider": "codex",
            "reviewer_provider": "gemini",
        }
    )

    checkpoint_dir = tmp_path / ".agn_workspace" / "event_driven" / "ssot" / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    (checkpoint_dir / "task-read-1.json").write_text(
        json.dumps({"task_id": "task-read-1", "trace_id": "trace-read-1", "state": "PLANNED", "paused": False, "updated_at": "2026-03-13T10:00:00+00:00"}),
        encoding="utf-8",
    )
    (checkpoint_dir / "task-read-2.json").write_text(
        json.dumps({"task_id": "task-read-2", "trace_id": "trace-read-2", "state": "NEED_ADMIN", "paused": True, "updated_at": "2026-03-13T10:05:00+00:00"}),
        encoding="utf-8",
    )

    events_dir = tmp_path / ".agn_workspace" / "event_driven" / "ssot" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / "trace-read-2.jsonl").write_text(
        json.dumps({"trace_id": "trace-read-2", "task_id": "task-read-2", "event_type": "NEED_ADMIN", "severity": "warn", "ts": "2026-03-13T10:06:00+00:00"}) + "\n",
        encoding="utf-8",
    )

    bus_dir = tmp_path / "runtime" / "bus"
    bus_dir.mkdir(parents=True, exist_ok=True)
    (bus_dir / "index.jsonl").write_text(
        json.dumps({"kind": "message", "ts": "2026-03-13T10:07:00+00:00", "from": "dispatcher", "to": "policy_gate", "related_trace": "trace-read-2", "related_task": "task-read-2"}) + "\n",
        encoding="utf-8",
    )
    dead_dir = bus_dir / "dead_letter"
    dead_dir.mkdir(parents=True, exist_ok=True)
    (dead_dir / "one.json").write_text(json.dumps({"ts": "2026-03-13T10:08:00+00:00", "message": {"related_trace": "trace-read-1"}}), encoding="utf-8")

    dispatcher_requests = tmp_path / "runtime" / "dispatcher" / "requests"
    dispatcher_results = tmp_path / "runtime" / "dispatcher" / "results"
    dispatcher_requests.mkdir(parents=True, exist_ok=True)
    dispatcher_results.mkdir(parents=True, exist_ok=True)
    (dispatcher_requests / "dispatch-vision.json").write_text(
        json.dumps(
            {
                "request_id": "dispatch-vision",
                "created_at": "2026-03-13T10:07:30+00:00",
                "trace_id": "trace-read-vision",
                "task_id": "task-read-vision",
                "caller": "codex",
                "target": "vision_parser",
                "target_kind": "vision_parser",
                "intent": "inspect_visual",
                "risk_level": "low",
            }
        ),
        encoding="utf-8",
    )
    (dispatcher_results / "dispatch-vision.json").write_text(
        json.dumps(
            {
                "request_id": "dispatch-vision",
                "completed_at": "2026-03-13T10:07:40+00:00",
                "trace_id": "trace-read-vision",
                "task_id": "task-read-vision",
                "target": "vision_parser",
                "target_kind": "vision_parser",
                "ok": True,
                "failure_class": "",
                "result": {
                    "handler": "vision_parser",
                    "quarantined_any": True,
                    "redacted_any": True,
                    "security_refs": ["agn://artifact/" + "s" * 64],
                    "evidence_refs_present": True,
                },
            }
        ),
        encoding="utf-8",
    )

    gate_request_ref = tmp_path / "request.json"
    gate_request_ref.write_text("{}", encoding="utf-8")
    pg.create_gate_entry(
        request={
            "trace_id": "trace-read-2",
            "task_id": "task-read-2",
            "caller": "admin",
            "target": "memory_recorder",
            "target_kind": "memory_recorder",
            "intent": "record_fact",
            "reason": "needs approval",
            "risk_level": "high",
            "input_payload": {},
        },
        request_ref=str(gate_request_ref),
        evaluation={"rule_id": "high_risk_dispatch_gate", "action_type": "", "requires_audit_refs": False, "council_required": True},
    )

    out = crm.refresh_read_models()
    assert out["ok"] is True

    overview = json.loads((tmp_path / "runtime" / "admin_control" / "read_models" / "overview.json").read_text(encoding="utf-8"))
    task_board = json.loads((tmp_path / "runtime" / "admin_control" / "read_models" / "task_board.json").read_text(encoding="utf-8"))
    approval_gate = json.loads((tmp_path / "runtime" / "admin_control" / "read_models" / "approval_gate.json").read_text(encoding="utf-8"))
    raw_stream = json.loads((tmp_path / "runtime" / "admin_control" / "read_models" / "raw_stream.json").read_text(encoding="utf-8"))
    capability_snapshot = json.loads((tmp_path / "runtime" / "admin_control" / "read_models" / "capability_snapshot.json").read_text(encoding="utf-8"))
    execution_discipline = json.loads((tmp_path / "runtime" / "admin_control" / "read_models" / "execution_discipline.json").read_text(encoding="utf-8"))
    host_info = json.loads((tmp_path / "runtime" / "admin_control" / "read_models" / "host_info.json").read_text(encoding="utf-8"))
    infrastructure_map = json.loads((tmp_path / "runtime" / "admin_control" / "read_models" / "infrastructure_map.json").read_text(encoding="utf-8"))
    evolution_pipeline = json.loads((tmp_path / "runtime" / "admin_control" / "read_models" / "evolution_pipeline.json").read_text(encoding="utf-8"))
    reconstruction_status = json.loads((tmp_path / "runtime" / "admin_control" / "read_models" / "reconstruction_status.json").read_text(encoding="utf-8"))

    assert overview["counts"]["active_tasks"] == 2
    assert overview["counts"]["blocked_tasks"] == 1
    assert overview["counts"]["policy_gate_pending"] == len([item for item in approval_gate["items"] if item["effective_status"] == "pending"])
    assert len(task_board["items"]) == 2
    assert any(item["trace_id"] == "trace-read-2" for item in raw_stream["items"])
    assert any(item["kind"] == "dispatcher_request" and item["trace_id"] == "trace-read-vision" for item in raw_stream["items"])
    assert any(
        item["kind"] == "dispatcher_result"
        and item["payload"]["quarantined_any"] is True
        and item["payload"]["security_refs"] == ["agn://artifact/" + "s" * 64]
        for item in raw_stream["items"]
    )
    assert capability_snapshot["surfaces"]["vision_parser"]["available"] is True
    assert execution_discipline["has_preflight"] is False
    assert execution_discipline["provider_policy"]["reviewer_policy"]["preferred_order"] == ["claude", "gemini"]
    assert host_info["schema_version"] == "agn.host_info.v1"
    assert infrastructure_map["schema_version"] == "agn.infrastructure_map.v1"
    assert evolution_pipeline["schema_version"] == "agn.evolution_pipeline.v1"
    assert reconstruction_status["schema_version"] == "agn.reconstruction_status.v1"
    assert overview["generated_at"]
    assert task_board["generated_at"]
    assert approval_gate["generated_at"]


def test_execution_discipline_reads_latest_preflight(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    preflight_dir = tmp_path / "runtime" / "admin_control" / "preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    (preflight_dir / "latest.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-03-13T22:00:00+00:00",
                "task_summary": "Ship the next AGN task cleanly.",
                "task_id": "task-1",
                "trace_id": "trace-1",
                "risk_level": "high",
                "subsystem": "agn2",
                "execution_checks": [
                    {"check": "system_mode", "status": "ok"},
                    {"check": "review_plan", "status": "attention"},
                ],
                "operator_brief": {
                    "status": "attention",
                    "summary": "1 attention item remains.",
                    "counts": {"blocking": 0, "attention": 1, "informational": 2},
                },
                "task_start_kernel": {
                    "schema_version": "agn.task_start_kernel.v1",
                    "summary": {"status": "attention", "host_readiness": "attention", "provider_count": 2},
                },
                "recommended_surfaces": [{"surface": "control_plane", "entry": "open /Applications/AGN2.0 Control Plane.app"}],
                "regression_signals": ["Started in a plain shell."],
                "next_actions": ["Open the control plane first."],
                "worker_and_review_state": {"claude": True, "gemini": True, "deepseek": True},
            }
        ),
        encoding="utf-8",
    )
    payload = crm.build_execution_discipline_model(
        {
            "provider_policy": {"reviewer_policy": {"preferred_order": ["claude", "gemini"]}},
            "surface_taxonomy": {"authority_control": ["control_plane"]},
        }
    )
    assert payload["has_preflight"] is True
    assert payload["status"] == "attention"
    assert payload["operator_brief"]["summary"] == "1 attention item remains."
    assert payload["current_task"]["summary"] == "Ship the next AGN task cleanly."
    assert payload["task_start_kernel"]["schema_version"] == "agn.task_start_kernel.v1"
    assert payload["provider_policy"]["reviewer_policy"]["preferred_order"] == ["claude", "gemini"]
    assert payload["recommended_surfaces"][0]["surface"] == "control_plane"


def test_read_model_generated_at_falls_back_when_system_mode_timestamp_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    payload = crm.build_task_board_model()
    assert payload["generated_at"]
