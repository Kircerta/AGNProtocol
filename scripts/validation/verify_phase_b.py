#!/usr/bin/env python3
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any
from uuid import uuid4

import httpx
import jwt

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agn_api.ssot_store import SSOTStore


HOST = "127.0.0.1"
PORT = 8802
BASE_URL = f"http://{HOST}:{PORT}"
JWT_ALGO = "HS256"
DEFAULT_JWT_SECRET = "phase-b-secret-32-bytes-minimum-value"

SSOT_DIR = ROOT / "ssot"
AUDIT_PATH = ROOT / "audit" / "events.jsonl"
REPORT_JSON_PATH = ROOT / "reports" / "phase_B_acceptance.json"
REPORT_MD_PATH = ROOT / "reports" / "phase_B_acceptance.md"
VERIFY_LOG_PATH = ROOT / "reports" / "phase_B_verify.log"

TASK_ID = "phase-b-stream-task"
TOTAL_EVENTS = 20
CLIENT_COUNT = 3



def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()



def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return round(values[0], 2)

    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    fraction = rank - lo
    result = ordered[lo] * (1.0 - fraction) + ordered[hi] * fraction
    return round(result, 2)



def dt_utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)



def ensure_paths() -> None:
    SSOT_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)



def write_seed_task() -> None:
    store = SSOTStore(SSOT_DIR)
    store.save_task(
        {
            "id": TASK_ID,
            "title": "Phase B streaming task",
            "review_requested": True,
        }
    )



def build_jwt(secret: str, subject: str) -> str:
    payload = {
        "sub": subject,
        "exp": dt_utc_now() + timedelta(minutes=15),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGO)



def wait_for_server(timeout_sec: float = 15.0) -> None:
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
    raise RuntimeError("Server did not become ready before timeout")



def parse_sse_lines(lines: Iterable[str]) -> Iterable[tuple[str, str | None, str]]:
    event_name = "message"
    event_id: str | None = None
    data_lines: list[str] = []

    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                yield event_name, event_id, "\n".join(data_lines)
            event_name = "message"
            event_id = None
            data_lines = []
            continue

        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if line.startswith("id:"):
            event_id = line[3:].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
            continue


@dataclass
class ReceivedEvent:
    event_id: str
    payload: dict[str, Any]
    received_ts_utc: str


