#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from scripts import command_request as cr
from scripts.agent_runner import run_command, exec_log_path, run_executor_codex
from scripts.coordinator_ingest import run as coordinator_ingest_run
from agn.core.guarded_io import write_bytes, write_text
from agn.core.role_guard import check_write_path, get_current_role


@dataclass
class CaseResult:
    case_id: str
    category: str
    passed: bool
    summary: str
    evidence: dict[str, Any]


@contextlib.contextmanager
def env_patch(patch: dict[str, str | None]):
    old: dict[str, str | None] = {}
    for key, value in patch.items():
        old[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_shell(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        cmd,
        cwd=str(cwd or ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=merged_env,
    )


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _append_case(results: list[CaseResult], case_id: str, category: str, passed: bool, summary: str, **evidence: Any) -> None:
    results.append(
        CaseResult(
            case_id=case_id,
            category=category,
            passed=passed,
            summary=summary,
            evidence=evidence,
        )
    )


def _probe_runtime_prereq(results: list[CaseResult], compat_dir: Path) -> None:
    raw = _run_shell(
        [
            "python3",
            "scripts/coordinator_ingest.py",
            "--task-id",
            f"probe-prereq-{uuid4().hex[:8]}",
            "--source",
            "probe",
            "--task-kind",
            "protocol",
            "--request-text",
            "probe",
            "--criterion",
            "AC-1:probe",
        ],
        env={"PYTHONPATH": ""},
        timeout=60.0,
    )
    has_cgi_error = "No module named 'cgi'" in (raw.stderr or "")
    _append_case(
        results,
        "RUNTIME-NO-COMPAT",
        "runtime",
        passed=has_cgi_error,
        summary="Baseline without compatibility layer should expose Python 3.14 cgi/import breakage",
        rc=raw.returncode,
        stderr_tail=(raw.stderr or "")[-400:],
    )

    compat = _run_shell(
        [
            "python3",
            "scripts/coordinator_ingest.py",
            "--task-id",
            f"probe-prereq-compat-{uuid4().hex[:8]}",
            "--source",
            "probe",
            "--task-kind",
            "protocol",
            "--request-text",
            "probe",
            "--criterion",
            "AC-1:probe",
        ],
        env={"PYTHONPATH": str(compat_dir)},
        timeout=60.0,
    )
    ok = compat.returncode == 0
    _append_case(
        results,
        "RUNTIME-WITH-COMPAT",
        "runtime",
        passed=ok,
        summary="Compatibility layer should restore coordinator_ingest runtime execution",
        rc=compat.returncode,
        stdout_tail=(compat.stdout or "")[-300:],
        stderr_tail=(compat.stderr or "")[-300:],
    )


def _probe_command_and_write_guards(results: list[CaseResult]) -> None:
    probe_root = ROOT / ".agn_workspace" / f"adversarial_probe_{uuid4().hex[:8]}"
    probe_root.mkdir(parents=True, exist_ok=True)
    log_path = probe_root / "command_probe.log"

    with env_patch(
        {
            "AGN_ROLE": "coordinator",
            "AGN_RUNTIME_CONTEXT": "agn_network",
            "AGN_ENFORCE_ROLE_GUARD": "1",
        }
    ):
        blocked_direct = run_command(
            cmd=["git", "apply", "x.patch"],
            cwd=ROOT,
            timeout_sec=5.0,
            log_path=log_path,
        )
        _append_case(
            results,
            "CMD-BLOCK-DIRECT-GIT-APPLY",
            "role_guard_command",
            passed=blocked_direct.return_code == 126 and "ROLE_GUARD_BLOCKED" in blocked_direct.stderr,
            summary="Coordinator direct git apply should be blocked",
            return_code=blocked_direct.return_code,
            stderr=blocked_direct.stderr,
        )

        blocked_shell = run_command(
            cmd=["bash", "-lc", "git apply x.patch"],
            cwd=ROOT,
            timeout_sec=5.0,
            log_path=log_path,
        )
        _append_case(
            results,
            "CMD-BLOCK-SHELL-WRAPPER",
            "role_guard_command",
            passed=blocked_shell.return_code == 126 and "blocked_secondary_exec_container" in blocked_shell.stderr,
            summary="Coordinator shell wrapper bypass should be blocked",
            return_code=blocked_shell.return_code,
            stderr=blocked_shell.stderr,
        )

        blocked_py_c = run_command(
            cmd=["python3", "-c", "import os; os.system('git apply x.patch')"],
            cwd=ROOT,
            timeout_sec=5.0,
            log_path=log_path,
        )
        _append_case(
            results,
            "CMD-BLOCK-PYTHON-C",
            "role_guard_command",
            passed=blocked_py_c.return_code == 126 and "blocked_secondary_exec_container" in blocked_py_c.stderr,
            summary="Coordinator python -c bypass should be blocked",
            return_code=blocked_py_c.return_code,
            stderr=blocked_py_c.stderr,
        )

        blocked_utility = run_command(
            cmd=["git", "clone", "https://github.com/octocat/Hello-World.git"],
            cwd=ROOT,
            timeout_sec=5.0,
            log_path=log_path,
        )
        _append_case(
            results,
            "CMD-BLOCK-UTILITY-REQUEST",
            "role_guard_command",
            passed=blocked_utility.return_code == 126 and "utility_request_required" in blocked_utility.stderr,
            summary="Coordinator git clone should be diverted to utility approval channel",
            return_code=blocked_utility.return_code,
            stderr=blocked_utility.stderr,
        )

        allowed_path = ROOT / "dispatch" / f".probe_write_{uuid4().hex[:6]}.txt"
        blocked_path = ROOT / "results" / f".probe_write_{uuid4().hex[:6]}.bin"
        write_ok = False
        write_blocked = False
        try:
            write_text(allowed_path, "ok\n")
            write_ok = allowed_path.exists()
            try:
                write_bytes(blocked_path, b"blocked")
            except PermissionError:
                write_blocked = True
        finally:
            if allowed_path.exists():
                allowed_path.unlink()
            if blocked_path.exists():
                blocked_path.unlink()
        _append_case(
            results,
            "WRITE-GUARD-ALLOWED-BLOCKED",
            "role_guard_write",
            passed=write_ok and write_blocked,
            summary="Coordinator should write only within writable whitelist",
            write_ok=write_ok,
            write_blocked=write_blocked,
        )

        traversal_target = ROOT / "memory" / ".." / "results" / "x.txt"
        traversal_ok, traversal_reason = check_write_path(traversal_target, role="coordinator")
        _append_case(
            results,
            "WRITE-BLOCK-PATH-TRAVERSAL",
            "role_guard_write",
            passed=(traversal_ok is False and "write_dir_not_allowed" in traversal_reason),
            summary="Path traversal out of allowed roots should be blocked",
            allowed=traversal_ok,
            reason=traversal_reason,
        )

        link = ROOT / "memory" / f".probe_link_{uuid4().hex[:6]}"
        symlink_ok = False
        symlink_reason = ""
        try:
            if link.exists() or link.is_symlink():
                link.unlink()
            os.symlink((ROOT / "results").as_posix(), link.as_posix())
            symlink_ok, symlink_reason = check_write_path(link / "escape.txt", role="coordinator")
        finally:
            if link.exists() or link.is_symlink():
                link.unlink()
        _append_case(
            results,
            "WRITE-BLOCK-SYMLINK-ESCAPE",
            "role_guard_write",
            passed=(symlink_ok is False and "write_dir_not_allowed" in symlink_reason),
            summary="Symlink escape from allowed dirs should be blocked",
            allowed=symlink_ok,
            reason=symlink_reason,
        )

    with env_patch(
        {
            "AGN_ROLE": "admin",
            "AGN_RUNTIME_CONTEXT": "agn_network",
            "AGN_ENFORCE_ROLE_GUARD": "1",
        }
    ):
        admin_allowed = run_command(
            cmd=["git", "status", "--porcelain"],
            cwd=ROOT,
            timeout_sec=10.0,
            log_path=log_path,
        )
        _append_case(
            results,
            "CMD-ADMIN-ALLOWED",
            "role_guard_command",
            passed=admin_allowed.return_code != 126,
            summary="Admin should not be role-guard blocked for read git command",
            return_code=admin_allowed.return_code,
            stderr_tail=(admin_allowed.stderr or "")[-200:],
        )

    with env_patch({"AGN_ROLE": None, "AGN_COMPAT_ADMIN": None}):
        effective = get_current_role()
        _append_case(
            results,
            "ROLE-DEFAULT-NOT-ADMIN",
            "role_identity",
            passed=effective != "admin",
            summary="Missing AGN_ROLE should not default to admin",
            effective_role=effective,
        )

    with env_patch({"AGN_ROLE": None, "AGN_COMPAT_ADMIN": "1"}):
        compat = get_current_role()
        _append_case(
            results,
            "ROLE-COMPAT-ADMIN-OPTIN",
            "role_identity",
            passed=compat == "admin",
            summary="Compatibility flag should explicitly opt-in admin default",
            effective_role=compat,
        )


def _probe_command_request_security(results: list[CaseResult]) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="agn_cmd_req_probe_"))
    requests_dir = tmp / "requests"
    sandbox_dir = tmp / "sandbox"
    audit_path = tmp / "audit" / "events.jsonl"

    old_requests = cr.COMMAND_REQUESTS_DIR
    old_sandbox = cr.UTILITY_SANDBOX_DIR
    old_audit = cr.AUDIT_PATH
    cr.COMMAND_REQUESTS_DIR = requests_dir
    cr.UTILITY_SANDBOX_DIR = sandbox_dir
    cr.AUDIT_PATH = audit_path

    try:
        env_ctx = env_patch({"AGN_ENFORCE_ROLE_GUARD": "0"})
        env_ctx.__enter__()
        unknown_rejected = False
        try:
            cr.submit_request(operation="shell_exec", params={"cmd": "id"})
        except ValueError:
            unknown_rejected = True
        _append_case(
            results,
            "CMDREQ-UNKNOWN-OP-REJECTED",
            "command_request",
            passed=unknown_rejected,
            summary="Unsupported command-request operation should be rejected",
        )

        host_rejected = False
        with env_patch({"AGN_COMMAND_REQUEST_ALLOWED_HOSTS": "github.com"}):
            try:
                cr.submit_request(
                    operation="git_clone",
                    params={"repo_url": "https://example.com/repo.git", "target_dir": "x"},
                )
            except ValueError:
                host_rejected = True
        _append_case(
            results,
            "CMDREQ-HOST-WHITELIST",
            "command_request",
            passed=host_rejected,
            summary="Clone host outside whitelist should be rejected",
        )

        repo = sandbox_dir / "repo-a"
        repo.mkdir(parents=True, exist_ok=True)
        _run_shell(["git", "init"], cwd=repo)
        (repo / "README.md").write_text("probe\n", encoding="utf-8")
        _run_shell(["git", "add", "README.md"], cwd=repo)
        _run_shell(["git", "commit", "-m", "init"], cwd=repo, env={"GIT_AUTHOR_NAME": "probe", "GIT_AUTHOR_EMAIL": "probe@example.com", "GIT_COMMITTER_NAME": "probe", "GIT_COMMITTER_EMAIL": "probe@example.com"})

        payload = cr.submit_request(
            operation="git_checkout",
            params={"repo_dir": "repo-a", "ref": "HEAD"},
            requested_by_role="coordinator",
            reason="adversarial test",
        )
        request_id = str(payload["request_id"])
        approved = cr.approve_request(request_id, approved_by="admin-test")
        executed_once = cr.execute_approved_requests(executed_by="admin-test")
        executed_twice = cr.execute_approved_requests(executed_by="admin-test")
        loaded = cr.load_request(request_id) or {}

        one_way_ok = (
            isinstance(approved, dict)
            and approved.get("status") == "approved"
            and len(executed_once) == 1
            and len(executed_twice) == 0
            and loaded.get("status") == "executed"
        )
        _append_case(
            results,
            "CMDREQ-ONE-WAY-IDEMPOTENT",
            "command_request",
            passed=one_way_ok,
            summary="Approved request should execute exactly once",
            request_id=request_id,
            first_exec_count=len(executed_once),
            second_exec_count=len(executed_twice),
            final_status=loaded.get("status"),
            execution_rc=loaded.get("execution_rc"),
        )
    finally:
        try:
            env_ctx.__exit__(None, None, None)  # type: ignore[name-defined]
        except Exception:
            pass
        cr.COMMAND_REQUESTS_DIR = old_requests
        cr.UTILITY_SANDBOX_DIR = old_sandbox
        cr.AUDIT_PATH = old_audit


