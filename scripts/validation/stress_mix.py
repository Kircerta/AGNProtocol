#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from uuid import uuid4

import httpx
import jwt

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agn_api.ssot_store import SSOTStore
from agn_api.task_engine import derive_status


SSOT_DIR = Path(os.getenv("SSOT_DIR", str(ROOT / "ssot")))
AUDIT_PATH = ROOT / "audit" / "events.jsonl"
REPORT_JSON_PATH = ROOT / "reports" / "phase_E_stress_mix.json"
REPORT_LOG_PATH = ROOT / "reports" / "phase_E_stress_mix.log"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    rank = (len(ordered) - 1) * p
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return round((ordered[lo] * (1.0 - frac)) + (ordered[hi] * frac), 2)


def parse_iso8601(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def append_audit_event_sync(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=True))
        f.write("\n")


def write_log(lines: list[str]) -> None:
    REPORT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report(report: dict[str, Any]) -> None:
    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


@dataclass
class SSEClientState:
    client_id: str
    connection_attempts: int = 0
    disconnect_count: int = 0
    reconnect_count: int = 0
    heartbeat_warnings: int = 0
    events_received: int = 0
    last_event_monotonic: float = 0.0
    last_warning_monotonic: float = 0.0


async def sse_client_worker(
    *,
    state: SSEClientState,
    base_url: str,
    end_monotonic: float,
    stop_event: asyncio.Event,
    sse_latencies_ms: list[float],
) -> None:
    timeout = httpx.Timeout(connect=4.0, read=20.0, write=4.0, pool=4.0)

    while not stop_event.is_set() and time.monotonic() < end_monotonic:
        state.connection_attempts += 1
        if state.connection_attempts > 1:
            state.reconnect_count += 1

        disconnected_before_end = False
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("GET", f"{base_url}/api/events", headers={"Accept": "text/event-stream"}) as resp:
                    if resp.status_code != 200:
                        disconnected_before_end = True
                        await asyncio.sleep(0.2)
                        continue

                    state.last_event_monotonic = time.monotonic()
                    data_lines: list[str] = []

                    async for raw_line in resp.aiter_lines():
                        if stop_event.is_set() or time.monotonic() >= end_monotonic:
                            break
                        line = raw_line.rstrip("\r")
                        if line == "":
                            if data_lines:
                                state.events_received += 1
                                state.last_event_monotonic = time.monotonic()
                                data = "\n".join(data_lines)
                                try:
                                    payload = json.loads(data)
                                except json.JSONDecodeError:
                                    payload = None

                                if isinstance(payload, dict):
                                    server_ts = payload.get("server_ts_utc")
                                    if isinstance(server_ts, str):
                                        server_dt = parse_iso8601(server_ts)
                                        if server_dt is not None:
                                            latency_ms = (datetime.now(tz=timezone.utc) - server_dt).total_seconds() * 1000.0
                                            if latency_ms >= 0:
                                                sse_latencies_ms.append(latency_ms)

                            data_lines = []
                            continue

                        if line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            data_lines.append(line[5:].strip())
                            continue

                    if time.monotonic() < end_monotonic and not stop_event.is_set():
                        disconnected_before_end = True

        except Exception:
            disconnected_before_end = True

        if disconnected_before_end:
            state.disconnect_count += 1
            await asyncio.sleep(0.2)


async def heartbeat_monitor(
    *,
    states: list[SSEClientState],
    stop_event: asyncio.Event,
    end_monotonic: float,
    heartbeat_timeout_seconds: float,
    warning_throttle_seconds: float,
) -> None:
    while not stop_event.is_set() and time.monotonic() < end_monotonic:
        now = time.monotonic()
        for state in states:
            if state.last_event_monotonic <= 0:
                continue
            stale_for = now - state.last_event_monotonic
            throttled_for = now - state.last_warning_monotonic
            if stale_for > heartbeat_timeout_seconds and throttled_for >= warning_throttle_seconds:
                state.last_warning_monotonic = now
                state.heartbeat_warnings += 1
                event = {
                    "timestamp": utc_now_iso(),
                    "route": "/api/events",
                    "status": 200,
                    "task_id": None,
                    "action": "heartbeat_warning",
                    "client_id": state.client_id,
                    "stale_seconds": round(stale_for, 2),
                }
                await asyncio.to_thread(append_audit_event_sync, AUDIT_PATH, event)

        await asyncio.sleep(1.0)