@dataclass
class SSEClientWorker:
    name: str
    base_url: str
    stop_event: threading.Event
    connected_once: threading.Event = field(default_factory=threading.Event)
    received_by_corr: dict[str, ReceivedEvent] = field(default_factory=dict)
    reconnect_count: int = 0
    read_errors: int = 0
    last_error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = None
    _active_client: httpx.Client | None = None
    _active_response: httpx.Response | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"sse-{self.name}", daemon=True)
        self._thread.start()

    def join(self, timeout: float = 3.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def stop(self) -> None:
        self.stop_event.set()
        with self._lock:
            if self._active_response is not None:
                try:
                    self._active_response.close()
                except Exception:
                    pass
            if self._active_client is not None:
                try:
                    self._active_client.close()
                except Exception:
                    pass

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                with httpx.Client(timeout=httpx.Timeout(connect=3.0, read=None, write=3.0, pool=3.0)) as client:
                    with self._lock:
                        self._active_client = client
                    with client.stream("GET", f"{self.base_url}/api/events", headers={"Accept": "text/event-stream"}) as resp:
                        with self._lock:
                            self._active_response = resp
                        if resp.status_code != 200:
                            self.last_error = f"status={resp.status_code}"
                            self.read_errors += 1
                            self.reconnect_count += 1
                            time.sleep(0.2)
                            continue

                        self.connected_once.set()
                        for event_name, event_id, data in parse_sse_lines(resp.iter_lines()):
                            if self.stop_event.is_set():
                                return
                            if event_name != "task_update":
                                continue

                            try:
                                payload = json.loads(data)
                            except json.JSONDecodeError as exc:
                                self.last_error = f"json_error={exc}"
                                self.read_errors += 1
                                continue

                            corr = payload.get("correlation_id")
                            if not corr:
                                continue

                            rec = ReceivedEvent(
                                event_id=str(payload.get("event_id") or event_id or ""),
                                payload=payload,
                                received_ts_utc=dt_utc_now().isoformat(),
                            )
                            with self._lock:
                                if corr not in self.received_by_corr:
                                    self.received_by_corr[corr] = rec

            except Exception as exc:
                if self.stop_event.is_set():
                    return
                self.last_error = str(exc)
                self.read_errors += 1
                self.reconnect_count += 1
                time.sleep(0.2)
            finally:
                with self._lock:
                    self._active_response = None
                    self._active_client = None



def read_audit_events() -> list[dict[str, Any]]:
    if not AUDIT_PATH.exists():
        return []
    events = []
    for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events



def render_markdown_report(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "# Phase B Acceptance Report",
        "",
        f"- pass: `{report['pass']}`",
        f"- event_count: `{metrics['event_count']}`",
        f"- clients: `{metrics['clients']}`",
        f"- drop_count: `{metrics['drop_count']}`",
        f"- p50_ms: `{metrics['p50_ms']}`",
        f"- p95_ms: `{metrics['p95_ms']}`",
        f"- max_ms: `{metrics['max_ms']}`",
        f"- reconnect_count: `{metrics['reconnect_count']}`",
        "",
        "## Artifacts",
    ]

    for item in report["artifacts"]:
        lines.append(f"- `{item['path']}` sha256=`{item['sha256']}`")

    lines.extend(
        [
            "",
            "## How To Run",
            "- `python3 -m pip install -r requirements.txt`",
            "- `pytest -q`",
            "- `make verify-phase-b`",
        ]
    )

    if report["fail_reasons"]:
        lines.append("")
        lines.append("## Fail Reasons")
        for reason in report["fail_reasons"]:
            lines.append(f"- {reason}")

    return "\n".join(lines) + "\n"



def main() -> int:
    ensure_paths()

    fail_reasons: list[str] = []
    logs: list[str] = []

    def log(msg: str) -> None:
        line = f"[{dt_utc_now().isoformat()}] {msg}"
        logs.append(line)
        print(line)

    metrics: dict[str, Any] = {
        "generated_at_utc": dt_utc_now().isoformat(),
        "event_count": TOTAL_EVENTS,
        "clients": CLIENT_COUNT,
        "expected_deliveries": TOTAL_EVENTS * CLIENT_COUNT,
        "received_deliveries": 0,
        "drop_count": TOTAL_EVENTS * CLIENT_COUNT,
        "p50_ms": 0.0,
        "p95_ms": 0.0,
        "max_ms": 0.0,
        "reconnect_count": 0,
        "read_error_count": 0,
        "audit_event_count": 0,
    }

    AUDIT_PATH.write_text("", encoding="utf-8")
    write_seed_task()

    jwt_secret = os.getenv("JWT_SECRET", DEFAULT_JWT_SECRET)
    reviewer_token = build_jwt(jwt_secret, "phase-b-reviewer")

    env = os.environ.copy()
    env["SSOT_DIR"] = str(SSOT_DIR)
    env["AUDIT_LOG_PATH"] = str(AUDIT_PATH)
    env["JWT_SECRET"] = jwt_secret
    env["JWT_ALGORITHM"] = JWT_ALGO

    proc: subprocess.Popen[str] | None = None
    stop_event = threading.Event()
    clients: list[SSEClientWorker] = []
    expected_order: list[str] = []

    try:
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
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        wait_for_server()
        log("server ready")

        for i in range(CLIENT_COUNT):
            worker = SSEClientWorker(name=f"client-{i+1}", base_url=BASE_URL, stop_event=stop_event)
            worker.start()
            clients.append(worker)

        connect_deadline = time.time() + 10.0
        while time.time() < connect_deadline:
            if all(client.connected_once.is_set() for client in clients):
                break
            time.sleep(0.1)
        else:
            fail_reasons.append("not all SSE clients connected within timeout")

        with httpx.Client(timeout=3.0) as api_client:
            for i in range(TOTAL_EVENTS):
                decision = "approve" if i % 2 == 0 else "reject"
                correlation_id = f"corr-{i:03d}-{uuid4().hex[:8]}"
                expected_order.append(correlation_id)
                route = f"/api/tasks/{TASK_ID}/{decision}"

                headers = {
                    "Authorization": f"Bearer {reviewer_token}",
                    "X-Correlation-ID": correlation_id,
                }
                resp = api_client.post(f"{BASE_URL}{route}", headers=headers)
                if resp.status_code != 200:
                    fail_reasons.append(f"decision request failed ({resp.status_code}) for {decision}:{correlation_id}")
                else:
                    log(f"decision[{i+1}/{TOTAL_EVENTS}] {decision} correlation_id={correlation_id}")

        wait_deadline = time.time() + 20.0
        while time.time() < wait_deadline:
            completed = 0
            for corr in expected_order:
                if all(corr in client.received_by_corr for client in clients):
                    completed += 1
            if completed == len(expected_order):
                break
            time.sleep(0.05)

    except Exception as exc:  # pragma: no cover
        fail_reasons.append(str(exc))
    finally:
        for client in clients:
            client.stop()
        for client in clients:
            client.join(timeout=2.0)

        # Give server a short window to flush disconnect audit events.
        time.sleep(0.35)

        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=4)

        if proc is not None and proc.stdout is not None:
            tail = proc.stdout.read()
            if tail:
                log("server log tail follows")
                for ln in tail.splitlines()[-30:]:
                    log(f"uvicorn: {ln}")

    latencies: list[float] = []
    received_deliveries = 0
    for corr in expected_order:
        for client in clients:
            rec = client.received_by_corr.get(corr)
            if rec is None:
                continue
            received_deliveries += 1
            try:
                server_ts = datetime.fromisoformat(str(rec.payload["server_ts_utc"]))
                recv_ts = datetime.fromisoformat(rec.received_ts_utc)
                latency_ms = max(0.0, (recv_ts - server_ts).total_seconds() * 1000.0)
                latencies.append(latency_ms)
            except Exception:
                fail_reasons.append(f"bad timestamp in event {corr} for {client.name}")

            required_keys = {"event_id", "correlation_id", "task_id", "decision", "server_ts_utc"}
            if not required_keys.issubset(rec.payload):
                fail_reasons.append(f"missing keys in SSE payload for {corr} on {client.name}")

    expected_deliveries = TOTAL_EVENTS * CLIENT_COUNT
    drop_count = expected_deliveries - received_deliveries

    metrics["received_deliveries"] = received_deliveries
    metrics["drop_count"] = drop_count
    metrics["p50_ms"] = percentile(latencies, 0.50)
    metrics["p95_ms"] = percentile(latencies, 0.95)
    metrics["max_ms"] = round(max(latencies), 2) if latencies else 0.0
    metrics["reconnect_count"] = sum(client.reconnect_count for client in clients)
    metrics["read_error_count"] = sum(client.read_errors for client in clients)

    events = read_audit_events()
    metrics["audit_event_count"] = len(events)

    connect_events = [e for e in events if e.get("route") == "/api/events" and e.get("action") == "connect"]
    disconnect_events = [e for e in events if e.get("route") == "/api/events" and e.get("action") == "disconnect"]
    broadcast_events = [e for e in events if e.get("route") == "/api/events" and e.get("action") == "broadcast"]

    metrics["sse_connect_events"] = len(connect_events)
    metrics["sse_disconnect_events"] = len(disconnect_events)
    metrics["sse_broadcast_events"] = len(broadcast_events)

    if len(connect_events) < CLIENT_COUNT:
        fail_reasons.append("audit missing SSE connect events")
    if len(broadcast_events) < TOTAL_EVENTS:
        fail_reasons.append("audit missing SSE broadcast events")

    VERIFY_LOG_PATH.write_text("\n".join(logs) + "\n", encoding="utf-8")

    if drop_count != 0:
        fail_reasons.append(f"drop_count must be 0, got {drop_count}")
    if metrics["p95_ms"] > 1000:
        fail_reasons.append(f"p95_ms must be <= 1000, got {metrics['p95_ms']}")

    artifacts_candidates = [
        AUDIT_PATH,
        VERIFY_LOG_PATH,
        SSOT_DIR / f"{TASK_ID}.json",
    ]
    artifacts: list[dict[str, str]] = []
    for artifact in artifacts_candidates:
        if artifact.exists():
            artifacts.append(
                {
                    "path": str(artifact.relative_to(ROOT)),
                    "sha256": sha256_file(artifact),
                }
            )
        else:
            fail_reasons.append(f"missing artifact: {artifact}")

    passed = len(fail_reasons) == 0
    report = {
        "pass": passed,
        "metrics": metrics,
        "artifacts": artifacts,
        "how_to_run": [
            "python3 -m pip install -r requirements.txt",
            "pytest -q",
            "make verify-phase-b",
        ],
        "fail_reasons": fail_reasons,
    }

    REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    md_text = render_markdown_report(report)
    REPORT_MD_PATH.write_text(md_text, encoding="utf-8")

    report["artifacts"].append(
        {
            "path": str(REPORT_MD_PATH.relative_to(ROOT)),
            "sha256": sha256_file(REPORT_MD_PATH),
        }
    )
    REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