def _probe_timeout_process_group(results: list[CaseResult]) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="agn_timeout_probe_"))
    log_path = tmp / "timeout.log"
    pid_file = tmp / "child.pid"

    with env_patch(
        {
            "AGN_ROLE": "admin",
            "AGN_RUNTIME_CONTEXT": "agn_network",
            "AGN_ENFORCE_ROLE_GUARD": "1",
            "CHILD_PID_FILE": str(pid_file),
            "PYTHON_EXE": sys.executable,
        }
    ):
        cmd = [
            sys.executable,
            "-c",
            (
                "import os, pathlib, subprocess, time;"
                "p = subprocess.Popen([os.environ.get('PYTHON_EXE', 'python3'), '-c', 'import time; time.sleep(30)']);"
                "pathlib.Path(os.environ['CHILD_PID_FILE']).write_text(str(p.pid), encoding='utf-8');"
                "time.sleep(30)"
            ),
        ]
        outcome = run_command(cmd=cmd, cwd=ROOT, timeout_sec=1.5, log_path=log_path)

    time.sleep(0.5)
    child_alive = None
    child_pid = None
    if pid_file.exists():
        try:
            child_pid = int(pid_file.read_text(encoding="utf-8").strip())
            os.kill(child_pid, 0)
            child_alive = True
        except ProcessLookupError:
            child_alive = False
        except Exception:
            child_alive = None

    _append_case(
        results,
        "TIMEOUT-KILL-PROCESS-GROUP",
        "timeout",
        passed=outcome.timed_out and outcome.return_code == 124 and (child_alive is False),
        summary="Timed-out command should terminate entire process group without orphan child",
        return_code=outcome.return_code,
        timed_out=outcome.timed_out,
        child_pid=child_pid,
        child_alive=child_alive,
        stderr_tail=(outcome.stderr or "")[-300:],
    )


