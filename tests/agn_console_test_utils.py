from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agn_api.config import AppConfig
from agn_api.main import create_app
from agn_api.ssot_store import SSOTStore


def patch_event_sourcing_paths(monkeypatch, *, tmp_root: Path) -> Path:
    import agn_api.main as main

    event_root = tmp_root / ".agn_workspace" / "event_driven"
    ssot_root = event_root / "ssot"
    monkeypatch.setattr(main.es, "ROOT", tmp_root)
    monkeypatch.setattr(main.es, "EVENT_ROOT", event_root)
    monkeypatch.setattr(main.es, "SSOT_ROOT", ssot_root)
    monkeypatch.setattr(main.es, "EVENTS_DIR", ssot_root / "events")
    monkeypatch.setattr(main.es, "CHECKPOINT_DIR", ssot_root / "checkpoints")
    monkeypatch.setattr(main.es, "MANIFEST_DIR", ssot_root / "manifests")
    monkeypatch.setattr(main.es, "PERF_DIR", ssot_root / "perf")
    monkeypatch.setattr(main.es, "SNAPSHOT_DIR", ssot_root / "snapshots")
    monkeypatch.setattr(main.es, "ACTIONS_DIR", event_root / "actions")
    monkeypatch.setattr(main.es, "ACTIONS_PENDING_DIR", event_root / "actions" / "pending")
    monkeypatch.setattr(main.es, "ACTIONS_DONE_DIR", event_root / "actions" / "done")
    monkeypatch.setattr(main.es, "ACTIONS_FAILED_DIR", event_root / "actions" / "failed")
    monkeypatch.setattr(main.es, "CONTROL_DIR", event_root / "control")
    monkeypatch.setattr(main.es, "CONTROL_PENDING_DIR", event_root / "control" / "pending")
    monkeypatch.setattr(main.es, "CONTROL_DONE_DIR", event_root / "control" / "done")
    monkeypatch.setattr(main.es, "CONTROL_FAILED_DIR", event_root / "control" / "failed")
    monkeypatch.setattr(main.es, "SCRATCH_DIR", event_root / "scratch")
    monkeypatch.setattr(main.es, "REPO_MAP_PATH", ssot_root / "repo_refs.json")
    main.es.ensure_event_dirs()
    return event_root


def make_console_client(
    tmp_path: Path,
    monkeypatch,
    *,
    local_only_mode: bool = True,
) -> tuple[TestClient, SSOTStore, Path]:
    import agn_api.main as main

    ssot_dir = tmp_path / "ssot"
    audit_path = tmp_path / "audit" / "events.jsonl"
    config = AppConfig(
        ssot_dir=ssot_dir,
        audit_log_path=audit_path,
        jwt_secret="test-secret-at-least-32-bytes-long",
        jwt_algorithm="HS256",
        local_only_mode=bool(local_only_mode),
    )
    patch_event_sourcing_paths(monkeypatch, tmp_root=tmp_path)
    monkeypatch.setattr(main, "_repo_root", lambda: tmp_path)

    for sub in ("dispatch", "results", "verdicts", "reports", "audit"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)

    app = create_app(config)
    return TestClient(app), SSOTStore(ssot_dir), tmp_path
