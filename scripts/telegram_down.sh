#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT_DIR/.telegram_pids"

if [[ ! -f "$PID_FILE" ]]; then
  echo "no pid file found ($PID_FILE), nothing to stop."
  exit 0
fi

while IFS=: read -r name pid; do
  [[ -z "${pid:-}" ]] && continue
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    echo "stopped ${name} pid=${pid}"
  else
    echo "process already not running: ${name} pid=${pid}"
  fi
done <"$PID_FILE"

sleep 1

while IFS=: read -r name pid; do
  [[ -z "${pid:-}" ]] && continue
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
    echo "force-stopped ${name} pid=${pid}"
  fi
done <"$PID_FILE"

rm -f "$PID_FILE"
echo "telegram shutdown complete"