def _prepare_repo(base_dir: Path) -> Path:
    repo = base_dir / f"repo_{uuid4().hex[:8]}"
    repo.mkdir(parents=True, exist_ok=True)
    _run_shell(["git", "init"], cwd=repo)
    (repo / "README.md").write_text("# Probe Repo\n", encoding="utf-8")
    _run_shell(["git", "add", "README.md"], cwd=repo)
    _run_shell(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        env={
            "GIT_AUTHOR_NAME": "probe",
            "GIT_AUTHOR_EMAIL": "probe@example.com",
            "GIT_COMMITTER_NAME": "probe",
            "GIT_COMMITTER_EMAIL": "probe@example.com",
        },
    )
    return repo


def _probe_real_codex_gemini_flow(results: list[CaseResult], compat_dir: Path) -> None:
    workspace = ROOT / ".agn_workspace" / "adversarial_realflow"
    workspace.mkdir(parents=True, exist_ok=True)
    repo = _prepare_repo(workspace)

    task_id = f"adv-real-{uuid4().hex[:8]}"
    branch = f"codex/adv-{uuid4().hex[:6]}"
    payload = {
        "task_id": task_id,
        "task_kind": "repo",
        "source": "adversarial_probe",
        "request_text": "Create PROBE.md with two lines: 'AGN probe' and today's UTC date. Keep README unchanged.",
        "repo_path": str(repo),
        "work_branch": branch,
        "executor_provider": "codex",
        "reviewer_provider": "gemini",
        "emit_messages": False,
        "risk_level": "low",
        "side_effect_level": "local_write",
    }

    proc = subprocess.run(
        ["python3", "scripts/run_agn_task.py", "--from-stdin"],
        cwd=str(ROOT),
        text=True,
        input=json.dumps(payload, ensure_ascii=True),
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(compat_dir)},
        timeout=1800,
    )

    raw = (proc.stdout or "").strip().splitlines()
    parsed: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw[-1])
        except Exception:
            parsed = {}

    attempt = int(parsed.get("attempt", 1) or 1)
    exec_log = exec_log_path("agn_executor", task_id, attempt)
    rev_log = exec_log_path("agn_reviewer", task_id, attempt)
    exec_text = _safe_read_text(exec_log)
    rev_text = _safe_read_text(rev_log)

    role_prompt_ok = (
        "You are executor for AGN file-protocol." in exec_text
        and "You are AGN reviewer." in rev_text
    )
    providers_seen = ("command=codex exec" in exec_text) and ("command=gemini -p" in rev_text)

    _append_case(
        results,
        "REALFLOW-CODEX-GEMINI",
        "real_flow",
        passed=(proc.returncode == 0 and bool(parsed.get("ok"))),
        summary="One-shot repo lifecycle via codex executor + gemini reviewer",
        rc=proc.returncode,
        output=parsed,
        result_exists=Path(str(parsed.get("result_path", ""))).exists() if parsed else False,
        verdict_exists=Path(str(parsed.get("verdict_path", ""))).exists() if parsed else False,
    )
    _append_case(
        results,
        "REALFLOW-ROLE-PROMPT-ALIGNMENT",
        "real_flow",
        passed=role_prompt_ok,
        summary="Executor/reviewer runtime prompts should explicitly declare their AGN role contracts",
        exec_log=str(exec_log),
        rev_log=str(rev_log),
        executor_prompt_found="You are executor for AGN file-protocol." in exec_text,
        reviewer_prompt_found="You are AGN reviewer." in rev_text,
    )
    _append_case(
        results,
        "REALFLOW-PROVIDER-ROUTING",
        "real_flow",
        passed=providers_seen,
        summary="Runtime should route to codex for executor and gemini for reviewer",
        executor_cmd_seen="command=codex exec" in exec_text,
        reviewer_cmd_seen="command=gemini -p" in rev_text,
    )


