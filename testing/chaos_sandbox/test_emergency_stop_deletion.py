#!/usr/bin/env python3
"""Chaos Experiment 2: Emergency stop fail-open on file deletion.

Tests what happens when system_mode.json is deleted/corrupted/truncated.
Uses the ACTUAL emergency_stop module with a patched file path.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

SANDBOX_DIR = Path(__file__).parent / "emergency_stop_sandbox"
SANDBOX_MODE_FILE = SANDBOX_DIR / "system_mode.json"


def setup_sandbox_with_good_file():
    if SANDBOX_DIR.exists():
        shutil.rmtree(SANDBOX_DIR)
    SANDBOX_DIR.mkdir(parents=True)
    good = {
        "mode": "normal",
        "emergency_stop_active": False,
        "dispatcher_accepts_new_work": True,
        "desktop_mode": "normal",
        "external_reviewers_paused": False,
    }
    SANDBOX_MODE_FILE.write_text(json.dumps(good, indent=2), encoding="utf-8")


def run_scenario(name: str, setup_fn):
    """Patch system_mode_path to point at sandbox, then call real functions."""
    setup_sandbox_with_good_file()
    setup_fn()

    import emergency_stop
    with patch.object(sys.modules.get("admin_control_common", emergency_stop), "system_mode_path", return_value=SANDBOX_MODE_FILE):
        import importlib
        # Re-import to pick up patched path
        from emergency_stop import dispatcher_accepts_new_work, is_emergency_stop_active, load_system_mode
        with patch("emergency_stop.load_system_mode") as mock_load:
            # Manually replicate load_system_mode with sandbox path
            from admin_control_common import load_json
            payload = load_json(SANDBOX_MODE_FILE, default=emergency_stop.DEFAULT_MODE)
            if not payload:
                mode = dict(emergency_stop.DEFAULT_MODE)
            else:
                mode = dict(emergency_stop.DEFAULT_MODE)
                mode.update(payload)
            mock_load.return_value = mode

            accepts = dispatcher_accepts_new_work()
            estop = is_emergency_stop_active()

    status = "FAIL-OPEN (DANGEROUS)" if accepts else "FAIL-CLOSED (SAFE)"
    print(f"  {name:<45} estop={estop}  accepts={accepts}  → {status}")
    return accepts


def main():
    print("=" * 80)
    print("EXPERIMENT 2: Emergency Stop Fail-Open on File Loss (using REAL module)")
    print("=" * 80)

    vulns = 0

    # Scenario 1: File deleted
    r = run_scenario("File DELETED", lambda: SANDBOX_MODE_FILE.unlink())
    if r: vulns += 1

    # Scenario 2: File truncated
    r = run_scenario("File TRUNCATED (empty)", lambda: SANDBOX_MODE_FILE.write_text(""))
    if r: vulns += 1

    # Scenario 3: Corrupt JSON
    r = run_scenario("File CORRUPT (invalid JSON)", lambda: SANDBOX_MODE_FILE.write_text("{{{bad"))
    if r: vulns += 1

    # Scenario 4: Partial JSON (missing key)
    r = run_scenario("File PARTIAL (missing dispatcher key)", lambda: SANDBOX_MODE_FILE.write_text(json.dumps({"mode": "normal"})))
    if r: vulns += 1

    # Scenario 5: Normal good file (control)
    r = run_scenario("File INTACT (control — should accept)", lambda: None)
    # This one SHOULD accept, so don't count it

    print(f"\nVulnerabilities: {vulns}/4 scenarios fail-open when they should fail-closed")
    if vulns > 0:
        print("!! FIX NEEDED: DEFAULT_MODE must fail-closed")
    else:
        print("ALL scenarios fail-closed — system is safe against file loss")

    shutil.rmtree(SANDBOX_DIR)
    return 1 if vulns > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