async def seed_tasks(*, seed_count: int) -> tuple[list[str], bool, str | None]:
    SSOT_DIR.mkdir(parents=True, exist_ok=True)
    store = SSOTStore(SSOT_DIR)
    task_ids: list[str] = []

    for idx in range(max(1, seed_count)):
        task_id = f"phase-e-seed-{idx + 1}-{uuid4().hex[:8]}"
        task = {
            "id": task_id,
            "source": "phase_e_seed",
            "created_at": utc_now_iso(),
            "correlation_id": f"phase-e-seed-corr-{uuid4().hex[:10]}",
            "review_requested": True,
            "decision": None,
            "status": "pending",
            "payload": {"seed": True, "index": idx + 1},
        }
        task["status"] = derive_status(task)
        await asyncio.to_thread(store.save_task, task)
        task_ids.append(task_id)

    return task_ids, True, None


def make_report(
    *,
    metrics: dict[str, Any],
    warnings: list[str],
    fail_reasons: list[str],
) -> dict[str, Any]:
    artifacts: list[dict[str, str]] = []
    for path in (AUDIT_PATH, REPORT_LOG_PATH):
        if path.exists():
            artifacts.append({"path": str(path.relative_to(ROOT)), "sha256": sha256_file(path)})

    return {
        "pass": len(fail_reasons) == 0,
        "metrics": metrics,
        "warnings": warnings,
        "artifacts": artifacts,
        "how_to_run": [
            "python3 scripts/validation/stress_mix.py --read-concurrency 100 --write-concurrency 10 --sse-clients 50 --duration-seconds 60 --base-url http://127.0.0.1:8000",
        ],
        "fail_reasons": fail_reasons,
    }


