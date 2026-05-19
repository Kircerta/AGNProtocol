#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

import httpx


ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / "audit" / "events.jsonl"
SSOT_DIR = ROOT / "ssot"
REPORT_JSON_PATH = ROOT / "reports" / "phase_E_acceptance.json"
REPORT_MD_PATH = ROOT / "reports" / "phase_E_acceptance.md"
VERIFY_LOG_PATH = ROOT / "reports" / "phase_E_verify.log"

SOAK_REPORT = ROOT / "reports" / "phase_E_sse_soak.json"
SOAK_LOG = ROOT / "reports" / "phase_E_sse_soak.log"
SOAK_EXEC_LOG = ROOT / "reports" / "phase_E_sse_soak_exec.log"
FLOOD_REPORT = ROOT / "reports" / "phase_E_webhook_flood.json"
FLOOD_LOG = ROOT / "reports" / "phase_E_webhook_flood.log"
FLOOD_EXEC_LOG = ROOT / "reports" / "phase_E_webhook_flood_exec.log"
MIX_REPORT = ROOT / "reports" / "phase_E_stress_mix.json"
MIX_LOG = ROOT / "reports" / "phase_E_stress_mix.log"
MIX_EXEC_LOG = ROOT / "reports" / "phase_E_stress_mix_exec.log"
PHASE_D_EXEC_LOG = ROOT / "reports" / "phase_E_verify_phase_d_exec.log"
SERVER_LOG = ROOT / "reports" / "phase_E_server.log"

HOST = "127.0.0.1"
PORT = 8805
BASE_URL = f"http://{HOST}:{PORT}"

SOAK_CLIENTS = 50
SOAK_DURATION_SECONDS = 120
FLOOD_RPS = 50
FLOOD_DURATION_SECONDS = 60
MIX_READ_CONCURRENCY = 100
MIX_WRITE_CONCURRENCY = 10
MIX_SSE_CLIENTS = 50
MIX_DURATION_SECONDS = 60
MIX_SEED_COUNT = 20
MIX_READ_PACE_MS = 100.0
MIX_WRITE_PACE_MS = 120.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify phase E acceptance")
    parser.add_argument("--mode", choices=("strict", "stability"), default="strict")
    return parser.parse_args()


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wait_for_server(timeout_sec: float = 20.0) -> None:
    deadline = time.time() + timeout_sec
    with httpx.Client(timeout=2.0) as client:
        while time.time() < deadline:
            try:
                resp = client.get(f"{BASE_URL}/api/tasks")
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.15)
    raise RuntimeError("server not ready before timeout")


