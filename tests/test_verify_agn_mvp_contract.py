from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
VALIDATION_DIR = SCRIPTS_DIR / "validation"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(VALIDATION_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATION_DIR))

import verify_agn_mvp


def test_verify_agn_mvp_constants_cover_required_contract() -> None:
    expected_run_task_keys = {
        "ok",
        "task_id",
        "attempt",
        "decision",
        "commit_hash",
        "no_change_reason",
        "result_path",
        "verdict_path",
        "fail_reasons",
    }
    assert expected_run_task_keys.issubset(set(verify_agn_mvp.REQUIRED_RUN_TASK_KEYS))

    expected_checks = {
        "a_run_agn_task_protocol_ok",
        "b_run_agn_task_contract_fields",
        "c_ingest_repo_missing_rejected_without_dispatch_pollution",
        "d_telegram_listener_repo_missing_rejected_without_dispatch_pollution",
        "e_external_publish_unapproved_denied_with_audit",
        "f_hallucination_lock_unlock_redispatch_chain",
    }
    assert set(verify_agn_mvp.CHECK_IDS) == expected_checks