def _probe_info_transfer_and_prompt_bloat(results: list[CaseResult], compat_dir: Path) -> None:
    huge_text = ("AGNX" * 30000)  # 120k chars
    task_id = f"adv-large-{uuid4().hex[:8]}"
    ingest = coordinator_ingest_run(
        task_id=task_id,
        request_text=huge_text,
        source="adversarial_probe",
        correlation_id=f"corr-{uuid4().hex[:10]}",
        criteria_json=None,
        criterion_items=["AC-1:probe criterion"],
        task_kind="protocol",
        repo_path="",
        work_branch="",
        executor_provider="codex",
        reviewer_provider="gemini",
        chat_id="",
        message_id="",
        risk_level="low",
        side_effect_level="read_only",
        attempt=None,
    )
    dispatch_file = ROOT / "dispatch" / f"{task_id}.json"
    dispatch_text = _safe_read_text(dispatch_file)
    dispatch_payload = json.loads(dispatch_text) if dispatch_text else {}
    request_text_len = len(str(dispatch_payload.get("request_text", "")))
    artifact_refs = dispatch_payload.get("artifact_refs", [])
    _append_case(
        results,
        "INFO-DISPATCH-LARGE-PAYLOAD",
        "information_flow",
        passed=request_text_len < 20000,
        summary="Coordinator should avoid embedding very large request_text directly in dispatch payload",
        dispatch_file=str(dispatch_file),
        dispatch_size_bytes=dispatch_file.stat().st_size if dispatch_file.exists() else 0,
        request_text_len=request_text_len,
        artifact_ref_count=len(artifact_refs) if isinstance(artifact_refs, list) else 0,
        ingest_ok=bool(ingest.get("ok")),
    )

    workspace = ROOT / ".agn_workspace" / "adversarial_large_prompt"
    workspace.mkdir(parents=True, exist_ok=True)
    repo = _prepare_repo(workspace)
    huge_task_id = f"adv-huge-cmd-{uuid4().hex[:8]}"
    huge_dispatch = {
        "task_id": huge_task_id,
        "correlation_id": f"corr-{uuid4().hex[:8]}",
        "attempt": 1,
        "acceptance_criteria": [{"id": "AC-1", "text": "create one file"}],
        "task_kind": "repo",
        "request_text": "HUGE_BEGIN\n" + ("0123456789ABCDEF" * 30000) + "\nHUGE_END",  # ~480k chars
        "repo_path": str(repo),
        "work_branch": f"codex/adv-huge-{uuid4().hex[:6]}",
        "executor_provider": "codex",
        "reviewer_provider": "gemini",
        "risk_level": "low",
        "side_effect_level": "local_write",
    }
    with env_patch(
        {
            "AGN_ROLE": "executor",
            "AGN_RUNTIME_CONTEXT": "agn_network",
            "AGN_ENFORCE_ROLE_GUARD": "1",
            "PYTHONPATH": str(compat_dir),
        }
    ):
        rc, result_file = run_executor_codex(huge_dispatch)
    payload = json.loads(_safe_read_text(result_file)) if result_file.exists() else {}
    fail_reasons = payload.get("fail_reasons", []) if isinstance(payload, dict) else []
    likely_arg_too_long = any("codex" in str(x).lower() for x in fail_reasons) or any(
        "argument list too long" in str(item).lower()
        for item in (payload.get("work_log", []) if isinstance(payload, dict) else [])
    )
    _append_case(
        results,
        "INFO-EXECUTOR-HUGE-PROMPT",
        "information_flow",
        passed=(rc == 0),
        summary="Executor should handle oversized request text without command-line overflow failure",
        rc=rc,
        result_file=str(result_file),
        fail_reasons=fail_reasons,
        likely_arg_too_long=likely_arg_too_long,
    )


