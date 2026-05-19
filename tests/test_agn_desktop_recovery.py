from __future__ import annotations

from scripts.agn_desktop_recovery import _run_json, build_payload


def test_desktop_recovery_surfaces_mismatch_recovery(monkeypatch) -> None:
    monkeypatch.setattr("scripts.agn_desktop_recovery._frontmost", lambda: {"app": "Codex", "window": "Codex"})
    monkeypatch.setattr("scripts.agn_desktop_recovery._status", lambda: {"ok": True, "frontmost": {"app": "Codex"}})
    monkeypatch.setattr("scripts.agn_desktop_recovery._activate", lambda app: {"ok": True, "app": app})

    payload = build_payload(
        task_id="desktop-recovery-test",
        expected_app="Preview",
        last_failure_class="unsafe_action_blocked",
        last_error="write actions require allow_execute=true",
        capture_path="",
        window_name="",
        active_window=False,
        target_texts=[],
        apply_activate=True,
    )
    assert payload["app_mismatch"] is True
    assert payload["activation_result"]["app"] == "Preview"
    assert payload["recovery_plan"]


def test_run_json_handles_missing_gui_agent(monkeypatch) -> None:
    def _raise(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("scripts.agn_desktop_recovery.subprocess.run", _raise)
    payload = _run_json(["/missing/gui-agent", "status"])
    assert payload["ok"] is False
    assert payload["failure_class"] == "executable_not_found"
    assert payload["returncode"] == 127
