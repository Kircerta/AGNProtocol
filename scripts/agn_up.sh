#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT_DIR/.agn_pids"
LOG_DIR="$ROOT_DIR/reports"

cd "$ROOT_DIR"

mkdir -p "$LOG_DIR"

if [[ -f "$PID_FILE" ]]; then
  echo "pid file already exists: $PID_FILE"
  echo "run make agn-down first if these processes are stale."
  exit 1
fi

resolve_runtime_value() {
  local field="$1"
  python3 - "$field" <<'PY'
import sys
from scripts.research_runtime import (
    resolve_research_publish_branch,
    resolve_research_publish_repo_path,
    resolve_telegram_bot_token,
)

field = sys.argv[1]
if field == "telegram_bot_token":
    print(resolve_telegram_bot_token(), end="")
elif field == "research_repo_path":
    print(resolve_research_publish_repo_path(), end="")
elif field == "research_work_branch":
    print(resolve_research_publish_branch(), end="")
PY
}

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  resolved_token="$(resolve_runtime_value telegram_bot_token)"
  if [[ -n "$resolved_token" ]]; then
    export TELEGRAM_BOT_TOKEN="$resolved_token"
  fi
fi

if [[ -z "${AGN_RESEARCH_REPO_PATH:-}" ]]; then
  resolved_repo_path="$(resolve_runtime_value research_repo_path)"
  if [[ -n "$resolved_repo_path" ]]; then
    export AGN_RESEARCH_REPO_PATH="$resolved_repo_path"
  fi
fi

if [[ -z "${AGN_RESEARCH_WORK_BRANCH:-}" ]]; then
  resolved_work_branch="$(resolve_runtime_value research_work_branch)"
  if [[ -n "$resolved_work_branch" ]]; then
    export AGN_RESEARCH_WORK_BRANCH="$resolved_work_branch"
  fi
fi

if [[ -n "${AGN_RESEARCH_REPO_PATH:-}" && -z "${AGN_DEFAULT_REPO_PATH:-}" ]]; then
  export AGN_DEFAULT_REPO_PATH="$AGN_RESEARCH_REPO_PATH"
fi
if [[ -n "${AGN_RESEARCH_WORK_BRANCH:-}" && -z "${AGN_DEFAULT_WORK_BRANCH:-}" ]]; then
  export AGN_DEFAULT_WORK_BRANCH="$AGN_RESEARCH_WORK_BRANCH"
fi

if [[ "${AGN_GIT_SYNC_ENABLE:-1}" == "1" ]]; then
  echo "running AGN git sync preflight..."
  if ! python3 scripts/agn_git_sync.py preflight; then
    echo "git sync preflight failed; aborting startup."
    exit 1
  fi
fi

# ── AGN2.0 governance initialization ──
# The governance layer (emergency stop, policy gate, constitution, admin
# control daemon, read models) MUST be active before any AGN1.0 subsystem
# workers start.  This ensures the AGN1.0 closed subsystem operates within
# AGN2.0's global governance from the very first tick.
echo "initializing AGN2.0 governance layer..."
if ! python3 scripts/agn2_system.py start; then
  echo "AGN2.0 governance initialization failed; aborting startup."
  exit 1
fi
echo "AGN2.0 governance layer active."

echo "probing provider capabilities..."
if ! python3 scripts/provider_registry.py probe --output "$ROOT_DIR/runtime/provider_capabilities.json"; then
  echo "provider probe failed; aborting startup."
  exit 1
fi

echo "publishing coordinator runtime briefing..."
if ! python3 scripts/network_runtime.py publish --reason startup; then
  echo "runtime briefing publish failed; aborting startup."
  exit 1
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

start_proc "agn_uvicorn"     env AGN_ROLE=admin AGN_RUNTIME_CONTEXT=agn_network AGN_ENFORCE_ROLE_GUARD=1 python3 -m uvicorn agn_api.main:app --host 127.0.0.1 --port 8000
start_proc "agn_coordinator" env AGN_ROLE=coordinator AGN_RUNTIME_CONTEXT=agn_network AGN_ENFORCE_ROLE_GUARD=1 python3 scripts/coordinator_loop.py --interval-seconds 1.0
start_proc "agn_executor"    env AGN_ROLE=executor AGN_RUNTIME_CONTEXT=agn_network AGN_ENFORCE_ROLE_GUARD=1 python3 scripts/executor_worker.py --interval-seconds 1.0
start_proc "agn_reviewer"    env AGN_ROLE=reviewer AGN_RUNTIME_CONTEXT=agn_network AGN_ENFORCE_ROLE_GUARD=1 python3 scripts/reviewer_worker.py --interval-seconds 1.0
if [[ "${AGN_GIT_SYNC_ENABLE:-1}" == "1" ]]; then
  start_proc "agn_git_sync" python3 scripts/agn_git_sync.py loop --interval-seconds "${AGN_GIT_SYNC_INTERVAL_SECONDS:-300}"
fi
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  if [[ -z "${TELEGRAM_NOTIFY_MODE:-}" ]]; then
    export TELEGRAM_NOTIFY_MODE="explicit"
  fi
  start_proc "telegram_listener" python3 scripts/telegram_listener.py --sleep-seconds "${TELEGRAM_LISTENER_INTERVAL_SECONDS:-1.0}"
  start_proc "telegram_sender" python3 scripts/telegram_sender.py --sleep-seconds "${TELEGRAM_SENDER_INTERVAL_SECONDS:-1.0}"
else
  echo "TELEGRAM_BOT_TOKEN not set; skipping telegram listener/sender."
fi
if [[ "${AGN_RESEARCH_AUTONOMY_ENABLE:-1}" == "1" ]]; then
  start_proc "research_autonomy" python3 scripts/research_autonomy.py --interval-seconds "${AGN_RESEARCH_AUTONOMY_INTERVAL_SECONDS:-60}"
fi

echo "all AGN services started; pid file: $PID_FILE"