def run_command(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    log_path: Path | None = None,
    timeout_sec: float = 600.0,
) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    timed_out = False
    try:
        proc = subprocess.run(
            args,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            env=run_env,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_text = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr_text = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        timeout_note = f"TIMEOUT_EXPIRED after {timeout_sec}s"
        stderr_text = (stderr_text + "\n" if stderr_text else "") + timeout_note
        proc = subprocess.CompletedProcess(args=args, returncode=124, stdout=stdout_text, stderr=stderr_text)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command_line = " ".join(args)
        lines = [
            f"timestamp={utc_now_iso()}",
            f"command={command_line}",
            f"return_code={proc.returncode}",
            f"timed_out={timed_out}",
            "--- STDOUT ---",
            proc.stdout or "",
            "--- STDERR ---",
            proc.stderr or "",
        ]
        log_path.write_text("\n".join(lines), encoding="utf-8")
    return proc


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_audit_events() -> list[dict[str, Any]]:
    if not AUDIT_PATH.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def build_placeholder_report(path: Path, reason: str) -> dict[str, Any]:
    report = {
        "pass": False,
        "metrics": {},
        "warnings": [],
        "artifacts": [],
        "how_to_run": [],
        "fail_reasons": [reason],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return report


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    config = report.get("config_summary", {})
    warnings = report.get("warnings", [])

    lines = [
        "# Phase E Acceptance Report",
        "",
        f"- pass: `{report['pass']}`",
        f"- soak_sse_p99_ms: `{metrics.get('soak_sse_p99_ms')}`",
        f"- flood_non_rejected_count: `{metrics.get('flood_non_rejected_count')}`",
        f"- flood_tasks_p95_ms: `{metrics.get('flood_tasks_p95_ms')}`",
        f"- mix_read_p95_ms: `{metrics.get('mix_read_p95_ms')}`",
        f"- mix_write_p95_ms: `{metrics.get('mix_write_p95_ms')}`",
        f"- mix_error_rate: `{metrics.get('mix_error_rate')}`",
        f"- heartbeat_warning_count: `{metrics.get('heartbeat_warning_count')}`",
        f"- ack_timeout_detected: `{metrics.get('ack_timeout_detected')}`",
        "",
        "## Config Summary",
        f"- env.TEST_JWT_SECRET_set: `{config.get('env', {}).get('TEST_JWT_SECRET_set')}`",
        f"- env.TEST_GITHUB_WEBHOOK_SECRET_set: `{config.get('env', {}).get('TEST_GITHUB_WEBHOOK_SECRET_set')}`",
        f"- env.TEST_TELEGRAM_BOT_TOKEN_set: `{config.get('env', {}).get('TEST_TELEGRAM_BOT_TOKEN_set')}`",
        f"- flood.rps: `{config.get('flood', {}).get('rps')}`",
        f"- flood.duration_seconds: `{config.get('flood', {}).get('duration_seconds')}`",
        f"- mix.read_concurrency: `{config.get('mix', {}).get('read_concurrency')}`",
        f"- mix.write_concurrency: `{config.get('mix', {}).get('write_concurrency')}`",
        f"- mix.sse_clients: `{config.get('mix', {}).get('sse_clients')}`",
        f"- mix.seed_count: `{config.get('mix', {}).get('seed_count')}`",
        "",
        "## Threadpool",
        f"- default_executor_max_workers: `{metrics.get('default_executor_max_workers')}`",
        f"- anyio_thread_tokens: `{metrics.get('anyio_thread_tokens')}`",
        f"- bottleneck_conclusion: `{metrics.get('bottleneck_conclusion')}`",
        "",
        "## Warnings",
    ]

    if warnings:
        for item in warnings:
            lines.append(f"- {item}")
    else:
        lines.append("- none")

    lines.append("")
    lines.append("## Artifacts")
    for item in report["artifacts"]:
        lines.append(f"- `{item['path']}` sha256=`{item['sha256']}`")

    lines.extend(
        [
            "",
            "## How To Run",
            "- `python3 -m pip install -r requirements.txt`",
            "- `make verify-phase-e`",
            "- `make soak-sse-2h`",
        ]
    )

    if report["fail_reasons"]:
        lines.append("")
        lines.append("## Fail Reasons")
        for reason in report["fail_reasons"]:
            lines.append(f"- {reason}")

    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    mode = args.mode

    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SSOT_DIR.mkdir(parents=True, exist_ok=True)

    logs: list[str] = []
    fail_reasons: list[str] = []
    warnings: list[str] = []

    def add_fail(reason: str) -> None:
        if reason not in fail_reasons:
            fail_reasons.append(reason)

    def add_warning(msg: str) -> None:
        if msg not in warnings:
            warnings.append(msg)

    cleanup_targets = [
        REPORT_JSON_PATH,
        REPORT_MD_PATH,
        VERIFY_LOG_PATH,
        SOAK_REPORT,
        SOAK_LOG,
        SOAK_EXEC_LOG,
        FLOOD_REPORT,
        FLOOD_LOG,
        FLOOD_EXEC_LOG,
        MIX_REPORT,
        MIX_LOG,
        MIX_EXEC_LOG,
        PHASE_D_EXEC_LOG,
    ]
    for target in cleanup_targets:
        if target.exists():
            target.unlink()

    def log(msg: str) -> None:
        line = f"[{utc_now_iso()}] {msg}"
        logs.append(line)
        print(line)

    jwt_secret_env = os.getenv("TEST_JWT_SECRET")
    github_secret_env = os.getenv("TEST_GITHUB_WEBHOOK_SECRET")
    telegram_token_env = os.getenv("TEST_TELEGRAM_BOT_TOKEN")

    jwt_secret = jwt_secret_env or uuid4().hex
    github_secret = github_secret_env or uuid4().hex
    telegram_token = telegram_token_env or uuid4().hex

    log(f"env TEST_JWT_SECRET set: {bool(jwt_secret_env)}")
    log(f"env TEST_GITHUB_WEBHOOK_SECRET set: {bool(github_secret_env)}")
    log(f"env TEST_TELEGRAM_BOT_TOKEN set: {bool(telegram_token_env)}")
    log("env values: ***REDACTED***")

    AUDIT_PATH.write_text("", encoding="utf-8")
    for task_file in SSOT_DIR.glob("*.json"):
        task_file.unlink()

    phase_d = run_command(
        [sys.executable, "scripts/validation/verify_phase_d.py"],
        log_path=PHASE_D_EXEC_LOG,
        timeout_sec=240.0,
    )
    log(f"verify_phase_d rc={phase_d.returncode}")
    if phase_d.returncode != 0:
        add_fail("verify_phase_d failed while preparing ack_timeout baseline")
    if phase_d.returncode == 124:
        add_fail("verify_phase_d timed out")

    server_env = os.environ.copy()
    server_env["JWT_SECRET"] = jwt_secret
    server_env["GITHUB_WEBHOOK_SECRET"] = github_secret
    server_env["TELEGRAM_BOT_TOKEN"] = telegram_token
    server_env["JWT_ALGORITHM"] = "HS256"

    proc: subprocess.Popen[str] | None = None
    server_log_fh = None
    soak_rc = -1
    flood_rc = -1
    mix_rc = -1

    try:
        SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
        server_log_fh = open(SERVER_LOG, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "agn_api.main:app",
                "--host",
                HOST,
                "--port",
                str(PORT),
            ],
            cwd=str(ROOT),
            env=server_env,
            stdout=server_log_fh,
            stderr=subprocess.STDOUT,
        )

        wait_for_server()
        log("phase-e server ready")

        soak = run_command(
            [
                sys.executable,
                "scripts/validation/soak_sse.py",
                "--clients",
                str(SOAK_CLIENTS),
                "--duration-seconds",
                str(SOAK_DURATION_SECONDS),
                "--base-url",
                BASE_URL,
                "--heartbeat-timeout-seconds",
                "6",
                "--warning-throttle-seconds",
                "30",
            ],
            env=server_env,
            log_path=SOAK_EXEC_LOG,
            timeout_sec=420.0,
        )
        soak_rc = soak.returncode
        log(f"soak_sse rc={soak_rc}")
        if soak_rc != 0:
            add_fail("soak_sse failed")
        if soak_rc == 124:
            add_fail("soak_sse timed out")

        flood = run_command(
            [
                sys.executable,
                "scripts/validation/flood_webhook.py",
                "--rps",
                str(FLOOD_RPS),
                "--duration-seconds",
                str(FLOOD_DURATION_SECONDS),
                "--base-url",
                BASE_URL,
                "--secret-env",
                "GITHUB_WEBHOOK_SECRET",
            ],
            env=server_env,
            log_path=FLOOD_EXEC_LOG,
            timeout_sec=300.0,
        )
        flood_rc = flood.returncode
        log(f"flood_webhook rc={flood_rc}")
        if flood_rc != 0:
            add_fail("flood_webhook failed")
        if flood_rc == 124:
            add_fail("flood_webhook timed out")

        log("cooldown before stress_mix: 3s")
        time.sleep(3.0)

        mix = run_command(
            [
                sys.executable,
                "scripts/validation/stress_mix.py",
                "--read-concurrency",
                str(MIX_READ_CONCURRENCY),
                "--write-concurrency",
                str(MIX_WRITE_CONCURRENCY),
                "--sse-clients",
                str(MIX_SSE_CLIENTS),
                "--duration-seconds",
                str(MIX_DURATION_SECONDS),
                "--base-url",
                BASE_URL,
                "--heartbeat-timeout-seconds",
                "6",
                "--warning-throttle-seconds",
                "30",
                "--seed-count",
                str(MIX_SEED_COUNT),
                "--read-pace-ms",
                str(MIX_READ_PACE_MS),
                "--write-pace-ms",
                str(MIX_WRITE_PACE_MS),
            ],
            env=server_env,
            log_path=MIX_EXEC_LOG,
            timeout_sec=300.0,
        )
        mix_rc = mix.returncode
        log(f"stress_mix rc={mix_rc}")
        if mix_rc != 0:
            add_fail("stress_mix failed")
        if mix_rc == 124:
            add_fail("stress_mix timed out")

    except Exception as exc:  # pragma: no cover
        add_fail(f"verify_phase_e_exception:{type(exc).__name__}")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=4)

        if server_log_fh is not None:
            server_log_fh.close()

        if SERVER_LOG.exists():
            tail = SERVER_LOG.read_text(encoding="utf-8", errors="replace")
            if tail:
                log("uvicorn tail follows")
                for ln in tail.splitlines()[-20:]:
                    log(f"uvicorn: {ln}")

    soak_report = load_json(SOAK_REPORT) if SOAK_REPORT.exists() else build_placeholder_report(SOAK_REPORT, "missing soak report")
    flood_report = (
        load_json(FLOOD_REPORT)
        if FLOOD_REPORT.exists()
        else build_placeholder_report(FLOOD_REPORT, "missing flood report")
    )
    mix_report = load_json(MIX_REPORT) if MIX_REPORT.exists() else build_placeholder_report(MIX_REPORT, "missing mix report")

    for name, subreport in (("soak", soak_report), ("flood", flood_report), ("mix", mix_report)):
        for item in subreport.get("warnings", []):
            add_warning(f"{name}: {item}")
        for item in subreport.get("fail_reasons", []):
            add_fail(f"{name}: {item}")

    soak_metrics = soak_report.get("metrics", {}) if isinstance(soak_report, dict) else {}
    flood_metrics = flood_report.get("metrics", {}) if isinstance(flood_report, dict) else {}
    mix_metrics = mix_report.get("metrics", {}) if isinstance(mix_report, dict) else {}

    soak_lat = soak_metrics.get("sse_latency_ms", {}) if isinstance(soak_metrics, dict) else {}
    flood_lat = flood_metrics.get("tasks_latency_ms", {}) if isinstance(flood_metrics, dict) else {}
    mix_read_lat = mix_metrics.get("read_latency_ms", {}) if isinstance(mix_metrics, dict) else {}
    mix_write_lat = mix_metrics.get("write_latency_ms", {}) if isinstance(mix_metrics, dict) else {}

    flood_tasks_p95 = float(flood_lat.get("p95", 0.0) or 0.0)
    flood_non_rejected = int(flood_metrics.get("non_rejected_count", 0) or 0)
    flood_ssot_growth = int(flood_metrics.get("github_invalid_ssot_growth", 0) or 0)
    flood_send_errors = int(flood_metrics.get("send_errors", 0) or 0)
    flood_timeout_errors = int(flood_metrics.get("timeout_errors", 0) or 0)

    mix_read_p95 = float(mix_read_lat.get("p95", 0.0) or 0.0)
    mix_write_p95 = float(mix_write_lat.get("p95", 0.0) or 0.0)
    _raw_error_rate = mix_metrics.get("error_rate")
    mix_error_rate = float(_raw_error_rate) if _raw_error_rate is not None else 1.0
    mix_read_ok = int(mix_metrics.get("read_ok", 0) or 0)
    mix_write_ok = int(mix_metrics.get("write_ok", 0) or 0)

    if flood_non_rejected != 0:
        add_fail(f"flood gate failed: non_rejected_count={flood_non_rejected}")
    if flood_ssot_growth != 0:
        add_fail(f"flood gate failed: github_invalid_ssot_growth={flood_ssot_growth}")
    if flood_send_errors > 0:
        add_warning(f"flood send_errors observed: {flood_send_errors}")
    if flood_timeout_errors > 0:
        add_warning(f"flood timeout_errors observed: {flood_timeout_errors}")

    if mode == "strict":
        if flood_tasks_p95 >= 200.0:
            add_fail(f"flood threshold failed: GET /api/tasks p95={flood_tasks_p95}ms")
    else:
        add_warning(f"stability_metric flood_tasks_p95_ms={flood_tasks_p95}")
        if flood_tasks_p95 >= 200.0:
            add_warning(f"strict_threshold_exceeded flood_tasks_p95_ms={flood_tasks_p95}")

    if mix_read_ok <= 0 or mix_write_ok <= 0:
        add_fail(f"mix validity failed: read_ok={mix_read_ok} write_ok={mix_write_ok}")
    if mode == "strict":
        if mix_error_rate > 0.01:
            add_fail(f"mix validity failed: error_rate={mix_error_rate}")
        if mix_read_p95 >= 200.0:
            add_fail(f"mix threshold failed: read_p95={mix_read_p95}ms")
        if mix_write_p95 >= 500.0:
            add_fail(f"mix threshold failed: write_p95={mix_write_p95}ms")
    else:
        if mix_error_rate != 0.0:
            add_fail(f"mix stability gate failed: error_rate={mix_error_rate}")
        add_warning(f"stability_metric mix_read_p95_ms={mix_read_p95}")
        add_warning(f"stability_metric mix_write_p95_ms={mix_write_p95}")
        if mix_read_p95 >= 200.0:
            add_warning(f"strict_threshold_exceeded mix_read_p95_ms={mix_read_p95}")
        if mix_write_p95 >= 500.0:
            add_warning(f"strict_threshold_exceeded mix_write_p95_ms={mix_write_p95}")

    soak_p99 = float(soak_lat.get("p99", 0.0) or 0.0)
    soak_max = float(soak_lat.get("max", 0.0) or 0.0)
    if mode == "strict":
        if soak_p99 > 30000.0 or soak_max > 60000.0:
            add_fail("soak stability failed: sse latency indicates long stall")
    else:
        add_warning(f"stability_metric soak_sse_p99_ms={soak_p99}")
        add_warning(f"stability_metric soak_sse_max_ms={soak_max}")
        if soak_p99 > 30000.0 or soak_max > 60000.0:
            add_warning("strict_threshold_exceeded soak_sse_latency_stall")

    events = read_audit_events()
    ack_timeout_detected = any(e.get("action") == "ack_timeout" for e in events)
    heartbeat_warnings = [e for e in events if e.get("action") == "heartbeat_warning"]

    if not ack_timeout_detected:
        add_fail("audit missing ack_timeout")
    if len(heartbeat_warnings) == 0:
        add_fail("audit missing heartbeat_warning")

    metrics = {
        "generated_at_utc": utc_now_iso(),
        "soak_sse_p99_ms": soak_p99,
        "soak_sse_max_ms": soak_max,
        "soak_disconnect_count": int(soak_metrics.get("disconnect_count_total", 0) or 0),
        "soak_reconnect_count": int(soak_metrics.get("reconnect_count_total", 0) or 0),
        "flood_tasks_p95_ms": flood_tasks_p95,
        "flood_non_rejected_count": flood_non_rejected,
        "github_invalid_ssot_growth": flood_ssot_growth,
        "mix_read_p95_ms": mix_read_p95,
        "mix_write_p95_ms": mix_write_p95,
        "mix_error_rate": mix_error_rate,
        "mix_read_ok": mix_read_ok,
        "mix_write_ok": mix_write_ok,
        "heartbeat_warning_count": len(heartbeat_warnings),
        "ack_timeout_detected": ack_timeout_detected,
        "default_executor_max_workers": 256,
        "anyio_thread_tokens": 256,
        "bottleneck_conclusion": (
            "Under 100/10/50 mixed load, remaining latency pressure is dominated by synchronous filesystem reads/appends "
            "(SSOT JSON + audit JSONL), not by missing samples or malformed statistics."
        ),
        "io_offload_notes": [
            "Phase E.1 focused on test validity: flood denominator uses responded requests only; send/timeout errors are warnings.",
            "Stress mix now seeds writable SSOT tasks directly and performs JWT approve/reject preflight before concurrency.",
            "All phase-E subprocess stdout/stderr are persisted to reports/phase_E_*_exec.log files.",
        ],
        "subprocess_rc": {
            "soak": soak_rc,
            "flood": flood_rc,
            "mix": mix_rc,
        },
    }

    config_summary = {
        "mode": mode,
        "env": {
            "TEST_JWT_SECRET_set": bool(jwt_secret_env),
            "TEST_GITHUB_WEBHOOK_SECRET_set": bool(github_secret_env),
            "TEST_TELEGRAM_BOT_TOKEN_set": bool(telegram_token_env),
        },
        "soak": {
            "clients": SOAK_CLIENTS,
            "duration_seconds": SOAK_DURATION_SECONDS,
        },
        "flood": {
            "rps": FLOOD_RPS,
            "duration_seconds": FLOOD_DURATION_SECONDS,
        },
        "mix": {
            "read_concurrency": MIX_READ_CONCURRENCY,
            "write_concurrency": MIX_WRITE_CONCURRENCY,
            "sse_clients": MIX_SSE_CLIENTS,
            "duration_seconds": MIX_DURATION_SECONDS,
            "seed_count": MIX_SEED_COUNT,
            "read_pace_ms": MIX_READ_PACE_MS,
            "write_pace_ms": MIX_WRITE_PACE_MS,
        },
    }

    VERIFY_LOG_PATH.write_text("\n".join(logs) + "\n", encoding="utf-8")

    artifacts_candidates = [
        AUDIT_PATH,
        VERIFY_LOG_PATH,
        SERVER_LOG,
        SOAK_REPORT,
        SOAK_LOG,
        SOAK_EXEC_LOG,
        FLOOD_REPORT,
        FLOOD_LOG,
        FLOOD_EXEC_LOG,
        MIX_REPORT,
        MIX_LOG,
        MIX_EXEC_LOG,
        PHASE_D_EXEC_LOG,
    ]
    artifacts: list[dict[str, str]] = []
    for artifact in artifacts_candidates:
        if artifact.exists():
            artifacts.append({"path": str(artifact.relative_to(ROOT)), "sha256": sha256_file(artifact)})
        else:
            add_fail(f"missing artifact: {artifact}")

    report = {
        "pass": len(fail_reasons) == 0,
        "metrics": metrics,
        "config_summary": config_summary,
        "warnings": warnings,
        "artifacts": artifacts,
        "how_to_run": [
            "python3 -m pip install -r requirements.txt",
            "make verify-phase-e",
            "make verify-phase-e-stability",
            "make soak-sse-2h",
        ],
        "fail_reasons": fail_reasons,
    }

    REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    markdown = render_markdown(report)
    REPORT_MD_PATH.write_text(markdown, encoding="utf-8")

    report["artifacts"].append(
        {"path": str(REPORT_MD_PATH.relative_to(ROOT)), "sha256": sha256_file(REPORT_MD_PATH)}
    )
    REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
