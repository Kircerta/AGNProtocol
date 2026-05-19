#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / "audit" / "events.jsonl"
REPORT_JSON_PATH = ROOT / "reports" / "phase_E_sse_soak.json"
LOG_PATH = ROOT / "reports" / "phase_E_sse_soak.log"



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


@dataclass
class ClientState:
    client_id: str
    connection_attempts: int = 0
    disconnect_count: int = 0
    reconnect_count: int = 0
    heartbeat_warnings: int = 0
    events_received: int = 0
    last_event_monotonic: float = 0.0
    last_warning_monotonic: float = 0.0


async def consume_sse_client(
    *,
    state: ClientState,
    base_url: str,
    end_monotonic: float,
    stop_event: asyncio.Event,
    latencies_ms: list[float],
    logs: list[str],
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
                    event_name = "message"
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
                                            latency = (datetime.now(tz=timezone.utc) - server_dt).total_seconds() * 1000.0
                                            if latency >= 0:
                                                latencies_ms.append(latency)

                            event_name = "message"
                            data_lines = []
                            continue

                        if line.startswith(":"):
                            continue
                        if line.startswith("event:"):
                            event_name = line[6:].strip()
                            continue
                        if line.startswith("data:"):
                            data_lines.append(line[5:].strip())
                            continue

                    if time.monotonic() < end_monotonic and not stop_event.is_set():
                        disconnected_before_end = True

        except Exception as exc:
            disconnected_before_end = True
            logs.append(f"[{utc_now_iso()}] client={state.client_id} stream_error={type(exc).__name__}")

        if disconnected_before_end:
            state.disconnect_count += 1
            await asyncio.sleep(0.2)


async def heartbeat_monitor(
    *,
    states: list[ClientState],
    stop_event: asyncio.Event,
    end_monotonic: float,
    heartbeat_timeout_seconds: float,
    warning_throttle_seconds: float,
    logs: list[str],
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
                logs.append(
                    f"[{utc_now_iso()}] heartbeat_warning client={state.client_id} stale_seconds={round(stale_for, 2)}"
                )

        await asyncio.sleep(1.0)


async def run_soak(
    *,
    clients: int,
    duration_seconds: int,
    base_url: str,
    heartbeat_timeout_seconds: float,
    warning_throttle_seconds: float,
) -> dict[str, Any]:
    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)

    logs: list[str] = []
    fail_reasons: list[str] = []
    latencies_ms: list[float] = []

    start_iso = utc_now_iso()
    start_monotonic = time.monotonic()
    end_monotonic = start_monotonic + float(duration_seconds)

    stop_event = asyncio.Event()
    states = [ClientState(client_id=f"sse-client-{i+1}") for i in range(clients)]

    tasks = [
        asyncio.create_task(
            consume_sse_client(
                state=state,
                base_url=base_url,
                end_monotonic=end_monotonic,
                stop_event=stop_event,
                latencies_ms=latencies_ms,
                logs=logs,
            )
        )
        for state in states
    ]
    monitor_task = asyncio.create_task(
        heartbeat_monitor(
            states=states,
            stop_event=stop_event,
            end_monotonic=end_monotonic,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
            warning_throttle_seconds=warning_throttle_seconds,
            logs=logs,
        )
    )

    try:
        remaining = max(0.0, end_monotonic - time.monotonic())
        await asyncio.sleep(remaining)
    finally:
        stop_event.set()
        pending_tasks = [*tasks, monitor_task]
        for task in pending_tasks:
            task.cancel()
        done, pending = await asyncio.wait(pending_tasks, timeout=5.0)
        for task in pending:
            task.cancel()

    end_iso = utc_now_iso()

    disconnect_total = sum(s.disconnect_count for s in states)
    reconnect_total = sum(s.reconnect_count for s in states)
    heartbeat_warning_total = sum(s.heartbeat_warnings for s in states)
    events_total = sum(s.events_received for s in states)

    metrics = {
        "started_at_utc": start_iso,
        "ended_at_utc": end_iso,
        "clients": clients,
        "duration_seconds": duration_seconds,
        "events_received_total": events_total,
        "disconnect_count_total": disconnect_total,
        "reconnect_count_total": reconnect_total,
        "heartbeat_warning_count": heartbeat_warning_total,
        "sse_latency_ms": {
            "p50": percentile(latencies_ms, 0.50),
            "p95": percentile(latencies_ms, 0.95),
            "p99": percentile(latencies_ms, 0.99),
            "max": round(max(latencies_ms), 2) if latencies_ms else 0.0,
        },
        "disconnect_count_by_client": {s.client_id: s.disconnect_count for s in states},
        "reconnect_count_by_client": {s.client_id: s.reconnect_count for s in states},
    }

    if events_total == 0:
        fail_reasons.append("no sse events received")

    LOG_PATH.write_text("\n".join(logs) + ("\n" if logs else ""), encoding="utf-8")

    artifacts: list[dict[str, str]] = []
    for path in (AUDIT_PATH, LOG_PATH):
        if path.exists():
            artifacts.append({"path": str(path.relative_to(ROOT)), "sha256": sha256_file(path)})

    report = {
        "pass": len(fail_reasons) == 0,
        "metrics": metrics,
        "artifacts": artifacts,
        "how_to_run": [
            "python3 scripts/validation/soak_sse.py --clients 50 --duration-seconds 120 --base-url http://127.0.0.1:8000",
            "python3 scripts/validation/soak_sse.py --clients 50 --duration-seconds 7200 --base-url http://127.0.0.1:8000",
        ],
        "fail_reasons": fail_reasons,
    }
    REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return report



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SSE soak test")
    parser.add_argument("--clients", type=int, default=50)
    parser.add_argument("--duration-seconds", type=int, default=120)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--heartbeat-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--warning-throttle-seconds", type=float, default=30.0)
    return parser.parse_args()



def main() -> int:
    args = parse_args()
    try:
        report = asyncio.run(
            run_soak(
                clients=args.clients,
                duration_seconds=args.duration_seconds,
                base_url=args.base_url.rstrip("/"),
                heartbeat_timeout_seconds=args.heartbeat_timeout_seconds,
                warning_throttle_seconds=args.warning_throttle_seconds,
            )
        )
        return 0 if report.get("pass") else 1
    except Exception as exc:  # pragma: no cover
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        fatal_line = f"[{utc_now_iso()}] fatal_error={type(exc).__name__}"
        LOG_PATH.write_text(fatal_line + "\n", encoding="utf-8")

        artifacts: list[dict[str, str]] = []
        for path in (AUDIT_PATH, LOG_PATH):
            if path.exists():
                artifacts.append({"path": str(path.relative_to(ROOT)), "sha256": sha256_file(path)})

        report = {
            "pass": False,
            "metrics": {
                "started_at_utc": utc_now_iso(),
                "ended_at_utc": utc_now_iso(),
                "clients": args.clients,
                "duration_seconds": args.duration_seconds,
                "events_received_total": 0,
                "disconnect_count_total": 0,
                "reconnect_count_total": 0,
                "heartbeat_warning_count": 0,
                "sse_latency_ms": {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0},
                "disconnect_count_by_client": {},
                "reconnect_count_by_client": {},
            },
            "artifacts": artifacts,
            "how_to_run": [
                "python3 scripts/validation/soak_sse.py --clients 50 --duration-seconds 120 --base-url http://127.0.0.1:8000",
                "python3 scripts/validation/soak_sse.py --clients 50 --duration-seconds 7200 --base-url http://127.0.0.1:8000",
            ],
            "fail_reasons": [f"soak_exception:{type(exc).__name__}"],
        }
        REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
