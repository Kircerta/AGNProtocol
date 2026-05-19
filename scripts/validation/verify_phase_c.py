#!/usr/bin/env python3
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import hmac
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


HOST = "127.0.0.1"
PORT = 8803
BASE_URL = f"http://{HOST}:{PORT}"
JWT_ALGO = "HS256"

SSOT_DIR = ROOT / "ssot"
AUDIT_PATH = ROOT / "audit" / "events.jsonl"
REPORT_JSON_PATH = ROOT / "reports" / "phase_C_acceptance.json"
REPORT_MD_PATH = ROOT / "reports" / "phase_C_acceptance.md"
VERIFY_LOG_PATH = ROOT / "reports" / "phase_C_verify.log"

GITHUB_EVENT_ID = "phase-c-github-event-1"
TELEGRAM_DEDUP_CHAT_ID = 42
TELEGRAM_DEDUP_MESSAGE_ID = 9001
TELEGRAM_CHAIN_CHAT_ID = 77
TELEGRAM_CHAIN_MESSAGE_ID = 9102



def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)



def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()



def clear_runtime_artifacts() -> None:
    SSOT_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)

    for path in SSOT_DIR.glob("*.json"):
        path.unlink(missing_ok=True)
    AUDIT_PATH.write_text("", encoding="utf-8")



def wait_for_server(timeout_sec: float = 15.0) -> None:
    deadline = time.time() + timeout_sec
    with httpx.Client(timeout=2.0) as client:
        while time.time() < deadline:
            try:
                response = client.get(f"{BASE_URL}/api/tasks")
                if response.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.12)
    raise RuntimeError("Server did not become ready before timeout")



def parse_sse(lines: Iterable[str]) -> Iterable[tuple[str, str | None, str]]:
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
class SSEEvent:
    payload: dict[str, Any]
    received_at_utc: str


@dataclass
class SSECapture:
    base_url: str
    stop_event: threading.Event
    connected_once: threading.Event = field(default_factory=threading.Event)
    received: dict[str, SSEEvent] = field(default_factory=dict)
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _client: httpx.Client | None = None
    _response: httpx.Response | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="phase-c-sse")
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self._lock:
            if self._response is not None:
                try:
                    self._response.close()
                except Exception:
                    pass
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass

    def join(self, timeout: float = 3.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def get_by_correlation(self, correlation_id: str) -> SSEEvent | None:
        with self._lock:
            return self.received.get(correlation_id)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                with httpx.Client(timeout=httpx.Timeout(connect=3.0, read=None, write=3.0, pool=3.0)) as client:
                    with self._lock:
                        self._client = client
                    with client.stream("GET", f"{self.base_url}/api/events", headers={"Accept": "text/event-stream"}) as resp:
                        with self._lock:
                            self._response = resp
                        if resp.status_code != 200:
                            time.sleep(0.2)
                            continue

                        self.connected_once.set()
                        for event_name, event_id, data in parse_sse(resp.iter_lines()):
                            if self.stop_event.is_set():
                                return
                            if event_name != "task_update":
                                continue
                            try:
                                payload = json.loads(data)
                            except json.JSONDecodeError:
                                continue

                            corr = payload.get("correlation_id")
                            if not corr:
                                continue

                            with self._lock:
                                if corr not in self.received:
                                    self.received[corr] = SSEEvent(
                                        payload=payload,
                                        received_at_utc=utc_now().isoformat(),
                                    )
            except Exception:
                if self.stop_event.is_set():
                    return
                time.sleep(0.2)
            finally:
                with self._lock:
                    self._response = None
                    self._client = None



def read_audit_events() -> list[dict[str, Any]]:
    if not AUDIT_PATH.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        events.append(json.loads(line))
    return events



def build_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"



def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "# Phase C Acceptance Report",
        "",
        f"- pass: `{report['pass']}`",
        f"- github_invalid_rejected: `{metrics['github_invalid_rejected']}`",
        f"- github_invalid_ssot_growth: `{metrics['github_invalid_ssot_growth']}`",
        f"- github_idempotency_hit_count: `{metrics['github_idempotency_hit_count']}`",
        f"- telegram_dedup_hit_count: `{metrics['telegram_dedup_hit_count']}`",
        f"- chain_2s_pass: `{metrics['chain_2s_pass']}`",
        f"- chain_2s_elapsed_ms: `{metrics['chain_2s_elapsed_ms']}`",
        "",
        "## Env Guard",
        f"- github_webhook_secret_set: `{metrics['env']['github_webhook_secret_set']}`",
        f"- telegram_bot_token_set: `{metrics['env']['telegram_bot_token_set']}`",
        f"- jwt_secret_set: `{metrics['env']['jwt_secret_set']}`",
        "",
        "## Artifacts",
    ]

    for artifact in report["artifacts"]:
        lines.append(f"- `{artifact['path']}` sha256=`{artifact['sha256']}`")

    lines.extend(
        [
            "",
            "## How To Run",
            "- `python3 -m pip install -r requirements.txt`",
            "- `pytest -q`",
            "- `make verify-phase-c`",
        ]
    )

    if report["fail_reasons"]:
        lines.append("")
        lines.append("## Fail Reasons")
        for reason in report["fail_reasons"]:
            lines.append(f"- {reason}")

    return "\n".join(lines) + "\n"