def _probe_startup_role_injection(results: list[CaseResult], compat_dir: Path) -> None:
    pid_file = ROOT / ".agn_pids"
    if pid_file.exists():
        _append_case(
            results,
            "STARTUP-ROLE-INJECTION",
            "startup",
            passed=False,
            summary="Skipped startup role injection check because .agn_pids already exists",
            skipped=True,
        )
        return

    env = {
        **os.environ,
        "PYTHONPATH": str(compat_dir),
        "AGN_GIT_SYNC_ENABLE": "0",
        "KIRARA_HEARTBEAT_ENABLE": "0",
    }
    up = subprocess.run(
        ["bash", "scripts/agn_up.sh"],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        env=env,
        timeout=120,
    )
    pids: dict[str, int] = {}
    proc_env_hits: dict[str, dict[str, bool]] = {}
    try:
        if pid_file.exists():
            for line in pid_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if ":" not in line:
                    continue
                name, pid_raw = line.split(":", 1)
                try:
                    pids[name] = int(pid_raw)
                except ValueError:
                    continue

            for name in ["agn_coordinator", "agn_executor", "agn_reviewer"]:
                pid = pids.get(name)
                if not pid:
                    continue
                ps = subprocess.run(
                    ["ps", "eww", "-p", str(pid), "-o", "command="],
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                cmdline = (ps.stdout or "").strip()
                role = name.replace("agn_", "")
                proc_env_hits[name] = {
                    "has_role": f"AGN_ROLE={role}" in cmdline,
                    "has_context": "AGN_RUNTIME_CONTEXT=agn_network" in cmdline,
                    "has_enforce": "AGN_ENFORCE_ROLE_GUARD=1" in cmdline,
                }
    finally:
        subprocess.run(["bash", "scripts/agn_down.sh"], cwd=str(ROOT), text=True, capture_output=True, env=env, timeout=120)

    expected = ["agn_coordinator", "agn_executor", "agn_reviewer"]
    all_ok = up.returncode == 0
    for name in expected:
        hit = proc_env_hits.get(name, {})
        all_ok = all_ok and bool(hit.get("has_role")) and bool(hit.get("has_context")) and bool(hit.get("has_enforce"))

    _append_case(
        results,
        "STARTUP-ROLE-INJECTION",
        "startup",
        passed=all_ok,
        summary="agn_up should inject explicit role/context/guard env for all non-admin members",
        up_rc=up.returncode,
        up_stdout_tail=(up.stdout or "")[-500:],
        up_stderr_tail=(up.stderr or "")[-500:],
        pids=pids,
        checks=proc_env_hits,
    )


def main() -> int:
    started = time.time()
    results: list[CaseResult] = []

    compat_dir = Path(tempfile.mkdtemp(prefix="agn_py314_compat_"))
    (compat_dir / "cgi.py").write_text(
        (
            "from email.message import Message\n"
            "from email.parser import Parser\n"
            "def parse_header(line):\n"
            "    if not isinstance(line, str):\n"
            "        line = str(line)\n"
            "    if not line:\n"
            "        return '', {}\n"
            "    msg = Parser().parsestr(f'Content-Type: {line}\\n\\n')\n"
            "    ctype = msg.get_content_type()\n"
            "    params = {k.lower(): v for k, v in msg.get_params()[1:]}\n"
            "    return ctype, params\n"
        ),
        encoding="utf-8",
    )

    def _run_probe(name: str, fn) -> None:  # type: ignore[no-untyped-def]
        try:
            fn()
        except Exception as exc:
            _append_case(
                results,
                f"PROBE-ERROR-{name}",
                "probe_runtime",
                passed=False,
                summary=f"Probe {name} crashed",
                error_type=type(exc).__name__,
                error=str(exc),
            )

    with env_patch({"PYTHONPATH": str(compat_dir)}):
        _run_probe("runtime_prereq", lambda: _probe_runtime_prereq(results, compat_dir))
        _run_probe("command_write_guards", lambda: _probe_command_and_write_guards(results))
        _run_probe("command_request_security", lambda: _probe_command_request_security(results))
        _run_probe("timeout_process_group", lambda: _probe_timeout_process_group(results))
        _run_probe("startup_role_injection", lambda: _probe_startup_role_injection(results, compat_dir))
        _run_probe("real_codex_gemini_flow", lambda: _probe_real_codex_gemini_flow(results, compat_dir))
        _run_probe("info_transfer_prompt_bloat", lambda: _probe_info_transfer_and_prompt_bloat(results, compat_dir))

    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_sec": round(time.time() - started, 2),
        "root": str(ROOT),
        "totals": {
            "all": len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed),
        },
        "results": [asdict(r) for r in results],
    }

    out_path = ROOT / "reports" / f"agn_adversarial_probe_{int(started)}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(out_path), "totals": output["totals"]}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
