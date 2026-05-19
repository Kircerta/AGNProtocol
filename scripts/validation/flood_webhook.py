#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[2]
SSOT_DIR = ROOT / "ssot"
AUDIT_PATH = ROOT / "audit" / "events.jsonl"
REPORT_JSON_PATH = ROOT / "reports" / "phase_E_webhook_flood.json"
REPORT_LOG_PATH = ROOT / "reports" / "phase_E_webhook_flood.log"


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


def write_report(report: dict[str, Any]) -> None:
    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def write_log(lines: list[str]) -> None:
    REPORT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def base_report(*, fail_reasons: list[str], warnings: list[str], metrics: dict[str, Any]) -> dict[str, Any]:
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
            "python3 scripts/validation/flood_webhook.py --rps 50 --duration-seconds 60 --base-url http://127.0.0.1:8000 --secret-env GITHUB_WEBHOOK_SECRET",
        ],
        "fail_reasons": fail_reasons,
    }


async def run_flood(*, rps: int, duration_seconds: int, base_url: str, secret_env: str) -> dict[str, Any]:
    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SSOT_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)

    logs: list[str] = []
    fail_reasons: list[str] = []
    warnings: list[str] = []

    def log(msg: str) -> None:
        line = f"[{utc_now_iso()}] {msg}"
        logs.append(line)
        print(line)

    secret_present = bool(os.getenv(secret_env))
    log(f"env {secret_env} set: {secret_present}")
    log("env values: ***REDACTED***")

    ssot_before = {p.name for p in SSOT_DIR.glob("*.json")}

    tasks_latency_ms: list[float] = []
    responded_statuses: list[int] = []
    send_errors = 0
    timeout_errors = 0
    sample_errors = 0
    total_attempted = 0

    duration_seconds = max(1, duration_seconds)
    rps = max(1, rps)

    timeout = httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=10.0)
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=40)

    end_monotonic = time.monotonic() + float(duration_seconds)
    send_interval = 1.0 / float(rps)

    async def send_invalid(client: httpx.AsyncClient, seq: int) -> None:
        nonlocal send_errors, timeout_errors, total_attempted

        total_attempted += 1
        payload = {
            "event_id": f"flood-{seq}",
            "action": "opened",
            "repository": {"full_name": "load/test"},
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        headers = {
            "X-Hub-Signature-256": "sha256=definitely-invalid-signature",
            "X-GitHub-Delivery": f"flood-{seq}",
            "X-GitHub-Event": "issues",
            "X-Correlation-ID": f"flood-corr-{seq}",
            "Content-Type": "application/json",
        }

        try:
            resp = await client.post(f"{base_url}/webhooks/github", content=body, headers=headers)
            responded_statuses.append(resp.status_code)
        except httpx.TimeoutException:
            timeout_errors += 1
        except Exception:
            send_errors += 1

    async def sender(client: httpx.AsyncClient) -> None:
        seq = 0
        next_tick = time.monotonic()
        while time.monotonic() < end_monotonic:
            await send_invalid(client, seq)
            seq += 1
            next_tick += send_interval
            sleep_for = max(0.0, next_tick - time.monotonic())
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    async def sampler(client: httpx.AsyncClient) -> None:
        nonlocal sample_errors
        while time.monotonic() < end_monotonic:
            t0 = time.perf_counter()
            try:
                resp = await client.get(f"{base_url}/api/tasks")
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                if resp.status_code == 200:
                    tasks_latency_ms.append(elapsed_ms)
                else:
                    sample_errors += 1
            except Exception:
                sample_errors += 1
            await asyncio.sleep(0.05)

    async with (
        httpx.AsyncClient(timeout=timeout, limits=limits) as sender_client,
        httpx.AsyncClient(timeout=timeout, limits=limits) as sampler_client,
    ):
        await asyncio.gather(sender(sender_client), sampler(sampler_client))

    ssot_after = {p.name for p in SSOT_DIR.glob("*.json")}
    ssot_growth = len(ssot_after - ssot_before)

    total_responded = len(responded_statuses)
    rejected_count = sum(1 for status in responded_statuses if status in (401, 403))
    non_rejected_count = sum(1 for status in responded_statuses if status not in (401, 403))
    reject_ratio = (rejected_count / total_responded) if total_responded else 0.0

    if total_responded == 0:
        fail_reasons.append("no webhook responses captured")
    if non_rejected_count != 0:
        fail_reasons.append(f"invalid github webhook returned non-401/403 responses: {non_rejected_count}")
    if ssot_growth != 0:
        fail_reasons.append("invalid github webhook flood caused ssot growth")
    if len(tasks_latency_ms) == 0:
        fail_reasons.append("no /api/tasks latency samples captured")

    if send_errors > 0:
        warnings.append(f"send_errors={send_errors}")
    if timeout_errors > 0:
        warnings.append(f"timeout_errors={timeout_errors}")
    if sample_errors > 0:
        warnings.append(f"sample_errors={sample_errors}")

    log(
        "summary "
        + json.dumps(
            {
                "total_attempted": total_attempted,
                "total_responded": total_responded,
                "rejected_count": rejected_count,
                "non_rejected_count": non_rejected_count,
                "send_errors": send_errors,
                "timeout_errors": timeout_errors,
                "ssot_growth": ssot_growth,
            },
            ensure_ascii=True,
        )
    )

    metrics = {
        "started_at_utc": utc_now_iso(),
        "duration_seconds": duration_seconds,
        "target_rps": rps,
        "total_attempted": total_attempted,
        "total_responded": total_responded,
        "rejected_count": rejected_count,
        "non_rejected_count": non_rejected_count,
        "reject_ratio_responded_only": round(reject_ratio, 6),
        "send_errors": send_errors,
        "timeout_errors": timeout_errors,
        "sample_errors": sample_errors,
        "github_invalid_ssot_growth": ssot_growth,
        "tasks_latency_ms": {
            "p50": percentile(tasks_latency_ms, 0.50),
            "p95": percentile(tasks_latency_ms, 0.95),
            "p99": percentile(tasks_latency_ms, 0.99),
            "max": round(max(tasks_latency_ms), 2) if tasks_latency_ms else 0.0,
            "samples": len(tasks_latency_ms),
        },
    }

    write_log(logs)
    report = base_report(fail_reasons=fail_reasons, warnings=warnings, metrics=metrics)
    write_report(report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Invalid-signature github webhook flood")
    parser.add_argument("--rps", type=int, default=50)
    parser.add_argument("--duration-seconds", type=int, default=60)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--secret-env", default="GITHUB_WEBHOOK_SECRET")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = asyncio.run(
            run_flood(
                rps=args.rps,
                duration_seconds=args.duration_seconds,
                base_url=args.base_url.rstrip("/"),
                secret_env=args.secret_env,
            )
        )
        return 0 if report.get("pass") else 1
    except Exception as exc:  # pragma: no cover
        lines = [
            f"[{utc_now_iso()}] fatal_error={type(exc).__name__}",
            f"[{utc_now_iso()}] env {args.secret_env} set: {bool(os.getenv(args.secret_env))}",
            f"[{utc_now_iso()}] env values: ***REDACTED***",
        ]
        write_log(lines)
        report = base_report(
            fail_reasons=[f"flood_script_exception:{type(exc).__name__}"],
            warnings=[],
            metrics={
                "duration_seconds": max(1, args.duration_seconds),
                "target_rps": max(1, args.rps),
                "total_attempted": 0,
                "total_responded": 0,
                "rejected_count": 0,
                "non_rejected_count": 0,
                "send_errors": 0,
                "timeout_errors": 0,
                "sample_errors": 0,
                "github_invalid_ssot_growth": 0,
                "tasks_latency_ms": {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "samples": 0},
            },
        )
        write_report(report)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