def main() -> int:
    clear_runtime_artifacts()

    logs: list[str] = []
    fail_reasons: list[str] = []

    def log(msg: str) -> None:
        line = f"[{utc_now().isoformat()}] {msg}"
        logs.append(line)
        print(line)

    github_secret = os.getenv("TEST_GITHUB_WEBHOOK_SECRET", "phase-c-github-secret")
    telegram_token = os.getenv("TEST_TELEGRAM_BOT_TOKEN", "phase-c-telegram-token")
    jwt_secret = os.getenv("TEST_JWT_SECRET", "phase-c-jwt-secret")

    metrics: dict[str, Any] = {
        "generated_at_utc": utc_now().isoformat(),
        "github_invalid_rejected": 0,
        "github_invalid_ssot_growth": 0,
        "github_idempotency_hit_count": 0,
        "telegram_dedup_hit_count": 0,
        "chain_2s_pass": False,
        "chain_2s_elapsed_ms": 0.0,
        "audit_event_count": 0,
        "env": {
            "github_webhook_secret_set": bool(github_secret),
            "telegram_bot_token_set": bool(telegram_token),
            "jwt_secret_set": bool(jwt_secret),
        },
    }

    log(f"env set github_secret={metrics['env']['github_webhook_secret_set']} telegram_token={metrics['env']['telegram_bot_token_set']} jwt_secret={metrics['env']['jwt_secret_set']}")
    log("env values: ***REDACTED***")

    env = os.environ.copy()
    env["SSOT_DIR"] = str(SSOT_DIR)
    env["AUDIT_LOG_PATH"] = str(AUDIT_PATH)
    env["JWT_SECRET"] = jwt_secret
    env["JWT_ALGORITHM"] = JWT_ALGO
    env["GITHUB_WEBHOOK_SECRET"] = github_secret
    env["TELEGRAM_BOT_TOKEN"] = telegram_token

    proc: subprocess.Popen[str] | None = None
    sse_stop = threading.Event()
    sse_capture = SSECapture(base_url=BASE_URL, stop_event=sse_stop)

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

        sse_capture.start()
        deadline = time.time() + 8.0
        while time.time() < deadline:
            if sse_capture.connected_once.is_set():
                break
            time.sleep(0.1)
        if not sse_capture.connected_once.is_set():
            fail_reasons.append("SSE client did not connect")

        with httpx.Client(timeout=3.0) as client:
            # 1) Wrong signature test.
            ssot_before = {p.name for p in SSOT_DIR.glob("*.json")}
            invalid_rejected = 0
            for i in range(10):
                body_obj = {"event_id": f"bad-sign-{i}", "action": "opened"}
                body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                headers = {
                    "X-Hub-Signature-256": "sha256=badbadbad",
                    "X-GitHub-Delivery": f"bad-sign-{i}",
                    "X-GitHub-Event": "issues",
                    "Content-Type": "application/json",
                    "X-Correlation-ID": f"bad-corr-{i}",
                }
                resp = client.post(f"{BASE_URL}/webhooks/github", content=body, headers=headers)
                if resp.status_code in (401, 403):
                    invalid_rejected += 1
            ssot_after_invalid = {p.name for p in SSOT_DIR.glob("*.json")}
            metrics["github_invalid_rejected"] = invalid_rejected
            metrics["github_invalid_ssot_growth"] = len(ssot_after_invalid - ssot_before)
            if invalid_rejected != 10:
                fail_reasons.append(f"wrong-signature reject count expected 10, got {invalid_rejected}")
            if metrics["github_invalid_ssot_growth"] != 0:
                fail_reasons.append("wrong-signature requests created new SSOT files")

            events_after_invalid = read_audit_events()
            invalid_reject_events = [
                e
                for e in events_after_invalid
                if e.get("route") == "/webhooks/github" and e.get("action") == "webhook_rejected"
            ]
            invalid_received_events = [
                e
                for e in events_after_invalid
                if e.get("route") == "/webhooks/github" and e.get("action") == "webhook_received"
            ]
            if len(invalid_reject_events) < 10:
                fail_reasons.append("audit missing webhook_rejected events for invalid signature test")
            if invalid_received_events:
                fail_reasons.append("invalid signature test unexpectedly produced webhook_received")

            # 2) event_id idempotency replay test.
            github_body_obj = {
                "event_id": GITHUB_EVENT_ID,
                "action": "opened",
                "repository": {"full_name": "demo/repo"},
            }
            github_body = json.dumps(github_body_obj, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            github_sig = build_signature(github_secret, github_body)

            webhook_task_path = SSOT_DIR / f"github-{GITHUB_EVENT_ID}.json"
            initial_hash: str | None = None
            for i in range(10):
                headers = {
                    "X-Hub-Signature-256": github_sig,
                    "X-GitHub-Delivery": GITHUB_EVENT_ID,
                    "X-GitHub-Event": "issues",
                    "Content-Type": "application/json",
                    "X-Correlation-ID": f"gh-replay-{i}",
                }
                resp = client.post(f"{BASE_URL}/webhooks/github", content=github_body, headers=headers)
                if resp.status_code != 200:
                    fail_reasons.append(f"github replay failed status={resp.status_code}")
                    continue
                if not webhook_task_path.exists():
                    fail_reasons.append("github task file missing after accepted webhook")
                    continue

                current_hash = sha256_file(webhook_task_path)
                if initial_hash is None:
                    initial_hash = current_hash
                elif current_hash != initial_hash:
                    fail_reasons.append("github idempotent replay changed SSOT content")

            events_after_replay = read_audit_events()
            idempotency_hits = [
                e
                for e in events_after_replay
                if e.get("route") == "/webhooks/github"
                and e.get("action") == "idempotency_hit"
                and e.get("event_id") == GITHUB_EVENT_ID
            ]
            metrics["github_idempotency_hit_count"] = len(idempotency_hits)
            if len(idempotency_hits) < 9:
                fail_reasons.append("github replay missing idempotency_hit audit events")

            # 3) Telegram dedup test.
            telegram_payload = {
                "chat_id": TELEGRAM_DEDUP_CHAT_ID,
                "message_id": TELEGRAM_DEDUP_MESSAGE_ID,
                "request_text": "create release checklist",
                "created_at": utc_now().isoformat(),
                "correlation_id": "tg-dedup-corr",
            }
            telegram_task_path = SSOT_DIR / f"telegram-{TELEGRAM_DEDUP_CHAT_ID}-{TELEGRAM_DEDUP_MESSAGE_ID}.json"
            for _ in range(10):
                resp = client.post(f"{BASE_URL}/webhooks/telegram", json=telegram_payload)
                if resp.status_code != 200:
                    fail_reasons.append(f"telegram dedup request failed status={resp.status_code}")

            if not telegram_task_path.exists():
                fail_reasons.append("telegram dedup task file missing")

            telegram_events = read_audit_events()
            dedup_hits = [
                e
                for e in telegram_events
                if e.get("route") == "/webhooks/telegram"
                and e.get("action") == "telegram_dedup_hit"
                and e.get("chat_id") == TELEGRAM_DEDUP_CHAT_ID
                and e.get("message_id") == TELEGRAM_DEDUP_MESSAGE_ID
            ]
            metrics["telegram_dedup_hit_count"] = len(dedup_hits)
            if len(dedup_hits) < 9:
                fail_reasons.append("telegram dedup missing telegram_dedup_hit events")

            # 4) 2-second chain test.
            chain_correlation = f"tg-chain-{uuid4().hex[:8]}"
            chain_payload = {
                "chat_id": TELEGRAM_CHAIN_CHAT_ID,
                "message_id": TELEGRAM_CHAIN_MESSAGE_ID,
                "request_text": "ship phase c",
                "created_at": utc_now().isoformat(),
                "correlation_id": chain_correlation,
            }
            chain_task_path = SSOT_DIR / f"telegram-{TELEGRAM_CHAIN_CHAT_ID}-{TELEGRAM_CHAIN_MESSAGE_ID}.json"

            start = time.perf_counter()
            chain_resp = client.post(f"{BASE_URL}/webhooks/telegram", json=chain_payload)
            if chain_resp.status_code != 200:
                fail_reasons.append(f"2s chain webhook status={chain_resp.status_code}")

            chain_ok = False
            while (time.perf_counter() - start) <= 2.0:
                has_task = chain_task_path.exists()
                latest_events = read_audit_events()
                has_audit = any(
                    e.get("route") == "/webhooks/telegram"
                    and e.get("action") == "telegram_message_received"
                    and e.get("correlation_id") == chain_correlation
                    for e in latest_events
                )
                has_sse = sse_capture.get_by_correlation(chain_correlation) is not None

                if has_task and has_audit and has_sse:
                    chain_ok = True
                    break
                time.sleep(0.03)

            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            metrics["chain_2s_pass"] = chain_ok
            metrics["chain_2s_elapsed_ms"] = elapsed_ms
            if not chain_ok:
                fail_reasons.append("2-second chain requirement not satisfied")
            else:
                sse_event = sse_capture.get_by_correlation(chain_correlation)
                if sse_event is None:
                    fail_reasons.append("missing SSE event for chain correlation")
                else:
                    payload = sse_event.payload
                    if payload.get("source") != "telegram":
                        fail_reasons.append("SSE chain event source mismatch")
                    if payload.get("task_id") != f"telegram-{TELEGRAM_CHAIN_CHAT_ID}-{TELEGRAM_CHAIN_MESSAGE_ID}":
                        fail_reasons.append("SSE chain event task_id mismatch")

    except Exception as exc:  # pragma: no cover
        fail_reasons.append(str(exc))
    finally:
        sse_capture.stop()
        sse_capture.join(timeout=2.0)

        # Ensure disconnect audit has time to flush.
        time.sleep(0.25)

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

    events = read_audit_events()
    metrics["audit_event_count"] = len(events)

    VERIFY_LOG_PATH.write_text("\n".join(logs) + "\n", encoding="utf-8")

    artifacts: list[dict[str, str]] = []
    artifact_candidates = [
        AUDIT_PATH,
        VERIFY_LOG_PATH,
        SSOT_DIR / f"github-{GITHUB_EVENT_ID}.json",
        SSOT_DIR / f"telegram-{TELEGRAM_CHAIN_CHAT_ID}-{TELEGRAM_CHAIN_MESSAGE_ID}.json",
    ]

    for artifact in artifact_candidates:
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
            "make verify-phase-c",
        ],
        "fail_reasons": fail_reasons,
    }

    REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    md_text = render_markdown(report)
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
