#!/usr/bin/env bash
set -euo pipefail

SOURCE_CODEX_HOME="${SOURCE_CODEX_HOME:-$HOME/.codex}"
AGN_CODEX_HOME="${AGN_CODEX_HOME:-$HOME/.codex_agn}"

mkdir -p "$AGN_CODEX_HOME" \
  "$AGN_CODEX_HOME/shell_snapshots" \
  "$AGN_CODEX_HOME/sessions" \
  "$AGN_CODEX_HOME/log" \
  "$AGN_CODEX_HOME/tmp" \
  "$AGN_CODEX_HOME/rules"

for name in auth.json config.toml AGENTS.md; do
  if [[ -f "$SOURCE_CODEX_HOME/$name" && ! -f "$AGN_CODEX_HOME/$name" ]]; then
    cp "$SOURCE_CODEX_HOME/$name" "$AGN_CODEX_HOME/$name"
  fi
done

export CODEX_HOME="$AGN_CODEX_HOME"
exec codex exec "$@"