async def run_mix(
    *,
    read_concurrency: int,
    write_concurrency: int,
    sse_clients: int,
    duration_seconds: int,
    base_url: str,
    heartbeat_timeout_seconds: float,
    warning_throttle_seconds: float,
    seed_count: int,
    read_pace_ms: float,
    write_pace_ms: float,
) -> dict[str, Any]:
    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)

    logs: list[str] = []
    fail_reasons: list[str] = []
    warnings: list[str] = []

    def log(msg: str) -> None:
        line = f"[{utc_now_iso()}] {msg}"
        logs.append(line)
        print(line)

    jwt_secret = os.getenv("JWT_SECRET")
    jwt_algorithm = os.getenv("JWT_ALGORITHM", "HS256")
    log(f"env JWT_SECRET set: {bool(jwt_secret)}")
    log(f"env JWT_ALGORITHM set: {bool(os.getenv('JWT_ALGORITHM'))}")
    log("env values: ***REDACTED***")

    if not jwt_secret:
        fail_reasons.append("JWT_SECRET is not set")
        metrics = {
            "error": "missing_jwt_secret",
            "seed": {"seeded_task_count": 0, "seed_success": False, "seed_fail_reason": "missing_jwt_secret"},
        }
        write_log(logs)
        report = make_report(metrics=metrics, warnings=warnings, fail_reasons=fail_reasons)
        write_report(report)
        return report

    token_payload = {
        "sub": "phase-e-writer",
        "exp": datetime.now(tz=timezone.utc) + timedelta(minutes=60),
    }
    writer_token = jwt.encode(token_payload, jwt_secret, algorithm=jwt_algorithm)

    read_latencies_ms: list[float] = []
    write_latencies_ms: list[float] = []
    sse_latencies_ms: list[float] = []

    counters = {
        "read_ok": 0,
        "read_err": 0,
        "write_ok": 0,
        "write_err": 0,
        "read_attempted": 0,
        "write_attempted": 0,
    }

    timeout = httpx.Timeout(connect=4.0, read=10.0, write=10.0, pool=10.0)
    max_connections = max(64, read_concurrency + write_concurrency + 40)
    limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=min(max_connections, 120))

    end_monotonic = time.monotonic() + float(max(1, duration_seconds))
    stop_event = asyncio.Event()

    seeded_task_ids: list[str] = []
    seed_success = False
    seed_fail_reason: str | None = None

    preflight_status_counts: dict[str, int] = {}
    preflight_samples: list[dict[str, Any]] = []
    preflight_ok = False

    read_sleep = max(0.0, read_pace_ms) / 1000.0
    write_sleep = max(0.0, write_pace_ms) / 1000.0

    try:
        seeded_task_ids, seed_success, seed_fail_reason = await seed_tasks(seed_count=seed_count)
    except Exception as exc:
        seed_success = False
        seed_fail_reason = f"seed_exception:{type(exc).__name__}"

    if not seed_success or not seeded_task_ids:
        fail_reasons.append("failed to seed writable tasks")
        if seed_fail_reason:
            fail_reasons.append(seed_fail_reason)

    sse_states = [SSEClientState(client_id=f"mix-sse-{i+1}") for i in range(max(0, sse_clients))]

    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        if seeded_task_ids:
            preflight_cases = [
                ("approve", seeded_task_ids[0]),
                ("reject", seeded_task_ids[min(1, len(seeded_task_ids) - 1)]),
            ]
            for endpoint, task_id in preflight_cases:
                sample: dict[str, Any] = {
                    "endpoint": endpoint,
                    "task_id": task_id,
                    "status": "unknown",
                    "body_excerpt": "",
                    "attempts": 0,
                }
                for attempt in range(2):
                    headers = {
                        "Authorization": f"Bearer {writer_token}",
                        "X-Correlation-ID": f"mix-preflight-{endpoint}-{attempt}-{uuid4().hex[:8]}",
                    }
                    sample["attempts"] = attempt + 1
                    try:
                        resp = await client.post(
                            f"{base_url}/api/tasks/{task_id}/{endpoint}",
                            headers=headers,
                            timeout=20.0,
                        )
                        key = str(resp.status_code)
                        preflight_status_counts[key] = preflight_status_counts.get(key, 0) + 1
                        sample["status"] = resp.status_code
                        sample["body_excerpt"] = resp.text[:200]
                        if resp.status_code == 200:
                            break
                    except Exception as exc:
                        key = f"exc:{type(exc).__name__}"
                        preflight_status_counts[key] = preflight_status_counts.get(key, 0) + 1
                        sample["status"] = key
                        sample["body_excerpt"] = "request_exception"

                    if attempt < 1:
                        await asyncio.sleep(0.25)

                preflight_samples.append(sample)

            preflight_ok = all(sample.get("status") == 200 for sample in preflight_samples)
            if not preflight_ok:
                fail_reasons.append("preflight approve/reject failed")
                fail_reasons.append(f"preflight_status_counts={json.dumps(preflight_status_counts, ensure_ascii=True)}")

        if not fail_reasons:
            async def read_worker() -> None:
                while not stop_event.is_set() and time.monotonic() < end_monotonic:
                    counters["read_attempted"] += 1
                    t0 = time.perf_counter()
                    try:
                        resp = await client.get(f"{base_url}/api/tasks")
                        latency_ms = (time.perf_counter() - t0) * 1000.0
                        if resp.status_code == 200:
                            read_latencies_ms.append(latency_ms)
                            counters["read_ok"] += 1
                        else:
                            counters["read_err"] += 1
                    except Exception:
                        counters["read_err"] += 1

                    if read_sleep > 0:
                        await asyncio.sleep(read_sleep)

            async def write_worker(worker_idx: int) -> None:
                step = 0
                while not stop_event.is_set() and time.monotonic() < end_monotonic:
                    task_id = seeded_task_ids[(worker_idx + step) % len(seeded_task_ids)]
                    endpoint = "approve" if step % 2 == 0 else "reject"
                    correlation_id = f"mix-{worker_idx}-{step}-{uuid4().hex[:6]}"
                    headers = {
                        "Authorization": f"Bearer {writer_token}",
                        "X-Correlation-ID": correlation_id,
                    }

                    counters["write_attempted"] += 1
                    t0 = time.perf_counter()
                    try:
                        resp = await client.post(f"{base_url}/api/tasks/{task_id}/{endpoint}", headers=headers)
                        latency_ms = (time.perf_counter() - t0) * 1000.0
                        if resp.status_code == 200:
                            write_latencies_ms.append(latency_ms)
                            counters["write_ok"] += 1
                        else:
                            counters["write_err"] += 1
                    except Exception:
                        counters["write_err"] += 1

                    step += 1
                    if write_sleep > 0:
                        await asyncio.sleep(write_sleep)

            sse_tasks = [
                asyncio.create_task(
                    sse_client_worker(
                        state=state,
                        base_url=base_url,
                        end_monotonic=end_monotonic,
                        stop_event=stop_event,
                        sse_latencies_ms=sse_latencies_ms,
                    )
                )
                for state in sse_states
            ]
            monitor_task = asyncio.create_task(
                heartbeat_monitor(
                    states=sse_states,
                    stop_event=stop_event,
                    end_monotonic=end_monotonic,
                    heartbeat_timeout_seconds=heartbeat_timeout_seconds,
                    warning_throttle_seconds=warning_throttle_seconds,
                )
            )

            read_tasks = [asyncio.create_task(read_worker()) for _ in range(max(1, read_concurrency))]
            write_tasks = [asyncio.create_task(write_worker(i)) for i in range(max(1, write_concurrency))]

            try:
                await asyncio.sleep(max(0.0, end_monotonic - time.monotonic()))
            finally:
                stop_event.set()
                pending_tasks = [*read_tasks, *write_tasks, *sse_tasks, monitor_task]
                for task in pending_tasks:
                    task.cancel()
                _, pending = await asyncio.wait(pending_tasks, timeout=5.0)
                for task in pending:
                    task.cancel()

    total_attempted = counters["read_attempted"] + counters["write_attempted"]
    total_errors = counters["read_err"] + counters["write_err"]
    error_rate = (total_errors / total_attempted) if total_attempted else 1.0

    sse_disconnect_total = sum(s.disconnect_count for s in sse_states)
    sse_reconnect_total = sum(s.reconnect_count for s in sse_states)
    heartbeat_warning_total = sum(s.heartbeat_warnings for s in sse_states)

    if counters["read_ok"] == 0:
        fail_reasons.append("no successful read samples")
    if counters["write_ok"] == 0:
        fail_reasons.append("no successful write samples")
    if error_rate > 0.01:
        fail_reasons.append(f"error_rate too high: {round(error_rate, 6)}")

    metrics = {
        "started_at_utc": utc_now_iso(),
        "duration_seconds": duration_seconds,
        "read_concurrency": read_concurrency,
        "write_concurrency": write_concurrency,
        "sse_clients": sse_clients,
        "read_ok": counters["read_ok"],
        "read_err": counters["read_err"],
        "write_ok": counters["write_ok"],
        "write_err": counters["write_err"],
        "read_attempted": counters["read_attempted"],
        "write_attempted": counters["write_attempted"],
        "error_rate": round(error_rate, 6),
        "read_latency_ms": {
            "p50": percentile(read_latencies_ms, 0.50),
            "p95": percentile(read_latencies_ms, 0.95),
            "p99": percentile(read_latencies_ms, 0.99),
            "max": round(max(read_latencies_ms), 2) if read_latencies_ms else 0.0,
            "samples": len(read_latencies_ms),
        },
        "write_latency_ms": {
            "p50": percentile(write_latencies_ms, 0.50),
            "p95": percentile(write_latencies_ms, 0.95),
            "p99": percentile(write_latencies_ms, 0.99),
            "max": round(max(write_latencies_ms), 2) if write_latencies_ms else 0.0,
            "samples": len(write_latencies_ms),
        },
        "sse": {
            "disconnect_count_total": sse_disconnect_total,
            "reconnect_count_total": sse_reconnect_total,
            "heartbeat_warning_count": heartbeat_warning_total,
            "latency_ms": {
                "p50": percentile(sse_latencies_ms, 0.50),
                "p95": percentile(sse_latencies_ms, 0.95),
                "p99": percentile(sse_latencies_ms, 0.99),
                "max": round(max(sse_latencies_ms), 2) if sse_latencies_ms else 0.0,
                "samples": len(sse_latencies_ms),
            },
        },
        "seed": {
            "seeded_task_count": len(seeded_task_ids),
            "seed_success": seed_success,
            "seed_fail_reason": seed_fail_reason,
        },
        "preflight": {
            "ok": preflight_ok,
            "status_counts": preflight_status_counts,
            "samples": preflight_samples,
        },
        "pace_ms": {
            "read": read_pace_ms,
            "write": write_pace_ms,
        },
    }

    log(
        "summary "
        + json.dumps(
            {
                "seeded": len(seeded_task_ids),
                "preflight_ok": preflight_ok,
                "read_ok": counters["read_ok"],
                "write_ok": counters["write_ok"],
                "error_rate": round(error_rate, 6),
            },
            ensure_ascii=True,
        )
    )

    if heartbeat_warning_total > 0:
        warnings.append(f"heartbeat_warning_count={heartbeat_warning_total}")

    write_log(logs)
    report = make_report(metrics=metrics, warnings=warnings, fail_reasons=fail_reasons)
    write_report(report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mixed read/write/SSE stress test")
    parser.add_argument("--read-concurrency", type=int, default=100)
    parser.add_argument("--write-concurrency", type=int, default=10)
    parser.add_argument("--sse-clients", type=int, default=50)
    parser.add_argument("--duration-seconds", type=int, default=60)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--heartbeat-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--warning-throttle-seconds", type=float, default=30.0)
    parser.add_argument("--seed-count", type=int, default=20)
    parser.add_argument("--read-pace-ms", type=float, default=100.0)
    parser.add_argument("--write-pace-ms", type=float, default=120.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = asyncio.run(
            run_mix(
                read_concurrency=args.read_concurrency,
                write_concurrency=args.write_concurrency,
                sse_clients=args.sse_clients,
                duration_seconds=args.duration_seconds,
                base_url=args.base_url.rstrip("/"),
                heartbeat_timeout_seconds=args.heartbeat_timeout_seconds,
                warning_throttle_seconds=args.warning_throttle_seconds,
                seed_count=args.seed_count,
                read_pace_ms=args.read_pace_ms,
                write_pace_ms=args.write_pace_ms,
            )
        )
        return 0 if report.get("pass") else 1
    except Exception as exc:  # pragma: no cover
        lines = [
            f"[{utc_now_iso()}] fatal_error={type(exc).__name__}",
            f"[{utc_now_iso()}] env JWT_SECRET set: {bool(os.getenv('JWT_SECRET'))}",
            f"[{utc_now_iso()}] env JWT_ALGORITHM set: {bool(os.getenv('JWT_ALGORITHM'))}",
            f"[{utc_now_iso()}] env values: ***REDACTED***",
        ]
        write_log(lines)
        report = make_report(
            metrics={
                "started_at_utc": utc_now_iso(),
                "duration_seconds": args.duration_seconds,
                "seed": {"seeded_task_count": 0, "seed_success": False, "seed_fail_reason": f"exception:{type(exc).__name__}"},
                "read_ok": 0,
                "write_ok": 0,
                "error_rate": 1.0,
            },
            warnings=[],
            fail_reasons=[f"stress_mix_script_exception:{type(exc).__name__}"],
        )
        write_report(report)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
