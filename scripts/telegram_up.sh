#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT_DIR/.telegram_pids"
LOG_DIR="$ROOT_DIR/reports"

cd "$ROOT_DIR"

mkdir -p "$LOG_DIR"

if [[ -f "$PID_FILE" ]]; then
  echo "pid file already exists: $PID_FILE"
  echo "run make telegram-down first if these processes are stale."
  exit 1
fi

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  resolved_token="$(python3 - <<'PY'
from scripts.research_runtime import resolve_telegram_bot_token
print(resolve_telegram_bot_token(), end="")
PY
)"
  if [[ -n "$resolved_token" ]]; then
    export TELEGRAM_BOT_TOKEN="$resolved_token"
  fi
fi

start_proc() {
  local name="$1"
  shift
  local log_file="$LOG_DIR/${name}.log"
  local pid
  pid="$(
    python3 - "$log_file" "$@" <<'PY'
import subprocess
import sys

log_path = sys.argv[1]
cmd = sys.argv[2:]
with open(log_path, "ab", buffering=0) as handle:
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
print(proc.pid)
PY
  )"
  echo "${name}:${pid}" >>"$PID_FILE"
  echo "started ${name} pid=${pid}"
}

touch "$PID_FILE"

LISTENER_CMD=(python3 scripts/telegram_listener.py --sleep-seconds 1.0)
SENDER_CMD=(python3 scripts/telegram_sender.py --sleep-seconds 1.0)
if [[ "${TELEGRAM_DRY_RUN:-0}" == "1" ]]; then
  SENDER_CMD+=(--dry-run)
fi
if [[ -z "${TELEGRAM_NOTIFY_MODE:-}" ]]; then
  export TELEGRAM_NOTIFY_MODE="explicit"
fi

start_proc "telegram_listener" "${LISTENER_CMD[@]}"
start_proc "telegram_sender" "${SENDER_CMD[@]}"

echo "telegram services started; pid file: $PID_FILE"
